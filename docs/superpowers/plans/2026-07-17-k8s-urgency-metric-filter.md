# K8S Urgency Metric Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure K8S Workload urgency ignores disk statistics, actions, and target changes while OpenStack VM urgency retains disk scoring.

**Architecture:** Resolve the normalized resource type once in `compute_urgency_breakdown()` and derive a resource-specific urgency metric allowlist. Reuse that type boundary for metric contributions and target-spec change dimensions so stale K8S disk fields cannot affect any urgency component; keep the output schema and frontend unchanged.

**Tech Stack:** Python 3, `unittest` assertions executed by pytest, existing `resource_type_of()` normalization, Markdown documentation.

## Global Constraints

- OpenStack VM urgency uses `cpu`, `memory`, and `disk`; K8S Workload urgency uses only `cpu` and `memory`.
- K8S residual `disk` fields must be ignored without errors or historical artifact rewrites.
- Do not change existing weights, thresholds, output schema, K8S decision logic, or VM disk behavior.
- Keep K8S current request/limit specs container-granular under `spec.containers`.
- Run Python commands through `.\.venv\Scripts\python.exe` in this Windows workspace.
- Preserve unrelated changes and remove project `__pycache__` directories outside `.venv` after checks.

---

## File Structure

- Modify `resource_predict/services/urgency.py`: select allowed metrics and target-change dimensions by resource type.
- Modify `tests/test_urgency.py`: reproduce stale K8S disk data and protect VM disk scoring.
- Modify `docs/architecture.md`: document resource-specific urgency metric scope.

### Task 1: Enforce Resource-Type Urgency Boundaries

**Files:**
- Modify: `tests/test_urgency.py`
- Modify: `resource_predict/services/urgency.py`

**Interfaces:**
- Consumes: `resource_type_of(item: dict) -> str`, `compute_urgency_breakdown(item, cfg) -> dict`, and `compute_urgency_score(item, cfg) -> float`.
- Produces: unchanged urgency APIs whose K8S results ignore every disk field while VM results retain disk contributions.

- [ ] **Step 1: Write the failing K8S residual-disk regression test**

Add `import copy` and this test to `UrgencyScoreTest`:

```python
def test_k8s_residual_disk_data_does_not_affect_urgency(self):
    clean = self._k8s_scale_in_item(
        analysis_only=False,
        ready_for_execution=True,
        target_spec={"replicas": 1},
    )
    dirty = copy.deepcopy(clean)
    dirty["spec"]["disk_gb"] = 100
    dirty["scaling_advice"]["target_spec"]["disk_gb"] = 50
    dirty["scaling_advice"]["metric_actions"]["disk"] = "scale_in_candidate"
    dirty["scaling_advice"]["stats"]["disk"] = {
        "avg": 0.01,
        "p95": 0.01,
        "peak": 0.01,
        "gap": 0.0,
    }

    clean_breakdown = compute_urgency_breakdown(clean, settings.decision)
    dirty_breakdown = compute_urgency_breakdown(dirty, settings.decision)

    self.assertEqual(dirty_breakdown["score"], clean_breakdown["score"])
    self.assertEqual(
        [entry["metric"] for entry in dirty_breakdown["metric_scores"]],
        ["cpu", "memory"],
    )
```

- [ ] **Step 2: Write the VM disk compatibility test**

Add this test to the same class:

```python
def test_vm_disk_signal_still_contributes_to_urgency(self):
    item = {
        "resource_id": "vm:cluster-a:server-1",
        "resource_type": "openstack_vm",
        "spec": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100},
        "scaling_advice": {
            "action": "scale_out",
            "confidence": "high",
            "metric_actions": {"disk": "scale_out"},
            "risk_profile": {"risk_score": 80.0},
            "stats": {
                "disk": {
                    "avg": 0.9,
                    "p95": 0.95,
                    "peak": 0.98,
                    "gap": 0.08,
                }
            },
            "target_spec": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 150},
        },
    }

    breakdown = compute_urgency_breakdown(item, settings.decision)

    self.assertEqual([entry["metric"] for entry in breakdown["metric_scores"]], ["disk"])
    self.assertGreater(breakdown["metric_scores"][0]["value"], 0.0)
```

