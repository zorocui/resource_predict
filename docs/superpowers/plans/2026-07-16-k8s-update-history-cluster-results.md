# K8S Update History Per-Cluster Results Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record and display the success or failure of every K8S Prometheus cluster involved in one update, including an overall `partial_success` state.

**Architecture:** Add a structured K8S Provider entry point that returns both Workload items and normalized per-cluster results while preserving the existing list-returning Provider wrapper. Pass the structured results through K8S ingest, the shared update status/history store, the history API, and the existing history-card renderer. Keep successful clusters flowing into upsert and prediction even when peer clusters fail.

**Tech Stack:** Python 3, `unittest`/`unittest.mock`, Flask JSON API, vanilla JavaScript, CSS, Markdown documentation.

## Global Constraints

- Preserve the existing `k8s_workload_prometheus_provider()` list-returning interface for current callers.
- Use only `success` and `failed` inside `cluster_results`; use `success`, `partial_success`, or `failed` for the overall record.
- A target that returns zero aggregatable Workloads is a failed cluster result.
- Missing requested clusters and targets skipped by `fail_fast` must still receive failed cluster results.
- Do not store Prometheus URLs, Bearer tokens, Basic Auth values, or request headers in update history.
- Do not restore automatic updater startup in `app.py` or change Prometheus queries, prediction models, or scaling decisions.
- Keep old history files and non-K8S records compatible by normalizing missing `cluster_results` to `[]`.
- Run project Python commands with `.\.venv\Scripts\python.exe`; never use system `python`.
- Preserve unrelated worktree changes and remove project `__pycache__` directories outside `.venv` after checks.

---

### Task 1: Return structured per-cluster Provider results

**Files:**
- Modify: `resource_predict/providers/k8s_prometheus.py:83-182`
- Test: `tests/test_k8s_workload_provider.py`

**Interfaces:**
- Produces: `fetch_k8s_workload_prometheus_result(*, resources: int, n: int, freq: str, clusters: Optional[Iterable[str]] = None, history_hours: Optional[float] = None) -> Dict[str, Any]`.
- Produces result shape: `{"items": List[Dict[str, Any]], "cluster_results": List[Dict[str, Any]]}`.
- Preserves: `k8s_workload_prometheus_provider(...) -> List[Dict[str, Any]]`, implemented as a compatibility wrapper over the structured function.
- Each cluster result contains exactly `cluster`, `status`, `resources_fetched`, `elapsed_seconds`, and `error`.

- [ ] **Step 1: Write failing tests for successful, partial, empty, missing, and fail-fast results**

Add imports and helpers to `tests/test_k8s_workload_provider.py`:

```python
from types import SimpleNamespace


def _target(cluster: str) -> PrometheusTarget:
    return PrometheusTarget(
        cluster=cluster,
        prometheus_url=f"http://{cluster}.example",
        namespace_regex="",
        bearer_token="",
        basic_auth="",
        history_days=1,
        step_seconds=300,
        request_timeout_seconds=5,
    )
```

Add these tests to `K8SWorkloadProviderTest`:

