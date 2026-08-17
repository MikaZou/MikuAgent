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
PORT = int(os.getenv("PORT", "8000"))

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
FRONTEND_DIR = BASE_DIR / "frontend"
DB_PATH = DATA_DIR / "mikuagent.db"

HAS_API_KEY = bool(DEEPSEEK_API_KEY) and DEEPSEEK_API_KEY != "sk-xxxxxxxx"
