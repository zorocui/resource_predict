from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from resource_predict.data.updater import run_upsert_with_data
from resource_predict.logging_setup import setup_application_logging
from resource_predict.pipeline.output_paths import scoped_out_dir
from resource_predict.providers.k8s_prometheus import (
    diagnose_k8s_prometheus,
    k8s_workload_prometheus_provider,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch K8S workload metrics from Prometheus and merge them into raw resource shards."
    )
    parser.add_argument(
        "--cluster",
        action="append",
        default=[],
        help="Only fetch the specified K8S cluster. Can be passed multiple times. Defaults to all configured clusters.",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Only diagnose whether Prometheus metrics can be aggregated into Workloads; do not write raw data or forecast.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Use with --diagnose to print machine-readable JSON.",
    )
    return parser.parse_args()


def _print_diagnosis(report: Dict[str, Any]) -> None:
    status = "passed" if report.get("ok") else "failed"
    print(f"K8S Prometheus diagnosis: {status}, clusters checked: {report.get('clusters_checked', 0)}")
    for cluster in report.get("clusters", []):
        ok = "OK" if cluster.get("ok") else "FAIL"
        print(f"\n[{ok}] {cluster.get('cluster')} - {cluster.get('prometheus_url')}")
        counts = cluster.get("counts", {})
        for key in (
            "cpu_usage_series",
            "memory_usage_series",
            "pod_owner_rows",
            "replicaset_owner_rows",
            "container_series",
            "workloads_resolved",
            "orphan_container_series",
            "cpu_request_series",
            "cpu_limit_series",
            "memory_request_series",
            "memory_limit_series",
        ):
            print(f"  {key}: {counts.get(key, 0)}")
        samples = cluster.get("sample_workloads") or []
        if samples:
            print("  sample_workloads:")
            for item in samples:
                print(
                    "   - "
                    f"{item.get('namespace')}/"
                    f"{item.get('workload_kind')}/"
                    f"{item.get('workload_name')}"
                )
        for warning in cluster.get("warnings") or []:
            print(f"  WARNING: {warning}")
        for error in cluster.get("errors") or []:
            print(f"  ERROR: {error}")


def _fetch_workload_items(clusters: List[str]) -> List[Dict[str, Any]]:
    items = k8s_workload_prometheus_provider(resources=0, n=0, freq="5min", clusters=clusters)
    if not isinstance(items, list) or not items:
        raise RuntimeError("Prometheus provider returned no K8S workload resources")
    return items


def main() -> int:
    args = _parse_args()
    setup_application_logging()

    out_dir = scoped_out_dir("k8s")
    if args.diagnose:
        report = diagnose_k8s_prometheus(clusters=args.cluster)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_diagnosis(report)
        return 0 if report.get("ok") else 2

    items = _fetch_workload_items(args.cluster)

    result = run_upsert_with_data(items, fail_if_busy=True, out_dir=out_dir)
    if not result.get("success"):
        print(f"K8S Workload upsert failed: {result.get('error')}", file=sys.stderr)
        return 1
    print(
        "K8S Workload upsert completed: "
        f"updated {result.get('resources_updated', 0)}, "
        f"created {result.get('resources_created', 0)}, "
        f"net new points {result.get('total_new_points', 0)}, "
        f"forecasted resources {result.get('predicted_resources', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
