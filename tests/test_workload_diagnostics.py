import json
from zipfile import ZipFile

import pytest

from tools.export_workload_diagnostics import export_workload


def test_export_selects_only_named_workload_and_preserves_sources(tmp_path):
    base = tmp_path / "k8s"
    base.mkdir()
    (base / "details").mkdir()
    rid = "k8s:c:ns:deployment:app"
    other = "k8s:c:ns:deployment:other"
    def write(name, obj):
        (base / name).write_text(json.dumps(obj), encoding="utf-8")
    write("summary_index.json", {"resources": [
        {"resource_id": rid, "detail_ref": {"file": "part.json", "offset": 0}},
        {"resource_id": other, "detail_ref": {"file": "part.json", "offset": 1}},
    ]})
    write("details/part.json", {"resources": [{"resource_id": rid}, {"resource_id": other}]})
    write("raw_index.json", {"resources": {rid: {"file": "raw.json"}}})
    write("raw.json", {"resource_id": rid, "cpu_request": [0, 0.5]})
    write("forecast_accuracy_summary.json", {"version": 2, "rows": [{"resource_id": rid, "accuracy": 0}, {"resource_id": other}]})
    (base / "manifest.json").write_text("not JSON", encoding="utf-8")
    before = {p: p.read_bytes() for p in base.rglob("*") if p.is_file()}
    path = export_workload("app", base, tmp_path / "export")
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        accuracy = json.loads(archive.read("forecast_accuracy_summary.json"))
        assert accuracy["rows"] == [{"resource_id": rid, "accuracy": 0}]
        assert json.loads(archive.read("detail.json"))["resource_id"] == rid
        assert "manifest.json" not in archive.namelist()
        assert len(json.loads(archive.read("summary.json"))["resources"]) == 1
    assert all(p.read_bytes() == content for p, content in before.items())
    with pytest.raises(ValueError, match="未找到"):
        export_workload("missing", base, tmp_path / "export")
    write("summary_index.json", {"resources": [{"resource_id": rid}, {"resource_id": "k8s:other:ns:deployment:app"}]})
    with pytest.raises(ValueError, match="同名"):
        export_workload("app", base, tmp_path / "export")


def test_export_rejects_external_detail_path(tmp_path):
    (tmp_path / "summary_index.json").write_text(json.dumps({"resources": [
        {"resource_id": "k8s:c:ns:deployment:app", "detail_ref": {"file": "../../outside.json", "offset": 0}}
    ]}), encoding="utf-8")
    with pytest.raises(ValueError, match="超出"):
        export_workload("app", tmp_path, tmp_path / "export")
