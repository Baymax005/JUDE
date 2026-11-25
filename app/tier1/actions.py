from __future__ import annotations

import ctypes
import os
import re
import shutil
import socket
import subprocess
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path

import psutil
import win32con
import win32gui
import win32process

from app.config import OLLAMA_BASE_URL, ROOT_DIR


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _check_ollama_port() -> bool:
    host_port = OLLAMA_BASE_URL.replace("http://", "").replace("https://", "").split("/")[0]
    host, _, port_str = host_port.partition(":")
    port = int(port_str or "11434")
    try:
        with socket.create_connection((host, port), timeout=0.15):
            return True
    except OSError:
        return False


def get_system_info(text: str) -> str:
    """Return local Windows system telemetry (time, disk, RAM, battery, CPU, Ollama)."""
    q = text.lower()
    now = datetime.now()

    if any(w in q for w in ("what time", "exact time", "clock", "today's date", "today date", "day of the week")):
        return f"It is {now.strftime('%I:%M %p')} on {now.strftime('%A, %B %d, %Y')}."

    mem = _MemoryStatusEx()
    mem.dwLength = ctypes.sizeof(_MemoryStatusEx)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
    total_ram_gb = mem.ullTotalPhys / (1024 ** 3)
    avail_ram_gb = mem.ullAvailPhys / (1024 ** 3)
    used_ram_gb = total_ram_gb - avail_ram_gb

    power = _SystemPowerStatus()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power))
    if power.BatteryLifePercent <= 100:
        ac_str = "Plugged in" if power.ACLineStatus == 1 else "On battery"
        battery_str = f"{power.BatteryLifePercent}% ({ac_str})"
    else:
        battery_str = "AC Power (no battery reported)"

    ws_usage = shutil.disk_usage(str(ROOT_DIR))
    ws_drive = Path(ROOT_DIR).drive or str(ROOT_DIR)
    ws_free_gb = ws_usage.free / (1024 ** 3)
    ws_total_gb = ws_usage.total / (1024 ** 3)

    sys_drive = os.environ.get("SystemDrive", "C:") + "\\"
    sys_usage = shutil.disk_usage(sys_drive)
    sys_free_gb = sys_usage.free / (1024 ** 3)
    sys_total_gb = sys_usage.total / (1024 ** 3)

    if any(w in q for w in ("battery", "charging", "plugged")):
        return f"Battery status: {battery_str}."
    if any(w in q for w in ("space", "storage", "disk", "partition", "full")):
        return (
            f"Storage status:\n"
            f"- Workspace drive ({ws_drive}): {ws_free_gb:.2f} GB free / {ws_total_gb:.2f} GB total\n"
            f"- System drive ({sys_drive[:2]}): {sys_free_gb:.2f} GB free / {sys_total_gb:.2f} GB total"
        )
    if any(w in q for w in ("ram", "memory")):
        return f"RAM usage: {used_ram_gb:.1f} GB used / {total_ram_gb:.1f} GB total ({mem.dwMemoryLoad}% load, {avail_ram_gb:.1f} GB free)."

    ollama_state = "Running (localhost:11434)" if _check_ollama_port() else "Idle / Stopped"
    return (
        f"System Snapshot ({now.strftime('%I:%M %p, %b %d')}):\n"
        f"- CPU Cores: {os.cpu_count() or 'Unknown'} logical threads\n"
        f"- RAM: {used_ram_gb:.1f} / {total_ram_gb:.1f} GB ({mem.dwMemoryLoad}% used)\n"
        f"- Drive {ws_drive}: {ws_free_gb:.2f} GB free / {ws_total_gb:.2f} GB\n"
        f"- Drive {sys_drive[:2]}: {sys_free_gb:.2f} GB free / {sys_total_gb:.2f} GB\n"
        f"- Battery: {battery_str}\n"
        f"- Local Ollama: {ollama_state}"
    )


