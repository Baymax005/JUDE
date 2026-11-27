from __future__ import annotations

import json
import random
import re
from pathlib import Path

_COLAB_DIR = Path(__file__).resolve().parent
_SCHEMAS_PATH = _COLAB_DIR / "jude_tool_schemas.json"

_CORE_TOOLS = (
    "open_app_or_site",
    "close_app",
    "list_windows",
    "focus_window",
    "window_control",
    "open_terminal",
    "run_windows_command",
    "navigate_files",
    "google_search",
    "read_active_screen",
    "memory_save",
    "memory_recall",
    "memory_delete",
)
_WORD_TOOLS = ("read_open_word_doc", "create_word_doc", "edit_word_doc")
_VMWARE_TOOLS = ("vmware_list_vms", "vmware_run_linux_cmd")

_WORD_CTX_RE = re.compile(
    r"\b(?:word|doc|docx|document|summary|executive|paragraph|section|highlighted|report|edit|rewrite|polish|add\s+my\s+name)\b",
    flags=re.IGNORECASE,
)
_VMWARE_CTX_RE = re.compile(
    r"\b(?:vmware|vm\b|linux|ubuntu|ssh|guest|distro|apt|systemctl|bash)\b",
    flags=re.IGNORECASE,
)
_UI_CTX_RE = re.compile(
    r"\b(?:ui|button|buttons|control|controls|element|elements|inspect|screen|menu)\b",
    flags=re.IGNORECASE,
)


def _load_schemas() -> list[dict]:
    if not _SCHEMAS_PATH.exists():
        raise FileNotFoundError(f"Missing locked tool schema file: {_SCHEMAS_PATH}")
    return json.loads(_SCHEMAS_PATH.read_text(encoding="utf-8"))


AGENT_TOOL_SCHEMAS: list[dict] = _load_schemas()
_TOOL_BY_NAME: dict[str, dict] = {s["function"]["name"]: s for s in AGENT_TOOL_SCHEMAS}


def select_tools_for_turn(user_text: str, live_ctx: str = "") -> list[dict]:
    """Prune irrelevant Word/VMware/UI tool schemas identically to app/tools/registry.py."""
    combined = f"{user_text}\n{live_ctx}"
    names: list[str] = list(_CORE_TOOLS)
    if "Open Word Doc:" in live_ctx or _WORD_CTX_RE.search(combined):
        names.extend(_WORD_TOOLS)
    if "vmware" in live_ctx.lower() or _VMWARE_CTX_RE.search(combined):
        names.extend(_VMWARE_TOOLS)
    if _UI_CTX_RE.search(user_text):
        names.append("inspect_ui_elements")
    return [_TOOL_BY_NAME[n] for n in names if n in _TOOL_BY_NAME]

SYSTEM_PROMPT = (
    "You are J.U.D.E., a local-first Windows 11 desktop AI agent with VMware Linux guest integration. "
    "Always identify yourself strictly as J.U.D.E. (never as Qwen or Alibaba Cloud). "
    "STRICT GROUNDING RULE: NEVER claim you opened, closed, typed, edited, sent a message, or saved anything "
    "unless you actually called a tool in this turn and received ok=true. If no tool exists for an action, "
    "honestly state that you cannot perform that action yet. "
    "Be concise (1-2 natural spoken sentences) without raw markdown symbols or repeating the user's name."
)

_PRIVACY_GUARD_SUBSTRINGS = ("@gmail.com", "@yahoo.com", "c:\\users\\", "d:\\jude", "password=")


def _assistant_tool_msg(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }


def _make_single_turn(
    category: str,
    user_text: str,
    tool_name: str | None,
    arguments: dict | None = None,
    direct_reply: str = "",
    live_ctx: str = "",
) -> dict:
    sys_txt = SYSTEM_PROMPT + (f"\n\n[Live Context]\n{live_ctx}" if live_ctx else "")
    tools = select_tools_for_turn(user_text, live_ctx)
    if tool_name is None:
        asst = {"role": "assistant", "content": direct_reply, "tool_calls": []}
    else:
        asst = _assistant_tool_msg(tool_name, arguments or {})
    return {
        "category": category,
        "expected_tool": tool_name,
        "expected_args": arguments or {},
        "tool_names": [t["function"]["name"] for t in tools],
        "messages": [
            {"role": "system", "content": sys_txt},
            {"role": "user", "content": user_text},
            asst,
        ],
    }


