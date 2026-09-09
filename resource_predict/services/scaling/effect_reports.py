"""Read-only outcome paging and SQL summaries without loading historical payloads."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from typing import Iterable


DB_NAME = "scaling_effects.sqlite3"
_STATUS = """CASE WHEN status NOT IN
 ('evaluated','failed','interrupted','basis_changed','evidence_conflict','expired')
 AND deadline_ms <= :now THEN 'expired' ELSE status END"""


@contextmanager
def _databases(out_dirs):
    paths = list(dict.fromkeys((Path(directory) / DB_NAME).resolve() for directory in out_dirs))
    paths = [path for path in paths if path.exists()]
    if not paths:
        yield None, []
        return
    db = sqlite3.connect(f"{paths[0].as_uri()}?mode=ro", uri=True)
    try:
        aliases = ["main"]
        for index, path in enumerate(paths[1:], 1):
            alias = f"scope_{index}"
            db.execute(f"ATTACH DATABASE ? AS {alias}", (f"{path.as_uri()}?mode=ro",))
            aliases.append(alias)
        db.execute("PRAGMA query_only=ON")
        yield db, aliases
    finally:
        db.close()


def _where(filters):
    clauses = []
    for field in ("resource_type", "action"):
        if filters.get(field):
            clauses.append(f"{field}=:{field}")
    if filters.get("status"):
        clauses.append(f"({_STATUS})=:status")
    if filters.get("q"):
        clauses.append("(instr(lower(resource_id),lower(:q))>0 OR instr(lower(task_id),lower(:q))>0)")
    if filters.get("from_ms") is not None:
        clauses.append("started_ms>=:from_ms")
    if filters.get("to_ms") is not None:
        clauses.append("started_ms<:to_ms")
    return " WHERE " + " AND ".join(clauses) if clauses else ""


def _candidates(db, aliases, params, limit=None):
    candidates = []
    for alias in aliases:
        sql = f"SELECT task_id,started_ms,{_STATUS} FROM {alias}.events" + _where(params)
        sql += " ORDER BY started_ms DESC,task_id DESC"
        if limit is not None:
            sql += " LIMIT :limit"
        candidates.extend((started, task_id, alias, status) for task_id, started, status in
                          db.execute(sql, {**params, "limit": limit}))
    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return candidates


def _payloads(db, candidates):
    result = []
    for _, task_id, alias, status in candidates:
        payload = db.execute(f"SELECT payload FROM {alias}.events WHERE task_id=?", (task_id,)).fetchone()[0]
        event = json.loads(payload)
        if event.get("status") != status and status == "expired":
            event["reason"] = "真实证据补齐期限已结束，未形成完整成效评估"
        event["status"] = status
        event["metrics"] = [metric for metric in event.get("metrics", []) if not metric.get("container", "")]
        result.append(event)
    return result


def _summary(db, aliases, params):
    # Only indexed identifiers/status and cached aggregate numbers enter these CTEs.
    events = " UNION ALL ".join(
        f"SELECT '{alias}' AS scope_id,task_id,resource_id,resource_type,action,{_STATUS} AS report_status "
        f"FROM {alias}.events" + _where(params) for alias in aliases
    )
    cte = f"WITH filtered_events AS ({events}) "
    count, resources = db.execute(cte + """
        SELECT (SELECT COUNT(*) FROM filtered_events),
        (SELECT COUNT(*) FROM (SELECT DISTINCT resource_type,resource_id FROM filtered_events))
        """, params).fetchone()
    statuses = dict(db.execute(cte + "SELECT report_status,COUNT(*) FROM filtered_events GROUP BY report_status", params))
    cached = " UNION ALL ".join(f"SELECT '{alias}' AS scope_id,* FROM {alias}.metric_summaries" for alias in aliases)
    metric_rows = db.execute(cte.rstrip() + f", cached AS ({cached}) " + """
        SELECT e.resource_type,e.action,m.metric,m.basis,MIN(m.unit),COUNT(*),COUNT(DISTINCT e.resource_id),
               COUNT(m.relative_change_pct),AVG(m.relative_change_pct),SUM(m.before_capacity),
               SUM(m.before_pct*m.before_capacity),SUM(m.after_pct*m.before_capacity),
               SUM(m.reclaimed_capacity),SUM(m.reclaimed_unit_hours)
        FROM filtered_events e JOIN cached m ON e.scope_id=m.scope_id AND e.task_id=m.task_id
        WHERE e.report_status='evaluated' AND m.before_capacity>0
          AND m.before_pct IS NOT NULL AND m.after_pct IS NOT NULL
        GROUP BY e.resource_type,e.action,m.metric,m.basis
        ORDER BY e.resource_type,e.action,m.metric,m.basis
        """, params)
    metrics = []
    for row in metric_rows:
        (resource_type, action, metric, basis, unit, event_count, resource_count, relative_count,
         mean_relative, weight, before_total, after_total, reclaimed, unit_hours) = row
        before, after = before_total / weight, after_total / weight
        metrics.append({
            "resource_type": resource_type, "action": action, "metric": metric, "basis": basis, "unit": unit,
            "event_count": event_count, "resource_count": resource_count, "relative_count": relative_count,
            "before_pct": before, "after_pct": after, "delta_pp": after - before,
            "mean_relative_change_pct": mean_relative,
            "weighted_relative_change_pct": (after - before) / before * 100 if before else None,
            "reclaimed_capacity": reclaimed, "reclaimed_unit_hours": unit_hours,
        })
    return {"event_count": count, "resource_count": resources, "status_counts": statuses, "metrics": metrics}


def list_events(out_dirs: Iterable[Path], *, resource_type: str = "", action: str = "",
                status: str = "", q: str = "", from_ms: int | None = None, to_ms: int | None = None) -> list[dict]:
    """Load all matching aggregate rows for a full CSV export."""
    params = {"resource_type": resource_type, "action": action, "status": status, "q": q,
              "from_ms": from_ms, "to_ms": to_ms, "now": int(time.time() * 1000)}
    with _databases(out_dirs) as (db, aliases):
        if db is None:
            return []
        db.execute("BEGIN")
        return _payloads(db, _candidates(db, aliases, params))


def outcome_report(out_dirs: Iterable[Path], *, page: int = 1, page_size: int = 20, **filters: str) -> dict:
    """Page event identifiers in SQL; deserialize only the selected page's payloads."""
    from resource_predict.services.scaling.effects import POLICY

    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be positive")
    now = int(time.time() * 1000)
    params = {**filters, "now": now}
    with _databases(out_dirs) as (db, aliases):
        summary = {"event_count": 0, "resource_count": 0, "status_counts": {}, "metrics": []}
        items = []
        if db is not None:
            # One read transaction keeps page/count/summary consistent during ingestion.
            db.execute("BEGIN")
            summary = _summary(db, aliases, params)
            offset = (page - 1) * page_size
            candidates = _candidates(db, aliases, params, page * page_size)
            items = _payloads(db, candidates[offset:offset + page_size])
    return {"version": 1, "policy": POLICY, "summary": summary, "items": items,
            "total": summary["event_count"], "page": page, "page_size": page_size, "generated_at_ms": now}