```python
def test_structured_fetch_reports_all_clusters_successful(self):
    targets = [_target("cluster-a"), _target("cluster-b")]

    def fake_fetch(target, limit, *, history_hours=None):
        return [{"resource_id": f"k8s:{target.cluster}:ns:deployment:api"}]

    with patch.object(provider, "_resolve_targets", return_value=targets):
        with patch.object(provider, "_fetch_target", side_effect=fake_fetch):
            with patch.object(provider, "settings", SimpleNamespace(
                k8s_prometheus=SimpleNamespace(fail_fast=False)
            )):
                result = provider.fetch_k8s_workload_prometheus_result(
                    resources=0, n=0, freq="5min"
                )

    self.assertEqual(len(result["items"]), 2)
    self.assertEqual(
        [(row["cluster"], row["status"]) for row in result["cluster_results"]],
        [("cluster-a", "success"), ("cluster-b", "success")],
    )

def test_structured_fetch_reports_partial_success(self):
    targets = [_target("cluster-a"), _target("cluster-b")]

    def fake_fetch(target, limit, *, history_hours=None):
        if target.cluster == "cluster-b":
            raise TimeoutError("Prometheus timeout")
        return [{"resource_id": "k8s:cluster-a:ns:deployment:api"}]

    with patch.object(provider, "_resolve_targets", return_value=targets):
        with patch.object(provider, "_fetch_target", side_effect=fake_fetch):
            with patch.object(provider, "settings", SimpleNamespace(
                k8s_prometheus=SimpleNamespace(fail_fast=False)
            )):
                result = provider.fetch_k8s_workload_prometheus_result(
                    resources=0, n=0, freq="5min"
                )

    self.assertEqual(len(result["items"]), 1)
    self.assertEqual(
        [(row["cluster"], row["status"]) for row in result["cluster_results"]],
        [("cluster-a", "success"), ("cluster-b", "failed")],
    )
    self.assertEqual(result["cluster_results"][0]["resources_fetched"], 1)
    self.assertIn("Prometheus timeout", result["cluster_results"][1]["error"])

def test_structured_fetch_marks_empty_and_missing_clusters_failed(self):
    with patch.object(provider, "_resolve_targets", return_value=[_target("cluster-a")]):
        with patch.object(provider, "_fetch_target", return_value=[]):
            with patch.object(provider, "settings", SimpleNamespace(
                k8s_prometheus=SimpleNamespace(fail_fast=False)
            )):
                result = provider.fetch_k8s_workload_prometheus_result(
                    resources=0,
                    n=0,
                    freq="5min",
                    clusters=["cluster-a", "cluster-missing"],
                )

    self.assertEqual(result["items"], [])
    by_cluster = {row["cluster"]: row for row in result["cluster_results"]}
    self.assertEqual(by_cluster["cluster-a"]["status"], "failed")
    self.assertIn("未返回可聚合", by_cluster["cluster-a"]["error"])
    self.assertEqual(by_cluster["cluster-missing"]["status"], "failed")
    self.assertIn("未找到 K8S Prometheus 集群配置", by_cluster["cluster-missing"]["error"])

def test_structured_fetch_records_fail_fast_targets_without_calling_them(self):
    targets = [_target("cluster-a"), _target("cluster-b")]
    calls = []

    def fake_fetch(target, limit, *, history_hours=None):
        calls.append(target.cluster)
        raise RuntimeError("boom")

    with patch.object(provider, "_resolve_targets", return_value=targets):
        with patch.object(provider, "_fetch_target", side_effect=fake_fetch):
            with patch.object(provider, "settings", SimpleNamespace(
                k8s_prometheus=SimpleNamespace(fail_fast=True)
            )):
                result = provider.fetch_k8s_workload_prometheus_result(
                    resources=0, n=0, freq="5min"
                )

    self.assertEqual(calls, ["cluster-a"])
    self.assertEqual([row["status"] for row in result["cluster_results"]], ["failed", "failed"])
    self.assertIn("fail_fast", result["cluster_results"][1]["error"])

def test_list_provider_remains_compatible(self):
    structured = {
        "items": [{"resource_id": "k8s:cluster-a:ns:deployment:api"}],
        "cluster_results": [],
    }
    with patch.object(provider, "fetch_k8s_workload_prometheus_result", return_value=structured):
        items = provider.k8s_workload_prometheus_provider(resources=0, n=0, freq="5min")
    self.assertEqual(items, structured["items"])
```

- [ ] **Step 2: Run the Provider tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_k8s_workload_provider.py -q
```

Expected: FAIL because `fetch_k8s_workload_prometheus_result` does not exist and the existing Provider does not expose `cluster_results`.

- [ ] **Step 3: Implement target-result normalization and the structured Provider**

In `resource_predict/providers/k8s_prometheus.py`, add these helpers before the public Provider functions:

```python
def _cluster_fetch_result(
    cluster: str,
    status: str,
    *,
    resources_fetched: int = 0,
    elapsed_seconds: Optional[float] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "cluster": str(cluster),
        "status": "success" if status == "success" else "failed",
        "resources_fetched": max(0, int(resources_fetched)),
        "elapsed_seconds": round(float(elapsed_seconds), 2) if elapsed_seconds is not None else None,
        "error": str(error) if error else None,
    }


def _missing_cluster_result(cluster: str) -> Dict[str, Any]:
    return _cluster_fetch_result(
        cluster,
        "failed",
        error=f"未找到 K8S Prometheus 集群配置: {cluster}",
    )
