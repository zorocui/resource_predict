"""Explicit opt-in for assessed calibration advice, with baseline rollback."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from resource_predict.pipeline.shadow import calibrated_advice
from resource_predict.pipeline.activation_assessment import activation_assessment
from resource_predict.resource_types import resource_type_of
from resource_predict.settings import settings

logger = logging.getLogger(__name__)


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",", ":"),allow_nan=False).encode()).hexdigest()


def policy_signature():
    return _hash([asdict(settings.decision),asdict(settings.forecast),
                  settings.k8s_prometheus.rate_window,settings.k8s_prometheus.step_seconds])


def context_signature(resource):
    return _hash([resource_type_of(resource),resource.get("resource_id"),resource.get("spec",{}),
                  resource.get("container_metric_modes",{})])


def _enabled(resource):
    cfg = settings.decision
    allowed = cfg.calibrated_advice_resource_ids
    return (cfg.calibrated_advice_enabled is True
            and isinstance(allowed,(tuple,list)) and all(isinstance(rid,str) for rid in allowed)
            and str(resource.get("resource_id")) in allowed)


def _proof(assessment, resource, batch, now_ms):
    if (assessment.get("rule_version") != "empirical_review_v1" or assessment.get("mode") != "review_only"
            or assessment.get("automatic_activation") is not False):
        raise ValueError("unsupported_assessment")
    generated = assessment.get("generated_at_epoch_ms")
    if not isinstance(generated,int) or not 0 <= now_ms-generated < 86400000:
        raise ValueError("stale_assessment")
    matches = [row for row in assessment.get("resources",[]) if row.get("resource_id")==resource.get("resource_id")
               and row.get("resource_type")==resource_type_of(resource)]
    if len(matches)!=1:
        raise ValueError("missing_or_ambiguous_assessment")
    proof = matches[0]
    if proof.get("status")!="eligible_for_review" or proof.get("latest_batch")!=batch:
        raise ValueError("assessment_not_eligible_for_current_batch")
    expires = proof.get("valid_until_epoch_ms")
    if not isinstance(expires,int) or expires<=now_ms:
        raise ValueError("assessment_expired")
    return proof


def apply_controlled_advice(items, *, fresh_ids, report_path, archive_metadata, feedback_metadata, now_ms=None):
    """Switch only complete new assessed predictions; all failure paths restore baseline."""
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    fresh_ids = set(fresh_ids)
    assessment = None
    if settings.decision.calibrated_advice_enabled:
        try:
            assessment = json.loads(Path(report_path).read_text(encoding="utf-8"))["activation_assessment"]
        except (OSError,ValueError,KeyError) as exc:
            logger.warning("[controlled_activation] assessment unavailable: %s",exc)
    batch = Path(archive_metadata.get("path") or "").name
    for item in items:
        advice = item.get("scaling_advice")
        if not isinstance(advice,dict):
            continue
        old = advice.get("calibration_activation",{})
        # Never continue a prior calibrated recommendation without this round's checks.
        if old.get("status")=="active":
            baseline = old.get("baseline_advice")
            if not isinstance(baseline,dict):
                advice["action_gate"] = {"state":"observe","reason":"missing baseline; regenerate predictions"}
                old["valid_until_epoch_ms"] = 0
                continue
            advice = copy.deepcopy(baseline)
            item["scaling_advice"] = advice
        metadata = {"status":"baseline","reason":"disabled","evaluated_at_epoch_ms":now_ms}
        advice["calibration_activation"] = metadata
        if not settings.decision.calibrated_advice_enabled:
            continue
        try:
            if not _enabled(item):
                raise ValueError("resource_not_allowlisted")
            if item.get("resource_id") not in fresh_ids:
                raise ValueError("not_a_fresh_prediction")
            if feedback_metadata.get("status")!="completed" or not feedback_metadata.get("report_published"):
                raise ValueError("fresh_report_unavailable")
            if archive_metadata.get("status") not in ("completed","completed_retention_failed") or not batch:
                raise ValueError("fresh_archive_unavailable")
            comparison = item.get("shadow_comparison",{})
            if comparison.get("status")!="paired" or comparison.get("source_spec")!=item.get("spec",{}):
                raise ValueError("incomplete_or_changed_shadow_pair")
            proof = _proof(assessment or {},item,batch,now_ms)
            signature = policy_signature()
            candidate = calibrated_advice(item)
            snapshot = {k:candidate.get(k) for k in ("action","target_spec","policy_tier")}
            if snapshot != comparison.get("candidate") or policy_signature()!=signature:
                raise ValueError("policy_changed_since_shadow_generation")
            baseline = copy.deepcopy(advice)
            baseline.pop("calibration_activation",None)
            candidate["calibration_activation"] = {
                "status":"active","reason":"explicit_opt_in_and_valid_assessment",
                "evaluated_at_epoch_ms":now_ms,"valid_until_epoch_ms":proof["valid_until_epoch_ms"],
                "batch":batch,"report_path":str(Path(report_path).resolve()),"rule_version":assessment["rule_version"],
                "context_signature":context_signature(item),"policy_signature":signature,
                "target_signature":_hash(snapshot),"baseline_advice":baseline,
            }
            item["scaling_advice"] = candidate
        except (OSError,ValueError,TypeError,KeyError,OverflowError) as exc:
            metadata["reason"] = str(exc)


def calibrated_execution_failure(resource, now_ms=None):
    """Additional gate for suggested calibrated targets, rechecked immediately before execution."""
    advice = resource.get("scaling_advice",{})
    metadata = advice.get("calibration_activation",{})
    if metadata.get("status")!="active":
        return None
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    try:
        if not _enabled(resource):
            raise ValueError("calibrated policy disabled or resource no longer allowlisted")
        expires = metadata.get("valid_until_epoch_ms")
        if not isinstance(expires,int) or expires<=now_ms:
            raise ValueError("calibrated assessment expired")
        if metadata.get("context_signature")!=context_signature(resource) or metadata.get("policy_signature")!=policy_signature():
            raise ValueError("calibrated context or policy changed")
        snapshot = {k:advice.get(k) for k in ("action","target_spec","policy_tier")}
        if metadata.get("target_signature")!=_hash(snapshot):
            raise ValueError("calibrated target changed")
        assessment = json.loads(Path(metadata["report_path"]).read_text(encoding="utf-8"))["activation_assessment"]
        _proof(assessment,resource,metadata["batch"],now_ms)
        _proof(_current_assessment(metadata["report_path"],resource,now_ms),resource,metadata["batch"],now_ms)
        return None
    except (OSError,ValueError,TypeError,KeyError,OverflowError,sqlite3.Error) as exc:
        return f"calibrated advice requires regeneration: {exc}"


def _current_assessment(report_path, resource, now_ms):
    # Raw scoring can advance while JSON publication is deferred; query the current ledger.
    path = Path(report_path).parent / "forecast_realized.sqlite3"
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=10)) as db:
        db.execute("BEGIN")
        return activation_assessment(db,now_ms,settings.forecast.archive_retention_days,
                                     resource_ids=(resource["resource_id"],))
