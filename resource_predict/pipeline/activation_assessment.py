"""Conservative, empirical review eligibility; never switches a scaling policy."""
from __future__ import annotations

import json
import logging
import math
from collections import Counter, defaultdict
from itertools import groupby

logger = logging.getLogger(__name__)
RULES = {
    "min_paired_runs": 12,
    "min_transitions": 10,
    "min_matched_targets": 100,
    "min_span_hours": 72,
    "min_observation_coverage": 0.95,
    "max_age_hours": 24,
    "min_reservation_reduction": 0.05,
    "max_exceedance_rate_increase": 0.0,
    "max_mean_excess_increase": 0.0,
    "max_change_rate_increase": 0.0,
    "numeric_tolerance": 1e-12,
}
HOUR_MS = 3600000


def _rows(db, sql, parameters=()):
    cursor = db.execute(sql, parameters)
    names = [column[0] for column in cursor.description]
    for row in cursor:
        yield dict(zip(names, row))


def _identity(budgets):
    identity = []
    for row in budgets:
        provenance = json.loads(row["provenance"])
        if not provenance.get("model_version") or not provenance.get("config_hash"):
            return None
        if row["eligible"] != 1 or row["skip_reason"] is not None or not all(
            isinstance(row[key], (int, float)) and math.isfinite(row[key]) and row[key] > 0
            for key in ("baseline_ratio", "shadow_ratio", "baseline_allocation", "shadow_allocation")
        ):
            return None
        identity.append((row["container"], row["metric"], row["model"], row["metric_unit"], row["basis"],
                         provenance["model_version"], provenance["config_hash"]))
    return tuple(sorted(identity)) or None


def _policy(run):
    baseline, candidate = json.loads(run["baseline"]), json.loads(run["candidate"])
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        return None
    tiers = (baseline.get("policy_tier"), candidate.get("policy_tier"))
    return tiers if all(tiers) else None


def _metric_evidence(db, run_ids, container, metric, now_ms):
    placeholders = ",".join("?" for _ in run_ids)
    sql = (
        "WITH ranked AS (SELECT p.*,b.baseline_ratio,b.shadow_ratio,b.baseline_allocation,b.shadow_allocation,"
        "ROW_NUMBER() OVER (PARTITION BY p.target_ms ORDER BY c.issued_ms DESC,c.batch DESC,c.id DESC) AS rank "
        "FROM shadow_budgets b JOIN curves c ON c.id=b.curve_id JOIN points p ON p.curve_id=c.id "
        f"WHERE b.run_id IN ({placeholders}) AND c.container=? AND c.metric=? "
        "AND p.target_ms<=? AND p.target_ms>c.issued_ms AND p.target_ms>c.data_end_ms AND c.eligible=1), "
        "observations AS (SELECT *,CASE WHEN actual IS NOT NULL AND scored_at_ms<=? "
        "AND ABS(actual)<1e300 AND actual>=0 THEN 1 ELSE 0 END AS matched FROM ranked WHERE rank=1) "
        "SELECT COUNT(*) AS due_targets,COALESCE(SUM(matched),0) AS matched_targets,"
        "MIN(CASE WHEN matched=1 THEN target_ms END) AS first_target_ms,"
        "MAX(CASE WHEN matched=1 THEN target_ms END) AS last_target_ms,"
        "AVG(CASE WHEN matched=1 THEN CASE WHEN actual>baseline_ratio THEN 1.0 ELSE 0 END END) AS baseline_rate,"
        "AVG(CASE WHEN matched=1 THEN CASE WHEN actual>shadow_ratio THEN 1.0 ELSE 0 END END) AS shadow_rate,"
        "AVG(CASE WHEN matched=1 THEN MAX(actual-baseline_ratio,0) END) AS baseline_excess,"
        "AVG(CASE WHEN matched=1 THEN MAX(actual-shadow_ratio,0) END) AS shadow_excess,"
        "AVG(CASE WHEN matched=1 THEN baseline_allocation END) AS baseline_allocation,"
        "AVG(CASE WHEN matched=1 THEN shadow_allocation END) AS shadow_allocation FROM observations"
    )
    result = next(_rows(db, sql, (*run_ids, container, metric, now_ms, now_ms)))
    count, due = result["matched_targets"], result["due_targets"]
    result["observation_coverage"] = count/due if due else 0.0
    result["span_hours"] = ((result["last_target_ms"]-result["first_target_ms"])/HOUR_MS) if count else 0.0
    result["reservation_reduction"] = (1-result["shadow_allocation"]/result["baseline_allocation"]) if count else None
    checks = {
        "sample_count": count >= RULES["min_matched_targets"],
        "time_span": result["span_hours"] >= RULES["min_span_hours"],
        "observation_coverage": result["observation_coverage"] >= RULES["min_observation_coverage"],
        "fresh_observations": bool(count and now_ms-result["last_target_ms"] <= RULES["max_age_hours"]*HOUR_MS),
        "exceedance_rate": bool(count and result["shadow_rate"]-result["baseline_rate"] <= RULES["max_exceedance_rate_increase"]),
        "excess_magnitude": bool(count and result["shadow_excess"]-result["baseline_excess"] <= RULES["max_mean_excess_increase"]+RULES["numeric_tolerance"]),
        "allocation_not_increased": bool(count and result["shadow_allocation"] <= result["baseline_allocation"]+RULES["numeric_tolerance"]),
    }
    return dict(container=container or None, metric=metric, **result, checks=checks,
                failed_checks=[name for name, passed in checks.items() if not passed])