```

Rename the current multi-target body to the structured entry point and implement it with this control flow:

```python
def fetch_k8s_workload_prometheus_result(
    *,
    resources: int,
    n: int,
    freq: str,
    clusters: Optional[Iterable[str]] = None,
    history_hours: Optional[float] = None,
) -> Dict[str, Any]:
    del n, freq
    cfg = settings.k8s_prometheus
    configured_targets = _resolve_targets()
    requested = [str(value).strip() for value in clusters or [] if str(value).strip()]
    wanted = set(requested)
    targets = [target for target in configured_targets if not wanted or target.cluster in wanted]
    configured_names = {target.cluster for target in targets}
    missing = sorted(wanted - configured_names)
    if not targets and not missing:
        raise ValueError(
            "请配置 settings.k8s_prometheus.clusters，或环境变量 K8S_PROMETHEUS_CLUSTERS"
        )

    limit = int(resources or 0)
    items_out: List[Dict[str, Any]] = []
    cluster_results: List[Dict[str, Any]] = []
    fail_fast_triggered = False

    for target in targets:
        if fail_fast_triggered:
            cluster_results.append(_cluster_fetch_result(
                target.cluster,
                "failed",
                error="因 fail_fast 在前序集群失败后未执行",
            ))
            continue
        target_started_perf = time.perf_counter()
        fetched_count = 0
        try:
            remaining = 0 if limit <= 0 else max(0, limit - len(items_out))
            target_items = _fetch_target(target, remaining, history_hours=history_hours)
            fetched_count = len(target_items)
            if not target_items:
                raise RuntimeError("Prometheus 未返回可聚合的 K8S Workload")
            items_out.extend(target_items)
            cluster_results.append(_cluster_fetch_result(
                target.cluster,
                "success",
                resources_fetched=fetched_count,
                elapsed_seconds=time.perf_counter() - target_started_perf,
            ))
        except Exception as exc:
            cluster_results.append(_cluster_fetch_result(
                target.cluster,
                "failed",
                resources_fetched=fetched_count,
                elapsed_seconds=time.perf_counter() - target_started_perf,
                error=str(exc),
            ))
            logger.error("[k8s_prometheus] fetch target failed: cluster=%s error=%s", target.cluster, exc)
            fail_fast_triggered = bool(cfg.fail_fast)

    cluster_results.extend(_missing_cluster_result(cluster) for cluster in missing)
    return {"items": items_out, "cluster_results": cluster_results}
```

Retain the existing public name and explicit signature as the compatibility wrapper:

```python
def k8s_workload_prometheus_provider(
    *,
    resources: int,
    n: int,
    freq: str,
    clusters: Optional[Iterable[str]] = None,
    history_hours: Optional[float] = None,
) -> List[Dict[str, Any]]:
    result = fetch_k8s_workload_prometheus_result(
        resources=resources,
        n=n,
        freq=freq,
        clusters=clusters,
        history_hours=history_hours,
    )
    return list(result["items"])
```

Keep the existing logger calls for target start, target finish, target failure, and whole-fetch summary in the function. Replace the final summary's removed `len(errors)` expression with:

```python
failed_count = sum(1 for item in cluster_results if item["status"] == "failed")
```

Pass `failed_count` to the existing `errors=%d` placeholder. Do not add `target.prometheus_url` to the returned dictionaries.

- [ ] **Step 4: Run the Provider tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_k8s_workload_provider.py -q
```

Expected: all tests in `tests/test_k8s_workload_provider.py` PASS.

- [ ] **Step 5: Commit the Provider change**

```bash
git add resource_predict/providers/k8s_prometheus.py tests/test_k8s_workload_provider.py
git commit -m "feat: report per-cluster prometheus fetch results"
```

---

### Task 2: Normalize and persist cluster results in update history

**Files:**
- Modify: `resource_predict/services/update_history.py:74-98`
- Modify: `resource_predict/data/updater.py:111-231`
- Test: `tests/test_update_history.py`

**Interfaces:**
- Consumes: cluster result dictionaries produced by Task 1.
- Extends: `mark_external_update_failed(error: str, phase: str = "error", *, cluster_results: Optional[List[Dict[str, Any]]] = None) -> None`.
- Stores: top-level `status` in `{"success", "partial_success", "failed"}` and normalized `cluster_results` in every history record.
- Produces: `_normalize_cluster_results(value: Any) -> List[Dict[str, Any]]` in `services/update_history.py`.

- [ ] **Step 1: Write failing normalization and integration tests**

Add to `UpdateHistoryStoreTest`:

```python
def test_partial_success_and_cluster_results_are_normalized(self):
    with tempfile.TemporaryDirectory() as tmp:
        append_update_history(
            {
                "status": "partial_success",
                "finished_at": 20,
                "cluster_results": [
                    {
                        "cluster": "cluster-a",
                        "status": "success",
                        "resources_fetched": "3",
                        "elapsed_seconds": "1.236",
                        "error": None,
                        "prometheus_url": "http://must-not-persist",
                    },
                    {
                        "cluster": "cluster-b",
                        "status": "failed",
                        "resources_fetched": -1,
                        "error": "timeout",
                    },
                ],
            },
            out_dir=tmp,
        )
        record = get_update_history(out_dir=tmp)[0]

    self.assertEqual(record["status"], "partial_success")
    self.assertEqual(record["cluster_results"][0]["resources_fetched"], 3)
    self.assertEqual(record["cluster_results"][0]["elapsed_seconds"], 1.24)
    self.assertNotIn("prometheus_url", record["cluster_results"][0])
    self.assertEqual(record["cluster_results"][1]["resources_fetched"], 0)

def test_old_history_record_gets_empty_cluster_results(self):
    with tempfile.TemporaryDirectory() as tmp:
        path = update_history_path(tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "records": [{
                "id": "legacy-1",
                "status": "success",
                "finished_at": 10,
            }],
        }), encoding="utf-8")
        record = get_update_history(out_dir=tmp)[0]
    self.assertEqual(record["cluster_results"], [])
```

Extend `test_external_success_writes_one_terminal_record` and add a failure case:

```python
def test_external_partial_success_writes_cluster_results(self):
    cluster_results = [
        {"cluster": "cluster-a", "status": "success", "resources_fetched": 2},
        {"cluster": "cluster-b", "status": "failed", "resources_fetched": 0, "error": "timeout"},
    ]
    with patch.object(updater, "append_update_history", return_value=True) as append:
        updater.mark_external_update_started("fetching", "正在拉取")
        updater.mark_external_update_finished({
            "success": True,
            "status": "partial_success",
            "cluster_results": cluster_results,
        })

    record = append.call_args.args[0]
    self.assertEqual(record["status"], "partial_success")
    self.assertEqual(record["cluster_results"], cluster_results)

def test_external_failure_keeps_cluster_results(self):
    cluster_results = [
        {"cluster": "cluster-a", "status": "failed", "resources_fetched": 0, "error": "timeout"}
    ]
    with patch.object(updater, "append_update_history", return_value=True) as append:
        updater.mark_external_update_started("fetching", "正在拉取")
        updater.mark_external_update_failed("all failed", cluster_results=cluster_results)

    record = append.call_args.args[0]
    self.assertEqual(record["status"], "failed")
    self.assertEqual(record["cluster_results"], cluster_results)
```

- [ ] **Step 2: Run update-history tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_update_history.py -q
```

Expected: FAIL because `partial_success` is normalized to `failed`, `cluster_results` is absent, and `mark_external_update_failed` does not accept the keyword.

- [ ] **Step 3: Add strict history normalization**

In `resource_predict/services/update_history.py`, add:

```python
_TERMINAL_STATUSES = {"success", "partial_success", "failed"}