def _make_multi_turn(
    category: str,
    user_text: str,
    tool_name: str,
    arguments: dict,
    tool_result: dict,
    final_reply: str,
    live_ctx: str = "",
) -> dict:
    sys_txt = SYSTEM_PROMPT + (f"\n\n[Live Context]\n{live_ctx}" if live_ctx else "")
    tools = select_tools_for_turn(user_text, live_ctx)
    return {
        "category": category,
        "expected_tool": tool_name,
        "expected_args": arguments,
        "tool_names": [t["function"]["name"] for t in tools],
        "messages": [
            {"role": "system", "content": sys_txt},
            {"role": "user", "content": user_text},
            _assistant_tool_msg(tool_name, arguments),
            {"role": "tool", "content": json.dumps(tool_result, ensure_ascii=False)},
            {"role": "assistant", "content": final_reply},
        ],
    }


def _build_all_examples() -> list[dict]:
    rows: list[dict] = []

    # 1. Windows Apps, Chrome Profiles, Terminal, File Manager, Window Control
    win_specs = [
        ("Open Chrome for me", "open_app_or_site", {"target": "chrome"}, {"ok": True, "message": "Launched Google Chrome."}, "Opened Google Chrome."),
        ("Jude open Chrome using Guest profile", "open_app_or_site", {"target": "chrome", "profile": "Guest"}, {"ok": True, "message": "Launched Google Chrome (profile: Guest)."}, "Opened Google Chrome in Guest mode."),
        ("Open Chrome under Alex profile", "open_app_or_site", {"target": "chrome", "profile": "Alex"}, {"ok": True, "message": "Launched Google Chrome (profile: Alex)."}, "Launched Chrome with your Alex profile."),
        ("Open File Manager", "open_app_or_site", {"target": "file manager"}, {"ok": True, "message": "Launched File Explorer."}, "Opened File Explorer."),
        ("Launch WhatsApp", "open_app_or_site", {"target": "whatsapp"}, {"ok": True, "message": "Opened WhatsApp."}, "Opened WhatsApp."),
        ("Open Microsoft Word", "open_app_or_site", {"target": "word"}, {"ok": True, "message": "Launched Microsoft Word."}, "Launched Microsoft Word."),
        ("Open my D drive", "open_app_or_site", {"target": "D drive"}, {"ok": True, "message": "Opened D:\\ in File Explorer."}, "Opened your D drive."),
        ("Open the terminal", "open_terminal", {"path": ""}, {"ok": True, "message": "Opened terminal."}, "Opened Windows Terminal."),
        ("Open a terminal at C:\\Projects\\Lab", "open_terminal", {"path": "C:\\Projects\\Lab"}, {"ok": True, "message": "Opened terminal at C:\\Projects\\Lab."}, "Opened terminal at C:\\Projects\\Lab."),
        ("Take me to Desktop in terminal", "open_terminal", {"path": "Desktop"}, {"ok": True, "message": "Opened terminal at Desktop."}, "Opened a terminal in your Desktop folder."),
        ("Search GitHub on Chrome", "google_search", {"query": "GitHub", "open_in_browser": True, "browser": "chrome"}, {"ok": True, "message": "Opened Google Chrome search for GitHub."}, "Opened a Google search for GitHub in Chrome."),
        ("Search Python asyncio documentation in Chrome under Sam profile", "google_search", {"query": "Python asyncio documentation", "open_in_browser": True, "browser": "chrome", "profile": "Sam"}, {"ok": True, "message": "Opened Chrome search."}, "Opened that search in Chrome using your Sam profile."),
        ("Look up latest FastAPI release notes online", "google_search", {"query": "latest FastAPI release notes", "open_in_browser": False}, {"ok": True, "summary": "FastAPI 0.115 introduces improved typed form handling."}, "FastAPI 0.115 adds improved typed form model support."),
        ("Close WhatsApp", "close_app", {"target": "WhatsApp"}, {"ok": True, "staged": True}, "I staged a close confirmation for WhatsApp."),
        ("Close File Explorer", "close_app", {"target": "file manager"}, {"ok": True, "staged": True}, "Ready to close your File Explorer folder window once you confirm."),
        ("Minimize Chrome", "window_control", {"action": "minimize", "query": "chrome"}, {"ok": True, "message": "Minimized Google Chrome."}, "Minimized Google Chrome."),
        ("Can you minimize this window?", "window_control", {"action": "minimize"}, {"ok": True, "message": "Minimized active window."}, "Minimized the active window."),
        ("Maximize Word", "window_control", {"action": "maximize", "query": "word"}, {"ok": True, "message": "Maximized Microsoft Word."}, "Maximized Microsoft Word."),
        ("Switch to VS Code window", "focus_window", {"query": "code"}, {"ok": True, "message": "Focused Visual Studio Code."}, "Switched focus to Visual Studio Code."),
        ("What windows are open right now?", "list_windows", {}, {"ok": True, "windows": [{"title": "Report.docx - Word", "process": "WINWORD.EXE"}, {"title": "Chrome", "process": "chrome.exe"}]}, "You currently have Microsoft Word and Google Chrome open."),
        ("List files inside Documents", "navigate_files", {"path": "Documents", "action": "list"}, {"ok": True, "result": "[DIR] Reports\n[FILE] outline.docx"}, "Your Documents folder contains a Reports directory and outline document."),
        ("Find budget pdf files in Downloads", "navigate_files", {"path": "Downloads", "action": "find", "pattern": "*budget*.pdf"}, {"ok": True, "result": "Downloads\\Q3_budget.pdf"}, "I found one matching file, Q3 budget PDF, in Downloads."),
        ("What buttons are on this screen?", "inspect_ui_elements", {"max_controls": 50}, {"ok": True, "count": 4, "controls": [{"type": "ButtonControl", "name": "Save"}, {"type": "ButtonControl", "name": "Cancel"}]}, "I see Save and Cancel buttons on the active window."),
        ("Read what is currently on my screen", "read_active_screen", {}, {"ok": True, "window_title": "Terminal", "text": "pytest: 12 passed in 0.9s"}, "Your screen shows a terminal where 12 pytest checks passed."),
    ]
    prefixes = ["", "Jude, ", "Hey Jude, ", "Please ", "Can you "]
    suffixes = ["", " for me", " please"]
    for idx in range(7):
        for q, tname, targs, tres, reply in win_specs:
            pref = prefixes[(idx + len(q)) % len(prefixes)] if not q.lower().startswith("jude") else ""
            suf = suffixes[idx % len(suffixes)] if not q.endswith("?") else ""
            prompt = f"{pref}{q[0].lower() + q[1:] if pref else q}{suf}".strip()
            ctx = "Active Window: Google Chrome (chrome.exe)" if idx % 2 == 0 else "Active Window: Explorer (explorer.exe)"
            if idx % 3 == 0:
                rows.append(_make_multi_turn("windows_apps", prompt, tname, targs, tres, reply, live_ctx=ctx))
            else:
                rows.append(_make_single_turn("windows_apps", prompt, tname, targs, live_ctx=ctx))

    # 2. Word Document Reading, Creation, Preference-Aware Editing
    word_ctx = "Active Window: Project_Report.docx - Word (winword.exe)\nOpen Word Doc: Project_Report.docx\nLearned Preferences: word_edit_style=concise, formal"
    word_specs = [
        ("Read the executive summary in my Word document", "read_open_word_doc", {"section_hint": "executive summary"}, {"ok": True, "doc_name": "Project_Report.docx", "content": "This project builds a local AI desktop assistant."}, "Your executive summary explains how the project builds a local AI desktop assistant."),
        ("Read what is inside my open Word file", "read_open_word_doc", {"section_hint": ""}, {"ok": True, "doc_name": "Project_Report.docx", "content": "Introduction and system benchmarks."}, "Your open Word document covers the introduction and system benchmarks."),
        ("Read the conclusion section for me", "read_open_word_doc", {"section_hint": "conclusion"}, {"ok": True, "doc_name": "Project_Report.docx", "content": "Two-tier routing reduces latency."}, "The conclusion highlights that two-tier routing reduces latency."),
        ("Create a new Word document titled Lab Proposal about neural routing", "create_word_doc", {"title": "Lab Proposal", "content": "Overview\nThis proposal outlines a two-tier local AI router combining DistilBERT and QLoRA."}, {"ok": True, "staged": True}, "I drafted the Lab Proposal document for your review before saving."),
        ("Make a Word document for weekly sprint notes", "create_word_doc", {"title": "Weekly Sprint Notes", "content": "Sprint Goals\n1. Complete tool schemas\n2. Run QLoRA benchmarks"}, {"ok": True, "staged": True}, "I prepared the Weekly Sprint Notes draft; confirm when you want me to save it."),
        ("Rewrite the highlighted paragraph to sound formal", "edit_word_doc", {"instruction": "Rewrite to sound formal and concise"}, {"ok": True, "staged": True}, "I drafted a formal revision of the highlighted paragraph for your confirmation."),
        ("Make the methodology section shorter and clearer", "edit_word_doc", {"instruction": "Make shorter and clearer", "section_hint": "methodology"}, {"ok": True, "staged": True}, "I prepared a shorter, clearer edit for the methodology section."),
        ("Add my name to the highlighted area in Word", "edit_word_doc", {"instruction": "Insert the user's saved name into the highlighted text"}, {"ok": True, "staged": True}, "I staged the Word update with your name for confirmation."),
        ("Polish the introduction in my Word doc", "edit_word_doc", {"instruction": "Polish wording formally", "section_hint": "introduction"}, {"ok": True, "staged": True}, "I prepared a polished draft of the introduction for your approval."),
    ]
    for idx in range(14):
        for q, tname, targs, tres, reply in word_specs:
            p = q if idx % 2 == 0 else f"Jude, {q[0].lower() + q[1:]}"
            if idx % 3 == 0:
                rows.append(_make_multi_turn("word_context", p, tname, targs, tres, reply, live_ctx=word_ctx))
            else:
                rows.append(_make_single_turn("word_context", p, tname, targs, live_ctx=word_ctx))

    # 3. VMware Linux Guest Bridge + Native OCR Viewport
    vm_ctx = "Active Window: Ubuntu 24.04 - VMware Workstation (vmware.exe) [VMware viewport -> OCR active]"
    vm_specs = [
        ("List running VMware virtual machines", "vmware_list_vms", {}, {"ok": True, "vms": [{"name": "Ubuntu", "ip": "192.168.56.101"}]}, "Your Ubuntu VM is running at 192.168.56.101."),
        ("Check my Linux VM disk usage", "vmware_run_linux_cmd", {"command": "df -h"}, {"ok": True, "exit_code": 0, "stdout": "/dev/sda1 40G 14G 24G 37% /"}, "Your Linux root partition has 24 gigabytes free out of 40."),
        ("Show running processes inside the Ubuntu VM", "vmware_run_linux_cmd", {"command": "ps aux --sort=-%cpu | head -n 6"}, {"ok": True, "exit_code": 0, "stdout": "PID 112 python3 server.py"}, "Python server is currently the top active process in the VM."),
        ("Check memory usage in my VMware Linux guest", "vmware_run_linux_cmd", {"command": "free -h"}, {"ok": True, "exit_code": 0, "stdout": "Mem: 4.0Gi total, 1.3Gi used"}, "Your Linux VM is using 1.3 of 4.0 gigabytes of memory."),
        ("Run sudo apt update inside Ubuntu", "vmware_run_linux_cmd", {"command": "sudo apt update"}, {"ok": True, "staged": True}, "Sudo package updates require confirmation in the dialog first."),
        ("Restart nginx service inside my Linux VM", "vmware_run_linux_cmd", {"command": "sudo systemctl restart nginx"}, {"ok": True, "staged": True}, "I staged the sudo nginx restart command for your approval."),
        ("Check nginx error logs in Ubuntu", "vmware_run_linux_cmd", {"command": "tail -n 20 /var/log/nginx/error.log"}, {"ok": True, "exit_code": 0, "stdout": "no critical errors"}, "There are no critical errors in the recent nginx log."),
        ("Read the error shown on my VMware Linux screen", "read_active_screen", {}, {"ok": True, "source": "vmware_ocr", "text": "PermissionError: port 8000 already in use"}, "Your VMware screen shows a PermissionError because port 8000 is already in use."),
        ("Show current directory and uptime on Ubuntu VM", "vmware_run_linux_cmd", {"command": "pwd && uptime"}, {"ok": True, "exit_code": 0, "stdout": "/home/dev  up 2:14"}, "You are in /home/dev and the VM has been up for 2 hours 14 minutes."),
        ("Inspect network interfaces in VMware Linux", "vmware_run_linux_cmd", {"command": "ip -brief addr"}, {"ok": True, "exit_code": 0, "stdout": "ens33 UP 192.168.56.101/24"}, "Interface ens33 is up on 192.168.56.101."),
    ]
    vm_prefixes = ["", "Hey Jude, ", "Jude, ", "Please "]
    for idx in range(15):
        for q, tname, targs, tres, reply in vm_specs:
            vp = vm_prefixes[(idx + len(q)) % len(vm_prefixes)]
            p = f"{vp}{q[0].lower() + q[1:] if vp else q}"
            if idx % 3 == 0:
                rows.append(_make_multi_turn("vmware_linux", p, tname, targs, tres, reply, live_ctx=vm_ctx))
            else:
                rows.append(_make_single_turn("vmware_linux", p, tname, targs, live_ctx=vm_ctx))

    # 4. Honest Out-of-Scope Refusals & Grounded Identity (ZERO tool_calls!)
    refusal_pairs = [
        ("Send hello to Alex on WhatsApp", "I can open WhatsApp for you, but I cannot send messages to contacts automatically yet."),
        ("Message Sam that I am running late", "I don't have a messaging tool to send texts automatically, though I can open WhatsApp if you'd like."),
        ("Type hello inside my WhatsApp chat", "I don't have an arbitrary keyboard-typing tool for WhatsApp messages yet, so I haven't typed anything."),
        ("Send an email to my professor", "I cannot send emails automatically yet because no email-sending tool is connected."),
        ("Transfer 50 dollars to my friend", "I cannot perform banking or money transfers."),
        ("Delete all files on drive C", "I cannot wipe drives or delete system files automatically."),
        ("Click the submit button inside Roblox", "I can list or close open windows, but I cannot automatically click inside Roblox."),
        ("Change my Windows password for me", "I cannot change system passwords or security credentials."),
        ("Who made you?", "I am J.U.D.E., a local-first Windows 11 desktop AI assistant running on your PC."),
        ("Are you Qwen from Alibaba Cloud?", "No, I am J.U.D.E., your local-first personal desktop AI agent."),
        ("Close nonexistent_mystery_app_xyz", "Let me check open windows first before claiming anything is closed."),
    ]
    variants = [
        lambda s: s,
        lambda s: f"Jude, {s[0].lower() + s[1:]}",
        lambda s: f"Can you {s[0].lower() + s[1:]}?",
        lambda s: f"Please {s[0].lower() + s[1:]} right now",
    ]
    for idx in range(12):
        fn = variants[idx % len(variants)]
        for q, ans in refusal_pairs:
            # Keep the last check tool-free for clear unsupported refusal training
            if "nonexistent" in q:
                rows.append(_make_single_turn("out_of_scope_refusal", fn("Send this file via Telegram to Alex"), None, direct_reply="I cannot send Telegram messages automatically because no Telegram tool is enabled."))
            else:
                rows.append(_make_single_turn("out_of_scope_refusal", fn(q), None, direct_reply=ans))

    # 5. Memory, Preferences & Messy Voice Phrasing
    mem_specs = [
        ("Remember my student ID is 24-CS-108", "memory_save", {"fact": "student ID is 24-CS-108"}, {"ok": True, "message": "Saved."}, "Saved your student ID to local memory."),
        ("Remember that I prefer concise formal Word edits", "memory_save", {"preference_key": "word_edit_style", "preference_value": "concise formal"}, {"ok": True, "message": "Saved."}, "Saved your preference for concise formal Word edits."),
        ("What is my student ID?", "memory_recall", {"query": "student ID"}, {"ok": True, "result": "From memory: student ID is 24-CS-108"}, "Your saved student ID is 24-CS-108."),
        ("Forget my temporary hotel booking note", "memory_delete", {"query": "hotel booking note"}, {"ok": True, "result": "Removed from memory."}, "Removed the hotel booking note from local memory."),
        ("uh hey jude can you um open chrome for me", "open_app_or_site", {"target": "chrome"}, {"ok": True, "message": "Launched Google Chrome."}, "Launched Google Chrome."),
        ("hey jude check my ubuntu vm memory", "vmware_run_linux_cmd", {"command": "free -h"}, {"ok": True, "exit_code": 0, "stdout": "Mem: 4.0Gi total, 1.4Gi used"}, "Your Ubuntu VM is using 1.4 of 4.0 gigabytes of RAM."),
        ("jude please open file manager right now", "open_app_or_site", {"target": "file manager"}, {"ok": True, "message": "Launched File Explorer."}, "Opened File Explorer."),
        ("open terminal please jude", "open_terminal", {"path": ""}, {"ok": True, "message": "Opened terminal."}, "Opened Windows Terminal."),
        ("save a note that project deadline is Friday", "memory_save", {"fact": "project deadline is Friday"}, {"ok": True, "message": "Saved."}, "Saved your project deadline note."),
        ("recall what browser I prefer", "memory_recall", {"query": "preferred browser"}, {"ok": True, "result": "From memory: default browser is Chrome."}, "Your saved preference is Chrome."),
        ("remember I like short bulleted summaries", "memory_save", {"preference_key": "word_edit_style", "preference_value": "short bulleted"}, {"ok": True, "message": "Saved."}, "Noted that you prefer short bulleted summaries."),
        ("open chrome guest profile please jude", "open_app_or_site", {"target": "chrome", "profile": "Guest"}, {"ok": True, "message": "Opened Chrome Guest."}, "Opened Google Chrome in Guest mode."),
    ]
    mem_prefixes = ["", "hey ", "please ", "jude "]
    for idx in range(12):
        for q, tname, targs, tres, reply in mem_specs:
            ctx = vm_ctx if "ubuntu" in q else "Active Window: Notes.txt (notepad.exe)"
            mp = mem_prefixes[(idx + len(q)) % len(mem_prefixes)]
            prompt = q if (not mp or q.lower().startswith(("hey", "jude", "uh", "please"))) else f"{mp}{q}"
            if idx % 3 == 0:
                rows.append(_make_multi_turn("voice_memory", prompt, tname, targs, tres, reply, live_ctx=ctx))
            else:
                rows.append(_make_single_turn("voice_memory", prompt, tname, targs, live_ctx=ctx))

    return rows


