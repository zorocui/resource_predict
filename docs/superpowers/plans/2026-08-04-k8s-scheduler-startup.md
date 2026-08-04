# K8S Prometheus Scheduler Startup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `python app.py` start the existing K8S Prometheus scheduler when `scheduled_update_enabled=True`, without duplicating it under Flask's debug reloader.

**Architecture:** Keep scheduler creation out of `create_app()` and add a focused `run_app()` entry point. The entry point starts the existing scheduler only in the serving process, runs Flask, and stops a scheduler thread that it actually started.

**Tech Stack:** Python 3, Flask, `unittest`, `unittest.mock`

## Global Constraints

- Only support the repository's documented `python app.py` launch path.
- Preserve existing Prometheus fetch, incremental-window, prediction, and upsert behavior.
- Do not start background work as a side effect of importing `app` or calling `create_app()`.
- Prevent duplicate scheduling in the Flask debug reloader parent process.
- Use `apply_patch` for UTF-8 source and Markdown edits.
- Run Python commands with `.\.venv\Scripts\python.exe`.

---

### Task 1: Wire the scheduler into the application entry point

**Files:**
- Create: `tests/test_app_scheduler.py`
- Modify: `app.py`

**Interfaces:**
- Consumes: `start_k8s_background_updater() -> Optional[threading.Thread]` and `stop_k8s_background_updater(timeout: float = 10.0) -> None`
- Produces: `run_app() -> None`

- [ ] **Step 1: Write failing entry-point lifecycle tests**

Add tests that patch `create_app`, scheduler start/stop functions, settings, and `WERKZEUG_RUN_MAIN`. Verify normal mode starts and stops once, a debug reloader parent does neither, and a debug reloader child starts and stops once.

- [ ] **Step 2: Run the focused test and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_app_scheduler.py`

Expected: FAIL because `app.run_app` does not exist.

- [ ] **Step 3: Implement the minimal lifecycle**

Import the existing K8S scheduler functions. Add `run_app()` that creates the Flask app, skips scheduler startup only when `settings.app.debug` is true and `WERKZEUG_RUN_MAIN` is not `"true"`, starts the scheduler otherwise, calls `app.run(...)`, and stops the scheduler in `finally` only when startup returned a thread. Call `run_app()` from the `__main__` block.

- [ ] **Step 4: Run focused scheduler tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_app_scheduler.py tests/test_scheduler_startup_delay.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the lifecycle fix**

```bash
git add app.py tests/test_app_scheduler.py
git commit -m "fix: start k8s scheduler with app"
```

### Task 2: Align operational documentation and run regression checks

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/configuration.md`

**Interfaces:**
- Consumes: the `run_app()` lifecycle from Task 1
- Produces: accurate operator guidance for `scheduled_update_enabled`

- [ ] **Step 1: Update the two stale startup statements**

Document that `python app.py` starts K8S scheduled pulling when `scheduled_update_enabled=True`, while VM updates remain manually triggered unless separately wired. Retain the meanings of interval, overlap, history, and startup-delay settings.

- [ ] **Step 2: Run source and documentation checks**

Run:

```bash
.\.venv\Scripts\python.exe -m compileall -q app.py resource_predict tests
.\.venv\Scripts\python.exe -m pyflakes app.py resource_predict tests
.\.venv\Scripts\vulture.exe app.py resource_predict tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all commands exit successfully.

- [ ] **Step 3: Remove project Python caches created by checks**

Remove only `__pycache__` directories inside the repository and outside `.venv`, then confirm the working tree contains only intended changes.

- [ ] **Step 4: Commit documentation updates**

```bash
git add docs/architecture.md docs/configuration.md
git commit -m "docs: explain k8s scheduled pulling"
```

