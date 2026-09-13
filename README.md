# MikuAgent · 初音未来虚拟桌宠

一个以**初音未来**为角色的**原生桌面宠物**：用 **PySide6 + Live2D Cubism Native SDK** 直接渲染，**不经过任何 Web Engine**；后端以 **DeepSeek API** 作为 Agent 大脑，内置完整的**角色设定**、**记忆系统**、**语音对话**（说 + 听）。

> 早期版本是「FastAPI + 静态网页 + pywebview 套壳」，现已整体替换为单进程原生实现。
> 旧版仍保留在 git 历史中。

## 📥 新电脑安装（2 分钟上手）

```bat
rem 1. 克隆仓库（需要已安装 Git；也可以直接在 GitHub 页面下载 ZIP 解压）
git clone git@github.com:MikaZou/MikuAgent.git
cd MikuAgent

rem 2. 一键搭建环境（自动创建 .venv 并安装依赖）
setup_windows.bat

rem 3. 编辑 .env，填入 DEEPSEEK_API_KEY（可选，不填则运行离线演示模式）
notepad .env

rem 4. 启动桌宠（无控制台窗口）
start.bat
```

**环境要求**

- Windows 10/11
- **Python 3.10 或以上**（Live2D 渲染依赖 `live2d-py`，官方只提供 3.10+ 的预编译包）
- 支持 OpenGL 3.3+ 的显卡（核显即可；有独显更流畅）
- 一键脚本会自动探测 Anaconda / 官方 Python

## ✨ 功能特性

- **原生 Live2D 渲染**：直接调用 Live2D Cubism Native SDK（Cubism Core 5.1），无 Chromium、无本地端口、无 WebView。支持鼠标注视跟随、点击互动、情感表情（腮红/吃惊/眯眯眼等 exp3）、自动眨眼与呼吸。
- **真正的透明桌宠窗口**：无边框、逐像素透明、始终置顶、不进任务栏，按住模型即可拖动。
- **系统托盘**：显示/隐藏、回到屏幕中央、设置、退出。
- **DeepSeek Agent 大脑**：通过 OpenAI 兼容接口接入 `deepseek-flash`（DeepSeek-V4.1-Flash，默认关闭思考模式以保响应速度），支持 function calling；可换 `deepseek-chat` / `deepseek-reasoner`。
- **角色设定**：完整人设（16 岁虚拟歌姬、活泼元气、喜欢葱和音乐），情感标签（`[HAPPY]` 等）实时驱动 Live2D 表情与动作。
- **记忆系统**：
  - 短期记忆：每次对话注入最近 20 条消息；
  - 长期记忆：LLM 通过 `write_memory` 工具自动提炼重要信息（姓名、喜好、约定等）存入 SQLite，之后每次对话都会引用；
  - 多会话管理 + 用户昵称记忆，重启不丢失。
- **视频对话（让 Miku 看见你）**：输入栏 📹 一开，摄像头保持打开并显示实时预览；**每次按住说话松手的那一刻自动抓一帧**，和语音一起发给 Miku，她就能针对你的样子回应。见下方「视频对话」章节。
- **语音输出（Miku 说话）**：回复自动合成语音播放，**并用真实音频包络驱动口型**（播放与口型读同一份 WAV，天然同步）。支持在线 `edge` 引擎与本地 `sovits` 初音音色引擎。
- **语音输入（按住说话）**：输入栏 🎤 按钮按住说话，松开自动转写并发送；faster-whisper 本地转写（默认中文），无需 API Key。
- **离线演示模式**：未配置 API Key 时自动使用本地预设回复，前端功能可完整体验。
- **线程安全**：所有阻塞调用（LLM、语音合成、语音转写）都在后台线程，UI 全程不冻结。

## 📁 目录结构

