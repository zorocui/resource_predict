import pandas as pd

from resource_predict.pipeline.prepare import prepare_recent_contiguous_forecast_data


def prepare(series, **kwargs):
    return prepare_recent_contiguous_forecast_data(
        [{"resource_id": "k8s:a:ns:deployment:api", "resource_type": "k8s_workload",
          "cpu_limit": series, "container_metrics": {"app": {"cpu_limit": series}}}],
        sample_interval_seconds=600, max_gap_steps=3, **kwargs,
    )


def test_skip_log_distinguishes_missing_gap_from_required_points(caplog):
    index = pd.DatetimeIndex(["2026-09-15 11:50", "2026-09-15 12:00"]).append(
        pd.date_range("2026-09-15 13:00", periods=120, freq="10min")
    )
    _, skips = prepare(pd.Series(1., index=index), test_size=144)
    assert len(skips) == 2
    text = caplog.text
    assert "metric=container/app/cpu_limit" in text
    assert "总有效点数=122" in text
    assert "连续有效点数=120" in text
    assert "最低需要=145点" in text
    assert "距最低要求还差=25点" in text
    assert "缺失采样点数=5" in text
    assert "缺失时刻=2026-09-15T12:10:00+00:00 至 2026-09-15T12:50:00+00:00" in text


def test_short_history_without_gap_does_not_invent_missing_times(caplog):
    prepare(pd.Series(1., index=pd.date_range("2026-09-15", periods=144, freq="10min")), test_size=144)
    assert "距最低要求还差=1点" in caplog.text
    assert "未发现截断缺口" in caplog.text
    assert "缺失时刻=" not in caplog.text


def test_gap_log_uses_last_break_and_explicit_utc(caplog):
    index = pd.DatetimeIndex([
        "2026-09-14 12:00", "2026-09-15 12:00", "2026-09-15 13:00", "2026-09-15 13:10",
    ], tz="Asia/Shanghai")
    prepare(pd.Series(1., index=index), test_size=144)
    assert "前一有效点=2026-09-15T04:00:00+00:00" in caplog.text
    assert "后一有效点=2026-09-15T05:00:00+00:00" in caplog.text
    assert "缺失采样点数=5" in caplog.text
