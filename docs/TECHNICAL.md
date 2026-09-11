# MikuAgent 技术文档

> 版本：对应提交 `2100fda`
> 环境：Windows 11 build 26200 · Python 3.10.18 · RTX 3050 Ti Laptop 4GB · 驱动 580.97

本文档分三部分：**现状实现**（TTS / Live2D 动作）、**后续需求的可行方案**、**附录工具与踩坑记录**。

文中标注：
- **【实测】** = 本机跑出来的数据，附测量方法
- **【代码】** = 当前代码里的事实
- **【推断】** = 尚未验证的分析与建议

---

## 1. 系统总览

### 1.1 架构

```
                    ┌──────────────────────────────────────┐
   麦克风 ──STT──┐   │            MikuAgent 进程             │
                 │   │                                      │
   键盘输入 ─────┴──▶│  ChatWorker (QThread)                │
                    │      │                               │
                    │      ▼                               │
                    │  DeepSeek API ──▶ 回复文本 + 情感标签  │
                    │      │                               │
                    │      ├──▶ Bubble 打字机显示           │
                    │      ├──▶ Live2DView.set_emotion()   │
                    │      │       └─ 动作 + 表情           │
                    │      └──▶ TtsPipelineWorker (QThread)│
                    │              │                       │
                    │              ▼                       │
                    │        socket 127.0.0.1:18520        │
                    └──────────────┼───────────────────────┘
                                   ▼
                    ┌──────────────────────────────────────┐
                    │   tts_server.py（独立进程）           │
                    │   GPT-SoVITS ──▶ WAV 文件             │
                    └──────────────────────────────────────┘
                                   │
                                   ▼
                    播放(sounddevice) + 口型(WavHandler)
                    读同一份 WAV ──▶ 天然同步
```

### 1.2 组件清单

| 层 | 技术 | 版本 |
| --- | --- | --- |
| UI / 渲染宿主 | PySide6-Essentials | 6.8.3 |
| Live2D 运行时 | live2d-py（Cubism Native SDK 5.1） | 0.7.0.4 |
| OpenGL | PyOpenGL | 3.1.10 |
| LLM | DeepSeek `deepseek-chat`（OpenAI 兼容协议） | — |
| TTS | GSV-TTS-Lite（GPT-SoVITS V2ProPlus） | 0.4.7 |
| TTS 推理后端 | PyTorch + cu128 | 2.11.0 |
| STT | faster-whisper（Whisper `small`） | 1.2.1 |
| 记忆 | SQLite | 3 |
| 音频 | sounddevice / PyAV | 0.5.6 / 17.1.0 |

---

## 2. TTS 实现

### 2.1 引擎与模型

**【代码】** `backend/tts.py` 支持两种引擎，由 `TTS_ENGINE` 选择：

| 引擎 | 实现 | 依赖体积 | 需要 GPU |
| --- | --- | --- | --- |
| `edge`（默认回退） | edge-tts（微软在线） | ~30KB | 否 |
| `sovits`（生产配置） | GSV-TTS-Lite 本地推理 | ~5GB（含 torch） | 是 |

`sovits` 用的模型：

| 组件 | 文件 | 作用 |
| --- | --- | --- |
| GPT 主模型 | `s1v3.ckpt` | 文本 → 语义 token |
| SoVITS 声码器 | `s2Gv2ProPlus.pth` | 语义 token → 波形 |
| BERT | `chinese-roberta-wwm-ext-large` | 中文语义增强（约 1.3GB 磁盘） |
| 音色编码 | `chinese-hubert-base` | 参考音频 → 音色向量 |
| 说话人验证 | `eres2netv2w24s4ep4` | 零样本音色的相似度约束 |

音色为**零样本克隆**：只需 4.2 秒参考音频 `assets/voice/miku_ref.wav`，无需训练。

### 2.2 为什么合成跑在独立进程

**【代码】** `backend/tts_server.py` 是独立进程，主程序通过 TCP JSON-line 协议通信。

原因是一个**无法绕过的运行时冲突**：

> Python 3.10 + torch 2.11 下，在 Qt 应用的**后台线程**里首次 `import torch` 会稳定抛出
> `TypeError: Plain typing.Self is not valid as type argument`
> （`torch/distributed/_pycute/layout.py:91` 用了 Python 3.11+ 才有的 `typing.Self`）。

排查中确认的现象：**同一份代码在脚本、线程、主线程里都正常，只在 Qt 后台线程里崩**。
排除过 import 顺序、并发、延迟（20 秒）、Qt+live2d 共存、SpeechToText 干扰、预导入
`typing_extensions` —— 均无效。最终方案是把推理移出进程。

这个架构带来两个附带要求，都已在代码中处理：

1. **退出必须收掉子进程**。`closeEvent` 里显式调用 `tts.shutdown()`。早期版本漏了这一步
   （`closeEvent` 里调的 `self.shutdown()` 是同名的 `Live2DView.shutdown`），导致退出后
   服务进程被孤立、白占约 1.5GB 显存。**【实测】** 修复前后对比：关闭后残留进程 1 个 / 显存未释放 → 0 个 / 显存回收至 567MiB。
2. **`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`** 必须在子进程 spawn 时设置。
   4GB 显存要和桌宠共享，不设的话 `cache_spk_audio` 阶段会 CUDA OOM。

### 2.3 合成流程

**【代码】** 客户端 `TextToSpeech.synthesize()`：

```
文本
 ├─ normalize_text()       剥离 [情感标签]、颜文字、emoji、日文假名；
 │                         省略号/波浪号 → 逗号；无有效字符则返回 None
 ├─ 截断到 TTS_MAX_CHARS
 ├─ 计算缓存键 sha1(engine|voice|rate|pitch|volume|text)
 │    └─ 命中 data/tts-cache/<hash>.wav → touch mtime 后直接返回
 ├─ 加锁 → 按 engine 分派
 │    ├─ _synth_edge()    edge-tts → mp3 → PyAV 转 WAV
 │    └─ _synth_sovits()  socket 请求 tts_server → 服务端写 WAV
 ├─ _evict_cache()        超 TTS_CACHE_MAX_MB 按 LRU 淘汰
 └─ 返回 (WAV 路径, 时长秒)
```

