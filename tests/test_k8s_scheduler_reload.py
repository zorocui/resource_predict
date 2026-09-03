"""K8S 后台调度器对配置保存的响应：只重读配置，不额外触发拉取。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from resource_predict.services import k8s_ingest


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeTime:
    def __init__(self, clock: FakeClock):
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock.now

    def perf_counter(self) -> float:
        return self._clock.now

    def sleep(self, seconds: float) -> None:
        self._clock.advance(seconds)


class ClockStopEvent:
    """假时钟越过 stop_at 后视为已请求停止，让循环自然退出。"""

    def __init__(self, clock: FakeClock, stop_at: float):
        self._clock = clock
        self._stop_at = stop_at
        self.waits: List[Optional[float]] = []

    def is_set(self) -> bool:
        return self._clock.now >= self._stop_at

    def wait(self, seconds: Optional[float] = None) -> bool:
        self.waits.append(seconds)
        if seconds:
            self._clock.advance(seconds)
        return self.is_set()


class ScriptedReloadEvent:
    """按脚本推进假时钟：woke=True 模拟配置保存唤醒，False 模拟睡满超时。"""

    def __init__(self, clock: FakeClock, cfg: SimpleNamespace, script: List[Tuple]):
        self._clock = clock
        self._cfg = cfg
        self._script = list(script)
        self.timeouts: List[Optional[float]] = []
        self.cleared = 0

    def wait(self, timeout: Optional[float] = None) -> bool:
        self.timeouts.append(timeout)
        if not self._script:
            raise AssertionError(f"reload event waited beyond script: {self.timeouts}")
        advance, woke, cfg_updates = self._script.pop(0)
        self._clock.advance(advance)
        for key, value in (cfg_updates or {}).items():
            setattr(self._cfg, key, value)
        return bool(woke)

    def clear(self) -> None:
        self.cleared += 1

    def set(self) -> None:
        pass


def run_loop(
    *,
    script: List[Tuple],
    interval_minutes: int = 60,
    enabled: bool = True,
    stop_at: float = 50000.0,
    upsert_raises: bool = False,
    upsert_seconds: float = 0.0,
) -> Tuple[FakeClock, SimpleNamespace, List[Tuple[float, str]], ScriptedReloadEvent]:
    clock = FakeClock()
    cfg = SimpleNamespace(
        scheduled_update_enabled=enabled,
        scheduled_update_interval_minutes=interval_minutes,
    )
    fetches: List[Tuple[float, str]] = []

    def fake_upsert(**kwargs: Any) -> Dict[str, Any]:
        # 先记录开始时刻，再推进假时钟模拟拉取耗时。
        fetches.append((clock.now, kwargs.get("trigger_source", "?")))
        if upsert_raises:
            raise RuntimeError("simulated fetch failure")
        if upsert_seconds:
            clock.advance(upsert_seconds)
        return {"success": True, "status": "success"}

    reload_event = ScriptedReloadEvent(clock, cfg, script)
    stop_event = ClockStopEvent(clock, stop_at)
    with patch.object(k8s_ingest, "settings", SimpleNamespace(k8s_prometheus=cfg)):
        with patch.object(k8s_ingest, "time", FakeTime(clock)):
            with patch.object(k8s_ingest, "_k8s_stop_event", stop_event):
                with patch.object(k8s_ingest, "_k8s_reload_event", reload_event):
                    with patch.object(k8s_ingest, "run_k8s_prometheus_upsert", fake_upsert):
                        k8s_ingest._k8s_scheduler_loop(interval_minutes * 60.0, 0.0)
    return clock, cfg, fetches, reload_event


class K8SSchedulerReloadTest(unittest.TestCase):
    def test_config_save_wakes_loop_without_extra_fetch(self):
        """保存配置把线程从 60 分钟等待中唤醒，但不应该立刻拉取。"""
        script = [
            (5.0, True, None),        # t=1005 保存配置，提前唤醒
            (3596.0, False, None),    # 睡满剩余时间，t=4601 正常到点
            (100000.0, False, None),  # 越过 stop_at，循环退出
        ]
        _clock, _cfg, fetches, reload_event = run_loop(script=script, interval_minutes=60)

        self.assertEqual([item[1] for item in fetches], ["scheduled_startup", "scheduled"])
        # 关键断言：第二轮发生在原定到期时刻 4601，而不是配置保存的 1005。
        self.assertEqual([item[0] for item in fetches], [1000.0, 4601.0])
        # 被提前唤醒后等待的是剩余 3595 秒，说明周期没有被重置也没有被提前。
        self.assertEqual(reload_event.timeouts, [3600.0, 3595.0, 3600.0])

    def test_lengthening_interval_applies_without_immediate_fetch(self):
        """保存时把周期从 60 分钟改成 120 分钟，应重排到期时刻且不触发拉取。"""
        script = [
            (5.0, True, {"scheduled_update_interval_minutes": 120}),
            (100000.0, False, None),
        ]
        _clock, cfg, fetches, reload_event = run_loop(script=script, interval_minutes=60)

        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0][1], "scheduled_startup")
        # 第二次等待按新周期从上一轮时刻重算：1000 + 7200 - 1005 = 7195。
        self.assertEqual(reload_event.timeouts, [3600.0, 7195.0])
        self.assertEqual(cfg.scheduled_update_interval_minutes, 120)

    def test_enabling_from_disabled_still_wakes_and_pulls(self):
        """热更新能力必须保留：从关闭改为开启后要能被唤醒并跑首轮。"""
        script = [
            (0.0, True, {"scheduled_update_enabled": True}),
            (100000.0, False, None),
        ]
        _clock, cfg, fetches, reload_event = run_loop(
            script=script, interval_minutes=60, enabled=False
        )

        self.assertTrue(cfg.scheduled_update_enabled)
        self.assertEqual([item[1] for item in fetches], ["scheduled_startup"])
        # 第一次是无超时等待（关闭状态），被唤醒后才进入正常周期。
        self.assertEqual(reload_event.timeouts, [None, 3600.0])

    def test_shortening_interval_pulls_when_new_deadline_already_overdue(self):
        """把周期改短到已逾期是唯一会因保存配置而立即拉取的情况之一。"""
        script = [
            # t=1000 首轮完成后，t=3000 保存配置把周期从 60 分钟改成 1 分钟。
            (2000.0, True, {"scheduled_update_interval_minutes": 1}),
            (100000.0, False, None),
        ]
        _clock, _cfg, fetches, reload_event = run_loop(script=script, interval_minutes=60)

        # 新到期时刻 1000 + 60 = 1060 已在 t=3000 之前，因此立即拉取。
        self.assertEqual(fetches, [(1000.0, "scheduled_startup"), (3000.0, "scheduled")])
        self.assertEqual(reload_event.timeouts, [3600.0, 60.0])

    def test_shortening_interval_waits_when_new_deadline_still_future(self):
        """改短周期但新到期时刻仍在未来时不拉取，只重排等待时长。"""
        script = [
            (5.0, True, {"scheduled_update_interval_minutes": 30}),
            (100000.0, False, None),
        ]
        _clock, _cfg, fetches, reload_event = run_loop(script=script, interval_minutes=60)

        self.assertEqual(fetches, [(1000.0, "scheduled_startup")])
        # 新到期时刻 1000 + 1800 = 2800，t=1005 时还剩 1795 秒。
        self.assertEqual(reload_event.timeouts, [3600.0, 1795.0])

    def test_reenable_within_interval_waits_for_original_deadline(self):
        """关闭后再打开，若关闭时长不足一个周期则不立即拉取。"""
        script = [
            (5.0, True, {"scheduled_update_enabled": False}),    # t=1005 关闭
            (495.0, True, {"scheduled_update_enabled": True}),   # t=1500 重新打开
            (100000.0, False, None),
        ]
        _clock, _cfg, fetches, reload_event = run_loop(script=script, interval_minutes=60)

        self.assertEqual(fetches, [(1000.0, "scheduled_startup")])
        # 原到期时刻 4600 保持不变，t=1500 时还剩 3100 秒。
        self.assertEqual(reload_event.timeouts, [3600.0, None, 3100.0])

    def test_reenable_after_interval_elapsed_pulls_immediately(self):
        """关闭超过一个周期后重新打开应立即补一轮，且标签已是 scheduled。"""
        script = [
            (5.0, True, {"scheduled_update_enabled": False}),     # t=1005 关闭
            (9994.0, True, {"scheduled_update_enabled": True}),   # t=10999 打开，已越过 4600
            (100000.0, False, None),
        ]
        _clock, _cfg, fetches, reload_event = run_loop(script=script, interval_minutes=60)

        self.assertEqual(fetches, [(1000.0, "scheduled_startup"), (10999.0, "scheduled")])
        self.assertEqual(reload_event.timeouts, [3600.0, None, 3600.0])

    def test_failed_fetch_consumes_slot_and_keeps_startup_label(self):
        """拉取失败同样占用本轮：不快速重试，且下一轮仍标记 scheduled_startup。"""
        script = [(100000.0, False, None)]
        _clock, _cfg, fetches, reload_event = run_loop(
            script=script, interval_minutes=60, upsert_raises=True
        )

        self.assertEqual(fetches, [(1000.0, "scheduled_startup")])
        # 失败后仍然等满一个完整周期，没有立即重试。
        self.assertEqual(reload_event.timeouts, [3600.0])

    def test_next_pull_is_anchored_to_fetch_start_not_completion(self):
        """拉取耗时不能把下一轮往后推，否则固定回看窗口盖不住实际间隔。"""
        script = [(3000.0, False, None), (100000.0, False, None)]
        _clock, _cfg, fetches, reload_event = run_loop(
            script=script, interval_minutes=60, upsert_seconds=600.0
        )

        # 首轮 t=1000 开始、t=1600 结束；第二轮仍在 t=4600（1000 + 3600）开始，
        # 而不是按完成时刻算出的 5200。
        self.assertEqual(fetches, [(1000.0, "scheduled_startup"), (4600.0, "scheduled")])
        self.assertEqual(reload_event.timeouts, [3000.0, 3000.0])

    def test_fetch_longer_than_interval_starts_next_round_immediately(self):
        """耗时超过周期时立刻开始下一轮，尽量让窗口贴合真实间隔。"""
        _clock, _cfg, fetches, reload_event = run_loop(
            script=[], interval_minutes=60, upsert_seconds=7200.0
        )

        # 首轮 t=1000 开始、t=8200 结束，已越过 next_due=4600，因此第二轮立即开始。
        self.assertEqual(fetches[:2], [(1000.0, "scheduled_startup"), (8200.0, "scheduled")])
        # 全程没有进入等待，因此不会有任何 reload 超时记录。
        self.assertEqual(reload_event.timeouts, [])


if __name__ == "__main__":
    unittest.main()
