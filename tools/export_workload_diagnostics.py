"""只读导出一个 Workload 的预测诊断数据；仅依赖 Python 标准库。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def export_workload(name: str, out_dir: Path, destination: Path) -> Path:
    base = out_dir.resolve()
    sources = {}
    warnings = []

    def read(relative):
        path = (base / relative).resolve()
        if base not in path.parents:
            raise ValueError(f"产物引用超出输出目录：{relative}")
        content = path.read_bytes()
        sources[str(relative)] = {"path": path, "sha256": hashlib.sha256(content).hexdigest(),
                                  "mtime_ns": path.stat().st_mtime_ns, "bytes": len(content)}
        return json.loads(content.decode("utf-8"))

    summary = read("summary_index.json")
    rows = [row for row in summary.get("resources", []) if isinstance(row, dict)]
    name = name.strip()
    matches = [row for row in rows if row.get("resource_id") == name]
    if not matches:
        matches = [row for row in rows if name in {
            str(row.get("spec", {}).get("workload_name") or ""),
            str(row.get("resource_id") or "").rsplit(":", 1)[-1],
        }]
    if not name or not matches:
        raise ValueError(f"未找到 Workload：{name!r}。请检查名称和 --out-dir。")
    if len(matches) != 1:
        raise ValueError("存在同名 Workload，请使用以下完整资源 ID 重试：\n" +
                         "\n".join(str(row["resource_id"]) for row in matches))
    row = matches[0]
    rid = str(row["resource_id"])
    if not rid.startswith("k8s:") and row.get("resource_type") != "k8s_workload":
        raise ValueError("该资源不是 Workload")
    ref = row.get("detail_ref") or {}
    if not ref.get("file"):
        raise ValueError("资源缺少 detail_ref，无法定位预测详情")
    detail_chunk = read(str(Path("details") / ref["file"]))
    offset = int(ref["offset"])
    details = detail_chunk.get("resources", [])
    if not 0 <= offset < len(details) or details[offset].get("resource_id") != rid:
        raise ValueError("预测详情与摘要不一致，可能正在生成产物，请稍后重试")
    detail = details[offset]
    raw_index = read("raw_index.json")
    raw_ref = raw_index.get("resources", {}).get(rid)
    if not raw_ref or not raw_ref.get("file"):
        raise ValueError("raw 索引缺少该 Workload，无法导出真实历史数据")
    raw = read(raw_ref["file"])
    if raw.get("resource_id") != rid:
        raise ValueError("raw 分片资源 ID 与索引不一致")
    payloads = {
        "summary.json": {"meta": summary.get("meta", {}), "resources": [row]},
        "detail.json": detail,
        "raw.json": raw,
        "raw_index.json": {**raw_index, "resources": {rid: raw_ref}},
    }
    accuracy_path = base / "forecast_accuracy_summary.json"
    if accuracy_path.exists():
        accuracy = read(accuracy_path.name)
        selected = [entry for entry in accuracy.get("rows", []) if entry.get("resource_id") == rid]
        payloads[accuracy_path.name] = {**accuracy, "rows": selected}
        if not selected:
            warnings.append("最近一次准确率汇总中没有该 Workload，可能未参与本次增量预测。")
    else:
        warnings.append("缺少 forecast_accuracy_summary.json，无法核对页面准确率汇总。")
    # 避免导出期间索引或分片变化，误把不同批次文件拼成一次快照。
    for relative, source in sources.items():
        if hashlib.sha256(source["path"].read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"导出期间文件发生变化：{relative}。请在采集/预测完成后重试。")
    info = {
        "resource_id": rid, "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "warnings": warnings,
        "sources": {key: {field: value for field, value in source.items() if field != "path"}
                    for key, source in sources.items()},
        "notes": ["只读取现有文件，没有重新预测；未读取 manifest 或集群连接配置。",
                  "各文件可能来自不同次运行，不能只凭导出时间认定它们属于同次预测。",
                  "raw 为当前留存观测；可能已发生增量更新或规格变更。",
                  "准确率按预处理后的独立测试实际值统计；产物未必保留该测试实际值，不能保证逐点精确复算。",
                  "包含资源身份、规格和监控数据，分享前可按需一致脱敏。"],
    }
    payloads["export_info.json"] = info
    destination.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", rid.rsplit(":", 1)[-1])[:80] or "workload"
    output = destination / f"workload_{slug}_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.zip"
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for filename, value in payloads.items():
            archive.writestr(filename, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        archive.writestr("说明.txt", "\n".join(info["notes"] + warnings))
    with ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("压缩包完整性校验失败")
    for warning in warnings:
        print(f"提示：{warning}")
    return output.resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", nargs="?", help="Workload 名称或完整资源 ID；省略则交互输入")
    parser.add_argument("--out-dir", type=Path, default=Path("outputs/k8s"), help="K8S 产物目录")
    parser.add_argument("--dest", type=Path, default=Path("diagnostics"), help="诊断 ZIP 输出目录")
    args = parser.parse_args()
    try:
        name = args.name or input("请输入 Workload 名称或完整资源 ID：").strip()
        output = export_workload(name, args.out_dir, args.dest)
    except (OSError, ValueError, KeyError, TypeError, EOFError) as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1
    print(f"导出完成：{output}\n请将该 ZIP 上传到对话中进行分析。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
