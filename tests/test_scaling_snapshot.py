from __future__ import annotations

from resource_predict.services.scaling.snapshot import _merge_spec


def test_merge_spec_patches_container_targets_without_dropping_untouched_containers():
    current = {
        "containers": {
            "app": {
                "cpu_request_cores": 0.5,
                "cpu_limit_cores": 1.0,
                "memory_request_gb": 0.5,
                "memory_limit_gb": 1.0,
            },
            "sidecar": {
                "cpu_request_cores": 0.1,
                "cpu_limit_cores": 0.2,
                "memory_request_gb": 0.1,
                "memory_limit_gb": 0.2,
            },
        },
        "replicas_observed": 2,
    }
    effective = {
        "containers": {
            "app": {
                "cpu_request_cores": 0.8,
                "cpu_limit_cores": None,
            }
        },
        "replicas": 3,
    }

    merged = _merge_spec(current, effective)

    assert merged["containers"]["app"]["cpu_request_cores"] == 0.8
    assert merged["containers"]["app"]["cpu_limit_cores"] == 1.0
    assert merged["containers"]["sidecar"]["cpu_request_cores"] == 0.1
    assert merged["containers"]["sidecar"]["memory_limit_gb"] == 0.2
    assert merged["replicas"] == 3
