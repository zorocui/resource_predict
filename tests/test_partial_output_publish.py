import json
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace

from resource_predict.pipeline.run import generate_forecasts, generate_predictions_only
from resource_predict.data.io import prepared_dict_to_raw_record
from resource_predict.services import workload_refresh
from resource_predict.services.update_history import get_update_history
from tests.test_forecast_store import _resource


def test_local_prediction_never_opens_manifest_or_other_detail(tmp_path, monkeypatch):
    monkeypatch.setattr("resource_predict.pipeline.run.read_forecast_config", lambda: {"enabled_methods":["rolling_mean"],"enable_ensemble":False})
    # 初始化正常全量产物后，将另一个资源放到独立详情分片。
    raw = [prepared_dict_to_raw_record(_resource(rid, .2)) for rid in ("vm-1","vm-2")]
    generate_forecasts(out_dir=str(tmp_path), data_provider=lambda **_kw: raw, test_size=2, future_steps=2, max_workers=1)
    summary_path = tmp_path / "summary_index.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    second = next(row for row in summary["resources"] if row["resource_id"]=="vm-2")
    old_ref = second["detail_ref"]
    item = json.loads((tmp_path / "details" / old_ref["file"]).read_text(encoding="utf-8"))["resources"][old_ref["offset"]]
    other = tmp_path / "details" / "other.json"
    other.write_text(json.dumps({"resources":[item]}),encoding="utf-8")
    second["detail_ref"] = {"file":"other.json","offset":0}
    summary_path.write_text(json.dumps(summary),encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    before = {p:p.read_bytes() for p in (manifest,other)}
    original_open = Path.open
    def guarded(path, *args, **kwargs):
        assert path.name != "manifest.json" and path != other
        return original_open(path,*args,**kwargs)
    with monkeypatch.context() as ctx:
        ctx.setattr(Path,"open",guarded)
        result = generate_predictions_only(out_dir=str(tmp_path),resource_ids=["vm-1"],local_update=True,
                                           test_size=2,future_steps=2,max_workers=1)
    assert [row["resource_id"] for row in result]==["vm-1"]
    assert all(p.read_bytes()==value for p,value in before.items())
    updated = json.loads(summary_path.read_text(encoding="utf-8"))
    assert len(updated["resources"])==2
    assert next(row for row in updated["resources"] if row["resource_id"]=="vm-2")==second
    accuracy = json.loads((tmp_path/'forecast_accuracy_summary.json').read_text(encoding='utf-8'))
    assert {row['resource_id'] for row in accuracy['rows']}=={'vm-1','vm-2'}
    assert accuracy['mixed_prediction_runs']


def test_single_refresh_persists_its_own_history_on_success_and_failure(tmp_path, monkeypatch):
    updater = workload_refresh.updater
    monkeypatch.setattr(workload_refresh,"settings",SimpleNamespace(app=SimpleNamespace(out_dir=tmp_path),
                        k8s_prometheus=SimpleNamespace(step_seconds=600)))
    monkeypatch.setattr(workload_refresh,"fetch_single_k8s_workload",lambda resource:resource)
    monkeypatch.setattr(updater,"get_update_status",lambda:{"last_started_at":1})
    def assert_history_visible(*_args, **_kwargs):
        assert get_update_history(out_dir=tmp_path)
    monkeypatch.setattr(updater,"mark_external_update_finished",Mock(side_effect=assert_history_visible))
    monkeypatch.setattr(updater,"mark_external_update_failed",Mock(side_effect=assert_history_visible))
    for result in ({"success":True,"resources_updated":1},{"success":False,"error":"fetch failed"}):
        monkeypatch.setattr(updater,"_do_update",Mock(return_value=result))
        assert updater._update_exclusive.acquire(blocking=False)
        workload_refresh._run({"resource_id":"k8s:c:ns:deployment:api"})
    rows=get_update_history(out_dir=tmp_path)
    assert len(rows)==2
    assert {row['status'] for row in rows}=={'success','failed'}
    assert all('k8s:c:ns:deployment:api' in row['message'] for row in rows)
    assert not updater._update_exclusive.locked()


def test_skipped_prediction_is_partial_success_in_history(tmp_path, monkeypatch):
    updater=workload_refresh.updater
    monkeypatch.setattr(workload_refresh,"settings",SimpleNamespace(app=SimpleNamespace(out_dir=tmp_path),
                        k8s_prometheus=SimpleNamespace(step_seconds=600)))
    rid="k8s:c:ns:deployment:api"
    (tmp_path/'k8s').mkdir()
    (tmp_path/'k8s'/'generation_stats.json').write_text(json.dumps({"predicted_resources":0,
        "prediction_skips":[{"resource_id":rid,"metric":"cpu_limit","reason":"recent_contiguous_segment_too_short"}]}),encoding='utf-8')
    monkeypatch.setattr(workload_refresh,"fetch_single_k8s_workload",lambda resource:resource)
    monkeypatch.setattr(updater,"get_update_status",lambda:{"last_started_at":1})
    monkeypatch.setattr(updater,"_do_update",Mock(return_value={"success":True}))
    monkeypatch.setattr(updater,"mark_external_update_finished",Mock())
    assert updater._update_exclusive.acquire(blocking=False)
    result=workload_refresh._run({"resource_id":rid})
    assert result['status']=='partial_success'
    record=get_update_history(out_dir=tmp_path)[0]
    assert record['status']=='partial_success'
    assert record['predicted_resources']==0
    assert '沿用旧预测' in record['message']
