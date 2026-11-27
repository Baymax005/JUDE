from __future__ import annotations

import json
import re
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus

from app.config import BACKUPS_DIR, MEMORY_DB_PATH
from app.tier1.actions import (
    control_window,
    focus_window,
    get_system_info,
    list_open_windows,
    navigate_files,
    open_chrome,
    open_target,
    open_terminal,
    plan_close_app,
)
from app.tier1.memory import (
    delete_memory_by_query,
    log_activity,
    recall_memory,
    save_memory,
    set_preference,
)
from app.tier2.ollama_client import JUDE_SYSTEM_PROMPT, OllamaClient
from app.tier2.screen_reader import (
    capture_window_context,
    get_live_context_header,
    inspect_ui_elements,
    read_word_document,
)
from app.tier2.web_search import run_web_search
from app.tier2.word_editor import prepare_word_create, prepare_word_edit
from app.tools.vmware_linux_tool import is_modifying_linux_command, vmware_list_vms, execute_vmware_ssh_command

AGENT_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_open_word_doc",
            "description": "Read text or a named section from the currently open Microsoft Word document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "section_hint": {"type": "string", "description": "Optional section heading or keyword, e.g. 'executive summary'."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_word_doc",
            "description": "Draft a new Microsoft Word (.docx) document for user confirmation before saving.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Document title."},
                    "content": {"type": "string", "description": "Full body content of the document."},
                    "save_path": {"type": "string", "description": "Optional target folder or .docx path."},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_word_doc",
            "description": "Prepare a proposed edit to the open Microsoft Word document selection or named section.",
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {"type": "string", "description": "What change or rewrite to make."},
                    "section_hint": {"type": "string", "description": "Optional section name if nothing is highlighted."},
                },
                "required": ["instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app_or_site",
            "description": "Open a Windows application, File Explorer, WhatsApp, Chrome (optional profile), Google, YouTube, drive, or folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "App name ('chrome', 'word', 'file manager', 'whatsapp'), drive ('D drive'), or path."},
                    "profile": {"type": "string", "description": "Optional Chrome user profile name (e.g., 'Guest' or profile display name)."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_app",
            "description": "Stage closing an open desktop application window for user confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Window title or application name to close."}
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_windows",
            "description": "List visible open desktop windows with titles and process names.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_window",
            "description": "Bring an open window to the foreground by title or process name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Partial window title or process name."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "window_control",
            "description": "Minimize, maximize, or restore an open desktop window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["minimize", "maximize", "restore"]},
                    "query": {"type": "string", "description": "Optional partial window title or app name; omit for active window."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_terminal",
            "description": "Open Windows Terminal or PowerShell. Leave path empty unless the user explicitly named a folder or drive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional existing directory path or drive ('D:\\'); omit or pass '' for default."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate_files",
            "description": "List files in a folder, search files by pattern, or open a path in Explorer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Folder path or drive."},
                    "action": {"type": "string", "enum": ["list", "find", "open"]},
                    "pattern": {"type": "string", "description": "Filename pattern when action is 'find'."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "google_search",
            "description": "Search the web and summarize results, or open Google Search directly in Google Chrome.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "open_in_browser": {"type": "boolean", "description": "Set true if the user wants Google opened in Chrome/browser."},
                    "browser": {"type": "string", "enum": ["chrome", "default"], "description": "Browser to open; defaults to 'chrome'."},
                    "profile": {"type": "string", "description": "Optional Chrome profile name."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_active_screen",
            "description": "Read text from the active window (uses native Windows OCR automatically for VMware).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_ui_elements",
            "description": "Inspect up to 50 visible interactive UI controls on the active window (masks passwords).",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_controls": {"type": "integer", "description": "Maximum controls to return (1-50)."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "Save a personal fact or a user workflow/style preference to local memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "Personal fact to remember."},
                    "preference_key": {"type": "string", "description": "Optional preference key, e.g. 'word_edit_style'."},
                    "preference_value": {"type": "string", "description": "Optional preference value, e.g. 'formal and concise'."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_recall",
            "description": "Search saved personal memories and facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Topic or query to search in local memory."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_delete",
            "description": "Delete a saved personal memory matching a query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Topic or memory description to remove."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vmware_list_vms",
            "description": "List running VMware virtual machines via vmrun and resolve guest IPs.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vmware_run_linux_cmd",
            "description": "Execute a Linux shell command inside a running VMware VM over key-based SSH.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Linux shell command to execute."},
                    "host_ip": {"type": "string", "description": "Optional VM IP address."},
                    "vmx_path": {"type": "string", "description": "Optional VM .vmx path."},
                },
                "required": ["command"],
            },
        },
    },
]

