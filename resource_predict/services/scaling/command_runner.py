from __future__ import annotations

import functools
import logging
import re
import subprocess
import time
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

# SSH StrictHostKeyChecking 允许值。
# - "yes"        : 每次连接必须已在 known_hosts 中，否则拒绝（最严格）
# - "accept-new" : 首次连接自动接受新指纹，后续连接校验已知指纹（推荐默认值，需 OpenSSH >= 7.6）
# - "no"         : 完全不校验（存在中间人攻击风险，仅用于已知安全的隔离网络）
_VALID_HOST_KEY_MODES = {"yes", "accept-new", "no"}
_DEFAULT_HOST_KEY_MODE = "accept-new"
# accept-new 是 OpenSSH 7.6 引入的；低于此版本的 SSH 不认识该选项
_ACCEPT_NEW_MIN_VERSION = (7, 6)


@functools.lru_cache(maxsize=1)
def _detect_ssh_version() -> Optional[tuple]:
    """检测本机 ssh 客户端版本，返回 (major, minor) 或 None（检测失败时）。

    结果通过 lru_cache 缓存，整个进程生命周期只执行一次。
    """
    try:
        proc = subprocess.run(
            ["ssh", "-V"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        # ssh -V 输出到 stderr，格式如 "OpenSSH_8.9p1 Ubuntu-3ubuntu0.14, ..."
        output = (proc.stderr or "") + (proc.stdout or "")
        match = re.search(r"OpenSSH_(\d+)\.(\d+)", output)
        if match:
            version = (int(match.group(1)), int(match.group(2)))
            logger.info("[command_runner] 检测到 SSH 版本: OpenSSH_%d.%d", *version)
            return version
        logger.warning("[command_runner] 无法从 ssh -V 输出中解析版本号: %r", output.strip())
    except FileNotFoundError:
        logger.warning("[command_runner] 未找到 ssh 可执行文件")
    except Exception as exc:
        logger.warning("[command_runner] 检测 SSH 版本失败: %s", exc)
    return None


def _ssh_supports_accept_new() -> bool:
    """判断本机 SSH 是否支持 StrictHostKeyChecking=accept-new（需 >= 7.6）。

    检测失败时保守返回 True，让 SSH 自己报错（而非我们静默降级后用户不知道）。
    """
    version = _detect_ssh_version()
    if version is None:
        # 无法检测时保守返回 True——如果 SSH 实际不支持，run_ssh_command 会
        # 拿到 stderr 报错，用户可以在集群配置里显式设置 strict_host_key_checking=no
        return True
    return version >= _ACCEPT_NEW_MIN_VERSION


def build_ssh_command(cluster_config: Dict[str, Any], remote_command: str) -> List[str]:
    host = str(cluster_config.get("control_host", "")).strip()
    user = str(cluster_config.get("ssh_user", "")).strip()
    port = str(cluster_config.get("ssh_port", "22")).strip() or "22"
    key = str(cluster_config.get("ssh_key", "")).strip()
    host_key_mode = _resolve_host_key_mode(cluster_config)
    target = f"{user}@{host}"
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"StrictHostKeyChecking={host_key_mode}",
        "-p",
        port,
    ]
    if key:
        cmd.extend(["-i", key])
    cmd.extend([target, remote_command])
    return cmd


def _resolve_host_key_mode(cluster_config: Dict[str, Any]) -> str:
    """解析集群配置中的 strict_host_key_checking，校验合法性后返回。

    默认值为 accept-new：首次连接自动接受服务器指纹，后续连接验证已知指纹，
    既防止中间人攻击，又不需要在每台机器上提前维护 known_hosts。

    若本机 SSH 版本低于 7.6（不支持 accept-new），自动降级为 no 并记录 WARNING。

    配置为 no 时记录 WARNING，提醒运维人员当前存在安全风险。
    """
    raw = str(cluster_config.get("strict_host_key_checking", "")).strip().lower()
    if not raw:
        # 用户未显式配置，使用默认值——但先检查 SSH 是否支持
        if _DEFAULT_HOST_KEY_MODE == "accept-new" and not _ssh_supports_accept_new():
            version = _detect_ssh_version()
            ver_str = f"OpenSSH_{version[0]}.{version[1]}" if version else "未知版本"
            logger.warning(
                "[command_runner] 本机 SSH 版本 (%s) 不支持 StrictHostKeyChecking=accept-new"
                "（需 OpenSSH >= 7.6），已自动降级为 no。"
                "建议升级 OpenSSH 或在集群配置中显式设置 strict_host_key_checking",
                ver_str,
            )
            return "no"
        return _DEFAULT_HOST_KEY_MODE
    if raw not in _VALID_HOST_KEY_MODES:
        logger.warning(
            "[command_runner] strict_host_key_checking 配置值无效: %r，已回退为 %s",
            raw,
            _DEFAULT_HOST_KEY_MODE,
        )
        # 回退时同样需要检查版本兼容性
        if _DEFAULT_HOST_KEY_MODE == "accept-new" and not _ssh_supports_accept_new():
            return "no"
        return _DEFAULT_HOST_KEY_MODE
    if raw == "no":
        logger.warning(
            "[command_runner] strict_host_key_checking=no：SSH 主机指纹校验已禁用，"
            "存在中间人攻击风险。生产环境建议改为 accept-new 或 yes"
        )
    return raw


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
        if item == "-i" and idx + 1 < len(cmd):
            redacted[idx + 1] = "***"
    return redacted
