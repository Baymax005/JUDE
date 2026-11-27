from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["JUDE_HEADLESS_TEST"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import CACHE_DIR, assert_no_c_drive_leakage, configure_d_drive_env


def _make_temp_dir(name: str) -> Path:
    d = CACHE_DIR / "tmp" / f"test_v2_{name}_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_no_nagging_intent_confirm_and_agent_routing() -> None:
    from app.engine import JudeEngine

    tmp = _make_temp_dir("silent_gate")
    try:
        class _StubOllama:
            def clear_history(self) -> None:
                pass

            def get_history(self) -> list[dict]:
                return []

            def record_turn(self, u: str, a: str) -> None:
                pass

            def ensure_server_running(self) -> bool:
                return True

            def chat_with_tools(self, messages, tools, temperature=0.2, max_tokens=600):
                return {
                    "ok": True,
                    "message": {
                        "role": "assistant",
                        "content": "I am J.U.D.E., your local-first personal desktop AI agent.",
                    },
                    "error": "",
                }

        engine = JudeEngine(
            memory_db_path=tmp / "memory.db",
            backups_dir=tmp / "backups",
            feedback_path=tmp / "feedback.jsonl",
            ollama=_StubOllama(),
        )
        # Previously triggered "Confirm RECALL_MEMORY (67%)" modal!
        res = engine.process("What is your name and what is the purpose of you?")
        assert res.tier_used == "tier2", f"Expected silent Tier 2 escalation, got: {res}"
        assert res.pending is None, f"Unexpected confirm popup: {res.pending}"
        assert "J.U.D.E." in res.reply
        print("[OK] Silent Tier 1 -> Tier 2 escalation (zero nagging popups) passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_habits_preferences_and_memory_dialog() -> None:
    from PySide6.QtWidgets import QApplication

    from app.tier1.memory import (
        build_user_profile_context,
        clear_activity_log,
        clear_all_preferences,
        delete_preference_by_id,
        get_frequent_activities,
        infer_word_style_preference,
        list_preferences,
        log_activity,
        set_preference,
    )
    from app.ui.memory_dialog import MemoryManagerDialog

    tmp = _make_temp_dir("habits")
    try:
        db = tmp / "memory.db"
        log_activity("open_terminal", r"X:\Projects\Demo", "Terminal", db_path=db)
        log_activity("open_terminal", r"X:\Projects\Demo", "Terminal", db_path=db)
        log_activity("open_target", "WhatsApp", "Opened WhatsApp", db_path=db)

        freq = get_frequent_activities(db_path=db, limit=5)
        assert freq and freq[0]["target"] == r"X:\Projects\Demo" and freq[0]["count"] == 2, freq

        inferred = infer_word_style_preference(
            "Make this paragraph formal and concise",
            "Long draft text " * 10,
            "Short formal version.",
            db_path=db,
        )
        assert inferred == "concise, formal", inferred
        set_preference("default_browser", "Chrome", source="explicit", db_path=db)

        profile = build_user_profile_context(db_path=db)
        assert "word_edit_style=concise, formal" in profile and r"X:\Projects\Demo" in profile, profile

        app = QApplication.instance() or QApplication([])
        dlg = MemoryManagerDialog(db)
        assert dlg.tabs.count() == 2
        assert dlg.pref_list_widget.count() == 2
        assert dlg.habit_list_widget.count() == 2
        dlg.close()
        app.processEvents()

        prefs = list_preferences(db_path=db)
        assert delete_preference_by_id(prefs[0]["id"], db_path=db)
        assert clear_all_preferences(db_path=db) == 1
        assert clear_activity_log(db_path=db) == 3
        print("[OK] Habit/preference learning & 2-tab MemoryManagerDialog passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_word_create_and_safety() -> None:
    from docx import Document
    from PySide6.QtWidgets import QApplication

    from app.engine import PendingConfirmation
    from app.tier2.word_editor import execute_word_create, prepare_word_create
    from app.ui.confirm_dialog import ConfirmationDialog

    tmp = _make_temp_dir("docx_create")
    try:
        prep = prepare_word_create(
            title="Project Executive Summary",
            content="First paragraph overview.\n\nSecond paragraph goals.",
            save_path=str(tmp),
        )
        assert prep["ok"], prep
        target = Path(prep["target_path"])
        assert target.parent == tmp and target.suffix == ".docx"

        # UI confirmation dialog supports word_create
        app = QApplication.instance() or QApplication([])
        pending = PendingConfirmation(
            id="wc1",
            kind="word_create",
            title="Confirm New Word Document",
            summary="Review content before creating .docx.",
            intent="CREATE_DOCUMENT",
            confidence=1.0,
            target_path=str(target),
            after_text=prep["content"],
            payload=prep,
        )
        dlg = ConfirmationDialog(pending)
        assert dlg.edited_after_text() == prep["content"]
        dlg.close()
        app.processEvents()

        msg = execute_word_create(prep, edited_content="Edited summary paragraph.", open_after=False)
        assert target.exists() and "Created Word document" in msg, msg

        parsed = Document(str(target))
        full_text = "\n".join(p.text for p in parsed.paragraphs)
        assert "Project Executive Summary" in full_text and "Edited summary paragraph." in full_text

        # Second creation with same title must NOT overwrite the first file
        prep2 = prepare_word_create("Project Executive Summary", "Another draft.", save_path=str(tmp))
        assert Path(prep2["target_path"]) != target, "Must auto-increment filename instead of overwriting!"
        print("[OK] Safe Word .docx creation + non-overwrite protection passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_screen_reader_cap_password_mask_and_vmware_ocr() -> None:
    from app.tier2 import screen_reader

    # Verify UI control inspection <= 50 cap and password masking
    class _FakeCtrl:
        def __init__(self, ctype: str, name: str, val: str, is_pw: bool = False):
            self.ControlTypeName = ctype
            self.Name = name
            self.AutomationId = name.lower()
            self.IsOffscreen = False
            self.IsEnabled = True
            self.IsPassword = is_pw
            self._val = val

        def GetValuePattern(self):
            class _VP:
                def __init__(self, v: str):
                    self.Value = v

            return _VP(self._val)

        def GetChildren(self):
            return []

    class _FakeRoot(_FakeCtrl):
        def __init__(self, children):
            super().__init__("WindowControl", "Fake App", "")
            self._children = children

        def GetChildren(self):
            return self._children

    fake_children = [_FakeCtrl("EditControl", "Password", "SuperSecret123!", is_pw=True)]
    for i in range(80):
        fake_children.append(_FakeCtrl("ButtonControl", f"Button {i}", ""))

    with (
        patch.object(screen_reader.win32gui, "IsWindow", return_value=True),
        patch.object(screen_reader.win32gui, "GetWindowText", return_value="Login App"),
        patch.object(screen_reader, "_get_process_name", return_value="notepad.exe"),
        patch.object(screen_reader.auto, "ControlFromHandle", return_value=_FakeRoot(fake_children)),
    ):
        ui_info = screen_reader.inspect_ui_elements(target_hwnd=12345, max_controls=100)
        assert ui_info["ok"] and len(ui_info["controls"]) <= 50, len(ui_info["controls"])
        pw_ctrl = ui_info["controls"][0]
        assert pw_ctrl["is_password"] is True
        assert pw_ctrl["value"] == "[REDACTED PASSWORD]"
        assert "SuperSecret123!" not in str(ui_info)

    # Verify vmware.exe bypasses UIA and triggers native Windows OCR
    with (
        patch.object(screen_reader.win32gui, "IsWindow", return_value=True),
        patch.object(screen_reader.win32gui, "GetWindowText", return_value="Ubuntu - VMware Workstation"),
        patch.object(screen_reader.win32gui, "GetClassName", return_value="VMUIFrame"),
        patch.object(screen_reader.win32gui, "GetWindowRect", return_value=(0, 0, 800, 600)),
        patch.object(screen_reader, "_get_process_name", return_value="vmware.exe"),
        patch.object(screen_reader, "ocr_image_windows", return_value="ubuntu@vm:~$ ls -la\nerror: port busy"),
        patch.object(screen_reader.auto, "ControlFromHandle", side_effect=AssertionError("Must NOT call UIA for vmware.exe!")),
    ):
        ctx = screen_reader.capture_window_context(target_hwnd=99999)
        assert ctx["ok"] and ctx["source"] == "vmware_ocr", ctx
        assert "error: port busy" in ctx["text"]
    print("[OK] Screen reader <=50 control cap, password masking, and VMware OCR bypass passed")


def test_vmware_linux_bridge_and_confirmation_gate() -> None:
    from app.engine import JudeEngine
    from app.tools.vmware_linux_tool import (
        execute_vmware_ssh_command,
        is_modifying_linux_command,
        vmware_list_vms,
    )

    assert not is_modifying_linux_command("ls -la /var/log")[0]
    assert not is_modifying_linux_command("df -h && free -m")[0]
    assert is_modifying_linux_command("sudo apt update")[0]
    assert is_modifying_linux_command("rm -rf /tmp/old_logs")[0]
    assert is_modifying_linux_command("echo test > /tmp/out.txt")[0]

    # Mocked vmrun output
    class _FakeProc:
        def __init__(self, returncode: int, stdout: str, stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_subproc(cmd, **_kwargs):
        if cmd[1] == "list":
            return _FakeProc(0, "Total running VMs: 1\nD:\\VMs\\Ubuntu\\Ubuntu.vmx\n")
        return _FakeProc(0, "192.168.120.132\n")

    with (
        patch("app.tools.vmware_linux_tool.find_vmrun_exe", return_value=Path(r"C:\VMware\vmrun.exe")),
        patch("app.tools.vmware_linux_tool.subprocess.run", side_effect=_fake_subproc),
    ):
        vms = vmware_list_vms()
        assert vms["ok"] and vms["vms"][0]["ip"] == "192.168.120.132", vms

    # Mocked paramiko key-based SSH execution
    class _FakeChan:
        def recv_exit_status(self) -> int:
            return 0

    class _FakeStream:
        def __init__(self, data: bytes):
            self._data = data
            self.channel = _FakeChan()

        def read(self) -> bytes:
            return self._data

    class _FakeSSH:
        def set_missing_host_key_policy(self, _policy) -> None:
            pass

        def connect(self, **kwargs) -> None:
            assert "password" not in kwargs or kwargs["password"] is None
            assert kwargs["hostname"] == "192.168.120.132"

        def exec_command(self, cmd: str, timeout: int = 20):
            return None, _FakeStream(b"Linux ubuntu 6.8.0"), _FakeStream(b"")

        def close(self) -> None:
            pass

    tmp = _make_temp_dir("vmware_gate")
    try:
        with patch("paramiko.SSHClient", return_value=_FakeSSH()):
            ssh_res = execute_vmware_ssh_command(
                "uname -a",
                host_ip="192.168.120.132",
                username="ubuntu",
            )
            assert ssh_res["ok"] and "Linux ubuntu" in ssh_res["stdout"], ssh_res

        # Verify sudo Linux command through JudeEngine stages PendingConfirmation instead of running immediately
        class _ToolOllama:
            def clear_history(self) -> None:
                pass

            def get_history(self) -> list[dict]:
                return []

            def record_turn(self, u: str, a: str) -> None:
                pass

            def ensure_server_running(self) -> bool:
                return True

            def chat_with_tools(self, messages, tools, temperature=0.2, max_tokens=600):
                return {
                    "ok": True,
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "vmware_run_linux_cmd",
                                    "arguments": {
                                        "command": "sudo apt update",
                                        "host_ip": "192.168.120.132",
                                    },
                                }
                            }
                        ],
                    },
                    "error": "",
                }

        engine = JudeEngine(
            memory_db_path=tmp / "memory.db",
            backups_dir=tmp / "backups",
            feedback_path=tmp / "feedback.jsonl",
            ollama=_ToolOllama(),
        )
        res = engine.process("Run sudo apt update on my Ubuntu VMware VM")
        assert res.tier_used == "pending" and res.pending is not None, res
        assert res.pending.kind == "vmware_cmd"
        assert "sudo apt update" in res.pending.payload["command"]
        print("[OK] VMware Linux bridge & sudo confirmation gate passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_hud_two_row_layout_and_tts_markdown_strip() -> None:
    from PySide6.QtWidgets import QApplication

    from app.ui.floating_widget import JudeFloatingWindow
    from app.voice.tts import normalize_for_speech

    sample = (
        "# Heading\n**Bold** `inline_code`\n```python\nprint('hi')\n```\n"
        "- Bullet item one for J.U.D.E."
    )
    spoken = normalize_for_speech(sample)
    assert "#" not in spoken and "*" not in spoken and "`" not in spoken, spoken
    assert "Jude" in spoken and "placed the code" in spoken.lower(), spoken

    app = QApplication.instance() or QApplication([])
    win = JudeFloatingWindow()
    win.show()
    win.toggle_hud()
    app.processEvents()
    for btn in (win.memory_btn, win.clear_btn, win.hud_mic_btn, win.hud_mute_btn, win.close_btn):
        assert btn.width() >= btn.minimumSizeHint().width(), f"Clipped HUD button: {btn.text()}"
    win._quit_app()
    win.close()
    app.processEvents()
    print("[OK] 2-row unclipped HUD layout & natural speech markdown filter passed")


def test_real_chat_fixes_routing_explorer_and_speech() -> None:
    from app.engine import JudeEngine
    from app.tier1.actions import open_target, plan_close_app
    from app.tools.registry import select_tools_for_turn
    from app.voice.listener import normalize_wake_word
    from app.voice.tts import normalize_for_speech

    # 1. Wake-word 'Jude' must NOT open workspace root when user asks to open Chrome/Word/WhatsApp
    ok_c, msg_c = open_target("Jude, open Chrome for me.", dry_run=True)
    assert ok_c and "Google Chrome" in msg_c, (ok_c, msg_c)
    ok_w, msg_w = open_target("Jude, open Word", dry_run=True)
    assert ok_w and "Microsoft Word" in msg_w, (ok_w, msg_w)
    ok_wa, msg_wa = open_target("Open WhatsApp, Jude.", dry_run=True)
    assert ok_wa and "WhatsApp" in msg_wa, (ok_wa, msg_wa)

    # 2. Explorer folder windows (CabinetWClass) can be closed safely, Shell_TrayWnd stays protected
    fake_windows = [
        {"hwnd": 101, "title": "Taskbar", "process": "explorer.exe", "class_name": "Shell_TrayWnd", "pid": 400},
        {"hwnd": 202, "title": "Downloads", "process": "explorer.exe", "class_name": "CabinetWClass", "pid": 400},
        {"hwnd": 303, "title": "WhatsApp", "process": "WhatsApp.exe", "class_name": "ApplicationFrameWindow", "pid": 500},
    ]
    with patch("app.tier1.actions.list_open_windows", return_value=fake_windows):
        plan_fm = plan_close_app("file manager")
        assert plan_fm["ok"] and plan_fm["hwnd"] == 202, plan_fm
        plan_wa = plan_close_app("WhatsApp")
        assert plan_wa["ok"] and plan_wa["hwnd"] == 303, plan_wa

    with patch("app.tier1.actions.list_open_windows", return_value=[fake_windows[0]]):
        blocked_tray = plan_close_app("Taskbar")
        assert not blocked_tray["ok"] and "protected system process" in blocked_tray["error"], blocked_tray

    # 3. 'Open WhatsApp' + 'Close it' + 'Who made you?' + Word edit routing
    tmp = _make_temp_dir("chat_fixes")
    try:
        engine = JudeEngine(
            memory_db_path=tmp / "memory.db",
            backups_dir=tmp / "backups",
            feedback_path=tmp / "feedback.jsonl",
        )
        who = engine.process("Jude, who made you?")
        assert who.tier_used == "tier1" and "J.U.D.E." in who.reply and "Alibaba" not in who.reply, who

        with patch("app.engine.open_target", return_value=(True, "Opened WhatsApp.")), patch(
            "app.tier1.actions.list_open_windows", return_value=fake_windows
        ):
            op = engine.process("Open WhatsApp, Jude.")
            assert op.tier_used == "tier1" and "WhatsApp" in op.reply, op
            cl = engine.process("Close it.")
            assert cl.tier_used == "pending" and cl.pending is not None and cl.pending.kind == "close_app", cl
            assert cl.pending.payload["hwnd"] == 303
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 4. Speech sanitization removes .undo.txt timestamps & backslashes
    spoken = normalize_for_speech("Updated `RnD DPG.docx` cleanly. Undo snapshot saved to `RnD DPG_20261126_013527.undo.txt`.")
    assert ".undo.txt" not in spoken and "20261126" not in spoken and ".docx" not in spoken, spoken
    assert "RnD DPG document" in spoken, spoken

    # 5. Whisper near-miss recovery + dynamic tool pruning
    assert normalize_wake_word("Jude, open Wordset for me.") == "Jude, open WhatsApp for me."
    core_names = {t["function"]["name"] for t in select_tools_for_turn("Open Chrome for me", "")}
    assert "vmware_run_linux_cmd" not in core_names and "create_word_doc" not in core_names, core_names
    word_names = {t["function"]["name"] for t in select_tools_for_turn("Read executive summary", "Open Word Doc: report.docx")}
    assert "read_open_word_doc" in word_names and "vmware_run_linux_cmd" not in word_names, word_names
    print("[OK] Real-world voice chat bug fixes & routing tests passed")


def test_step2_chrome_file_manager_terminal_minimize_and_colab_artifacts() -> None:
    import json
    from app.config import ROOT_DIR
    from app.engine import JudeEngine
    from app.tier1.actions import control_window, open_chrome, open_target, open_terminal
    from app.tools.registry import AGENT_TOOL_SCHEMAS

    # 1. File manager launches File Explorer cleanly
    ok_fm, msg_fm = open_target("Open File Manager", dry_run=True)
    assert ok_fm and "File Explorer" in msg_fm, (ok_fm, msg_fm)

    # 2. Chrome direct launch + profile matching from mock Local State (never redirects to Edge)
    tmp = _make_temp_dir("chrome_prof")
    try:
        local_state = tmp / "Local State"
        local_state.write_text(
            json.dumps(
                {
                    "profile": {
                        "info_cache": {
                            "Default": {"name": "Personal"},
                            "Profile 2": {"name": "Work"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        with patch("app.tier1.actions.find_chrome_exe", return_value=Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")):
            ok_ch, msg_ch = open_chrome(
                url="https://www.google.com/search?q=GitHub",
                profile="Work",
                dry_run=True,
                local_state_path=local_state,
            )
            assert ok_ch and "profile: Work" in msg_ch and "GitHub" in msg_ch, (ok_ch, msg_ch)

            ok_g, msg_g = open_chrome(profile="Guest", dry_run=True, local_state_path=local_state)
            assert ok_g and "profile: Guest" in msg_g, (ok_g, msg_g)

        # 3. Terminal non-existent path falls back safely to ROOT_DIR instead of looping
        ok_t, msg_t = open_terminal(r"C:\NonExistent\Fake\ISE", dry_run=True)
        assert ok_t and "default folder" in msg_t, (ok_t, msg_t)

        # 4. Window minimize + bare terminal deterministic routing in JudeEngine
        engine = JudeEngine(
            memory_db_path=tmp / "memory.db",
            backups_dir=tmp / "backups",
            feedback_path=tmp / "feedback.jsonl",
        )
        with patch("app.engine.open_terminal", return_value=(True, "Opened terminal at default.")) as mock_term:
            res_t = engine.process("Open the terminal")
            assert res_t.tier_used == "tier1" and mock_term.called, res_t

        with patch("app.tier1.actions.win32gui.ShowWindow") as mock_sw, patch(
            "app.tier1.actions.win32gui.IsWindow", return_value=True
        ), patch("app.tier1.actions.win32gui.GetWindowText", return_value="Chrome"):
            ok_min, msg_min = control_window("minimize", target_hwnd=555)
            assert ok_min and "Minimized" in msg_min and mock_sw.called, (ok_min, msg_min)

        with patch("app.engine.control_window", return_value=(True, "Minimized `Chrome`.")) as mock_ctrl:
            res_m = engine.process("Can you minimize?", target_hwnd=555)
            assert res_m.tier_used == "tier1" and "Minimized" in res_m.reply and mock_ctrl.called, res_m
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 5. Locked 19 tool schemas + QLoRA JSONL datasets + Colab notebook structure validation
    assert len(AGENT_TOOL_SCHEMAS) == 19, len(AGENT_TOOL_SCHEMAS)
    schema_names = {s["function"]["name"] for s in AGENT_TOOL_SCHEMAS}
    colab_dir = ROOT_DIR / "colab"
    schemas_file = colab_dir / "jude_tool_schemas.json"
    train_jsonl = colab_dir / "jude_agent_dataset.jsonl"
    eval_jsonl = colab_dir / "jude_agent_eval.jsonl"
    nb_file = colab_dir / "train_tier2_jude_lora.ipynb"

    assert schemas_file.exists() and train_jsonl.exists() and eval_jsonl.exists() and nb_file.exists()
    assert train_jsonl.stat().st_size < 1.5 * 1024 * 1024, "Keep train JSONL compact (<1.5 MB)"
    train_rows = [json.loads(line) for line in train_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    eval_rows = [json.loads(line) for line in eval_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(train_rows) >= 500 and len(eval_rows) == 90, (len(train_rows), len(eval_rows))
    for r in train_rows + eval_rows:
        for tn in r.get("tool_names", []):
            assert tn in schema_names, f"Unknown tool name {tn}"
        if r.get("expected_tool"):
            assert r["expected_tool"] in schema_names
    nb_data = json.loads(nb_file.read_text(encoding="utf-8"))
    assert nb_data.get("nbformat") == 4 and len(nb_data.get("cells", [])) >= 8
    print("[OK] Chrome profiles, File Manager, Terminal fallback, Minimize, and QLoRA dataset/notebook passed")


def test_screen_grounding_lead_in_and_hallucination_guard() -> None:
    from app.engine import JudeEngine
    from app.tools.registry import run_agent_turn

    tmp = _make_temp_dir("screen_leadin")
    try:
        fake_windows = [
            {"hwnd": 701, "title": "Projects", "process": "explorer.exe", "class_name": "CabinetWClass", "pid": 444}
        ]
        engine = JudeEngine(
            memory_db_path=tmp / "memory.db",
            backups_dir=tmp / "backups",
            feedback_path=tmp / "feedback.jsonl",
        )

        # 1. 'No, close my File Manager.' strips 'No, ' lead-in -> stages real close_app confirmation
        with patch("app.tier1.actions.list_open_windows", return_value=fake_windows):
            res_close = engine.process("No, close my File Manager.")
            assert res_close.tier_used == "pending" and res_close.pending is not None, res_close
            assert res_close.pending.kind == "close_app" and res_close.pending.payload["hwnd"] == 701, res_close.pending

        # 2. 'What is on my screen?' ALWAYS runs explain_screen(target_hwnd=...) instead of blind agent guessing
        with patch("app.engine.explain_screen", return_value="Active window is Projects in File Explorer.") as mock_es:
            res_scr = engine.process("What is on my screen?", target_hwnd=701)
            assert res_scr.tier_used == "tier2" and mock_es.called, res_scr
            assert mock_es.call_args.kwargs.get("target_hwnd") == 701
            assert "Projects" in res_scr.reply

        # 3. Unexecuted internal tool-name claim is caught when tools_used == []
        class _LyingOllama:
            def get_history(self) -> list[dict]:
                return []

            def record_turn(self, u: str, a: str) -> None:
                pass

            def chat_with_tools(self, messages, tools, temperature=0.2, max_tokens=600):
                return {
                    "ok": True,
                    "message": {
                        "role": "assistant",
                        "content": "I called window_control with action='minimize' for the active window.",
                    },
                    "error": "",
                }

        turn = run_agent_turn(
            user_text="Do something weird",
            ollama=_LyingOllama(),
            target_hwnd=701,
            memory_db_path=tmp / "memory.db",
            backups_dir=tmp / "backups",
        )
        assert "window_control" not in turn["reply"] and "didn't run any action" in turn["reply"], turn
        print("[OK] Real screen capture, lead-in stripping, and fake-tool hallucination guard passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bubble_ui_win_cmd_confirm_and_multilingual_voice() -> None:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.engine import EngineResponse, JudeEngine
    from app.tier1.actions import execute_windows_command, plan_windows_command
    from app.tier2.ollama_client import JUDE_SYSTEM_PROMPT
    from app.ui.confirm_dialog import ConfirmationDialog
    from app.ui.floating_widget import JudeFloatingWindow
    from app.voice import listener as listener_mod

    # 1. Windows CMD safety + Smart Mode classification
    bad_plan = plan_windows_command("format C: /q")
    assert not bad_plan["ok"] and "Refusing" in bad_plan["error"], bad_plan
    ro_plan = plan_windows_command("ipconfig /all")
    assert ro_plan["ok"] and ro_plan["open_visible_cmd"] is False, ro_plan
    vis_plan = plan_windows_command("npm run dev")
    assert vis_plan["ok"] and vis_plan["open_visible_cmd"] is True, vis_plan

    # 2. Safe captured CMD execution + JudeEngine confirmation gate
    cap_out = execute_windows_command({"command": "echo JUDE_CMD_OK", "open_visible_cmd": False})
    assert "JUDE_CMD_OK" in cap_out, cap_out

    tmp = _make_temp_dir("cmd_confirm")
    try:
        engine = JudeEngine(
            memory_db_path=tmp / "memory.db",
            backups_dir=tmp / "backups",
            feedback_path=tmp / "feedback.jsonl",
        )
        res = engine.process("Type ipconfig in my cmd")
        assert res.tier_used == "pending" and res.pending is not None, res
        assert res.pending.kind == "win_cmd" and res.pending.payload["open_visible_cmd"] is True

        app = QApplication.instance() or QApplication([])
        dlg = ConfirmationDialog(res.pending)
        assert dlg.after_edit is not None and "ipconfig" in dlg.after_edit.toPlainText()
        dlg.close()

        res.pending.payload["open_visible_cmd"] = False
        done = engine.confirm_pending(res.pending.id, edited_after_text="echo EDITED_OK")
        assert "EDITED_OK" in done.reply, done

        # 3. Floating speech bubble UI + single-click caption toggle + double-click HUD toggle
        win = JudeFloatingWindow(engine=engine)
        win.show()
        app.processEvents()
        assert win._hud_open is False and not win.speech_bubble.isVisible()

        win.submit_query("Hello Jude", from_voice=True)
        assert win._hud_open is False, "Voice query must NOT auto-open full chat card"
        assert win.speech_bubble.isVisible() and win.bubble_label.text() == "Thinking..."

        win._on_engine_response(
            EngineResponse(
                reply="All systems are ready.",
                spoken_summary="All systems are ready.",
                intent="GENERAL_CHAT",
                tier_used="tier1",
                confidence=1.0,
                gate_action="direct",
                router_ms=0.2,
                total_ms=1.0,
                badge_text="Tier 1 · Fast",
            )
        )
        app.processEvents()
        assert win._hud_open is False and win.speech_bubble.isVisible()
        assert "All systems are ready." in win.bubble_label.text()

        # Single click hides/shows caption bubble without opening HUD; double_clicked opens HUD
        win._on_avatar_single_click()
        assert not win.speech_bubble.isVisible() and win._hud_open is False
        win._on_avatar_single_click()
        assert win.speech_bubble.isVisible() and win._hud_open is False

        dbl = QMouseEvent(
            QMouseEvent.Type.MouseButtonDblClick,
            QPoint(20, 20),
            QPoint(20, 20),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        win.avatar.mouseDoubleClickEvent(dbl)
        app.processEvents()
        assert win._hud_open is True and not win.speech_bubble.isVisible()

        win._quit_app()
        win.close()
        app.processEvents()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 4. Multilingual Whisper small + English/Urdu/Roman Urdu LLM instruction check
    listener_src = Path(listener_mod.__file__).read_text(encoding="utf-8")
    assert '"small"' in listener_src and '"small.en"' not in listener_src
    assert '"language": "en"' not in listener_src and '"task": "transcribe"' in listener_src
    assert "Urdu" in JUDE_SYSTEM_PROMPT and "Roman Urdu" in JUDE_SYSTEM_PROMPT
    print("[OK] Bubble caption UI, double-click HUD, CMD confirm gate & multilingual Whisper passed")


def main() -> int:
    configure_d_drive_env()
    assert_no_c_drive_leakage()
    test_no_nagging_intent_confirm_and_agent_routing()
    test_habits_preferences_and_memory_dialog()
    test_word_create_and_safety()
    test_screen_reader_cap_password_mask_and_vmware_ocr()
    test_vmware_linux_bridge_and_confirmation_gate()
    test_hud_two_row_layout_and_tts_markdown_strip()
    test_real_chat_fixes_routing_explorer_and_speech()
    test_step2_chrome_file_manager_terminal_minimize_and_colab_artifacts()
    test_screen_grounding_lead_in_and_hallucination_guard()
    test_bubble_ui_win_cmd_confirm_and_multilingual_voice()
    print("\nALL J.U.D.E. V2 AGENT TESTS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