```text
MikuAgent/
├── main.py                  # 入口：QApplication + 桌宠窗口
├── ui/                      # 原生 UI 层（PySide6）
│   ├── pet_window.py        # 主窗口：画布 + 气泡 + 输入栏 + 角标按钮
│   ├── live2d_view.py       # Live2D 渲染、表情/动作、口型、命中检测
│   ├── bubble.py            # 头顶气泡（情感标签 + 打字机）
│   ├── input_bar.py         # 输入栏（含按住说话）
│   ├── chat_worker.py       # 后台线程：对话 / 转写 / 合成
│   ├── audio.py             # WAV 播放（sounddevice）
│   ├── settings_dialog.py   # 设置面板
│   └── tray.py              # 系统托盘
├── backend/                 # Agent 核心（与 UI 解耦，纯 Python）
│   ├── agent.py             # DeepSeek Agent（对话、工具调用、情感解析）
│   ├── persona.py           # 初音未来角色设定（系统提示词）
│   ├── memory.py            # 记忆系统（SQLite）
│   ├── stt.py               # 语音输入（麦克风 + faster-whisper）
│   ├── tts.py               # 语音输出（文本清洗 + 合成 + 缓存）
│   ├── tts_server.py        # 可选：GPT-SoVITS 合成服务（独立进程，见 TTS 一节）
│   └── config.py            # 配置读取
├── assets/                  # 资源
│   ├── live2d/miku/         # 初音 Live2D 模型（MIKU.moc3 + 表情/动作）
│   └── img/                 # 备用立绘
├── tools/                   # 开发辅助脚本
│   ├── smoke_live2d.py      # 最小渲染验证（排查显卡/驱动问题）
│   ├── selftest_chat.py     # 端到端自检（对话→TTS→口型）
│   ├── diag_expressions.py  # 逐个 exp3 表情截图对照（排查素材问题）
│   ├── diag_emotions.py     # 逐个情感截图对照
│   ├── diag_tts.py          # TTS 引擎连通性诊断
│   ├── prepare_ref_audio.py # 从媒体里挑干净的参考音频（含质量指标）
│   ├── test_sovits.py       # 直接测 GPT-SoVITS（绕过服务进程）
│   ├── fetch_wheel.py       # 支持断点续传的下载器（curl 在本机 TLS 不可用）
│   └── capture_window.ps1   # 抓取窗口截图
├── data/                    # 运行时数据（自动生成，不入库）
│   ├── mikuagent.db         # 会话 / 消息 / 长期记忆
│   ├── window.json          # 窗口位置记忆
│   └── tts-cache/           # 语音合成缓存
├── requirements.txt
├── requirements-tts.txt     # 可选：本地初音音色（体积大）
├── .env.example
├── setup_windows.bat        # 一键环境搭建
└── start.bat                # 一键启动
```

## 🚀 快速开始

### 1. 一键搭建 + 启动

```bat
setup_windows.bat   rem 创建 .venv 并安装依赖
start.bat           rem 启动桌宠
```

`start.bat` 用 `pythonw.exe` 启动，**不会留下黑色控制台窗口**。

### 2. 手动安装（或不想用脚本时）

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python main.py
```

### 3. 配置 DeepSeek API Key

编辑项目根目录的 `.env`：

```ini
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

