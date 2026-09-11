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
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
DEEPSEEK_TEMPERATURE = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.9"))

MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
MOCK_MODE = _flag("MOCK_MODE")

# ===== 语音输入（STT：按住说话） =====
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
