from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

from resource_predict.data.io import read_raw_dataset, write_raw_dataset
from resource_predict.data.updater import backup_raw_dataset, run_upsert_with_data
from resource_predict.logging_setup import setup_application_logging
from resource_predict.pipeline import generate_all_images
from resource_predict.providers.k8s_prometheus import k8s_pod_prometheus_provider
from resource_predict.resource_types import resource_type_of
from resource_predict.settings import settings


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch K8S Pod metrics from Prometheus and merge them into raw_data.json."
    )
    parser.add_argument(
        "--mode",
        choices=("upsert", "replace-k8s", "init"),
        default="upsert",
        help=(
            "upsert: merge Pod data into existing raw_data.json; "
            "replace-k8s: remove existing k8s_pod rows first, then merge; "
            "init: initialize raw_data.json when it does not exist."
        ),
    )
    return parser.parse_args()


def _fetch_pod_items() -> List[Dict[str, Any]]:
    items = k8s_pod_prometheus_provider(resources=0, n=0, freq="5min")
    if not isinstance(items, list) or not items:
        raise RuntimeError("Prometheus provider returned no K8S Pod resources")
    return items


def _initialize_raw(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return generate_all_images(
        data_provider=lambda resources, n, freq: items,
        resources=len(items),
        freq="5min",
    )


def _remove_existing_k8s(raw_path: Path) -> tuple[int, int]:
    prepared, meta = read_raw_dataset(raw_path)
    kept = [item for item in prepared if resource_type_of(item) != "k8s_pod"]
    removed = len(prepared) - len(kept)
    if removed <= 0:
        return 0, len(kept)
    backup_path = backup_raw_dataset(raw_path)
    if backup_path is not None:
        print(f"已备份 raw_data.json: {backup_path}")
    if kept:
        write_raw_dataset(raw_path, kept, freq=str(meta.get("freq", settings.generation.freq)))
    return removed, len(kept)


def main() -> int:
    args = _parse_args()
    setup_application_logging()

    out_dir = Path(settings.app.out_dir)
    raw_path = out_dir / settings.app.raw_data_filename
    items = _fetch_pod_items()

    if args.mode == "init" or not raw_path.exists():
        if raw_path.exists() and args.mode == "init":
            backup_path = backup_raw_dataset(raw_path)
            if backup_path is not None:
                print(f"已备份 raw_data.json: {backup_path}")
        out = _initialize_raw(items)
        print(f"已初始化 K8S Pod 预测 {len(out)} 个资源，目录: {out_dir}")
        return 0

    if args.mode == "replace-k8s":
        removed, kept = _remove_existing_k8s(raw_path)
        print(f"已移除现有 K8S Pod 资源: {removed} 个，VM 数据保留")
        if kept == 0:
            out = _initialize_raw(items)
            print(f"raw 中没有可保留的 VM 资源，已重新初始化 K8S Pod 预测 {len(out)} 个资源")
            return 0

    result = run_upsert_with_data(items, fail_if_busy=True)
    if not result.get("success"):
        print(f"K8S Pod upsert 失败: {result.get('error')}", file=sys.stderr)
        return 1
    print(
        "K8S Pod upsert 完成: "
        f"更新 {result.get('resources_updated', 0)} 个，"
        f"新增 {result.get('resources_created', 0)} 个，"
        f"数据点净增 {result.get('total_new_points', 0)}，"
        f"预测资源 {result.get('predicted_resources', 0)} 个"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
