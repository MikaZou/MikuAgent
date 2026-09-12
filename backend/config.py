"""MikuAgent 全局配置：读取环境变量与 .env 文件。"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
# deepseek-flash = DeepSeek-V4.1-Flash（2026-09 GA），支持视觉输入且比 deepseek-chat 快。
# 旧名 deepseek-chat 仍可用，但不在账号模型列表里，有下线风险；需要回退时改这里即可。
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip()
DEEPSEEK_TEMPERATURE = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.9"))
# 思考模式：disabled（默认，快）/ enabled / auto（不发送该参数，用服务端默认）
# 注意：flash 默认是 enabled，会变慢、且**静默忽略 temperature**；
# 且 max_tokens 偏小时可能只输出推理内容、正文为空。桌宠场景用 disabled。
DEEPSEEK_THINKING = os.getenv("DEEPSEEK_THINKING", "disabled").strip().lower()

# ===== MiniMax（云端 TTS / ASR，见 docs/TECHNICAL.md 5.5 节） =====
# 走云端可以把本地 GPT-SoVITS 的重计算搬走：省约 1.4~2.2GB 显存 + 2.9GB 内存。
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY", "").strip()
# 国内站 api.minimaxi.com；国际站 api.minimax.io
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com").strip()
# speech-2.8-hd（音质最好）/ speech-2.8-turbo（最快）；另有 2.6、02 系列
MINIMAX_TTS_MODEL = os.getenv("MINIMAX_TTS_MODEL", "speech-2.8-hd").strip()
# 音色克隆得到的 voice_id；空则回退到系统音色
MINIMAX_VOICE_ID = os.getenv("MINIMAX_VOICE_ID", "").strip()
MINIMAX_ASR_MODEL = os.getenv("MINIMAX_ASR_MODEL", "asr-1.0").strip()
# 语速。克隆自 v4c 发布会致辞，那段本身语速偏慢：
#   实测 speed=1.0 → 2.2 字/秒（明显偏慢）；1.8 → 3.7 字/秒（接近自然）
#   本地 GPT-SoVITS 同文本约 4.6 字/秒。可用 tools/minimax_speed_test.py 复测。
MINIMAX_SPEED = float(os.getenv("MINIMAX_SPEED", "1.8"))
MINIMAX_TIMEOUT = int(os.getenv("MINIMAX_TIMEOUT", "120"))
HAS_MINIMAX_KEY = bool(MINIMAX_API_KEY) and not MINIMAX_API_KEY.startswith("sk-xxx")

MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
MOCK_MODE = _flag("MOCK_MODE")

# ===== 远程服务（路线 A：手机作为瘦客户端连到本机） =====
# 在 PC 端桌宠进程里额外起一个 HTTP + WebSocket 服务，
# 手机浏览器访问 http://<PC的局域网IP>:<端口>/ 即可使用。
# 关掉它不影响桌宠本身。
#
# ⚠️ 安全默认值：**默认关闭**。
# 该服务监听 0.0.0.0 且没有鉴权（同一局域网内任何设备都能连上来对话，
# 会消耗你自己的 DeepSeek / MiniMax 额度），所以必须由用户显式开启。
# 首次设置向导里有这一项；也可在这里或 .env 里打开。
REMOTE_ENABLED = _flag("REMOTE_ENABLED")
REMOTE_PORT = int(os.getenv("REMOTE_PORT", "8765"))
# 手机端要加载的 JS 库来源（PIXI.js / pixi-live2d-display）。
# 网络受限时换成可用镜像，例如 https://fastly.jsdelivr.net/npm
REMOTE_CDN = os.getenv("REMOTE_CDN", "https://cdn.jsdelivr.net/npm").rstrip("/")

# ===== 视觉（视频对话：让 Miku 看见主人） =====
# 总开关；实际以设置面板的 QSettings 为准，这里只是默认值
VISION_ENABLED = _flag("VISION_ENABLED")
# 摄像头索引（OpenCV 的 device index）
VISION_CAMERA_INDEX = int(os.getenv("VISION_CAMERA_INDEX", "0"))
# 图片精细度：low(512x512，最省) / high / original / auto
# 桌宠场景是「看到人」而非 OCR，low 足够，实测约 192 tokens/张
VISION_DETAIL = os.getenv("VISION_DETAIL", "low").strip().lower()
# 发送前等比缩放的最长边（控制体积与 token）
VISION_MAX_SIDE = int(os.getenv("VISION_MAX_SIDE", "768"))
VISION_JPEG_QUALITY = int(os.getenv("VISION_JPEG_QUALITY", "80"))
# 实时预览帧率（仅本地显示，不上传）
VISION_PREVIEW_FPS = int(os.getenv("VISION_PREVIEW_FPS", "5"))

# ===== 语音输入（STT：按住说话） =====
# 转写通道：local-whisper（本地，约 1GB 内存）/ minimax（云端 API，几乎不吃内存）
STT_TRANSCRIBER = os.getenv("STT_TRANSCRIBER", "local-whisper").strip().lower()
# Whisper 模型：tiny / base / small / medium（首次使用自动下载，越大越准越慢）
STT_MODEL = os.getenv("STT_MODEL", "small").strip()
# 识别语言：zh / ja / en 等；留空 = 自动检测
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "zh").strip()
STT_SAMPLE_RATE = int(os.getenv("STT_SAMPLE_RATE", "16000"))
# 短于此秒数的录音会被忽略（防误触）
STT_MIN_DURATION = float(os.getenv("STT_MIN_DURATION", "0.3"))
# HuggingFace 下载镜像（国内网络默认用 hf-mirror.com；留空 = 官方源）
STT_HF_ENDPOINT = os.getenv("STT_HF_ENDPOINT", "https://hf-mirror.com").strip()

DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "mikuagent.db"

# ===== 原生桌宠资源（原 frontend/ 已迁移到 assets/） =====
ASSETS_DIR = BASE_DIR / "assets"
MODEL_PATH = ASSETS_DIR / "live2d" / "miku" / "miku.model3.json"
AVATAR_PATH = ASSETS_DIR / "img" / "miku_avatar.png"
WINDOW_STATE_FILE = DATA_DIR / "window.json"

# 窗口尺寸（透明无边框）
WINDOW_WIDTH = int(os.getenv("WINDOW_WIDTH", "360"))
# 高度不能随便减：顶部按钮(40) + 气泡(最多132) + 底部输入栏(70) 是固定开销，
# 减到 560 以下时模型会被迫骤降一档。360x600 是实测的平衡点，
# 改尺寸请用 tools/measure_framing.py 重新量取景。
WINDOW_HEIGHT = int(os.getenv("WINDOW_HEIGHT", "600"))
WINDOW_FPS = int(os.getenv("WINDOW_FPS", "60"))

# ===== 语音输出（TTS） =====
TTS_ENABLED = os.getenv("TTS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
TTS_ENGINE = os.getenv("TTS_ENGINE", "edge").strip().lower()
TTS_VOICE = os.getenv("TTS_VOICE", "zh-CN-XiaoyiNeural").strip()
TTS_RATE = os.getenv("TTS_RATE", "+0%").strip()
TTS_PITCH = os.getenv("TTS_PITCH", "+25Hz").strip()
TTS_VOLUME = os.getenv("TTS_VOLUME", "+0%").strip()
TTS_DEVICE = os.getenv("TTS_DEVICE", "cuda").strip().lower()
TTS_REF_AUDIO = os.getenv("TTS_REF_AUDIO", "").strip()
TTS_PROMPT_AUDIO = os.getenv("TTS_PROMPT_AUDIO", "").strip()
TTS_PROMPT_TEXT = os.getenv("TTS_PROMPT_TEXT", "").strip()
TTS_GPT_MODEL = os.getenv("TTS_GPT_MODEL", "").strip()
TTS_SOVITS_MODEL = os.getenv("TTS_SOVITS_MODEL", "").strip()
TTS_MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "200"))
TTS_CACHE_DIR = DATA_DIR / "tts-cache"
# GSV-TTS-Lite 的预训练模型目录（默认落在 ~/.cache/gsv，改到项目内便于管理）
TTS_MODELS_DIR = Path(os.getenv("TTS_MODELS_DIR", str(DATA_DIR / "gsv-models")))
# 是否启用 BERT（中文效果更好，但多占约 0.65GB 显存）
TTS_USE_BERT = os.getenv("TTS_USE_BERT", "true").strip().lower() in {"1", "true", "yes", "on"}
# sovits 合成服务（独立进程）监听端口与就绪超时
TTS_SERVER_PORT = int(os.getenv("TTS_SERVER_PORT", "18520"))
TTS_SERVER_TIMEOUT = int(os.getenv("TTS_SERVER_TIMEOUT", "180"))
# 单次合成的 socket 超时（首次含模型加载，给足时间）
TTS_SYNTH_TIMEOUT = int(os.getenv("TTS_SYNTH_TIMEOUT", "300"))
# 启动时是否预热 CUDA graph（多花约 30~40s 启动，换取之后全程稳态速度）
TTS_WARMUP = os.getenv("TTS_WARMUP", "true").strip().lower() in {"1", "true", "yes", "on"}
# 合成缓存上限（MB），超出按最近最少使用淘汰
TTS_CACHE_MAX_MB = int(os.getenv("TTS_CACHE_MAX_MB", "200"))

# GPT-SoVITS 的英文 G2P 会用到 nltk（文本里出现英文单词时触发）。
# 默认数据目录在用户目录下且国内下载源常连不上，这里固定到项目内。
NLTK_DATA_DIR = DATA_DIR / "nltk_data"
os.environ.setdefault("NLTK_DATA", str(NLTK_DATA_DIR))


def resolve_path(value: str) -> Path:
    """把配置里的路径解析成绝对路径（相对路径按项目根目录算）。"""
    p = Path(value).expanduser()
    return p if p.is_absolute() else (BASE_DIR / p)

HAS_API_KEY = bool(DEEPSEEK_API_KEY) and DEEPSEEK_API_KEY != "sk-xxxxxxxx"
