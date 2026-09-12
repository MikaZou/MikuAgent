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
| **LLM** | DeepSeek `deepseek-flash`（V4.1-Flash，thinking 关闭） | — |
| TTS | GSV-TTS-Lite（GPT-SoVITS V2ProPlus）；<br>可切换 MiniMax `speech-2.8-hd`（见 5.5 节） | 0.4.7 |
| TTS 推理后端 | PyTorch + cu128 | 2.11.0 |
| STT | faster-whisper（Whisper `small`）；<br>可切换 MiniMax ASR `asr-1.0`（见 5.5 节） | 1.2.1 |
| 视觉 | DeepSeek 原生多模态 + `opencv-python-headless`（可选） | cv2 5.0 |
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
| 参考音频来源 | 已改用 v4c 发布会致辞切片（见 2.1），底噪 −79.6 dBFS、动态范围 62.5 dB |
| `text_language` 硬编码 `zh` | 日文/英文混排按中文音素处理；`normalize_text` 又会剥离假名 |
| 本地 GPT-SoVITS 的情感→语速效果有限 | 单次合成时长本身有约 30% 采样波动（同句两次跑 NORMAL 得到 5.24s / 4.02s），情感差异部分被噪声掩盖；MiniMax 通道另有专门的语速控制（见 2.7） |

### 2.7 语速与情绪的适配（MiniMax 通道）

#### 问题：同一个 `speed`，实际语速却不一样

用户反馈「有时快有时慢」。实测发现 MiniMax 的 `voice_setting.emotion` **不只是音色，
它还会连带改变真实说话快慢**。同一段文本、同一个 `speed=1.8`：

| emotion | 有声字/秒 | 相对 neutral |
| --- | --- | --- |
| neutral | 4.85 | 1.00 |
| sad | 4.40 | 0.91 |
| angry | 5.80 | 1.20 |
| surprised | 6.29 | 1.30 |
| happy | 6.67 | 1.38 |

情绪标签由主模型对每条回复现给，所以相邻两句话的语速会无缘无故地差出 1.5 倍。

> 测量口径：只统计**有声时长**而非文件总时长。否则某个情绪多带一点首尾静音
> 就会被误判成语速变慢（实测各情绪静音长度都在 0.4~0.7s，用总时长会把这点噪声算进语速）。

#### 两个走过一次的弯路

1. **按 `1/倍率` 直接补偿 → 补偿过头。**
   实测 `angry` 的 speed 从 1.8 降到 1.25 后，语速掉到 3.99 字/秒，
   比 `neutral` 的 5.65 还慢。
2. **说明 `speed` 与语速不是线性关系。** 用两组独立数据拟合：

   ```
   angry  : 1.25 → 3.99、1.80 → 6.15  ⇒  指数 1.18
   neutral: 1.00 → 2.60、1.80 → 5.24  ⇒  指数 1.20
   ```

   取 `语速 ∝ speed^1.19`（`MINIMAX_SPEED_EXPONENT`）。补偿必须用
   `期望倍率^(1/1.19)` 而不是 `1/倍率`。

#### 现在的模型：先设计期望曲线，再反解 speed

```
最终语速 = 基准语速 × EMOTION_SPEED_TARGET[情绪] × 句式微调
最终语速 ∝ speed^1.19 × MINIMAX_EMOTION_RATE[mm_emotion]

⇒ speed = MINIMAX_SPEED × (期望倍率 / MiniMax情绪倍率)^(1/1.19)
```

`EMOTION_SPEED_TARGET`（`backend/tts.py`）就是「情绪该有多快」的设计值：

| 情绪 | 期望倍率 | 下发 speed | 反解出的最终相对语速 |
| --- | --- | --- | --- |
| NORMAL | 1.00 | 1.80 | 1.000 |
| HAPPY | 1.08 | 1.61 | 1.080 |
| MOTIVATED | 1.10 | 1.64 | 1.104 |
| ANGRY | 1.12 | 1.73 | 1.120 |
| SURPRISED | 1.06 | 1.53 | 1.060 |
| EMPATHY | 0.94 | 1.71 | 0.941 |
| SAD | 0.90 | 1.75 | 0.898 |

注意这和「把所有情绪拉平成同一语速」是两回事：差异被保留，但幅度从失控的
~1.5 倍收到设计好的 ±12% 以内。**改这张表就能调音色性格。**

#### 句式微调（`CONTENT_SPEED_BIAS`）

让念白跟着句子本身的语气走，幅度都只有几个百分点：

| 触发 | 倍率 | 理由 |
| --- | --- | --- |
| `！` | ×1.04 | 语气上扬，略快 |
| `？` | ×1.03 | 疑问，略快 |
| `……` | ×0.94 | 迟疑／留白，放慢 |
| 字数 ≥ 30 | ×0.95 | 长句放慢便于听清 |

**踩坑**：`normalize_text()` 会把 `…+`、`。{2,}`、`\.{3,}` 统一替换成 `，`
（这是为了让 TTS 自然停顿）。所以句式判断**必须用归一化之前的原始文本**——
`synthesize()` 通过 `bias_source=` 把原文本传进 `_synth_minimax()`。
用归一化后的文本判断的话，省略号规则永远不会命中（实测 SAD 句的 speed
停在 1.75 而不是应有的 1.66）。

#### 关掉 / 复测

- `MINIMAX_SPEED_NORMALIZE=false`：完全用原始 `speed`，恢复 MiniMax 的野生行为
- `MINIMAX_CONTENT_BIAS=false`：只用情绪曲线，不加句式微调
- `python tools/measure_emotion_speed.py`：复测（默认绕过缓存、交叉轮询采样）
  - `--raw` 看未补偿的原始倍率（用来重标 `MINIMAX_EMOTION_RATE`）
  - `-n 5 --delay 2` 增加采样、放慢请求避开 RPM 限流

#### 测量精度说明

MiniMax 对**完全相同**的请求本身就有 5~9% 的随机波动（个别情绪到 14%）。
所以设计出来的 ±12% 差异处在噪声边缘，单次采样偏差在 ±15% 内都算命中。
若要把语速压到完全一致，需要本地做时长规整（时间伸缩），代价是可能引入
音质损伤——当前选择不做。

#### 顺带修掉的限流问题

MiniMax 在并发稍高时会返回 **HTTP 200 但 `base_resp.status_code=1002`
（rate limit exceeded/RPM）**。原先被当成硬失败，结果那一句**直接没有声音**。
现在 `_minimax_post()` 对 1002 / 1039 / 1042 做指数退避重试（`MINIMAX_RETRIES=3`，
1.5s → 3s），鉴权/余额/音色这类硬错误不重试。

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
| **`LoadExtraMotion()`** 运行时加载动作 | 完全未用 | 动态生成动作文件的落地入口（见 5.2.3） |
| **`SetPartOpacity()` / `SetPartMultiplyColor()`** | 完全未用 | 部件级特效（脸红叠色、出汗） |
| **`HitPart()`** 部件级命中检测 | 用 `glReadPixels` 读 alpha 代替 | 更精确的交互（点头发 vs 点脸） |
| **`StartMotion` 的完成回调**<br>（`onFinishMotionHandler`） | 用 `IsMotionFinished()` 轮询 | 动作串联的精确衔接 |
| **`infer_stream()`** TTS 流式合成 | 用逐句流水线代替 | 首字延迟 1.52s → 约 0.15s |

**【实测】** 其中「情感混合参数」是本次排查最大的意外收获 ——
72 个参数里有 18 个专为情感混合设计，且都是 `0~1` 的连续量，
比 `exp3.json` 的整张表情切换表达力强得多。

---

## 4. 视频对话（视觉）

让 Miku 在语音轮次里「看见」主人。对标豆包的行为：**在用户说完话的那一刻抓取单帧**，
随这一轮对话一起发给模型。模型侧仍是单帧，但采集自动化，形成「视频对话」体感。

### 4.1 核心数据流

```
【视频模式开启】
   按住 🎤 ──▶ stt.start()
   松开   ──▶ stop_voice_input()        ← 「说完那一刻」
                ├─▶ TranscribeWorker（Whisper，约 1s）  ┐
                └─▶ take_latest()（取缓存帧，约 0ms）   ├─▶ 都就绪
                                                        ┘
                    ──▶ send_message(text, frame)
                          │
                    DeepSeek 多模态请求（1 图 + 文本 + tools）
                          │
                    ──▶ [情感标签] 回复 ──▶ Live2D / TTS
```

**为什么在 `stop_voice_input()` 抓而不是转写完成后抓**：转写要 1 秒左右。在松开瞬间抓帧
既更贴合「说完那一刻」的画面，又能与转写并行 —— **零额外延迟**。

**为什么不另开 Worker 现抓**：`cv2.VideoCapture` 不是线程安全的。由 `CameraWorker`
独占设备并持续缓存最新一帧，抓帧时直接读缓存（最多落后 `1/VISION_PREVIEW_FPS` 秒，
默认 200ms），**不去抢设备**，因此没有采集延迟。

### 4.2 DeepSeek 视觉 API 规格