服务端 `tts_server.py` 的请求格式与处理：

```json
请求: {"text": "...", "emotion": "HAPPY", "speed": 1.08, "out": "data/tts-cache/x.wav"}
响应: {"ok": true, "path": "...", "duration": 8.83}
```

```
emotion ──▶ EMOTION_SPEED ──▶ infer(speed=...)   # 情感 → 语速
infer() ──▶ AudioClip.save(tmp) ──▶ tmp.replace(out)   # 原子落地
```

### 2.4 延迟优化（本会话完成）

#### 优化一：启动预热 CUDA graph

**【实测】** GSV 用静态 CUDA graph 缓存（`gpt_cache` 默认 5 组形状），**每种序列长度的第一次推理都要现捕获计算图**：

| | 第 1 次 | 第 3 次 |
| --- | --- | --- |
| 同一文本合成耗时 | 9.63s | **1.94s** |
| RTF | 0.72 | **0.20** |

因此 `tts_server.py` 在 `listen()` 之前按短/中/长各跑两遍预热：

| | 预热前 | 预热后 |
| --- | --- | --- |
| 短 6 字 | 5.34s (RTF 2.04) | **0.91s (RTF 0.54)** |
| 中 20 字 | 2.23s (RTF 0.47) | **1.80s (RTF 0.34)** |
| 长 57 字 | 9.86s (RTF 0.69) | **3.05s (RTF 0.22)** |

代价：**[实测] 启动多花 12.3 秒**（日志 `预热结束，共 12.3s`）。可用 `TTS_WARMUP=false` 关闭。

#### 优化二：逐句流水线

**【代码】** GPT-SoVITS 是「整段合成完才出声」。`split_sentences()` 把回复按句切分，
`TtsPipelineWorker` 边合成边交给主线程播放：

```
句子1 合成 ──▶ 播放 ──┐
句子2 合成 ────────────┴──▶ 播放 ──┐
句子3 合成 ────────────────────────┴──▶ 播放
```

**【实测】** 94 字回复切成 3 段（8.83 / 8.32 / 3.98 秒），**首字延迟 1.52 秒**（整段合成需约 5 秒）。

切句规则：按 `。！？!?；;\n` 切分；超 40 字的句子再按逗号切；不足 4 字的碎片并入前一段。

### 2.5 实测资源占用

#### 显存拆解

**【实测】** `tools/measure_vram.py base`（BERT 开、只加载不推理）：

| 阶段 | 显存 allocated | 系统可用内存 |
| --- | --- | --- |
| 起始 | 0 | 2343 MB |
| `TTS()` 构造完成后 | **0.61 GB** | 2377 MB |
| + `load_gpt_model()` | **0.97 GB** | 1656 MB |
| + `load_sovits_model()` | **1.15 GB** | 1384 MB |
| + `cache_spk_audio()` | **1.16 GB** | 1023 MB |
| 峰值 | **1.34 GB** | — |
| **GPU 实际占用** | **2254 MiB**（基线 818 MiB，净增约 1.43 GB） | — |

**【实测·本会话早期】** 开启 BERT 并完成推理后整体约 **2.2 GB**，关闭 BERT 约 **1.7 GB**
→ 即 **BERT 常驻约 0.65 GB**。

> ⚠️ **测量注意**：本机可用内存只有约 3 GB，而一次完整 `infer()` 会按需加载
> BERT + hubert + 说话人验证，内存峰值会顶满。我第一版测量脚本在**同一进程内连跑 4 个配置**，
> 直接导致系统内存耗尽、**NVIDIA 驱动失联、PowerShell 模块无法加载**。
> 现版本已加固：默认不推理、每步检查可用内存、低于 2200MB 立即中止。
> **复现测量时务必一次只跑一个配置，并留足内存。**

#### 内存

| 项 | 占用 |
| --- | --- |
| TTS 服务进程 RSS | **2869 MB**【实测】 |
| 桌宠主进程 RSS | 约 780 MB【实测】 |

### 2.6 已知限制

| 限制 | 说明 |
| --- | --- |
| **`TTS_DEVICE=cpu` 不可用** | `gsv_tts` 的 `choose_attention_backend()` 按 `torch.cuda.is_available()` 决策；**有显卡的机器**即使强制 `device=cpu` 也会选到 CUDA 专用的 `CUDNN_ATTENTION`，随后抛 `No viable backend for scaled_dot_product_attention`。无显卡请改用 `edge`。 |
| 参考音频质量 | 4.2 秒、取自导航语音包，是「说话」而非「唱歌」音色 |
| `text_language` 硬编码 `zh` | 日文/英文混排按中文音素处理；`normalize_text` 又会剥离假名 |
| 情感→语速效果有限 | 单次合成时长本身有约 30% 采样波动（同句两次跑 NORMAL 得到 5.24s / 4.02s），情感差异部分被噪声掩盖 |

---

## 3. Live2D 动作实现

### 3.1 资源构成

**【实测】** `tools/analyze_motions.py` 解析结果：**13 个动作，分 6 组**。

| 组 | 数量 | 动作（时长） | 曲线数 | 控制点总数 |
| --- | --- | --- | --- | --- |
| **Idle** | 3 | 07_点头 (1.33s)、14_点头 (2.93s)、09_渐入睡眠 (21.00s) | 20/26/23 | 182/391/765 |
| **Tap** | 3 | 02_高兴 (2.00s)、04_开心 (2.93s)、11_装可爱 (5.63s) | 18/23/24 | 235/316/820 |
| **Flick** | 2 | 01_生气 (2.00s)、10_愤怒 (2.00s) | 22/21 | 538/533 |
| **FlickUp** | 1 | 05_转头 (2.00s) | 20 | 151 |
| **Cry** | 1 | 06_大哭 (6.00s) | 20 | 1310 |
| **Dance** | 3 | 08_走路 (6.07s)、12_活动身体 (7.23s)、13_扭腰 (3.17s) | 22/26/24 | 1196/1047/407 |

