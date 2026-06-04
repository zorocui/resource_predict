# Project Working Notes

These notes capture project-specific mistakes already encountered in this repo. Follow them before making future edits.

- Treat `README.md` as a CentOS/Linux-facing document. Use `bash` command blocks, `source .venv/bin/activate`, `export ...`, and `python ...`; do not introduce PowerShell commands.
- Edit text files as UTF-8. Do not use PowerShell `Set-Content` for Chinese Markdown or source text unless encoding is explicitly verified. Prefer `apply_patch` for manual edits.
- PowerShell tool output may render Chinese text as mojibake even when the underlying UTF-8 files are correct. Treat this as a terminal display issue by default; verify with UTF-8 reads or `git diff` only when file encoding is genuinely in doubt, and avoid spending extra tokens re-investigating the same display artifact.
- README.md is a quick-start guide (~270 lines). Detailed docs live in `docs/`. Keep README concise with architecture diagrams, commands, API summary, and links to docs/.
- To reduce token and output waste, exclude generated/vendor-heavy paths from normal searches unless explicitly needed: `outputs/`, `static/vendor/`, `.venv/`, `.pytest_cache/`, `.playwright-cli/`.
- Do not leave compatibility shim files when the user asks to remove old names. Remove old files and update references instead.
- Current naming: `generate_forecasts.py`, `ingest_k8s_workloads.py`, `resource_predict/core/k8s_workload_decision.py`, `generate_forecasts`, and `build_k8s_workload_advice`.
- Avoid reintroducing old names: `generate_images.py`, `generate_k8s_pods.py`, `k8s_pod_decision.py`, `generate_all_images`, and `build_k8s_pod_advice`.
- K8S terminology should use Workload for project concepts. Use Pod only for Prometheus/Kubernetes labels or explicit legacy artifact validation.
- K8S current resource specs must be stored and displayed at container granularity in `spec.containers`; do not keep or reintroduce Workload-level summed request/limit current-spec fields.
- Keep K8S small-spec recommendations from being inflated: request/limit targets below `2C/2Gi` should preserve fractional granularity instead of being rounded up to even integers.
- For real scaling execution, preserve the pre-execute gates: `action_gate`, `confidence`, `data_quality`, `cooldown`, and `policy_tier` must be checked before queuing an `execute` task.
- Keep K8S Prometheus CPU usage queries aligned with the configured `rate_window`; do not reintroduce hardcoded CPU `rate(...[10m])` windows in fetch or diagnose paths.
- When prediction output schema changes, update both artifacts and docs. `forecast_error_report.json` is a first-class output and should report errors by resource, metric, model, and window with RMSE/MAE/MAPE/P95 error fields.
- This repo often has a dirty worktree. Preserve unrelated user changes and do not revert files unless explicitly asked.
- Run project Python commands through the local Windows virtual environment: `.\.venv\Scripts\python.exe ...`; do not use system `python`.
- After Python logic edits, run relevant checks; for broad/core changes run the full regression suite: compileall + pyflakes + vulture --min-confidence 80 + pytest -q (see README §6 or docs/development.md for exact commands).
- After commands that create caches, remove project `__pycache__` directories outside `.venv`.
- When `docs/` files are renamed, moved, or deleted, update the documentation index table in README.md's "详细文档" section to keep links valid.
- Detailed reference documents are in `docs/`: [architecture.md](docs/architecture.md), [configuration.md](docs/configuration.md), [api-reference.md](docs/api-reference.md), [development.md](docs/development.md).
