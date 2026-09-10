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
- **DeepSeek Agent 大脑**：通过 OpenAI 兼容接口接入 `deepseek-chat`（可换 `deepseek-reasoner`），支持 function calling。
- **角色设定**：完整人设（16 岁虚拟歌姬、活泼元气、喜欢葱和音乐），情感标签（`[HAPPY]` 等）实时驱动 Live2D 表情与动作。
- **记忆系统**：
  - 短期记忆：每次对话注入最近 20 条消息；
  - 长期记忆：LLM 通过 `write_memory` 工具自动提炼重要信息（姓名、喜好、约定等）存入 SQLite，之后每次对话都会引用；
  - 多会话管理 + 用户昵称记忆，重启不丢失。
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
│   └── config.py            # 配置读取
├── assets/                  # 资源
│   ├── live2d/miku/         # 初音 Live2D 模型（MIKU.moc3 + 表情/动作）
│   └── img/                 # 备用立绘
├── tools/                   # 开发辅助脚本
│   ├── smoke_live2d.py      # 最小渲染验证（排查显卡/驱动问题）
│   ├── selftest_chat.py     # 端到端自检（对话→TTS→口型）
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

想用**真正的初音音色**，需要装 GPT-SoVITS 推理后端：

```bat
rem 1. 装 PyTorch（NVIDIA 显卡，CUDA 12.8）
.venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128

rem 2. 装 GSV-TTS-Lite
.venv\Scripts\python.exe -m pip install -r requirements-tts.txt

rem 3. .env 里切换引擎，并给一段初音参考音频（5~10 秒干净人声）
TTS_ENGINE=sovits
TTS_REF_AUDIO=D:\path\to\miku_reference.wav
```

实测参考（RTX 3050 Laptop，与开发机同型号）：**RTF ≈ 0.125、显存 ≈ 0.8GB**，远快于实时。

> ⚠️ **版权**：初音未来的音色归 Crypton Future Media 所有。克隆出的音色请仅用于本机个人学习，不要分发。

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

## ❓ 常见问题

- **看不到 Miku / 窗口一片空白**：先跑 `python tools\smoke_live2d.py` 验证 OpenGL 与模型。若报显卡驱动问题，请更新显卡驱动。
- **`import PySide6.QtCore` 报 `ERROR_PROC_NOT_FOUND`**：装到了 PySide6 6.11。该版本的 wheel 缺 ICU DLL，请按 `requirements.txt` 约束装回 6.8.x。
- **回复是「演示模式」**：`.env` 中未配置或未正确配置 `DEEPSEEK_API_KEY`，或 `MOCK_MODE=true`。
- **没有声音**：检查系统默认播放设备；在设置里确认「语音输出」已开启。首次使用 `edge` 引擎需要联网。
- **语音输入没反应 / 转写失败**：首次使用需联网下载 Whisper 模型（默认 `small` 约 460MB）；确认麦克风可用且未被占用；可调整 `.env` 中 `STT_MODEL`（`base` 更轻）与 `STT_LANGUAGE`。
- **模型下载慢 / 下载失败**：国内网络默认走 `hf-mirror.com` 镜像；可在 `.env` 中设置 `STT_HF_ENDPOINT`（留空 = 官方源）。
- **想改窗口大小**：调 `.env` 里的 `WINDOW_WIDTH` / `WINDOW_HEIGHT`，模型会自动重新适配。

## ⚠️ 说明

- Live2D 模型资源（`assets/live2d/`）为项目内已有素材，仅供学习与个人使用，请遵守原作者的许可声明。
- **Live2D Cubism SDK 授权**：`live2d-py` 封装的是 Live2D 官方 Native SDK，使用需遵守 [Live2D 的发布许可](https://www.live2d.com/sdk/license/)（个人 / 小规模用途通常免费，但需确认是否需要刊登标识）。
- 本项目为学习用途的桌宠示例，不做生产级加固。