_TOOL_BY_NAME: dict[str, dict] = {
    schema["function"]["name"]: schema for schema in AGENT_TOOL_SCHEMAS
}

_CORE_TOOLS = (
    "open_app_or_site",
    "close_app",
    "list_windows",
    "focus_window",
    "window_control",
    "open_terminal",
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


def select_tools_for_turn(user_text: str, live_ctx: str = "") -> list[dict]:
    """Prune irrelevant Word/VMware/UI tool schemas to cut tokens and improve accuracy."""
    combined = f"{user_text}\n{live_ctx}"
    names: list[str] = list(_CORE_TOOLS)
    if "Open Word Doc:" in live_ctx or _WORD_CTX_RE.search(combined):
        names.extend(_WORD_TOOLS)
    if "vmware" in live_ctx.lower() or _VMWARE_CTX_RE.search(combined):
        names.extend(_VMWARE_TOOLS)
    if _UI_CTX_RE.search(user_text):
        names.append("inspect_ui_elements")
    return [_TOOL_BY_NAME[n] for n in names if n in _TOOL_BY_NAME]


_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", flags=re.DOTALL)
_FAKE_TOOL_CLAIM_RE = re.compile(
    r"\bI\s+(?:called|executed|ran|invoked)\s+(?:window_control|close_app|open_app_or_site|"
    r"open_terminal|read_active_screen|edit_word_doc|create_word_doc|list_windows|focus_window)\b",
    flags=re.IGNORECASE,
)


def _extract_tool_calls(msg: dict) -> list[tuple[str, dict]]:
    """Extract (tool_name, args_dict) pairs from Ollama's message dict or <tool_call> tags."""
    parsed: list[tuple[str, dict]] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "").strip()
        raw_args = fn.get("arguments") or {}
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except json.JSONDecodeError:
                raw_args = {}
        if name and isinstance(raw_args, dict):
            parsed.append((name, raw_args))

    if parsed:
        return parsed

    content = str(msg.get("content") or "")
    for match in _TOOL_CALL_TAG_RE.finditer(content):
        try:
            obj = json.loads(match.group(1))
            name = str(obj.get("name") or "").strip()
            args = obj.get("arguments") or {}
            if isinstance(args, str):
                args = json.loads(args)
            if name and isinstance(args, dict):
                parsed.append((name, args))
        except Exception:
            continue
    return parsed