def _assess_resource(db, runs, now_ms):
    latest = runs[-1]
    result = {"resource_id": latest["resource_id"], "resource_type": latest["resource_type"],
              "status": "continue_observing", "reasons": [], "latest_batch": latest["batch"],
              "paired_runs": 0, "metrics": [], "valid_until_epoch_ms": now_ms}
    if latest["status"] != "paired":
        result["reasons"].append("latest_run_not_paired")
        return result
    if not 0 <= now_ms-latest["issued_ms"] <= RULES["max_age_hours"]*HOUR_MS:
        result["reasons"].append("stale_or_future_prediction")
        return result
    snapshot = json.loads(latest["snapshot"])
    if snapshot.get("version") != 1 or snapshot.get("executable") is not False:
        result["reasons"].append("invalid_shadow_snapshot")
        return result
    latest_spec = snapshot.get("source_spec", {})
    if latest["resource_type"] == "k8s_workload":
        expected = {(name, metric) for name in latest_spec.get("containers", {})
                    for metric in ("cpu_request", "cpu_limit", "memory_request", "memory_limit")}
    elif latest["resource_type"] == "openstack_vm":
        expected = {( "", metric) for metric in ("cpu", "memory", "disk")}
    else:
        expected = set()
    budgets_by_run = defaultdict(list)
    for row in _rows(db,
        "SELECT b.*,c.container,c.metric,c.model,c.unit AS metric_unit,c.basis,c.provenance,c.eligible,c.data_end_ms "
        "FROM shadow_runs r JOIN batches batch ON batch.name=r.batch "
        "JOIN shadow_budgets b ON b.run_id=r.id JOIN curves c ON c.id=b.curve_id "
        "WHERE r.resource_id=? AND r.resource_type=? AND batch.issued_ms>=? AND batch.issued_ms<=?",
        (latest["resource_id"],latest["resource_type"],runs[0]["issued_ms"],latest["issued_ms"]),
    ):
        budgets_by_run[row["run_id"]].append(row)
    current = budgets_by_run[latest["id"]]
    result["budget_skip_reasons"] = dict(Counter(b["skip_reason"] for b in current if b["skip_reason"]))
    result["missing_metrics"] = [{"container": c or None,"metric": m}
                                 for c,m in sorted(expected-{(b["container"],b["metric"]) for b in current})]
    identity, policy = _identity(current), _policy(latest)
    if not expected or {(b["container"],b["metric"]) for b in current} != expected or identity is None or policy is None:
        result["reasons"].append("incomplete_or_incomparable_latest_budgets")
        return result
    if any(not isinstance(b["data_end_ms"], int) or not 0 <= now_ms-b["data_end_ms"] <= RULES["max_age_hours"]*HOUR_MS for b in current):
        result["reasons"].append("stale_or_future_forecast_data")
        return result
    comparable = []
    for run in reversed(runs):
        if (run["status"] != "paired" or run["basis"] != latest["basis"] or _policy(run) != policy
                or _identity(budgets_by_run[run["id"]]) != identity):
            break
        comparable.append(run)
    comparable.reverse()
    result["paired_runs"] = len(comparable)
    result["first_batch"] = comparable[0]["batch"]
    if len(comparable) < RULES["min_paired_runs"]:
        result["reasons"].append("insufficient_continuous_paired_runs")
    transitions = len(comparable)-1
    baseline_changes = candidate_changes = 0
    for before, after in zip(comparable, comparable[1:]):
        baseline_changes += before["baseline"] != after["baseline"]
        candidate_changes += before["candidate"] != after["candidate"]
    result["stability"] = {"transitions": transitions, "baseline_changes": baseline_changes,
                           "candidate_changes": candidate_changes,
                           "baseline_change_rate": baseline_changes/transitions if transitions else None,
                           "candidate_change_rate": candidate_changes/transitions if transitions else None}
    if transitions < RULES["min_transitions"]:
        result["reasons"].append("insufficient_transitions")
    if transitions and (candidate_changes-baseline_changes)/transitions > RULES["max_change_rate_increase"]:
        result["reasons"].append("recommendation_changes_increased")
    run_ids = [run["id"] for run in comparable]
    saving = False
    validity = [latest["issued_ms"]+RULES["max_age_hours"]*HOUR_MS,
                min(b["data_end_ms"] for b in current)+RULES["max_age_hours"]*HOUR_MS]
    for budget in sorted(current, key=lambda b: (b["container"],b["metric"])):
        evidence = _metric_evidence(db, run_ids, budget["container"], budget["metric"], now_ms)
        evidence.update(unit=budget["unit"], role=budget["role"])
        result["metrics"].append(evidence)
        if evidence["failed_checks"]:
            result["reasons"].append("metric_checks_failed")
        if evidence["last_target_ms"] is not None:
            validity.append(evidence["last_target_ms"]+RULES["max_age_hours"]*HOUR_MS)
        saving_metric = latest["resource_type"] == "openstack_vm" or budget["role"] == "request_budget"
        saving |= bool(saving_metric and evidence["reservation_reduction"] is not None
                       and evidence["reservation_reduction"]+RULES["numeric_tolerance"] >= RULES["min_reservation_reduction"])
    if not saving:
        result["reasons"].append("insufficient_reservation_benefit")
    result["reasons"] = list(dict.fromkeys(result["reasons"]))
    if not result["reasons"]:
        result["status"] = "eligible_for_review"
        result["valid_until_epoch_ms"] = min(validity)
    return result


