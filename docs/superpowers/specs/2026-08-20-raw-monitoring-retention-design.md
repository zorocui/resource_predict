# Raw Monitoring Data 30-Day Retention Design

## Goal

Limit every locally stored monitoring series to its most recent 30 days of
observations, covering VM metrics, K8S Workload aggregate metrics, and K8S
container metrics, without weakening the existing partial-cluster failure and
prediction-preservation behavior.

## Retention Semantics

- Add a positive integer `collection.retention_days`, defaulting to `30`.
- For each individual metric series, calculate the cutoff from that series'
  newest valid timestamp: `latest_timestamp - retention_days`.
- Retain samples whose timestamps are greater than or equal to the cutoff.
- A stale or temporarily unavailable resource therefore keeps a bounded
  30-day observation window instead of being erased merely because wall-clock
  time advanced while its source was unavailable.
- `collection.history_days` remains the Prometheus full-fetch window and stays
  independent from local retention. Its current value remains `7`.

This design guarantees that no stored series spans more than 30 days. It does
not implement regulatory wall-clock expiry for offline resources. Strict
wall-clock expiry would additionally require resource deletion, prediction
artifact reconciliation, and a global maintenance scheduler, which are outside
this optimization.

## Considered Approaches

### Recommended: trim each series relative to its newest sample

Apply one shared trimming helper after new points are normalized and merged.
The helper works for both newly created resources and existing resources.

Advantages:

- Bounds all active and inactive resource series without emptying stale data.
- Preserves the current rule that failed clusters and absent Workloads retain
  their last usable raw and prediction artifacts.
- Works with historical fixtures, delayed ingestion, and backfills.
- Requires no full-dataset scan or additional scheduler.

### Alternative: trim relative to current UTC time

This gives strict wall-clock deletion, but a resource offline for 30 days would
lose every required metric. The current raw schema rejects empty primary
metrics, so this approach also requires deleting the resource from the raw
index and all prediction manifests/details. It is not selected.

### Alternative: reuse `sliding_window`

The existing option preserves the pre-update point count. It neither expresses
30 calendar days nor handles irregular sampling correctly. It is not selected.

## Architecture and Data Flow

Introduce a focused helper in `resource_predict/data/updater.py` that accepts a
`pandas.Series` and a positive retention-day count and returns the sorted,
deduplicated retained window. Apply it in three paths:

1. Existing resource aggregate metrics, after incremental merge.
2. Existing resource `container_metrics`, after container-series merge.
3. Newly created resources, after all aggregate and container series are
   coerced.

Trimming is treated as a resource change even when incoming timestamps only
overlap existing points. The existing content-addressed raw writer then writes
a new resource shard, atomically changes `raw_index.json`, and removes obsolete
unreferenced shards through its established cleanup path.

No raw-store schema change is required. Predictions continue to be regenerated
only for resources changed by the update.

## Configuration

Add `retention_days: 30` to the `collection` section of
`deploy/runtime_config.json` and to the runtime configuration dataclass and
validator. Project-facing configuration payloads expose the effective value.

The existing legacy `UpdateConfig.sliding_window` remains unchanged for VM
mock/incremental compatibility, but time retention takes precedence after the
merge. It is not used as the 30-day implementation.

## Boundary Behavior

- A sample exactly on the cutoff is retained.
- Irregularly sampled and sparse series are filtered by timestamp, not by point
  count.
- Each container metric uses its own newest timestamp, so one lagging container
  cannot erase another container's history.
- A newly ingested payload containing more than 30 days is trimmed before its
  first raw shard is committed.
- Because the cutoff is relative to an existing latest sample, at least that
  latest valid sample remains and the raw schema never receives an empty
  primary metric solely because of retention.

## Tests and Verification

Unit tests cover the exact cutoff boundary, irregular timestamps, existing
aggregate metrics, container metrics, and newly created resources. Runtime
configuration tests verify the default and reject zero, negative, boolean, and
non-integer retention values.

After implementation, run the focused updater/runtime tests followed by the
project's full Python regression checks: compileall, pyflakes, vulture at 80%
minimum confidence, and `pytest -q`. Remove project `__pycache__` directories
outside `.venv` afterward.

## Documentation

Update `docs/configuration.md` to distinguish the 7-day Prometheus fetch window
from the 30-day local retention window. Update `docs/architecture.md` to explain
the per-series trimming step before atomic raw-shard replacement.
