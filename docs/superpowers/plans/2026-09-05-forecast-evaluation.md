# Forecast evaluation implementation plan

**Goal:** Implement the user-approved independent evaluation, fresh online forecasting and immutable forecast archive.

**Architecture:** Keep existing pipeline interfaces. Select on training-only temporal validation, score on outer holdout, refit online on all observations. Archive compact new forecasts before partial merges.

**Tech Stack:** Existing Python virtual environment, numpy/pandas, statsmodels, standard-library gzip/JSON.

## Global constraints

Preserve container spec semantics and execution gates. UTF-8 files, Linux-facing README. No new dependencies. Use local .venv/Scripts/python.exe. Preserve baseline a57a2e2 on origin/main. Work on codex/forecast-evaluation-baseline.

## Tasks

- [x] 1. Update pipeline/fit.py and metrics.py: validation-only model selection, independent outer holdout, true ensemble residual metrics, all-observation online refit, provenance and short-history fallback. Add regression cases in tests/test_forecast_optimizations.py and tests/test_forecast_evaluation.py. Change reuse default/normalization with matching config tests.
- [x] 2. Correct core/forecasting.py seasonal handling at fine sampling cadence with tests/test_forecasting.py regression coverage. Preserve daily semantics without unbounded seasonal states.
- [x] 3. Add pipeline/forecast_archive.py and tests/test_forecast_archive.py; integrate before partial merges in run.py. Archive gzip JSONL using unique exclusive temporary file and atomic rename, completed-batch metadata, compact selected forecasts only, configurable retention, no recursive deletion. Test incremental exclusion, provenance, scopes and retention.
- [x] 4. Update error report with container dimension and evaluation provenance; document schema/config changes in configuration.md, architecture.md, development.md and current innovation text. Add isolated regression cases.
- [x] 5. Review combined diff and run compileall, pyflakes, vulture --min-confidence 80 and pytest -q. Inspect all failures, repair, run affected checks and clean caches. Commit verified optimization separately from pushed baseline.

## Validation commands

```bash
python -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
python -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
python -m vulture app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
python -m pytest -q
```

Tests must assert observable correctness: selection unaffected by changing outer labels, latest observation affects online forecast, fixed ensemble weights on independent test, no false model provenance, bounded archive size and period-aware seasonality.

## Verification record (2026-09-06)

- Baseline: 390 tests passed, 21 subtests; one existing NumPy nonfinite-input warning.
- Final implementation: compileall, pyflakes and vulture --min-confidence 80 passed; pytest: 413 passed, 28 subtests, same baseline warning.
- Review correction: K8S provider filled gaps before temporal splitting. Replaced two-sided short-gap interpolation with past-only filling and added boundary regression. Existing preprocessed raw cannot be reconstructed; strict experiments require recollection/causal input, documented in development.md.
- Review correction: partial metric merge now updates forecast_diagnostics and data_quality with the fresh metric, preserving unrelated metric provenance.
- Subtasks independently verified SARIMA period semantics and gzip archive/error-report contracts; root ran the combined regression and real pipeline partial archive checks.
- No production precision, throughput, resource-savings or SLA improvement was claimed or measured in this implementation batch. Probabilistic calibration and automatic realized-error scoring remain next-stage work.
