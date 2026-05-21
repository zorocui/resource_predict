from __future__ import annotations

import subprocess
import time
from typing import Any, Dict, List


def build_ssh_command(cluster_config: Dict[str, Any], remote_command: str) -> List[str]:
    host = str(cluster_config.get("control_host", "")).strip()
    user = str(cluster_config.get("ssh_user", "")).strip()
    port = str(cluster_config.get("ssh_port", "22")).strip() or "22"
    key = str(cluster_config.get("ssh_key", "")).strip()
    target = f"{user}@{host}"
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-p",
        port,
    ]
    if key:
        cmd.extend(["-i", key])
    cmd.extend([target, remote_command])
    return cmd


def run_ssh_command(
    cluster_config: Dict[str, Any],
    remote_command: str,
    *,
    timeout_seconds: int = 300,
) -> Dict[str, Any]:
    started = time.time()
    cmd = build_ssh_command(cluster_config, remote_command)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "command": _redact_command_for_log(cmd),
            "exit_code": proc.returncode,
            "stdout": proc.stdout[-8000:],
            "stderr": proc.stderr[-8000:],
            "duration_seconds": round(time.time() - started, 3),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": _redact_command_for_log(cmd),
            "exit_code": 124,
            "stdout": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
            "stderr": "SSH command timed out",
            "duration_seconds": round(time.time() - started, 3),
        }


def _redact_command_for_log(cmd: List[str]) -> List[str]:
    redacted = list(cmd)
    for idx, item in enumerate(redacted):
        if item == "-i" and idx + 1 < len(redacted):
            redacted[idx + 1] = "***"
    return redacted
