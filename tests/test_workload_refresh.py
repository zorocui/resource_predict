from unittest.mock import Mock

from flask import Flask
import pytest

from resource_predict.api.scaling import register_scaling_routes
from resource_predict.data import updater
from resource_predict.data.io import prepared_dict_to_raw_record
from resource_predict.data.raw_store import RawResourceStore, write_raw_resource_dataset
from resource_predict.providers import k8s_prometheus as provider
from resource_predict.services import workload_refresh
from resource_predict.services.store.query import safe_int
from tests.test_k8s_workload_provider import FakePrometheusClient, _target
from tests.test_raw_store import _k8s


def test_single_fetch_limits_usage_queries_to_target_pods(monkeypatch):
    monkeypatch.setattr(provider, "PrometheusClient", FakePrometheusClient)
    monkeypatch.setattr(provider, "_resolve_targets", lambda: [_target("cluster-a"), _target("other")])
    FakePrometheusClient.range_calls = []
    resource = {"resource_id":"k8s:cluster-a:ns:deployment:api", "spec":{
        "cluster":"cluster-a", "namespace":"ns", "workload_kind":"Deployment", "workload_name":"api"}}
    result = provider.fetch_single_k8s_workload(resource)
    assert result["resource_id"] == resource["resource_id"]
    queries = [call["query"] for call in FakePrometheusClient.range_calls
               if "container_cpu_usage_seconds_total" in call["query"] or "container_memory_working_set_bytes" in call["query"]]
    assert len(queries) == 2
    assert all('pod=~' in query and 'namespace=~"ns"' in query for query in queries)


def test_refresh_admission_holds_lock_and_failure_releases_it(monkeypatch):
    callbacks = []
    monkeypatch.setattr(workload_refresh, "start_update_task_async", lambda func, resource, **_kw: callbacks.append((func,resource)))
    monkeypatch.setattr(workload_refresh, "fetch_single_k8s_workload", Mock(side_effect=ValueError("missing data")))
    monkeypatch.setattr(updater, "_update_status", {"running":False})
    resource = {"resource_id":"k8s:c:ns:deployment:api"}
    workload_refresh.start_workload_refresh(resource)
    assert updater._update_exclusive.locked()
    with pytest.raises(updater.UpdateBusyError):
        workload_refresh.start_workload_refresh(resource)
    with pytest.raises(ValueError, match="missing data"):
        callbacks[0][0](callbacks[0][1])
    assert not updater._update_exclusive.locked()
    assert not updater.get_update_status()["running"]


def test_force_predict_unchanged_resource_does_not_recompute_others(tmp_path, monkeypatch):
    first, other = _k8s("k8s:c:ns:deployment:api"), _k8s("k8s:c:ns:deployment:other")
    write_raw_resource_dataset(tmp_path, [first,other], freq="5min")
    ref = RawResourceStore(tmp_path).raw_ref(other["resource_id"])
    generate = Mock(return_value=[])
    monkeypatch.setattr("resource_predict.pipeline.generate_predictions_only", generate)
    result = updater._do_update(new_data_list=[prepared_dict_to_raw_record(first)], out_dir=tmp_path,
                                allow_create=True, force_predict=True)
    assert result["success"], result
    assert generate.call_args.kwargs["resource_ids"] == [first["resource_id"]]
    assert RawResourceStore(tmp_path).raw_ref(other["resource_id"]) == ref


def test_refresh_api_is_workload_only_and_returns_accepted(monkeypatch):
    app = Flask(__name__)
    resource = {"resource_id":"k8s:c:ns:deployment:api", "resource_type":"k8s_workload"}
    register_scaling_routes(app,{"get_resource_detail":lambda rid, **_kw: resource if rid==resource["resource_id"] else None,
                                "safe_int":safe_int})
    start = Mock()
    monkeypatch.setattr(workload_refresh, "start_workload_refresh", start)
    client = app.test_client()
    assert client.post('/api/resources/'+resource["resource_id"]+'/refresh-forecast').status_code == 202
    start.assert_called_once_with(resource)
    assert client.post('/api/resources/missing/refresh-forecast').status_code == 404
    resource["resource_type"] = "openstack_vm"
    assert client.post('/api/resources/'+resource["resource_id"]+'/refresh-forecast').status_code == 400
