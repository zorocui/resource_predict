"""Read-only, transaction-consistent accuracy summaries and streaming evidence."""
from contextlib import contextmanager
import json
import math
from pathlib import Path
from resource_predict.sqlite_runtime import sqlite3
import time

DB_NAME = "forecast_realized.sqlite3"
STATUSES = (
    "matched", "awaiting_target", "awaiting_observation", "missing_provenance",
    "not_future_at_publication", "nonfinite_observation", "basis_mismatch",
    "unsupported_unit",
    "invalid_prediction",
)
_GROUP = "resource_type,level,metric,model,unit,basis_unit,horizon"
_FIELDS = ("resource_id,resource_type,level,container,metric,model,unit,basis_unit,horizon,"
           "batch,issued_ms,target_ms,data_end_ms,predicted,actual,error,abs_error,status,"
           "hit_5pp,hit_10pp,observation_source,basis,provenance,evaluation,skip_reason")


def _unit(unit):
    if unit == "openstack_vm:ratio" or unit in {
        "k8s_workload:cpu_usage/cpu_limit", "k8s_workload:cpu_usage/cpu_request",
        "k8s_workload:memory_working_set/memory_limit",
        "k8s_workload:memory_working_set/memory_request",
    }:
        return "percentage_points"
    if unit in {"cores", "k8s_workload:cpu_usage_cores"}:
        return "cores"
    if unit in {"GiB", "k8s_workload:memory_working_set_gb"}:
        return "GiB"
    return "unsupported"


def _resource_type(basis, unit):
    try:
        value = json.loads(basis)
        if isinstance(value, list) and value and value[0] in {"openstack_vm", "k8s_workload"}:
            return value[0]
    except (TypeError, ValueError):
        pass
    prefix = str(unit).split(":", 1)[0]
    return prefix if prefix in {"openstack_vm", "k8s_workload"} else "unknown"


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


