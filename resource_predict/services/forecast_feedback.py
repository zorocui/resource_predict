"""Read-only, mtime-cached feedback report projection for resource details."""
from __future__ import annotations

import json
import threading
import time

from resource_predict.pipeline.output_paths import scoped_out_dir, scope_for_resource
from resource_predict.settings import settings

_LOCK = threading.Lock()
_CACHE: dict = {}


def resource_feedback(resource: dict) -> dict:
    path = scoped_out_dir(scope_for_resource(resource)) / "forecast_realized_report.json"
    now = int(time.time()*1000)
    allowed = settings.decision.calibrated_advice_resource_ids
    result = {"resource_id":resource["resource_id"],"server_time_ms":now,"report_status":"missing",
              "assessment":None,"report_generated_at_ms":None,"rules":{},
              "policy_enabled":settings.decision.calibrated_advice_enabled is True,
              "resource_allowlisted":isinstance(allowed,(tuple,list)) and resource["resource_id"] in allowed}
    try:
        stat=path.stat()
        key=str(path.resolve())
        stamp=(stat.st_mtime_ns,stat.st_size)
        with _LOCK:
            cached=_CACHE.get(key)
            if cached is None or cached[0]!=stamp:
                report=json.loads(path.read_text(encoding="utf-8"))
                assessment=report.get("activation_assessment",{})
                by_id={str(row["resource_id"]):row for row in assessment.get("resources",[]) if isinstance(row,dict) and row.get("resource_id")}
                cached=(stamp,report.get("generated_at_epoch_ms"),assessment.get("rules",{}),by_id)
                if len(_CACHE)>=2 and key not in _CACHE:
                    _CACHE.clear()
                _CACHE[key]=cached
            _,generated,rules,by_id=cached
            entry=by_id.get(str(resource["resource_id"]))
        result.update(report_status="available",assessment=entry,report_generated_at_ms=generated,rules=rules)
        if not isinstance(generated,int) or generated>now or now-generated>=86400000:
            result["report_status"]="stale"
    except FileNotFoundError:
        pass
    except (OSError,ValueError,TypeError,AttributeError):
        result["report_status"]="error"
    return result
