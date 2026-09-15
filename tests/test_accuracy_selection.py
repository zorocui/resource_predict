import pandas as pd

from resource_predict.core.accuracy import tolerance_hit
from resource_predict.pipeline.metrics import validation_backtest_metrics
from resource_predict.pipeline.model_selection import choose_best_method
from resource_predict.pipeline.worker import _with_identity


def test_accuracy_wins_over_lower_rmse_and_reports_three_folds():
    history = pd.Series([.8] * 24 + ([.8] * 8 + [.4] * 2) * 3,
                        index=pd.date_range("2026-01-01", periods=54, freq="h"))
    def predict(method, train, index, phase):
        assert phase == "validation" and train.index[-1] < index[0]
        return pd.Series(.72 if method == "rolling_mean" else .8, index=index)
    for ratio in (True, False):
        scores, info = validation_backtest_metrics(history, ["rolling_mean", "seasonal_naive"],
            test_size=10, folds=3, enable_ensemble=False, predict=predict, accuracy_ratio=ratio)
        assert scores["rolling_mean"]["selection_rmse"] < scores["seasonal_naive"]["selection_rmse"]
        assert choose_best_method(metrics_by_method=scores, anomaly={}) == ("seasonal_naive" if ratio else "rolling_mean")
        if ratio:
            assert scores["seasonal_naive"]["validation_accuracy"] == .8
            assert scores["rolling_mean"]["validation_accuracy"] == 0
            assert scores["seasonal_naive"]["validation_valid_points"] == 30
            assert info["validation_fold_accuracies"]["seasonal_naive"] == [.8] * 3


def test_accuracy_ties_use_rmse_and_tolerance_matches_page_boundaries():
    assert tolerance_hit(.5, .55)
    assert tolerance_hit(40, 42)
    assert not tolerance_hit(.5, .551)
    assert choose_best_method(metrics_by_method={
        "a": {"validation_accuracy": .8, "selection_rmse": .2},
        "b": {"validation_accuracy": .8, "selection_rmse": .1},
    }, anomaly={"is_anomalous": True}) == "b"


def test_worker_never_treats_absolute_container_usage_as_percentage():
    series = pd.Series([.2, .3])
    resource = {"resource_id": "k8s:c:ns:deployment:app", "resource_type": "k8s_workload",
                "container_metric_modes": {"app": {"memory_request": "memory_working_set_gb"}}}
    assert not _with_identity(series, resource, "app", "memory_request", None).attrs["accuracy_ratio"]
    resource["container_metric_modes"]["app"]["memory_request"] = "memory_working_set/memory_request"
    assert _with_identity(series, resource, "app", "memory_request", None).attrs["accuracy_ratio"]
    assert "accuracy_ratio" not in series.attrs
