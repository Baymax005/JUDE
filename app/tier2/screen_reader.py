from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import psutil
import pythoncom
import uiautomation as auto
import win32com.client
import win32gui
import win32process
from PIL import ImageGrab

from app.config import CACHE_DIR, MEMORY_DB_PATH
from app.tier1.memory import build_user_profile_context
from app.tier2.ollama_client import OllamaClient

_VMWARE_PROCESS_NAMES = {"vmware.exe", "vmware-vmx.exe"}
_PASSWORD_KEYWORDS = ("password", "passcode", "pin", "secret", "cvv", "token")
_INTERACTIVE_CONTROL_TYPES = {
    "ButtonControl",
    "EditControl",
    "MenuItemControl",
    "TabItemControl",
    "ListItemControl",
    "HyperlinkControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "ComboBoxControl",
}

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

_WINRT_OCR_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($WinRtTask, $ResultType) {
    $netTask = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
$imgPath = $env:JUDE_OCR_IMG
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imgPath)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    $lang = [Windows.Globalization.Language]::new('en-US')
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
}
if ($null -eq $engine) {
    Write-Output ''
    exit 0
}
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
if ($result -and $result.Lines) {
    ($result.Lines | ForEach-Object { $_.Text }) -join "`n"
}
"""


def _get_process_name(hwnd: int) -> str:
    """Return lowercase executable filename for a window handle (e.g., 'vmware.exe')."""
    if not hwnd:
        return ""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid <= 0:
            return ""
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def _is_password_control(ctrl) -> bool:
    """Return True if a UIA control represents a password or secret input."""
    try:
        is_pw = getattr(ctrl, "IsPassword", None)
        if isinstance(is_pw, bool) and is_pw:
            return True
    except Exception:
        pass

    try:
        prop_id = getattr(auto.PropertyId, "IsPasswordProperty", None)
        if prop_id is not None and hasattr(ctrl, "GetPropertyValue"):
            if bool(ctrl.GetPropertyValue(prop_id)):
                return True
    except Exception:
        pass

    try:
        meta = f"{getattr(ctrl, 'Name', '') or ''} {getattr(ctrl, 'AutomationId', '') or ''}".lower()
        ctype = getattr(ctrl, "ControlTypeName", "") or ""
        if any(kw in meta for kw in _PASSWORD_KEYWORDS) and (
            "Edit" in ctype or "Text" in ctype or not ctype
        ):
            return True
    except Exception:
        pass
    return False


def ocr_image_windows(image_path: Path) -> str:
    """Run Windows 11 native WinRT OcrEngine on an image file without third-party binaries."""
    img_path = Path(image_path).resolve()
    if not img_path.exists():
        return ""
    env = os.environ.copy()
    env["JUDE_OCR_IMG"] = str(img_path)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINRT_OCR_PS],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creation_flags,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        return ""
    return ""


def capture_vmware_ocr(hwnd: int, max_chars: int = 3000) -> dict:
    """Capture a VMware viewport screenshot and extract text via native Windows OCR."""
    window_title = win32gui.GetWindowText(hwnd).strip() if hwnd else "VMware"
    class_name = win32gui.GetClassName(hwnd).strip() if hwnd else ""
    proc_name = _get_process_name(hwnd) or "vmware.exe"

    tmp_dir = CACHE_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_png = tmp_dir / f"vmware_ocr_{uuid.uuid4().hex[:8]}.png"
    try:
        rect = win32gui.GetWindowRect(hwnd)
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            return {
                "ok": False,
                "source": "vmware_ocr",
                "window_title": window_title,
                "class_name": class_name,
                "process_name": proc_name,
                "focused_text": "",
                "selected_text": "",
                "text": "VMware window is minimized or has zero visible area.",
            }
        shot = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        shot.save(str(tmp_png), format="PNG")
        ocr_text = ocr_image_windows(tmp_png)[:max_chars]
        return {
            "ok": True,
            "source": "vmware_ocr",
            "window_title": window_title,
            "class_name": class_name,
            "process_name": proc_name,
            "focused_text": "VMware Viewport (Windows Native OCR)",
            "selected_text": "",
            "text": ocr_text or "(No readable text detected by Windows OCR in the VMware viewport.)",
        }
    except Exception as exc:
        return {
            "ok": False,
            "source": "vmware_ocr",
            "window_title": window_title,
            "class_name": class_name,
            "process_name": proc_name,
            "focused_text": "",
            "selected_text": "",
            "text": f"VMware OCR capture failed: {exc}",
        }
    finally:
        try:
            tmp_png.unlink(missing_ok=True)
        except OSError:
            pass


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
            "doc_full_path": str(doc.FullName),
            "selected_text": sel_text,
            "doc_excerpt": full_text,
        }
    except Exception:
        return None
    finally:
        pythoncom.CoUninitialize()


def read_word_document(section_hint: str = "", max_chars: int = 3500) -> dict:
    """Read the currently open Microsoft Word document (even if not in foreground)."""
    pythoncom.CoInitialize()
    try:
        try:
            word = win32com.client.GetActiveObject("Word.Application")
        except Exception:
            return {"ok": False, "error": "Microsoft Word is not currently open."}

        if word.Documents.Count == 0:
            return {"ok": False, "error": "Microsoft Word is open, but no document is loaded."}

        doc = word.ActiveDocument
        doc_name = str(doc.Name)
        sel_text = (word.Selection.Text or "").replace("\r", "\n").strip()
        hint = " ".join((section_hint or "").strip().lower().split())

        if hint:
            matched_chunks: list[str] = []
            capturing = False
            char_budget = 0
            count = int(doc.Paragraphs.Count)
            for idx in range(1, min(count, 400) + 1):
                para_txt = (doc.Paragraphs(idx).Range.Text or "").replace("\r", "").strip()
                if not para_txt:
                    continue
                low_p = para_txt.lower()
                if not capturing:
                    if hint in low_p:
                        capturing = True
                        matched_chunks.append(para_txt)
                        char_budget += len(para_txt)
                else:
                    # Stop if we hit a short heading-like line after collecting body text
                    if len(matched_chunks) >= 2 and len(para_txt) < 60 and para_txt.endswith(":") is False and para_txt.istitle():
                        break
                    matched_chunks.append(para_txt)
                    char_budget += len(para_txt)
                    if char_budget >= max_chars or len(matched_chunks) >= 12:
                        break
            if matched_chunks:
                return {
                    "ok": True,
                    "doc_name": doc_name,
                    "section": section_hint,
                    "selected_text": sel_text[:800],
                    "content": "\n\n".join(matched_chunks)[:max_chars],
                }

        full_text = (doc.Content.Text or "").replace("\r", "\n").strip()[:max_chars]
        return {
            "ok": True,
            "doc_name": doc_name,
            "section": section_hint or "document",
            "selected_text": sel_text[:800],
            "content": full_text or "(Document is currently empty.)",
        }
    finally:
        pythoncom.CoUninitialize()


def _extract_text_pattern(ctrl, max_chars: int = 2500) -> tuple[str, str]:
    """Return (selected_text, document_text) from a UIA TextPattern if supported."""
    if _is_password_control(ctrl):
        return "", "[REDACTED PASSWORD]"
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
    """Capture active window context via VMware OCR, Word COM, or sanitized Windows UIA."""
    hwnd = target_hwnd or _find_external_foreground_hwnd()
    if not hwnd or not win32gui.IsWindow(hwnd):
        return {
            "ok": False,
            "source": "none",
            "window_title": "",
            "class_name": "",
            "process_name": "",
            "focused_text": "",
            "selected_text": "",
            "text": "",
        }

    window_title = win32gui.GetWindowText(hwnd).strip()
    class_name = win32gui.GetClassName(hwnd).strip()
    proc_name = _get_process_name(hwnd)

    # Automatic VMware viewport OCR bypass (do NOT attempt Windows UIA on guest pixels)
    if proc_name in _VMWARE_PROCESS_NAMES:
        return capture_vmware_ocr(hwnd, max_chars=max_chars)

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
                "source": "word_com",
                "window_title": window_title,
                "class_name": class_name,
                "process_name": proc_name or "winword.exe",
                "focused_text": word_ctx["doc_name"],
                "selected_text": word_ctx["selected_text"],
                "text": "\n\n".join(parts)[:max_chars],
            }

    with auto.UIAutomationInitializerInThread():
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
            if _is_password_control(focused):
                _add_snippet("[REDACTED PASSWORD]")
            else:
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
                    if _is_password_control(ctrl):
                        label = (ctrl.Name or "Password").strip()
                        _add_snippet(f"{label}: [REDACTED PASSWORD]")
                        continue

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
            "source": "uia",
            "window_title": window_title,
            "class_name": class_name,
            "process_name": proc_name,
            "focused_text": focused_text,
            "selected_text": selected_found,
            "text": "\n".join(snippets),
        }


def inspect_ui_elements(target_hwnd: int | None = None, max_controls: int = 50) -> dict:
    """List up to 50 visible interactive UI controls on the target window, masking passwords."""
    cap = max(1, min(int(max_controls), 50))
    hwnd = target_hwnd or _find_external_foreground_hwnd()
    if not hwnd or not win32gui.IsWindow(hwnd):
        return {"ok": False, "controls": [], "error": "No active external window found."}

    proc_name = _get_process_name(hwnd)
    window_title = win32gui.GetWindowText(hwnd).strip()
    if proc_name in _VMWARE_PROCESS_NAMES:
        ocr = capture_vmware_ocr(hwnd, max_chars=2000)
        return {
            "ok": True,
            "source": "vmware_ocr_bypass",
            "window_title": window_title,
            "process_name": proc_name,
            "controls": [],
            "ocr_text": ocr.get("text", ""),
        }

    controls: list[dict] = []
    with auto.UIAutomationInitializerInThread():
        try:
            root_ctrl = auto.ControlFromHandle(hwnd)
            queue: list[tuple[auto.Control, int]] = [(root_ctrl, 0)]
            visited = 0
            while queue and visited < 350 and len(controls) < cap:
                ctrl, depth = queue.pop(0)
                visited += 1
                try:
                    ctype = getattr(ctrl, "ControlTypeName", "") or ""
                    is_offscreen = bool(getattr(ctrl, "IsOffscreen", False))
                    is_enabled = bool(getattr(ctrl, "IsEnabled", True))
                    if not is_offscreen and is_enabled and ctype in _INTERACTIVE_CONTROL_TYPES:
                        is_pw = _is_password_control(ctrl)
                        val = ""
                        if is_pw:
                            val = "[REDACTED PASSWORD]"
                        elif hasattr(ctrl, "GetValuePattern"):
                            try:
                                vp = ctrl.GetValuePattern()
                                if vp and vp.Value:
                                    val = str(vp.Value)[:120]
                            except Exception:
                                val = ""
                        name = (getattr(ctrl, "Name", "") or "").strip()[:100]
                        if name or val:
                            controls.append(
                                {
                                    "type": ctype,
                                    "name": name,
                                    "value": val,
                                    "is_password": is_pw,
                                }
                            )
                    if depth < 8 and len(controls) < cap:
                        for child in ctrl.GetChildren():
                            queue.append((child, depth + 1))
                except Exception:
                    continue
        except Exception as exc:
            return {"ok": False, "window_title": window_title, "controls": controls[:cap], "error": str(exc)}

    return {
        "ok": True,
        "source": "uia",
        "window_title": window_title,
        "process_name": proc_name,
        "count": len(controls[:cap]),
        "controls": controls[:cap],
    }


def get_live_context_header(
    target_hwnd: int | None = None,
    memory_db_path: Path = MEMORY_DB_PATH,
) -> str:
    """Return a compact <=4-line live context summary for Tier 2 tool-calling prompts."""
    lines: list[str] = []
    hwnd = target_hwnd or _find_external_foreground_hwnd()
    if hwnd and win32gui.IsWindow(hwnd):
        title = win32gui.GetWindowText(hwnd).strip()[:90]
        proc = _get_process_name(hwnd) or "unknown"
        vm_note = " [VMware viewport -> OCR active]" if proc in _VMWARE_PROCESS_NAMES else ""
        lines.append(f"Active Window: {title or 'Untitled'} ({proc}){vm_note}")

    word_ctx = _read_word_com_context(max_chars=200)
    if word_ctx and word_ctx.get("doc_name"):
        lines.append(f"Open Word Doc: {word_ctx['doc_name']}")

    profile = build_user_profile_context(db_path=memory_db_path)
    if profile:
        lines.append(profile)
    return "\n".join(lines[:4])


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
    """Read the target Windows UI via VMware OCR / Word COM / UIA and explain it using Ollama."""
    ctx = capture_window_context(target_hwnd=target_hwnd)
    if not ctx["ok"] and not ctx["text"]:
        return "I couldn't detect an active external window to read right now."

    body = ctx["text"] or "(No readable UI text controls exposed by this window; only title captured.)"
    prompt = (
        f"User question: {question}\n\n"
        f"Active Window Title: {ctx['window_title']} ({ctx.get('process_name') or ctx['class_name']})\n"
        f"Capture Source: {ctx.get('source', 'uia')}\n"
        f"Focused Element: {ctx['focused_text'] or 'N/A'}\n"
        f"Captured Screen / Document Content:\n{body}\n\n"
        "Describe ONLY the captured window title, app, and visible text above in 1-3 concise natural sentences. "
        "NEVER invent or guess text, apps, or commands that are not present in the captured content."
    )
    return ollama.chat(
        prompt,
        system="You are J.U.D.E., a factual screen reader. Report only the captured window and text provided.",
        temperature=0.1,
        max_tokens=400,
        use_history=False,
    )
