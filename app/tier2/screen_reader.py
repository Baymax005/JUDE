from __future__ import annotations

import os

import pythoncom
import uiautomation as auto
import win32com.client
import win32gui
import win32process

from app.tier2.ollama_client import OllamaClient

_IGNORED_TITLES = {"", "Program Manager", "Windows Input Experience"}
_IGNORED_CHROME_LABELS = {
    "Minimize",
    "Maximize",
    "Restore",
    "Restore Down",
    "Close",
    "System",
    "Close Tab",
    "New Tab",
    "Back",
    "Forward",
    "Reload",
    "DropShadowTop",
    "MsoDockTop",
    "MsoDockBottom",
    "Ribbon",
    "Ribbon Tabs",
    "Lower Ribbon",
    "Collapse the Ribbon",
    "Quick Access Toolbar",
    "Customize Quick Access Toolbar",
    "Status Bar",
    "DesktopWindowXamlSource",
    "More Options",
    "Vertical",
    "Vertical Small Decrease",
    "Vertical Large Decrease",
    "Vertical Small Increase",
    "Vertical Large Increase",
    "Horizontal",
    "AutoSave",
    "Save",
    "Can't Undo",
    "Repeat Copy",
    "Microsoft search",
    "Search",
    "Type to search and use the up and down arrow keys to navigate",
    "Home",
    "Insert",
    "Draw",
    "Design",
    "Layout",
    "References",
    "Mailings",
    "Review",
    "View",
    "Help",
    "Share",
    "Zoom Out",
    "Zoom In",
    "Zoom",
    "Display Settings",
    "Focus",
    "Read Mode",
    "Print Layout",
    "Web Layout",
    "File Tab",
    "Position",
}


