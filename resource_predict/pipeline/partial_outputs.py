"""发布局部预测：复用未变化详情引用，不读写全量 manifest。"""
import json
import hashlib
import logging
import tempfile
import time
from pathlib import Path

from resource_predict.data.io import atomic_write_json
from resource_predict.pipeline.write_outputs import write_prediction_outputs

logger = logging.getLogger(__name__)


def write_partial_prediction_outputs(*, out_base, resources_items, **kwargs):
    base = Path(out_base)
    ids = {str(item["resource_id"]) for item in resources_items}
    started = time.perf_counter()
    def read(name, default):
        path = base / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    summary = read("summary_index.json", {"meta": {}, "resources": []})
    with tempfile.TemporaryDirectory(prefix=".partial-", dir=base) as temporary:
        stage = Path(temporary)
        write_prediction_outputs(out_base=stage, resources_items=resources_items, write_manifest=False,
                                 **{**kwargs, "detail_chunk_size": 1})
        generated = json.loads((stage / "summary_index.json").read_text(encoding="utf-8"))
        replacements = {}
        files = {}
        for row in generated["resources"]:
            ref = row["detail_ref"]
            source = ref["file"]
            if source not in files:
                name = "partial-" + hashlib.sha256(str(row["resource_id"]).encode()).hexdigest() + ".json"
                target = base / "details" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                (stage / "details" / source).replace(target)
                files[source] = name
            ref["file"] = files[source]
            replacements[str(row["resource_id"])] = row
        kept = [row for row in summary["resources"] if str(row.get("resource_id")) not in ids]
        summary["resources"] = kept + list(replacements.values())
        meta = summary.setdefault("meta", {})
        if not meta:
            meta.update(generated["meta"])
        meta.update(resources=len(summary["resources"]), last_partial_generated_at_epoch_ms=int(time.time()*1000),
                    details_files=sorted({row["detail_ref"]["file"] for row in summary["resources"] if row.get("detail_ref", {}).get("file")}))
        old_skips = [row for row in meta.get("prediction_skips", []) if row.get("resource_id") not in ids]
        meta["prediction_skips"] = old_skips + list(kwargs.get("prediction_skips") or [])

        # 误差汇总保留其他资源；不解析包含完整曲线的其他详情文件。
        errors = read("forecast_error_report.json", {"meta": {}, "resources": [], "rows": []})
        new_errors = json.loads((stage / "forecast_error_report.json").read_text(encoding="utf-8"))
        for field in ("resources", "rows"):
            errors[field] = [row for row in errors[field] if row.get("resource_id") not in ids] + new_errors[field]
        errors.setdefault("meta", {}).update(resources=len(errors["resources"]), rows=len(errors["rows"]),
            last_partial_generated_at_epoch_ms=int(time.time()*1000), prediction_skips=meta["prediction_skips"])
        atomic_write_json(base / "forecast_error_report.json", errors, separators=(",", ":"))
        stats = json.loads((stage / "generation_stats.json").read_text(encoding="utf-8"))
        stats["output_mode"] = "partial"
        stats["output_seconds"] = round(time.perf_counter() - started, 3)
        atomic_write_json(base / "generation_stats.json", stats, separators=(",", ":"))
        # 最后切换索引；读取者始终能找到被索引引用的详情文件。
        atomic_write_json(base / "summary_index.json", summary, separators=(",", ":"))
    logger.info("[partial_output] 局部预测发布完成: updated=%d retained=%d detail_files=%d elapsed_seconds=%.3f",
                len(ids), len(kept), len(files), time.perf_counter()-started)
    return resources_items
