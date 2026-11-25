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


def main() -> int:
    configure_d_drive_env()
    assert_no_c_drive_leakage()
    test_no_nagging_intent_confirm_and_agent_routing()
    test_habits_preferences_and_memory_dialog()
    test_word_create_and_safety()
    test_screen_reader_cap_password_mask_and_vmware_ocr()
    test_vmware_linux_bridge_and_confirmation_gate()
    test_hud_two_row_layout_and_tts_markdown_strip()
    print("\nALL J.U.D.E. V2 AGENT TESTS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
