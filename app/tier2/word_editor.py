from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pythoncom
import win32com.client

from app.config import BACKUPS_DIR
from app.tier2.ollama_client import OllamaClient


def prepare_word_edit(instruction: str, ollama: OllamaClient) -> dict:
    """Read active Microsoft Word selection and draft a proposed edit without modifying the file."""
    pythoncom.CoInitialize()
    try:
        try:
            word = win32com.client.GetActiveObject("Word.Application")
        except Exception:
            return {
                "ok": False,
                "error": "Microsoft Word is not currently open with an active document.",
            }

        if word.Documents.Count == 0:
            return {"ok": False, "error": "Microsoft Word is open, but no document is loaded."}

        doc = word.ActiveDocument
        sel = word.Selection
        before_text = (sel.Text or "").replace("\r", "\n").strip()

        if len(before_text) < 2:
            try:
                para_range = sel.Paragraphs(1).Range
                para_text = (para_range.Text or "").replace("\r", "\n").strip()
                if len(para_text) >= 2:
                    before_text = para_text
                    range_start = int(para_range.Start)
                    range_end = int(para_range.End)
                else:
                    return {
                        "ok": False,
                        "error": "Please highlight a sentence or paragraph in Word (or place the cursor inside one) first.",
                    }
            except Exception:
                return {
                    "ok": False,
                    "error": "Please highlight the text you want J.U.D.E. to edit in Microsoft Word.",
                }
        else:
            range_start = int(sel.Range.Start)
            range_end = int(sel.Range.End)

        prompt = (
            f"Editing instruction: {instruction}\n\n"
            f"Original text:\n{before_text}\n\n"
            "Return ONLY the revised text with no commentary, quotes, or markdown headings."
        )
        after_text = ollama.chat(prompt, temperature=0.25, max_tokens=400).strip()

        return {
            "ok": True,
            "doc_name": str(doc.Name),
            "doc_full_path": str(doc.FullName),
            "range_start": range_start,
            "range_end": range_end,
            "before_text": before_text,
            "after_text": after_text,
        }
    finally:
        pythoncom.CoUninitialize()


def _create_backup_snapshot(
    doc_name: str,
    doc_full_path: str,
    before_text: str,
    backups_dir: Path = BACKUPS_DIR,
) -> Path:
    """Write a timestamped text backup (and copy the .docx if saved on disk) before modifying Word."""
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = Path(doc_name).stem or "word_doc"

    txt_backup = backups_dir / f"{stem}_{stamp}.bak.txt"
    txt_backup.write_text(before_text, encoding="utf-8")

    src_path = Path(doc_full_path) if doc_full_path else None
    if src_path and src_path.exists() and src_path.is_file():
        doc_backup = backups_dir / f"{src_path.stem}_{stamp}{src_path.suffix}"
        shutil.copy2(src_path, doc_backup)
        return doc_backup

    return txt_backup


def apply_word_edit(
    payload: dict,
    revised_text: str | None = None,
    backups_dir: Path = BACKUPS_DIR,
) -> str:
    """Create a backup snapshot and apply the confirmed edit into the open Word document."""
    final_text = (revised_text if revised_text is not None else payload.get("after_text", "")).strip()
    if not final_text:
        return "Cancelled edit because the proposed replacement text was empty."

    backup_path = _create_backup_snapshot(
        doc_name=payload.get("doc_name", "document"),
        doc_full_path=payload.get("doc_full_path", ""),
        before_text=payload.get("before_text", ""),
        backups_dir=backups_dir,
    )

    pythoncom.CoInitialize()
    try:
        word = win32com.client.GetActiveObject("Word.Application")
        doc = word.ActiveDocument
        target_range = doc.Range(int(payload["range_start"]), int(payload["range_end"]))
        target_range.Text = final_text
        return (
            f"Updated `{payload.get('doc_name', 'document')}` cleanly. "
            f"Backup saved to `{backup_path}`."
        )
    except Exception as exc:
        return f"Backup saved to `{backup_path}`, but applying to Word failed: {exc}"
    finally:
        pythoncom.CoUninitialize()
