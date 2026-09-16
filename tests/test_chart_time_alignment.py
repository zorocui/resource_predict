import pandas as pd
import pytest

from resource_predict.data.io import merge_charts_into_detail


@pytest.mark.parametrize("explicit", [True, False])
def test_new_observations_do_not_move_test_predictions(explicit):
    index = pd.date_range("2026-09-14", periods=10, freq="h")
    ms = (index.asi8 // 1_000_000).tolist()
    # 当次测试窗口是第4到第6点；raw 后来增加了第7到第10点，并缺少第5点。
    raw_series = pd.Series(range(10), index=index).drop(index[4])
    block = {"test_end_ms": ms[5], "sample_interval_seconds": 3600,
             "best_method": "rolling_mean", "preds": {"rolling_mean": [3, 4, 5]},
             "x_pred_ms": ms[6:8], "preds_future": {"rolling_mean": [6, 7]}}
    if explicit:
        block["x_test_ms"] = ms[3:6]
    detail = {"resource_id": "k8s:c:ns:deployment:app", "data_quality":{"cpu_request":{"prediction_skipped":True}},
              "container_data_quality":{"app":{"cpu_request":{"prediction_skipped":True}}}, "charts_forecast": {"cpu_request": block},
              "container_charts_forecast": {"app": {"cpu_request": block}}}
    raw = {"resource_id": detail["resource_id"], "resource_type": "k8s_workload", "spec":{"last_scaled_at_epoch_ms":ms[6]}, "cpu_request": raw_series,
           "container_metrics": {"app": {"cpu_request": raw_series}}}
    merged = merge_charts_into_detail(detail, {detail["resource_id"]: raw}, test_size=99)
    for chart in [merged["charts"]["cpu_request"], merged["container_charts"]["app"]["cpu_request"]]:
        assert chart["x_test_ms"] == ms[3:6]
        assert chart["y_test"] == [3, None, 5]
        assert chart["preds"]["rolling_mean"] == [3, 4, 5]
        assert chart["x_observed_ms"] == ms[6:]
        assert chart["y_observed"] == [6, 7, 8, 9]
        assert chart["x_train_ms"] == ms[:3]
        assert chart["latest_observation_ms"] == ms[-1]
        assert chart["last_scaled_at_epoch_ms"] == ms[6]
        assert chart["prediction_skipped"] is True


def test_no_known_test_boundary_does_not_guess_from_latest_raw():
    from resource_predict.data.io import _split_chart_observations
    series = pd.Series([1, 2, 3], index=pd.date_range("2026-01-01", periods=3, freq="h"))
    history, test, post = _split_chart_observations(series, {}, 2)
    assert history.equals(series)
    assert test.empty and post.empty
