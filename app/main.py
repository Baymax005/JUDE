from __future__ import annotations

from app.config import (
    MEMORY_DB_PATH,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
    ROOT_DIR,
    TIER1_CONFIDENCE_THRESHOLD,
    TIER1_CONFIRM_THRESHOLD,
    assert_no_c_drive_leakage,
    configure_d_drive_env,
)


def main() -> None:
    configure_d_drive_env()
    assert_no_c_drive_leakage()
    print("=" * 56)
    print("  J.U.D.E. Environment Ready (Self-Contained Isolation)")
    print("=" * 56)
    print(f"  Root Dir       : {ROOT_DIR}")
    print(f"  Memory DB      : {MEMORY_DB_PATH}")
    print(f"  Ollama Endpoint: {OLLAMA_BASE_URL} (model={OLLAMA_MODEL})")
    print(
        f"  Tier 1 Gate    : Direct >= {TIER1_CONFIDENCE_THRESHOLD:.2f} | "
        f"Confirm {TIER1_CONFIRM_THRESHOLD:.2f}-{TIER1_CONFIDENCE_THRESHOLD:.2f} | "
        f"Escalate < {TIER1_CONFIRM_THRESHOLD:.2f}"
    )
    print("=" * 56)


if __name__ == "__main__":
    main()