import unittest
from unittest.mock import patch

from resource_predict.data import updater
from resource_predict.services import k8s_ingest


class StopDuringDelayEvent:
    def __init__(self):
        self.waits = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        self.stopped = True
        return True


class SchedulerStartupDelayTest(unittest.TestCase):
    def test_vm_scheduler_can_stop_during_startup_delay_without_updating(self):
        event = StopDuringDelayEvent()
        with patch.object(updater, "_stop_event", event):
            with patch.object(updater, "run_update") as run_update:
                updater._scheduler_loop(3600.0, None, 1, 60.0)

        self.assertEqual(event.waits, [60.0])
        run_update.assert_not_called()

    def test_k8s_scheduler_can_stop_during_startup_delay_without_fetching(self):
        event = StopDuringDelayEvent()
        with patch.object(k8s_ingest, "_k8s_stop_event", event):
            with patch.object(k8s_ingest, "run_k8s_prometheus_upsert") as run_upsert:
                k8s_ingest._k8s_scheduler_loop(3600.0, 60.0)

        self.assertEqual(event.waits, [60.0])
        run_upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