| 项 | 值 |
| --- | --- |
| 支持视觉的模型 | **`deepseek-flash`（DeepSeek-V4.1-Flash）**；`deepseek-v4-pro` **不支持** |
| 投递方式 | base64 data URL / 外部 URL / Files API `file_id` |
| 图片格式 | JPEG、PNG、GIF、WebP（按内容嗅探，不看扩展名） |
| `detail` | `low`（缩到 512×512）/ `high` / `original` / `auto` |
| 单图上限 | 32 MiB（base64/URL）、64 MiB（Files API） |
| 请求体上限 | 48 MiB；单请求最多 600 张 |
| 尺寸上限 | 每边 8192px（≥15 张图时降到 4096px） |
| **限制** | **图片只能出现在 `user` 消息里**，放进 system/assistant 会返回 400 |

**token 计费**：图片按尺寸折算 token，先做一次归一化 —— 小于约 544×544 的会被**放大**，
大于的按比例缩到约 1300×1300 的总像素量，因此**单张图有 1024 token 的上限**。

### 4.3 本项目的实现与实测

| 项 | 实现 / 实测 |
| --- | --- |
| 投递方式 | base64 data URL（本地文件，最省事） |
| `detail` | `low` |
| 发送前处理 | 等比缩放到 `VISION_MAX_SIDE`（默认 768），JPEG 质量 80 |
| **实测 token 成本** | **约 184~192 tokens/张**（同提示词带图 vs 不带图对照） |
| 历史存储 | 只存 `f"{文本} [图片]"` 占位符，**绝不存 base64** |
| 图片落盘 | 无（仅内存） |

**【实测·集成验证】** 造一张合成人像（圆脸 + 眼睛 + 微笑 + 红色衣服）走完整链路：

> `[HAPPY]` 喔～主人今天在笑呢，眼睛圆圆的好可爱☆ 不过画面里只看到一个大大的笑脸和红色的小方块……

她正确描述了画面内容，且情感标签解析、function calling、长期记忆引用都正常。

### 4.4 三路采集

| 来源 | 依赖 | 说明 |
| --- | --- | --- |
| 📹 摄像头 | `opencv-python-headless`（**可选**） | 视频模式下常开，`CameraWorker` 独占 |
| 🖥️ 截屏 | **无**（`QScreen.grabWindow`） | Qt 有 GUI 线程亲和性，故在主线程直接调用 |
| 📋 剪贴板 | **无**（`QClipboard.image`） | 截屏失败时的兜底 |

**为什么用 headless 版 OpenCV**：`opencv-python` 会捆绑自己的一套 Qt，和本项目的
PySide6 放一起容易插件冲突；headless 不含 GUI 代码，只提供 `VideoCapture`。
**为什么不用 QtMultimedia**：它在 `PySide6-Addons` 里（约 168MB），而本项目刻意只装 Essentials。

**踩到的坑**：OpenCV 5.0 的 **DSHOW 后端不支持按索引打开**
（`backend is generally available but can't be used to capture by index`），
必须走默认后端（Windows 上是 MSMF）。`CameraSession.open()` 因此按「默认 → MSMF」依次尝试。

**另一个坑**：`camera_available()` 刻意用 `importlib.util.find_spec("cv2")` 而**不 import cv2** ——
OpenCV 导入时会初始化 OpenCL，在共享 GPU 的机器上可能干扰另一个进程里的 CUDA 推理；
不用摄像头的用户也不该为它付出启动开销。

### 4.5 隐私设计

- 摄像头**只在视频模式开启期间**打开，关闭立即 `release()`，LED 熄灭
- **每轮只上传 1 帧**，不是视频流
- 帧只存在于内存，**不写磁盘、不入库、不打日志**
- 设置面板明确告知「画面会上传至 DeepSeek 云端用于识别」
- 默认关闭，状态持久化在 `QSettings`

### 4.6 已知问题：TTS 与桌宠争 GPU

**现象**：桌宠运行期间，合成服务在 `cache_spk_audio` 阶段抛
`torch.AcceleratorError: CUDA error: unknown error`（在 `ERes2NetV2` 的 `batch_norm` 内核）。

**已确认的事实**：
- 服务**单独运行完全正常**（`python backend/tts_server.py`，24 秒就绪、预热 11.9 秒）
- 故障只在**和桌宠（60fps OpenGL 渲染）同时跑**时出现
- **用 `git stash` 回退到加视觉功能之前的代码，故障完全一样** —— 不是本功能引入的
- 当时 RAM 4.5GB、显存仅占 565MiB，**不是资源耗尽**

**推断**：WDDM 下 OpenGL 渲染与 CUDA 在同一块 4GB 笔记本 GPU 上的互操作问题，
或该机曾发生的驱动异常留下的状态降级。**缓解**：关掉占显存的程序后重启桌宠；
`TTS_USE_BERT=false` 可再省约 0.65GB。

---

## 5. 后续需求的可行方案

### 5.1 TTS 显存优化

#### 5.1.1 现状

**【实测】** 当前生产配置（`use_bert=true`，fp16，CUDA）显存构成：

```
GPU 实际占用 2254 MiB（基线 818 MiB，净增约 1436 MiB）
├─ TTS() 构造（含 CUDA graph 预分配）   0.61 GB  ← 最大单块
├─ GPT 主模型                            +0.36 GB
├─ SoVITS 声码器                         +0.18 GB
├─ 音色参考缓存                          +0.01 GB
└─ BERT（推理时按需加载，常驻）          +0.65 GB【早期实测】
```

#### 5.1.2 优化路径

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

#### 5.1.3 延迟优化（剩余空间）

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

### 5.2 Agent 驱动的 Live2D 动画

这是本次调研的核心问题。结论：**可行，而且已有成熟的开源先例。**

#### 5.2.1 核心洞察

**Live2D「动作」= 已知参数集上的一组时间关键帧，不是视频、不是网格动画。**

这个事实来自 3.2 节的数据格式分析，它带来三个结论：

1. **不需要生成模型**。生成动画只需要产出「参数 → 时间曲线」，这是一段结构化数据。
2. **参数空间是有限的、有界的**。本模型 72 个参数，扣掉 24 个物理输出，实际可控约 40 余个，
   每个都有明确的 `[min, max]` —— 这正是 LLM 擅长处理的**结构化受限输出**。
3. **可以纯程序生成，不需要 Cubism Editor**。动作文件就是 JSON，
   已经有人做出「只用文本编辑器 + AI Agent 加动作」的完整工具链。

#### 5.2.2 已有开源实现（重要参考）

**Soullink Emotion SDK** — <https://github.com/nanlingyin/soullink-emotion-sdk>（MIT）

> **这是目前最值得研究的参考项目。** 它是 SoulLink_Live2D 作者把同一套思路抽成的
> 独立 SDK，已经不只是「Demo」，而是工程化的实时表演引擎。详见 **6.3 ①**。

核心是把「收到一句话 → 切一个表情」升级为**连续的情绪与动作状态**：

- **连续情绪 VAD**：用 Valence / Arousal / Dominance 三轴表达情绪方向与强度
  （对比本项目现在的 7 个离散情感标签）
- **FACS / AU 表情语义层**：模型无关地描述微笑、皱眉、注视、姿态
- **分层动作混合**：Idle / Reaction / Speech Performance 三层独立混合，
  说话层**不抢 LipSync 的嘴部控制权** —— 正好解决「动作与口型打架」
- **Profile 自动适配**：扫描模型参数生成 `soullink.profile.json` 与覆盖率
- **可复现调试**：`seed` 固定随机序列

⚠️ **它是 TypeScript / npm 生态，我们的项目是 Python + PySide6，不能直接引入。**
建议的做法不是「装它」，而是**读架构、把这四件事（VAD / FACS / 分层混合 / Profile 扫描）
用 Python 实现到我们自己的项目里**。

**SoulLink_Live2D** — <https://github.com/nanlingyin/SoulLink_Live2D>

上面那个 SDK 的前身，原理文档写得更细（见 **6.3 ③**）。关键设计：

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

#### 5.2.3 三层方案对比

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

#### 5.2.4 推荐架构

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

#### 5.2.5 与对话协同的时序设计

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

#### 5.2.6 成本与延迟控制

| 方案 | 额外延迟 | 额外成本 | 说明 |
| --- | --- | --- | --- |
| 复用主回复调用（推荐） | **0** | 0 | 在同一个 function call 里返回 `motion_plan`，不增加 API 调用 |
| 独立并行调用 | 0（并行） | +1 次调用 | 可用更便宜的小模型专门做动作规划 |
| 本地小模型（SLM） | 低 | 无 | SoulLink 的路线图里有本地 Qwen2.5 + LoRA |

**推荐第一种**：把 `motion_plan` 作为主模型 function call 的一个字段，
与 `reply` / `emotion` 一次产出。这样零额外延迟、零额外成本，
与当前「情感标签复用同一次调用」的设计一脉相承。

### 5.3 「模型直接生成动画」的边界

#### 5.3.1 能做到的（按难度递增）