另有 **6 个表情**（`exp3.json`）：`Chijing`、`Saihong`、`liuhan`、`Yanjing`、`Dazhihui`、`Mimiyan`。
其中 `Dazhihui`（大眼睛）和 `Mimiyan`（咪咪眼）会**画歪眼睛**，已在代码中禁用 —— 这是本项目
早期「表情重叠」问题的真正原因。

### 3.2 动作数据格式

**【代码 + 实测】** `.motion3.json` **不是逐帧动画，而是参数关键帧曲线**：

```json
{
  "Version": 3,
  "Meta": {
    "Duration": 2, "Fps": 30.0,
    "FadeInTime": 0.0, "FadeOutTime": 0.0,
    "Loop": true, "AreBeziersRestricted": true,
    "CurveCount": 18, "TotalSegmentCount": 37, "TotalPointCount": 99
  },
  "Curves": [
    { "Target": "Model",     "Id": "Opacity",      "Segments": [0, 100, 0, 2, 100] },
    { "Target": "Parameter", "Id": "PARAM_CRY_ON", "Segments": [0, 0, 0, 2, 0] }
  ]
}
```

- `Target` 有两种：`Parameter`（驱动模型参数）、`Model`（模型级属性如 `Opacity`）
- `Segments` 是**扁平数组**：`[t₀, v₀, 类型, ...控制点, t₁, v₁, ...]`
  - `[0, 100, 0, 2, 100]` = t=0 值 100，**线性**（类型 0）到 t=2 值 100
  - 类型：`0` 线性 / `1` 贝塞尔 / `2` 阶梯 / `3` 反贝塞尔
- `Fps: 30` 只是采样基准，运行时按时间插值 —— 所以 60fps 渲染依然平滑

> **关键含义**：动作文件就是「已知参数集上的一组关键帧」。这意味着
> **只要有参数清单，就能纯手写/程序生成动作，不需要 Cubism Editor、不需要重新绑定网格。**

### 3.3 运行时渲染管线

每帧 `model.Update()` 内部的叠加顺序：

```
1. LoadParameters     从上一帧保存的值恢复
2. Expression         表情 exp3 写参数
3. Motion             动作曲线按当前时间插值，写入参数
4. Physics            物理演算：从输入参数推导头发/双马尾摆动
5. SaveParameters     保存供下一帧
6. Draw               用最终参数变形 ArtMesh 顶点 → OpenGL 绘制
```

**因此「播动作」的本质是：曲线按时间采样 → 写参数 → 参数驱动网格形变 → 重绘。**

这解释了一个重要事实：**口型、视线跟随、呼吸与「动作」走的是同一套参数系统**，
只是数据来源不同（音频包络 / 鼠标坐标 / 正弦波 / 关键帧曲线）。

### 3.4 参数系统

**【实测】** 本模型有 **72 个参数**，命名风格**不统一**：

- 绝大多数是**大写** `PARAM_*`：`PARAM_ANGLE_X`、`PARAM_BREATH`、`PARAM_EYE_L_OPEN` …
- 只有 **`ParamMouthOpenY` 是 Cubism 标准命名**

这是一个**极易踩的坑**：Cubism 官方 API 用的标准名（`ParamAngleX`、`ParamBreath`、
`ParamEyeLOpen`）在本模型上**全部不存在**，而 `SetParameterValue` 传不存在的 ID
**不会报错，只是静默无效**。本项目已经发现并修掉两处：

| 位置 | 原写法（无效） | 实际参数名 |
| --- | --- | --- |
| 生气压眉毛 | `ParamBrowLY` / `ParamBrowRY` | `PARAM_BROW_L_Y` / `PARAM_BROW_R_Y` |
| 自动呼吸 | `SetAutoBreathEnable(True)` 内部用 `ParamBreath` | `PARAM_BREATH` |

**已加入的防御**：`resolve_param(*candidates)` / `set_param(value, *candidates)`
按候选名列表解析真实 ID，同时兼容两种命名风格。

#### 物理演算的安全边界

**【实测】** `MIKU.physics3.json` 有 6 组物理设置：

| 类别 | 参数 | 数量 | 能否程序驱动 |
| --- | --- | --- | --- |
| **物理输入** | `PARAM_ANGLE_X/Y/Z`、`PARAM_BODY_ANGLE_X/Y/Z` | 6 | ✅ 安全，物理会据此推导 |
| **物理输出** | `PARAM_HAIR_FLUFFY`、`Param2/3/6/7`、`Param_Angle_Rotation_*_ArtMesh*`（21 个） | 24 | ❌ **不要直接驱动**，会被物理引擎覆盖 |

> 生成动画时的硬约束：**只写输入参数，别写输出参数**。
> `PARAM_BREATH` 不在输出列表里（它是物理的输入侧），所以本项目的呼吸驱动是安全的。

#### 参数分类与取值范围【实测】

**① 头部 / 身体（物理输入，可安全驱动）**

| 参数 | 范围 | 默认 | 含义 |
| --- | --- | --- | --- |
| `PARAM_ANGLE_X` / `Y` / `Z` | -30 ~ 30 | 0 | 头：左右转 / 上下点 / 侧倾 |
| `PARAM_BODY_ANGLE_X` / `Y` / `Z` | -10 ~ 10 | 0 | 身体：前后倾 / 升降 / 左右摆 |
| `PARAM_BODY` | -10 ~ 10 | 0 | 整体位移 |

**② 面部（表情主力）**

| 参数 | 范围 | 默认 | 含义 |
| --- | --- | --- | --- |
| `ParamMouthOpenY` | — | — | 张嘴（**唯一的标准命名**，口型用） |
| `PARAM_EYE_L_OPEN` / `R_OPEN` | 0 ~ 1 | 1 | 眼睛睁开度（眨眼用） |
| `PARAM_EYE_BALL_X` / `Y` | -1 ~ 1 | 0 | 眼球方向 |
| `PARAM_BROW_L_Y` / `R_Y` | -1 ~ 1 | 0 | 眉毛高低（+扬眉 / -皱眉） |
| `PARAM_BROW_L_X` / `R_X` | -1 ~ 1 | 0 | 眉毛左右 |
| `PARAM_BROW_L_ANGLE` / `R_ANGLE` | -1 ~ 1 | 0 | 眉毛倾角 |
| `PARAM_CHEEK` | 0 ~ 1 | 0 | 脸红 |

