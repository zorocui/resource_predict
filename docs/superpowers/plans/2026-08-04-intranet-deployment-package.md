# Intranet Deployment Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a double-click Windows entry point that generates a validated, minimal ZIP with the original runtime directory structure for intranet deployment.

**Architecture:** A small Python module owns an explicit runtime allowlist, archive-path validation, temporary ZIP creation, and final artifact reporting. A root-level batch file only selects Python and invokes the module, while unit tests construct an isolated project tree and inspect the generated ZIP instead of depending on the developer's workspace contents.

**Tech Stack:** Python 3 standard library (`argparse`, `dataclasses`, `datetime`, `pathlib`, `tempfile`, `zipfile`), Windows batch, pytest

## Global Constraints

- Preserve `resource_predict/` as the ZIP's only top-level directory and keep every included file's repository-relative path.
- Include `deploy/runtime_config.json` unchanged.
- Include only `deploy/*.example.json`; exclude `deploy/clusters.json`, `deploy/k8s_prometheus_clusters.json`, and `deploy/forecast_config.json`.
- Include `static/vendor/echarts/echarts.min.js` and all other files under the runtime allowlist.
- Exclude caches, bytecode, logs, temporary files, outputs, tests, docs, development tools, virtual environments, and source-control metadata.
- Do not download or bundle Python dependencies or `.venv`.
- Never overwrite an existing successful ZIP and never leave a partial final ZIP after failure.
- Run project Python commands through `.\.venv\Scripts\python.exe` and remove project `__pycache__` directories outside `.venv` after verification.

---

### Task 1: Implement and test the archive allowlist

**Files:**
- Create: `tools/build_deployment_package.py`
- Create: `tests/test_deployment_package.py`

**Interfaces:**
- Produces: `PackageBuildError(ValueError)` for stable user-facing failures.
- Produces: frozen `PackageResult(path: Path, file_count: int, size_bytes: int)`.
- Produces: `collect_runtime_files(project_root: Path) -> list[Path]` returning sorted absolute source files.
- Produces: `validate_archive_names(names: Iterable[str], top_level: str = "resource_predict") -> None`.
- Produces: `build_deployment_package(project_root: Path, output_dir: Path | None = None, now: datetime | None = None) -> PackageResult`.

- [ ] **Step 1: Write failing allowlist and structure tests**

Create a temporary project fixture with all required files plus excluded files. Assert the collected relative paths are exactly the runtime roots, `deploy/runtime_config.json`, and `deploy/clusters.example.json`; assert these examples are excluded: `tests/test_app.py`, `outputs/resources.json`, `resource_predict/__pycache__/module.pyc`, `resource_predict/app.log`, and all three active/legacy deploy JSON files.

Use this concrete archive assertion:

```python
with ZipFile(result.path) as archive:
    names = set(archive.namelist())

assert "resource_predict/app.py" in names
assert "resource_predict/deploy/runtime_config.json" in names
assert "resource_predict/static/vendor/echarts/echarts.min.js" in names
assert "resource_predict/deploy/clusters.json" not in names
assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
assert archive.read("resource_predict/deploy/runtime_config.json") == runtime_bytes
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_deployment_package.py -q`

Expected: FAIL because `tools.build_deployment_package` does not exist.

- [ ] **Step 3: Implement deterministic file collection**

Define these allowlists:

```python
ROOT_FILES = ("app.py", "generate_forecasts.py", "ingest_k8s_workloads.py", "requirements.txt")
RUNTIME_DIRS = ("resource_predict", "templates", "static")
REQUIRED_PATHS = (
    "app.py",
    "requirements.txt",
    "resource_predict/__init__.py",
    "templates/index.html",
    "static/js/index.js",
    "static/css/index.css",
    "static/vendor/echarts/echarts.min.js",
    "deploy/runtime_config.json",
)
EXCLUDED_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
EXCLUDED_FILE_NAMES = {".DS_Store", "Thumbs.db"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".temp", ".bak", "~"}
```

`collect_runtime_files` must fail with `PackageBuildError("缺少运行必需文件: <path>")` for each missing required path, walk only `RUNTIME_DIRS`, append `deploy/runtime_config.json` and sorted `deploy/*.example.json`, reject symlinks that resolve outside `project_root`, remove duplicate resolved paths, and return sources sorted by POSIX relative path.

- [ ] **Step 4: Implement archive validation and creation**

