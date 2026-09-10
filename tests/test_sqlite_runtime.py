import json
import sqlite3 as native

import pytest

from resource_predict.sqlite_runtime import register_functions, sqlite3


def test_native_library_and_database_are_unchanged(tmp_path):
    path = tmp_path / "existing.sqlite3"
    with native.connect(path) as db:
        db.execute("CREATE TABLE evidence(value INTEGER)")
        db.execute("INSERT INTO evidence VALUES (7)")
    db = sqlite3.connect(path)
    try:
        assert isinstance(db, native.Connection)
        assert sqlite3.sqlite_version == native.sqlite_version
        assert sqlite3.Error is native.Error
        assert db.execute("SELECT value FROM evidence").fetchone() == (7,)
    finally:
        db.close()


@pytest.mark.parametrize("document,field,expected", [
    ('{"role":"independent_test"}', "role", "independent_test"),
    ('{"flag":true,"count":0}', "count", 0),
    ('{"flag":true}', "flag", 1),
    ('{"value":null}', "value", None),
    ('{}', "missing", None),
    ('not-json', "role", None),
    ('[]', "role", None),
    (None, "role", None),
    ('{"value":{"x":1}}', "value", json.dumps({"x": 1}, separators=(",", ":"))),
])
def test_json_field_without_json1(document, field, expected):
    db = native.connect(":memory:")
    try:
        register_functions(db)
        assert db.execute("SELECT rp_json_field(?,?)", (document, field)).fetchone()[0] == expected
    finally:
        db.close()


def test_helpers_do_not_request_new_sqlite_function_flags():
    class OldConnection:
        def create_function(self, name, arity, function):
            assert name == "rp_json_field" and arity == 2
            assert function('{"x":1}', "x") == 1
    register_functions(OldConnection())


def test_production_consumers_use_native_connection_helper():
    from resource_predict.pipeline import calibration, controlled_activation, realized_error
    from resource_predict.services.scaling import effects, effect_reports
    from resource_predict.services import forecast_accuracy
    for module in (calibration, controlled_activation, realized_error, effects, effect_reports, forecast_accuracy):
        assert module.sqlite3 is sqlite3
