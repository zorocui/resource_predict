import json
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from resource_predict.api.resources import register_resource_routes
from resource_predict.services.forecast_feedback import resource_feedback, _CACHE


def test_missing_stale_corrupt_and_updated_report(tmp_path):
    _CACHE.clear()
    item={"resource_id":"vm-a","resource_type":"openstack_vm"}
    path=tmp_path/"forecast_realized_report.json"
    with patch("resource_predict.services.forecast_feedback.scoped_out_dir",return_value=tmp_path), patch("time.time",return_value=200000):
        assert resource_feedback(item)["report_status"]=="missing"
        path.write_text(json.dumps({"generated_at_epoch_ms":200000000,"activation_assessment":{"rules":{"min_paired_runs":12},"resources":[{"resource_id":"vm-a","status":"continue_observing"},{"resource_id":"vm-b","status":"eligible_for_review"}]}}))
        result=resource_feedback(item)
        assert result["assessment"]["resource_id"]=="vm-a"
        assert result["report_status"]=="available"
        with patch.object(Path,"read_text",side_effect=AssertionError("cached report should not be reparsed")):
            assert resource_feedback(item)["assessment"]==result["assessment"]
        path.write_text(json.dumps({"generated_at_epoch_ms":1,"activation_assessment":{"resources":[]}}))
        assert resource_feedback(item)["report_status"]=="stale"
        path.write_text("invalid json")
        assert resource_feedback(item)["report_status"]=="error"


def test_feedback_endpoint_only_reads_metadata():
    app=Flask(__name__)
    calls=[]
    def detail(rid,**kwargs):
        calls.append(kwargs)
        return {"resource_id":rid} if rid=="vm-a" else None
    helpers={name:lambda *a,**k:None for name in ("get_summary","matches_query","safe_int","action_priority","prediction_pending_for","get_resource_charts")}
    helpers["get_resource_detail"]=detail
    register_resource_routes(app,helpers)
    with patch("resource_predict.api.resources.resource_feedback",return_value={"report_status":"missing"}):
        client=app.test_client()
        assert client.get("/api/resources/vm-a/feedback").json=={"report_status":"missing"}
        assert client.get("/api/resources/missing/feedback").status_code==404
    assert calls==[{"include_charts":False},{"include_charts":False}]
