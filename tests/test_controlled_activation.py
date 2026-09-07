import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from resource_predict.pipeline.controlled_activation import apply_controlled_advice, calibrated_execution_failure
from resource_predict.pipeline.action_gate_state import apply_action_gate_confirmations
from resource_predict.pipeline.shadow import build_shadow_advice, calibrated_advice
from resource_predict.settings import settings
from resource_predict.services.scaling import tasks
from test_shadow import vm, T


@pytest.fixture
def context(tmp_path):
    item = vm()
    build_shadow_advice([item])
    report = tmp_path/"forecast_realized_report.json"
    batch = "forecast_current.jsonl.gz"
    assessment = {"rule_version":"empirical_review_v1","mode":"review_only","automatic_activation":False,
                  "generated_at_epoch_ms":T,"resources":[{"resource_id":"vm-a","resource_type":"openstack_vm",
                  "status":"eligible_for_review","latest_batch":batch,"valid_until_epoch_ms":T+3600000}]}
    report.write_text(json.dumps({"activation_assessment":assessment}),encoding="utf-8")
    kwargs = {"fresh_ids":{"vm-a"},"report_path":report,"archive_metadata":{"status":"completed","path":str(tmp_path/batch)},
              "feedback_metadata":{"status":"completed","report_published":True},"now_ms":T}
    cfg = replace(settings.decision,calibrated_advice_enabled=True,calibrated_advice_resource_ids=("vm-a",))
    test_settings = SimpleNamespace(decision=cfg,forecast=settings.forecast,k8s_prometheus=settings.k8s_prometheus)
    with patch("resource_predict.pipeline.controlled_activation.settings",test_settings), patch(
        "resource_predict.pipeline.controlled_activation._current_assessment",
        side_effect=lambda path,resource,now: json.loads(report.read_text())["activation_assessment"],
    ):
        yield item,kwargs,assessment,test_settings


def test_default_is_disabled():
    assert settings.decision.calibrated_advice_enabled is False
    assert settings.decision.calibrated_advice_resource_ids == ()


def test_adopt_complete_advice_and_preserve_baseline(context):
    item,kwargs,_,_ = context
    baseline = copy.deepcopy(item["scaling_advice"])
    expected = calibrated_advice(item)
    apply_controlled_advice([item],**kwargs)
    active = item["scaling_advice"]
    assert active["calibration_activation"]["status"]=="active"
    assert active["calibration_activation"]["baseline_advice"]==baseline
    for key in ("action","target_spec","confidence","confidence_score","risk_profile","action_gate"):
        assert active[key]==expected[key]
    assert calibrated_execution_failure(item,T) is None


@pytest.mark.parametrize("reason",["disabled","not_allowlisted","not_fresh","missing_report","failed_report",
                                   "failed_archive","partial","expired","old_batch","changed_target","changed_spec"])
def test_failed_conditions_preserve_baseline(context,reason):
    item,kwargs,assessment,cfg = context
    baseline = copy.deepcopy(item["scaling_advice"])
    if reason=="disabled": cfg.decision=replace(cfg.decision,calibrated_advice_enabled=False)
    elif reason=="not_allowlisted": cfg.decision=replace(cfg.decision,calibrated_advice_resource_ids=())
    elif reason=="not_fresh": kwargs["fresh_ids"]=set()
    elif reason=="missing_report": kwargs["report_path"].unlink()
    elif reason=="failed_report": kwargs["feedback_metadata"]["status"]="failed"
    elif reason=="failed_archive": kwargs["archive_metadata"]["status"]="failed"
    elif reason=="partial": item["shadow_comparison"]["status"]="unavailable"
    elif reason=="changed_target": item["shadow_comparison"]["candidate"]["target_spec"]={}
    elif reason=="changed_spec": item["spec"]["cpu_cores"]=64
    else:
        if reason=="expired": assessment["resources"][0]["valid_until_epoch_ms"]=T
        else: assessment["resources"][0]["latest_batch"]="old.jsonl.gz"
        kwargs["report_path"].write_text(json.dumps({"activation_assessment":assessment}),encoding="utf-8")
    apply_controlled_advice([item],**kwargs)
    assert item["scaling_advice"]["calibration_activation"]["status"]=="baseline"
    for key in baseline:
        assert item["scaling_advice"][key]==baseline[key]