_APP_TARGETS: list[tuple[tuple[str, ...], str, str]] = [
    (("calculator", "calc"), "calc.exe", "Windows Calculator"),
    (("notepad++", "notepad plus plus"), "notepad++.exe", "Notepad++"),
    (("notepad",), "notepad.exe", "Notepad"),
    (("paint",), "mspaint.exe", "Paint"),
    (("snipping tool",), "snippingtool.exe", "Snipping Tool"),
    (("task manager",), "taskmgr.exe", "Task Manager"),
    (("control panel",), "control.exe", "Control Panel"),
    (("device manager",), "devmgmt.msc", "Device Manager"),
    (("disk management",), "diskmgmt.msc", "Disk Management"),
    (("resource monitor",), "resmon.exe", "Resource Monitor"),
    (("task scheduler",), "taskschd.msc", "Task Scheduler"),
    (("bluetooth",), "ms-settings:bluetooth", "Bluetooth Settings"),
    (("sound settings",), "ms-settings:sound", "Sound Settings"),
    (("startup apps",), "ms-settings:startupapps", "Startup Apps Settings"),
    (("settings",), "ms-settings:", "Windows Settings"),
    (("windows terminal", "terminal"), "wt.exe", "Windows Terminal"),
    (("powershell",), "powershell.exe", "PowerShell"),
    (("command prompt", "cmd"), "cmd.exe", "Command Prompt"),
    (("file explorer", "explorer"), "explorer.exe", "File Explorer"),
    (("vs code", "visual studio code"), "code", "Visual Studio Code"),
    (("microsoft word", "word"), "winword", "Microsoft Word"),
    (("microsoft excel", "excel"), "excel", "Microsoft Excel"),
    (("powerpoint",), "powerpnt", "Microsoft PowerPoint"),
    (("google chrome", "chrome"), "chrome", "Google Chrome"),
    (("microsoft edge", "edge"), "msedge", "Microsoft Edge"),
    (("sticky notes",), "explorer.exe shell:AppsFolder\\Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe!App", "Sticky Notes"),
]

_URI_OR_WEB_TARGETS: list[tuple[tuple[str, ...], str, str, str | None]] = [
    (("whatsapp",), "whatsapp:", "WhatsApp", "https://web.whatsapp.com"),
    (("google",), "https://www.google.com", "Google", None),
    (("youtube",), "https://www.youtube.com", "YouTube", None),
]

_PROTECTED_PROCESSES = {
    "explorer.exe",
    "svchost.exe",
    "csrss.exe",
    "winlogon.exe",
    "dwm.exe",
    "lsass.exe",
    "services.exe",
    "smss.exe",
    "system",
}

_FOLDER_ALIASES: dict[str, Path] = {
    "downloads": Path.home() / "Downloads",
    "desktop": Path.home() / "Desktop",
    "documents": Path.home() / "Documents",
    "pictures": Path.home() / "Pictures",
    "videos": Path.home() / "Videos",
    "music": Path.home() / "Music",
    "jude": ROOT_DIR,
}

_DISALLOWED_EXEC_EXTS = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".com"}


def _extract_path_candidate(text: str) -> Path | None:
    """Extract an explicit Windows path or user-folder-relative path from text."""
    # Explicit drive path (e.g. D:\folder\sub or C:\Users\...)
    drive_match = re.search(r"([A-Za-z]:\\[^\n\"'<>|*?]+)", text)
    if drive_match:
        raw = drive_match.group(1).strip().rstrip(".,;!?")
        # Trim trailing preposition words like "in explorer" or "into a zip"
        raw = re.split(r"\s+(?:in|into|and|for|to)\s+", raw, maxsplit=1, flags=re.I)[0].strip()
        p = Path(raw)
        if p.exists():
            return p

    # Relative path inside ROOT_DIR (e.g. "app/ui", "colab", "models/tier1_router")
    rel_match = re.search(r"\b([A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+)\b", text)
    if rel_match:
        cand_rel = rel_match.group(1).strip()
        for base in (ROOT_DIR, Path.home()):
            cand = base / cand_rel
            if cand.exists():
                return cand

    # Known user folder + optional subfolder (e.g. "Downloads\Assignments")
    low = text.lower()
    for alias, base_dir in _FOLDER_ALIASES.items():
        if alias in low and base_dir.exists():
            sub_match = re.search(rf"{alias}[\\/]([^\s\"'<>|*?]+)", text, flags=re.I)
            if sub_match:
                sub = base_dir / sub_match.group(1).strip(".,;!?")
                if sub.exists():
                    return sub
            return base_dir

    return None


_DRIVE_PHRASE_RE = re.compile(
    r"\b(?:local\s+disk|drive)\s+([a-z])\b|\b([a-z])\s+drive\b",
    flags=re.IGNORECASE,
)