def main() -> int:
    rng = random.Random(42)
    all_rows = _build_all_examples()
    rng.shuffle(all_rows)

    # Deduplicate by (user_text, category) for clean held-out eval split (18 per category = 90 eval)
    by_cat: dict[str, list[dict]] = {}
    for row in all_rows:
        by_cat.setdefault(row["category"], []).append(row)

    eval_rows: list[dict] = []
    train_rows: list[dict] = []
    seen_eval_prompts: set[str] = set()
    for cat, cat_rows in sorted(by_cat.items()):
        picked = 0
        for row in cat_rows:
            u_norm = row["messages"][1]["content"].lower().strip()
            # Prefer single-turn prompt->action rows for deterministic eval scoring
            is_eval_candidate = len(row["messages"]) == 3
            if is_eval_candidate and picked < 18 and u_norm not in seen_eval_prompts:
                seen_eval_prompts.add(u_norm)
                eval_rows.append(
                    {
                        "category": cat,
                        "expected_tool": row["expected_tool"],
                        "expected_args": row["expected_args"],
                        "tool_names": row["tool_names"],
                        "messages": row["messages"][:-1],
                    }
                )
                picked += 1
            else:
                train_rows.append(row)

    # Privacy check on generated rows
    for r in train_rows + eval_rows:
        blob = json.dumps(r).lower()
        for bad in _PRIVACY_GUARD_SUBSTRINGS:
            if bad in blob:
                raise RuntimeError(f"Privacy violation detected ({bad}) in generated dataset!")

    colab_dir = Path(__file__).resolve().parent
    train_path = colab_dir / "jude_agent_dataset.jsonl"
    eval_path = colab_dir / "jude_agent_eval.jsonl"

    with train_path.open("w", encoding="utf-8") as f:
        for row in train_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    with eval_path.open("w", encoding="utf-8") as f:
        for row in eval_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Using {len(AGENT_TOOL_SCHEMAS)} schemas from {_SCHEMAS_PATH.name}")
    print(f"Generated {len(train_rows)} train examples -> {train_path} ({train_path.stat().st_size / 1024:.1f} KB)")
    print(f"Generated {len(eval_rows)} eval examples -> {eval_path} ({eval_path.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