**③ 情感混合系统（重要，之前未利用）**

模型内置 **6 组情感**，每组 3 个参数 —— `X_ON` 是启用开关，`X1`/`X2` 是两级混合权重，
**全部为 0 ~ 1**：

| 情感组 | 参数 | 范围 |
| --- | --- | --- |
| 花（喜形于色） | `PARAM_HANA_ON` / `PARAM_HANA` / `PARAM_HANA_2` | 0 ~ 1 |
| 恐惧 | `PARAM_FEAR_ON` / `PARAM_FEAR1` / `PARAM_FEAR2` | 0 ~ 1 |
| 惊讶 | `PARAM_SURP_ON` / `PARAM_SURP1` / `PARAM_SURP2` | 0 ~ 1 |
| 哭泣 | `PARAM_CRY_ON` / `PARAM_CRY1` / `PARAM_CRY2` | 0 ~ 1 |
| 开心 | `PARAM_JOY_ON` / `PARAM_JOY1` / `PARAM_JOY2` | 0 ~ 1 |
| 生气 | `PARAM_ANGER_ON` / `PARAM_ANGER1` / `PARAM_ANGER2` | 0 ~ 1 |

> **这是本模型最有价值的发现之一**：18 个参数组成的情感混合系统意味着可以
> **连续插值情感强度**（如 `PARAM_JOY1` = 0.4 vs 0.9 是不同强度的开心），
> 而不是只能切换 `exp3.json` 表情。当前代码只用了后者。
> 而且多组可**同时启用**做复合情绪（如 JOY1=0.6 + CRY1=0.3 = 喜极而泣）。

**④ 四肢与装饰（手势用，之前未利用）**

| 参数 | 范围 | 默认 | 含义 |
| --- | --- | --- | --- |
| `PARAM_ARM_L_01` / `R_01` | -10 ~ 10 | 0 | 左/右手臂 |
| `PARAM_LEG_L_Z` / `R_Z` | -10 ~ 10 | 0 | 左/右腿 |
| `PARAM_NECKTIE` | -1 ~ 1 | 0 | 领带（非物理输出，可驱动） |
| `PARAM_BREATH` | 0 ~ 1 | 0 | 呼吸 |

**⑤ 物理输出（❌ 不可直接驱动）**：`PARAM_HAIR_FLUFFY`、`Param2/3/6/7`、
21 个 `Param_Angle_Rotation_*_ArtMesh*`，共 24 个，见上表。

**⑥ 未明确定义**：`Param19`、`Param27`、`Param32`、`Param4`、`Param5`
—— 名称无语义，含义需通过试值 + 截图确认，**生成动画时不要使用**。

### 3.5 当前动作调用逻辑

**【代码】** 三个触发源，收敛到同一个底层调用：

```
收到回复  →  set_emotion(emotion)   ┐
点击模型  →  click_interaction()    ├→ play_motion(group)
每帧轮询  →  _update_idle()         ┘        ↓
                                    StartMotion / StartRandomMotion
                                             ↓
                                    Update() 按时间插值
```

#### 触发点 1：情感 → 动作组

```python
EMOTION_MOTION = {
    "HAPPY": "Tap",    "MOTIVATED": "Tap",
    "ANGRY": "Flick",  "SURPRISED": "FlickUp",
    "SAD": "Cry",      "EMPATHY": "Idle",  "NORMAL": "Idle",
}
```

7 种情感映射到 5 个组 —— **`Dance` 组完全不在情感映射里**。

#### 触发点 2：点击 → 随机池

```python
CLICK_MOTIONS = ["Tap", "Tap", "Flick", "FlickUp", "Dance", "Idle"]
```

6 项（`Tap` 出现两次 ≈ 权重 2/6），**这是唯一会播 `Dance` 的地方**。

#### 触发点 3：待机调度

每帧检查 `IsMotionFinished()` → 播完隔 2.5~7 秒 → 加权挑一个 Idle 动作。

加权规则 `1/(1+i)`：本模型 Idle 组最后一个 `09_渐入睡眠` 是 **21 秒**，
均匀随机会有 1/3 概率长时间看着不动，加权后降到约 18%。

#### 优先级

| 调用 | priority | 数值 |
| --- | --- | --- |
| 情感动作 / 点击动作 | `NORMAL` | 2 |
| 待机动作 | `IDLE` | 1 |
| （未使用） | `FORCE` | 3 |
| （未使用） | `NONE` | 0 |

#### 待机动画的「僵」是怎么修的

**【实测】** 修复前的状态：**动作只在收到回复和点击时各播一次，播完停在默认姿态**，
`timerEvent` 每帧只做视线跟随 / 口型 / `update()`，**没有任何「接下一个」的调度**。
加上这个模型 `motion3.json` 虽写 `Loop: true` 但**运行时 `IsMotionFinished()` 几秒后就变 True（并不循环）**，
结果就是除了眨眼全静止。

修复后**【实测】** 待机 8 秒内的参数变化幅度：

| 参数 | 幅度 | 来源 |
| --- | --- | --- |
| `PARAM_BREATH` | 1.000 | 手动正弦 |
| `PARAM_ANGLE_Y` | 25.000 | Idle 动作 |
| `PARAM_ANGLE_Z` | 21.000 | Idle 动作 |
| `PARAM_BODY_ANGLE_Z` | 10.000 | Idle 动作 |
| `PARAM_EYE_L_OPEN` | 1.000 | 自动眨眼 |

真机连续 4 帧像素差：**9000~13000 个明显变化像素**【实测】。

### 3.6 四类程序化驱动的实现方式

| 驱动 | 机制 | 参数 | 代码位置 |
| --- | --- | --- | --- |
| **口型** | `WavHandler` 读真实音频包络，逐帧写嘴部参数 | `ParamMouthOpenY` | `timerEvent` + `start_lipsync` |
| **视线跟随** | 鼠标窗口坐标 → `model.Drag(x, y)` | 内部映射到 `PARAM_ANGLE_X/Y`、`PARAM_EYE_BALL_*` | `timerEvent` |
| **呼吸** | 正弦波手动驱动（自动呼吸在本模型失效） | `PARAM_BREATH` | `_update_breath` |
| **待机调度** | 轮询 `IsMotionFinished()` 接续动作 | 走动作文件 | `_update_idle` |

