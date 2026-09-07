import math
import sqlite3
from collections import defaultdict
from contextlib import closing
from unittest.mock import patch

import pytest

from benchmarks.forecast_feedback_benchmark import seed, resource, NOW, METRICS
from resource_predict.pipeline.calibration import _calibrate_curve, HORIZONS
from resource_predict.pipeline.realized_error import _report, _basis, _unit, _json, DB_NAME, REPORT_NAME, score_realized_forecasts
from resource_predict.pipeline.forecast_archive import archive_forecasts


def test_streamed_calibration_matches_original_sql_per_horizon(tmp_path):
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        seed(db,1,1,7,120)
        with db:
            db.execute("UPDATE points SET actual=predicted+(curve_id%7)*0.03,scored_at_ms=target_ms+1")
        item = resource(0,1)
        chart = {"best_method": "seasonal_naive", "x_pred_ms": [NOW+1800000,NOW+7200000,NOW+43200000],
                 "preds_future": {"seasonal_naive": [0.2,0.3,0.4]}}
        diagnostics = {"provenance": {"data_end_ms": NOW,"generated_at_epoch_ms": NOW,
                                       "model_version": "benchmark-v1","config_hash": "benchmark"}}
        actual = _calibrate_curve(db,item,"app-0",METRICS[0],chart,diagnostics,7)
        for block in actual["buckets"]:
            low,high = HORIZONS[block["horizon_bucket"]]
            rows = db.execute(
                "SELECT residual,target_ms,scored_at_ms,batch FROM ("
                "SELECT p.actual-p.predicted AS residual,p.target_ms,p.scored_at_ms,c.batch,"
                "ROW_NUMBER() OVER(PARTITION BY p.target_ms ORDER BY c.issued_ms DESC,c.id DESC) AS rank "
                "FROM curves c JOIN points p ON p.curve_id=c.id WHERE c.resource_id=? AND c.container=? "
                "AND c.metric=? AND c.model=? AND c.unit=? AND c.basis=? AND p.actual IS NOT NULL "
                "AND p.scored_at_ms<? AND p.target_ms<=? AND c.issued_ms>=? "
                "AND p.target_ms-c.data_end_ms>? AND p.target_ms-c.data_end_ms<=? "
                "AND json_extract(c.provenance,'$.model_version')=? AND json_extract(c.provenance,'$.config_hash')=?"
                ") WHERE rank=1 ORDER BY target_ms DESC LIMIT 500",
                (item["resource_id"],"app-0",METRICS[0],"seasonal_naive",_unit(item,"app-0",METRICS[0]),
                 _basis(item,"app-0",METRICS[0]),NOW,NOW,NOW-7*86400000,low,high,"benchmark-v1","benchmark"),
            ).fetchall()
            import hashlib
            assert block["sample_digest"] == hashlib.sha256(_json(rows).encode()).hexdigest()
            assert block["sample_count"] == len(rows)
            margin = max(0,sorted(r[0] for r in rows)[math.ceil((len(rows)+1)*0.95)-1]) if len(rows)>=60 else None
            assert block["margin"] == margin


def test_two_stage_report_preserves_point_weighting(tmp_path):
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        seed(db,2,1,3,24)
        with db:
            db.execute("UPDATE points SET actual=predicted+(curve_id%5)*0.07-0.1 WHERE target_ms%1200000=0")
        expected = defaultdict(list)
        for model,metric,unit,delta,error in db.execute(
            "SELECT c.model,c.metric,c.unit,p.target_ms-c.data_end_ms,p.actual-p.predicted "
            "FROM points p JOIN curves c ON c.id=p.curve_id WHERE p.actual IS NOT NULL"
        ):
            horizon = "0-1h" if delta<=3600000 else "1-6h"
            expected[model,metric,unit,horizon].append(error)
        for row in _report(db,NOW,7)["rows"]:
            errors = expected[row["model"],row["metric"],row["unit"],row["horizon"]]
            assert row["count"] == len(errors)
            assert row["mae"] == pytest.approx(sum(abs(e) for e in errors)/len(errors))
            assert row["rmse"] == pytest.approx(math.sqrt(sum(e*e for e in errors)/len(errors)))
            assert row["underestimate_rate"] == pytest.approx(sum(e>0 for e in errors)/len(errors))
            assert row["mean_underestimate"] == pytest.approx(sum(max(e,0) for e in errors)/len(errors))


def test_deferred_report_keeps_scores_then_publishes_without_double_count(tmp_path):
    provenance = {"data_end_ms": NOW,"generated_at_epoch_ms": NOW}
    item = {"resource_id": "vm", "spec": {},"charts_forecast": {"cpu": {
        "x_pred_ms": [NOW+1000],"best_method": "rolling_mean","preds_future": {"rolling_mean": [0.2]}}},
        "forecast_diagnostics": {"cpu": {"provenance": provenance}}}
    with patch("time.time",return_value=NOW/1000):
        archive_forecasts(tmp_path,[item])
    incoming = {"resource_id": "vm","observation_evidence": {"schema_version": 1,"source": "test",
                "spec": {},"metrics": {"cpu": {"timestamps": [NOW+1000],"values": [0.4]}}}}
    with patch("time.time",return_value=(NOW+2000)/1000):
        with patch("resource_predict.pipeline.realized_error._report",side_effect=AssertionError("must defer")):
            result = score_realized_forecasts(tmp_path,[incoming],publish_report=False)
        assert result["newly_scored"] == 1
        assert result["coverage"] is None
        assert not (tmp_path / REPORT_NAME).exists()
        result = score_realized_forecasts(tmp_path,[incoming])
    assert result["newly_scored"] == 0
    assert result["coverage"] == {"scored": 1}
    assert result["report_published"] is True
