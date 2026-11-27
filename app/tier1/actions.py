from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import re
import shutil
import socket
import subprocess
import time
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
    (("file explorer", "file manager", "explorer"), "explorer.exe", "File Explorer"),
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
    "jude folder": ROOT_DIR,
    "jude project": ROOT_DIR,
}

_DISALLOWED_EXEC_EXTS = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".com"}

_NON_APP_SKIP_WORDS = {
    "file",
    "files",
    "folder",
    "tab",
    "new tab",
    "screen",
    "window",
    "terminal",
    "cmd",
    "command prompt",
    "downloads",
    "desktop",
    "documents",
    "pictures",
    "videos",
    "music",
}

_TERMINAL_PROCESSES = {"windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe"}
_TERMINAL_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass"}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


def _default_start_menu_roots() -> list[Path]:
    roots = [Path(r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs")]
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    else:
        roots.append(Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return roots


def _find_start_menu_shortcut(
    query: str,
    search_roots: list[Path] | None = None,
) -> tuple[Path, str] | None:
    """Resolve an installed Start Menu .lnk shortcut dynamically (e.g. VMware Workstation Pro, GNS3)."""
    q = " ".join((query or "").strip().split())
    q = re.sub(
        r"^(?:(?:can|could|would)\s+you\s+|please\s+|just\s+|open\s+|launch\s+|start\s+|run\s+)+",
        "",
        q,
        flags=re.IGNORECASE,
    )
    q = re.sub(
        r"(?:\s+(?:app|application|software|program|please|for\s+me))+$",
        "",
        q,
        flags=re.IGNORECASE,
    )
    q_clean = re.sub(r"[^a-z0-9\s]", " ", q.lower()).strip()
    q_clean = " ".join(q_clean.split())
    if not q_clean or len(q_clean) < 2 or q_clean in _NON_APP_SKIP_WORDS:
        return None
    q_tokens = q_clean.split()

    roots = search_roots if search_roots is not None else _default_start_menu_roots()
    candidates: list[tuple[tuple[int, int, str], Path, str]] = []
    bad_markers = ("uninstall", "readme", "help", "manual", "website", "license", "documentation")

    for root in roots:
        try:
            if not root.exists():
                continue
            for lnk in root.rglob("*.lnk"):
                try:
                    stem = lnk.stem.strip()
                    stem_low = re.sub(r"[^a-z0-9\s]", " ", stem.lower())
                    stem_low = " ".join(stem_low.split())
                    if not stem_low or any(b in stem_low for b in bad_markers):
                        continue
                    stem_words = set(stem_low.split())
                    if stem_low == q_clean:
                        score = (0, len(stem_low), stem_low)
                    elif stem_low.startswith(q_clean):
                        score = (1, len(stem_low), stem_low)
                    elif all(tok in stem_words or (len(tok) >= 4 and tok in stem_low) for tok in q_tokens):
                        score = (2, len(stem_low), stem_low)
                    else:
                        continue
                    candidates.append((score, lnk, stem))
                except OSError:
                    continue
        except OSError:
            continue

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    _, best_path, best_name = candidates[0]
    return best_path, best_name


def _strip_wake_word(text: str) -> str:
    """Remove leading/trailing J.U.D.E. wake words and polite filler before target parsing."""
    s = " ".join((text or "").strip().split())
    s = re.sub(r"^(?:(?:hey|hi|hello)\s+)?jude[\s,:-]+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[\s,]+jude[.?!]*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"(?:\s+(?:for\s+me|right\s+now|now|please))+[.?!]*$", "", s, flags=re.IGNORECASE)
    return s.strip()


def _extract_path_candidate(text: str, allow_aliases: bool = True) -> Path | None:
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

    if not allow_aliases:
        return None

    # Known user folder + optional subfolder (e.g. "Downloads\Assignments")
    low = text.lower()
    for alias, base_dir in _FOLDER_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low) and base_dir.exists():
            sub_match = re.search(rf"{re.escape(alias)}[\\/]([^\s\"'<>|*?]+)", text, flags=re.I)
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


_CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)


def find_chrome_exe() -> Path | None:
    """Locate Google Chrome executable cleanly across standard Windows install paths."""
    on_path = shutil.which("chrome") or shutil.which("chrome.exe")
    if on_path:
        p = Path(on_path)
        try:
            if p.exists():
                return p
        except OSError:
            pass
    local_app = Path(os.environ.get("LOCALAPPDATA", ""))
    cands = list(_CHROME_CANDIDATES)
    if str(local_app):
        cands.append(local_app / "Google" / "Chrome" / "Application" / "chrome.exe")
    for cand in cands:
        try:
            if cand.exists():
                return cand
        except OSError:
            continue
    return None


def list_chrome_profiles(local_state_path: Path | None = None) -> list[dict]:
    """Read Chrome profile directory names and display names from Local State safely."""
    state_path = local_state_path or (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google"
        / "Chrome"
        / "User Data"
        / "Local State"
    )
    try:
        if not state_path.exists():
            return []
        data = json.loads(state_path.read_text(encoding="utf-8"))
        info_cache = (data.get("profile") or {}).get("info_cache") or {}
        profiles: list[dict] = []
        for dir_name, meta in info_cache.items():
            if not isinstance(meta, dict):
                continue
            disp = str(meta.get("name") or meta.get("gaia_name") or dir_name).strip()
            profiles.append({"dir": str(dir_name), "name": disp})
        return profiles
    except OSError:
        return []
    except Exception:
        return []


def _extract_chrome_profile_hint(text: str) -> str:
    m = re.search(
        r"\b(?:profile|from|user|as|for)\s+([A-Za-z0-9 ._-]{2,40})$",
        text.strip(),
        flags=re.IGNORECASE,
    )
    if m:
        cand = m.group(1).strip(" .?!\"'")
        cand = re.sub(r"\s+(?:user|profile)$", "", cand, flags=re.IGNORECASE).strip()
        if cand.lower() not in {"me", "chrome", "google", "browser"}:
            return cand
    return ""


def open_chrome(
    url: str = "",
    profile: str = "",
    dry_run: bool = False,
    local_state_path: Path | None = None,
) -> tuple[bool, str]:
    """Launch Google Chrome directly (bypassing default Edge) with optional profile selection."""
    chrome_exe = find_chrome_exe()
    clean_url = (url or "").strip()
    clean_prof = (profile or "").strip()

    if chrome_exe is None:
        if clean_url:
            if not dry_run:
                webbrowser.open(clean_url)
            return True, f"Opened `{clean_url}` in the default browser (Chrome executable not found)."
        return False, "Google Chrome was not found on this PC."

    cmd: list[str] = [str(chrome_exe)]
    matched_label = ""
    if clean_prof:
        low_p = clean_prof.lower()
        if "guest" in low_p:
            cmd.append("--guest")
            matched_label = "Guest"
        else:
            profiles = list_chrome_profiles(local_state_path=local_state_path)
            exact = [
                p for p in profiles
                if p["name"].lower() == low_p or p["dir"].lower() == low_p
            ]
            partial = [
                p for p in profiles
                if low_p in p["name"].lower() or low_p in p["dir"].lower()
            ]
            chosen = (exact or partial or [None])[0]
            if chosen is not None:
                cmd.append(f"--profile-directory={chosen['dir']}")
                matched_label = chosen["name"]

    if clean_url:
        cmd.append(clean_url)

    if not dry_run:
        subprocess.Popen(cmd, shell=False)

    prof_note = f" (profile: {matched_label})" if matched_label else ""
    if clean_url:
        return True, f"Opened Google Chrome{prof_note} to `{clean_url}`."
    return True, f"Launched Google Chrome{prof_note}."


def open_target(text: str, dry_run: bool = False) -> tuple[bool, str]:
    """Open a whitelisted app/settings URI, drive root, or existing folder/document safely."""
    cleaned_text = _strip_wake_word(text)
    low = cleaned_text.lower()

    # 1. Explicit drive/slash path first
    if ":" in cleaned_text or "\\" in cleaned_text or "/" in cleaned_text:
        path_cand = _extract_path_candidate(cleaned_text, allow_aliases=False)
        if path_cand is not None:
            if path_cand.is_file() and path_cand.suffix.lower() in _DISALLOWED_EXEC_EXTS:
                return False, f"Blocked launching executable file directly for safety: {path_cand.name}"
            if not dry_run:
                os.startfile(str(path_cand))
            return True, f"Opened `{path_cand}`."

    # 2. Spoken drive requests ("local disk D", "D drive", "drive C")
    drive_match = _DRIVE_PHRASE_RE.search(cleaned_text)
    if drive_match:
        letter = (drive_match.group(1) or drive_match.group(2) or "").upper()
        drive_root = Path(f"{letter}:\\")
        if drive_root.exists():
            if not dry_run:
                os.startfile(str(drive_root))
            return True, f"Opened `{drive_root}` in File Explorer."
        return False, f"Drive `{letter}:` is not available on this PC."

    # 2b. Chrome (with optional profile/Google URL) BEFORE generic URI/app fallback so Chrome never redirects to Edge
    if re.search(r"\b(?:google\s+chrome|chrome)\b", low):
        prof_hint = _extract_chrome_profile_hint(cleaned_text)
        target_url = "https://www.google.com" if re.search(r"\bgoogle\b", low) and "chrome" in low and not re.search(r"\bgoogle\s+chrome\b", low) else ""
        return open_chrome(url=target_url, profile=prof_hint, dry_run=dry_run)

    # 3. URI / web targets BEFORE folder aliases (WhatsApp, Google, YouTube)
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

    # 4. Desktop applications BEFORE folder aliases
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

    # 4b. Installed Start Menu shortcuts (VMware Workstation Pro, GNS3, etc.)
    shortcut = _find_start_menu_shortcut(cleaned_text)
    if shortcut is not None:
        lnk_path, display_name = shortcut
        if not dry_run:
            os.startfile(str(lnk_path))
        return True, f"Launched {display_name}."

    # 5. Folder aliases (Downloads, Desktop, Documents, etc.)
    alias_cand = _extract_path_candidate(cleaned_text, allow_aliases=True)
    if alias_cand is not None:
        if alias_cand.is_file() and alias_cand.suffix.lower() in _DISALLOWED_EXEC_EXTS:
            return False, f"Blocked launching executable file directly for safety: {alias_cand.name}"
        if not dry_run:
            os.startfile(str(alias_cand))
        return True, f"Opened `{alias_cand}`."

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
    """Open Windows Terminal (or PowerShell fallback) at the requested or default directory."""
    raw_path = (path or "").strip()
    resolved = resolve_directory_path(raw_path) if raw_path else ROOT_DIR
    fell_back = False
    if resolved is None or not resolved.exists():
        resolved = ROOT_DIR
        fell_back = bool(raw_path)

    if not dry_run:
        wt_exe = shutil.which("wt.exe") or shutil.which("wt")
        if wt_exe:
            subprocess.Popen([wt_exe, "-d", str(resolved)], shell=False)
        else:
            subprocess.Popen(
                ["powershell.exe", "-NoExit", "-Command", f"Set-Location -LiteralPath '{resolved}'"],
                shell=False,
            )
    if fell_back:
        return True, f"Path `{raw_path}` was not found; opened terminal at default folder `{resolved}`."
    return True, f"Opened terminal at `{resolved}`."


def control_window(
    action: str,
    query: str = "",
    target_hwnd: int | None = None,
) -> tuple[bool, str]:
    """Minimize, maximize, or restore an open window (matching query or active external hwnd)."""
    act = (action or "").strip().lower()
    sw_map = {
        "minimize": win32con.SW_MINIMIZE,
        "maximize": win32con.SW_MAXIMIZE,
        "restore": win32con.SW_RESTORE,
    }
    if act not in sw_map:
        return False, f"Unsupported window action `{action}`. Use minimize, maximize, or restore."

    chosen_title = ""
    chosen_proc = ""
    hwnd = 0
    q = " ".join(_strip_wake_word(query).lower().split())
    q = re.sub(r"^(?:my|the)\s+", "", q)
    q = re.sub(r"\s+(?:app|application|window)$", "", q).strip()

    if q:
        for win in list_open_windows():
            if q in win["title"].lower() or q in win["process"].lower().replace(".exe", ""):
                hwnd = win["hwnd"]
                chosen_title = win["title"]
                chosen_proc = win["process"]
                break
        if not hwnd:
            return False, f"No open window matching `{query}` was found."
    elif target_hwnd and win32gui.IsWindow(target_hwnd):
        hwnd = int(target_hwnd)
        chosen_title = win32gui.GetWindowText(hwnd).strip() or "Active Window"
    else:
        wins = list_open_windows()
        if not wins:
            return False, "No external open window was found."
        hwnd = wins[0]["hwnd"]
        chosen_title = wins[0]["title"]
        chosen_proc = wins[0]["process"]

    try:
        win32gui.ShowWindow(hwnd, sw_map[act])
        verb = {"minimize": "Minimized", "maximize": "Maximized", "restore": "Restored"}[act]
        suffix = f" ({chosen_proc})" if chosen_proc else ""
        return True, f"{verb} `{chosen_title}`{suffix}."
    except Exception as exc:
        return False, f"Could not {act} `{chosen_title}`: {exc}"


def list_open_windows() -> list[dict]:
    """Return visible top-level non-JUDE windows with hwnd, title, process, and class_name."""
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
            class_name = win32gui.GetClassName(hwnd) or ""
        except Exception:
            proc_name = "unknown"
            class_name = ""
        windows.append(
            {
                "hwnd": int(hwnd),
                "title": title,
                "process": proc_name,
                "class_name": class_name,
                "pid": int(pid),
            }
        )
        return True

    win32gui.EnumWindows(_cb, None)
    return windows


def focus_window(query: str) -> tuple[bool, str]:
    """Focus and restore a matching visible window by title or process name."""
    q = " ".join(_strip_wake_word(query).lower().split())
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
    """Resolve a target window/app for safe user-confirmed closing (allowing Explorer folders only)."""
    q = " ".join(_strip_wake_word(target).lower().split())
    q = re.sub(r"^(?:my|the)\s+", "", q)
    q = re.sub(r"\s+(?:app|application|window)$", "", q).strip()
    if not q:
        return {"ok": False, "error": "Specify which application window to close."}

    windows = list_open_windows()
    is_file_manager = q in {"file manager", "file explorer", "files", "explorer", "folder"}
    if is_file_manager:
        matches = [
            w for w in windows
            if w["process"].lower() == "explorer.exe" and w.get("class_name") == "CabinetWClass"
        ]
    else:
        aliases = {
            "word": ("winword", "word"),
            "excel": ("excel",),
            "powerpoint": ("powerpnt", "powerpoint"),
            "chrome": ("chrome",),
            "whatsapp": ("whatsapp",),
            "edge": ("msedge", "edge"),
            "terminal": ("windowsterminal", "wt", "powershell", "cmd"),
        }
        tokens = aliases.get(q, (q,))
        matches = [
            w for w in windows
            if any(
                tok in w["title"].lower() or tok in w["process"].lower().replace(".exe", "")
                for tok in tokens
            )
        ]

    if not matches:
        return {"ok": False, "error": f"No open window matching `{target}` was found."}

    chosen = matches[0]
    proc_low = chosen["process"].lower()
    class_name = chosen.get("class_name", "")
    is_explorer_folder = proc_low == "explorer.exe" and class_name == "CabinetWClass"
    if (proc_low in _PROTECTED_PROCESSES and not is_explorer_folder) or chosen["pid"] == os.getpid():
        return {"ok": False, "error": f"Refusing to close protected system process `{chosen['process']}`."}

    return {
        "ok": True,
        "hwnd": chosen["hwnd"],
        "pid": chosen["pid"],
        "title": chosen["title"],
        "process": chosen["process"],
        "class_name": class_name,
    }


def plan_close_hwnd(hwnd: int) -> dict:
    """Resolve a specific visible HWND (e.g. 'close this on my screen') for safe confirmation."""
    if not hwnd:
        return {"ok": False, "error": "No active window handle is available to close."}
    windows = list_open_windows()
    chosen = next((w for w in windows if int(w.get("hwnd") or 0) == int(hwnd)), None)
    if chosen is None:
        return {"ok": False, "error": "The window on your screen could not be identified."}
    proc_low = chosen["process"].lower()
    class_name = chosen.get("class_name", "")
    is_explorer_folder = proc_low == "explorer.exe" and class_name == "CabinetWClass"
    if (
        (proc_low in _PROTECTED_PROCESSES and not is_explorer_folder)
        or class_name in {"Shell_TrayWnd", "Progman", "WorkerW"}
        or chosen["pid"] == os.getpid()
    ):
        return {"ok": False, "error": f"Refusing to close protected system window `{chosen['title']}`."}
    return {
        "ok": True,
        "hwnd": chosen["hwnd"],
        "pid": chosen["pid"],
        "title": chosen["title"],
        "process": chosen["process"],
        "class_name": class_name,
    }


def _is_terminal_window(win: dict) -> bool:
    proc = str(win.get("process") or "").lower()
    cls = str(win.get("class_name") or "")
    return proc in _TERMINAL_PROCESSES or cls in _TERMINAL_CLASSES


def find_open_terminal_window(target_hwnd: int | None = None) -> dict | None:
    """Return an open Windows Terminal / Command Prompt window dict, preferring target_hwnd."""
    windows = list_open_windows()
    if target_hwnd:
        for w in windows:
            if int(w.get("hwnd") or 0) == int(target_hwnd) and _is_terminal_window(w):
                return w
    for w in windows:
        if _is_terminal_window(w):
            return w
    return None


def type_into_terminal_window(hwnd: int, command: str) -> tuple[bool, str]:
    """Focus an open terminal window and send Unicode keystrokes + Enter via Win32 SendInput."""
    cmd = " ".join((command or "").strip().splitlines()).strip()
    if not cmd:
        return False, "No command text to type."
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False, "The target terminal window is no longer open."

    title = win32gui.GetWindowText(hwnd).strip() or "Command Prompt"
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        fg_hwnd = win32gui.GetForegroundWindow()
        cur_tid = int(kernel32.GetCurrentThreadId())
        fg_tid = int(win32process.GetWindowThreadProcessId(fg_hwnd)[0]) if fg_hwnd else 0
        tgt_tid = int(win32process.GetWindowThreadProcessId(hwnd)[0])

        attached_fg = False
        attached_tgt = False
        try:
            if fg_tid and fg_tid != cur_tid:
                attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
            if tgt_tid and tgt_tid != cur_tid:
                attached_tgt = bool(user32.AttachThreadInput(cur_tid, tgt_tid, True))
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if attached_tgt:
                user32.AttachThreadInput(cur_tid, tgt_tid, False)
            if attached_fg:
                user32.AttachThreadInput(cur_tid, fg_tid, False)

        time.sleep(0.15)
        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004
        VK_RETURN = 0x0D

        inputs: list[_INPUT] = []
        utf16 = cmd.encode("utf-16-le")
        for i in range(0, len(utf16), 2):
            scan = int.from_bytes(utf16[i : i + 2], "little")
            inputs.append(_INPUT(type=INPUT_KEYBOARD, union=_INPUT_UNION(ki=_KEYBDINPUT(0, scan, KEYEVENTF_UNICODE, 0, 0))))
            inputs.append(
                _INPUT(
                    type=INPUT_KEYBOARD,
                    union=_INPUT_UNION(ki=_KEYBDINPUT(0, scan, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)),
                )
            )
        inputs.append(_INPUT(type=INPUT_KEYBOARD, union=_INPUT_UNION(ki=_KEYBDINPUT(VK_RETURN, 0, 0, 0, 0))))
        inputs.append(_INPUT(type=INPUT_KEYBOARD, union=_INPUT_UNION(ki=_KEYBDINPUT(VK_RETURN, 0, KEYEVENTF_KEYUP, 0, 0))))

        arr = (_INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
        if sent != len(inputs):
            return False, f"SendInput sent {sent}/{len(inputs)} key events to `{title}`."
        return True, f"Typed and executed `{cmd}` in `{title}`."
    except Exception as exc:
        return False, f"Could not type command into `{title}`: {exc}"


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


_DANGEROUS_WIN_CMD_PATTERNS = (
    re.compile(r"\bformat\s+[a-z]:", flags=re.I),
    re.compile(r"\bdiskpart\b", flags=re.I),
    re.compile(r"\bbcdedit\b", flags=re.I),
    re.compile(r"\bcipher\s+/w:", flags=re.I),
    re.compile(r"\b(?:rd|rmdir)\b[^\r\n]*\/s\b[^\r\n]*[a-z]:\\(?:\s|$)", flags=re.I),
    re.compile(r"\bdel\b[^\r\n]*\/[fs]\b[^\r\n]*[a-z]:\\\*", flags=re.I),
    re.compile(r"\breg\s+delete\s+(?:hklm|hkey_local_machine)\b", flags=re.I),
    re.compile(r"\bshutdown\b[^\r\n]*\/(?:s|r)\b", flags=re.I),
)

_READONLY_WIN_CMD_HEADS = {
    "ipconfig",
    "whoami",
    "hostname",
    "ver",
    "systeminfo",
    "dir",
    "tree",
    "echo",
    "where",
    "ping",
    "nslookup",
    "netstat",
    "tasklist",
    "wmic",
    "getmac",
    "route",
    "arp",
    "sc query",
    "python --version",
    "python -v",
    "node -v",
    "node --version",
    "npm -v",
    "pip list",
    "pip show",
    "git status",
    "git log",
    "git branch",
    "git diff",
}


def _is_dangerous_windows_command(cmd: str) -> str | None:
    for pat in _DANGEROUS_WIN_CMD_PATTERNS:
        if pat.search(cmd):
            return f"Refusing destructive system command matching `{pat.pattern}`."
    return None


def _should_open_visible_cmd(cmd: str, explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    # Chained/piped commands or non-readonly heads run in a visible cmd.exe window
    lowered = " ".join(cmd.strip().lower().split())
    if any(op in lowered for op in ("&&", "||", "|", ">", ">>", ";", "\n")):
        return True
    parts = lowered.split()
    head1 = parts[0] if parts else ""
    head2 = " ".join(parts[:2]) if len(parts) >= 2 else head1
    if head2 in _READONLY_WIN_CMD_HEADS or head1 in _READONLY_WIN_CMD_HEADS:
        return False
    return True


def plan_windows_command(
    command: str,
    cwd: str = "",
    open_visible_cmd: bool | None = None,
    target_hwnd: int | None = None,
) -> dict:
    """Validate and stage a Windows CMD command for mandatory user confirmation."""
    cleaned_cmd = " ".join((command or "").strip().splitlines()).strip()
    if not cleaned_cmd:
        return {"ok": False, "error": "Specify a Windows CMD command to run."}

    danger_err = _is_dangerous_windows_command(cleaned_cmd)
    if danger_err:
        return {"ok": False, "error": danger_err}

    explicit_cwd = bool(cwd and cwd.strip())
    work_dir = Path.cwd()
    if explicit_cwd:
        resolved = resolve_directory_path(cwd.strip())
        if resolved is None:
            candidate = Path(cwd.strip()).expanduser()
            if candidate.exists() and candidate.is_dir():
                resolved = candidate.resolve()
        if resolved is None:
            return {"ok": False, "error": f"Could not resolve working directory `{cwd}`."}
        work_dir = resolved

    term_win = find_open_terminal_window(target_hwnd)
    if open_visible_cmd is None and term_win is not None:
        visible = True
    else:
        visible = _should_open_visible_cmd(cleaned_cmd, open_visible_cmd)

    return {
        "ok": True,
        "command": cleaned_cmd,
        "cwd": str(work_dir),
        "explicit_cwd": explicit_cwd,
        "open_visible_cmd": visible,
        "reuse_terminal_hwnd": int(term_win["hwnd"]) if (visible and term_win is not None) else 0,
        "reuse_terminal_title": str(term_win["title"]) if (visible and term_win is not None) else "",
    }


def execute_windows_command(
    payload: dict,
    edited_command: str | None = None,
    dry_run: bool = False,
) -> str:
    """Execute a confirmed Windows CMD command in Smart Mode (captured, live-typed, or new cmd.exe)."""
    cmd = " ".join(((edited_command if edited_command is not None else payload.get("command")) or "").strip().splitlines()).strip()
    if not cmd:
        return "Cancelled: command was empty."

    danger_err = _is_dangerous_windows_command(cmd)
    if danger_err:
        return danger_err

    cwd = str(payload.get("cwd") or Path.cwd())
    open_visible = bool(payload.get("open_visible_cmd", False))

    if open_visible:
        reuse_hwnd = int(payload.get("reuse_terminal_hwnd") or 0)
        if not reuse_hwnd and payload.get("reuse_terminal", True):
            dyn_term = find_open_terminal_window()
            if dyn_term is not None:
                reuse_hwnd = int(dyn_term.get("hwnd") or 0)

        if reuse_hwnd:
            live_cmd = cmd
            if payload.get("explicit_cwd") and cwd and cwd.lower() not in cmd.lower():
                live_cmd = f'cd /d "{cwd}" && {cmd}'
            if dry_run:
                return f"[Dry-run] Would type `{live_cmd}` into open terminal (hwnd={reuse_hwnd})."
            ok, msg = type_into_terminal_window(reuse_hwnd, live_cmd)
            if ok:
                return msg

        if dry_run:
            return f"[Dry-run] Would execute `{cmd}` in `{cwd}` (visible CMD window)."

        subprocess.Popen(
            ["cmd.exe", "/k", cmd],
            cwd=cwd,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        return f"Opened Command Prompt in `{cwd}` and ran: `{cmd}`"

    if dry_run:
        return f"[Dry-run] Would execute `{cmd}` in `{cwd}` (captured output)."

    proc = subprocess.run(
        ["cmd.exe", "/c", cmd],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
    )
    combined = (proc.stdout or "").strip()
    err_txt = (proc.stderr or "").strip()
    if err_txt:
        combined = f"{combined}\n{err_txt}".strip() if combined else err_txt
    if len(combined) > 900:
        combined = combined[:897].rstrip() + "..."
    if proc.returncode != 0:
        return f"CMD exited with code {proc.returncode}: {combined or '(no output)'}"
    return f"CMD `{cmd}` finished:\n{combined or '(no output)'}"
