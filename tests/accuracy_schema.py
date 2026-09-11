"""Legacy accuracy database schema for read-only API test fixtures."""
_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
 name TEXT PRIMARY KEY, issued_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS curves (
 id INTEGER PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(name) ON DELETE CASCADE,
 resource_id TEXT NOT NULL, container TEXT NOT NULL, metric TEXT NOT NULL,
 model TEXT NOT NULL, unit TEXT NOT NULL, data_end_ms INTEGER, issued_ms INTEGER NOT NULL,
 basis TEXT NOT NULL, provenance TEXT NOT NULL, eligible INTEGER NOT NULL,
 UNIQUE(batch, resource_id, container, metric)
);
CREATE TABLE IF NOT EXISTS points (
 curve_id INTEGER NOT NULL REFERENCES curves(id) ON DELETE CASCADE,
 target_ms INTEGER NOT NULL, predicted REAL NOT NULL, actual REAL,
 scored_at_ms INTEGER, observation_source TEXT, skip_reason TEXT,
 PRIMARY KEY(curve_id, target_ms)
);
CREATE INDEX IF NOT EXISTS curves_lookup ON curves(resource_id, container, metric);
CREATE INDEX IF NOT EXISTS points_pending ON points(curve_id, actual, target_ms);
CREATE INDEX IF NOT EXISTS curves_batch ON curves(batch);
CREATE INDEX IF NOT EXISTS points_target ON points(target_ms,curve_id);
CREATE TABLE IF NOT EXISTS holdout_curves (
 id INTEGER PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(name) ON DELETE CASCADE,
 resource_id TEXT NOT NULL, container TEXT NOT NULL, metric TEXT NOT NULL,
 model TEXT NOT NULL, unit TEXT NOT NULL, data_end_ms INTEGER, issued_ms INTEGER NOT NULL,
 basis TEXT NOT NULL, provenance TEXT NOT NULL, evaluation TEXT NOT NULL, eligible INTEGER NOT NULL,
 UNIQUE(batch, resource_id, container, metric, model)
);
CREATE TABLE IF NOT EXISTS holdout_points (
 curve_id INTEGER NOT NULL REFERENCES holdout_curves(id) ON DELETE CASCADE,
 target_ms INTEGER NOT NULL, predicted REAL, actual REAL, skip_reason TEXT,
 PRIMARY KEY(curve_id, target_ms)
);
CREATE INDEX IF NOT EXISTS holdout_curves_batch ON holdout_curves(batch);
CREATE INDEX IF NOT EXISTS holdout_curves_lookup ON holdout_curves(resource_id,container,metric);
CREATE INDEX IF NOT EXISTS holdout_points_target ON holdout_points(target_ms,curve_id);
"""