def test_active_advice_rolls_back_when_disabled_or_not_fresh(context):
    item,kwargs,_,cfg=context
    original=copy.deepcopy(item["scaling_advice"])
    apply_controlled_advice([item],**kwargs)
    active=copy.deepcopy(item)
    cfg.decision=replace(cfg.decision,calibrated_advice_enabled=False)
    assert calibrated_execution_failure(item,T) is not None
    apply_controlled_advice([item],**kwargs)
    assert item["scaling_advice"]["target_spec"]==original["target_spec"]
    assert item["scaling_advice"]["calibration_activation"]["status"]=="baseline"
    cfg.decision=replace(cfg.decision,calibrated_advice_enabled=True)
    kwargs["fresh_ids"]=set()
    apply_controlled_advice([active],**kwargs)
    assert active["scaling_advice"]["target_spec"]==original["target_spec"]
    assert active["scaling_advice"]["calibration_activation"]["reason"]=="not_a_fresh_prediction"


@pytest.mark.parametrize("change",["expiry","config","context","target","report"])
def test_execution_revalidates_active_advice(context,change):
    item,kwargs,_,cfg=context
    apply_controlled_advice([item],**kwargs)
    now=T
    if change=="expiry": now=T+3600000
    elif change=="config": cfg.decision=replace(cfg.decision,scale_out_threshold=0.7)
    elif change=="context": item["spec"]["cpu_cores"]=64
    elif change=="target": item["scaling_advice"]["target_spec"]={"cpu_cores":64}
    else: kwargs["report_path"].unlink()
    assert calibrated_execution_failure(item,now) is not None
    failures=tasks._execution_gate_failures(item,now_ms=now,target_source="confirmed")
    assert any("calibrated" in failure for failure in failures)


def test_strategy_switch_resets_existing_confirmation_count(context):
    item,kwargs,_,cfg=context
    apply_controlled_advice([item],**kwargs)
    advice=item["scaling_advice"]
    advice["action"]="scale_in"
    advice["action_gate"]={"required_consistent_rounds":3}
    now=datetime.fromtimestamp(T/1000,tz=timezone.utc)
    prior={"resources":{"vm-a":{"action_direction":"scale_in","consistent_rounds":3,
                                  "last_confirmed_at":now.isoformat(),"strategy":"baseline"}}}
    state=apply_action_gate_confirmations([item],eligible_resource_ids={"vm-a"},prior_state=prior,retention_days=30,now=now)
    assert advice["action_gate"]["observed_consistent_rounds"]==1
    assert advice["action_gate"]["state"]=="observe"
    state=apply_action_gate_confirmations([item],eligible_resource_ids={"vm-a"},prior_state=state,retention_days=30,now=now)
    assert advice["action_gate"]["observed_consistent_rounds"]==2
    cfg.decision=replace(cfg.decision,calibrated_advice_enabled=False)
    apply_controlled_advice([item],**kwargs)
    item["scaling_advice"]["action"]="scale_in"
    item["scaling_advice"]["action_gate"]={"required_consistent_rounds":3}
    apply_action_gate_confirmations([item],eligible_resource_ids={"vm-a"},prior_state=state,retention_days=30,now=now)
    assert item["scaling_advice"]["action_gate"]["observed_consistent_rounds"]==1


def test_execute_rejected_before_enqueue(context):
    item,kwargs,_,cfg=context
    apply_controlled_advice([item],**kwargs)
    cfg.decision=replace(cfg.decision,calibrated_advice_enabled=False)
    with patch.object(tasks,"get_active_task_for_resource",return_value=None), patch.object(tasks,"_upsert_task") as queued:
        with pytest.raises(RuntimeError,match="calibrated"):
            tasks.create_scaling_task(item,mode="execute")
        queued.assert_not_called()


def test_queue_wait_cannot_bypass_expired_authorization(context):
    item,kwargs,_,cfg=context
    apply_controlled_advice([item],**kwargs)
    cfg.decision=replace(cfg.decision,calibrated_advice_enabled=False)
    task={"mode":"execute","resource_id":"vm-a","target_source":"suggested"}
    with patch.object(tasks,"get_task",return_value=task), patch.object(tasks,"_patch_task") as patched, \
         patch.object(tasks,"get_cluster_config",return_value={}), patch.object(tasks,"build_scaling_plan") as plan:
        tasks._run_task("test",item)
    plan.assert_not_called()
    assert any(call.args[1].get("status")=="failed" for call in patched.call_args_list)


def test_new_scoring_can_revoke_still_fresh_report(context):
    item,kwargs,assessment,_=context
    apply_controlled_advice([item],**kwargs)
    current=copy.deepcopy(assessment)
    current["resources"][0]["status"]="continue_observing"
    with patch("resource_predict.pipeline.controlled_activation._current_assessment",return_value=current):
        assert "not_eligible" in calibrated_execution_failure(item,T)


def test_allowlist_string_is_not_treated_as_substring_permission(context):
    item,kwargs,_,cfg=context
    cfg.decision=replace(cfg.decision,calibrated_advice_resource_ids="vm-a-longer")
    apply_controlled_advice([item],**kwargs)
    assert item["scaling_advice"]["calibration_activation"]["reason"]=="resource_not_allowlisted"
