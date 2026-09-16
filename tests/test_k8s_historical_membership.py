import pandas as pd
import pytest

from resource_predict.providers import k8s_prometheus as provider


def test_retired_pod_history_and_old_capacity_survive_rollout(monkeypatch):
    old_times = pd.date_range("2026-09-14 16:00", "2026-09-14 18:40", freq="10min")
    new_times = pd.date_range("2026-09-14 19:00", "2026-09-14 20:00", freq="10min")
    calls = []
    def row(labels, times, value):
        return {"metric": labels, "values": [[t.timestamp(), str(value)] for t in times]}
    def labels(pod):
        return {"namespace": "ns", "pod": pod, "container": "app"}
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def query(self, query, **kwargs):
            if "kube_pod_owner" in query:
                return [{"metric": {"namespace":"ns", "pod":"new", "owner_kind":"ReplicaSet", "owner_name":"new-rs"}, "value":[0,"1"]}]
            if "kube_replicaset_owner" in query:
                return [{"metric": {"namespace":"ns", "replicaset":"new-rs", "owner_kind":"Deployment", "owner_name":"api"}, "value":[0,"1"]}]
            if "kube_deployment_spec_replicas" in query:
                return [{"metric":{"namespace":"ns", "deployment":"api"}, "value":[0,"1"]}]
            if "resource_" in query:
                value = 1024**3 if "memory" in query else 1
                return [{"metric":labels("new"), "value":[0,str(value)]}]
            return []
        def query_range(self, query, *, start, end, step):
            calls.append((query, start, end, step))
            if "kube_pod_owner" in query:
                return [row({"namespace":"ns", "pod":pod, "owner_kind":"ReplicaSet", "owner_name":pod+"-rs"}, ts, 1)
                        for pod,ts in (("old",old_times),("new",new_times))]
            if "kube_replicaset_owner" in query:
                return [row({"namespace":"ns", "replicaset":pod+"-rs", "owner_kind":"Deployment", "owner_name":"api"}, ts, 1)
                        for pod,ts in (("old",old_times),("new",new_times))]
            memory = "memory" in query
            capacity = "kube_pod_container_resource_" in query
            unit = 1024**3 if memory else 1
            return [row(labels("old"), old_times, unit*(2 if capacity else 1)),
                    row(labels("new"), new_times, unit*(1 if capacity else .5))]
    monkeypatch.setattr(provider, "PrometheusClient", Client)
    target = provider.PrometheusTarget(cluster="c", prometheus_url="http://unused", namespace_regex="",
        bearer_token="", basic_auth="", history_days=1, step_seconds=600, request_timeout_seconds=5)
    items = provider._fetch_target(target, limit=0)
    assert len(items) == 1
    item = items[0]
    assert item["resource_id"] == "k8s:c:ns:deployment:api"
    assert item["spec"]["pods_observed"] == ["new"]
    assert item["spec"]["containers"]["app"]["memory_limit_gb"] == 1
    for metric in ("cpu_request", "cpu_limit", "memory_request", "memory_limit"):
        data = item["observation_evidence"]["container_metrics"]["app"][metric]
        assert len(data["timestamps"]) == len(old_times) + len(new_times)
        assert data["values"] == pytest.approx([.5] * len(data["values"]))
        assert max(b-a for a,b in zip(data["timestamps"],data["timestamps"][1:])) == 20*60*1000
    assert len({(start,end,step) for _,start,end,step in calls}) == 1
    assert sum("kube_pod_owner" in q for q, *_ in calls) == 1
    monkeypatch.setattr(provider, "_scaling_capacity_history", lambda *args: ({}, ["query failed"]))
    with pytest.raises(RuntimeError, match="历史容量查询无结果"):
        provider._fetch_target(target, limit=0)


def test_missing_member_sample_does_not_halve_utilization():
    times = pd.date_range("2026-01-01", periods=3, freq="10min")
    a, b = ("ns","a","app"), ("ns","b","app")
    usage = {a:pd.Series([.8,.8,.8],index=times), b:pd.Series([.8,.8],index=times[[0,2]])}
    capacity = {key:pd.Series(1.,index=times) for key in (a,b)}
    owners = {key[:2]:("Deployment","api") for key in (a,b)}
    mode, values = provider._historical_normalized_series(usage,capacity,owners,"cpu","request")[(('ns','Deployment','api'),'app')]
    assert mode == "cpu_usage/cpu_request"
    assert list(values.index) == list(times[[0,2]])
    assert values.tolist() == [.8,.8]


def test_conflicting_historical_owner_is_not_assigned_to_current_workload():
    class Client:
        def query_range(self, query, **kwargs):
            if "kube_pod_owner" in query:
                return [{"metric":{"namespace":"ns","pod":"reused","owner_kind":"StatefulSet","owner_name":"old"}, "values":[[1,"1"]]}]
            return []
    result = provider._historical_workload_owners(Client(), 'pod!=""', "", 0, 2, 1,
                                                 {("ns","reused"):("StatefulSet","new")}, {})
    assert ("ns","reused") not in result
