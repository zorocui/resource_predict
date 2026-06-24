import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from resource_predict.pipeline.action_gate_state import (
    ACTION_GATE_STATE_FILENAME,
    apply_action_gate_confirmations,
    load_action_gate_state,
    write_action_gate_state,
)
from resource_predict.pipeline.run import generate_forecasts, generate_predictions_only


NOW = datetime(2026, 6, 18, 8, 0, tzinfo=timezone.utc)


def _resource(resource_id="resource-1", action="scale_in_candidate", required=3, target=2):
    return {
        "resource_id": resource_id,
        "scaling_advice": {
            "action": action,
            "target_spec": {"cpu_cores": target},
            "action_gate": {
                "state": "observe",
                "required_consistent_rounds": required,
                "observed_consistent_rounds": 0,
            },
        },
    }


class ActionGateStateTests(unittest.TestCase):
    def test_same_direction_accumulates_until_ready_despite_target_changes(self):
        state = {"schema_version": 1, "resources": {}}
        observed_states = []
        for target in (2, 3, 4):
            item = _resource(target=target)
            state = apply_action_gate_confirmations(
                [item],
                eligible_resource_ids={"resource-1"},
                prior_state=state,
                retention_days=30,
                now=NOW,
            )
            observed_states.append(dict(item["scaling_advice"]["action_gate"]))

        self.assertEqual([gate["observed_consistent_rounds"] for gate in observed_states], [1, 2, 3])
        self.assertEqual([gate["state"] for gate in observed_states], ["observe", "observe", "ready"])
        self.assertEqual(state["resources"]["resource-1"]["consistent_rounds"], 3)

    def test_direction_change_restarts_at_one(self):
        prior = {
            "schema_version": 1,
            "resources": {
                "resource-1": {
                    "action_direction": "scale_in",
                    "consistent_rounds": 3,
                    "last_confirmed_at": "2026-06-18T07:00:00Z",
                }
            },
        }
        item = _resource(action="scale_out_candidate", required=2)

        state = apply_action_gate_confirmations(
            [item],
            eligible_resource_ids={"resource-1"},
            prior_state=prior,
            retention_days=30,
            now=NOW,
        )

        self.assertEqual(item["scaling_advice"]["action_gate"]["observed_consistent_rounds"], 1)
        self.assertEqual(item["scaling_advice"]["action_gate"]["state"], "observe")
        self.assertEqual(state["resources"]["resource-1"]["action_direction"], "scale_out")

    def test_hold_clears_confirmation_state(self):
        prior = {
            "schema_version": 1,
            "resources": {
                "resource-1": {
                    "action_direction": "scale_in",
                    "consistent_rounds": 2,
                    "last_confirmed_at": "2026-06-18T07:00:00Z",
                }
            },
        }
        item = _resource(action="hold", required=1)
        item["scaling_advice"]["action_gate"]["state"] = "ready"

        state = apply_action_gate_confirmations(
            [item],
            eligible_resource_ids={"resource-1"},
            prior_state=prior,
            retention_days=30,
            now=NOW,
        )

        self.assertNotIn("resource-1", state["resources"])
        self.assertEqual(item["scaling_advice"]["action_gate"]["observed_consistent_rounds"], 0)
        self.assertEqual(item["scaling_advice"]["action_gate"]["state"], "ready")

    def test_partial_prediction_preserves_unselected_resource(self):
        prior = {
            "schema_version": 1,
            "resources": {
                "resource-1": {
                    "action_direction": "scale_in",
                    "consistent_rounds": 1,
                    "last_confirmed_at": "2026-06-18T07:00:00Z",
                },
                "resource-2": {
                    "action_direction": "scale_out",
                    "consistent_rounds": 1,
                    "last_confirmed_at": "2026-06-18T07:00:00Z",
                },
            },
        }
        selected = _resource("resource-1")
        unselected = _resource("resource-2", action="scale_out_candidate", required=2)

        state = apply_action_gate_confirmations(
            [selected, unselected],
            eligible_resource_ids={"resource-1"},
            prior_state=prior,
            retention_days=30,
            now=NOW,
        )

        self.assertEqual(state["resources"]["resource-1"]["consistent_rounds"], 2)
        self.assertEqual(state["resources"]["resource-2"]["consistent_rounds"], 1)
        self.assertEqual(unselected["scaling_advice"]["action_gate"]["observed_consistent_rounds"], 0)

    def test_expired_state_restarts_at_one(self):
        prior = {
            "schema_version": 1,
            "resources": {
                "resource-1": {
                    "action_direction": "scale_in",
                    "consistent_rounds": 2,
                    "last_confirmed_at": (NOW - timedelta(days=31)).isoformat(),
                }
            },
        }
        item = _resource()

        state = apply_action_gate_confirmations(
            [item],
            eligible_resource_ids={"resource-1"},
            prior_state=prior,
            retention_days=30,
            now=NOW,
        )

        self.assertEqual(state["resources"]["resource-1"]["consistent_rounds"], 1)

    def test_state_roundtrip_and_corrupt_file_recovery(self):
        payload = {
            "schema_version": 1,
            "resources": {
                "resource-1": {
                    "action_direction": "scale_out",
                    "consistent_rounds": 1,
                    "last_confirmed_at": "2026-06-18T08:00:00Z",
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            out_base = Path(tmp)
            write_action_gate_state(out_base, payload)
            self.assertEqual(load_action_gate_state(out_base), payload)

            (out_base / ACTION_GATE_STATE_FILENAME).write_text("{broken", encoding="utf-8")
            recovered = load_action_gate_state(out_base)

        self.assertEqual(recovered, {"schema_version": 1, "resources": {}})

    def test_prediction_only_round_accumulates_without_rewriting_raw_index(self):
        index = pd.date_range("2026-01-01", periods=240, freq="h")
        values = pd.Series([0.95] * len(index), index=index)
        source = {
            "resource_id": "vm-hot",
            "resource_type": "openstack_vm",
            "spec": {"cpu_cores": 2, "memory_gb": 4, "disk_gb": 40},
            "metrics": {
                metric: {
                    "timestamps": (index.view("int64") // 1_000_000).tolist(),
                    "values": values.tolist(),
                }
                for metric in ("cpu", "memory", "disk")
            },
        }
        forecast_cfg = {"enabled_methods": ["rolling_mean"], "enable_ensemble": False}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch("resource_predict.pipeline.run.read_forecast_config", return_value=forecast_cfg):
                generate_forecasts(
                    out_dir=str(base),
                    data_provider=lambda resources, n, freq: [source],
                    test_size=12,
                    future_steps=6,
                    max_workers=1,
                )
                raw_before = (base / "raw_index.json").read_bytes()
                generate_predictions_only(
                    out_dir=str(base),
                    test_size=12,
                    future_steps=6,
                    max_workers=1,
                )
                raw_after = (base / "raw_index.json").read_bytes()

            summary = json.loads((base / "summary_index.json").read_text(encoding="utf-8"))
            gate = summary["resources"][0]["scaling_advice"]["action_gate"]

        self.assertEqual(raw_before, raw_after)
        self.assertEqual(gate["observed_consistent_rounds"], 2)
        self.assertEqual(gate["state"], "ready")


if __name__ == "__main__":
    unittest.main()