> **口型同步的关键设计**：播放（`sounddevice`）和口型（`WavHandler`）读**同一份 WAV 文件**，
> 因此天然同步，不需要任何对齐算法。若音频不可用，自动退回正弦模拟口型。

### 3.7 当前未利用的模型能力

排查中发现模型有不少能力**代码里完全没用上**，这是后续提升表现力最省力的方向（无需新资源）：

| 能力 | 现状 | 潜在用法 |
| --- | --- | --- |
| **18 个情感混合参数**<br>（`PARAM_{JOY,CRY,ANGER,FEAR,SURP,HANA}_ON/1/2`） | 只用 `exp3.json` 表情切换 | 连续情感强度插值 + 多组叠加做复合情绪（喜极而泣 = JOY+CRY） |
| **`Dance` 组 3 个动作**<br>（合计 16.5 秒动画） | 只在点击池里占 1/6 | 接入情感映射（`MOTIVATED` 时跳舞）、定时表演 |
| **四肢参数**<br>（`PARAM_ARM_L_01/R_01`、`PARAM_LEG_L_Z/R_Z`） | 完全未用 | 招手、叉腰、踏步等手势 |
| **眉毛 X / ANGLE**<br>（`PARAM_BROW_L_X/R_X/L_ANGLE/R_ANGLE`） | 完全未用 | 更细腻的表情（挑眉、困惑） |
| **`PARAM_NECKTIE`** | 完全未用 | 转身/走动时领带飘动的配合 |
| **`LoadExtraMotion()`** 运行时加载动作 | 完全未用 | 动态生成动作文件的落地入口（见 4.2.3） |
| **`SetPartOpacity()` / `SetPartMultiplyColor()`** | 完全未用 | 部件级特效（脸红叠色、出汗） |
| **`HitPart()`** 部件级命中检测 | 用 `glReadPixels` 读 alpha 代替 | 更精确的交互（点头发 vs 点脸） |
| **`StartMotion` 的完成回调**<br>（`onFinishMotionHandler`） | 用 `IsMotionFinished()` 轮询 | 动作串联的精确衔接 |
| **`infer_stream()`** TTS 流式合成 | 用逐句流水线代替 | 首字延迟 1.52s → 约 0.15s |

**【实测】** 其中「情感混合参数」是本次排查最大的意外收获 ——
72 个参数里有 18 个专为情感混合设计，且都是 `0~1` 的连续量，
比 `exp3.json` 的整张表情切换表达力强得多。

---

## 4. 后续需求的可行方案

### 4.1 TTS 显存优化

#### 4.1.1 现状

**【实测】** 当前生产配置（`use_bert=true`，fp16，CUDA）显存构成：

```
GPU 实际占用 2254 MiB（基线 818 MiB，净增约 1436 MiB）
├─ TTS() 构造（含 CUDA graph 预分配）   0.61 GB  ← 最大单块
├─ GPT 主模型                            +0.36 GB
├─ SoVITS 声码器                         +0.18 GB
├─ 音色参考缓存                          +0.01 GB
└─ BERT（推理时按需加载，常驻）          +0.65 GB【早期实测】
```

#### 4.1.2 优化路径

按「收益 / 风险」排序：

| # | 措施 | 预期收益 | 风险 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | **`TTS_USE_BERT=false`** | **-0.65 GB** | 中文韵律略降 | 已有开关，改 `.env` 即生效，零代码改动 |
| 2 | **缩小 `gpt_cache`** | 待测 | 长句可能变慢 | 默认 `[(1,512),(1,768),(1,1024),(4,512),(4,1024)]` 会为 5 种形状预分配 CUDA graph。单句场景 `(4,*)` 两档可能用不上 |
| 3 | **`sovits_cache` 缩小** | 待测 | 同上 | 默认 `[50, 55]` |
| 4 | **`always_load_cnhubert=False` / `always_load_sv=False`** | 待测 | 每次推理多几秒加载 | 已是默认值，需确认实际是否常驻 |
| 5 | **`unload_gpt_model()` / `unload_sovits_model()`** | 空闲时释放 | 下次合成变慢 | 可做「闲置 N 分钟卸载」策略 |
| 6 | **int8 量化** | 可能 -30~50% | 音质风险，需实测 | 需自行改 GSV 加载逻辑 |
| 7 | **改用 fp8 / 更小模型** | 显著 | 音色变化 | V2ProPlus 换 V2 / 蒸馏版 |

**【推断】** 措施 1 是**唯一确定收益且零风险**的一项，建议先做。
措施 2~4 需要用 `tools/measure_vram.py` 逐个测（**注意一次只跑一个配置**，见 2.5 的警告）。

#### 4.1.3 延迟优化（剩余空间）

| 措施 | 预期 | 复杂度 |
| --- | --- | --- |
| **改用 `infer_stream()`** | 首字延迟从数秒降至 **约 0.15 秒** | 中高 |
| 预热扩充句长覆盖 | 减少长句首次的图捕获 | 低 |
| LLM 流式输出 + 边生成边合成 | 省掉 LLM 全量等待（2~4 秒） | 中 |

**【实测】** GSV 提供 `infer_stream(..., stream_mode='token'|'sentence', stream_chunk=25, boost_first_chunk=True)`。

**为什么本会话没做流式**：当前架构是「服务端出 WAV → 客户端播放 + 读同一份 WAV 做口型」，
这个设计让口型天然同步。改成流式需要引入**流式播放器 + 实时包络提取**（替换
`WavHandler` 的文件读取），是对 `ui/audio.py` 和口型链路的较大改动。
逐句流水线已拿到大部分收益（首字 1.52 秒），因此当时判断风险收益比不划算。

### 4.2 Agent 驱动的 Live2D 动画

这是本次调研的核心问题。结论：**可行，而且已有成熟的开源先例。**

#### 4.2.1 核心洞察

**Live2D「动作」= 已知参数集上的一组时间关键帧，不是视频、不是网格动画。**

