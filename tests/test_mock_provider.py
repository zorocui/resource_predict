from resource_predict.providers.mock import _mock_k8s_workload_provider


def _values(item, container, metric):
    return item["container_metrics"][container][metric]["values"]


def test_mock_k8s_container_metrics_are_normalized_per_request_and_limit():
    rows = _mock_k8s_workload_provider(resources=1, n=24, freq="h")
    item = rows[0]

    sidecar = item["spec"]["containers"]["sidecar"]
    assert sidecar["memory_request_gb"] == 0.5
    assert sidecar["memory_limit_gb"] == 1.0
    assert sidecar["cpu_request_cores"] == 0.25
    assert sidecar["cpu_limit_cores"] == 1.0

    memory_limit = _values(item, "sidecar", "memory_limit")
    memory_request = _values(item, "sidecar", "memory_request")
    cpu_limit = _values(item, "sidecar", "cpu_limit")
    cpu_request = _values(item, "sidecar", "cpu_request")

    assert memory_limit != memory_request
    assert cpu_limit != cpu_request
    assert memory_request[0] > memory_limit[0]
    assert cpu_request[0] > cpu_limit[0]

