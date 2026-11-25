from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from app.config import VMWARE_SSH_CONFIG_PATH

_COMMON_VMRUN_PATHS = (
    Path(r"C:\Program Files\VMware\VMware Workstation\vmrun.exe"),
    Path(r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"),
    Path(r"C:\Program Files\VMware\VMware VIX\vmrun.exe"),
    Path(r"C:\Program Files (x86)\VMware\VMware VIX\vmrun.exe"),
)

_MODIFYING_CMDS = {
    "sudo",
    "su",
    "rm",
    "mv",
    "cp",
    "dd",
    "mkfs",
    "fdisk",
    "parted",
    "chmod",
    "chown",
    "chgrp",
    "apt",
    "apt-get",
    "dpkg",
    "dnf",
    "yum",
    "pacman",
    "snap",
    "systemctl",
    "service",
    "kill",
    "pkill",
    "killall",
    "reboot",
    "shutdown",
    "poweroff",
    "halt",
    "init",
    "tee",
    "touch",
    "mkdir",
    "rmdir",
    "truncate",
    "ln",
    "install",
    "useradd",
    "usermod",
    "userdel",
    "passwd",
}

_MODIFYING_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:^|\s)(?:>|>>|1>|2>)\s*\S+"), "Shell output redirection writes or overwrites files."),
    (re.compile(r"\bsed\s+-[a-zA-Z]*i\b"), "In-place file editing with sed -i."),
    (re.compile(r"\b(?:pip|pip3)\s+(?:install|uninstall)\b"), "Python package installation or removal."),
    (re.compile(r"\bnpm\s+(?:install|uninstall|update)\b"), "Node package modification."),
    (re.compile(r"\bgit\s+(?:commit|push|reset|clean|checkout\s+--|merge|rebase)\b"), "Git repository state change."),
    (re.compile(r"\bdocker\s+(?:rm|rmdir|stop|kill|run|exec|prune|build)\b"), "Docker container/image mutation."),
]


def find_vmrun_exe() -> Path | None:
    """Locate VMware Workstation's vmrun.exe cleanly without hardcoded private state."""
    on_path = shutil.which("vmrun")
    if on_path:
        p = Path(on_path)
        if p.exists():
            return p
    for cand in _COMMON_VMRUN_PATHS:
        if cand.exists():
            return cand
    return None


def vmware_list_vms() -> dict:
    """List active VMware VMs via `vmrun list` and resolve guest IPs via open-vm-tools."""
    vmrun = find_vmrun_exe()
    if vmrun is None:
        return {
            "ok": False,
            "vmrun_found": False,
            "vms": [],
            "error": "VMware vmrun.exe was not found on PATH or standard VMware Workstation directories.",
        }

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [str(vmrun), "list"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=creation_flags,
        )
    except Exception as exc:
        return {"ok": False, "vmrun_found": True, "vms": [], "error": f"Failed running vmrun list: {exc}"}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "vmrun list returned an error").strip()
        return {"ok": False, "vmrun_found": True, "vms": [], "error": err}

    vmx_paths: list[str] = []
    for raw_line in (proc.stdout or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("total running vms"):
            continue
        if line.lower().endswith(".vmx") or Path(line).suffix.lower() == ".vmx":
            vmx_paths.append(line)

    vms: list[dict] = []
    for vmx in vmx_paths:
        ip_addr = ""
        ip_err = ""
        for args in ([str(vmrun), "getGuestIPAddress", vmx], [str(vmrun), "getGuestIPAddress", vmx, "-wait"]):
            try:
                ip_proc = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=8,
                    creationflags=creation_flags,
                )
                out = (ip_proc.stdout or "").strip()
                if ip_proc.returncode == 0 and re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", out):
                    ip_addr = out
                    ip_err = ""
                    break
                ip_err = (ip_proc.stderr or out or "open-vm-tools did not report an IP").strip()
            except subprocess.TimeoutExpired:
                ip_err = "Timed out waiting for guest IP from open-vm-tools."
            except Exception as exc:
                ip_err = str(exc)

        vms.append(
            {
                "name": Path(vmx).stem,
                "vmx_path": vmx,
                "ip": ip_addr,
                "ip_error": "" if ip_addr else ip_err,
            }
        )

    return {"ok": True, "vmrun_found": True, "vms": vms}


def load_vmware_ssh_config(config_path: Path = VMWARE_SSH_CONFIG_PATH) -> dict:
    """Load local key-based SSH defaults from gitignored data/vmware_ssh.json or environment."""
    cfg = {
        "username": os.environ.get("JUDE_VM_SSH_USER", "").strip(),
        "key_path": os.environ.get("JUDE_VM_SSH_KEY", "").strip(),
        "port": 22,
        "timeout": 20,
    }
    p = Path(config_path)
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                if loaded.get("username") and not cfg["username"]:
                    cfg["username"] = str(loaded["username"]).strip()
                if loaded.get("key_path") and not cfg["key_path"]:
                    cfg["key_path"] = str(loaded["key_path"]).strip()
                if loaded.get("port"):
                    cfg["port"] = int(loaded["port"])
                if loaded.get("timeout"):
                    cfg["timeout"] = int(loaded["timeout"])
        except Exception:
            pass

    if not cfg["key_path"]:
        try:
            ssh_dir = Path.home() / ".ssh"
            for name in ("id_ed25519", "id_rsa"):
                cand = ssh_dir / name
                try:
                    if cand.exists():
                        cfg["key_path"] = str(cand)
                        break
                except OSError:
                    continue
        except OSError:
            pass
    return cfg