这个事实来自 3.2 节的数据格式分析，它带来三个结论：

1. **不需要生成模型**。生成动画只需要产出「参数 → 时间曲线」，这是一段结构化数据。
2. **参数空间是有限的、有界的**。本模型 72 个参数，扣掉 24 个物理输出，实际可控约 40 余个，
   每个都有明确的 `[min, max]` —— 这正是 LLM 擅长处理的**结构化受限输出**。
3. **可以纯程序生成，不需要 Cubism Editor**。动作文件就是 JSON，
   已经有人做出「只用文本编辑器 + AI Agent 加动作」的完整工具链。

#### 4.2.2 已有开源实现（重要参考）

**SoulLink_Live2D** — <https://github.com/nanlingyin/SoulLink_Live2D>

LLM 驱动的 Live2D 表情/动作控制系统，思路与本需求完全一致。它的关键设计：

- **不播放注册动作，而是 LLM 直接生成参数值**
- 流程：`收集模型参数 → 嵌入 Prompt → LLM 返回 JSON → 范围校验/钳位 → 缓动过渡`
- 参数列表（含 `min/max/默认值`）动态注入系统提示词
- **两阶段 TTS 连续动作**：① 规划阶段，按语音时长每 2 秒一帧，LLM 生成每帧动作描述；
  ② 参数阶段，LLM 把描述转成 Live2D 参数值；前端逐帧调度播放
- 每个模型可放 `model_prompt.txt` 定制该模型的参数规则
- **物理参数过滤**：排除 `Hair / Ribbon / Skirt / Bust / Sway / Rotation_ / Skinning / Breath`
  —— 与本项目实测的 24 个物理输出参数完全吻合
- 性能建议：缓存常用表情、本地预设兜底、`temperature` 降至 0.1~0.3 提高一致性

**live2d-add-motion-sample-web-ui** — <https://github.com/shinshin86/live2d-add-motion-sample-web-ui>

明确验证了「**只改 JSON 就能给模型加新动作，无需 Cubism Editor**」。它的工程实践值得直接借鉴：

- `analyze_model.py`：先分析可用参数、安全范围、**哪些是物理驱动参数**
- 生成器 + **独立实现的校验器** + 无头浏览器真实渲染验证，三段式流水线
- 附带 `AGENTS.md`，让 AI Agent 能自主完成「加一个新动作」
- 设计规则：值域约束、动作结束要**回到基准姿态**、避开物理参数

**Bunraku**（论文）— 从单张插画生成可编辑的 Live2D 角色。属于「自动化绑定（rigging）」方向，
与本需求不同（我们要的是动作而非模型），但说明**建模环节**也在被自动化。

#### 4.2.3 三层方案对比

| 层 | 做法 | 可控性 | LLM 难度 | 适用 |
| --- | --- | --- | --- | --- |
| **L1 参数级** | LLM 输出 `{参数: 目标值}`，运行时缓动过渡 | 中 | **低** | 表情、情绪反应、姿态微调 |
| **L2 关键帧级** | LLM 输出 `{参数: [(t, v), ...]}` 时序曲线 | 高 | 中 | 招手、点头、转身等具体动作 |
| **L3 动作文件级** | LLM 生成完整 `.motion3.json`，用 `LoadExtraMotion()` 运行时加载 | 最高 | 中高 | 需要复用官方动作系统 / 叠加物理 |

**【实测】关键 API 已确认存在**（`live2d.v3.LAppModel`）：

```python
LoadExtraMotion(group: str, motionJsonPath: str) -> int   # ✅ 运行时加载动作文件！
StartMotion(group, no, priority, onStartMotionHandler, onFinishMotionHandler)
StartRandomMotion(group=None, priority=3, onStart..., onFinish...)
SetParameterValue(paramId: str, value: float, weight: float = 1.0)
SetIndexParamValue(index: int, value: float, weight: float = 1.0)   # 按索引，更快
AddParameterValue(paramId: str, value: float)
AddIndexParamValue(index: int, value: float)
ResetParameters()
GetParamIds() / GetParameter(i) -> {id, min, max, default, value}
IsMotionFinished() / StopAllMotions()
HitPart(x, y, topOnly=False)      # 部件级命中
GetPartIds() / SetPartOpacity() / SetPartMultiplyColor() / SetPartScreenColor()
```

`LoadExtraMotion` 的存在意味着 **L3 完全可行** —— 可以先在临时目录写一个
`.motion3.json`，再注册进模型并播放。

#### 4.2.4 推荐架构

**【推断】** 建议采用 **L1 + L2 为主、L3 为补充** 的混合方案：

```
                    ┌─────────────────────────────────────────┐
   用户输入 ────────▶│  DeepSeek 主模型（一次调用，并行产出）    │
                    │                                         │
                    │  function call / JSON 模式：            │
                    │  {                                      │
                    │    "reply":   "回复文本",               │
                    │    "emotion": "HAPPY",                  │
                    │    "motion_plan": [                     │
                    │      {"t": 0.0, "beats": [              │
                    │         {"name":"head_tilt", "amt":0.6} │
                    │      ]}                                 │
                    │    ]                                    │
                    │  }                                      │
                    └────────────┬────────────────────────────┘
                                 │
              ┌──────────────────┴──────────────────┐
              ▼                                     ▼
    ┌──────────────────┐                 ┌──────────────────────┐
    │ 语义动作 DSL      │                 │  同时启动 TTS 流水线  │
    │  beats → 参数曲线 │                 │  （并行，不等动作）    │
    │  （确定性映射）    │                 └──────────────────────┘
    └────────┬─────────┘
             ▼
    ┌──────────────────────────────────────┐
    │ MotionDirector（每帧驱动）             │
    │  · 与口型/呼吸/视线/物理叠加           │
    │  · 只写「输入参数」，避开 24 个物理输出 │
    │  · 值域钳位 + 缓动 + 自动回基准姿态     │
    └──────────────────────────────────────┘
```

**为什么用「语义 DSL」而不是让 LLM 直接吐参数值**：