class AccuracySession:
    def __init__(self, db, unions, source, filters, warnings):
        self.db, self.source, self.warnings = db, source, warnings
        self.now = int(filters.pop("now_ms", time.time() * 1000))
        self.params = dict(filters, now=self.now)
        self.sql = ""
        if unions:
            self._query(unions, filters)
            self.sql = " "

    def _query(self, unions, filters):
        clauses = []
        for key in ("resource_type", "level", "metric", "horizon"):
            if filters.get(key):
                clauses.append(f"{key}=:{key}")
        if filters.get("q"):
            clauses.append("accuracy_contains(resource_id,:q)")
        if filters.get("from_ms") is not None:
            clauses.append("target_ms>=:from_ms")
        if filters.get("to_ms") is not None:
            clauses.append("target_ms<:to_ms")
        where = " AND ".join(clauses) or "1"
        model_filter = " AND model=:model" if filters.get("model") else ""
        holdout = self.source == "holdout"
        origin = "data_end_ms" if holdout else "issued_ms"
        partition_model = ",model" if holdout else ""
        order = "data_end_ms DESC," if holdout else ""
        provenance = ("eligible<>1 OR data_end_ms IS NULL OR "
                      "COALESCE(evaluation_role,'')"
                      "<>'independent_test'" if holdout else "eligible<>1 OR data_end_ms IS NULL OR issued_ms IS NULL")
        future = "target_ms<=data_end_ms" if holdout else "target_ms<=issued_ms OR target_ms<=data_end_ms"
        source_check = "0" if holdout else "observation_source IS NULL OR trim(observation_source)='' OR lower(observation_source) LIKE '%mock%'"
        self.db.execute(f"CREATE TEMP VIEW raw AS {' UNION ALL '.join(unions)}")
        self.db.execute(f"""CREATE TEMP VIEW typed AS SELECT *,
          CASE WHEN container='' THEN 'resource' ELSE 'container' END AS level,
          accuracy_unit(basis_unit) AS unit,
          CASE WHEN {origin} IS NULL OR target_ms-{origin}<=0 THEN 'unknown'
           WHEN target_ms-{origin}<=3600000 THEN '0-1h'
           WHEN target_ms-{origin}<=21600000 THEN '1-6h'
           WHEN target_ms-{origin}<=86400000 THEN '6-24h' ELSE '>24h' END AS horizon FROM raw""")
        self.db.execute(f"CREATE TEMP TABLE candidates AS SELECT * FROM typed WHERE {where}", self.params)
        self.db.execute("CREATE INDEX candidate_key ON candidates(db_index,id,target_ms)")
        self.db.execute("CREATE TEMP TABLE chosen_keys(db_index INTEGER,id INTEGER,target_ms INTEGER)")
        cohort = f"resource_type,resource_id,container,metric,basis_unit,target_ms,horizon{partition_model}"
        key_count = len(cohort.split(','))
        cursor = self.db.execute(f"SELECT {cohort},db_index,id,target_ms FROM candidates ORDER BY {cohort},{order}issued_ms DESC,batch DESC,id DESC,db_index DESC")
        previous, pending = None, []
        for row in cursor:
            key = tuple(row[:key_count])
            if key != previous:
                pending.append(tuple(row[key_count:]))
                previous = key
                if len(pending) == 1000:
                    self.db.executemany("INSERT INTO chosen_keys VALUES (?,?,?)", pending)
                    pending.clear()
        if pending:
            self.db.executemany("INSERT INTO chosen_keys VALUES (?,?,?)", pending)
        self.db.execute(f"""CREATE TEMP TABLE chosen AS SELECT c.* FROM chosen_keys k
            JOIN candidates c ON c.db_index=k.db_index AND c.id=k.id AND c.target_ms=k.target_ms
            WHERE 1{model_filter}""", self.params)
        self.db.execute(f"""CREATE TEMP VIEW classified AS SELECT *,CASE
          WHEN {provenance} THEN 'missing_provenance'
          WHEN {future} THEN 'not_future_at_publication'
          WHEN unit='unsupported' THEN 'unsupported_unit'
          WHEN target_ms>{self.now} THEN 'awaiting_target'
          WHEN skip_reason='invalid_prediction' OR NOT accuracy_finite(raw_predicted) THEN 'invalid_prediction'
          WHEN skip_reason='invalid_observation' THEN 'nonfinite_observation'
          WHEN skip_reason IN ('missing_provenance','not_future_at_publication','nonfinite_observation','basis_mismatch') THEN skip_reason
          WHEN raw_actual IS NULL THEN '{"nonfinite_observation" if holdout else "awaiting_observation"}'
          WHEN NOT accuracy_finite(raw_actual) THEN 'nonfinite_observation'
          WHEN {source_check} THEN 'awaiting_observation'
          ELSE 'matched' END AS status FROM chosen""")
        self.db.execute("""CREATE TEMP VIEW presented AS SELECT *,
          CASE WHEN accuracy_finite(raw_predicted) THEN raw_predicted*CASE WHEN unit='percentage_points' THEN 100.0 ELSE 1.0 END END AS predicted,
          CASE WHEN accuracy_finite(raw_actual) THEN raw_actual*CASE WHEN unit='percentage_points' THEN 100.0 ELSE 1.0 END END AS actual,
          CASE WHEN status='matched' THEN (raw_predicted-raw_actual)*CASE WHEN unit='percentage_points' THEN 100.0 ELSE 1.0 END END AS error,
          CASE WHEN status='matched' AND unit='percentage_points' THEN abs(raw_predicted-raw_actual)<=0.050000000000001 END AS hit_5pp,
          CASE WHEN status='matched' AND unit='percentage_points' THEN abs(raw_predicted-raw_actual)<=0.100000000000001 END AS hit_10pp FROM classified""")
        self.db.execute("CREATE TEMP TABLE selected AS SELECT *,abs(error) AS abs_error FROM presented")
        # CTAS insertion order makes rowids contiguous within each error group.
        self.db.execute(f"""CREATE TEMP TABLE errors AS SELECT {_GROUP},resource_id,container,
            error,abs_error,hit_5pp,hit_10pp FROM selected WHERE status='matched' ORDER BY {_GROUP},abs_error""")
        # JSON evidence belongs to curves, not every forecast point. Join it only
        # during export, after indexed ordering has avoided sorting large blobs.
        for table, order_fields in (
            ("selected", "target_ms,resource_type,resource_id,container,metric,basis_unit,model,batch"),
            ("candidates", "target_ms,resource_type,resource_id,container,metric,basis_unit,model,issued_ms,batch"),
        ):
            self.db.execute(f"CREATE INDEX {table}_order ON {table}({order_fields})")
            self.db.execute(f"""CREATE TEMP VIEW {table}_export AS
                SELECT p.*,m.basis,m.provenance,m.evaluation FROM {table} p
                JOIN curve_metadata m ON m.db_index=p.db_index AND m.id=p.id""")

    def _rows(self, sql, params=None):
        return self.db.execute(self.sql + sql, params or self.params)

    def points(self):
        """Yield every selected row; never materialize the evidence set in Python."""
        if self.sql:
            for row in self._rows(f"SELECT {_FIELDS} FROM selected_export ORDER BY target_ms,resource_type,resource_id,container,metric,basis_unit,model,batch"):
                yield dict(row)

    def candidates(self):
        """Stream the pre-selection universe so snapshot consumers can audit deduplication."""
        if self.sql:
            fields = """resource_id,resource_type,level,container,metric,model,basis_unit,horizon,batch,
                issued_ms,target_ms,data_end_ms,eligible,skip_reason,observation_source,basis,provenance,evaluation,
                accuracy_finite(raw_predicted) AS predicted_finite,accuracy_finite(raw_actual) AS actual_finite,
                CASE WHEN accuracy_finite(raw_predicted) THEN raw_predicted END AS predicted_native,
                CASE WHEN accuracy_finite(raw_actual) THEN raw_actual END AS actual_native"""
            for row in self._rows(f"SELECT {fields} FROM candidates_export ORDER BY target_ms,resource_type,resource_id,container,metric,basis_unit,model,issued_ms,batch"):
                yield dict(row)

    def report(self, page=1, page_size=50):
        page, page_size = max(1, int(page)), max(1, min(500, int(page_size)))
        coverage = dict.fromkeys(STATUSES, 0)
        coverage.update(candidate_points=0, selected_points=0, duplicate_points=0,
                        due_points=0, matched_points=0, observation_coverage=None)
        summary, items, total = [], [], 0
        if self.sql:
            # Candidate/duplicate counts precede model filtering, matching dedup policy.
            candidate, dedup = self._rows("SELECT (SELECT count(*) FROM candidates),(SELECT count(*) FROM chosen_keys)").fetchone()
            for row in self._rows("SELECT status,count(*) AS n,sum(target_ms<=:now) AS due FROM selected GROUP BY status"):
                coverage[row["status"]] = row["n"]
                coverage["due_points"] += row["due"]
                total += row["n"]
            matched = coverage["matched"]
            coverage.update(candidate_points=candidate, duplicate_points=candidate-dedup,
                            model_filtered_points=dedup-total,
                            selected_points=total, matched_points=matched,
                            observation_coverage=matched/coverage["due_points"] if coverage["due_points"] else None)
            sql = f"""SELECT aggregates.*,percentile.abs_error AS p95_error,
              cohort_count,macro_hit_rate_5pp,macro_hit_rate_10pp FROM
              (SELECT {_GROUP},count(*) AS count,count(DISTINCT resource_id) AS resource_count,
                avg(abs_error) AS mae,accuracy_sqrt(avg(error*error)) AS rmse,
                min(rowid)+(95*count(*)+99)/100-1 AS percentile_rowid,
                avg(hit_5pp) AS hit_rate_5pp,avg(hit_10pp) AS hit_rate_10pp,
                avg(CASE WHEN error<0 THEN 1.0 ELSE 0.0 END) AS underestimate_rate
                FROM errors GROUP BY {_GROUP}) aggregates
              JOIN (SELECT {_GROUP},count(*) AS cohort_count,avg(h5) AS macro_hit_rate_5pp,
                avg(h10) AS macro_hit_rate_10pp FROM
                (SELECT {_GROUP},resource_id,container,avg(hit_5pp) AS h5,avg(hit_10pp) AS h10
                FROM errors GROUP BY {_GROUP},resource_id,container) cohorts GROUP BY {_GROUP}) macros
              USING ({_GROUP}) JOIN errors percentile ON percentile.rowid=aggregates.percentile_rowid
              ORDER BY {','.join('aggregates.'+key for key in _GROUP.split(','))}"""
            for row in self._rows(sql):
                item = dict(row)
                item.pop("percentile_rowid")
                summary.append(item)
            params = dict(self.params, limit=page_size, offset=(page-1)*page_size)
            items = [dict(row) for row in self._rows(f"SELECT {_FIELDS} FROM selected_export ORDER BY target_ms,resource_type,resource_id,container,metric,basis_unit,model,batch LIMIT :limit OFFSET :offset", params)]
        coverage["status_counts"] = {key: coverage[key] for key in STATUSES}
        return dict(version=1, source=self.source, generated_at_ms=self.now,
                    policy=dict(tolerance="inclusive ±5/±10 percentage points; not relative percent",
                                p95="nearest_rank", horizon_origin="data_end_ms" if self.source == "holdout" else "issued_ms",
                                dedup="latest training cutoff per model" if self.source == "holdout" else "latest publication before model filtering",
                                error="predicted-actual", macro="equal resource/container cohorts",
                                candidate_counts="before model filtering", units="percentage_points, cores, GiB"),
                    coverage=coverage, summary=summary, items=items, total=total,
                    page=page, page_size=page_size, warnings=self.warnings)