def open_target(text: str, dry_run: bool = False) -> tuple[bool, str]:
    """Open a whitelisted app/settings URI, drive root, or existing folder/document safely."""
    low = text.lower()

    # Check explicit existing path first if a drive path or subfolder is named
    path_cand = _extract_path_candidate(text)
    if path_cand is not None and (":" in text or "\\" in text or "/" in text or any(a in low for a in _FOLDER_ALIASES)):
        if path_cand.is_file() and path_cand.suffix.lower() in _DISALLOWED_EXEC_EXTS:
            return False, f"Blocked launching executable file directly for safety: {path_cand.name}"
        if not dry_run:
            os.startfile(str(path_cand))
        return True, f"Opened `{path_cand}`."

    # Check spoken drive requests ("local disk D", "D drive", "drive C")
    drive_match = _DRIVE_PHRASE_RE.search(text)
    if drive_match:
        letter = (drive_match.group(1) or drive_match.group(2) or "").upper()
        drive_root = Path(f"{letter}:\\")
        if drive_root.exists():
            if not dry_run:
                os.startfile(str(drive_root))
            return True, f"Opened `{drive_root}` in File Explorer."
        return False, f"Drive `{letter}:` is not available on this PC."

    for keywords, uri_target, display_name, fallback_url in _URI_OR_WEB_TARGETS:
        if any(re.search(rf"\b{re.escape(kw)}\b", low) for kw in keywords):
            if not dry_run:
                try:
                    os.startfile(uri_target)
                except OSError:
                    if fallback_url:
                        webbrowser.open(fallback_url)
                    else:
                        webbrowser.open(uri_target)
            return True, f"Opened {display_name}."

    for keywords, target_cmd, display_name in _APP_TARGETS:
        if any(re.search(rf"\b{re.escape(kw)}\b", low) for kw in keywords):
            if not dry_run:
                if target_cmd.startswith("ms-settings:"):
                    os.startfile(target_cmd)
                elif target_cmd.startswith("explorer.exe "):
                    subprocess.Popen(target_cmd, shell=False)
                else:
                    try:
                        os.startfile(target_cmd)
                    except OSError:
                        subprocess.Popen([target_cmd], shell=False)
            return True, f"Launched {display_name}."

    if path_cand is not None:
        if not dry_run:
            os.startfile(str(path_cand))
        return True, f"Opened `{path_cand}`."

    return False, "I couldn't identify a valid local folder, file, or supported Windows app in that request."


def resolve_directory_path(path_hint: str) -> Path | None:
    """Resolve an explicit path, drive phrase, or user folder alias to an existing directory."""
    raw = (path_hint or "").strip()
    if not raw:
        return ROOT_DIR
    cand = _extract_path_candidate(raw)
    if cand is not None:
        return cand if cand.is_dir() else cand.parent
    drive_match = _DRIVE_PHRASE_RE.search(raw)
    if drive_match:
        letter = (drive_match.group(1) or drive_match.group(2) or "").upper()
        root = Path(f"{letter}:\\")
        if root.exists():
            return root
    bare_drive = re.fullmatch(r"([A-Za-z]):[\\/]?", raw)
    if bare_drive:
        root = Path(f"{bare_drive.group(1).upper()}:\\")
        if root.exists():
            return root
    return None


def open_terminal(path: str = "", dry_run: bool = False) -> tuple[bool, str]:
    """Open Windows Terminal (or PowerShell fallback) at the requested directory."""
    target_dir = resolve_directory_path(path) if path.strip() else ROOT_DIR
    if target_dir is None or not target_dir.exists():
        return False, f"Directory not found for terminal: `{path}`."

    if not dry_run:
        wt_exe = shutil.which("wt.exe") or shutil.which("wt")
        if wt_exe:
            subprocess.Popen([wt_exe, "-d", str(target_dir)], shell=False)
        else:
            subprocess.Popen(
                ["powershell.exe", "-NoExit", "-Command", f"Set-Location -LiteralPath '{target_dir}'"],
                shell=False,
            )
    return True, f"Opened terminal at `{target_dir}`."


