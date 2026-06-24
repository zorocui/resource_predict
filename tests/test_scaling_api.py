from __future__ import annotations

import unittest
from unittest.mock import patch

from flask import Flask

from resource_predict.api.scaling import register_scaling_routes


class ScalingApiTest(unittest.TestCase):
    def test_scale_route_passes_manual_target_spec_to_task_creation(self):
        app = Flask(__name__)
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {"cluster": "cluster", "namespace": "ns"},
            "scaling_advice": {"action": "hold", "target_spec": {}},
        }
        register_scaling_routes(
            app,
            {
                "get_resource_detail": lambda resource_id, **_kwargs: resource if resource_id == resource["resource_id"] else None,
                "safe_int": lambda value, default: default,
            },
        )
        manual_target = {
            "containers": {
                "app": {"cpu_request_cores": 0.5},
            },
            "replicas": 3,
        }

        with patch("resource_predict.api.scaling.create_scaling_task") as create_task:
            create_task.return_value = {"task_id": "scale-test"}
            response = app.test_client().post(
                f"/api/resources/{resource['resource_id']}/scale",
                json={
                    "mode": "dry_run",
                    "target_source": "manual",
                    "target_spec": manual_target,
                },
            )

        self.assertEqual(response.status_code, 202)
        kwargs = create_task.call_args.kwargs
        self.assertEqual(kwargs["target_source"], "manual")
        self.assertEqual(kwargs["target_spec_override"], manual_target)
        self.assertFalse(kwargs["ignore_cooldown"])

    def test_scale_route_passes_ignore_cooldown_to_task_creation(self):
        app = Flask(__name__)
        resource = {
            "resource_id": "k8s:cluster:ns:deployment:api",
            "resource_type": "k8s_workload",
            "spec": {"cluster": "cluster", "namespace": "ns"},
            "scaling_advice": {"action": "scale_out_candidate", "target_spec": {}},
        }
        register_scaling_routes(
            app,
            {
                "get_resource_detail": lambda resource_id, **_kwargs: resource if resource_id == resource["resource_id"] else None,
                "safe_int": lambda value, default: default,
            },
        )

        with patch("resource_predict.api.scaling.create_scaling_task") as create_task:
            create_task.return_value = {"task_id": "scale-test"}
            response = app.test_client().post(
                f"/api/resources/{resource['resource_id']}/scale",
                json={
                    "mode": "execute",
                    "confirm": True,
                    "ignore_cooldown": True,
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(create_task.call_args.kwargs["ignore_cooldown"])


if __name__ == "__main__":
    unittest.main()
