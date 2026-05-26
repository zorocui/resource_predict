# Project Working Notes

These notes capture project-specific mistakes already encountered in this repo. Follow them before making future edits.

- Treat `README.md` as a CentOS/Linux-facing document. Use `bash` command blocks, `source .venv/bin/activate`, `export ...`, and `python ...`; do not introduce PowerShell commands.
- Edit text files as UTF-8. Do not use PowerShell `Set-Content` for Chinese Markdown or source text unless encoding is explicitly verified. Prefer `apply_patch` for manual edits.
- Do not replace the README with an overly short stub. Keep it concise, but preserve useful architecture diagrams, data-flow diagrams, current commands, API summary, and operational guidance.
- Do not leave compatibility shim files when the user asks to remove old names. Remove old files and update references instead.
- Current naming: `generate_forecasts.py`, `ingest_k8s_workloads.py`, `resource_predict/core/k8s_workload_decision.py`, `generate_forecasts`, and `build_k8s_workload_advice`.
- Avoid reintroducing old names: `generate_images.py`, `generate_k8s_pods.py`, `k8s_pod_decision.py`, `generate_all_images`, and `build_k8s_pod_advice`.
- K8S terminology should use Workload for project concepts. Use Pod only for Prometheus/Kubernetes labels or explicit legacy artifact validation.
- This repo often has a dirty worktree. Preserve unrelated user changes and do not revert files unless explicitly asked.
- After Python edits, run the relevant checks from the current virtualenv when possible:
  - `python -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests`
  - `python -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests`
  - `vulture app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80`
  - `python -m pytest -q`
- After commands that create caches, remove project `__pycache__` directories outside `.venv`.
