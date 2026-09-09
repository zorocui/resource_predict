import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import pytest

import generate_forecasts as cli
from resource_predict.internal_settings import Settings, settings
from resource_predict.services.runtime_config import (
    RuntimeConfigStore,
    RuntimeConfigValidationError,
    default_runtime_config,
    normalize_runtime_config,
)


def test_parallel_defaults_and_validation():
    assert normalize_runtime_config({}).prediction.parallel_backend == "auto"
    assert normalize_runtime_config({}).prediction.max_workers == 0
    for backend in ("auto", "process", "thread", "serial"):
        for workers in (0, 1, 256):
            cfg = normalize_runtime_config({"prediction": {"parallel_backend": backend, "max_workers": workers}})
            assert (cfg.prediction.parallel_backend, cfg.prediction.max_workers) == (backend, workers)
    for field, values in (("max_workers", (-1, 257, True, 1.5, "2")), ("parallel_backend", ("other", None, True, []))):
        for value in values:
            with pytest.raises(RuntimeConfigValidationError) as caught:
                normalize_runtime_config({"prediction": {field: value}})
            assert caught.value.field == f"runtime.prediction.{field}"


def test_frozen_settings_survive_config_changes_restore_and_pickle():
    store = RuntimeConfigStore(default_runtime_config())
    with patch("resource_predict.services.runtime_config.runtime_config_store", store):
        snapshot = settings.freeze()
        assert isinstance(snapshot, Settings)
        assert pickle.loads(pickle.dumps(snapshot)) == snapshot
        with pytest.raises(FrozenInstanceError):
            snapshot.generation.max_workers = 7
        store.replace_payload({"prediction": {"parallel_backend": "process", "max_workers": 7, "enabled_methods": ["rolling_mean"]},
                               "collection": {"rate_window": "30m"}, "decision": {"scale_out_threshold": 0.9}})
        updated = settings.freeze()
        assert updated.generation.max_workers == 7
        assert updated.generation.parallel_backend == "process"
        assert snapshot.generation.max_workers is None
        with pytest.raises(RuntimeError):
            with settings.use(snapshot):
                assert settings.freeze() is snapshot
                for field in ("generation", "forecast", "decision", "k8s_prometheus"):
                    assert getattr(settings, field) is getattr(snapshot, field)
                with settings.use(updated):
                    assert settings.freeze() is updated
                assert settings.freeze() is snapshot
                raise RuntimeError("restore on error")
        assert settings.freeze() == updated
        with pytest.raises(TypeError):
            with settings.use({}):
                pass


def test_frozen_settings_are_isolated_between_threads():
    original = settings.freeze()
    snapshots = [replace(original, generation=replace(original.generation, max_workers=count)) for count in (2, 5)]
    barrier = threading.Barrier(2)

    def read(snapshot):
        with settings.use(snapshot):
            barrier.wait(timeout=5)
            return settings.generation.max_workers

    with settings.use(original), ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(read, snapshots)) == [2, 5]
        assert settings.freeze() is original


@pytest.mark.parametrize("args", [["--max-workers", "-1"], ["--max-workers", "257"], ["--max-workers", "1.2"], ["--parallel-backend", "invalid"]])
def test_cli_rejects_invalid_parallel_options(args):
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(args)


@pytest.mark.parametrize("command", ["all", "predict"])
@pytest.mark.parametrize("flags,expected", [([], {}), (["--parallel-backend", "process", "--max-workers", "0"], {"parallel_backend": "process", "max_workers": 0})])
def test_cli_forwards_only_explicit_overrides(tmp_path, command, flags, expected):
    (tmp_path / cli.RAW_INDEX_FILENAME).write_text("{}", encoding="utf-8")
    with patch.object(cli, "scoped_out_dir", return_value=tmp_path), patch.object(cli, "provider", return_value=[]), \
         patch.object(cli, "split_items_by_scope", return_value={"vm": [object()], "k8s": []}), \
         patch.object(cli, "generate_forecasts", return_value=[]) as generate, \
         patch.object(cli, "generate_predictions_only", return_value=[]) as predict, \
         patch("resource_predict.logging_setup.setup_application_logging"):
        assert cli.main([command, *flags]) == 0
    calls = predict.call_args_list if command == "predict" else generate.call_args_list
    assert calls
    for call in calls:
        assert {k: v for k, v in call.kwargs.items() if k in ("max_workers", "parallel_backend")} == expected