| | 直接吐参数值 | 语义 DSL（推荐） |
| --- | --- | --- |
| 可移植性 | 绑死本模型的 `PARAM_*` 命名 | 与命名无关，换模型只改映射表 |
| 幻觉风险 | 可能编出不存在的参数/超范围值 | 只从固定枚举里选 beat |
| 可调性 | 改幅度要改 prompt | 调映射系数即可整体放大/缩小 |
| 时序表达 | LLM 要自己算贝塞尔 | LLM 只需给「第几拍做什么」 |
| 校验成本 | 每个参数都要钳位 | 枚举校验 + 映射表兜底 |

语义 DSL 的 beat 词表建议（**参数名已逐个核实存在**）：

```python
# 每个 beat = 若干 (参数, 方向系数) 的组合；运行时再乘上 amount 与幅度缩放
BEAT_MAP = {
    # ---- 头部 / 身体（物理输入，安全）----
    "head_nod":      [("PARAM_ANGLE_Y", -8)],                                 # 点头
    "head_tilt":     [("PARAM_ANGLE_Z", 12)],                                 # 歪头
    "head_turn":     [("PARAM_ANGLE_X", 15)],                                 # 转头
    "body_sway":     [("PARAM_BODY_ANGLE_Z", 5)],                             # 身体摆
    "lean_forward":  [("PARAM_BODY_ANGLE_X", -4)],                            # 前倾
    "lean_back":     [("PARAM_BODY_ANGLE_X", 4)],                             # 后仰

    # ---- 眼神 ----
    "look_away":     [("PARAM_EYE_BALL_X", -0.7)],
    "look_down":     [("PARAM_EYE_BALL_Y", -0.6)],
    "eyes_wide":     [("PARAM_EYE_L_OPEN", 1.0), ("PARAM_EYE_R_OPEN", 1.0)],  # 睁大
    "eyes_narrow":   [("PARAM_EYE_L_OPEN", 0.4), ("PARAM_EYE_R_OPEN", 0.4)],  # 眯眼

    # ---- 眉毛 ----
    "brow_raise":    [("PARAM_BROW_L_Y", 0.6), ("PARAM_BROW_R_Y", 0.6)],
    "brow_furrow":   [("PARAM_BROW_L_Y", -0.7), ("PARAM_BROW_R_Y", -0.7)],
    "brow_angle":    [("PARAM_BROW_L_ANGLE", 0.5), ("PARAM_BROW_R_ANGLE", 0.5)],

    # ---- 肢体（模型支持，之前完全未用）----
    "arm_up_l":      [("PARAM_ARM_L_01", 6)],
    "arm_up_r":      [("PARAM_ARM_R_01", 6)],
    "leg_step":      [("PARAM_LEG_L_Z", 4), ("PARAM_LEG_R_Z", -4)],

    # ---- 情绪强度（连续插值，不是开关）----
    "blush":         [("PARAM_CHEEK", 0.7)],
    "joy":           [("PARAM_JOY_ON", 1.0), ("PARAM_JOY1", 0.8)],
    "surprise":      [("PARAM_SURP_ON", 1.0), ("PARAM_SURP1", 0.7)],
    "sadness":       [("PARAM_CRY_ON", 1.0), ("PARAM_CRY1", 0.5)],
    "anger":         [("PARAM_ANGER_ON", 1.0), ("PARAM_ANGER1", 0.6)],
    "fear":          [("PARAM_FEAR_ON", 1.0), ("PARAM_FEAR1", 0.5)],
    "delight":       [("PARAM_HANA_ON", 1.0), ("PARAM_HANA", 0.6)],
}
```

> ⚠️ **本表是用 `GetParamIds()` 逐个核实过的**。写这份文档时我的初稿里写了
> `PARAM_MOUTH_FORM`（微笑嘴型）—— **该参数在本模型不存在**。
> 这类错误不会报错、只会静默无效，所以 beat 表**必须**由运行时探测生成，不能手写死。

#### 4.2.5 与对话协同的时序设计

**【推断】** 关键在于**并行**与**对齐**：

```
t=0.0s  用户发送
t=0.0s  ├─ LLM 调用（~2-4s）           ┐ 并行
        └─ （无法提前，依赖 LLM 输出）  ┘
t=3.0s  LLM 返回 {reply, emotion, motion_plan}
t=3.0s  ├─ Bubble 打字机开始           ┐
        ├─ set_emotion() 立即播表情动作 │ 并行
        └─ TTS 流水线启动（首句 ~1.5s） │
t=4.5s  首句出声 ──▶ 口型启动
t=4.5s  MotionDirector 按 motion_plan 逐帧驱动（只写输入参数）
        · 说话期间降幅 ×0.4，避免与口型/物理打架
t=...   播放结束 ──▶ 回到待机调度
```

要点：

1. **表情动作立刻播，语音异步**（当前实现已如此）—— 让反馈不等语音
2. **motion_plan 的时间轴对齐「语音开始」而不是「LLM 返回」**，否则说话前的动作会空转
3. **说话期间动作幅度衰减**，把注意力留给口型；这也避免 `PARAM_ANGLE_*` 大幅变化让物理抖动
4. **失败兜底**：`motion_plan` 解析失败 → 退回当前的 `EMOTION_MOTION` 静态映射
5. **缓存**：`(emotion, beat 序列)` 做 LRU，高频情绪不必每次调 LLM

#### 4.2.6 成本与延迟控制

| 方案 | 额外延迟 | 额外成本 | 说明 |
| --- | --- | --- | --- |
| 复用主回复调用（推荐） | **0** | 0 | 在同一个 function call 里返回 `motion_plan`，不增加 API 调用 |
| 独立并行调用 | 0（并行） | +1 次调用 | 可用更便宜的小模型专门做动作规划 |
| 本地小模型（SLM） | 低 | 无 | SoulLink 的路线图里有本地 Qwen2.5 + LoRA |

**推荐第一种**：把 `motion_plan` 作为主模型 function call 的一个字段，
与 `reply` / `emotion` 一次产出。这样零额外延迟、零额外成本，
与当前「情感标签复用同一次调用」的设计一脉相承。

### 4.3 「模型直接生成动画」的边界

#### 4.3.1 能做到的（按难度递增）