def _normalize_cluster_results(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    results: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        cluster = str(item.get("cluster") or "").strip()
        if not cluster:
            continue
        elapsed = _optional_float(item.get("elapsed_seconds"))
        results.append({
            "cluster": cluster,
            "status": "success" if item.get("status") == "success" else "failed",
            "resources_fetched": _non_negative_int(item.get("resources_fetched")),
            "elapsed_seconds": round(elapsed, 2) if elapsed is not None else None,
            "error": str(item.get("error")) if item.get("error") else None,
        })
    return results
```

At the start of `_normalize_record`, normalize the status and include the new field in the returned dictionary:

```python
status = str(record.get("status") or "failed")
if status not in _TERMINAL_STATUSES:
    status = "failed"
```

```python
"status": status,
"cluster_results": _normalize_cluster_results(record.get("cluster_results")),
```

Change `_read_records` so records already on disk, including legacy records, pass through the same schema normalization:

```python
valid = [_normalize_record(item) for item in records if isinstance(item, dict)]
```

- [ ] **Step 4: Pass terminal status and copied cluster results through updater state**

Add `"cluster_results": []` to `_update_status`. Reset it in `mark_external_update_started`, then accept a list from metadata using copied dictionaries:

```python
cluster_results = metadata.get("cluster_results")
if isinstance(cluster_results, list):
    _update_status["cluster_results"] = [dict(item) for item in cluster_results if isinstance(item, dict)]
```

Change failure handling to:

```python
def mark_external_update_failed(
    error: str,
    phase: str = "error",
    *,
    cluster_results: Optional[List[Dict[str, Any]]] = None,
) -> None:
    with _lock:
        _update_status["running"] = False
        _update_status["phase"] = phase
        _update_status["last_error"] = error
        _update_status["last_finished_at"] = time.time()
        _update_status["message"] = error
        if cluster_results is not None:
            _update_status["cluster_results"] = [
                dict(item) for item in cluster_results if isinstance(item, dict)
            ]
    _record_current_update_history(error=error)
```

In `mark_external_update_finished`, copy `result["cluster_results"]` into `_update_status`. In `_record_current_update_history`, derive and store the terminal status without collapsing `partial_success`:

```python
requested_status = str(result_data.get("status") or "")
if requested_status not in {"success", "partial_success", "failed"}:
    requested_status = "success" if bool(result_data.get("success")) and not error_text else "failed"
```

```python
"status": requested_status if not error_text else "failed",
"cluster_results": [
    dict(item)
    for item in (result_data.get("cluster_results") or status.get("cluster_results") or [])
    if isinstance(item, dict)
],
```

- [ ] **Step 5: Run update-history tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_update_history.py -q
```

Expected: all tests in `tests/test_update_history.py` PASS.

- [ ] **Step 6: Commit history persistence changes**

```bash
git add resource_predict/services/update_history.py resource_predict/data/updater.py tests/test_update_history.py
git commit -m "feat: persist per-cluster update outcomes"
```

---

### Task 3: Integrate structured results into K8S ingest

**Files:**
- Modify: `resource_predict/services/k8s_ingest.py:18-106`
- Test: `tests/test_cluster_configs.py:104-173`

**Interfaces:**
- Consumes: `fetch_k8s_workload_prometheus_result()` from Task 1.
- Consumes: `mark_external_update_failed(..., cluster_results=...)` from Task 2.
- Produces: K8S ingest result with `status` and `cluster_results` in addition to existing update counts.
- Overall status: all successful clusters plus successful upsert is `success`; mixed cluster results plus successful upsert is `partial_success`; no successful clusters or any downstream failure is `failed`.

- [ ] **Step 1: Replace ingest mocks with structured-result tests**

Update the existing K8S ingest tests to patch `fetch_k8s_prometheus_result` and add these assertions:

```python
def test_k8s_ingest_preserves_partial_success(self):
    items = [{"resource_id": "k8s:cluster-a:ns:deployment:api"}]
    cluster_results = [
        {"cluster": "cluster-a", "status": "success", "resources_fetched": 1, "elapsed_seconds": 1.0, "error": None},
        {"cluster": "cluster-b", "status": "failed", "resources_fetched": 0, "elapsed_seconds": 2.0, "error": "timeout"},
    ]
    fake_settings = SimpleNamespace(
        app=SimpleNamespace(out_dir="outputs"),
        k8s_prometheus=SimpleNamespace(
            scheduled_update_interval_minutes=360,
            incremental_overlap_minutes=60,
        ),
    )
    with patch.object(k8s_ingest, "settings", fake_settings):
        with patch.object(k8s_ingest, "_has_existing_k8s_raw_data", return_value=True):
            with patch.object(k8s_ingest, "fetch_k8s_prometheus_result", return_value={
                "items": items,
                "cluster_results": cluster_results,
            }):
                with patch.object(k8s_ingest, "run_upsert_with_data", return_value={"success": True}) as upsert:
                    with patch.object(k8s_ingest, "mark_external_update_finished") as finished:
                        result = k8s_ingest.run_k8s_prometheus_upsert()

    self.assertTrue(result["success"])
    self.assertEqual(result["status"], "partial_success")
    self.assertEqual(result["cluster_results"], cluster_results)
    upsert.assert_called_once()
    finished.assert_called_once_with(result)

def test_k8s_ingest_all_cluster_failures_skip_upsert_and_are_recorded(self):
    cluster_results = [
        {"cluster": "cluster-a", "status": "failed", "resources_fetched": 0, "elapsed_seconds": 1.0, "error": "timeout"}
    ]
    with patch.object(k8s_ingest, "fetch_k8s_prometheus_result", return_value={
        "items": [],
        "cluster_results": cluster_results,
    }):
        with patch.object(k8s_ingest, "run_upsert_with_data") as upsert:
            with patch.object(k8s_ingest, "mark_external_update_failed") as failed:
                with self.assertRaisesRegex(RuntimeError, "所有 K8S Prometheus 集群拉取失败"):
                    k8s_ingest.run_k8s_prometheus_upsert()

    upsert.assert_not_called()
    self.assertEqual(failed.call_args.kwargs["cluster_results"], cluster_results)

def test_k8s_ingest_downstream_failure_overrides_partial_status(self):
    cluster_results = [
        {"cluster": "cluster-a", "status": "success", "resources_fetched": 1, "elapsed_seconds": 1.0, "error": None}
    ]
    with patch.object(k8s_ingest, "fetch_k8s_prometheus_result", return_value={
        "items": [{"resource_id": "k8s:cluster-a:ns:deployment:api"}],
        "cluster_results": cluster_results,
    }):
        with patch.object(k8s_ingest, "run_upsert_with_data", return_value={"success": False, "error": "raw write failed"}):
            with patch.object(k8s_ingest, "mark_external_update_failed") as failed:
                result = k8s_ingest.run_k8s_prometheus_upsert()

    self.assertFalse(result["success"])
    self.assertEqual(result["status"], "failed")
    self.assertEqual(failed.call_args.kwargs["cluster_results"], cluster_results)
```

In the existing full-versus-incremental window tests, replace:

```python
with patch.object(k8s_ingest, "fetch_k8s_prometheus_items", return_value=items) as fetch:
```

with:

```python
with patch.object(k8s_ingest, "fetch_k8s_prometheus_result", return_value={
    "items": items,
    "cluster_results": [{
        "cluster": "cluster-a",
        "status": "success",
        "resources_fetched": len(items),
        "elapsed_seconds": 1.0,
        "error": None,
    }],
}) as fetch:
```

Keep their exact call assertions as `fetch.assert_called_once_with(["cluster-a"], history_hours=7.0)` and `fetch.assert_called_once_with(["cluster-a"], history_hours=None)` respectively.

- [ ] **Step 2: Run cluster-config tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cluster_configs.py -q
```

Expected: FAIL because K8S ingest still calls the list-only Provider helper and does not return `status` or `cluster_results`.

- [ ] **Step 3: Implement structured ingest and overall status derivation**

Replace the Provider import with `fetch_k8s_workload_prometheus_result` and replace `fetch_k8s_prometheus_items` with:

```python
def fetch_k8s_prometheus_result(
    clusters: Optional[Iterable[str]] = None,
    *,
    history_hours: Optional[float] = None,
) -> Dict[str, Any]:
    result = fetch_k8s_workload_prometheus_result(
        resources=0,
        n=0,
        freq="5min",
        clusters=clusters,
        history_hours=history_hours,
    )
    if not isinstance(result, dict):
        raise RuntimeError("Prometheus provider returned an invalid K8S fetch result")
    return result


def _cluster_terminal_status(cluster_results: List[Dict[str, Any]]) -> str:
    succeeded = sum(1 for item in cluster_results if item.get("status") == "success")
    failed = sum(1 for item in cluster_results if item.get("status") == "failed")
    if succeeded and failed:
        return "partial_success"
    if succeeded:
        return "success"
    return "failed"
```

In `run_k8s_prometheus_upsert`, initialize `cluster_results: List[Dict[str, Any]] = []` before `try`. Replace the fetch and terminal handling with:

```python
fetch_result = fetch_k8s_prometheus_result(cluster_list, history_hours=history_hours)
items = list(fetch_result.get("items") or [])
cluster_results = [
    dict(item) for item in fetch_result.get("cluster_results") or [] if isinstance(item, dict)
]
cluster_status = _cluster_terminal_status(cluster_results)
if not items:
    errors = [str(item.get("error")) for item in cluster_results if item.get("error")]
    detail = "；".join(errors) or "Prometheus provider returned no K8S workload resources"
    raise RuntimeError(f"所有 K8S Prometheus 集群拉取失败: {detail}")

result = run_upsert_with_data(items, fail_if_busy=fail_if_busy, out_dir=out_dir)
result = dict(result)
result["cluster_results"] = cluster_results
if not result.get("success"):
    result["status"] = "failed"
    mark_external_update_failed(
        str(result.get("error") or "K8S Prometheus 数据拉取失败"),
        cluster_results=cluster_results,
    )
else:
    result["status"] = cluster_status
    mark_external_update_finished(result)
return result
```

Update the exception handler so all-failed and unexpected downstream exceptions keep the results:

```python
except Exception as exc:
    mark_external_update_failed(str(exc), cluster_results=cluster_results)
    raise
```

- [ ] **Step 4: Run K8S ingest and history tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cluster_configs.py tests/test_update_history.py -q
```

Expected: all selected tests PASS, including incremental/full-window behavior and one terminal history record.

- [ ] **Step 5: Commit K8S ingest integration**

```bash
git add resource_predict/services/k8s_ingest.py tests/test_cluster_configs.py
git commit -m "feat: propagate per-cluster k8s fetch status"
```

---

### Task 4: Render partial success and per-cluster details

**Files:**
- Modify: `static/js/index.js:227-264`
- Modify: `static/css/index.css:1367-1430`
- Test: `tests/test_update_history.py`

**Interfaces:**
- Consumes: history API records containing overall `status` and `cluster_results`.
- Produces: escaped cluster rows in each K8S history card.
- Keeps: records with `cluster_results: []` visually unchanged.

- [ ] **Step 1: Add failing static-render contract assertions**

Extend `test_update_page_contains_history_region_and_refresh_logic`:

```python
self.assertIn('record.status === "partial_success"', script)
self.assertIn('record.cluster_results', script)
self.assertIn('list.escapeHtml(cluster.cluster', script)
self.assertIn('update-history-clusters', script)

stylesheet = (root / "static" / "css" / "index.css").read_text(encoding="utf-8")
self.assertIn(".update-history-item.is-partial", stylesheet)
self.assertIn(".update-history-clusters", stylesheet)
```

- [ ] **Step 2: Run the static contract test and verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_update_history.py::UpdateHistoryIntegrationTest::test_update_page_contains_history_region_and_refresh_logic -q
```

Expected: FAIL because partial-success and cluster-list rendering are absent.

- [ ] **Step 3: Add escaped cluster-detail rendering**

Add these functions before `renderUpdateHistory` in `static/js/index.js`:

```javascript
function updateHistoryStatus(record) {
  if (record.status === "partial_success") {
    return { label: "部分成功", className: "is-partial" };
  }
  if (record.status === "success") {
    return { label: "成功", className: "is-success" };
  }
  return { label: "失败", className: "is-failed" };
}

function renderClusterResults(value) {
  const clusters = Array.isArray(value) ? value : [];
  if (!clusters.length) return "";
  const rows = clusters.map((cluster) => {
    const success = cluster.status === "success";
    const status = success ? "成功" : "失败";
    const count = success ? `${Number(cluster.resources_fetched || 0)} 个 Workload` : "";
    const duration = cluster.elapsed_seconds == null ? "" : formatDuration(cluster.elapsed_seconds);
    const detail = success
      ? [count, duration].filter(Boolean).join(" · ")
      : [cluster.error || "未知错误", duration].filter(Boolean).join(" · ");
    return `
      <li class="${success ? "is-success" : "is-failed"}">
        <strong>${list.escapeHtml(cluster.cluster || "未命名集群")}</strong>
        <span>${status}</span>
        <small>${list.escapeHtml(detail)}</small>
      </li>
    `;
  }).join("");
  return `<ul class="update-history-clusters" aria-label="逐集群拉取结果">${rows}</ul>`;
}
```

Change the start of each history-card render to:

```javascript
const status = updateHistoryStatus(record);
const source = record.task_source || "数据更新";
const windowLabel = record.fetch_window_label || "未指定拉取窗口";
const detail = record.error || record.message || (record.status === "failed" ? "更新失败" : "更新完成");
const clusterResults = renderClusterResults(record.cluster_results);
```

Use `status.className` on `<article>`, `status.label` inside the status pill, and insert `${clusterResults}` after the detail paragraph.

- [ ] **Step 4: Style partial-success cards and compact cluster rows**

Add to `static/css/index.css`:

```css
.update-history-item.is-partial {
  border-left-color: #d97706;
}

.is-partial .update-history-status {
  background: #fffbeb;
  color: #b45309;
}

.update-history-clusters {
  display: grid;
  gap: 5px;
  margin: 9px 0 0;
  padding: 0;
  list-style: none;
}

.update-history-clusters li {
  display: grid;
  grid-template-columns: minmax(120px, auto) auto minmax(0, 1fr);
  gap: 8px;
  align-items: baseline;
  font-size: 11px;
}

.update-history-clusters li > span {
  font-weight: 800;
}

.update-history-clusters li.is-success > span {
  color: #047857;
}

.update-history-clusters li.is-failed > span {
  color: #b91c1c;
}

.update-history-clusters small {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--muted);
}
```

Inside the existing `@media (max-width: 760px)` block beginning near `static/css/index.css:1457`, add:

```css
.update-history-clusters li {
  grid-template-columns: 1fr auto;
}

