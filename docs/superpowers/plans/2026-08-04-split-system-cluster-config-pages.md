# Split System and Cluster Configuration Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the mixed configuration workspace into independent system-configuration and cluster-configuration navigation pages without changing the backend configuration contract.

**Architecture:** Both pages load one complete `/api/system-config` snapshot into shared frontend state. Each save action replaces only its owned section in that snapshot and submits a complete payload, preserving atomic validation and preventing the other page's values from being erased.

**Tech Stack:** Flask/Jinja HTML, vanilla JavaScript, CSS, Python unittest/pytest

## Global Constraints

- Keep `GET/PUT /api/system-config` and all existing configuration storage files unchanged.
- System configuration owns only runtime collection, prediction, and decision fields.
- Cluster configuration owns scaling-control clusters, K8S Prometheus connections, diagnosis, and immediate fetch actions.
- A save from either page must preserve all values owned by the other page.
- Preserve UTF-8 Chinese text and use the local Windows virtual environment for Python checks.
- Preserve unrelated `.codex_tmp/` and `.qoder/` worktree content.

---

### Task 1: Split navigation and template ownership

**Files:**
- Modify: `templates/index.html`
- Modify: `static/js/app-state.js`
- Test: `tests/test_system_config.py`

**Interfaces:**
- Produces: `system-config-view`, `cluster-config-view`, `system-config-save`, `cluster-config-save`, `system-config-message`, and `cluster-config-message` DOM elements.
- Consumes: Existing configuration list element IDs so render functions remain compatible.

- [ ] **Step 1: Add a failing template structure test**

Add a test that reads `templates/index.html` as UTF-8 and asserts both `data-view="system-config"` and `data-view="cluster-config"`, both corresponding view IDs, and both save-button IDs are present; assert `collection-config-list` occurs inside the system view and `vm-cluster-list` occurs inside the cluster view.

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_system_config.py -q`

Expected: FAIL because the split view IDs do not exist.

- [ ] **Step 3: Split the template and element registry**

Replace the `configs` navigation entry with separate `system-config` and `cluster-config` entries. Move only runtime sections into `system-config-view`, move only cluster sections into `cluster-config-view`, give each page its own heading, save button, and message element, and register `systemConfigSave`/`systemConfigMessage` in `app-state.js` while retaining cluster-specific references.

- [ ] **Step 4: Run the focused test and verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_system_config.py -q`

Expected: PASS.

### Task 2: Separate load, messages, and save ownership

**Files:**
- Modify: `static/js/index.js`
- Test: `tests/test_system_config.py`

**Interfaces:**
- Produces: `refreshSystemConfig()`, `saveSystemConfig()`, `saveClusterConfigs()`, and page-specific message updates.
- Consumes: `app.state.clusterConfigPayload`, `collectRuntimeConfig()`, `collectClusterConfigs()`, and `api.postJson()`.

- [ ] **Step 1: Extend the static JavaScript contract test**

Read `static/js/index.js` as UTF-8 and assert that both configuration view names trigger `refreshSystemConfig`, that `saveSystemConfig` uses cached `vm_scaling_clusters` and `k8s_prometheus_clusters`, and that `saveClusterConfigs` uses cached `runtime`.

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_system_config.py -q`

Expected: FAIL because only the combined save function exists.

- [ ] **Step 3: Implement shared loading and independent saves**

Rename the loader to `refreshSystemConfig` and invoke it for either config view. Add page-specific message helpers. Implement system save with edited runtime plus cached clusters, and cluster save with cached runtime plus edited clusters. After either successful PUT, update the shared snapshot and rerender both page sections. Bind each save button independently; keep diagnose/fetch/remove feedback on the cluster page.

- [ ] **Step 4: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_system_config.py -q`

Expected: PASS.

### Task 3: Regression and browser verification

**Files:**
- Modify only if verification exposes a defect: `static/css/index.css`, `templates/index.html`, `static/js/index.js`, `static/js/app-state.js`

**Interfaces:**
- Consumes: The two page routes and the unchanged system-config API.
- Produces: Verified desktop and narrow-screen configuration flows.

- [ ] **Step 1: Run static and Python regression checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m compileall app.py resource_predict tests
.\.venv\Scripts\python.exe -m pyflakes app.py resource_predict tests
.\.venv\Scripts\python.exe -m vulture app.py resource_predict tests --min-confidence 80
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/js/*.mjs
```

Expected: all commands exit 0.

- [ ] **Step 2: Remove generated Python caches outside `.venv`**

Resolve every project `__pycache__` directory, verify it is under the repository and not under `.venv`, then remove it with native PowerShell `Remove-Item -LiteralPath ... -Recurse -Force`.

- [ ] **Step 3: Verify the UI through `python app.py`**

Start the application with `.\.venv\Scripts\python.exe app.py`, open the local page, and confirm desktop and narrow widths show two navigation entries, system fields only on the system page, cluster fields/actions only on the cluster page, and page-specific save status without horizontal overflow.