| 层级 | 内容 | 可行性 | 依据 |
| --- | --- | --- | --- |
| ✅ 容易 | 生成**表情参数组合** | 成熟 | SoulLink 已实现并开源 |
| ✅ 容易 | 生成**语义化手势序列**（点头/歪头/转身） | 成熟 | 同上，`live2d-add-motion` 亦验证 |
| ✅ 可行 | 生成**完整 `.motion3.json` 关键帧曲线** | 可行 | `LoadExtraMotion()` **【实测】存在**；格式已完全解析（3.2 节） |
| ⚠️ 有难度 | 与**语音节奏精确对齐**的连续动作 | 需工程 | SoulLink 的两阶段方案（每 2 秒一帧） |
| ⚠️ 有难度 | **风格化/表演性**动作（唱歌跳舞） | 需数据 | 需在动捕或人工标注的动作库上训练 |
| ❌ 不可行 | 生成**新模型本体**（`.moc3`、网格、ArtMesh） | 需建模工具 | 见下 |

#### 4.3.2 做不到的

**LLM 无法直接生成 Live2D 模型本体**，原因是：

1. `.moc3` 是**编译后的二进制**，包含 ArtMesh 顶点、三角剖分、变形器层级、蒙版关系
2. 生成它需要**把一张插画分割成有语义的图层**（头发/眼睛/嘴/身体分离），并建立
   「参数 → 顶点位移」的绑定关系 —— 这是**美术 + 绑定**工作，不是文本生成任务
3. 即便有论文在做自动化绑定（如 Bunraku 从单图生成可编辑 Live2D 角色），
   也远未到「一句话生成一个角色」的程度，且需要图像输入而非文本

**结论**：**动作可以生成，模型不能。** 对当前项目而言，这不构成障碍 ——
我们已经有 Miku 的模型，缺的只是动作的丰富度，而这恰好是可生成的部分。

#### 4.3.3 需要注意的工程约束

**【实测 + 参考】** 生成动画时必须遵守：

1. **只写物理输入，不写物理输出**（本模型 6 个输入 / 24 个输出，见 3.4 节）
2. **值域钳位** —— 超出 `[min, max]` 会导致网格撕裂或渲染异常
3. **动作结束回到基准姿态** —— 否则会累积偏移，模型逐渐"歪掉"
4. **参数名要先探测** —— 本模型 `PARAM_*` 大写风格，标准名全部无效且**静默失败**
5. **验证要独立于生成** —— `live2d-add-motion` 的做法：生成器 + 独立校验器 +
   真实渲染验证三段式。本项目的对应工具是 `tools/diag_idle.py`（参数幅度验证）
   和 `tools/capture_window.ps1`（真机截图）

---

## 5. 附录

### 5.1 工具清单

| 工具 | 用途 |
| --- | --- |
| `tools/measure_framing.py` | 测量模型在指定窗口尺寸下的包围盒，用于精确摆放气泡与模型 |
| `tools/analyze_motions.py` | 解析全部动作的分组/时长/曲线规模，展示 motion3.json 结构 |
| `tools/measure_vram.py` | TTS 显存/内存逐组件拆解（**含内存守卫，一次只跑一个配置**） |
| `tools/diag_idle.py` | 诊断待机动画：呼吸/头身参数是否在动 |
| `tools/diag_idle2.py` | 导出全部参数 ID，测试 Idle 动作是否循环 |
| `tools/diag_zorder.py` | 复现 `WA_AlwaysStackOnTop` 导致的子控件遮挡 |
| `tools/diag_cpu_tts.py` | 排查 CPU 模式为何在有显卡机器上失败 |
| `tools/bench_tts.py` | 量化各长度合成耗时、情感→语速、speed 参数校验 |
| `tools/selftest_chat.py` | 端到端自检（走 `close()` 以便触发 TTS 清理） |
| `tools/capture_window.ps1` | 按 PID 定位窗口并截图（EnumWindows） |

### 5.2 本会话踩过的坑（按代价排序）

| 坑 | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| **`WA_AlwaysStackOnTop`** | 气泡/按钮被模型盖住 | 该属性让 GL 内容无视层叠顺序永远置顶 | 删除该属性；用最小复现确认 |
| **参数名静默失效** | 生气压眉毛、自动呼吸都不生效，也不报错 | 模型用 `PARAM_*`，代码写的是 Cubism 标准名 | `resolve_param()` 候选名解析 |
| **动作无调度** | 待机只有眨眼在动 | 动作只在回复/点击时播一次，播完无接续 | `_update_idle()` 轮询接续 |
| **TTS 进程孤立** | 退出后占 1.5GB 显存 | `tts.shutdown()` 定义了但从未被调用 | 接进 `closeEvent` |
| **托盘僵尸进程** | Alt+F4 后进程不退 | `setQuitOnLastWindowClosed(False)` | `closeEvent` 里显式 `quit()` |
| **窗口尺寸无效** | 改了 `config.py` 窗口还是旧尺寸 | `.env` 覆盖了默认值 | 同步改 `.env` |
| **内存耗尽** | NVIDIA 驱动失联、系统降级 | 测量脚本在单进程内连跑 4 个模型配置 | 加内存守卫、一次一个配置 |

### 5.3 参考资料

- SoulLink_Live2D（LLM 驱动 Live2D 表情控制）：<https://github.com/nanlingyin/SoulLink_Live2D>
- LLM 表情控制原理文档：<https://github.com/nanlingyin/SoulLink_Live2D/blob/main/docs/LLM_EXPRESSION_PRINCIPLE.md>
- 纯 JSON 添加动作 + Agent 工作流：<https://github.com/shinshin86/live2d-add-motion-sample-web-ui>
- Live2D Cubism SDK 官方文档：<https://docs.live2d.com/>
- GSV-TTS-Lite：本地 GPT-SoVITS 高性能推理实现
- live2d-py（Cubism Native SDK 的 Python 绑定）

### 5.4 版权声明

初音未来的音色与形象归 **Crypton Future Media** 所有。
参考音频 `assets/voice/` 已在 `.gitignore` 中，**不随仓库分发**。
模型素材的使用请遵守 Live2D 的许可协议。本项目的相关代码仅供本机个人学习。

---

*文档生成时间：对应提交 `2100fda`*