def activation_assessment(db, now_ms: int, retention_days: int, *, resource_ids=None) -> dict:
    """Run inside the report's consistent ledger snapshot; one resource at a time."""
    rows = []
    requested = tuple(resource_ids) if resource_ids is not None else None
    resource_filter = (" AND r.resource_id IN ("+",".join("?" for _ in requested)+")") if requested else (" AND 0" if requested is not None else "")
    headers = _rows(db,
        "SELECT r.*,b.issued_ms FROM shadow_runs r JOIN batches b ON b.name=r.batch "
        "WHERE b.issued_ms>=?"+resource_filter+" ORDER BY r.resource_type,r.resource_id,b.issued_ms,r.batch",
        (now_ms-retention_days*86400000,*(requested or ())),
    )
    for _, group in groupby(headers, key=lambda row: (row["resource_type"], row["resource_id"])):
        runs = list(group)
        try:
            rows.append(_assess_resource(db,runs,now_ms))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            logger.warning("[activation_assessment] %s: %s",runs[-1]["resource_id"],exc)
            rows.append({"resource_id": runs[-1]["resource_id"],"resource_type": runs[-1]["resource_type"],
                         "status": "continue_observing", "reasons": ["invalid_evidence"],
                         "valid_until_epoch_ms": now_ms})
    return {"schema_version": 1, "rule_version": "empirical_review_v1", "mode": "review_only",
            "automatic_activation": False, "generated_at_epoch_ms": now_ms,
            "window_start_ms": now_ms-retention_days*86400000, "rules": dict(RULES),
            "status": "evaluated" if rows else "no_shadow_evidence",
            "counts": dict(Counter(row["status"] for row in rows)), "resources": rows}
