from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(os.environ.get("JUDE_ROOT", Path(__file__).resolve().parents[1])).resolve()
APP_DIR = ROOT_DIR / "app"
MODELS_DIR = ROOT_DIR / "models"
TIER1_MODEL_DIR = MODELS_DIR / "tier1_router"
AUDIO_MODELS_DIR = MODELS_DIR / "audio"
TTS_MODELS_DIR = AUDIO_MODELS_DIR / "tts"
OLLAMA_MODELS_DIR = MODELS_DIR / "ollama"
DATA_DIR = ROOT_DIR / "data"
BACKUPS_DIR = ROOT_DIR / "backups"
CACHE_DIR = ROOT_DIR / "cache"
MEMORY_DB_PATH = DATA_DIR / "memory.db"
VMWARE_SSH_CONFIG_PATH = DATA_DIR / "vmware_ssh.json"

OLLAMA_BASE_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "jude"
OLLAMA_NUM_CTX = 4096
OLLAMA_KEEP_ALIVE = "3m"
TIER1_CONFIDENCE_THRESHOLD = 0.88
TIER1_CONFIRM_THRESHOLD = 0.55

ENV_VARS = {
    "PIP_CACHE_DIR": str(CACHE_DIR / "pip"),
    "HF_HOME": str(CACHE_DIR / "huggingface"),
    "TORCH_HOME": str(CACHE_DIR / "torch"),
    "XDG_CACHE_HOME": str(CACHE_DIR / "xdg"),
    "TEMP": str(CACHE_DIR / "tmp"),
    "TMP": str(CACHE_DIR / "tmp"),
    "OLLAMA_MODELS": str(OLLAMA_MODELS_DIR),
}


def configure_d_drive_env() -> None:
    """Force all caches, temp directories, and model paths inside ROOT_DIR."""
    for key, value in ENV_VARS.items():
        os.environ[key] = value
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def assert_no_c_drive_leakage() -> None:
    """Fail fast if any tracked environment variable points outside ROOT_DIR."""
    expected_prefix = str(ROOT_DIR).lower()
    for key in ENV_VARS:
        val = os.environ.get(key, "")
        if not val or not val.lower().startswith(expected_prefix):
            raise RuntimeError(
                f"Environment leakage detected: {key}={val!r} does not start with {str(ROOT_DIR)!r}"
            )