| 层级 | 内容 | 可行性 | 依据 |
| --- | --- | --- | --- |
| ✅ 容易 | 生成**表情参数组合** | 成熟 | SoulLink 已实现并开源 |
| ✅ 容易 | 生成**语义化手势序列**（点头/歪头/转身） | 成熟 | 同上，`live2d-add-motion` 亦验证 |
| ✅ 可行 | 生成**完整 `.motion3.json` 关键帧曲线** | 可行 | `LoadExtraMotion()` **【实测】存在**；格式已完全解析（3.2 节） |
| ⚠️ 有难度 | 与**语音节奏精确对齐**的连续动作 | 需工程 | SoulLink 的两阶段方案（每 2 秒一帧） |
| ⚠️ 有难度 | **风格化/表演性**动作（唱歌跳舞） | 需数据 | 需在动捕或人工标注的动作库上训练 |
| ❌ 不可行 | 生成**新模型本体**（`.moc3`、网格、ArtMesh） | 需建模工具 | 见下 |

#### 5.3.2 做不到的

**LLM 无法直接生成 Live2D 模型本体**，原因是：

1. `.moc3` 是**编译后的二进制**，包含 ArtMesh 顶点、三角剖分、变形器层级、蒙版关系
2. 生成它需要**把一张插画分割成有语义的图层**（头发/眼睛/嘴/身体分离），并建立
   「参数 → 顶点位移」的绑定关系 —— 这是**美术 + 绑定**工作，不是文本生成任务
3. 即便有论文在做自动化绑定（如 Bunraku 从单图生成可编辑 Live2D 角色），
   也远未到「一句话生成一个角色」的程度，且需要图像输入而非文本

**结论**：**动作可以生成，模型不能。** 对当前项目而言，这不构成障碍 ——
我们已经有 Miku 的模型，缺的只是动作的丰富度，而这恰好是可生成的部分。

#### 5.3.3 需要注意的工程约束

**【实测 + 参考】** 生成动画时必须遵守：

1. **只写物理输入，不写物理输出**（本模型 6 个输入 / 24 个输出，见 3.4 节）
2. **值域钳位** —— 超出 `[min, max]` 会导致网格撕裂或渲染异常
3. **动作结束回到基准姿态** —— 否则会累积偏移，模型逐渐"歪掉"
4. **参数名要先探测** —— 本模型 `PARAM_*` 大写风格，标准名全部无效且**静默失败**
5. **验证要独立于生成** —— `live2d-add-motion` 的做法：生成器 + 独立校验器 +
   真实渲染验证三段式。本项目的对应工具是 `tools/diag_idle.py`（参数幅度验证）
   和 `tools/capture_window.ps1`（真机截图）

### 5.4 手机版可行性

> 起因：看到有人把那个免费 Live2D 初音模型加载进了手机，问本项目能否也做手机版。

#### 5.4.1 先分清两件事

