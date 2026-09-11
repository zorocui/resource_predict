from copy import deepcopy
from unittest.mock import patch

import pytest

from resource_predict.data.raw_store import RawResourceStore
from resource_predict.data.updater import run_scoped_upsert_with_data


def vm_payload(resource_id):
    return {
        "resource_id": resource_id,
        "resource_type": "openstack_vm",
        "metrics": {
            metric: {"timestamps": [1789084800000, 1789085100000], "values": [0.2, 0.3]}
            for metric in ("cpu", "memory", "disk")
        },
    }


@pytest.mark.parametrize("invalid_metric", [None, {}, {"timestamps": [], "values": []},
                                            {"timestamps": [1789084800000], "values": [0.2, 0.3]}])
def test_upsert_skips_invalid_new_resource_and_keeps_existing_updates(tmp_path, invalid_metric):
    with patch("resource_predict.data.updater.scoped_out_dir", return_value=tmp_path / "vm"), \
         patch("resource_predict.data.updater._record_current_update_history"), \
         patch("resource_predict.pipeline.generate_predictions_only", return_value=[]) as predict:
        assert run_scoped_upsert_with_data([vm_payload("existing")])["success"]
        predict.reset_mock()
        existing = vm_payload("existing")
        existing["metrics"]["cpu"]["values"] = [0.4, 0.5]
        invalid = vm_payload("invalid")
        invalid["metrics"]["cpu"] = deepcopy(invalid_metric)
        result = run_scoped_upsert_with_data([existing, invalid, vm_payload("new")])

    assert result["success"], result
    assert result["resources_updated"] == 1
    assert result["resources_created"] == 1
    assert result["created_resource_ids"] == ["new"]
    skipped = [warning for warning in result["warnings"] if "invalid" in warning]
    assert len(skipped) == 1
    assert "cpu" in skipped[0]
    resources = {item["resource_id"]: item for item in RawResourceStore(tmp_path / "vm").read_many()}
    assert set(resources) == {"existing", "new"}
    assert resources["existing"]["cpu"].tolist() == [0.4, 0.5]
    assert predict.call_args.kwargs["resource_ids"] == ["existing", "new"]


def test_upsert_all_invalid_keeps_warnings_without_writing_or_predicting(tmp_path):
    invalid = vm_payload("invalid")
    invalid["metrics"]["cpu"] = {"timestamps": [], "values": []}
    with patch("resource_predict.data.updater.scoped_out_dir", return_value=tmp_path / "vm"), \
         patch("resource_predict.data.updater._record_current_update_history"), \
         patch("resource_predict.pipeline.generate_predictions_only") as predict:
        result = run_scoped_upsert_with_data([invalid])

    assert not result["success"]
    assert result["resources_created"] == 0
    assert "invalid" in result["warnings"][0]
    assert "timestamps/values" in result["warnings"][0]
    assert not (tmp_path / "vm" / "raw_index.json").exists()
    predict.assert_not_called()
