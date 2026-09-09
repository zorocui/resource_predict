from benchmarks.multicore_forecast import _outputs_match, main, run_benchmark


def test_comparison_tolerance_and_prediction_drift():
    baseline = {"scores": {"rmse": 0.2}, "predictions": [0.1, 0.2], "best": "arima"}
    assert _outputs_match(baseline, {**baseline, "predictions": [0.1, 0.200000001]})
    assert not _outputs_match(baseline, {**baseline, "predictions": [0.1, 0.3]})
    assert not _outputs_match(baseline, {**baseline, "best": "rolling_mean"})


def test_small_real_benchmark_reports_scope_and_serial_reference():
    report = run_benchmark(jobs=2, points=40, workers=2, models=["rolling_mean"], backends=["serial", "thread"])
    assert report["scope"] == "model_fit_only"
    assert report["runs"]["serial"]["speedup_vs_serial"] == 1
    assert report["runs"]["thread"]["outputs_match"] is True
    assert report["runs"]["thread"]["completed"] == 2
    assert report["runs"]["thread"]["model_failure_task_counts"] == {}


def test_cli_writes_json_report(tmp_path, capsys):
    output = tmp_path / "benchmark.json"
    assert main(["--jobs", "1", "--points", "40", "--models", "rolling_mean",
                 "--backends", "serial", "--output", str(output)]) == 0
    assert '"model_fit_only"' in output.read_text(encoding="utf-8")
    assert '"outputs_match": true' in capsys.readouterr().out
