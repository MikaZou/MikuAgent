# MikuAgent · 初音未来虚拟桌宠

一个以**初音未来**为角色的虚拟桌宠项目：前端以 **Live2D 模型**为主体，后端以 **DeepSeek API** 作为 Agent 大脑，内置完整的**角色设定**与**记忆系统**。

## 📸 运行截图

### 透明桌宠窗口版

![透明桌宠窗口版](https://cdn.jsdelivr.net/gh/MikaZou/MikuAgent@main/docs/screenshots/pet.png)

### Web 浏览器版

![Web 浏览器版](https://cdn.jsdelivr.net/gh/MikaZou/MikuAgent@main/docs/screenshots/web.png)

## 📥 新电脑安装（2 分钟上手）

在一台全新的电脑上，只需四步：

```bat
rem 1. 克隆仓库（需要已安装 Git；也可以直接在 GitHub 页面下载 ZIP 解压）
git clone git@github.com:MikaZou/MikuAgent.git
cd MikuAgent

rem 2. 一键搭建环境（自动创建 .venv 并安装依赖）
setup_windows.bat

rem 3. 编辑 .env，填入 DEEPSEEK_API_KEY（可选，不填则运行离线演示模式）
notepad .env

rem 4. 启动桌宠，浏览器会自动打开 http://127.0.0.1:8000
start.bat
```

想以**透明置顶桌宠窗口**运行，改执行 `start_pet.bat`。

## ✨ 功能特性

- **Live2D 初音未来**：使用 Cubism 4 渲染（pixi.js + pixi-live2d-display + Cubism Core 5.1），支持鼠标注视跟随、点击交互动作、情感表情（腮红/吃惊/眯眯眼等 exp3）与说话口型模拟。
- **悬停对话形态**：Miku 是页面的绝对主角——鼠标悬停在她身上时，下方浮现输入栏与发送按钮；她的回复以头顶气泡形式呈现（带打字机效果与情感标签），不显示完整聊天记录。
- **DeepSeek Agent 大脑**：通过 OpenAI 兼容接口接入 `deepseek-chat`（可换 `deepseek-reasoner`），支持 function calling。
- **角色设定**：完整人设（16 岁虚拟歌姬、活泼元气、喜欢葱和音乐），情感标签规则（`[HAPPY]` 等）会实时驱动 Live2D 表情与动作。
- **记忆系统**：
  - 短期记忆：每次对话注入最近 20 条消息作为上下文；
  - 长期记忆：LLM 通过 `write_memory` 工具自动提炼重要信息（姓名、喜好、约定等）存入 SQLite，之后每次对话都会引用；
  - 多会话管理 + 用户昵称记忆，重启不丢失。
- **桌宠形态**：可选 pywebview **Qt 后端**透明置顶窗口模式（`start_pet.bat`），Miku 像真正的桌宠一样浮在桌面上，透明区域直接露出桌面；悬停时舞台左上角提供「最小化 / 退出」按钮。
- **记忆隐藏化**：长期记忆仍由后端自动保存并在对话中引用，但桌宠界面上不再展示记忆列表，保持纯净的宠物体验。
- **离线演示模式**：未配置 API Key 时自动使用本地预设回复，前端功能可完整体验。
- **语音输入（按住说话）**：输入栏旁的 🎤 按钮支持按住说话，松开自动转写并发送；faster-whisper 本地转写（默认中文，模型首次自动下载，之后完全离线），无需 API Key。可在设置中开关。

## 📁 目录结构

```text
MikuAgent/
├── backend/                 # 后端（FastAPI + DeepSeek Agent）
│   ├── main.py              # API 路由 + 静态前端托管
│   ├── agent.py             # DeepSeek Agent（对话、工具调用、情感解析）
│   ├── persona.py           # 初音未来角色设定（系统提示词）
│   ├── stt.py              # 语音输入（麦克风录音 + faster-whisper 转写）
│   ├── memory.py            # 记忆系统（SQLite）
│   ├── config.py            # 配置读取
│   └── pet_window.py        # 可选：pywebview 透明桌宠窗口
├── frontend/                # 前端（原生 HTML/CSS/JS，无需构建）
│   ├── index.html           # 桌宠页面（Live2D 主体 + 悬停输入栏）
│   ├── css/style.css
│   ├── js/live2d.js         # Live2D 加载与表情/口型
│   ├── js/app.js            # 悬停输入、气泡回复、设置
│   ├── vendor/              # 本地化第三方库（pixi.js 等）
│   ├── img/                 # 备用立绘
│   └── live2d/miku/         # 初音 Live2D 模型（MIKU.moc3 + 表情/动作）
├── data/                    # 运行时数据库（自动生成，不入库）
├── requirements.txt
├── .env.example
├── setup_windows.bat        # 一键环境搭建
├── start.bat                # 一键启动（浏览器模式）
└── start_pet.bat            # 桌宠窗口模式（透明置顶）
```

## 🚀 快速开始

### 1. 环境要求

- Windows 10/11（macOS / Linux 可参照下方手动命令）
- Python 3.10+（或 Anaconda，`setup_windows.bat` 会自动探测本机已装的 Anaconda 环境）
- Git（用于克隆仓库；不装 Git 也可直接下载 ZIP 压缩包解压）
- 浏览器需支持 WebGL（现代 Chrome / Edge 即可）

### 2. 一键搭建 + 启动

```bat
setup_windows.bat   rem 创建 .venv 并安装依赖
start.bat           rem 启动后端并打开浏览器
```

启动后访问 **http://127.0.0.1:8000** 即可看到 Miku。

### 2.5 手动安装（macOS / Linux 或不想用脚本时）

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows 下用 .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # 然后编辑 .env 填入 DEEPSEEK_API_KEY
uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
```

### 3. 配置 DeepSeek API Key

编辑项目根目录的 `.env`：

```ini
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

Key 在 [DeepSeek 开放平台](https://platform.deepseek.com) 申请。配置后重启 `start.bat`；在「设置」（鼠标悬停 Miku 后右上角 ⚙）中可查看当前模式。

### 4.（可选）透明桌宠窗口模式

```bat
start_pet.bat
```

该模式通过 pywebview 打开**透明、置顶、无边框**的桌宠窗口，可以像真正的桌面宠物一样浮在桌面上。
透明窗口使用 **Qt 后端（PySide6 + QtWebEngine）**：Qt 原生支持逐像素透明，且鼠标/键盘输入正常
（pywebview 的 EdgeChromium 后端透明模式无法接收鼠标输入，故桌宠窗口固定走 Qt）。

## 🧠 记忆机制说明

- **短期记忆**：`MAX_HISTORY_MESSAGES`（默认 20）条最近消息会注入每次请求的上下文。
- **长期记忆**：对话中当 Miku 判断出现重要信息时，会调用 `write_memory` 工具，将内容按分类（用户信息/偏好/事件/约定/其他）和重要程度（1~5 星）存入 `data/mikuagent.db`。之后每次对话这些记忆都会出现在系统提示词中，让 Miku「记得」你。
- 在「设置」中填写的昵称会保存为元数据（`user_name`），角色设定会据此称呼你。

## 🔌 API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 服务状态、是否演示模式、模型信息 |
| POST | `/api/chat` | 发送消息 `{message, session_id?}`，返回 `{reply, emotion, session_id, mock}` |
| GET | `/api/sessions` | 会话列表 |
| POST | `/api/sessions` | 新建会话 |
| GET | `/api/sessions/{id}/messages` | 会话消息历史 |
| DELETE | `/api/sessions/{id}` | 删除会话 |
| GET | `/api/memory` | 长期记忆列表 |
| DELETE | `/api/memory/{id}` | 删除长期记忆 |
| POST | `/api/meta` | 设置元数据（如用户昵称） |
| POST | `/api/mic/start` | 开始麦克风录音（按住说话） |
| POST | `/api/mic/stop` | 停止录音并返回转写文本 |
| POST | `/api/mic/cancel` | 放弃本次录音 |

## 🎭 自定义角色设定

编辑 `backend/persona.py` 即可调整 Miku 的性格、爱好、说话风格与情感标签规则。情感标签与 Live2D 动作的映射在 `frontend/js/live2d.js` 的 `setEmotion()` 中。

## ❓ 常见问题

- **看不到 Live2D 模型**：请确认浏览器支持 WebGL；如仍失败会自动回退为静态立绘（`img/miku_avatar.png`）。
- **桌宠窗口不透明 / 背景异常**：请使用 `start_pet.bat` 启动（Qt 后端透明窗口）；也可以在普通浏览器访问 `http://127.0.0.1:8000/?pet=1` 预览桌宠布局。
- **回复是「演示模式」**：`.env` 中未配置或未正确配置 `DEEPSEEK_API_KEY`，或 `MOCK_MODE=true`。
- **DeepSeek 调用失败**：检查网络与 Key 是否有效；失败时后端会自动回退到演示回复，不会报错。
- **修改模型**：想换 `deepseek-reasoner`，修改 `.env` 中 `DEEPSEEK_MODEL` 即可。
- **语音输入没反应 / 转写失败**：首次使用需联网下载 Whisper 模型（默认 `small` 约 460MB）；确认麦克风可用且未被其他程序占用；可在 `.env` 中调整 `STT_MODEL`（`base` 更轻、`small` 更准）与 `STT_LANGUAGE`。
- **模型下载慢 / 下载失败**：国内网络默认走 hf-mirror.com 镜像；如需切换，可在 `.env` 中设置 `STT_HF_ENDPOINT`（留空 = 官方源）。

## ⚠️ 说明

- Live2D 模型资源（`frontend/live2d/`）为项目内已有素材，仅供学习与个人使用，请遵守原作者的许可声明。
- 本项目为学习用途的桌宠示例，未做生产级鉴权（API 仅监听 `127.0.0.1`）。
