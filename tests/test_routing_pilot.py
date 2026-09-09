from types import SimpleNamespace

import numpy as np
import pandas as pd

from benchmarks.routing_pilot import replay, summarize


def test_selection_does_not_see_test_and_features_precede_validation():
    series = pd.Series(np.r_[np.zeros(96), np.ones(24)], index=pd.date_range("2026-01-01", periods=120, freq="h"))

    def predict(method, history, steps):
        return SimpleNamespace(yhat=np.full(steps, 1.0 if method == "prophet" else 0.0))

    row = replay(series, 24, 1, predict)[0]
    assert row["enhanced"]["selected"] != "prophet"
    assert row["delta_rmse"] == 0
    assert row["features"]["mean"] == 0
    assert row["feature_end"] < row["validation_end"] < row["origin_time"]
    assert row["methods"]["prophet"]["test"]["rmse"] == 0


def test_missing_and_failures_are_explicit():
    series = pd.Series(np.zeros(120), index=pd.date_range("2026-01-01", periods=120, freq="h"))

    def fail(method, history, steps):
        raise RuntimeError("backend failed")

    row = replay(series, 24, 1, fail)[0]
    assert row["status"] == "failed"
    assert summarize([row])["paired"] == 0
    series.iloc[2] = np.nan
    assert replay(series, 24, 1, fail)[0]["reason"] == "missing_or_irregular"


def test_validation_winner_can_have_negative_test_gain():
    series = pd.Series(np.r_[np.zeros(72), np.ones(24), np.zeros(24)],
                       index=pd.date_range("2026-01-01", periods=120, freq="h"))

    def predict(method, history, steps):
        return SimpleNamespace(yhat=np.full(steps, float(method == "prophet")))

    row = replay(series, 24, 1, predict)[0]
    row["series_id"] = "example"
    assert row["enhanced"]["selected"] == "prophet"
    assert row["delta_rmse"] == -1
    assert summarize([row])["negative_gain_pairs"] == 1
    phases = row["methods"]
    expected = sum(phases[m]["validation"]["wall_seconds"] for m in phases)
    expected += phases["prophet"]["test"]["wall_seconds"]
    assert row["enhanced"]["estimated_workflow_wall_seconds"] == expected