def is_modifying_linux_command(command: str) -> tuple[bool, str]:
    """Return (True, reason) if a Linux shell command uses sudo or modifies files/system state."""
    cmd = " ".join((command or "").strip().split())
    if not cmd:
        return False, ""

    for pat, reason in _MODIFYING_PATTERNS:
        if pat.search(cmd):
            return True, reason

    # Split chained segments safely (&&, ||, ;, |)
    segments = re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            tokens = shlex.split(seg, posix=True)
        except ValueError:
            tokens = seg.split()
        if not tokens:
            continue

        # Skip leading environment assignments like FOO=bar cmd
        idx = 0
        while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
            idx += 1
        if idx >= len(tokens):
            continue

        head = Path(tokens[idx]).name.lower()
        if head in _MODIFYING_CMDS:
            if head in {"sudo", "su"}:
                return True, f"Elevated privileges requested via `{head}`."
            return True, f"Potentially state-modifying Linux command `{head}`."

    return False, ""


def execute_vmware_ssh_command(
    command: str,
    host_ip: str = "",
    vmx_path: str = "",
    username: str = "",
    key_path: str = "",
    port: int = 22,
    timeout: int = 20,
    config_path: Path = VMWARE_SSH_CONFIG_PATH,
) -> dict:
    """Run a Linux shell command inside a running VMware VM using key-based paramiko SSH."""
    import paramiko

    cmd = (command or "").strip()
    if not cmd:
        return {"ok": False, "error": "Linux command cannot be empty."}

    resolved_ip = host_ip.strip()
    resolved_vm = Path(vmx_path).stem if vmx_path else ""
    if not resolved_ip:
        listing = vmware_list_vms()
        if not listing.get("ok"):
            return {"ok": False, "error": listing.get("error", "Could not query VMware VMs.")}
        vms = listing.get("vms") or []
        if not vms:
            return {"ok": False, "error": "No running VMware virtual machines were detected."}
        chosen = None
        if vmx_path:
            for item in vms:
                if vmx_path.lower() in item["vmx_path"].lower():
                    chosen = item
                    break
        chosen = chosen or vms[0]
        resolved_ip = chosen.get("ip", "")
        resolved_vm = chosen.get("name", "")
        if not resolved_ip:
            return {
                "ok": False,
                "error": f"VM `{resolved_vm}` is running, but open-vm-tools did not report an IP ({chosen.get('ip_error')}).",
            }

    cfg = load_vmware_ssh_config(config_path=config_path)
    ssh_user = (username or cfg.get("username") or "").strip()
    ssh_key = (key_path or cfg.get("key_path") or "").strip()
    ssh_port = int(port or cfg.get("port") or 22)
    ssh_timeout = int(timeout or cfg.get("timeout") or 20)

    if not ssh_user:
        return {
            "ok": False,
            "host_ip": resolved_ip,
            "vm_name": resolved_vm,
            "error": (
                "No VMware SSH username configured. Set `JUDE_VM_SSH_USER` or create local "
                "`data/vmware_ssh.json` with `{\"username\": \"your_linux_user\", \"key_path\": \"...\"}`."
            ),
        }

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict = {
        "hostname": resolved_ip,
        "port": ssh_port,
        "username": ssh_user,
        "timeout": min(ssh_timeout, 15),
        "allow_agent": True,
        "look_for_keys": True,
    }
    if ssh_key:
        try:
            if Path(ssh_key).exists():
                connect_kwargs["key_filename"] = ssh_key
        except OSError:
            pass

    try:
        client.connect(**connect_kwargs)
        _, stdout_io, stderr_io = client.exec_command(cmd, timeout=ssh_timeout)
        exit_code = stdout_io.channel.recv_exit_status()
        out_txt = stdout_io.read().decode("utf-8", errors="replace").strip()[:2800]
        err_txt = stderr_io.read().decode("utf-8", errors="replace").strip()[:1200]
        return {
            "ok": exit_code == 0,
            "vm_name": resolved_vm,
            "host_ip": resolved_ip,
            "exit_code": int(exit_code),
            "stdout": out_txt,
            "stderr": err_txt,
        }
    except Exception as exc:
        return {
            "ok": False,
            "vm_name": resolved_vm,
            "host_ip": resolved_ip,
            "error": (
                f"Key-based SSH to {ssh_user}@{resolved_ip}:{ssh_port} failed: {exc}. "
                "Ensure sshd is running inside the VM and your public SSH key is in ~/.ssh/authorized_keys."
            ),
        }
    finally:
        client.close()
