"""Legacy references and atomic, reproducible accuracy report snapshots."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager

from resource_predict.services.forecast_accuracy import accuracy_session

SUMMARY_FIELDS = ("resource_type", "level", "metric", "model", "unit", "basis_unit", "horizon", "count",
                  "resource_count", "cohort_count", "mae", "rmse", "p95_error", "hit_rate_5pp", "hit_rate_10pp",
                  "macro_hit_rate_5pp", "macro_hit_rate_10pp", "underestimate_rate")
POINT_FIELDS = ("resource_id", "resource_type", "container", "metric", "model", "unit", "basis_unit", "horizon",
                "batch", "issued_ms", "data_end_ms", "target_ms", "predicted", "actual", "error", "abs_error",
                "status", "hit_5pp", "hit_10pp", "observation_source", "basis", "provenance", "evaluation", "skip_reason")
LEGACY_FIELDS = ("resource_id", "resource_type", "container", "metric", "model", "unit", "mae", "rmse", "mape",
                 "p95_error", "evaluation_role", "status", "report_generated_at_ms")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _finite(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


class LegacySession:
    """Preserve original aggregate errors without pretending point evidence exists."""
    def __init__(self, out_dirs, filters):
        self.rows = []
        self.sources = []
        for directory in out_dirs:
            path = Path(directory) / "forecast_error_report.json"
            if not path.exists():
                continue
            report = json.loads(path.read_text(encoding="utf-8"))
            generated = report.get("meta", {}).get("generated_at_epoch_ms")
            for entry in report.get("rows", []):
                if not isinstance(entry, dict):
                    continue
                row = {key: entry.get(key) for key in ("resource_id", "resource_type", "container", "metric", "model")}
                row.update({key: _finite(entry.get(key)) for key in ("mae", "rmse", "mape", "p95_error")})
                row.update(unit="legacy_native", evaluation_role=entry.get("evaluation_role") or "legacy_holdout",
                           status="legacy_unverified", report_generated_at_ms=generated,
                           window=entry.get("window"), provenance=entry.get("provenance"))
                if any(filters.get(key) and row.get(key) != filters[key] for key in ("resource_type", "metric", "model")):
                    continue
                if filters.get("level") and filters["level"] != ("container" if row["container"] else "resource"):
                    continue
                if filters.get("q", "").lower() not in str(row["resource_id"]).lower():
                    continue
                # A report row is not a target point or a forecast horizon. Do not invent temporal filtering.
                if filters.get("horizon") or filters.get("from_ms") is not None or filters.get("to_ms") is not None:
                    continue
                self.rows.append(row)
            self.sources.append({"file": path.name, "scope": Path(directory).name, "generated_at_ms": generated,
                                 "report_rows": len(report.get("rows", []))})
        self.now = int(time.time() * 1000)

    def points(self):
        yield from self.rows

    def report(self, page=1, page_size=50):
        return {
            "version": 1, "source": "legacy", "generated_at_ms": self.now,
            "policy": {"evidence": "aggregate_reference_only", "units": "unverified_original_units"},
            "coverage": {"legacy_report_rows": len(self.rows), "resource_count": len({r["resource_id"] for r in self.rows}),
                         "candidate_points": None, "selected_points": None, "duplicate_points": None,
                         "due_points": None, "matched_points": None, "observation_coverage": None,
                         "status_counts": {"legacy_unverified": len(self.rows)}},
            "summary": [], "items": self.rows[(page-1)*page_size:page*page_size], "total": len(self.rows),
            "page": page, "page_size": page_size, "sources": self.sources,
            "warnings": ["此入口仅参考已有汇总误差，不代表逐点证据或独立测试已被核验。原单位未追认，不能计算±5/±10个百分点达标率。",
                         "旧报告不支持目标时刻或提前量筛选；设置这些筛选时不返回无法核验的记录。"],
        }


@contextmanager
def report_session(out_dirs, *, source="realized", **filters):
    if source == "legacy":
        yield LegacySession(out_dirs, filters)
    else:
        with accuracy_session(out_dirs, source=source, **filters) as session:
            yield session


def csv_cell(value):
    if isinstance(value, (dict, list)):
        value = _json(value)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return "" if value is None else value


def csv_chunks(fields, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(fields)
    yield "\ufeff" + buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    for row in rows:
        writer.writerow([csv_cell(row.get(key)) for key in fields])
        if buffer.tell() >= 65536:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
    if buffer.tell():
        yield buffer.getvalue()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def freeze_report(out_dirs, destination, *, source="realized", **filters):
    """One consistent read session, streaming all rows, then atomic ZIP publication."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    snapshot_id = uuid.uuid4().hex
    final = destination / f"{snapshot_id}.zip"
    with tempfile.TemporaryDirectory(prefix=".accuracy-", dir=destination) as temp:
        work = Path(temp)
        with report_session(out_dirs, source=source, **filters) as session:
            report = session.report()
            report["filters"] = {"source": source, **filters}
            (work / "report.json").write_text(_json(report), encoding="utf-8")
            legacy = source == "legacy"
            records_name = "legacy_rows.jsonl.gz" if legacy else "points.jsonl.gz"
            csv_name = "legacy_rows.csv" if legacy else "points.csv"
            fields = LEGACY_FIELDS if legacy else POINT_FIELDS
            count = 0
            candidate_count = 0
            with gzip.open(work / records_name, "wt", encoding="utf-8", newline="\n") as stream, (work / csv_name).open("w", encoding="utf-8-sig", newline="") as csv_stream:
                writer = csv.writer(csv_stream)
                writer.writerow(fields)
                for row in session.points():
                    stream.write(_json(row) + "\n")
                    writer.writerow([csv_cell(row.get(key)) for key in fields])
                    count += 1
            if not legacy:
                (work / "summary.csv").write_text("".join(csv_chunks(SUMMARY_FIELDS, report["summary"])), encoding="utf-8")
                with gzip.open(work / "candidates.jsonl.gz", "wt", encoding="utf-8", newline="\n") as stream:
                    for row in session.candidates():
                        stream.write(_json(row) + "\n")
                        candidate_count += 1
        manifest = {
            "schema_version": 1, "snapshot_id": snapshot_id, "source": source,
            "generated_at_ms": report["generated_at_ms"], "filters": report["filters"], "policy": report["policy"],
            "record_count": count, "point_count": 0 if legacy else count,
            "candidate_count": candidate_count,
            "files": {path.name: {"sha256": file_hash(path), "bytes": path.stat().st_size} for path in sorted(work.iterdir())},
            "note": "SHA256 verifies package consistency, not an external authenticity signature. Rates are fractions; selected percentage_points values are already multiplied by 100. Candidates preserve native units before deduplication and model filtering.",
        }
        (work / "manifest.json").write_text(_json(manifest), encoding="utf-8")
        temporary_zip = work / "snapshot.zip"
        with zipfile.ZipFile(temporary_zip, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(work.iterdir()):
                if path != temporary_zip:
                    archive.write(path, path.name, compress_type=zipfile.ZIP_STORED if path.suffix == ".gz" else zipfile.ZIP_DEFLATED)
        temporary_zip.replace(final)
    return {"snapshot_id": snapshot_id, "download_url": f"/api/forecast-accuracy/snapshots/{snapshot_id}/download",
            "sha256": file_hash(final), "point_count": manifest["point_count"], "record_count": count}
