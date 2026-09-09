"""Durable, observation-only scaling outcomes; independent of the task history cap."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from resource_predict.services.scaling.effect_metrics import evaluate_series
from resource_predict.services.scaling.effect_reports import list_events as list_events, outcome_report as outcome_report

__all__ = ["start_event", "complete_event", "capture_failure", "ingest_evidence", "try_ingest_evidence",
           "event_evidence", "list_events", "outcome_report"]

logger = logging.getLogger(__name__)
DB_NAME = "scaling_effects.sqlite3"
POLICY = {"version": 1, "before_hours": 24, "after_hours": 24, "stabilization_minutes": 60,
          "min_coverage": 0.8, "max_gap_ms": 900000}
_TERMINAL = {"evaluated", "failed", "interrupted", "basis_changed", "evidence_conflict", "expired"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
 task_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL, resource_type TEXT NOT NULL,
 action TEXT NOT NULL, status TEXT NOT NULL, started_ms INTEGER NOT NULL, payload TEXT NOT NULL,
 deadline_ms INTEGER
);
CREATE INDEX IF NOT EXISTS events_resource ON events(resource_id,started_ms);
CREATE INDEX IF NOT EXISTS events_filter ON events(action,status,started_ms);
CREATE INDEX IF NOT EXISTS events_order ON events(started_ms DESC,task_id DESC);
CREATE TABLE IF NOT EXISTS metric_summaries (
 task_id TEXT NOT NULL REFERENCES events(task_id), metric TEXT NOT NULL, basis TEXT NOT NULL, unit TEXT NOT NULL,
 before_pct REAL, after_pct REAL, relative_change_pct REAL, before_capacity REAL,
 reclaimed_capacity REAL, reclaimed_unit_hours REAL,
 PRIMARY KEY(task_id,metric,basis)
);
CREATE TABLE IF NOT EXISTS batches (
 task_id TEXT NOT NULL REFERENCES events(task_id), hash TEXT NOT NULL, metadata TEXT NOT NULL,
 PRIMARY KEY(task_id,hash)
);
CREATE TABLE IF NOT EXISTS samples (
 task_id TEXT NOT NULL REFERENCES events(task_id), container TEXT NOT NULL, metric TEXT NOT NULL,
 basis TEXT NOT NULL, unit TEXT NOT NULL, ts INTEGER NOT NULL, usage REAL, capacity REAL, batch_hash TEXT NOT NULL,
 PRIMARY KEY(task_id,container,metric,basis,ts)
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> int:
    return int(time.time() * 1000)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@contextmanager
def _connect(out_base: Path):
    Path(out_base).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(Path(out_base) / DB_NAME, timeout=30)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(_SCHEMA)
        if "deadline_ms" not in {row[1] for row in db.execute("PRAGMA table_info(events)")}:
            db.execute("ALTER TABLE events ADD COLUMN deadline_ms INTEGER")
        with db:
            yield db
    finally:
        db.close()


def _target(before: dict, target: dict, kind: str) -> dict:
    result = json.loads(_json(before))
    if kind == "k8s_workload":
        containers = result.setdefault("containers", {})
        incoming = target.get("containers", {})
        if not incoming and len(containers) == 1:
            incoming = {next(iter(containers)): {key: value for key, value in target.items()
                        if key in {"cpu_request_cores", "cpu_limit_cores", "memory_request_gb", "memory_limit_gb"}}}
        for name, values in incoming.items():
            containers.setdefault(name, {}).update(values)
        if target.get("replicas") is not None:
            result["replicas"] = target["replicas"]
            result["replicas_observed"] = target["replicas"]
    else:
        result.update(target)
    return result


def _capacities(spec: dict, kind: str) -> dict[tuple[str, str, str], float]:
    result = {}
    if kind == "openstack_vm":
        for metric, field in (("cpu", "cpu_cores"), ("memory", "memory_gb")):
            value = _number(spec.get(field))
            if value is not None and value > 0:
                result[("", metric, "capacity")] = value
        return result
    replicas = _number(spec.get("replicas") or spec.get("replicas_observed"))
    if replicas is None or replicas <= 0:
        return result
    for name, values in spec.get("containers", {}).items():
        if not isinstance(values, dict):
            continue
        for metric, suffix in (("cpu", "cores"), ("memory", "gb")):
            for basis in ("request", "limit"):
                value = _number(values.get(f"{metric}_{basis}_{suffix}"))
                if value is not None and value > 0:
                    result[(str(name), metric, basis)] = value * replicas
    return result


def _action(before: dict, after: dict, kind: str) -> str:
    old, new = _capacities(before, kind), _capacities(after, kind)
    differences = [new[key] - old[key] for key in old.keys() & new.keys()]
    up = any(value > 1e-9 for value in differences)
    down = any(value < -1e-9 for value in differences)
    return "mixed" if up and down else "scale_out" if up else "scale_in" if down else "unknown"


def _spec_matches(observed: dict, target: dict, kind: str) -> bool:
    """Replica count and each container field must match, not just their products."""
    if kind == "openstack_vm":
        fields = ("cpu_cores", "memory_gb", "disk_gb")
        pairs = [(observed.get(field), target[field]) for field in fields if target.get(field) is not None]
    else:
        expected = target.get("containers", {})
        actual = observed.get("containers", {})
        if set(actual) != set(expected):
            return False
        pairs = [(observed.get("replicas") or observed.get("replicas_observed"),
                  target.get("replicas") or target.get("replicas_observed"))]
        for name, values in expected.items():
            for field in ("cpu_request_cores", "cpu_limit_cores", "memory_request_gb", "memory_limit_gb"):
                if values.get(field) is not None:
                    pairs.append((actual[name].get(field), values[field]))
    return bool(pairs) and all(_number(left) is not None and _number(right) is not None
                               and math.isclose(float(left), float(right), rel_tol=1e-6, abs_tol=1e-9)
                               for left, right in pairs)


def _save(db: sqlite3.Connection, event: dict) -> None:
    if event["status"] in _TERMINAL - {"evaluated"}:
        for metric in event.get("metrics", []):
            metric["status"] = event["status"]
            for field in ("delta_pp", "relative_change_pct", "reclaimed_capacity", "capacity_reduction_pct", "reclaimed_unit_hours"):
                metric[field] = None
    db.execute("UPDATE events SET status=?,payload=? WHERE task_id=?",
               (event["status"], _json(event), event["task_id"]))
    db.execute("DELETE FROM metric_summaries WHERE task_id=?", (event["task_id"],))
    if event["status"] == "evaluated":
        db.executemany("INSERT INTO metric_summaries VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (event["task_id"], metric["metric"], metric["basis"], metric["unit"],
             metric["before"]["utilization_pct"], metric["after"]["utilization_pct"], metric["relative_change_pct"],
             metric["before"]["mean_capacity"], metric["reclaimed_capacity"], metric["reclaimed_unit_hours"])
            for metric in event["metrics"] if not metric["container"] and metric["status"] == "evaluated"
        ])


def start_event(out_base: Path, task: dict, before_spec: dict, target_spec: dict,
                *, evidence: dict | None = None, now_ms: int | None = None, policy: dict | None = None) -> None:
    if task.get("mode") != "execute":
        return
    now = _now() if now_ms is None else now_ms
    kind = str(task["resource_type"])
    resolved = _target(before_spec, target_spec, kind)
    rules = {**POLICY, **(policy or {})}
    if kind == "k8s_workload":
        rules["expected_containers"] = sorted(before_spec.get("containers", {}))
    event = {
        "task_id": str(task["task_id"]), "resource_id": str(task["resource_id"]), "resource_type": kind,
        "action": _action(before_spec, resolved, kind), "status": "executing", "reason": "等待命令完成及实测生效",
        "started_at_ms": now, "completed_at_ms": None, "effective_at_ms": None,
        "before_spec": before_spec, "target_spec": resolved, "after_spec": None,
        "policy": rules, "metrics": [], "computed_at_ms": now,
        "deadline_ms": now + int((rules["after_hours"] + rules["stabilization_minutes"] / 60 + 7 * 24) * 3600000),
    }
    with _connect(out_base) as db:
        if db.execute("SELECT 1 FROM events WHERE task_id=?", (event["task_id"],)).fetchone():
            return
        # Any new real execution interrupts a previous still-open outcome, even if it fails later.
        for row in db.execute("SELECT payload FROM events WHERE resource_id=?", (event["resource_id"],)).fetchall():
            previous = json.loads(row[0])
            previous_effective = previous.get("effective_at_ms")
            previous_rules = previous["policy"]
            previous_end = (previous_effective + int((previous_rules["after_hours"] + previous_rules["stabilization_minutes"] / 60) * 3600000)
                            if previous_effective is not None else None)
            if previous["status"] not in _TERMINAL and (previous_end is None or now < previous_end):
                previous.update(status="interrupted", interrupted_at_ms=now, reason="观察窗口内再次调配")
                _save(db, previous)
        db.execute("INSERT INTO events(task_id,resource_id,resource_type,action,status,started_ms,payload,deadline_ms) VALUES (?,?,?,?,?,?,?,?)", (
            event["task_id"], event["resource_id"], kind, event["action"], event["status"], now, _json(event), event["deadline_ms"]))
    # A malformed baseline must not roll back the durable execution event.
    if isinstance(evidence, dict):
        try:
            ingest_evidence(out_base, [{"resource_id": event["resource_id"], "scaling_evidence": evidence}], now_ms=now)
        except Exception as exc:
            capture_failure(out_base, event["task_id"], str(exc))


def capture_failure(out_base: Path, task_id: str, error: str) -> None:
    with _connect(out_base) as db:
        row = db.execute("SELECT payload FROM events WHERE task_id=?", (task_id,)).fetchone()
        if row:
            event = json.loads(row[0])
            event.update(status="capture_failed", capture_error=error, reason="证据采集失败；执行记录保留，不能证明成效")
            _save(db, event)


def complete_event(out_base: Path, task_id: str, *, success: bool, now_ms: int | None = None) -> None:
    if not (Path(out_base) / DB_NAME).exists():
        return
    now = _now() if now_ms is None else now_ms
    with _connect(out_base) as db:
        row = db.execute("SELECT payload FROM events WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return
        event = json.loads(row[0])
        if event.get("completed_at_ms") is not None:
            return
        event.update(completed_at_ms=now, command_success=success)
        if event["status"] not in _TERMINAL:
            event.update(status="awaiting_effective" if success else "failed",
                         reason="命令完成，等待监控确认目标容量" if success else "调配失败，不计入利用率提升")
        _save(db, event)


def _rows(evidence: dict, now: int) -> list[tuple]:
    if evidence.get("schema_version") != 1 or not evidence.get("source") or "mock" in str(evidence["source"]).lower():
        return []
    collected = evidence.get("collected_at_ms")
    if not isinstance(collected, int) or collected > now:
        return []
    rows = []
    for series in evidence.get("series", []):
        metric, basis, unit = series.get("metric"), series.get("basis"), series.get("unit")
        if metric not in {"cpu", "memory"} or basis not in {"capacity", "request", "limit"}:
            continue
        if unit != ("cores" if metric == "cpu" else "GiB"):
            continue
        times, usages, capacities = (series.get(key, []) for key in ("timestamps", "usage", "capacity"))
        if len(times) != len(usages) or len(times) != len(capacities):
            raise ValueError("scaling evidence arrays are not aligned")
        for ts, usage, capacity in zip(times, usages, capacities):
            if not isinstance(ts, int) or isinstance(ts, bool) or ts > min(now, collected) or ts < 0:
                continue
            u, c = _number(usage), _number(capacity)
            if u is None or c is None or u < 0 or c <= 0:
                u = c = None
            rows.append((str(series.get("container") or ""), metric, basis, unit, ts, u, c))
    return rows


def _series(db: sqlite3.Connection, task_id: str) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for container, metric, basis, unit, ts, usage, capacity, source_id in db.execute(
        "SELECT container,metric,basis,unit,ts,usage,capacity,batch_hash FROM samples WHERE task_id=? ORDER BY ts", (task_id,)
    ):
        key = (container, metric, basis, unit)
        group = groups.setdefault(key, dict(container=container, metric=metric, basis=basis, unit=unit,
                                            timestamps=[], usage=[], capacity=[], source_ids=[]))
        group["timestamps"].append(ts)
        group["usage"].append(usage)
        group["capacity"].append(capacity)
        group["source_ids"].append(source_id)
    return list(groups.values())


def _ingest(db: sqlite3.Connection, event: dict, evidence: dict, now: int) -> None:
    points = _rows(evidence, now)
    event["evidence_diagnostics"] = {key: evidence[key] for key in (
        "source", "collected_at_ms", "query_errors", "limitations", "provenance") if key in evidence}
    if not points:
        _save(db, event)
        return
    start, rules = event["started_at_ms"], event["policy"]
    low = start - int(rules["before_hours"] * 3600000) - int(rules["max_gap_ms"])
    effective = event.get("effective_at_ms")
    high = (effective or now) + int((rules["after_hours"] + rules["stabilization_minutes"] / 60) * 3600000) + int(rules["max_gap_ms"])
    points = [p for p in points if low <= p[4] <= high]
    old = _capacities(event["before_spec"], event["resource_type"])
    target = _capacities(event["target_spec"], event["resource_type"])
    spec_matches = bool(target) and _spec_matches(evidence.get("spec", {}), event["target_spec"], event["resource_type"])
    completed = event.get("completed_at_ms")
    if completed is not None and event["status"] != "failed" and effective is None and spec_matches:
        matches: dict[int, set] = {}
        for container, metric, basis, _unit, ts, usage, capacity in points:
            key = (container, metric, basis)
            if ts >= completed and usage is not None and key in target and math.isclose(capacity, target[key], rel_tol=1e-6):
                matches.setdefault(ts, set()).add(key)
        confirmed = [ts for ts, keys in matches.items() if keys >= target.keys()]
        if confirmed:
            effective = min(confirmed)
            event.update(effective_at_ms=effective, after_spec=evidence["spec"], status="observing",
                         reason="已由同期真实用量与容量确认生效，收集观察窗口")
            event["effective_confirmation"] = {
                "timestamp_ms": effective, "source": evidence["source"], "collected_at_ms": evidence["collected_at_ms"],
                "points": [dict(container=p[0], metric=p[1], basis=p[2], unit=p[3], usage=p[5], capacity=p[6])
                           for p in points if p[4] == effective and (p[0], p[1], p[2]) in target],
            }
    post_start = effective + int(rules["stabilization_minutes"] * 60000) if effective is not None else None
    post_end = post_start + int(rules["after_hours"] * 3600000) if post_start is not None else None
    existing = {(r[0], r[1], r[2], r[3]): (r[4], r[5]) for r in db.execute(
        "SELECT container,metric,basis,ts,usage,capacity FROM samples WHERE task_id=?", (event["task_id"],))}
    kept = []
    for point in points:
        container, metric, basis, unit, ts, usage, capacity = point
        key = (container, metric, basis)
        if event["resource_type"] == "openstack_vm" and (container or basis != "capacity"):
            continue
        if event["resource_type"] == "k8s_workload" and (not container or basis == "capacity"):
            continue
        if ts <= start:
            previous = existing.get((container, metric, basis, ts))
            if evidence["collected_at_ms"] > start and previous and previous[0] is not None:
                # Current owner maps can lose departed replicas. Never rewrite a frozen baseline with that history.
                continue
            expected = old.get(key)
        elif post_start is not None and post_start <= ts <= post_end + rules["max_gap_ms"]:
            expected = target.get(key)
        else:
            continue
        if expected is None:
            continue
        if capacity is not None and not math.isclose(capacity, expected, rel_tol=1e-6):
            if ts <= start and evidence["collected_at_ms"] > start:
                # Incomplete later reconstruction is missing evidence, not proof of an earlier capacity change.
                continue
            if (start - rules["before_hours"] * 3600000 <= ts < start) or (post_start is not None and post_start <= ts < post_end):
                event.update(status="basis_changed", reason="评估窗口内真实容量发生额外变化，无法归因于本次调配")
            continue
        kept.append(point)
    metadata = {k: v for k, v in evidence.items() if k != "series"}
    changed = []
    for container, metric, basis, unit, ts, usage, capacity in kept:
        previous = existing.get((container, metric, basis, ts))
        if previous and previous[0] is not None and usage is not None and not (
            math.isclose(previous[0], usage, rel_tol=1e-6, abs_tol=1e-9) and math.isclose(previous[1], capacity, rel_tol=1e-6)
        ):
            event.update(status="evidence_conflict", reason="同一时点收到不同的有效实测值，需核查证据")
        if previous is None or (previous[0] is None and usage is not None):
            changed.append((container, metric, basis, unit, ts, usage, capacity))
    # Batch ids link sample provenance; only the complete export's SHA256 is a verification hash.
    digest = hashlib.sha256(_json({"metadata": metadata, "points": changed}).encode("utf-8")).hexdigest()
    if changed:
        db.execute("INSERT OR IGNORE INTO batches VALUES (?,?,?)", (event["task_id"], digest, _json(metadata)))
    db.executemany(
        "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id,container,metric,basis,ts) "
        "DO UPDATE SET usage=excluded.usage,capacity=excluded.capacity,batch_hash=excluded.batch_hash "
        "WHERE samples.usage IS NULL AND excluded.usage IS NOT NULL",
        [(event["task_id"], *p, digest) for p in changed],
    )
    event["computed_at_ms"] = now
    if effective is not None:
        event["metrics"] = evaluate_series(_series(db, event["task_id"]), started_at_ms=start,
                                           effective_at_ms=effective, policy=rules, now_ms=now)
        if event["status"] not in _TERMINAL:
            if now < post_end:
                event.update(status="observing", reason="调配后观察窗口尚未结束")
            elif {(m["metric"], m["basis"]) for m in event["metrics"] if m["status"] == "evaluated" and not m["container"]} >= {
                (metric, basis) for _, metric, basis in target
            }:
                event.update(status="evaluated", reason="前后实测窗口已评估；属于观察性变化，不保证因果关系")
            else:
                event.update(status="insufficient_data", reason="前后有效覆盖率不足，等待真实数据补齐")
    _save(db, event)


def ingest_evidence(out_base: Path, items: Iterable[dict], *, now_ms: int | None = None) -> None:
    if not (Path(out_base) / DB_NAME).exists():
        return
    now = _now() if now_ms is None else now_ms
    with _connect(out_base) as db:
        expired = db.execute("SELECT payload FROM events WHERE deadline_ms<=? AND status NOT IN ('evaluated','failed','interrupted','basis_changed','evidence_conflict','expired')", (now,)).fetchall()
        for row in expired:
            event = json.loads(row[0])
            event.update(status="expired", reason="已超过观察窗口及7天补齐期，证据不足；保留现有记录供审计")
            _save(db, event)
        pending: dict[str, list] = {}
        for row in db.execute("SELECT payload FROM events WHERE status NOT IN ('evaluated','failed','interrupted','basis_changed','evidence_conflict','expired')"):
            event = json.loads(row[0])
            pending.setdefault(event["resource_id"], []).append(event)
        for item in items:
            evidence = item.get("scaling_evidence")
            if not isinstance(evidence, dict):
                continue
            for event in pending.get(str(item.get("resource_id")), []):
                db.execute("SAVEPOINT effect_evidence")
                try:
                    _ingest(db, event, evidence, now)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    db.execute("ROLLBACK TO effect_evidence")
                    saved = json.loads(db.execute("SELECT payload FROM events WHERE task_id=?", (event["task_id"],)).fetchone()[0])
                    saved.update(status="capture_failed", capture_error=str(exc), reason="本次证据格式错误，其他资源继续评估")
                    _save(db, saved)
                    logger.warning("[scaling_effects] rejected evidence for %s: %s", event["task_id"], exc)
                finally:
                    db.execute("RELEASE effect_evidence")


def try_ingest_evidence(out_base: Path, items: Iterable[dict]) -> None:
    try:
        ingest_evidence(out_base, items)
    except Exception:
        logger.exception("[scaling_effects] evidence update failed; existing outcome evidence preserved")


def event_evidence(out_dirs: Iterable[Path], task_id: str) -> dict | None:
    for out_dir in out_dirs:
        path = Path(out_dir) / DB_NAME
        if not path.exists():
            continue
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
            row = db.execute("SELECT payload FROM events WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                continue
            event = json.loads(row[0])
            event["series"] = _series(db, task_id)
            if event["status"] not in _TERMINAL and event.get("deadline_ms", _now()+1) <= _now():
                event.update(status="expired", reason="已超过观察窗口及7天补齐期，证据不足")
            event["evidence_sources"] = [{"batch_id": digest, **json.loads(metadata)} for digest, metadata in db.execute(
                "SELECT hash,metadata FROM batches WHERE task_id=? ORDER BY hash", (task_id,))]
            payload = {"schema_version": 1, "event": event}
            payload["sha256"] = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
            return payload
    return None