def _execute_tool_call(
    name: str,
    args: dict,
    ollama: OllamaClient,
    target_hwnd: int | None,
    memory_db_path: Path,
    backups_dir: Path,
) -> tuple[str, dict | None, list[tuple[str, str]]]:
    """Execute a tool call or return a pending_spec when user confirmation is required."""
    sources: list[tuple[str, str]] = []

    if name == "read_open_word_doc":
        res = read_word_document(section_hint=str(args.get("section_hint") or ""))
        if res.get("ok"):
            log_activity("word_read", res.get("doc_name", "Word"), str(args.get("section_hint") or ""), memory_db_path)
        return json.dumps(res, ensure_ascii=False)[:3000], None, sources

    if name == "create_word_doc":
        prep = prepare_word_create(
            title=str(args.get("title") or "J.U.D.E. Document"),
            content=str(args.get("content") or ""),
            save_path=str(args.get("save_path") or ""),
        )
        if not prep.get("ok"):
            return json.dumps(prep, ensure_ascii=False), None, sources
        spec = {
            "kind": "word_create",
            "title": f"Confirm New Word Document ({Path(prep['target_path']).name})",
            "summary": "Review or edit the proposed document content below before J.U.D.E. writes the .docx file.",
            "intent": "CREATE_DOCUMENT",
            "confidence": 1.0,
            "target_path": prep["target_path"],
            "backup_note": "Existing files are never overwritten; writes using a unique filename.",
            "after_text": prep["content"],
            "payload": prep,
        }
        return json.dumps({"ok": True, "staged": True}, ensure_ascii=False), spec, sources

    if name == "edit_word_doc":
        prep = prepare_word_edit(
            instruction=str(args.get("instruction") or "Improve this text"),
            ollama=ollama,
            memory_db_path=memory_db_path,
            section_hint=str(args.get("section_hint") or ""),
        )
        if not prep.get("ok"):
            return json.dumps(prep, ensure_ascii=False), None, sources
        spec = {
            "kind": "word_edit",
            "title": f"Confirm Word Edit ({prep['doc_name']})",
            "summary": "Review the proposed revision below. A lightweight text undo snapshot will be saved first.",
            "intent": "EDIT_DOCUMENT",
            "confidence": 1.0,
            "target_path": prep.get("doc_full_path") or prep["doc_name"],
            "backup_note": f"Text undo snapshot saved into {Path(backups_dir).name}/ before applying.",
            "before_text": prep["before_text"],
            "after_text": prep["after_text"],
            "payload": {**prep, "instruction": str(args.get("instruction") or "")},
        }
        return json.dumps({"ok": True, "staged": True}, ensure_ascii=False), spec, sources

    if name == "open_app_or_site":
        target = str(args.get("target") or "")
        profile = str(args.get("profile") or "").strip()
        if "chrome" in target.lower():
            ok, msg = open_chrome(profile=profile)
        else:
            ok, msg = open_target(target)
        if ok:
            log_activity("open_target", target, msg, memory_db_path)
        return json.dumps({"ok": ok, "message": msg}, ensure_ascii=False), None, sources

    if name == "window_control":
        act = str(args.get("action") or "minimize")
        q = str(args.get("query") or "")
        ok, msg = control_window(action=act, query=q, target_hwnd=target_hwnd)
        if ok:
            log_activity("window_control", f"{act}:{q or 'active'}", msg, memory_db_path)
        return json.dumps({"ok": ok, "message": msg}, ensure_ascii=False), None, sources

    if name == "close_app":
        plan = plan_close_app(str(args.get("target") or ""))
        if not plan.get("ok"):
            return json.dumps(plan, ensure_ascii=False), None, sources
        spec = {
            "kind": "close_app",
            "title": f"Confirm Closing {plan['process']}",
            "summary": f"Send a close request to window `{plan['title']}` ({plan['process']})? Unsaved work inside the app may prompt to save.",
            "intent": "CLOSE_APP",
            "confidence": 1.0,
            "target_path": f"{plan['title']} ({plan['process']})",
            "payload": plan,
        }
        return json.dumps({"ok": True, "staged": True}, ensure_ascii=False), spec, sources

    if name == "list_windows":
        wins = list_open_windows()[:25]
        return json.dumps({"ok": True, "windows": wins}, ensure_ascii=False)[:2800], None, sources

    if name == "focus_window":
        q = str(args.get("query") or "")
        ok, msg = focus_window(q)
        if ok:
            log_activity("focus_window", q, msg, memory_db_path)
        return json.dumps({"ok": ok, "message": msg}, ensure_ascii=False), None, sources

    if name == "open_terminal":
        path_arg = str(args.get("path") or "")
        ok, msg = open_terminal(path_arg)
        if ok:
            log_activity("open_terminal", path_arg or "JUDE", msg, memory_db_path)
        return json.dumps({"ok": ok, "message": msg}, ensure_ascii=False), None, sources

    if name == "navigate_files":
        res_txt = navigate_files(
            path=str(args.get("path") or ""),
            action=str(args.get("action") or "list"),
            pattern=str(args.get("pattern") or ""),
        )
        log_activity("navigate_files", str(args.get("path") or ""), str(args.get("action") or "list"), memory_db_path)
        return json.dumps({"ok": True, "result": res_txt[:2500]}, ensure_ascii=False), None, sources

    if name == "google_search":
        q = str(args.get("query") or "").strip()
        if bool(args.get("open_in_browser")):
            url = f"https://www.google.com/search?q={quote_plus(q)}"
            browser = str(args.get("browser") or "chrome").strip().lower()
            profile = str(args.get("profile") or "").strip()
            if browser == "chrome":
                ok, msg = open_chrome(url=url, profile=profile)
            else:
                webbrowser.open(url)
                ok, msg = True, f"Opened Google search for '{q}' in default browser."
            log_activity("google_browser", q, url, memory_db_path)
            return json.dumps({"ok": ok, "message": msg}, ensure_ascii=False), None, sources
        summary, srcs = run_web_search(q, ollama)
        sources.extend(srcs)
        log_activity("web_search", q, "", memory_db_path)
        return json.dumps({"ok": True, "summary": summary[:2500]}, ensure_ascii=False), None, sources

    if name == "read_active_screen":
        ctx = capture_window_context(target_hwnd=target_hwnd, max_chars=2600)
        return json.dumps(ctx, ensure_ascii=False)[:3000], None, sources

    if name == "inspect_ui_elements":
        max_c = int(args.get("max_controls") or 50)
        res = inspect_ui_elements(target_hwnd=target_hwnd, max_controls=max_c)
        return json.dumps(res, ensure_ascii=False)[:3000], None, sources

    if name == "memory_save":
        pref_k = str(args.get("preference_key") or "").strip()
        pref_v = str(args.get("preference_value") or "").strip()
        fact = str(args.get("fact") or "").strip()
        msgs: list[str] = []
        if pref_k and pref_v:
            msgs.append(set_preference(pref_k, pref_v, source="agent", db_path=memory_db_path))
        if fact:
            msgs.append(save_memory(fact, db_path=memory_db_path))
        if not msgs:
            return json.dumps({"ok": False, "error": "Nothing provided to save."}), None, sources
        return json.dumps({"ok": True, "message": " ".join(msgs)}, ensure_ascii=False), None, sources

    if name == "memory_recall":
        res = recall_memory(str(args.get("query") or ""), db_path=memory_db_path)
        return json.dumps({"ok": True, "result": res}, ensure_ascii=False), None, sources

    if name == "memory_delete":
        res = delete_memory_by_query(str(args.get("query") or ""), db_path=memory_db_path)
        return json.dumps({"ok": True, "result": res}, ensure_ascii=False), None, sources

    if name == "vmware_list_vms":
        res = vmware_list_vms()
        return json.dumps(res, ensure_ascii=False)[:2600], None, sources

    if name == "vmware_run_linux_cmd":
        cmd = str(args.get("command") or "").strip()
        host_ip = str(args.get("host_ip") or "").strip()
        vmx_path = str(args.get("vmx_path") or "").strip()
        needs_confirm, reason = is_modifying_linux_command(cmd)
        if needs_confirm:
            spec = {
                "kind": "vmware_cmd",
                "title": "Confirm Linux VM Command",
                "summary": f"{reason} Confirm before executing inside the VMware Linux guest via key-based SSH.",
                "intent": "VMWARE_LINUX_CMD",
                "confidence": 1.0,
                "target_path": host_ip or vmx_path or "Auto-detected VMware Linux Guest",
                "backup_note": cmd,
                "payload": {"command": cmd, "host_ip": host_ip, "vmx_path": vmx_path},
            }
            return json.dumps({"ok": True, "staged": True}, ensure_ascii=False), spec, sources

        res = execute_vmware_ssh_command(cmd, host_ip=host_ip, vmx_path=vmx_path)
        if res.get("ok"):
            log_activity("vmware_ssh", cmd[:80], res.get("host_ip", ""), memory_db_path)
        return json.dumps(res, ensure_ascii=False)[:3000], None, sources

    if name == "system_info":
        return json.dumps({"ok": True, "result": get_system_info("")}, ensure_ascii=False), None, sources

    return json.dumps({"ok": False, "error": f"Unknown tool: {name}"}), None, sources


