from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import (
    ENV_VARS,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    ROOT_DIR,
    assert_no_c_drive_leakage,
    configure_d_drive_env,
)


def dir_size_mb(path: Path) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            fp = Path(root) / name
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total / (1024 * 1024)


def main() -> int:
    configure_d_drive_env()
    assert_no_c_drive_leakage()

    exe_path = Path(sys.executable).resolve()
    venv_dir = (ROOT_DIR / "venv").resolve()
    if venv_dir not in exe_path.parents:
        raise RuntimeError(f"Expected Python inside {venv_dir}, got {exe_path}")
    print(f"[OK] Python executable: {exe_path}")

    for k, v in ENV_VARS.items():
        print(f"[OK] {k}={os.environ.get(k)} (expected {v})")

    modules = [
        "PySide6",
        "onnxruntime",
        "tokenizers",
        "numpy",
        "requests",
        "ddgs",
        "bs4",
        "trafilatura",
        "win32com.client",
        "uiautomation",
        "PIL",
        "pyttsx3",
        "sounddevice",
    ]
    for mod in modules:
        importlib.import_module(mod)
        print(f"[OK] Imported {mod}")

    import requests

    tags_resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
    tags_resp.raise_for_status()
    models = [m.get("name", "") for m in tags_resp.json().get("models", [])]
    if not any(m == OLLAMA_MODEL or m.startswith(f"{OLLAMA_MODEL}:") for m in models):
        raise RuntimeError(f"Model {OLLAMA_MODEL!r} not found in Ollama tags: {models}")
    print(f"[OK] Ollama models available: {models}")

    chat_resp = requests.post(
        f"{OLLAMA_BASE_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "stream": False,
            "keep_alive": "1m",
            "messages": [
                {
                    "role": "user",
                    "content": "Reply in one short sentence: what is your name and role?",
                }
            ],
        },
        timeout=60,
    )
    chat_resp.raise_for_status()
    reply = chat_resp.json().get("message", {}).get("content", "").strip()
    print(f"[OK] Ollama ({OLLAMA_MODEL}) reply: {reply}")

    jude_mb = dir_size_mb(ROOT_DIR)
    free_gb = shutil.disk_usage(str(ROOT_DIR)).free / (1024 ** 3)
    print(f"[INFO] Project disk usage: {jude_mb:.1f} MB")
    print(f"[INFO] Drive free space  : {free_gb:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
