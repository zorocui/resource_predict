import csv
import gzip
import hashlib
import io
import json
import sqlite3
import zipfile

import pytest
from flask import Flask

from resource_predict.api.forecast_accuracy import register_forecast_accuracy_routes
from resource_predict.services import accuracy_exports
from resource_predict.pipeline.realized_error import _SCHEMA
from resource_predict.services.forecast_accuracy import DB_NAME
from test_forecast_accuracy import add


@pytest.fixture
def api(tmp_path):
    directory = tmp_path / "vm"
    directory.mkdir()
    db = sqlite3.connect(directory / DB_NAME)
    db.executescript(_SCHEMA)
    add(db, rid="=untrusted", target=10000, predicted=.55, actual=.5)
    add(db, rid="=untrusted", target=20000, predicted=.5, actual=.5)
    add(db, rid="other", target=30000, predicted=.8, actual=.5)
    app = Flask(__name__)
    register_forecast_accuracy_routes(app, lambda: [directory], tmp_path / "snapshots")
    yield app.test_client(), db, tmp_path
    db.close()


def test_api_point_summary_export_same_filter_and_zero(api):
    client, _, _ = api
    report = client.get("/api/forecast-accuracy?q=untrusted&page_size=1").get_json()
    assert report["total"] == 2 and len(report["items"]) == 1
    assert report["summary"][0]["hit_rate_5pp"] == 1
    exported = client.get("/api/forecast-accuracy/export.csv?kind=points&q=untrusted")
    rows = list(csv.DictReader(io.StringIO(exported.data.decode("utf-8-sig"))))
    assert len(rows) == 2
    assert rows[0]["resource_id"] == "'=untrusted"
    assert float(rows[1]["error"]) == 0
    summary = client.get("/api/forecast-accuracy/export.csv?kind=summary&q=untrusted")
    assert len(list(csv.DictReader(io.StringIO(summary.data.decode("utf-8-sig"))))) == 1


def test_snapshot_freezes_all_rows_and_survives_source_pruning(api):
    client, db, _ = api
    response = client.post("/api/forecast-accuracy/snapshots", json={"q": "untrusted", "page_size": 1})
    assert response.status_code == 201
    snapshot = response.get_json()
    assert snapshot["point_count"] == 2
    package = client.get(snapshot["download_url"])
    assert hashlib.sha256(package.data).hexdigest() == snapshot["sha256"]
    with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for name, metadata in manifest["files"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == metadata["sha256"]
        points = [json.loads(line) for line in gzip.decompress(archive.read("points.jsonl.gz")).splitlines()]
        candidates = [json.loads(line) for line in gzip.decompress(archive.read("candidates.jsonl.gz")).splitlines()]
        report = json.loads(archive.read("report.json"))
        assert len(points) == report["total"] == 2
        assert len(candidates) == manifest["candidate_count"] == report["coverage"]["candidate_points"]
        assert candidates[0]["predicted_native"] == .55
        assert sum(abs(p["predicted"]-p["actual"]) for p in points) / len(points) == pytest.approx(report["summary"][0]["mae"])
        assert all(p["basis"] and p["provenance"] for p in points)
    db.execute("DELETE FROM points")
    db.commit()
    assert client.get("/api/forecast-accuracy").get_json()["total"] == 0
    assert client.get(snapshot["download_url"]).data == package.data


def test_snapshot_failures_never_publish_partial_zip(api, monkeypatch):
    client, _, base = api
    def fail(_):
        raise OSError("simulated storage failure")
    monkeypatch.setattr(accuracy_exports, "file_hash", fail)
    response = client.post("/api/forecast-accuracy/snapshots", json={})
    assert response.status_code == 503
    assert not list((base / "snapshots").glob("*.zip"))


def test_legacy_reference_does_not_claim_point_accuracy(api):
    client, _, base = api
    old = {"meta": {"generated_at_epoch_ms": 10000}, "rows": [
        {"resource_id": "old", "resource_type": "openstack_vm", "metric": "cpu", "model": "legacy",
         "mae": .2, "rmse": .3, "mape": .4, "p95_error": .5},
    ]}
    (base / "vm" / "forecast_error_report.json").write_text(json.dumps(old), encoding="utf-8")
    report = client.get("/api/forecast-accuracy?source=legacy").get_json()
    assert report["coverage"]["matched_points"] is None
    assert report["summary"] == []
    assert report["items"][0]["status"] == "legacy_unverified"
    assert report["items"][0]["mae"] == .2
    assert client.get("/api/forecast-accuracy?source=legacy&from_ms=1").get_json()["total"] == 0
    assert client.get("/api/forecast-accuracy/export.csv?source=legacy&kind=points").status_code == 400
    snapshot = client.post("/api/forecast-accuracy/snapshots", json={"source": "legacy"}).get_json()
    assert snapshot["point_count"] == 0 and snapshot["record_count"] == 1
    with zipfile.ZipFile(io.BytesIO(client.get(snapshot["download_url"]).data)) as archive:
        assert "legacy_rows.jsonl.gz" in archive.namelist()
        assert "points.jsonl.gz" not in archive.namelist()


def test_empty_read_does_not_create_evidence_or_fake_metrics(tmp_path):
    app = Flask(__name__)
    register_forecast_accuracy_routes(app, lambda: [tmp_path / "missing"], tmp_path / "snapshots")
    response = app.test_client().get("/api/forecast-accuracy").get_json()
    assert response["total"] == 0 and response["summary"] == []
    assert response["coverage"]["observation_coverage"] is None
    assert not list(tmp_path.iterdir())


def test_invalid_filters_and_snapshot_paths_are_rejected(api):
    client, _, _ = api
    for query in ("source=invalid", "page=0", "page_size=201", "from_ms=2&to_ms=1", "from_ms=9e99", "level=all"):
        assert client.get("/api/forecast-accuracy?"+query).status_code == 400
    assert client.post("/api/forecast-accuracy/snapshots", json=[]).status_code == 400
    assert client.get("/api/forecast-accuracy/snapshots/not-a-valid-id/download").status_code == 404