def run_agent_turn(
    user_text: str,
    ollama: OllamaClient,
    target_hwnd: int | None = None,
    memory_db_path: Path = MEMORY_DB_PATH,
    backups_dir: Path = BACKUPS_DIR,
    max_steps: int = 3,
) -> dict:
    """Run up to max_steps native Ollama tool-calling turns with live window & habit context."""
    live_ctx = get_live_context_header(target_hwnd=target_hwnd, memory_db_path=memory_db_path)
    sys_content = JUDE_SYSTEM_PROMPT
    if live_ctx:
        sys_content += f"\n\n[Live Context]\n{live_ctx}"

    messages: list[dict] = [{"role": "system", "content": sys_content}]
    messages.extend(ollama.get_history())
    messages.append({"role": "user", "content": user_text})

    all_sources: list[tuple[str, str]] = []
    tools_used: list[str] = []
    opened_targets: list[str] = []
    turn_tools = select_tools_for_turn(user_text, live_ctx)

    for _ in range(max_steps):
        resp = ollama.chat_with_tools(messages=messages, tools=turn_tools)
        if not resp.get("ok"):
            err = resp.get("error", "Tier 2 agent failed.")
            return {
                "reply": err,
                "sources": all_sources,
                "pending_spec": None,
                "tools_used": tools_used,
                "opened_targets": opened_targets,
            }

        msg = resp["message"]
        tool_calls = _extract_tool_calls(msg)
        if not tool_calls:
            reply = _TOOL_CALL_TAG_RE.sub("", str(msg.get("content") or "")).strip()
            if not tools_used and _FAKE_TOOL_CLAIM_RE.search(reply):
                reply = "I didn't run any action for that yet. Tell me clearly which app or window you want me to act on."
            if not reply:
                reply = "Done."
            ollama.record_turn(user_text, reply)
            if all_sources:
                src_lines = "\n".join(f"- [{t}]({u})" for t, u in all_sources[:3])
                if "Sources:" not in reply:
                    reply = f"{reply}\n\nSources:\n{src_lines}"
            return {
                "reply": reply,
                "sources": all_sources,
                "pending_spec": None,
                "tools_used": tools_used,
                "opened_targets": opened_targets,
            }

        messages.append(msg)
        for tool_name, tool_args in tool_calls:
            tools_used.append(tool_name)
            result_json, pending_spec, new_sources = _execute_tool_call(
                name=tool_name,
                args=tool_args,
                ollama=ollama,
                target_hwnd=target_hwnd,
                memory_db_path=memory_db_path,
                backups_dir=backups_dir,
            )
            if tool_name == "open_app_or_site":
                try:
                    parsed_res = json.loads(result_json)
                    if parsed_res.get("ok"):
                        opened_targets.append(str(tool_args.get("target") or "").strip())
                except Exception:
                    pass
            all_sources.extend(new_sources)
            if pending_spec is not None:
                return {
                    "reply": f"Ready: {pending_spec['title']}. Please confirm in the dialog.",
                    "sources": all_sources,
                    "pending_spec": pending_spec,
                    "tools_used": tools_used,
                    "opened_targets": opened_targets,
                }
            messages.append({"role": "tool", "content": result_json})

    # Final synthesis turn after tool steps complete
    final_resp = ollama.chat_with_tools(messages=messages, tools=[])
    final_msg = final_resp.get("message") or {}
    reply = _TOOL_CALL_TAG_RE.sub("", str(final_msg.get("content") or "")).strip() or "Completed your request."
    ollama.record_turn(user_text, reply)
    if all_sources and "Sources:" not in reply:
        src_lines = "\n".join(f"- [{t}]({u})" for t, u in all_sources[:3])
        reply = f"{reply}\n\nSources:\n{src_lines}"
    return {
        "reply": reply,
        "sources": all_sources,
        "pending_spec": None,
        "tools_used": tools_used,
        "opened_targets": opened_targets,
    }
