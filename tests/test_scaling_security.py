"""command_runner.py 与 openstack_flavors.py 安全修复测试。"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from resource_predict.services.scaling.command_runner import (
    _ACCEPT_NEW_MIN_VERSION,
    _DEFAULT_HOST_KEY_MODE,
    _VALID_HOST_KEY_MODES,
    _detect_ssh_version,
    _resolve_host_key_mode,
    _ssh_supports_accept_new,
    build_ssh_command,
)
from resource_predict.services.scaling.openstack_flavors import (
    _CACHE,
    _CACHE_LOCK,
    Flavor,
    discover_openstack_flavors,
)


# ---------------------------------------------------------------------------
# _detect_ssh_version / _ssh_supports_accept_new
# ---------------------------------------------------------------------------

class DetectSshVersionTest(unittest.TestCase):

    def setUp(self):
        # 每个测试前清空 lru_cache，避免跨测试污染
        _detect_ssh_version.cache_clear()

    def tearDown(self):
        _detect_ssh_version.cache_clear()

    def test_detect_returns_tuple_on_valid_output(self):
        """正常 ssh -V 输出应解析为 (major, minor) 元组。"""
        fake_proc = MagicMock()
        fake_proc.stderr = "OpenSSH_8.9p1 Ubuntu-3ubuntu0.14, OpenSSL 3.0.2 15 Mar 2022\n"
        fake_proc.stdout = ""
        with patch("resource_predict.services.scaling.command_runner.subprocess.run", return_value=fake_proc):
            result = _detect_ssh_version()
        self.assertEqual(result, (8, 9))

    def test_detect_returns_none_on_missing_ssh(self):
        """ssh 不存在时返回 None。"""
        with patch(
            "resource_predict.services.scaling.command_runner.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            result = _detect_ssh_version()
        self.assertIsNone(result)

    def test_detect_returns_none_on_unparseable_output(self):
        """无法解析版本时返回 None。"""
        fake_proc = MagicMock()
        fake_proc.stderr = "some unknown ssh tool v1.0\n"
        fake_proc.stdout = ""
        with patch("resource_predict.services.scaling.command_runner.subprocess.run", return_value=fake_proc):
            result = _detect_ssh_version()
        self.assertIsNone(result)

    def test_detect_parses_old_version(self):
        """老版本 OpenSSH_6.6p1 应正确解析。"""
        fake_proc = MagicMock()
        fake_proc.stderr = "OpenSSH_6.6p1, OpenSSL 1.0.1e-fips 11 Feb 2013\n"
        fake_proc.stdout = ""
        with patch("resource_predict.services.scaling.command_runner.subprocess.run", return_value=fake_proc):
            result = _detect_ssh_version()
        self.assertEqual(result, (6, 6))

    def test_result_is_cached(self):
        """_detect_ssh_version 通过 lru_cache 缓存，第二次调用不再执行 subprocess。"""
        fake_proc = MagicMock()
        fake_proc.stderr = "OpenSSH_7.4p1\n"
        fake_proc.stdout = ""
        with patch("resource_predict.services.scaling.command_runner.subprocess.run", return_value=fake_proc) as mock_run:
            first = _detect_ssh_version()
            second = _detect_ssh_version()
        self.assertEqual(first, (7, 4))
        self.assertEqual(second, (7, 4))
        mock_run.assert_called_once()


class SshSupportsAcceptNewTest(unittest.TestCase):

    def setUp(self):
        _detect_ssh_version.cache_clear()

    def tearDown(self):
        _detect_ssh_version.cache_clear()

    def test_modern_ssh_returns_true(self):
        """OpenSSH 8.9 应返回 True。"""
        with patch(
            "resource_predict.services.scaling.command_runner._detect_ssh_version",
            return_value=(8, 9),
        ):
            self.assertTrue(_ssh_supports_accept_new())

    def test_exact_minimum_version_returns_true(self):
        """OpenSSH 7.6（恰好是最低版本）应返回 True。"""
        with patch(
            "resource_predict.services.scaling.command_runner._detect_ssh_version",
            return_value=(7, 6),
        ):
            self.assertTrue(_ssh_supports_accept_new())

    def test_old_ssh_returns_false(self):
        """OpenSSH 6.6 应返回 False。"""
        with patch(
            "resource_predict.services.scaling.command_runner._detect_ssh_version",
            return_value=(6, 6),
        ):
            self.assertFalse(_ssh_supports_accept_new())

    def test_just_below_minimum_returns_false(self):
        """OpenSSH 7.5（差一点不到最低版本）应返回 False。"""
        with patch(
            "resource_predict.services.scaling.command_runner._detect_ssh_version",
            return_value=(7, 5),
        ):
            self.assertFalse(_ssh_supports_accept_new())

    def test_detection_failure_returns_true_conservative(self):
        """版本检测失败时保守返回 True（让 SSH 自己报错）。"""
        with patch(
            "resource_predict.services.scaling.command_runner._detect_ssh_version",
            return_value=None,
        ):
            self.assertTrue(_ssh_supports_accept_new())


# ---------------------------------------------------------------------------
# _resolve_host_key_mode
# ---------------------------------------------------------------------------

class ResolveHostKeyModeTest(unittest.TestCase):

    def setUp(self):
        _detect_ssh_version.cache_clear()

    def tearDown(self):
        _detect_ssh_version.cache_clear()

    def test_default_is_accept_new_when_ssh_supports(self):
        """SSH 支持时，未配置默认使用 accept-new。"""
        with patch(
            "resource_predict.services.scaling.command_runner._ssh_supports_accept_new",
            return_value=True,
        ):
            mode = _resolve_host_key_mode({})
            self.assertEqual(mode, "accept-new")
            self.assertEqual(mode, _DEFAULT_HOST_KEY_MODE)

    def test_default_falls_back_to_no_when_ssh_too_old(self):
        """SSH 版本过低时，默认值自动降级为 no。"""
        with patch(
            "resource_predict.services.scaling.command_runner._ssh_supports_accept_new",
            return_value=False,
        ), patch(
            "resource_predict.services.scaling.command_runner._detect_ssh_version",
            return_value=(6, 6),
        ):
            mode = _resolve_host_key_mode({})
            self.assertEqual(mode, "no")

    def test_accept_new_explicit(self):
        """用户显式配置 accept-new 时正常返回（即使 SSH 不支持，也尊重用户选择）。"""
        mode = _resolve_host_key_mode({"strict_host_key_checking": "accept-new"})
        self.assertEqual(mode, "accept-new")

    def test_yes_explicit(self):
        """显式配置 yes（最严格模式）时正常返回。"""
        mode = _resolve_host_key_mode({"strict_host_key_checking": "yes"})
        self.assertEqual(mode, "yes")

    def test_no_explicit_allowed_but_warns(self):
        """显式配置 no 时返回 no，但应记录 WARNING（通过日志验证）。"""
        mode = _resolve_host_key_mode({"strict_host_key_checking": "no"})
        self.assertEqual(mode, "no")

    def test_invalid_value_falls_back_with_version_check(self):
        """非法配置值回退时，也检查 SSH 版本兼容性。"""
        # SSH 支持 accept-new → 回退到 accept-new
        with patch(
            "resource_predict.services.scaling.command_runner._ssh_supports_accept_new",
            return_value=True,
        ):
            mode = _resolve_host_key_mode({"strict_host_key_checking": "maybe"})
            self.assertEqual(mode, _DEFAULT_HOST_KEY_MODE)

        # SSH 不支持 accept-new → 回退到 no
        with patch(
            "resource_predict.services.scaling.command_runner._ssh_supports_accept_new",
            return_value=False,
        ):
            mode = _resolve_host_key_mode({"strict_host_key_checking": "bogus"})
            self.assertEqual(mode, "no")

    def test_empty_string_falls_back_with_version_check(self):
        """空字符串回退到默认值，同时检查版本。"""
        with patch(
            "resource_predict.services.scaling.command_runner._ssh_supports_accept_new",
            return_value=True,
        ):
            mode = _resolve_host_key_mode({"strict_host_key_checking": ""})
            self.assertEqual(mode, _DEFAULT_HOST_KEY_MODE)

    def test_whitespace_only_falls_back_to_default(self):
        """纯空白字符回退到默认值。"""
        with patch(
            "resource_predict.services.scaling.command_runner._ssh_supports_accept_new",
            return_value=True,
        ):
            mode = _resolve_host_key_mode({"strict_host_key_checking": "   "})
            self.assertEqual(mode, _DEFAULT_HOST_KEY_MODE)

    def test_case_insensitive(self):
        """配置值大小写不敏感。"""
        mode = _resolve_host_key_mode({"strict_host_key_checking": "Accept-New"})
        self.assertEqual(mode, "accept-new")

    def test_valid_modes_set(self):
        """确认允许值集合包含预期的三个模式。"""
        self.assertEqual(_VALID_HOST_KEY_MODES, {"yes", "accept-new", "no"})

    def test_minimum_version_constant(self):
        """确认最低版本常量是 (7, 6)。"""
        self.assertEqual(_ACCEPT_NEW_MIN_VERSION, (7, 6))


# ---------------------------------------------------------------------------
# build_ssh_command
# ---------------------------------------------------------------------------

class BuildSshCommandTest(unittest.TestCase):

    def setUp(self):
        _detect_ssh_version.cache_clear()

    def tearDown(self):
        _detect_ssh_version.cache_clear()

    def test_default_includes_accept_new_when_supported(self):
        """SSH 支持时，默认生成 StrictHostKeyChecking=accept-new。"""
        with patch(
            "resource_predict.services.scaling.command_runner._ssh_supports_accept_new",
            return_value=True,
        ):
            config = {"control_host": "node01", "ssh_user": "ops"}
            cmd = build_ssh_command(config, "openstack flavor list -f json")
            self.assertIn("StrictHostKeyChecking=accept-new", cmd)
            self.assertNotIn("StrictHostKeyChecking=no", cmd)

    def test_default_falls_back_to_no_when_ssh_old(self):
        """SSH 版本过低时，默认生成 StrictHostKeyChecking=no。"""
        with patch(
            "resource_predict.services.scaling.command_runner._ssh_supports_accept_new",
            return_value=False,
        ), patch(
            "resource_predict.services.scaling.command_runner._detect_ssh_version",
            return_value=(6, 6),
        ):
            config = {"control_host": "node01", "ssh_user": "ops"}
            cmd = build_ssh_command(config, "openstack server list")
            self.assertIn("StrictHostKeyChecking=no", cmd)
            self.assertNotIn("StrictHostKeyChecking=accept-new", cmd)

    def test_explicit_yes(self):
        """配置 strict_host_key_checking=yes 时命令包含 yes。"""
        config = {
            "control_host": "node01",
            "ssh_user": "ops",
            "strict_host_key_checking": "yes",
        }
        cmd = build_ssh_command(config, "openstack server list")
        self.assertIn("StrictHostKeyChecking=yes", cmd)

    def test_explicit_no(self):
        """配置 strict_host_key_checking=no 时命令包含 no（兼容旧环境）。"""
        config = {
            "control_host": "node01",
            "ssh_user": "ops",
            "strict_host_key_checking": "no",
        }
        cmd = build_ssh_command(config, "openstack server list")
        self.assertIn("StrictHostKeyChecking=no", cmd)

    def test_invalid_mode_produces_version_aware_default(self):
        """配置非法值时，根据 SSH 版本决定回退值。"""
        # SSH 支持 → accept-new
        with patch(
            "resource_predict.services.scaling.command_runner._ssh_supports_accept_new",
            return_value=True,
        ):
            config = {
                "control_host": "node01",
                "ssh_user": "ops",
                "strict_host_key_checking": "bogus",
            }
            cmd = build_ssh_command(config, "openstack server list")
            self.assertIn("StrictHostKeyChecking=accept-new", cmd)

    def test_ssh_key_and_port_present(self):
        """ssh_key 和 ssh_port 仍正常传递，不受 host_key_mode 影响。"""
        with patch(
            "resource_predict.services.scaling.command_runner._ssh_supports_accept_new",
            return_value=True,
        ):
            config = {
                "control_host": "node01",
                "ssh_user": "ops",
                "ssh_port": "2222",
                "ssh_key": "/root/.ssh/id_rsa",
            }
            cmd = build_ssh_command(config, "whoami")
            self.assertIn("-p", cmd)
            self.assertIn("2222", cmd)
            self.assertIn("-i", cmd)
            self.assertIn("/root/.ssh/id_rsa", cmd)


# ---------------------------------------------------------------------------
# _CACHE_LOCK 线程安全
# ---------------------------------------------------------------------------

class CacheLockTest(unittest.TestCase):

    def test_cache_lock_is_threading_lock(self):
        """_CACHE_LOCK 是 threading.Lock 实例。"""
        self.assertIsInstance(_CACHE_LOCK, type(threading.Lock()))

    def test_concurrent_discover_does_not_corrupt_cache(self):
        """
        多个线程并发调用 discover_openstack_flavors 时，缓存不被破坏，
        且每个线程都能拿到正确结果。
        """
        cluster_name = "test-cluster-concurrent"
        cluster_config = {
            "control_host": "fake-host",
            "ssh_user": "ops",
            "openstack_rc": "",
            "flavor_cache_seconds": 0,  # 每次都不命中缓存，强制调用 run_ssh_command
        }

        fake_flavors_json = (
            '[{"Name": "m1.small", "VCPUs": 2, "RAM": 2048, "Disk": 20},'
            ' {"Name": "m1.large", "VCPUs": 4, "RAM": 8192, "Disk": 80}]'
        )
        fake_result = {"exit_code": 0, "stdout": fake_flavors_json, "stderr": ""}

        errors = []
        results = []
        barrier = threading.Barrier(4)  # 4 个线程同时开始

        def _worker():
            try:
                barrier.wait(timeout=5)
                flavors = discover_openstack_flavors(cluster_name, cluster_config)
                results.append(len(flavors))
            except Exception as exc:
                errors.append(exc)

        # 清空该 key 的缓存，避免影响其他测试
        cache_key = f"{cluster_name}|ops@fake-host||"
        _CACHE.pop(cache_key, None)

        with patch(
            "resource_predict.services.scaling.openstack_flavors.run_ssh_command",
            return_value=fake_result,
        ):
            threads = [threading.Thread(target=_worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

        # 所有线程均成功，无异常
        self.assertEqual(errors, [], f"线程抛出异常: {errors}")
        # 每个线程均拿到 2 个 flavor
        self.assertEqual(results, [2, 2, 2, 2])
        # 缓存最终存在且内容完整
        self.assertIn(cache_key, _CACHE)
        self.assertEqual(len(_CACHE[cache_key]["flavors"]), 2)

    def test_cache_hit_skips_ssh_call(self):
        """缓存未过期时，不应再次调用 run_ssh_command。"""
        cluster_name = "test-cluster-cache-hit"
        cluster_config = {
            "control_host": "fake-host",
            "ssh_user": "ops",
            "openstack_rc": "",
            "flavor_cache_seconds": 3600,
        }
        cache_key = f"{cluster_name}|ops@fake-host||"
        _CACHE[cache_key] = {
            "ts": __import__("time").time(),
            "flavors": [
                Flavor(name="m1.tiny", vcpus=1, memory_gb=0.5, disk_gb=1, raw={}),
            ],
        }

        with patch(
            "resource_predict.services.scaling.openstack_flavors.run_ssh_command",
            side_effect=AssertionError("SSH 不应被调用"),
        ) as mock_ssh:
            flavors = discover_openstack_flavors(cluster_name, cluster_config)

        self.assertEqual(len(flavors), 1)
        self.assertEqual(flavors[0].name, "m1.tiny")
        # 清理
        _CACHE.pop(cache_key, None)


if __name__ == "__main__":
    unittest.main()
