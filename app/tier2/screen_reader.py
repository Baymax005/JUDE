from __future__ import annotations

import os

import uiautomation as auto
import win32gui
import win32process

from app.tier2.ollama_client import OllamaClient

_IGNORED_TITLES = {"", "Program Manager", "Windows Input Experience"}
_IGNORED_CHROME_LABELS = {
    "Minimize",
    "Maximize",
    "Restore",
    "Close",
    "System",
    "Close Tab",
    "New Tab",
    "Back",
    "Forward",
    "Reload",
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


def capture_window_context(target_hwnd: int | None = None, max_chars: int = 2500) -> dict:
    """Capture active window title, focused element, and visible UIA text safely on any thread."""
    with auto.UIAutomationInitializerInThread():
        hwnd = target_hwnd or _find_external_foreground_hwnd()
        if not hwnd or not win32gui.IsWindow(hwnd):
            return {
                "ok": False,
                "window_title": "",
                "class_name": "",
                "focused_text": "",
                "text": "",
            }

        window_title = win32gui.GetWindowText(hwnd).strip()
        class_name = win32gui.GetClassName(hwnd).strip()
        snippets: list[str] = []
        seen: set[str] = set()
        total_len = 0

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
            "text": "\n".join(snippets),
        }


def explain_screen(
    question: str,
    ollama: OllamaClient,
    target_hwnd: int | None = None,
) -> str:
    """Read the target Windows UI via UIA and explain it using local Ollama."""
    ctx = capture_window_context(target_hwnd=target_hwnd)
    if not ctx["ok"] and not ctx["text"]:
        return "I couldn't detect an active external window to read right now."

    body = ctx["text"] or "(No readable UI text controls exposed by this window; only title captured.)"
    prompt = (
        f"User question: {question}\n\n"
        f"Active Window Title: {ctx['window_title']} ({ctx['class_name']})\n"
        f"Focused Element: {ctx['focused_text'] or 'N/A'}\n"
        f"Visible UI Text:\n{body}\n\n"
        "Explain clearly and concisely what is shown and how to fix any error if one is present."
    )
    return ollama.chat(prompt, temperature=0.2, max_tokens=450)
