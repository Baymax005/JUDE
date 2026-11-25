from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import pythoncom
import win32com.client

from app.config import BACKUPS_DIR, MEMORY_DB_PATH
from app.tier1.memory import list_memories, list_preferences
from app.tier2.ollama_client import OllamaClient


def _find_section_range(doc, section_hint: str) -> tuple[int, int, str] | None:
    """Locate a paragraph or short section matching section_hint in an open Word document."""
    hint = " ".join((section_hint or "").strip().lower().split())
    if not hint:
        return None
    count = int(doc.Paragraphs.Count)
    for idx in range(1, min(count, 400) + 1):
        rng = doc.Paragraphs(idx).Range
        txt = (rng.Text or "").replace("\r", "\n").strip()
        if hint in txt.lower() and len(txt) >= 2:
            # Include the following body paragraph if the matched paragraph looks like a short heading
            if len(txt) < 80 and idx < count:
                next_rng = doc.Paragraphs(idx + 1).Range
                next_txt = (next_rng.Text or "").replace("\r", "\n").strip()
                if next_txt:
                    combined = f"{txt}\n{next_txt}"
                    return int(rng.Start), int(next_rng.End), combined
            return int(rng.Start), int(rng.End), txt
    return None


def prepare_word_edit(
    instruction: str,
    ollama: OllamaClient,
    memory_db_path: Path = MEMORY_DB_PATH,
    section_hint: str = "",
) -> dict:
    """Read active Microsoft Word selection or section and draft an edit without modifying the file."""
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
        range_start = 0
        range_end = 0

        if len(before_text) >= 2:
            range_start = int(sel.Range.Start)
            range_end = int(sel.Range.End)
        elif section_hint.strip():
            found = _find_section_range(doc, section_hint)
            if found is not None:
                range_start, range_end, before_text = found

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
                        "error": "Please highlight a sentence or paragraph in Word (or name the section) first.",
                    }
            except Exception:
                return {
                    "ok": False,
                    "error": "Please highlight the text you want J.U.D.E. to edit in Microsoft Word.",
                }

        memory_block = ""
        low = instruction.lower()
        if any(k in low for k in ("name", "roll", "enroll", "email", "id", "memory", "details", "fill")):
            saved = list_memories(db_path=memory_db_path, limit=15)
            if saved:
                memory_block += "\nSaved user facts:\n" + "\n".join(f"- {m['fact_text']}" for m in saved) + "\n"

        prefs = list_preferences(db_path=memory_db_path, limit=5)
        if prefs:
            pref_lines = "; ".join(f"{p['pref_key']}={p['pref_value']}" for p in prefs)
            memory_block += f"\nUser style preferences: {pref_lines}\n"

        prompt = (
            f"Editing instruction: {instruction}\n"
            f"{memory_block}\n"
            f"Original text:\n{before_text}\n\n"
            "Return ONLY the revised text with no commentary, quotes, or markdown headings."
        )
        after_text = ollama.chat(prompt, temperature=0.25, max_tokens=450, use_history=False).strip()

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
    """Write a lightweight text-only undo snapshot (never copying full .docx binaries)."""
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = Path(doc_name).stem or "word_doc"

    undo_path = backups_dir / f"{stem}_{stamp}.undo.txt"
    header = f"Document: {doc_name}\nSource: {doc_full_path}\nTimestamp: {stamp}\n---\n"
    undo_path.write_text(header + before_text, encoding="utf-8")
    return undo_path


def apply_word_edit(
    payload: dict,
    revised_text: str | None = None,
    backups_dir: Path = BACKUPS_DIR,
) -> str:
    """Save a lightweight text undo snapshot and apply the confirmed edit into the open Word document."""
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
            f"Undo snapshot saved to `{backup_path.name}`."
        )
    except Exception as exc:
        return f"Undo snapshot saved to `{backup_path.name}`, but applying to Word failed: {exc}"
    finally:
        pythoncom.CoUninitialize()


def _resolve_unique_docx_path(title: str, save_path: str = "") -> Path:
    """Compute a non-colliding .docx output path inside the target directory or Documents."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", (title or "Jude_Document").strip()).strip("_") or "Jude_Document"
    if save_path.strip():
        cand = Path(save_path.strip())
        if cand.suffix.lower() != ".docx":
            cand = cand / f"{slug}.docx"
    else:
        docs_dir = Path.home() / "Documents"
        docs_dir.mkdir(parents=True, exist_ok=True)
        cand = docs_dir / f"{slug}.docx"

    cand.parent.mkdir(parents=True, exist_ok=True)
    if not cand.exists():
        return cand

    stem = cand.stem
    for idx in range(1, 100):
        nxt = cand.with_name(f"{stem}_{idx}.docx")
        if not nxt.exists():
            return nxt
    return cand.with_name(f"{stem}_{datetime.now().strftime('%H%M%S')}.docx")


def prepare_word_create(title: str, content: str, save_path: str = "") -> dict:
    """Stage a new .docx document creation for explicit user confirmation."""
    clean_title = (title or "Untitled Document").strip()
    clean_content = (content or "").strip()
    if not clean_content:
        return {"ok": False, "error": "Document content cannot be empty."}
    target = _resolve_unique_docx_path(clean_title, save_path=save_path)
    return {
        "ok": True,
        "title": clean_title,
        "content": clean_content,
        "target_path": str(target),
    }


def execute_word_create(
    payload: dict,
    edited_content: str | None = None,
    open_after: bool = True,
) -> str:
    """Write the confirmed .docx file via python-docx without overwriting existing files."""
    from docx import Document

    final_content = (edited_content if edited_content is not None else payload.get("content", "")).strip()
    if not final_content:
        return "Cancelled creating document because content was empty."

    target = Path(payload["target_path"])
    if target.exists():
        target = _resolve_unique_docx_path(payload.get("title", "Document"), str(target.parent))
    target.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    title = (payload.get("title") or "").strip()
    if title:
        doc.add_heading(title, level=1)
    for block in re.split(r"\n{2,}", final_content):
        para = block.strip()
        if para:
            doc.add_paragraph(para)
    doc.save(str(target))

    if open_after:
        try:
            os.startfile(str(target))
        except OSError:
            pass

    return f"Created Word document `{target.name}` at `{target}`."