def _find_external_foreground_hwnd() -> int:
    """Return the topmost visible window handle that does not belong to J.U.D.E."""
    own_pid = os.getpid()
    fg = win32gui.GetForegroundWindow()
    if fg and win32gui.IsWindowVisible(fg):
        _, pid = win32process.GetWindowThreadProcessId(fg)
        title = win32gui.GetWindowText(fg).strip()
        if pid != own_pid and title not in _IGNORED_TITLES:
            return fg

    found: list[int] = []

    def _enum_cb(hwnd: int, _param: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        title = win32gui.GetWindowText(hwnd).strip()
        if pid != own_pid and title not in _IGNORED_TITLES:
            found.append(hwnd)
            return False
        return True

    win32gui.EnumWindows(_enum_cb, None)
    return found[0] if found else 0


def _read_word_com_context(max_chars: int = 2500) -> dict | None:
    """Read real document text and selection from Microsoft Word via COM."""
    pythoncom.CoInitialize()
    try:
        word = win32com.client.GetActiveObject("Word.Application")
        if word.Documents.Count == 0:
            return None
        doc = word.ActiveDocument
        sel_text = (word.Selection.Text or "").replace("\r", "\n").strip()
        full_text = (doc.Content.Text or "").replace("\r", "\n").strip()[:max_chars]
        return {
            "doc_name": str(doc.Name),
            "selected_text": sel_text,
            "doc_excerpt": full_text,
        }
    except Exception:
        return None
    finally:
        pythoncom.CoUninitialize()


def _extract_text_pattern(ctrl, max_chars: int = 2500) -> tuple[str, str]:
    """Return (selected_text, document_text) from a UIA TextPattern if supported."""
    try:
        tp = ctrl.GetTextPattern()
        if not tp:
            return "", ""
        selected = ""
        try:
            sel_ranges = tp.GetSelection()
            if sel_ranges:
                selected = " ".join(r.GetText(max_chars).strip() for r in sel_ranges if r).strip()
        except Exception:
            selected = ""
        doc_text = ""
        try:
            doc_text = (tp.DocumentRange.GetText(max_chars) or "").strip()
        except Exception:
            doc_text = ""
        return selected, doc_text
    except Exception:
        return "", ""


def capture_window_context(target_hwnd: int | None = None, max_chars: int = 2500) -> dict:
    """Capture active window title, Word COM content if Word is active, or UIA text safely."""
    with auto.UIAutomationInitializerInThread():
        hwnd = target_hwnd or _find_external_foreground_hwnd()
        if not hwnd or not win32gui.IsWindow(hwnd):
            return {
                "ok": False,
                "window_title": "",
                "class_name": "",
                "focused_text": "",
                "selected_text": "",
                "text": "",
            }

        window_title = win32gui.GetWindowText(hwnd).strip()
        class_name = win32gui.GetClassName(hwnd).strip()

        if class_name == "OpusApp" or " - word" in window_title.lower():
            word_ctx = _read_word_com_context(max_chars=max_chars)
            if word_ctx:
                parts = []
                if word_ctx["selected_text"] and len(word_ctx["selected_text"]) >= 2:
                    parts.append(f"Highlighted Selection:\n{word_ctx['selected_text']}")
                if word_ctx["doc_excerpt"]:
                    parts.append(f"Document Content ({word_ctx['doc_name']}):\n{word_ctx['doc_excerpt']}")
                return {
                    "ok": True,
                    "window_title": window_title,
                    "class_name": class_name,
                    "focused_text": word_ctx["doc_name"],
                    "selected_text": word_ctx["selected_text"],
                    "text": "\n\n".join(parts)[:max_chars],
                }

        snippets: list[str] = []
        seen: set[str] = set()
        total_len = 0
        selected_found = ""

        def _add_snippet(val: str) -> None:
            nonlocal total_len
            cleaned = " ".join(val.strip().split())
            if (
                not cleaned
                or len(cleaned) < 2
                or cleaned in _IGNORED_CHROME_LABELS
                or cleaned in seen
                or total_len >= max_chars
            ):
                return
            seen.add(cleaned)
            remaining = max_chars - total_len
            clipped = cleaned[:remaining]
            snippets.append(clipped)
            total_len += len(clipped)

        try:
            focused = auto.GetFocusedControl()
            focused_text = focused.Name or ""
            sel, doc_t = _extract_text_pattern(focused, max_chars)
            if sel:
                selected_found = sel
                _add_snippet(f"Selected: {sel}")
            if doc_t:
                _add_snippet(doc_t)
        except Exception:
            focused_text = ""

        try:
            root_ctrl = auto.ControlFromHandle(hwnd)
            queue: list[tuple[auto.Control, int]] = [(root_ctrl, 0)]
            visited = 0
            while queue and visited < 350 and total_len < max_chars:
                ctrl, depth = queue.pop(0)
                visited += 1
                try:
                    sel, doc_t = _extract_text_pattern(ctrl, max_chars - total_len)
                    if sel and not selected_found:
                        selected_found = sel
                        _add_snippet(f"Selected: {sel}")
                    if doc_t:
                        _add_snippet(doc_t)

                    name = ctrl.Name or ""
                    if name and name != window_title:
                        _add_snippet(name)
                    if hasattr(ctrl, "GetValuePattern"):
                        try:
                            vp = ctrl.GetValuePattern()
                            if vp and vp.Value:
                                _add_snippet(vp.Value)
                        except Exception:
                            pass
                    if depth < 10:
                        for child in ctrl.GetChildren():
                            queue.append((child, depth + 1))
                except Exception:
                    continue
        except Exception:
            pass

        return {
            "ok": True,
            "window_title": window_title,
            "class_name": class_name,
            "focused_text": focused_text,
            "selected_text": selected_found,
            "text": "\n".join(snippets),
        }


def read_highlighted_text(target_hwnd: int | None = None) -> tuple[bool, str]:
    """Return the user's currently highlighted/selected text from Word or the active window."""
    ctx = capture_window_context(target_hwnd=target_hwnd)
    sel = (ctx.get("selected_text") or "").strip()
    if len(sel) >= 2:
        return True, f"Highlighted text ({ctx.get('window_title') or 'Active Window'}):\n{sel}"
    if ctx.get("text"):
        excerpt = ctx["text"][:600]
        return False, f"No specific text selection was highlighted, but here is the active window content:\n{excerpt}"
    return False, "I couldn't detect any highlighted text in the active window."


def explain_screen(
    question: str,
    ollama: OllamaClient,
    target_hwnd: int | None = None,
) -> str:
    """Read the target Windows UI via Word COM / UIA and explain it using local Ollama."""
    ctx = capture_window_context(target_hwnd=target_hwnd)
    if not ctx["ok"] and not ctx["text"]:
        return "I couldn't detect an active external window to read right now."

    body = ctx["text"] or "(No readable UI text controls exposed by this window; only title captured.)"
    prompt = (
        f"User question: {question}\n\n"
        f"Active Window Title: {ctx['window_title']} ({ctx['class_name']})\n"
        f"Focused Element: {ctx['focused_text'] or 'N/A'}\n"
        f"Captured Screen / Document Content:\n{body}\n\n"
        "Answer the user's question directly based on this captured content."
    )
    return ollama.chat(prompt, temperature=0.2, max_tokens=500, use_history=False)