Key 在 [DeepSeek 开放平台](https://platform.deepseek.com) 申请。配置后重启 `start.bat`；在「设置」（悬停 Miku 后右上角 ⚙，或右键托盘）中可查看当前模式。

## 🎤 语音输出（TTS）

Miku 会把回复**念出来**。开箱即用，无需额外配置；可在设置里随时开关。

### 默认引擎：`edge`（微软在线 TTS）

零模型下载，依赖只有 30KB。音色可在 `.env` 调整：

```ini
TTS_ENGINE=edge
TTS_VOICE=zh-CN-XiaoyiNeural   # 晓伊，年轻活泼的少女音
TTS_PITCH=+25Hz                # 音调调高更接近动漫少女音
```

### 可选引擎：`sovits`（本地初音音色）

想用**真正的初音音色**，需要装 GPT-SoVITS 推理后端。已在本机实测跑通（RTX 3050 Ti Laptop 4GB）。

**1. 装 PyTorch。** cu128 的 torch wheel 有 **2.6GB**，pip 不支持断点续传，国内直连容易断，
用项目自带的下载器（支持续传，中断后重跑同一条命令即可）：

```bat
python tools\fetch_wheel.py "https://download.pytorch.org/whl/cu128/torch-2.11.0%%2Bcu128-cp310-cp310-win_amd64.whl" ".tmp/wheels/torch.whl"
python tools\fetch_wheel.py "https://download.pytorch.org/whl/cu128/torchaudio-2.11.0%%2Bcu128-cp310-cp310-win_amd64.whl" ".tmp/wheels/torchaudio.whl"
.venv\Scripts\python.exe -m pip install .tmp\wheels\torch.whl .tmp\wheels\torchaudio.whl
```

**2. 装 GSV-TTS-Lite。** 预训练模型（约 1.6GB）首次运行会从 ModelScope 自动下载到 `data/gsv-models/`。

```bat
.venv\Scripts\python.exe -m pip install -r requirements-tts.txt
```

**3. 准备参考音频**（5~10 秒、单人、**干声无伴奏**）。带伴奏的素材会把伴奏一起学进去：

```bat
rem 从任意媒体里自动挑一段最干净的语音
python tools\prepare_ref_audio.py 你的素材.m4a --out assets\voice\miku_ref.wav --seconds 6
```

判断依据是脚本会打印的**噪声底**（低于 -45dB 才算干声）、**调制深度**（>7dB 像语音，<5dB 像音乐）
和**真静音占比**。歌曲/演唱会录像通常全程带 BGM，不建议直接用。

**4. 拿到参考音频的转写文本。** GPT-SoVITS 的 `prompt_audio_text` 是必填的，
且必须与音频内容一致 —— 用项目自带的 Whisper 转一下即可：

```bat
.venv\Scripts\python.exe -c "from faster_whisper import WhisperModel; m=WhisperModel('small',device='cpu',compute_type='int8'); s,_=m.transcribe('assets/voice/miku_ref.wav',language='zh'); print(''.join(x.text for x in s))"
```

**5. 在 `.env` 里切换：**

```ini
TTS_ENGINE=sovits
TTS_REF_AUDIO=assets/voice/miku_ref.wav
TTS_PROMPT_TEXT=这里填上一步转出来的文本
TTS_USE_BERT=true          # 中文效果更好；显存吃紧设 false
```

**实机数据**（RTX 3050 Ti Laptop 4GB）：

| 项目 | 数值 |
| --- | --- |
| 显存 | 开 BERT 约 **2.2GB**，关掉约 1.7GB |
| 启动到可发声 | 约 **35 秒**（torch 导入 12s + 模型加载 6s + 预热 12s） |
| 稳态合成速度 | **RTF 0.17 ~ 0.37**（比实时快 3~6 倍） |
| 首字延迟 | 约 **1.5 秒**（逐句流水线，见下） |

> **关于「推理慢」**：GSV 用静态 CUDA graph 缓存，**每种句长的第一次推理都要现捕获计算图** ——
> 实测同长度第 1 次 9.6 秒、第 3 次 1.9 秒。所以启动时会按短/中/长各跑两遍预热
> （`TTS_WARMUP=true`，只多花 12 秒），之后全程都是稳态速度。
> 若关掉预热，前几条回复会慢 3~6 倍。

> **两处踩过的坑，已内置处理：**
> - 4GB 显存要和桌宠共享，必须给 PyTorch 设 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`，
>   否则加载参考音频时会 CUDA OOM。程序启动合成服务时会自动设置。
> - 合成跑在**独立进程**里（`backend/tts_server.py`，监听 `127.0.0.1:18520`）。
>   原因见 `backend/tts_server.py` 顶部注释：Python 3.10 + torch 2.11 下，
>   在 Qt 应用的后台线程里首次 `import torch` 会稳定抛
>   `TypeError: Plain typing.Self is not valid as type argument`，而在干净进程里正常。

> ⚠️ **版权**：初音未来的音色归 Crypton Future Media 所有。参考音频与克隆出的音色
> 请仅用于本机个人学习，**不要分发**（`assets/voice/` 已在 `.gitignore` 中）。

### 逐句流水线

GPT-SoVITS 是「整段合成完才出声」。回复会被按句切开，**第一句合成完就先播**，
后台同时合成下一句 —— 首字延迟从「整段合成时间」降到「第一句合成时间」。
实测一条 94 字回复切成 3 段（8.83 / 8.32 / 3.98 秒），首段 1.52 秒就出声。

### 情感 → 语速

`agent` 解析出的情感标签会映射成 GSV 的 `speed` 参数，
让不同情绪的语速有区别（`MOTIVATED` 1.12 / `HAPPY` 1.08 / `NORMAL` 1.0 / `SAD` 0.92）。

> 注意：单次合成的时长本身有约 30% 的采样波动，所以听感差异没有参数差距那么显著。

### 口型同步

合成结果统一产出 WAV，播放（`sounddevice`）和口型（`live2d.utils.lipsync.WavHandler`）读**同一份文件**，因此嘴型与声音天然同步，无需额外对齐。若音频不可用，会自动退回正弦模拟口型。

## 🧠 记忆机制说明

- **短期记忆**：`MAX_HISTORY_MESSAGES`（默认 20）条最近消息会注入每次请求的上下文。
- **长期记忆**：对话中当 Miku 判断出现重要信息时，会调用 `write_memory` 工具，将内容按分类（用户信息/偏好/事件/约定/其他）和重要程度（1~5 星）存入 `data/mikuagent.db`。之后每次对话这些记忆都会出现在系统提示词里，让 Miku「记得」你。
- 在「设置」中填写的称呼会保存为元数据（`user_name`），角色设定会据此称呼你。

## 🎭 自定义角色设定

编辑 `backend/persona.py` 即可调整 Miku 的性格、爱好、说话风格与情感标签规则。

情感标签与 Live2D 动作/表情的映射在 `ui/live2d_view.py` 的 `EMOTION_MOTION` / `EMOTION_EXPRESSION` 两张表里：

```python
EMOTION_MOTION     = {"HAPPY": "Tap", "ANGRY": "Flick", ...}
EMOTION_EXPRESSION = {"HAPPY": "Saihong", "SURPRISED": "Chijing", ...}
```

模型取景（大小与垂直位置）在 `ui/pet_window.py` 顶部的 `FRAMING_SCALE` / `FRAMING_OFFSET`。

## 🛠️ 技术栈

> 📖 **完整技术文档见 [`docs/TECHNICAL.md`](docs/TECHNICAL.md)** —— 涵盖 TTS 与 Live2D 动作的实现原理、
> 实测性能数据（显存拆解 / 延迟优化）、视频对话的实现，以及后续扩展方案
> （显存优化路径、Agent 驱动的 Live2D 动画可行性）。
>
> 🔖 **参考项目笔记见 [技术文档 6.3 节](docs/TECHNICAL.md)** —— 含
> [Soullink Emotion SDK](https://github.com/nanlingyin/soullink-emotion-sdk)（连续情绪 / FACS / 分层动作混合）、
> [Live2D 初音未来免费模型](https://www.bilibili.com/video/BV1B1Mo67E3g/)（含授权条款提醒）等。

| 技术 | 版本 | 用途 |
| --- | --- | --- |
| Python | 3.10+ | 运行时 |
| **PySide6-Essentials** | ≥6.6,<6.9 | Qt 原生 UI 框架 |
| **live2d-py** | 0.7.0.4 | Live2D Cubism Native SDK 的 Python 绑定（`live2d.v3`） |
| Live2D Cubism Core | 5.1 | moc3 模型解析与渲染 |
| PyOpenGL | 3.1 | OpenGL 上下文与 `glReadPixels` 命中检测 |
| OpenAI SDK | 1.x | OpenAI 兼容协议接入 DeepSeek |
| SQLite | 内置 | 会话与长期记忆 |
| faster-whisper | 1.x | 本地语音转写（STT） |
| sounddevice | 0.5 | 音频播放与麦克风采集 |
| edge-tts | 7.x | 在线语音合成（默认 TTS 引擎） |
| PyAV | 17.x | mp3 → WAV 解码重采样 |

模型本体：`MIKU.moc3`（moc3 格式 v4）+ `miku.model3.json`（Version 3，Cubism 4 规范），含物理演算、表情（exp3）与 13 组动作（motion3）。

## 🎥 视频对话（让 Miku 看见你）

输入栏那个 📹 按钮就是开关。开启后：

1. 摄像头保持打开，左下角显示实时预览（**指示灯常亮 = 正在工作**）
2. 你**按住 🎤 说话、松手的那一刻**，自动抓取一帧画面
3. 这一帧和转写出来的文字一起发给 Miku —— 她就能针对你的样子回应

抓帧读的是预览线程缓存的最新帧（最多落后 200ms），**与 Whisper 转写并行，不增加等待时间**。

### 隐私

- 摄像头**只在视频对话开启期间**打开，关掉按钮立即释放，LED 熄灭
- **每轮对话只上传 1 帧**，不是持续上传视频流
- 画面只存在于内存，**不写入本地磁盘**；聊天记录里只保存 `[图片]` 占位符
- 画面会**上传到 DeepSeek 云端**用于识别 —— 不愿联网就不要开

### 没有摄像头也能用

🖥️ 按钮可以把**当前屏幕截图**发给 Miku（"帮我看看这个报错"之类）。
这条路径**不需要任何额外依赖**，走 PySide6 自带的截图接口。

想用摄像头需要装可选依赖：

```bat
pip install -r requirements-vision.txt
```

（`opencv-python-headless`，约 44MB。用 headless 版是为了不捆绑第二套 Qt 造成冲突。）

### 诊断工具

```bat
python tools\diag_vision.py camera     # 探测摄像头并抓一帧存到 .diag\ 查看
python tools\diag_vision.py api        # 验证 DeepSeek 视觉 API 生效 + 量 token 成本
python tools\diag_vision.py pipeline   # 不需要摄像头的端到端验证
```

## 📱 手机端

PC 端**仍然是原生桌宠窗口**，手机只是它的一个瘦客户端 —— 所有 AI 逻辑都在 PC 上跑。
三端（桌面 / 手机 / 网页）**共用同一个进程里的一批对象**（agent / 记忆 / TTS / STT），
所以共享长期记忆与**当天的会话**：在桌面聊完切到手机能接上同一个话题。

手机端有两种入口，**推荐用原生 App**：

### 方式一：原生 Android App（推荐）

`android/` 下的 Kotlin 客户端。**WebView 只负责渲染 Live2D，不联网**；
WebSocket / 录音 / 播放 / 口型 / 相机全部由 Kotlin 持有。

**为什么不用浏览器**：`http://192.168.x.x` **不是安全上下文**，
浏览器里 `navigator.mediaDevices` 直接是 `undefined`，麦克风和摄像头根本用不了
（这不是权限问题，加什么参数都没用）。原生端没有这个限制。

**用法**

1. PC 上启动桌宠（`start.bat`），控制台会打印形如 `http://192.168.x.x:8765/` 的地址
2. 手机连**同一个 WiFi**，装好 APK 后打开，填入那个地址
3. 打字 / 按住 🎤 说话 / 点 📷 拍照 / 点 📹 开视频对话

**能做**：Live2D 渲染与口型、文字对话、语音输入输出、拍照与视频对话、情绪表情动作。

**说明**

- **模型不打进 APK**（授权要求「不可二传二改」），首次运行从 PC 拉取
  （约 34MB，逐文件 sha1 校验后缓存在应用私有目录），之后启动不再下载
- **零 CDN**：PIXI / pixi-live2d-display / Cubism Core 全部打进 APK，
  经 `WebViewAssetLoader` 挂在 `https://appassets.androidplatform.net/` 下
- 想用 USB 线联调可以 `adb reverse tcp:8765 tcp:8765`，然后用 `127.0.0.1` 连 ——
  **完全绕开 WiFi、路由器隔离和防火墙**
- 自行构建：`cd android && gradle assembleDebug`（需要 JDK 17+ 与 Android SDK 35），
  详见 `android/README.md`（含模拟器、真机与排查）

### 方式二：浏览器（备用入口）

1. PC 上启动桌宠，手机连**同一个 WiFi**，浏览器打开控制台打印的地址
2. 直接打字 / 按住 🎤 说话 / 点 📷 拍照发给她

适合快速验证，或在没有装 APK 的设备上用。**但麦克风与摄像头在手机上不可用**
（原因见上），只能打字和拍照；桌面浏览器打开则完全正常。

**说明**

- 浏览器端需要能访问 CDN 加载 PIXI.js 与 pixi-live2d-display；受限时在 `.env` 里
  改 `REMOTE_CDN` 为可用镜像。**Live2D 模型文件始终由 PC 提供，不依赖外网。**
- 端口默认 `8765`（`.env` 的 `REMOTE_PORT`）；不想要就设 `REMOTE_ENABLED=false`，
  **关掉它完全不影响 PC 端桌宠**。
- 为什么手机端也建议走云端 TTS/STT：本地 GPT-SoVITS + Whisper 要 3.6GB 内存 +
  2.2GB 显存，PC 自己都很紧张；切到 MiniMax 后 PC 只用 **248MB 内存 + 538MiB 显存**。


## ❓ 常见问题

- **看不到 Miku / 窗口一片空白**：先跑 `python tools\smoke_live2d.py` 验证 OpenGL 与模型。若报显卡驱动问题，请更新显卡驱动。
- **`import PySide6.QtCore` 报 `ERROR_PROC_NOT_FOUND`**：装到了 PySide6 6.11。该版本的 wheel 缺 ICU DLL，请按 `requirements.txt` 约束装回 6.8.x。
- **回复是「演示模式」**：`.env` 中未配置或未正确配置 `DEEPSEEK_API_KEY`，或 `MOCK_MODE=true`。
- **回复变短 / 变空 / 很奇怪**：若把 `DEEPSEEK_MODEL` 改成 `deepseek-flash`，务必同时设
  `DEEPSEEK_THINKING=disabled`。flash **默认开启思考模式** —— 会变慢、**静默忽略 `temperature`**，
  且 `max_tokens` 偏小时可能只输出推理内容、正文为空。想回退旧模型就把 `DEEPSEEK_MODEL`
  改回 `deepseek-chat`（旧名仍可用，但不在账号模型列表里，有下线风险）。
- **📹 点了提示打不开摄像头**：多为被微信 / 腾讯会议等占用，或系统未授予摄像头权限
  （设置 → 隐私和安全性 → 摄像头）。没有摄像头就改用 🖥️ 截屏。
- **TTS 合成服务启动失败（日志里是 `CUDA error: unknown error`）**：多见于显存/驱动被其他
  程序挤占（浏览器、视频会议、游戏）。服务单独运行是正常的（`python backend\tts_server.py`），
  只有和桌宠争同一块 GPU 时才可能触发。先关掉一些占显存的程序再重启桌宠。
- **没有声音**：检查系统默认播放设备；在设置里确认「语音输出」已开启。首次使用 `edge` 引擎需要联网。
- **`sovits` 启动很慢 / 前几句特别慢**：正常。torch 导入约 12 秒、模型加载约 6 秒、
  预热约 12 秒，合计约 35 秒后才可发声；预热完就一直是稳态速度。
  嫌启动慢可设 `TTS_WARMUP=false`，代价是每种句长的头几次合成会慢 3~6 倍。
- **切 `TTS_DEVICE=cpu` 报 `No viable backend for scaled_dot_product_attention`**：
  `gsv_tts` 的注意力后端是按 `torch.cuda.is_available()` 选的，**有显卡的机器**
  即使用 `device=cpu` 也会选到 CUDA 专用后端，因而失败。没有显卡时请改用 `TTS_ENGINE=edge`。
- **语音输入没反应 / 转写失败**：首次使用需联网下载 Whisper 模型（默认 `small` 约 460MB）；确认麦克风可用且未被占用；可调整 `.env` 中 `STT_MODEL`（`base` 更轻）与 `STT_LANGUAGE`。
- **模型下载慢 / 下载失败**：国内网络默认走 `hf-mirror.com` 镜像；可在 `.env` 中设置 `STT_HF_ENDPOINT`（留空 = 官方源）。
- **想改窗口大小**：调 `.env` 里的 `WINDOW_WIDTH` / `WINDOW_HEIGHT`，模型会自动重新适配。
  **注意**：改完要用 `python tools\measure_framing.py <宽> <高>` 重新量取景，并同步
  `ui/pet_window.py` 的 `FRAMING_SCALE` / `FRAMING_OFFSET`，否则气泡会和模型重叠。

## ⚠️ 说明

- Live2D 模型资源（`assets/live2d/`）为项目内已有素材，仅供学习与个人使用，请遵守原作者的许可声明。
- **Live2D Cubism SDK 授权**：`live2d-py` 封装的是 Live2D 官方 Native SDK，使用需遵守 [Live2D 的发布许可](https://www.live2d.com/sdk/license/)（个人 / 小规模用途通常免费，但需确认是否需要刊登标识）。
- 本项目为学习用途的桌宠示例，不做生产级加固。