截图里那个水印是 **VTube Studio** —— 它是一个**现成的** Live2D 面捕/展示应用，
有 [Google Play 版](https://play.google.com/store/apps/details?id=com.denchi.vtubestudio)。
所以那个人**并没有开发什么**，只是把模型导进 VTube Studio 而已。

| 你的诉求 | 成本 |
| --- | --- |
| 「我只是想在手机上看到她」 | **零开发** —— 装 VTube Studio，导入模型即可 |
| 「我要把 MikuAgent 变成手机 App」 | 需要真正的架构决策，见下 |

#### 5.4.2 Live2D 在手机上完全没问题

**【官方文档】** Live2D Cubism SDK 的平台支持（<https://docs.live2d.com/zh-CHS/cubism-sdk-manual/platform/>）：

| SDK | Android | iOS | 说明 |
| --- | :---: | :---: | --- |
| **Native**（OpenGL） | 〇 | 〇 | 纯 C++，自己接平台层 |
| **Web** | 〇 | 〇 | Chrome/Firefox/Edge/Safari 全支持 |
| **Java** | 〇 | – | Android 5.0 (API 21) ~ 14.0 (API 34) |
| **Unity** | 〇 | 〇 | 还含 WebGL / HarmonyOS |

**所以渲染这一层不是障碍，而且有四条官方路线可选。**

#### 5.4.3 真正的三个障碍

| 障碍 | 原因 | 严重度 |
| --- | --- | --- |
| **`live2d-py` 上不了手机** | 它是 CPython C 扩展，官方只有 Native / Web / Java / Unity 版，**没有 Python 版** | 决定性的 |
| **PySide6 不支持 Android / iOS** | Qt for Android 存在但 PySide6 官方不发移动端 wheel | 决定性的 |
| **GPT-SoVITS 跑不动** | 需要约 2GB 显存 + 3GB 内存；手机既没显存也没这么大可用内存 | 高（但可替换） |

#### 5.4.4 好消息：后端几乎可以全部复用

**【实测】** 本项目的代码分层非常干净：

| 层 | 规模 | 依赖 Qt？ | 手机端可复用性 |
| --- | --- | --- | --- |
| `backend/`（agent / memory / persona / stt / tts / config） | **1302 行** | **✅ 完全不依赖** | 逻辑 100% 可搬 |
| `ui/`（窗口 / Live2D 视图 / 气泡 / 输入栏 …） | 2344 行 | ❌ 全是 Qt + OpenGL | 必须重写 |

> 校验方式：在 `backend/` 里搜 `PySide6|Qt`，只命中两处**注释**（解释为什么 TTS 要独立进程），
> 没有任何实际依赖。

**这意味着：LLM 调用、记忆库、人设、STT 封装、TTS 客户端逻辑都能照搬，重写的只是「壳」。**

#### 5.4.5 三条路线

**路线 A：瘦客户端（推荐，复用率最高）**

```
手机（只做渲染 + 采集）              桌面 PC（现有代码几乎不动）
┌────────────────┐                 ┌──────────────────────────┐
│ Live2D 渲染     │◀── WebSocket ──▶│ backend/ 全部复用         │
│ 麦克风 / 摄像头  │   （局域网）     │  · DeepSeek Agent        │
│ 音频播放        │                 │  · 记忆 SQLite           │
└────────────────┘                 │  · TTS（用桌面 GPU）      │
                                   │  · STT（Whisper）        │
                                   └──────────────────────────┘
```

- **复用率**：后端 100%；只需加一层 WebSocket/HTTP 服务
- **手机端**：Web（`pixi-live2d-display`）做成 PWA，或 Unity
- **有意思的地方**：本项目**刚删掉的 `frontend/` 网页版**，正好可以改造成手机端 ——
  当初它作为桌面 UI 是多余的，但作为**手机客户端**反而合适
- **代价**：必须开着电脑、同一局域网
- **工作量**：一个周末能出原型

**路线 B：全本地手机 App（重写）**

| 组件 | 方案 |
| --- | --- |
| Live2D | 官方 SDK for Android(Java) / iOS，或 Unity |
| STT | **`whisper.cpp`** —— 在手机上跑得很好（tiny/base 模型） |
| LLM | DeepSeek API（联网） |
| TTS | **最大问题**：GPT-SoVITS 跑不了，必须换成<br>① 云端 TTS ② 手机端 ONNX 量化小模型 ③ 退回 `edge` |
| 后端 | 用 Kotlin / Swift / C# 重写 |

- 这**不是移植，是新项目**。`ui/` 2344 行 + 部分 `backend/` 都要重写
- 好处：不依赖电脑，真正随身

**路线 C：混合** —— 手机本地做 Live2D + STT，LLM 走云，TTS 走云或本地小模型；
桌面端保留用于音色克隆与记忆管理。

#### 5.4.6 建议

| 目标 | 建议路线 |
| --- | --- |
| 只是想手机上看到她 | **装 VTube Studio**，零开发 |
| 想快速验证手机端体验 | **路线 A**，后端不动，加个 WebSocket + 网页客户端 |
| 想做独立 App 上架 | **路线 B**，但要有「重写 UI 层 + 换 TTS」的心理准备 |
| 想两边都能用 | A → C 演进：先把 A 跑通，再把 STT/TTS 逐步下沉到手机 |

> 📌 **前置条件**：无论选哪条路线，**都要先做 5.5 节的 TTS/STT provider 抽象** ——
> 它是把「本地重计算」换成「云端 API」的唯一入口，也是路线 A/B 的公共地基。

**许可证提醒**：Live2D Cubism SDK 商用需要 [publication license](https://www.live2d.com/en/sdk/license/)，
个人 / 小规模通常免费，但**上架收费 App 前必须确认**。

### 5.5 TTS / STT 的「本地 ⇄ API」双通道

> 需求来源：GPT-SoVITS 与 Whisper 的资源占用已经让 PC 也吃力（见 4.6 节的 GPU 争用问题），
> 更是手机化的直接阻碍。需要让用户在**设置界面里自行选择**走本地还是走 API。
> **本节只写方案，不改代码。**

#### 5.5.1 为什么要做这件事

| 问题 | 本地路径的代价 |
| --- | --- |
| **显存** | GPT-SoVITS 常驻约 1.4~2.2GB（见 2.5 节），与桌宠的 OpenGL 渲染争一块 4GB 卡 |
| **内存** | TTS 服务约 2.9GB + Whisper 约 1GB；本机可用内存常在 3GB 上下 → 已多次触发 OOM |
| **启动** | 模型加载 + 预热约 35 秒才可发声 |
| **GPU 互操作** | 已实测到 `CUDA error: unknown error`（4.6 节），本地路径在共享 GPU 上不稳定 |
| **手机化** | 手机既无显存也无 3GB 可用内存，**本地路径根本走不通**（5.4.3 节） |

**做双通道的收益是叠加的**：PC 上可以立刻卸掉资源压力，
同时它也是手机路线 A/B 的**前置条件** —— 没有 provider 抽象，换端就得重写。

#### 5.5.2 现状：TTS 有雏形，STT 完全没有

**【代码】** 当前形状：

| 模块 | 已有的抽象 | 缺口 |
| --- | --- | --- |
| `backend/tts.py` | 已有 `engine` 概念，`edge` / `sovits` 两种实现，`synthesize()` 内部按 engine 分派，`status` 属性也分了派 | `edge` 只能算「在线但不可选音色」，**没有真正的「云端 + 音色克隆」通道** |
| `backend/stt.py` | 只有一路：`sd.InputStream` 录音 → `WhisperModel` 本地转写，**模型尺寸写死在 config** | **没有任何 API 通道** |

**关键观察**：STT 其实有两种性质完全不同的工作：

```
录音（麦克风采集）        转写（音频 → 文本）
  · 设备层，必须本地        · 计算密集，可以搬到云端
  · 开销极小               · Whisper small 约 1GB 内存
  · sounddevice 已封装好   · 这才是要抽象的部分
```

**所以 STT 的抽象应该只包住「转写」，录音保持在本地。** 这一刀切下去，
改造量比想象中小得多。

#### 5.5.3 设计：Provider 抽象

**TTS —— 扩展现有的 `engine` 为 provider 表**

```
TextToSpeech
├─ provider = local-sovits   现有：GPT-SoVITS 本地推理（默认，音色最准）
├─ provider = local-edge     现有：edge-tts（微软在线，无克隆）
├─ provider = api-minimax    新增：MiniMax speech-2.8 + 克隆音色  ← 推荐
└─ provider = none           关闭
```

**STT —— 拆成两层**

```
SpeechToText
├─ recorder（保持在本地，不抽象）
│    └─ sounddevice 采集 16kHz 单声道 → 内存缓冲
└─ transcriber（这一层可切换）
     ├─ local-whisper    现有：faster-whisper，可以选模型尺寸
     ├─ api-minimax      新增：MiniMax ASR
     └─ api-openai       可选：OpenAI Whisper API（生态最通用）
```

**统一的 provider 契约**（伪接口，用于说明形状，不是最终代码）：

```python
class TtsProvider:
    name: str
    needs_gpu: bool
    def available(self) -> tuple[bool, str]: ...     # 依赖是否就绪 + 原因
    def synthesize(self, text: str, emotion: str) -> tuple[Path, float] | None: ...
    def shutdown(self) -> None: ...

class SttTranscriber:
    name: str
    def available(self) -> tuple[bool, str]: ...
    def transcribe(self, pcm: bytes, sample_rate: int) -> dict: ...
```

**保留现有对外行为**：`synthesize()` 仍返回 `(Path, float)`，
`transcribe` 仍返回 `{"text": ...}` / `{"error": ...}`。
上层的 `TtsPipelineWorker`、`ChatWorker`、`pet_window` **一行都不用改**。

#### 5.5.4 MiniMax 作为推荐 API provider（已核实官方规格）

选它的理由：**一个供应商同时提供 TTS 与 ASR**，音色克隆是官方能力，
国内可直连，API 是标准 REST。

**TTS —— 同步语音合成**

| 项 | 值 |
| --- | --- |
| Endpoint | `POST https://api.minimax.cn/v1/t2a_v2` |
| 鉴权 | `Authorization: Bearer <API_KEY>` |
| 模型 | **`speech-2.8-hd`**（最新 HD，情绪渲染融合语气词）/ `speech-2.8-turbo`（极速）<br>另有 `speech-2.6-hd/turbo`、`speech-02-hd/turbo` |
| 单次上限 | **10,000 字符**（远超我们 `TTS_MAX_CHARS=200`） |
| 输出格式 | mp3 / pcm / flac / wav |
| 语种 | 40 种（含中文、日语、粤语） |

请求体关键字段：

```json
{
  "model": "speech-2.8-hd",
  "text": "今天是不是很开心呀(laughs)，当然了！",
  "stream": false,
  "voice_setting": {
    "voice_id": "<克隆得到的 voice_id>",
    "speed": 1, "vol": 1, "pitch": 0,
    "emotion": "happy"
  },
  "audio_setting": {"sample_rate": 32000, "format": "mp3", "channel": 1}
}
```

响应：`{"data": {"audio": "<hex 编码的音频>"}, "extra_info": {"audio_length": 9900, ...}}`

> **两个和我们现有系统天然契合的点**：
> 1. `voice_setting.emotion` **官方支持情感**（`happy` 等）—— 正好接我们已有的情感标签链路，
>    比现在 `sovits` 只能改 `speed` 强得多（见 2.6 节「情感→语速效果有限」）
> 2. `extra_info.audio_length` 直接给出时长（毫秒），**省掉我们本地读 WAV 算时长那一步**；
>    音频若是 mp3，仍走现有的 PyAV 转 WAV（`_synth_edge` 已有这套逻辑可复用）

**音色克隆（把初音的声音搬上云）**

```
1. 上传复刻音频   POST /v1/files/upload  → file_id
     约束：mp3/m4a/wav；时长 10 秒 ~ 5 分钟；≤20MB
2. (可选) 上传示例音频 → 增强相似度与稳定性（<8 秒）
3. 快速复刻       → 自定义 voice_id
4. 用 voice_id 调 t2a_v2 合成
```

> ⚠️ **两个必须写进用户提示的约束**：
> 1. **复刻音色是临时的**：若 **168 小时（7 天）内未被任何合成接口使用**，系统会删除该音色。
>    → 我们的「独立进程 + 启动预热」模式**天然满足**这条（每次启动都会用），
>    但**长期不启动桌宠就会掉音色**，需要在设置界面提示「音色已过期，请重新克隆」
> 2. **调用复刻接口前需完成个人或企业认证**（实名）

> 📌 **我们已有的 `assets/voice/miku_ref.wav`（4.2 秒）时长不足 10 秒**，
> 走 MiniMax 复刻需要重新准备一段 **≥10 秒** 的干净人声。
> 这正好可以把之前搁置的「用 B 站演唱会视频做 vocal separation」那件事用上。

**ASR —— 语音识别**

| 项 | 值 |
| --- | --- |
| Endpoint | `POST https://api.minimaxi.com/v1/speech_to_text` |
| 请求 | `multipart/form-data` |
| 模型 | `asr-1.0` |
| 音频约束 | wav/aiff/flac/alac(m4a)/mp3/aac/opus/ogg；**≤500 秒**；**≤50MB** |
| 语言提示 | 请求头 `language`，BCP-47（`zh` / `ja` / `en` …）；**不传则混合语言识别** |
| 返回格式 | `json`（text+duration）/ `verbose_json`（含说话人分离与时间戳）/ `srt` / `vtt` |
| 流式 | `stream=true` 走 SSE 增量返回 |

> 官方明确提示：**识别不依赖高采样率与立体声**，建议先转单声道 16kHz 或用压缩格式。
> 我们现在的录音**正好就是单声道 16kHz float32**（`STT_SAMPLE_RATE=16000`、`channels=1`），
> 只需编码成 wav 或 opus 即可直接上传，**不需要任何重采样**。

#### 5.5.5 设置界面设计

在现有「设置」面板（`ui/settings_dialog.py`）里新增两块。
现有面板已有「语音输出」「语音输入」「视频对话」三个开关，延续同样的交互范式：

```
┌─ 语音输出（Miku 说话） ─────────────────────────────┐
│  [✓] 启用语音输出                                    │
│  引擎   ( ) 本地 · GPT-SoVITS（音色最准，需 GPU）     │
│         ( ) 本地 · edge（轻量，无克隆）               │
│         (•) API · MiniMax（推荐，省显存）             │
│  状态   就绪 · 音色：Miku-克隆-20260911               │
│         [测试音色]  [重新克隆音色]                     │
│  密钥   [sk-··············]（写入 .env）              │
└─────────────────────────────────────────────────────┘

┌─ 语音输入（按住说话） ───────────────────────────────┐
│  [✓] 启用语音输入                                    │
│  转写   ( ) 本地 · Whisper  模型 [small ▾]            │
│         (•) API · MiniMax ASR                        │
│  状态   就绪（API）· 上次耗时 0.8s                     │
└─────────────────────────────────────────────────────┘
```

**设计要点**：

1. **每个选项都标注资源代价**（"需 GPU" / "省显存"），让用户自己权衡
2. **`available()` 不通过时显示原因**（如"未安装 opencv" / "未配置密钥"），
   并把该选项置灰 —— 沿用 `TtsWorker` 现有的 `status` 机制
3. **API 密钥输入框**：目前密钥只能改 `.env` 重启，设置界面直接可填是明显改进
4. **一键测试**：合成一句固定话术，让用户当场听出差异（这比文字描述有效得多）
5. **音色克隆入口**放在设置里（低频操作），**日常切换**放在托盘菜单（高频）
6. **优雅降级**：API 连续失败 N 次 → 自动回退本地并在气泡里说明（复用现有 `_mock_reply` 的兜底思路）

**配置项**（写进 `.env.example`）：

```ini
# ===== 语音 provider 选择 =====
TTS_PROVIDER=local-sovits      # local-sovits | local-edge | api-minimax | none
STT_TRANSCRIBER=local-whisper  # local-whisper | api-minimax | api-openai

# ===== MiniMax（api-* 时必填）=====
MINIMAX_API_KEY=
MINIMAX_GROUP_ID=
MINIMAX_TTS_MODEL=speech-2.8-hd
MINIMAX_TTS_VOICE_ID=          # 克隆得到的 voice_id
MINIMAX_ASR_MODEL=asr-1.0
```

#### 5.5.6 与手机化路线的适配（回答「怎么和前面的方向配合」）

**这张表是本节的重点** —— provider 抽象不是独立的一件事，它是 5.4 节三条路线的**公共前置**：

| 路线 | TTS provider | STT transcriber | LLM | 说明 |
| --- | --- | --- | --- | --- |
| **现在（PC 全本地）** | `local-sovits` | `local-whisper` | DeepSeek API | 资源吃紧，只在这台机器上勉强跑 |
| **PC 减压（立刻可做）** | **`api-minimax`** | **`api-minimax`** | DeepSeek API | **一步卸掉约 2.4GB 显存 + 3.9GB 内存**，4.6 节的 GPU 争用问题直接消失 |
| **路线 A 瘦客户端** | `api-minimax`<br>或桌面本地 | `api-minimax`<br>或桌面本地 | DeepSeek API | 手机只做 Live2D + 录制 + 播放；<br>**provider 抽象让「后端在桌面还是云」对手机透明** |
| **路线 B 全本地 App** | **必须** `api-minimax` | 手机本地 `whisper.cpp`<br>或 `api-minimax` | DeepSeek API | 手机跑不了 GPT-SoVITS；<br>STT 可以用 whisper.cpp 保住离线 |
| **路线 C 混合** | `api-minimax` | 手机本地 `whisper.cpp` | DeepSeek API | 桌面保留 `local-sovits` 仅用于**音色克隆**（克隆一次，云端长期使用） |

**几个关键结论**：

1. **provider 抽象是手机化的第一块砖**。不做它，路线 A 要把 TTS/STT 的调用点全改一遍；
   做了它，桌面与手机只是**同一套 provider 契约的两个实现**
2. **它同时解决了眼前的 PC 问题**。选 `api-minimax` 后：
   - 显存：`-1.4~2.2GB`（TTS 服务不再需要）
   - 内存：`-2.9GB`（TTS 服务）+ `-1GB`（Whisper）
   - 启动：从 35 秒降到 1~2 秒（不用加载模型与预热）
   - **4.6 节的 `CUDA error` 自然消失**（不再有 CUDA 上下文）
   - 代价：需要联网 + 按量计费
3. **桌面端仍有存在价值**：`local-sovits` 用于**音色克隆与 A/B 对比**，
   毕竟本地克隆不需要上传、不受 7 天过期限制。建议**保留但不默认启用**。
4. **手机路线 B 的 TTS 别无选择**：目前没有能在手机上跑的 GPT-SoVITS 级别方案，
   API 是唯一现实路径。这反过来印证了 provider 抽象的必要性。

#### 5.5.7 落地顺序（建议）

| 步骤 | 内容 | 依赖 |
| --- | --- | --- |
| **1** | 抽出 `TtsProvider` / `SttTranscriber` 契约，把现有 `edge` / `sovits` / `whisper` **包装成 provider**（行为完全不变） | 无，纯重构 |
| **2** | 实现 `api-minimax` 两个 provider（TTS + ASR） | MiniMax API Key + 实名认证 |
| **3** | 设置界面加 provider 选择 + 密钥输入 + 一键测试 | 步骤 1、2 |
| **4** | 准备 ≥10 秒的参考音频，跑通音色克隆，把 `voice_id` 写进配置 | 步骤 2 |
| **5** | 优雅降级：API 失败自动回退本地 | 步骤 1~4 |
| **6** | （手机路线 A）把 provider 层搬到 WebSocket 服务端 | 步骤 1~5 |

> **步骤 1 是关键**：它是**纯重构、零行为变化**，可以先做、单独验证，
> 不影响任何现有功能。做完之后步骤 2~5 都只是「加一个 provider 实现」。

### 5.6 路线 A 的实现（**已完成**）

5.4.5 只写了路线 A 的思路，这里记录实际落地结果。

#### 5.6.1 架构

```
手机（只做渲染 + 采集）                    PC（现有代码几乎不动）
┌──────────────────────┐                ┌────────────────────────────────┐
│ Live2D 渲染           │                │ PetWindow（原生 PySide6 桌宠）  │
│ （PIXI + pixi-live2d）│◀── WebSocket ──▶│   ── 与以前完全一致 ──          │
│ 麦克风 / 摄像头        │   （局域网）    │                                │
│ 音频播放              │                │ RemoteServer（新增后台线程）    │
└──────────────────────┘                │   ├─ aiohttp: HTTP + WS 同端口  │
                                        │   └─ 复用 agent / memory /      │
                                        │      tts / stt                  │
                                        └────────────────────────────────┘
```

**PC 端仍然是原生窗口，不会变成网页。** 远程服务只是同一进程里多一个
daemon 线程，起不来也只打日志、不影响桌宠。

#### 5.6.2 关键实现点

| 点 | 做法 | 原因 |
| --- | --- | --- |
| **阻塞调用** | LLM / TTS / STT 全走 `asyncio.to_thread` | 它们都是秒级阻塞，直接 await 会堵死事件循环，别的客户端全卡住 |
| **静态资源** | 一个 aiohttp 同时提供页面、`/model/`、WebSocket | 手机只需连一个地址，不用再开第二个端口 |
| **模型下发** | `/model/` → `assets/live2d/miku/` | 模型是本机文件，**不依赖外网**；含中文路径的 motion3 走 URL 编码也正常 |
| **JS 库** | 走 CDN，`__CDN__` 占位可换镜像 | 不把 Cubism Core 打进仓库（它有独立的许可条款） |
| **音频格式** | 手机端把录音转成 16k 单声道 WAV 再上传 | 免去服务端装 WebM/Opus 解码器，且正好符合 ASR 的推荐规格 |
| **降级** | 服务起不来只打日志 | 手机端是附加能力，绝不能因为它起不来导致桌宠打不开 |

#### 5.6.3 协议

```
客户端 → 服务端
  {"type":"chat",  "text":"…", "image":"<base64 jpeg 可选>", "session_id":…}
  {"type":"audio", "data":"<base64 wav>", "session_id":…}
  {"type":"ping"}

服务端 → 客户端
  {"type":"ready",      "provider":{llm, tts, stt, voice_id}}
  {"type":"transcript", "text":"…"}                      语音输入转写结果
  {"type":"reply",      "text":"…", "emotion":"…"}
  {"type":"speech",     "data":"<base64 wav>", "duration":2.87}
  {"type":"error",      "message":"…"}
  {"type":"pong"}
```

**`reply` 与 `speech` 分开下发**：手机可以先显示文字气泡，再等音频到达播放，
不必让用户盯着空白等 TTS。

#### 5.6.4 实测验证

| 项 | 结果 |
| --- | --- |
| HTTP `/` | 200，15405 B，`__CDN__` 已替换 |
| HTTP `/health` | 200，provider = deepseek-flash / minimax / minimax / MikuV4C2026 |
| 模型资源 | moc3 365KB、贴图 3.7MB、中文路径 motion3 全部 200 |
| **文字对话** | reply **0.79s**（HAPPY）→ speech 179KB / 音频 2.87s @32kHz（落盘校验为合法 WAV） |
| **语音上传** | transcript「大家好，我是初音未来。」**1.71s** → reply → speech 479KB |
| ping/pong | ✅ |
| **共存** | 桌宠窗口正常 + 远程服务 **4 秒**起监听；总内存 **248MB**、显存 **538MiB** |

> 最后一行是对比的关键：本地 `sovits` 模式下是 **3.6GB 内存 + 2.2GB 显存**。
> 走云端 provider 后，PC 端从「勉强跑」变成「很轻松」，手机端才有余量接进来。

#### 5.6.5 手机端使用步骤

1. PC 上启动桌宠（`start.bat`），控制台会打印形如
   `http://192.168.x.x:8765/` 的地址
2. 手机连**同一个 WiFi**，浏览器打开该地址
3. 直接打字 / 按住 🎤 说话 / 点 📷 拍照发给她

> 手机需要能访问 CDN 加载 PIXI 与 pixi-live2d-display；
> 受限时在 `.env` 里改 `REMOTE_CDN` 为可用镜像即可。模型文件始终由 PC 提供。

---

### 5.7 运行中切换引擎（**已完成**）

设置面板新增「⚙ 修改配置」按钮，直接打开首次设置向导的**编辑模式**，
7 项（DeepSeek Key / TTS 引擎 / MiniMax Key / STT 通道 / 称呼 / 视频对话 / 手机端）
都能在运行中改，保存后**立即生效，不需要重启**。

#### 为什么不能只改 `.env`

这是本节的重点。把新值写进 `.env` 只是第一步，真正的坑在于：

| 资源 | 不释放的后果 |
| --- | --- |
| GPT-SoVITS 是**独立子进程** | 切到云端后它还在跑，约 2.2GB 显存一直被占，桌宠照样卡 |
| Whisper 模型常驻内存 | 约 1GB 内存不还，所谓「云端省内存」只是纸面数字 |
| `REMOTE_ENABLED` | 以前只在启动时读一次，改了开关必须重启 |
| `TextToSpeech.engine` / `SpeechToText.transcriber` | 在 `__init__` 就固化成实例字段，reload config 也改不到 |

所以链路是：

```
点「修改配置」→ 向导写 .env → load_dotenv(override=True) + importlib.reload(config)
   → tts.reconfigure()  → 停旧引擎进程 / 拉新引擎（后台）
   → stt.reconfigure()  → 卸载 / 加载 Whisper
   → remote_controller.sync() → 启停 / 换端口重启远程服务
   → 刷新面板 + 气泡反馈
```

用**就地 reconfigure 而不是重建对象**：`RemoteServer` 等也持有这同一批
引用，换对象就要重新接线，容易漏一处就静默失效。

#### 实测数据（`tools/test_engine_switch.py`）

| 动作 | 结果 |
| --- | --- |
| 云端 → 本地 | `reconfigure()` 立即返回（0.00s，预热在后台）；36.8s 服务就绪 |
| | 显存 `449 → 2011 MiB`（+1562）；合成出 2.80s 音频 |
| 本地 → 云端 | 子进程已退出、端口已释放、显存回到 `449 MiB`（与基线差 `+0`） |
| STT 本地 | Whisper 4.0s 加载完，本进程内存 `57 → 390 MB` |
| STT 切回云端 | `_model is None: True`，内存 `390 → 82 MB` |

#### 踩到的坑：预热线程留下野进程

预热是异步的。用户可能在它 `spawn` **之前**就切走或退出，于是这个函数
返回之后进程才被拉起来 —— 实测留下了一个 736MB 的野进程 + 子进程，
显存一直挂着。

修法是加一个「是否还需要这个服务」的旗标 `_server_wanted`：

* `reconfigure()` **第一件事**就是立旗（在停旧引擎之前）
* `shutdown()` 先立旗再停进程
* `_ensure_server()` 在持 `_server_lock` 时检查旗标，为假直接不 spawn
* `_stop_server()` 全程持 `_server_lock`，避免和 spawn 交错出现「刚停掉又被拉起来」

锁顺序始终是 `_lock → _server_lock`，不会死锁。
`tools/test_engine_switch.py` 阶段 4 是这条的回归测试：
切到本地后立刻 `shutdown()`，然后盯 45 秒确认端口始终没被占用。

#### 一键复测

```bash
python tools/test_engine_switch.py    # 引擎切换 + 资源释放 + 野进程回归
python tools/test_reconfigure_ui.py   # 按钮信号 + 远程服务启停/换端口
python tools/test_reconfigure_e2e.py  # 真实 PetWindow 跑完整链路（会快照并还原 .env）
```

> 「⚙ 修改配置」按钮在设置面板里的位置，正好覆盖了原先那块**白色空白区**。
> 那块白不是布局问题——离屏渲染 `SettingsDialog` 是干净的；它是真实窗口下
> 的合成伪影：设置面板是「无边框 + 透明 + 置顶 + 带 OpenGL 子窗口」的桌宠
> 的子窗口，GL 表面盖不住的地方就露白底。

---

### 5.8 气泡长文本可滚动（**已完成**）

#### 气泡高度与取景是配套的

气泡高度上限 `BUBBLE_MAX_H` 直接决定模型避让的安全区
（`BUBBLE_TOP + BUBBLE_MAX_H`），**不能单独加大**。所以窗口高度也一并调了：

| 窗口高 | 气泡上限 | 模型 scale / dy | 模型 top / bottom | 安全区 |
| --- | --- | --- | --- | --- |
| 600（旧） | 132 | 0.80 / 0.15 | 191 / 507 | [184, 524] |
| 660（现） | 188 | 0.80 / 0.00 | 248 / 564 | [240, 584] |

关键点：**模型尺寸完全没变**（都是 128×316），只是整体下移 57px，
用窗口多出来的高度换气泡空间。所以「气泡变高」并没有让 Miku 变小。

`ui/pet_window.py` 里用一张按窗口高度索引的表 `_LAYOUT_BY_HEIGHT` 选档，
而不是写死单一数值 —— 否则一旦 `WINDOW_HEIGHT` 被改小（本地 `.env` 里就有
这一项），188px 的气泡会直接压到模型头上。没量过的尺寸取「不超过它的最大
已量档」，宁可气泡小一点也不突破安全区。改窗口尺寸必须用
`tools/measure_framing.py` 重新量。

#### 结构

```
气泡（≤ BUBBLE_MAX_H）
 └ QVBoxLayout
    ├ 情绪 chip
    └ QScrollArea（滚动条按需出现）
       └ QLabel（wordWrap，顶对齐）
```

#### 底部对齐：气泡下沿固定，向上生长

气泡高度随内容在 72~188px 之间变化，于是「从哪里长」就成了问题：

| 方案 | 结果 |
| --- | --- |
| 顶部固定、向下生长（最初） | 短消息（72px）时气泡下方留 **107px** 空白 |
| 模型跟着气泡跑 | 空白只是被推到窗口底部，模型还会随每条消息跳动 |
| **底部固定、向上生长（现方案）** | 下沿恒定，模型不动，任何长度都没有多余空白 |

所以 `_layout_children()` 里是：

```python
bubble_bottom = BUBBLE_TOP + BUBBLE_MAX_H          # 下沿钉死
bubble_y = max(BUBBLE_TOP, bubble_bottom - self.bubble.height())
self.bubble.move(x, bubble_y)
```

高度一变就要重新摆位，所以气泡加了 `height_changed` 信号，
`_layout_children()` 里再用 `_laying_out` 标志防重入
（`set_max_height()` → `apply_content_height()` → 可能发信号 → 又回调布局）。

实测四种长度（`tools/test_bubble_align.py`）：

| 消息 | 气泡高 | 气泡 y | 下沿 | 距模型 |
| --- | --- | --- | --- | --- |
| 短 | 72 | 162 | 234 | 13 |
| 中 | 90 | 144 | 234 | 13 |
| 长 | 188 | 46 | 234 | 13 |
| 超长 | 188 | 46 | 234 | 13 |

下沿波动 0px、间距波动 0px。超长时气泡顶部正好到 `BUBBLE_TOP = 46`，
不会再往上盖住角标按钮。

> 副作用：打字过程中每换一行，气泡会向上"顶"一行，正文整体上移。
> 这是底部对齐的必然结果（等价于最后一行位置固定、旧行往上走）。
> 换成顶部对齐就没有这个位移，但会退回"短消息下方一大片空白"。

**连带修的：角标按钮也得跟着气泡走。**
气泡底部对齐后会上移，而 `_button_row_y()` 原本写的是「有气泡就回窗口顶端」，
于是短消息时气泡飘到 y≈100、按钮还钉在 y=10，中间空出一大截
（看起来就像按钮"失去动态效果"了）。改成贴气泡上沿：

```python
if self.bubble.isVisible():
    return max(BUTTON_MARGIN, self.bubble.y() - BUTTON_SIZE - 8)
```

**顺序问题**：`_layout_children_inner()` 原先先算按钮位置、后摆气泡，
那样读到的是上一轮的 `bubble.y()`。必须把按钮定位挪到气泡定位**之后**。

实测（`tools/test_bubble_align.py`）：

| 消息 | 气泡 y | 按钮 y | 按钮距气泡 |
| --- | --- | --- | --- |
| 短 | 162 | 124 | 8 |
| 中 | 144 | 106 | 8 |
| 长 | 46 | 10 | 6（受 BUTTON_MARGIN 下限约束） |

测试里加了断言：按钮不得压到气泡上，且短消息与超长消息之间按钮 y 必须不同
（否则就是又被钉死在顶端了）。

#### 五个必须处理的细节

**1. QLabel 默认垂直居中，必须显式顶对齐。**
滚动区会把 QLabel 拉得比内容高，居中的结果是整段文字被顶到可视区下半部分、
上方留一大片空白（实测复现过）。要用
`setAlignment(AlignLeft | AlignTop)` 并给纵向 `SizePolicy.Minimum`。

**2. 不能用 `QLabel.heightForWidth()` 算高度 —— 它会明显高估。**
实测 320px 宽、真实 4 行的文本，`heightForWidth` 给出 216px，实际只要约 84px，
结果是往下滚能看到一大片空白。改用 `QFontMetrics.boundingRect` 按实际宽度
和换行规则算，与 QLabel 的渲染口径一致：

```python
fm = self._content.fontMetrics()
rect = fm.boundingRect(QRect(0, 0, width, 100000), Qt.TextFlag.TextWordWrap, text)
height = max(rect.height(), fm.height()) + fm.lineSpacing() // 3
```

**3. 自动跟随要能被用户打断。**
打字过程中跟随底部（看得到字在冒），但分两种「滚动」：程序滚动要吞掉
`valueChanged`（否则会被当成用户操作），用户滚动才更新 `_follow`。
打完字后如果内容超框，**回到顶部**——停在底部会让人以为「就这么几句」。
用户中途自己滚过就不动他。

> 用 `QAbstractSlider.actionTriggered` 区分也可以，但它触发时 value 还没更新，
> 判断时机不对；用 `_auto_scrolling` 标志位更稳。

**4. 引入 QScrollArea 之后 `adjustSize()` 失效了 —— 必须自己算高度。**
这是最隐蔽的一个。原先气泡靠 `adjustSize()` 按 QLabel 的 sizeHint 长高，
放进滚动区后，滚动区的 sizeHint **不随内容增长**，于是气泡只会缩到最小、
下面留一大片空白，然后全靠滚动。

实测数值：窗口 660、上限 188，但气泡只有 **94px**，
下方空白 **107px**。改成显式计算：

```python
want = min(self._chrome_height() + needed + self._pad(), self._max_height)
self.setFixedHeight(want)
```

修完后气泡 188px，与模型之间只剩 13px 安全间距。
`tools/test_bubble_scroll.py` 里加了这条的回归断言
（超长文本的气泡高度必须 ≥ 上限 − 4px）。

**5. 防裁切余量只能加在气泡高度上，不能加进内容高度。**
一开始把 `+ lineSpacing()//3` 的余量算进了正文高度，结果正文比可视区高
那么几像素 —— 连「你好」这种短句都会冒出一条多余的滚动条（实测滚动范围 2px）。
现在 `measure_content_height()` 返回**精确**值，余量只在 `apply_content_height()`
里加到气泡总高度上。

#### 顺带修掉：开场白定时器吞掉回复

`_greet` 原先用 `QTimer.singleShot(9000, self.bubble.hide_bubble)` 排了一个
**外部**定时器。`show_message()` 只会停掉自己内部的 `_autohide`，停不掉外部
那个 —— 用户如果在开场 9 秒内说话，回复刚显示出来就被隐藏了。
改成 `show_message(GREETING, "HAPPY", autohide_ms=9000)`，新消息会自动取消它。

#### 顺带修掉：`[HAPPY]` 标签漏进正文

截图里出现过 `…吗～？ [HAPPY] 当然可以呀！` —— 标签既显示在气泡里，也会被
TTS 念成「左括号 HAPPY 右括号」。

根因：`agent.EMOTION_TAG` 是 `^\s*\[([A-Za-z_]+)\]\s*`，**只匹配开头**。
模型把标签写在了第二段就匹配不到。

修法：保留原有的「开头标签」逻辑，另外用只含已知情感名的
`INLINE_EMOTION_TAG` 把正文里的标签也清掉。**只清已知情感名**，
避免误伤正文里像 `[1]` 这样的正常方括号。

#### 复测

```bash
python tools/test_bubble_scroll.py
# 测试 1：parse_emotion 六种情况（含 [1] 不被误删、小写 [happy]）
# 测试 2：长文本可滚动 / 短文本不出现滚动条
# 测试 3：新消息取消上一条的自动隐藏
```

---

## 6. 附录

### 6.1 工具清单

| 工具 | 用途 |
| --- | --- |
| `tools/measure_framing.py` | 测量模型在指定窗口尺寸下的包围盒，用于精确摆放气泡与模型 |
| `tools/diag_vision.py` | 视觉诊断：摄像头探测 / API 视觉验证 / 合成画面的端到端链路 |
| `tools/ui_probe.py` | 轻量 UI 夹具（不加载 TTS/STT），内存吃紧时验证界面布局 |
| `tools/analyze_motions.py` | 解析全部动作的分组/时长/曲线规模，展示 motion3.json 结构 |
| `tools/measure_vram.py` | TTS 显存/内存逐组件拆解（**含内存守卫，一次只跑一个配置**） |
| `tools/diag_idle.py` | 诊断待机动画：呼吸/头身参数是否在动 |
| `tools/diag_idle2.py` | 导出全部参数 ID，测试 Idle 动作是否循环 |
| `tools/diag_zorder.py` | 复现 `WA_AlwaysStackOnTop` 导致的子控件遮挡 |
| `tools/diag_cpu_tts.py` | 排查 CPU 模式为何在有显卡机器上失败 |
| `tools/bench_tts.py` | 量化各长度合成耗时、情感→语速、speed 参数校验 |
| `tools/selftest_chat.py` | 端到端自检（走 `close()` 以便触发 TTS 清理） |
| `tools/capture_window.ps1` | 按 PID 定位窗口并截图（EnumWindows） |
| `tools/measure_emotion_speed.py` | 标定 MiniMax 各 emotion 的实际语速（绕过缓存、交叉轮询采样） |
| `tools/test_engine_switch.py` | 引擎切换 + 资源释放 + 预热野进程回归（见 5.7） |
| `tools/test_reconfigure_ui.py` | 「修改配置」按钮信号 + 远程服务运行中启停/换端口 |
| `tools/test_reconfigure_e2e.py` | 真实 PetWindow 跑完整重配链路（快照并还原 .env） |
| `tools/test_bubble_scroll.py` | 气泡长文本滚动 + `parse_emotion` 标签清理 + 自动隐藏取消 |
| `tools/test_bubble_align.py` | 气泡下沿与模型始终对齐 + 角标按钮跟随气泡（真实 PetWindow） |

### 6.2 本会话踩过的坑（按代价排序）

| 坑 | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| **内存耗尽** | NVIDIA 驱动失联、系统降级 | 测量脚本在单进程内连跑 4 个模型配置 | 加内存守卫、一次一个配置 |
| **OpenCV 5.0 不能按索引开摄像头** | `VideoCapture(0, CAP_DSHOW)` 全部失败 | DSHOW 后端不支持索引 | 改用默认后端（MSMF）并加回退链 |
| **启动时 import cv2 的副作用** | 可能干扰另一进程的 CUDA 推理 | OpenCV 导入会初始化 OpenCL | 改用 `importlib.util.find_spec` 只探测 |
| **`WA_AlwaysStackOnTop`** | 气泡/按钮被模型盖住 | 该属性让 GL 内容无视层叠顺序永远置顶 | 删除该属性；用最小复现确认 |
| **参数名静默失效** | 生气压眉毛、自动呼吸都不生效，也不报错 | 模型用 `PARAM_*`，代码写的是 Cubism 标准名 | `resolve_param()` 候选名解析 |
| **动作无调度** | 待机只有眨眼在动 | 动作只在回复/点击时播一次，播完无接续 | `_update_idle()` 轮询接续 |
| **TTS 进程孤立** | 退出后占 1.5GB 显存 | `tts.shutdown()` 定义了但从未被调用 | 接进 `closeEvent` |
| **托盘僵尸进程** | Alt+F4 后进程不退 | `setQuitOnLastWindowClosed(False)` | `closeEvent` 里显式 `quit()` |
| **窗口尺寸无效** | 改了 `config.py` 窗口还是旧尺寸 | `.env` 覆盖了默认值 | 同步改 `.env` |
| **内存耗尽** | NVIDIA 驱动失联、系统降级 | 测量脚本在单进程内连跑 4 个模型配置 | 加内存守卫、一次一个配置 |

### 6.3 参考项目笔记

以下三个是本项目后续开发最值得参考的外部资源，附**已核实**的内容与可用性判断。

---

#### ① Soullink Emotion SDK ★ 最相关

- 仓库：<https://github.com/nanlingyin/soullink-emotion-sdk>（MIT）
- npm：`npm install @soullink-emotion/sdk` · 快速上手也可只装 `@soullink-emotion/engine`
- 演示视频：<https://www.bilibili.com/video/BV1MXKi6NEbR/>（278s，UP：骥南凌音_official）
- 同源项目与原理文档：
  - SoulLink_Live2D：<https://github.com/nanlingyin/SoulLink_Live2D>
  - LLM 表情控制原理：<https://github.com/nanlingyin/SoulLink_Live2D/blob/main/docs/LLM_EXPRESSION_PRINCIPLE.md>

**它是什么**：面向 Live2D 数字角色的实时表演引擎，把「收到一句话 → 切一个表情」升级成
**一条连续的情绪与动作状态**：情绪有强度、动作有时序、语音有口型、模型有自己的参数能力。

**核心能力（直接对应本项目的 5.2 节规划）**

| 能力 | 说明 | 与本项目规划的关系 |
| --- | --- | --- |
| **连续情绪 VAD** | Valence / Arousal / Dominance 三轴表达情绪方向与强度 | 比我们「7 个情感标签」细腻得多 |
| **FACS / AU** | 模型无关的表情语义（微笑、皱眉、注视、姿态） | 正是 5.2.4 建议的「语义 DSL」，但更成熟 |
| **分层动作混合** | Idle / Reaction / Speech Performance 各自独立成层 | 对应我们的 MotionDirector 构想 |
| 语音口型 ownership | 说话层不抢 LipSync 的嘴部控制权 | 解决了「动作与口型打架」的问题 |
| **Profile 自动适配** | 扫描模型参数生成 `soullink.profile.json` + 覆盖率 | **正是我们该做的「先探测再映射」** |
| 可复现调试 | `seed` 固定随机序列 | 便于回归对比 |
| 渐进式接入 | 不绑定 LLM / Embedding / TTS / UI 框架 | 可只取 engine 一层 |

**工作流**（摘自其 README）：

```
消息 / 外部事件 / 语音
   └─▶ 可选语义层（本地规则 / Embedding / OpenAI 兼容 Planner）
         └─▶ EmotionIntent
               ├─▶ VAD 情绪状态      ┐
               ├─▶ FACS / AU 表情     ├─▶ MotionMixer ─▶ ModelProfile 参数映射 ─▶ Live2D Renderer
               └─▶ Idle/Speech/Reaction┘
```

**对本项目的可用性判断（重要）**

- ⚠️ **它是 TypeScript / npm 生态，我们是 Python + PySide6，不能直接引入**
- ✅ 但**设计可以直接搬**：VAD 三轴、FACS 语义层、分层混合、Profile 自动扫描这四点，
  用 Python 复刻一遍是完全可行的，且不需要 npm
- ✅ `@soullink-emotion/profile-generator` 的思路（扫模型文件生成参数能力表）
  我们可以用自己的 `live2d-py` API（`GetParamIds` / `GetParameter` 的 min/max/default）
  直接实现，见 3.4 节的参数表
- 💡 若将来愿意引入 Node 侧：它的 `api-client` / `planner-openai` 提供了 HTTP 与
  OpenAI 兼容接口，理论上可由 Python 通过 HTTP 调用其服务模式

**结论**：**这是目前最值得深入研究的参考项目。** 建议的用法不是「装它」，而是
**读它的架构，把 VAD + FACS + 分层混合 + Profile 扫描这四件事用 Python 实现到我们自己的项目里**。

---

#### ② Live2D 初音未来免费模型 ★ 可能是现成资源

- 视频：<https://www.bilibili.com/video/BV1B1Mo67E3g/>（62s，UP：玄宝酱）
- 合集：`miku初音未来免费模型`（共 3 集，含面捕 + 前倾 + 大小变）
- 首发数据：约 25 万播放 / 6.6 万赞 / 3.8 万收藏

**内容**：UP 主免费发布的 Live2D 初音未来模型，可用于桌宠或 VTS 面捕。

**授权条款（原文摘录，务必遵守）**

> 画师：@玄宝酱　建模：@怂不过三秒-　剪辑：@纱糖sato
> 该模型唯一作者：玄宝酱 / 怂怂koe
> 1. 模型可免费使用桌宠或者 vts 面捕使用，但**不可二传二改**
> 2. **严禁将该模型用于任何商业途径**，严禁直播牟利，严禁使用该模型进行违法行为
> 3. 如非商用需求发布视频，**需要标明出处**
> 4. 任何使用该模型进行的商业或违法行为产生的一切法律责任将由使用者自行承担

**⚠️ 与本项目直接相关的法律风险，需要你确认**

本项目是**公开 GitHub 仓库**，`assets/live2d/miku/` 里的模型素材是**随仓库分发**的。
如果该模型就是从这个免费发布而来，「**不可二传**」这一条意味着
**当前仓库的公开分发可能不符合其授权**。建议做一件事：

1. 确认当前 `assets/live2d/miku/` 的来源与授权
2. 若确为「不可二传」的模型 → 把 `assets/live2d/` 移出仓库（加进 `.gitignore`），
   在 README 写清「请自行获取模型并放入该目录」，与 `assets/voice/` 的处理方式一致

（我**没有**擅自改动仓库，因为无法确认你这个模型的真实来源，这需要你判断。）

**可用性**：模型本身是**动作 + 参数**资源，可以直接替换我们现在的 `miku.model3.json`；
但替换后**必须**用 `tools/measure_framing.py` 重新量取景，
并用 `tools/diag_idle.py` 重新核对参数命名（不同模型命名可能不同，见 3.4 节的坑）。

---

#### ③ SoulLink_Live2D 的功能展示视频

- 视频：<https://www.bilibili.com/video/BV1MXKi6NEbR/>（同上，即 Soullink Emotion SDK 演示）
- 同系列另外两集：
  - `BV18o6fBSEhk`：SoulLink_Live2D 功能展示（58s）
  - `BV1Ye6DBYEVk`：基于 LLM api 控制 l2d 皮套的尝试（61s）
- 配套文档（**强烈建议精读**）：
  <https://github.com/nanlingyin/SoulLink_Live2D/blob/main/docs/LLM_EXPRESSION_PRINCIPLE.md>

**这份原理文档里最值得抄的四件事**：

1. **参数列表动态注入 Prompt**：模型加载后读取所有参数及 `min/max/默认值`，
   生成参数说明塞进系统提示词，让 LLM 知道能控制什么
2. **LLM 返回结构化 JSON**：`{expression, parameters:{参数:数值}, duration}`
3. **范围校验与钳位**：对 LLM 返回值逐个 `clamp(min, max)`，防幻觉导致模型变形
4. **物理参数过滤**：排除 `Hair / Ribbon / Skirt / Bust / Sway / Rotation_ / Skinning`
   —— **与我们实测的 24 个物理输出参数完全吻合**（见 3.4 节）

另外它提到的工程经验也值得借鉴：缓存高频情感的结果、本地预设兜底、
`temperature` 降到 0.1~0.3 提高参数一致性、批量更新参数减少调用次数。

**与本项目现状的差距**：我们目前的动作逻辑是「7 个情感标签 → 5 个动作组」的**离散映射**
（见 3.5 节），正是 SoulLink 明确要取代的那套做法。改造成本可控，
且第 3.7 节列出的 18 个情感混合参数是现成的基础。

---

### 6.4 参考资料

- **Soullink Emotion SDK**（LLM/事件驱动的 Live2D 表演引擎，MIT）：<https://github.com/nanlingyin/soullink-emotion-sdk>
- **Live2D 初音未来免费模型**（UP：玄宝酱）：<https://www.bilibili.com/video/BV1B1Mo67E3g/>
- **Soullink Emotion SDK 技术演示**（UP：骥南凌音_official）：<https://www.bilibili.com/video/BV1MXKi6NEbR/>
- **MiniMax 开放平台**（TTS + 音色克隆 + ASR，5.5 节的 API provider）：
  - 接口概览（含全部语音模型）：<https://platform.minimaxi.com/docs/api-reference/api-overview>
  - 同步语音合成 HTTP：<https://platform.minimaxi.com/docs/api-reference/speech-t2a-http>
  - 音色快速复刻：<https://platform.minimaxi.com/docs/guides/speech-voice-clone>
  - 语音识别 ASR：<https://platform.minimaxi.com/docs/api-reference/speech-to-text>
- SoulLink_Live2D（LLM 驱动 Live2D 表情控制）：<https://github.com/nanlingyin/SoulLink_Live2D>
- LLM 表情控制原理文档：<https://github.com/nanlingyin/SoulLink_Live2D/blob/main/docs/LLM_EXPRESSION_PRINCIPLE.md>
- 纯 JSON 添加动作 + Agent 工作流：<https://github.com/shinshin86/live2d-add-motion-sample-web-ui>
- Live2D Cubism SDK 官方文档：<https://docs.live2d.com/>
- DeepSeek Vision 指南：<https://api-docs.deepseek.com/guides/vision>
- GSV-TTS-Lite：本地 GPT-SoVITS 高性能推理实现
- live2d-py（Cubism Native SDK 的 Python 绑定）

### 6.5 版权声明

初音未来的音色与形象归 **Crypton Future Media** 所有。
参考音频 `assets/voice/` 已在 `.gitignore` 中，**不随仓库分发**。

> ⚠️ **待确认**：`assets/live2d/` 下的模型素材目前**随公开仓库分发**。
> 若其来源包含「不可二传」条款的免费发布（见 6.3 ②），需要移出仓库。
> 详见 6.3 ② 的说明。

模型素材的使用请遵守 Live2D 的许可协议与各发布方的授权条款。
本项目的相关代码仅供本机个人学习。

---

*文档基线：视频对话功能（提交 `38a5922`）之后的版本*
