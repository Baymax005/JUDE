from __future__ import annotations

import argparse
import ctypes
import sys
import traceback

from app.config import (
    CACHE_DIR,
    MEMORY_DB_PATH,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    ROOT_DIR,
    TIER1_CONFIDENCE_THRESHOLD,
    TIER1_CONFIRM_THRESHOLD,
    assert_no_c_drive_leakage,
    configure_d_drive_env,
)


def _redirect_windowless_streams() -> None:
    """Redirect stdout/stderr to a local log file when launched under pythonw.exe."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_path = CACHE_DIR / "tmp" / "jude_gui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log_file
    if sys.stderr is None:
        sys.stderr = log_file


def _print_banner(router_status: str) -> None:
    print("=" * 60)
    print("  J.U.D.E. Environment Ready (Self-Contained Isolation)")
    print("=" * 60)
    print(f"  Root Dir       : {ROOT_DIR}")
    print(f"  Memory DB      : {MEMORY_DB_PATH}")
    print(f"  Ollama Endpoint: {OLLAMA_BASE_URL} (model={OLLAMA_MODEL})")
    print(
        f"  Tier 1 Gate    : Direct >= {TIER1_CONFIDENCE_THRESHOLD:.2f} | "
        f"Confirm {TIER1_CONFIRM_THRESHOLD:.2f}-{TIER1_CONFIDENCE_THRESHOLD:.2f} | "
        f"Escalate < {TIER1_CONFIRM_THRESHOLD:.2f}"
    )
    print(f"  Tier 1 Router  : {router_status}")
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    configure_d_drive_env()
    _redirect_windowless_streams()
    assert_no_c_drive_leakage()

    parser = argparse.ArgumentParser(description="J.U.D.E. Desktop AI Assistant")
    parser.add_argument("--check", action="store_true", help="Verify environment and router, then exit.")
    parser.add_argument("--once", type=str, help="Run one command in CLI mode and exit.")
    args = parser.parse_args(argv)

    if args.check:
        from app.engine import JudeEngine

        engine = JudeEngine()
        _print_banner(engine.router_status)
        return 0

    if args.once:
        from app.engine import JudeEngine

        engine = JudeEngine()
        resp = engine.process(args.once)
        print(f"[{resp.badge_text}] ({resp.intent}, conf={resp.confidence:.3f})")
        print(resp.reply)
        return 0

    from PySide6.QtWidgets import QApplication

    from app.ui.floating_widget import JudeFloatingWindow

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = JudeFloatingWindow()
    window.show()
    window.raise_()
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        err_text = traceback.format_exc()
        try:
            log_path = CACHE_DIR / "tmp" / "jude_gui.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(err_text + "\n")
        except Exception:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                f"J.U.D.E. failed to start:\n\n{err_text[-800:]}",
                "J.U.D.E. Startup Error",
                0x10,
            )
        except Exception:
            pass
        raise