def list_open_windows() -> list[dict]:
    """Return visible top-level non-JUDE windows with hwnd, title, and process name."""
    own_pid = os.getpid()
    windows: list[dict] = []

    def _cb(hwnd: int, _param: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        if not title or title in {"Program Manager", "Windows Input Experience"}:
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == own_pid or pid <= 0:
                return True
            proc_name = psutil.Process(pid).name()
        except Exception:
            proc_name = "unknown"
        windows.append({"hwnd": int(hwnd), "title": title, "process": proc_name, "pid": int(pid)})
        return True

    win32gui.EnumWindows(_cb, None)
    return windows


def focus_window(query: str) -> tuple[bool, str]:
    """Focus and restore a matching visible window by title or process name."""
    q = " ".join((query or "").strip().lower().split())
    if not q:
        return False, "Tell me which open window you want to focus."
    windows = list_open_windows()
    for win in windows:
        if q in win["title"].lower() or q in win["process"].lower().replace(".exe", ""):
            hwnd = win["hwnd"]
            try:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
                return True, f"Focused `{win['title']}` ({win['process']})."
            except Exception as exc:
                return False, f"Found `{win['title']}`, but Windows blocked focusing it: {exc}"
    return False, f"No open window matched `{query}`."


def plan_close_app(target: str) -> dict:
    """Resolve a target window/app for safe user-confirmed closing (blocking system processes)."""
    q = " ".join((target or "").strip().lower().split())
    if not q:
        return {"ok": False, "error": "Specify which application window to close."}

    windows = list_open_windows()
    matches = [
        w for w in windows
        if q in w["title"].lower() or q in w["process"].lower().replace(".exe", "")
    ]
    if not matches:
        return {"ok": False, "error": f"No open window matching `{target}` was found."}

    chosen = matches[0]
    proc_low = chosen["process"].lower()
    if proc_low in _PROTECTED_PROCESSES or chosen["pid"] == os.getpid():
        return {"ok": False, "error": f"Refusing to close protected system process `{chosen['process']}`."}

    return {
        "ok": True,
        "hwnd": chosen["hwnd"],
        "pid": chosen["pid"],
        "title": chosen["title"],
        "process": chosen["process"],
    }


def execute_close_app(payload: dict) -> str:
    """Send graceful WM_CLOSE to the confirmed target window."""
    hwnd = int(payload.get("hwnd") or 0)
    title = payload.get("title") or "application"
    proc = payload.get("process") or ""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return f"Window `{title}` is already closed."
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    return f"Sent close request to `{title}` ({proc})."


def navigate_files(
    path: str,
    action: str = "list",
    pattern: str = "",
    dry_run: bool = False,
) -> str:
    """List directory contents, search files by pattern, or open a path in Explorer."""
    act = (action or "list").strip().lower()
    target = resolve_directory_path(path)
    if target is None or not target.exists():
        return f"Path not found: `{path}`."

    if act == "open":
        if target.is_file() and target.suffix.lower() in _DISALLOWED_EXEC_EXTS:
            return f"Blocked opening executable directly for safety: {target.name}"
        if not dry_run:
            os.startfile(str(target))
        return f"Opened `{target}` in File Explorer."

    if act == "find":
        pat = pattern.strip() or "*"
        if "*" not in pat and "?" not in pat:
            pat = f"*{pat}*"
        hits: list[str] = []
        try:
            for item in target.rglob(pat):
                hits.append(str(item))
                if len(hits) >= 25:
                    break
        except Exception as exc:
            return f"Search failed in `{target}`: {exc}"
        if not hits:
            return f"No files matching `{pat}` inside `{target}`."
        return f"Matches in `{target}` ({len(hits)} shown):\n" + "\n".join(f"- {h}" for h in hits)

    entries: list[str] = []
    try:
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for item in children[:40]:
            tag = "[DIR]" if item.is_dir() else "[FILE]"
            entries.append(f"{tag} {item.name}")
    except Exception as exc:
        return f"Could not list `{target}`: {exc}"
    if not entries:
        return f"Directory `{target}` is empty."
    return f"Contents of `{target}` ({len(entries)} shown):\n" + "\n".join(entries)


_SKIP_ZIP_DIRS = {"venv", ".git", "__pycache__", "node_modules"}


def plan_compression(text: str) -> dict:
    """Plan a zip compression job without writing any files yet."""
    source = _extract_path_candidate(text)
    if source is None:
        return {
            "ok": False,
            "error": "Please specify an existing folder or file path to compress (e.g., `Compress Downloads\\Notes`).",
        }

    resolved = source.resolve()
    # Refuse bare drive roots like C:\ or D:\
    if len(resolved.parts) <= 1:
        return {"ok": False, "error": f"Refusing to compress an entire drive root ({resolved})."}

    # Check for custom output zip name in text
    out_match = re.search(r"\b([A-Za-z0-9_.-]+\.zip)\b", text, flags=re.I)
    if out_match:
        archive_name = out_match.group(1)
    else:
        archive_name = f"{resolved.stem or resolved.name}.zip"
    archive_path = resolved.parent / archive_name

    file_count = 0
    total_bytes = 0
    if resolved.is_file():
        file_count = 1
        total_bytes = resolved.stat().st_size
    else:
        for root, dirs, files in os.walk(resolved):
            dirs[:] = [d for d in dirs if d not in _SKIP_ZIP_DIRS]
            for fname in files:
                fp = Path(root) / fname
                try:
                    total_bytes += fp.stat().st_size
                    file_count += 1
                except OSError:
                    pass

    return {
        "ok": True,
        "source_path": str(resolved),
        "archive_path": str(archive_path),
        "file_count": file_count,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
    }


def execute_compression(source_path: Path, archive_path: Path) -> str:
    """Create the planned zip archive after user confirmation."""
    src = Path(source_path).resolve()
    dst = Path(archive_path).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if src.is_file():
            zf.write(src, arcname=src.name)
        else:
            for root, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if d not in _SKIP_ZIP_DIRS]
                for fname in files:
                    fp = Path(root) / fname
                    if fp.resolve() == dst:
                        continue
                    zf.write(fp, arcname=str(fp.relative_to(src.parent)))

    size_mb = dst.stat().st_size / (1024 * 1024)
    return f"Compressed `{src.name}` into `{dst}` ({size_mb:.2f} MB)."
