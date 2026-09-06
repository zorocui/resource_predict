import copy
import gzip
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import pandas as pd

from resource_predict.pipeline.forecast_archive import archive_forecasts
from resource_predict.pipeline.run import generate_forecasts


def _chart():
    return {
        "best_method": "rolling_mean", "x_pred_ms": [1000, 2000],
        "preds_future": {"rolling_mean": [1.0, 2.0], "arima": [88, 99]},
        "preds": {"rolling_mean": [999]}, "y_train": [123],
        "forecast_diagnostics": {"provenance": {"config_hash": "container-config"}},
    }


def _resource():
    return {
        "resource_id": "workload-1", "resource_type": "k8s_workload",
        "spec": {"containers": [{"name": "app", "cpu_request": 0.5}]},
        "data_quality": {"usable": True},
        "container_data_quality": {"app": {"usable": True}},
        "scaling_advice": {"action": "keep"},
        "charts_forecast": {"cpu": _chart()},
        "forecast_diagnostics": {"cpu": {"provenance": {"config_hash": "resource-config"}}},
        "container_charts_forecast": {"app": {"cpu": _chart(), "memory": _chart()}},
    }


def _read(metadata):
    with gzip.open(metadata["path"], "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def test_selected_only_with_resource_and_container_context(tmp_path):
    resource = _resource()
    before = copy.deepcopy(resource)
    metadata = archive_forecasts(tmp_path, iter([resource]))
    assert metadata["status"] == "completed"
    assert metadata["count"] == 1
    record, = _read(metadata)
    assert record["spec"] == resource["spec"]
    assert record["data_quality"] == resource["data_quality"]
    assert record["container_data_quality"] == resource["container_data_quality"]
    assert record["scaling_advice"] == resource["scaling_advice"]
    assert record["forecasts"]["cpu"] == {
        "x_pred_ms": [1000, 2000], "yhat": [1.0, 2.0],
        "selected_model": "rolling_mean", "provenance": {"config_hash": "resource-config"},
    }
    assert set(record["container_forecasts"]["app"]) == {"cpu", "memory"}
    assert record["container_forecasts"]["app"]["cpu"]["provenance"]["config_hash"] == "container-config"
    assert "preds" not in json.dumps(record)
    assert "arima" not in json.dumps(record)
    assert resource == before


def test_same_timestamp_unique_batches_and_separate_scopes(tmp_path):
    with patch("resource_predict.pipeline.forecast_archive.time.time", return_value=1700000000):
        first = archive_forecasts(tmp_path / "vm", [_resource()])
        original = Path(first["path"]).read_bytes()
        second = archive_forecasts(tmp_path / "vm", [_resource()])
        workload = archive_forecasts(tmp_path / "workload", [_resource()])
    assert first["run_id"] != second["run_id"]
    assert Path(first["path"]).read_bytes() == original
    assert len(list((tmp_path / "vm" / "forecast_history").glob("*.gz"))) == 2
    assert Path(workload["path"]).parent == tmp_path / "workload" / "forecast_history"


def test_run_id_collision_never_overwrites_archive(tmp_path):
    with patch("resource_predict.pipeline.forecast_archive.time.time", return_value=1700000000):
        with patch("resource_predict.pipeline.forecast_archive.uuid.uuid4") as new_id:
            new_id.return_value.hex = "a" * 32
            first = archive_forecasts(tmp_path, [_resource()])
            original = Path(first["path"]).read_bytes()
            with pytest.raises(FileExistsError):
                archive_forecasts(tmp_path, [_resource()])
    assert Path(first["path"]).read_bytes() == original
    assert list(Path(first["path"]).parent.iterdir()) == [Path(first["path"])]


def test_retention_only_removes_old_completed_owned_files(tmp_path):
    with patch("resource_predict.pipeline.forecast_archive.time.time", return_value=1700000000):
        old = archive_forecasts(tmp_path, [_resource()])
    directory = Path(old["path"]).parent
    unrelated = directory / "notes.jsonl.gz"
    unrelated.write_text("keep", encoding="utf-8")
    partial = directory / ".forecast_1700000000000_01234567890123456789012345678901.jsonl.gz.tmp"
    partial.write_text("partial", encoding="utf-8")
    nested = directory / "forecast_1700000000000_01234567890123456789012345678901.jsonl.gz"
    nested.mkdir()
    with patch("resource_predict.pipeline.forecast_archive.time.time", return_value=1700000000 + 8 * 86400):
        current = archive_forecasts(tmp_path, [_resource()], retention_days=7)
    assert not Path(old["path"]).exists()
    assert Path(current["path"]).exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert partial.exists() and nested.is_dir()


@pytest.mark.parametrize("failure", ["stream", "rename"])
def test_failure_cleans_partial_and_preserves_prior_archive(tmp_path, failure):
    prior = archive_forecasts(tmp_path, [_resource()])
    original = Path(prior["path"]).read_bytes()

    def broken_items():
        yield _resource()
        raise RuntimeError("stream failed")

    if failure == "stream":
        with pytest.raises(RuntimeError, match="stream failed"):
            archive_forecasts(tmp_path, broken_items())
    else:
        with patch.object(Path, "rename", side_effect=OSError("rename failed")):
            with pytest.raises(OSError, match="rename failed"):
                archive_forecasts(tmp_path, [_resource()])
    assert Path(prior["path"]).read_bytes() == original
    assert list((tmp_path / "forecast_history").iterdir()) == [Path(prior["path"])]


def test_disabled_empty_and_invalid_selection(tmp_path):
    assert archive_forecasts(tmp_path, [_resource()], enabled=False)["status"] == "disabled"
    assert not (tmp_path / "forecast_history").exists()
    assert archive_forecasts(tmp_path, [])["status"] == "empty"
    invalid = _resource()
    invalid["charts_forecast"]["cpu"]["preds_future"]["rolling_mean"] = [1]
    with pytest.raises(ValueError, match="aligned"):
        archive_forecasts(tmp_path, [invalid])
    assert not list((tmp_path / "forecast_history").iterdir())


def test_pipeline_partial_archive_excludes_retained_resources_and_metrics(tmp_path):
    values = pd.Series([0.2] * 44 + [0.8] * 4,
                       index=pd.date_range("2026-08-01", periods=48, freq="h"))
    resources = [{"resource_id": rid, "resource_type": "vm",
                  "spec": {"cpu_cores": 2, "memory_gb": 4, "disk_gb": 50},
                  "metrics": {metric: values for metric in ("cpu", "memory", "disk")}}
                 for rid in ("vm-a", "vm-b")]
    options = {"out_dir": str(tmp_path), "test_size": 4, "future_steps": 2, "max_workers": 1}
    with patch("resource_predict.pipeline.run.read_forecast_config",
               return_value={"enabled_methods": ["rolling_mean"], "enable_ensemble": False}):
        generate_forecasts(**options, data_provider=lambda **kwargs: resources, save_raw=True)
        first = json.loads((tmp_path / "generation_stats.json").read_text(encoding="utf-8"))["forecast_archive"]
        first_bytes = Path(first["path"]).read_bytes()
        assert first["count"] == 2
        generate_forecasts(**options, predict_only=True, resource_ids=["vm-a"],
                           metric_names_by_resource={"vm-a": ["cpu"]})
    second = json.loads((tmp_path / "generation_stats.json").read_text(encoding="utf-8"))["forecast_archive"]
    record, = _read(second)
    assert record["resource_id"] == "vm-a"
    assert set(record["forecasts"]) == {"cpu"}
    assert record["forecasts"]["cpu"]["provenance"]["train_end_ms"] == values.index[-1].value // 1_000_000
    assert Path(first["path"]).read_bytes() == first_bytes
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["resources"]) == 2


def test_archive_failure_is_reported_without_aborting_forecasts(tmp_path):
    with patch("resource_predict.pipeline.run.read_forecast_config",
               return_value={"enabled_methods": ["rolling_mean"], "enable_ensemble": False}), patch(
        "resource_predict.pipeline.run.archive_forecasts", side_effect=OSError("disk full")
    ):
        generate_forecasts(out_dir=str(tmp_path), resources=1, n=48,
                           test_size=4, future_steps=2, max_workers=1)
    stats = json.loads((tmp_path / "generation_stats.json").read_text(encoding="utf-8"))
    assert stats["forecast_archive"]["status"] == "failed"
    assert stats["forecast_archive"]["error"] == "disk full"
    assert (tmp_path / "manifest.json").exists()
