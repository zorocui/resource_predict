"""Keep the existing stdlib SQLite and supply the small scalar helpers we need."""
from __future__ import annotations

import json
import sqlite3 as _stdlib


def _json_field(document, field):
    try:
        value = json.loads(document)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    result = value.get(field)
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return result


def register_functions(db):
    # No deterministic keyword: Python 3.10 can be linked to SQLite 3.7.17.
    db.create_function("rp_json_field", 2, _json_field)


class _NativeSQLite:
    def __getattr__(self, name):
        return getattr(_stdlib, name)

    def connect(self, *args, **kwargs):
        db = _stdlib.connect(*args, **kwargs)
        try:
            register_functions(db)
        except Exception:
            db.close()
            raise
        return db


sqlite3 = _NativeSQLite()
