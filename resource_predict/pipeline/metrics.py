"""Training-only temporal validation; the outer holdout is never read here."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

import pandas as pd

from resource_predict.pipeline.forecasting import ensemble_series
from resource_predict.pipeline.series_utils import compute_metrics


def validation_backtest_metrics(
    history: pd.Series,
    methods: List[str],
    *,
    test_size: int,
    folds: int,
    enable_ensemble: bool,
    predict: Callable[[str, pd.Series, pd.DatetimeIndex, str], Optional[pd.Series]],
) -> tuple[Dict[str, Dict[str, float]], Dict[str, object]]:
    """Score expanding folds, with ensemble weights learned on earlier folds.

    The first ensemble fold uses equal weights. Final deployment weights can
    use every validation fold; no independent test label enters these weights.
    """
    min_train = max(test_size, 24)
    count = min(max(1, folds), max(0, (len(history) - min_train) // test_size))
    truths: Dict[str, List[pd.Series]] = {}
    predictions: Dict[str, List[pd.Series]] = {}
    scores: Dict[str, Dict[str, float]] = {}
    windows = []
    for fold in reversed(range(count)):
        end = len(history) - fold * test_size
        start = end - test_size
        train, valid = history.iloc[:start], history.iloc[start:end]
        windows.append({"train_end_ms": int(train.index[-1].value // 1_000_000),
                        "validation_start_ms": int(valid.index[0].value // 1_000_000),
                        "validation_end_ms": int(valid.index[-1].value // 1_000_000)})
        fold_predictions = {}
        for method in methods:
            pred = predict(method, train, valid.index, "validation")
            if pred is not None:
                fold_predictions[method] = pred
        if enable_ensemble and len(methods) > 1 and len(fold_predictions) == len(methods):
            previous = {method: scores.get(method, {"selection_rmse": 1.0}) for method in methods}
            ensemble = ensemble_series(fold_predictions, previous, enable_ensemble=True)
            if ensemble is not None:
                fold_predictions["ensemble"] = ensemble
        for method, pred in fold_predictions.items():
            truths.setdefault(method, []).append(valid)
            predictions.setdefault(method, []).append(pred)
            latest = compute_metrics(valid, pred)
            pooled = compute_metrics(pd.concat(truths[method]), pd.concat(predictions[method]))
            n = len(predictions[method])
            scores[method] = {
                **{f"validation_{key}": value for key, value in pooled.items()},
                "validation_folds": float(n),
                "selection_rmse": (0.65 * latest["rmse"] + 0.35 * pooled["rmse"])
                if n > 1 else pooled["rmse"],
            }
            if n > 1:
                scores[method].update(rolling_rmse=pooled["rmse"], rolling_mae=pooled["mae"],
                                      rolling_folds=float(n))
    # A candidate must succeed on every intended fold to compete fairly.
    complete = {method: values for method, values in scores.items()
                if values["validation_folds"] == count}
    return complete, {"validation_windows": windows, "validation_folds_requested": max(1, folds),
                      "validation_folds_available": count,
                      "incomplete_validation_methods": sorted(set(scores) - set(complete))}