- [ ] **Step 3: Run the focused tests and verify the K8S case fails**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_urgency.py -q
```

Expected: the K8S test fails because the dirty score is greater and `metric_scores` contains `disk`; the VM test passes.

- [ ] **Step 4: Implement resource-specific metric and target-dimension selection**

After validating `advice` in `compute_urgency_breakdown()`, add:

```python
    resource_type = resource_type_of(item)
    urgency_metrics = ("cpu", "memory") if resource_type == "k8s_workload" else ("cpu", "memory", "disk")
```

Inside `_target_change_score()`, replace the target-dimension setup with:

```python
        vm_dims = ("cpu_cores", "memory_gb", "disk_gb")
        k8s_dims = ("cpu_request_cores", "cpu_cores", "memory_request_gb", "memory_gb")
        all_dims = k8s_dims if resource_type == "k8s_workload" else vm_dims
```

Replace the fixed contribution loop with:

```python
    for metric in urgency_metrics:
```

Do not mutate input dictionaries; the allowlist only controls which values are read into the score.

- [ ] **Step 5: Run the focused tests and verify they pass**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_urgency.py -q
```

Expected: all urgency tests pass, including clean/dirty K8S score equality and VM disk preservation.

- [ ] **Step 6: Commit the tested behavior change**

```bash
git add resource_predict/services/urgency.py tests/test_urgency.py
git commit -m "fix: exclude disk from k8s urgency"
```

### Task 2: Document the Scope and Run Full Regression

**Files:**
- Modify: `docs/architecture.md`

**Interfaces:**
- Consumes: resource-specific urgency behavior from Task 1.
- Produces: documented VM/K8S metric scopes and a fully verified repository state.

- [ ] **Step 1: Update urgency metric contribution documentation**

Immediately after the opening paragraph under `#### 紧急度指标贡献`, add:

```markdown
参与紧急度计算的指标按资源类型过滤：OpenStack VM 使用 CPU、内存和磁盘；K8S Workload 仅使用 CPU 和内存。K8S 旧产物或异常输入中即使残留 `disk` 统计、动作或目标规格，磁盘也不会进入指标贡献、多指标加成或目标变化分。
```

- [ ] **Step 2: Run the focused urgency suite**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_urgency.py -q
```

Expected: all urgency tests pass.

- [ ] **Step 3: Run the full project regression checks**

Run separately:

```powershell
.\.venv\Scripts\python.exe -m compileall -q app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\python.exe -m pyflakes app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests
.\.venv\Scripts\vulture.exe app.py check_outputs.py generate_forecasts.py ingest_k8s_workloads.py resource_predict tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all commands exit 0; compileall, pyflakes, and vulture report no findings; pytest reports the complete suite passing.

- [ ] **Step 4: Remove generated caches outside `.venv`**

Enumerate caches first:

```powershell
Get-ChildItem -LiteralPath . -Directory -Recurse -Filter __pycache__ |
    Where-Object { $_.FullName -notlike "*\.venv\*" } |
    Select-Object -ExpandProperty FullName
```

Then resolve and validate every target before removal:

```powershell
$repoRoot = (Resolve-Path -LiteralPath .).Path
$repoPrefix = $repoRoot + [IO.Path]::DirectorySeparatorChar
$cacheDirs = Get-ChildItem -LiteralPath . -Directory -Recurse -Filter __pycache__ |
    Where-Object { $_.FullName -notlike "*\.venv\*" }
foreach ($cacheDir in $cacheDirs) {
    $resolvedCache = $cacheDir.FullName
    if (-not $resolvedCache.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $resolvedCache -like "*\.venv\*") {
        throw "Unsafe cache target: $resolvedCache"
    }
    Remove-Item -LiteralPath $resolvedCache -Recurse -Force
}
```

Expected: only `__pycache__` directories beneath the repository and outside `.venv` are removed.

- [ ] **Step 5: Review the final diff and state**

```bash
git diff --check
git diff -- resource_predict/services/urgency.py tests/test_urgency.py docs/architecture.md
git status --short
```

Expected: no whitespace errors; only the approved filter, tests, and documentation are changed; unrelated user changes remain untouched.

- [ ] **Step 6: Commit documentation after successful regression**

```bash
git add docs/architecture.md
git commit -m "docs: clarify urgency metric scope"
```