`validate_archive_names` must normalize every name with `PurePosixPath`, reject absolute paths, `..`, any top-level component other than `resource_predict`, forbidden cache/suffix names, active cluster config paths, and missing required archive paths.

`build_deployment_package` must:

1. Resolve `project_root` and choose `project_root / "dist"` unless `output_dir` is supplied.
2. Create `resource_predict_YYYYMMDD_HHMMSS.zip`; fail if that final path already exists.
3. Create a temporary file inside the output directory.
4. Write each source with `ZIP_DEFLATED` at `resource_predict/<relative-posix-path>`.
5. Reopen and validate the ZIP names.
6. Rename the temporary file to the final name only after validation.
7. Delete the temporary file and any incomplete final path in `finally` on failure.
8. Return `PackageResult(final_path, len(files), final_path.stat().st_size)`.

- [ ] **Step 5: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_deployment_package.py -q`

Expected: PASS, including missing-required-file, existing-output, forbidden-archive-name, and partial-file cleanup tests.

- [ ] **Step 6: Commit the archive builder**

```bash
git add tools/build_deployment_package.py tests/test_deployment_package.py
git commit -m "feat: add minimal deployment package builder"
```

### Task 2: Add the double-click entry point and validate a real package

**Files:**
- Create: `一键打包内网部署.bat`
- Modify: `.gitignore`
- Modify: `tests/test_deployment_package.py`

**Interfaces:**
- Consumes: `tools/build_deployment_package.py` CLI exit status and Chinese summary.
- Produces: a double-clickable root command that prefers `.venv\Scripts\python.exe` and falls back to `python` on PATH.

- [ ] **Step 1: Add a failing batch-entry contract test**

Read `一键打包内网部署.bat` as UTF-8 and assert it contains `%~dp0`, `.venv\Scripts\python.exe`, `tools\build_deployment_package.py`, `where python`, `pause`, and `exit /b`.

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_deployment_package.py -q`

Expected: FAIL because the batch file does not exist.

- [ ] **Step 3: Implement the CLI and batch launcher**

Add `main(argv: Sequence[str] | None = None) -> int` to the Python builder using `argparse` options `--project-root` and `--output-dir`. On success print the absolute ZIP path, file count, and MiB size; on `PackageBuildError` or `OSError` print `打包失败：<message>` to stderr and return `1`.

Create the batch entry with this control flow:

```bat
@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
if exist "%PYTHON_EXE%" goto run_package
where python >nul 2>nul
if errorlevel 1 goto python_missing
set "PYTHON_EXE=python"
:run_package
"%PYTHON_EXE%" tools\build_deployment_package.py --project-root "%CD%"
set "PACKAGE_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %PACKAGE_EXIT%
:python_missing
echo [ERROR] Python not found.
echo.
pause
exit /b 1
```

Save the batch file as UTF-8 without a BOM only if `cmd.exe` correctly displays it; keep the executable messages ASCII to avoid relying on Windows code-page behavior.

Add `dist/` to `.gitignore` so generated deployment archives cannot be staged accidentally.

- [ ] **Step 4: Run focused tests and build the real archive**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_deployment_package.py -q
.\.venv\Scripts\python.exe tools\build_deployment_package.py --project-root .
```

Expected: tests pass and one new `dist/resource_predict_<timestamp>.zip` is reported.

- [ ] **Step 5: Inspect the real ZIP manifest**

Open the generated ZIP with Python's `zipfile`, run `validate_archive_names`, and print its members. Confirm `runtime_config.json` and ECharts exist, the archive has one `resource_predict/` top level, and no active cluster configs, caches, outputs, tests, docs, tools, `.venv`, or old ZIP files appear.

- [ ] **Step 6: Run full regression checks and clean caches**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall app.py resource_predict tools tests
.\.venv\Scripts\python.exe -m pyflakes app.py resource_predict tools tests
.\.venv\Scripts\python.exe -m vulture app.py resource_predict tools tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/js/*.mjs
```

Expected: all commands exit 0. Remove project `__pycache__` directories outside `.venv`, then confirm `git status --short` contains only intended changes, the generated ignored `dist/` artifact, and the pre-existing untracked `.codex_tmp/` and `.qoder/` directories.

- [ ] **Step 7: Commit the launcher**

```bash
git add 一键打包内网部署.bat tools/build_deployment_package.py tests/test_deployment_package.py .gitignore
git commit -m "feat: add one-click intranet deployment package"
```
