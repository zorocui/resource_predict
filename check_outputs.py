from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from resource_predict.services.output_health import check_outputs, format_health_report
from resource_predict.settings import settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate generated VM and K8S Workload prediction artifacts."
    )
    parser.add_argument(
        "--out-dir",
        default=settings.app.out_dir,
        help="Prediction output directory. Defaults to settings.app.out_dir.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON.")
    parser.add_argument(
        "--allow-missing-type",
        action="store_true",
        help="Do not fail when only VM or only K8S Workload artifacts are present.",
    )
    args = parser.parse_args()

    report = check_outputs(
        Path(args.out_dir),
        require_both_types=not bool(args.allow_missing_type),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_health_report(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
