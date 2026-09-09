import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd
import pytest

from benchmarks.routing_share import export_summary, preflight, probe
from tools.build_routing_package import build


def records():
    rows = []
    for resource in range(12):
        for day in range(14):
            origin = pd.Timestamp("2026-01-01") + pd.Timedelta(days=day)
            features = {"mean": .5, "std": .1, "points": 200 + day*24,
                        "range": .2, "slope": resource/100, "step_seconds": 3600}
            rows.append({"series_id": json.dumps([f"SECRET-{resource}", "SECRET-container", "cpu"]),
                         "status": "paired", "features": features,
                         "origin_time": str(origin), "feature_end": str(origin-pd.Timedelta(days=2)),
                         "test_end": str(origin+pd.Timedelta(hours=23)),
                         "delta_rmse": resource/100, "delta_wall_seconds": .2,
                         "methods": {m: {p: {"status": "ok", "error": "SECRET-exception"}
                                         for p in ("validation", "test")}
                                     for m in ("prophet", "seasonal_naive", "rolling_mean")}})
    return rows


def test_export_is_numeric_allowlist_and_probe_uses_sufficient_holdouts():
    report = {"metadata": {"source": "SECRET-source", "protocol": "routing-pilot-v2",
                           "arguments": {"raw_dir": "SECRET-path"}, "index_sha256": "SECRET-hash"}}
    result = export_summary(report, records())
    encoded = json.dumps(result, allow_nan=False)
    assert "SECRET" not in encoded
    assert "2026-01" not in encoded
    assert result["counts"]["resources"] == 12
    assert result["resource_holdout_probe"]["status"] == "ok"
    assert result["time_holdout_probe"]["status"] == "ok"
    assert result["time_holdout_probe"]["ridge_mae"] < result["time_holdout_probe"]["constant_mae"]


def test_probe_reports_insufficient_data_and_does_not_train_on_future_labels():
    rows = records()
    assert probe(rows[:3], "resource")["status"] == "insufficient_data"
    before = probe(rows, "time")
    # Post-cutoff targets may change test metrics, but never the training set size.
    for row in rows:
        if pd.Timestamp(row["origin_time"]) >= pd.Timestamp("2026-01-10"):
            row["delta_rmse"] = -20
    after = probe(rows, "time")
    assert before["train_rows"] == after["train_rows"] == 84
    assert before["test_rows"] == after["test_rows"] == 60


def test_preflight_does_not_fit_models(monkeypatch):
    good = pd.Series(np.zeros(240), index=pd.date_range("2026-01-01", periods=240, freq="h"))
    missing = good.copy()
    missing.iloc[2] = np.nan
    monkeypatch.setattr("benchmarks.routing_share.snapshot_series",
                        lambda *args: iter([("secret-a", good), ("secret-b", missing), ("secret-c", good[:2])]))
    result = preflight(Path("unused"), 12, 42, 24, 3)
    assert result["counts"] == {"series": 3, "eligible": 1, "short": 1, "irregular_or_missing": 1}
    assert result["model_fits_planned"] == 18


def test_source_package_excludes_private_files_and_refuses_overwrite(tmp_path):
    root = Path(__file__).resolve().parents[1]
    target = tmp_path / "experiment.zip"
    build(root, target)
    with ZipFile(target) as archive:
        names = archive.namelist()
        assert "routing-experiment/benchmarks/routing_share.py" in names
        assert not any("outputs/" in n or "deploy/" in n or ".venv/" in n or "__pycache__" in n for n in names)
        assert archive.testzip() is None
    with pytest.raises(FileExistsError):
        build(root, target)