.update-history-clusters small {
  grid-column: 1 / -1;
}
```

- [ ] **Step 5: Run history tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_update_history.py -q
```

Expected: all history tests PASS.

- [ ] **Step 6: Commit frontend history rendering**

```bash
git add static/js/index.js static/css/index.css tests/test_update_history.py
git commit -m "feat: show k8s cluster outcomes in update history"
```

---

### Task 5: Document the API contract and run full regression checks

**Files:**
- Modify: `docs/api-reference.md:56-61`
- Modify: `docs/architecture.md:226-234`

**Interfaces:**
- Documents: `GET /api/update-history` overall states and per-cluster result fields.
- Documents: partial-success data flow and successful-cluster continuation.

- [ ] **Step 1: Add the history response example to API documentation**

After the update-history endpoint description in `docs/api-reference.md`, add:

```json
{
  "records": [
    {
      "status": "partial_success",
      "task_source": "页面手动拉取",
      "fetch_window_label": "增量窗口：最近 7 小时",
      "cluster_results": [
        {
          "cluster": "cluster-a",
          "status": "success",
          "resources_fetched": 32,
          "elapsed_seconds": 12.4,
          "error": null
        },
        {
          "cluster": "cluster-b",
          "status": "failed",
          "resources_fetched": 0,
          "elapsed_seconds": 3.1,
          "error": "Prometheus timeout"
        }
      ]
    }
  ]
}
```