@contextmanager
def accuracy_session(out_dirs, *, source="realized", **filters):
    """Use existing ledgers only, keeping all reads in a single read transaction."""
    if source not in {"realized", "holdout"}:
        raise ValueError("accuracy source must be realized or holdout")
    warnings = (["Independent tests use preprocessed historical data, not production observations."]
                if source == "holdout" else ["issued_ms is an archive/generation-time proxy, not confirmed frontend publication time.",
                                              "Realized evidence evaluates selected models; it is not a fair all-model comparison."])
    db = sqlite3.connect(":memory:", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA temp_store=FILE")
    db.create_function("accuracy_type", 2, _resource_type)
    db.create_function("accuracy_unit", 1, _unit)
    db.create_function("accuracy_finite", 1, _finite)
    db.create_function("accuracy_sqrt", 1, math.sqrt)
    db.create_function("accuracy_contains", 2, lambda value, query: str(query).lower() in str(value).lower())
    try:
        unions, metadata_unions = [], []
        paths = dict.fromkeys((Path(directory) / DB_NAME).resolve() for directory in out_dirs)
        for index, path in enumerate(paths):
            if not path.is_file():
                continue
            alias = f"ledger_{index}"
            db.execute(f"ATTACH DATABASE ? AS {alias}", (path.as_uri()+"?mode=ro",))
            tables = {row[0] for row in db.execute(f"SELECT name FROM {alias}.sqlite_master WHERE type='table'")}
            curves, points = ("holdout_curves", "holdout_points") if source == "holdout" else ("curves", "points")
            if not {curves, points} <= tables:
                warnings.append(f"{source} evidence tables are absent in an existing ledger.")
                continue
            observation = "'preprocessed_history'" if source == "holdout" else "p.observation_source"
            evaluation = "c.evaluation" if source == "holdout" else "'{}'"
            metadata_unions.append(f"SELECT {index} AS db_index,c.id,c.basis,c.provenance,"
                                   f"{evaluation} AS evaluation,accuracy_type(c.basis,c.unit) AS resource_type,"
                                   f"rp_json_field({evaluation},'role') AS evaluation_role FROM {alias}.{curves} c")
            unions.append(f"SELECT {index} AS db_index,c.id,c.batch,c.resource_id,c.container,c.metric,c.model,"
                          f"c.unit AS basis_unit,c.data_end_ms,c.issued_ms,c.eligible,m.resource_type,m.evaluation_role,"
                          f"p.target_ms,p.predicted AS raw_predicted,p.actual AS raw_actual,p.skip_reason,"
                          f"{observation} AS observation_source "
                          f"FROM {alias}.{curves} c JOIN {alias}.{points} p ON p.curve_id=c.id "
                          f"JOIN curve_metadata m ON m.db_index={index} AND m.id=c.id")
        db.execute("BEGIN")
        # Establish every attached ledger's read snapshot before consumers export.
        for index, path in enumerate(paths):
            if path.is_file():
                db.execute(f"SELECT count(*) FROM ledger_{index}.sqlite_master").fetchone()
        if not unions:
            warnings.append("No archived point evidence is available; historical reports are not backfilled.")
        else:
            db.execute(f"CREATE TEMP TABLE curve_metadata AS {' UNION ALL '.join(metadata_unions)}")
            db.execute("CREATE UNIQUE INDEX curve_metadata_key ON curve_metadata(db_index,id)")
        session = AccuracySession(db, unions, source, filters, warnings)
        db.execute("PRAGMA query_only=ON")
        yield session
    finally:
        db.close()
