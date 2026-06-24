from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from resource_predict.api.updates import register_update_routes
from resource_predict.data import updater
from resource_predict.services.update_history import (
    UPDATE_HISTORY_RETENTION,
    append_update_history,
    get_update_history,
    update_history_path,
)


class UpdateHistoryStoreTest(unittest.TestCase):
    def test_history_is_persistent_sorted_and_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            for idx in range(UPDATE_HISTORY_RETENTION + 5):
                self.assertTrue(
                    append_update_history(
                        {
                            "status": "success",
                            "started_at": float(idx),
                            "finished_at": float(idx + 1),
                            "resources_updated": idx,
                        },
                        out_dir=tmp,
                    )
                )

            records = get_update_history(UPDATE_HISTORY_RETENTION, out_dir=tmp)
            payload = json.loads(update_history_path(tmp).read_text(encoding="utf-8"))

        self.assertEqual(len(records), UPDATE_HISTORY_RETENTION)
        self.assertEqual(records[0]["resources_updated"], UPDATE_HISTORY_RETENTION + 4)
        self.assertEqual(records[-1]["resources_updated"], 5)
        self.assertEqual(len(payload["records"]), UPDATE_HISTORY_RETENTION)

    def test_corrupt_history_returns_empty_and_can_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = update_history_path(tmp)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{broken", encoding="utf-8")

            self.assertEqual(get_update_history(out_dir=tmp), [])
            self.assertTrue(
                append_update_history(
                    {"status": "failed", "finished_at": 10, "error": "boom"},
                    out_dir=tmp,
                )
            )
            records = get_update_history(out_dir=tmp)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "failed")
        self.assertEqual(records[0]["error"], "boom")


class UpdateHistoryIntegrationTest(unittest.TestCase):
    def setUp(self):
        updater._last_history_started_at = None

    def test_external_success_writes_one_terminal_record(self):
        with patch.object(updater, "append_update_history", return_value=True) as append:
            updater.mark_external_update_started(
                "fetching_k8s_prometheus",
                "正在拉取",
                metadata={
                    "task_source": "页面手动拉取",
                    "fetch_window_label": "增量窗口：最近 7 小时",
                },
            )
            updater.mark_external_update_finished(
                {
                    "success": True,
                    "resources_updated": 3,
                    "resources_created": 1,
                    "predicted_resources": 4,
                    "total_new_points": 120,
                    "elapsed_seconds": 2.5,
                }
            )
            updater.mark_external_update_finished({"success": True})

        append.assert_called_once()
        record = append.call_args.args[0]
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["task_source"], "页面手动拉取")
        self.assertEqual(record["resources_updated"], 3)

    def test_external_failure_is_recorded(self):
        with patch.object(updater, "append_update_history", return_value=True) as append:
            updater.mark_external_update_started("fetching", "正在拉取")
            updater.mark_external_update_failed("Prometheus timeout")

        record = append.call_args.args[0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "Prometheus timeout")

    def test_history_api_validates_limit_and_returns_records(self):
        app = Flask(__name__)
        register_update_routes(app)
        records = [{"id": "update-1", "status": "success"}]

        with patch("resource_predict.api.updates.get_update_history", return_value=records) as get_history:
            response = app.test_client().get("/api/update-history?limit=7")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["records"], records)
        get_history.assert_called_once_with(7)
        self.assertEqual(app.test_client().get("/api/update-history?limit=0").status_code, 400)
        self.assertEqual(app.test_client().get("/api/update-history?limit=nope").status_code, 400)

    def test_update_page_contains_history_region_and_refresh_logic(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "templates" / "index.html").read_text(encoding="utf-8")
        script = (root / "static" / "js" / "index.js").read_text(encoding="utf-8")

        self.assertIn('id="update-history"', template)
        self.assertIn("/api/update-history?limit=20", script)
        self.assertIn("refreshUpdateHistory();", script)


if __name__ == "__main__":
    unittest.main()
