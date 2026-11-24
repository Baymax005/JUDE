from __future__ import annotations

import os
import shutil
import sys
import threading
import uuid
import zipfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["JUDE_HEADLESS_TEST"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import CACHE_DIR, ROOT_DIR, assert_no_c_drive_leakage, configure_d_drive_env


def _make_temp_dir(name: str) -> Path:
    d = CACHE_DIR / "tmp" / f"test_{name}_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_tier1_memory_and_sysinfo() -> None:
    from app.engine import JudeEngine

    tmp = _make_temp_dir("mem")
    try:
        engine = JudeEngine(
            memory_db_path=tmp / "memory.db",
            backups_dir=tmp / "backups",
            feedback_path=tmp / "feedback.jsonl",
        )
        r1 = engine.process("Remember our capstone meeting room is Hall C-204")
        assert r1.tier_used == "tier1", r1
        assert "Hall C-204" in r1.reply

        r2 = engine.process("Store a note that my AI lab submission is due every Friday at 5 PM")
        assert r2.tier_used == "tier1", r2

        rec = engine.process("What room is our capstone meeting in?")
        assert rec.tier_used == "tier1", rec
        assert "Hall C-204" in rec.reply, rec.reply

        sys_res = engine.process("How much free space is left on my main drive?")
        assert sys_res.tier_used == "tier1", sys_res
        assert "GB free" in sys_res.reply, sys_res.reply
        print("[OK] Tier 1 memory + system_info passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compression_confirmation_gate() -> None:
    from app.engine import JudeEngine

    tmp = _make_temp_dir("zip")
    try:
        src = tmp / "sample_notes"
        src.mkdir(parents=True, exist_ok=True)
        (src / "lecture1.txt").write_text("Two-tier AI architecture notes.", encoding="utf-8")
        (src / "lecture2.txt").write_text("INT8 ONNX quantization notes.", encoding="utf-8")

        feedback_file = tmp / "feedback.jsonl"
        engine = JudeEngine(
            memory_db_path=tmp / "memory.db",
            backups_dir=tmp / "backups",
            feedback_path=feedback_file,
        )

        # Cancel flow
        plan_cancel = engine.process(f"Compress {src} into a zip file")
        assert plan_cancel.tier_used == "pending" and plan_cancel.pending is not None, plan_cancel
        archive_path = Path(plan_cancel.pending.target_path)
        assert not archive_path.exists(), "Zip must NOT be created before confirmation!"
        cancel_res = engine.cancel_pending(plan_cancel.pending.id, escalate_to_tier2=False)
        assert not archive_path.exists()
        assert feedback_file.exists() and "cancelled" in feedback_file.read_text(encoding="utf-8")
        assert "Cancelled" in cancel_res.reply

        # Confirm flow
        plan_ok = engine.process(f"Compress {src} into a zip file")
        assert plan_ok.pending is not None
        assert not archive_path.exists()
        done_res = engine.confirm_pending(plan_ok.pending.id)
        assert done_res.tier_used == "tier1"
        assert archive_path.exists() and archive_path.stat().st_size > 0
        with zipfile.ZipFile(archive_path, "r") as zf:
            names = zf.namelist()
            assert any("lecture1.txt" in n for n in names) and any("lecture2.txt" in n for n in names), names
        print("[OK] Compression confirmation + cancellation safety gate passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_word_editor_backup_and_safety() -> None:
    from app.tier2.ollama_client import OllamaClient
    from app.tier2.word_editor import _create_backup_snapshot, prepare_word_edit

    tmp = _make_temp_dir("word")
    try:
        sample_doc = tmp / "report_draft.docx"
        sample_doc.write_bytes(b"PK-placeholder-docx")
        backup_dir = tmp / "backups"

        backup_path = _create_backup_snapshot(
            doc_name="report_draft.docx",
            doc_full_path=str(sample_doc),
            before_text="Old rough draft text.",
            backups_dir=backup_dir,
        )
        assert backup_path.exists()
        txt_backups = list(backup_dir.glob("*.bak.txt"))
        assert len(txt_backups) == 1 and "Old rough draft text." in txt_backups[0].read_text(encoding="utf-8")

        # Confirm prepare_word_edit handles closed/missing Word safely without crashing
        prep = prepare_word_edit("Rewrite this formally", OllamaClient())
        assert isinstance(prep, dict) and "ok" in prep
        print("[OK] Word editor backup & safety check passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_screen_reader_off_main_thread() -> None:
    from app.tier2.screen_reader import capture_window_context

    results: list[dict] = []
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            results.append(capture_window_context())
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=10.0)
    assert not errors, f"UIA failed off main thread: {errors}"
    assert len(results) == 1 and isinstance(results[0], dict) and "window_title" in results[0]
    print("[OK] Thread-safe Windows UIA screen reader passed")


def test_pure_qpainter_baymax_and_zero_images() -> None:
    from PySide6.QtWidgets import QApplication

    from app.engine import PendingConfirmation
    from app.ui.baymax_avatar import AvatarState, BaymaxAvatarWidget, build_baymax_tray_icon
    from app.ui.confirm_dialog import ConfirmationDialog
    from app.ui.floating_widget import JudeFloatingWindow

    image_exts = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp"}
    found_imgs = [p for p in (ROOT_DIR / "app").rglob("*") if p.suffix.lower() in image_exts]
    assert not found_imgs, f"Expected 0 external image assets under app/, found: {found_imgs}"

    app = QApplication.instance() or QApplication([])

    avatar = BaymaxAvatarWidget(size=108)
    for st in AvatarState:
        avatar.set_state(st)
        avatar.set_blink_fraction(0.0)
        pm = avatar.grab()
        img = pm.toImage()
        assert img.width() == 108 and img.height() == 108
        # Verify visible non-transparent pixels exist in center capsule region
        center_px = img.pixelColor(54, 52)
        assert center_px.alpha() > 0, f"Expected opaque white capsule pixel at center for {st}"

    # Verify blink state renders cleanly
    avatar.set_blink_fraction(1.0)
    blink_img = avatar.grab().toImage()
    assert blink_img.width() == 108

    tray_icon = build_baymax_tray_icon(64)
    assert not tray_icon.isNull()

    pending = PendingConfirmation(
        id="demo1",
        kind="word_edit",
        title="Confirm Word Edit",
        summary="Review edit before applying.",
        intent="EDIT_DOCUMENT",
        confidence=0.92,
        before_text="Old text",
        after_text="Improved text",
    )
    dlg = ConfirmationDialog(pending)
    assert dlg.edited_after_text() == "Improved text"
    dlg.close()

    win = JudeFloatingWindow()
    win.show()
    app.processEvents()
    assert not win.hud_card.isVisible()
    win.toggle_hud()
    app.processEvents()
    assert win.hud_card.isVisible()
    win.toggle_mute()
    win._quit_app()
    win.close()
    app.processEvents()
    print("[OK] Pure QPainter Baymax UI, blinking states, and 0 MB image check passed")


def main() -> int:
    configure_d_drive_env()
    assert_no_c_drive_leakage()
    test_tier1_memory_and_sysinfo()
    test_compression_confirmation_gate()
    test_word_editor_backup_and_safety()
    test_screen_reader_off_main_thread()
    test_pure_qpainter_baymax_and_zero_images()
    print("\nALL PHASE 2 TESTS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