State explicitly that overall status is `success`, `partial_success`, or `failed`, while each cluster is only `success` or `failed`.

- [ ] **Step 2: Update architecture data-flow semantics**

Extend the update-history paragraph in `docs/architecture.md` with:

```markdown
K8S Prometheus 更新还保存 `cluster_results`：每个目标集群分别记录成功/失败、Workload 数、耗时和错误。只要成功集群的数据完成 upsert 与预测，多集群混合结果记为 `partial_success`；全部集群失败或后续写入/预测失败时记为 `failed`。部分失败不会丢弃成功集群的数据。
```

- [ ] **Step 3: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_k8s_workload_provider.py tests/test_cluster_configs.py tests/test_update_history.py -q
```

Expected: all focused tests PASS.

- [ ] **Step 4: Run full project regression checks**

Run each command separately from the repository root:

```powershell
.\.venv\Scripts\python.exe -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\python.exe -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\python.exe -m vulture app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all four commands exit with code 0.

- [ ] **Step 5: Remove generated project caches and inspect the final diff**

Use a single PowerShell filesystem flow and verify every resolved deletion target is inside the repository and outside `.venv` before removing it:

```powershell
$root = (Resolve-Path '.').Path
$cacheDirs = Get-ChildItem -LiteralPath $root -Directory -Recurse -Filter '__pycache__' |
  Where-Object { $_.FullName -notlike "$root\.venv\*" }
$cacheDirs | ForEach-Object {
  $resolved = (Resolve-Path -LiteralPath $_.FullName).Path
  if ($resolved.StartsWith($root + '\') -and $resolved -notlike "$root\.venv\*") {
    Remove-Item -LiteralPath $resolved -Recurse -Force
  }
}
git diff --check
git status --short
```

Expected: no project `__pycache__` directories remain outside `.venv`, `git diff --check` emits no errors, and status lists only intended files.

- [ ] **Step 6: Commit documentation and final verification state**

```bash
git add docs/api-reference.md docs/architecture.md
git commit -m "docs: describe per-cluster k8s update results"
```

Confirm `git status --short` is clean after the commit.
