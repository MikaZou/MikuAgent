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
WINDOW_WIDTH = int(os.getenv("WINDOW_WIDTH", "400"))
WINDOW_HEIGHT = int(os.getenv("WINDOW_HEIGHT", "580"))
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

HAS_API_KEY = bool(DEEPSEEK_API_KEY) and DEEPSEEK_API_KEY != "sk-xxxxxxxx"
