"""CLI entrypoint for generating resource forecasts.

Common commands:
  python generate_forecasts.py
  python generate_forecasts.py predict
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import List, Optional

from resource_predict.pipeline import (
    generate_forecasts,
    generate_predictions_only,
)
from resource_predict.pipeline.constants import RAW_INDEX_FILENAME
from resource_predict.pipeline.output_paths import scoped_out_dir, split_items_by_scope
from resource_predict.providers.mock import mock_provider


def provider(resources: int, n: int, freq: str):
    return mock_provider(resources=resources, n=n, freq=freq)


def _file_sha256(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate resource prediction outputs.")
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "predict", "predict-only", "predict_only"],
        help="'predict' reuses raw_index.json + raw/ and does not collect or overwrite raw data.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="VM output directory. Defaults to outputs/vm.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    from resource_predict.logging_setup import setup_application_logging
    setup_application_logging()

    command = args.command.strip().lower()
    vm_out_dir = Path(args.out_dir) if args.out_dir else scoped_out_dir("vm")
    k8s_out_dir = scoped_out_dir("k8s")
    scope_dirs = {"vm": vm_out_dir, "k8s": k8s_out_dir}

    if command in {"predict", "predict-only", "predict_only"}:
        for scope in ("vm", "k8s"):
            out_dir = scope_dirs[scope]
            raw_index_path = out_dir / RAW_INDEX_FILENAME
            if not raw_index_path.exists():
                print(f"Skipping {scope} predict: {raw_index_path} not found")
                continue
            raw_before = _file_sha256(raw_index_path)
            out = generate_predictions_only(out_dir=str(out_dir))
            raw_after = _file_sha256(raw_index_path)
            if raw_before != raw_after:
                raise RuntimeError(f"{raw_index_path} changed during predict-only generation")
            print(f"Recalculated {scope} predictions: {len(out)} output resources, dir: {out_dir}")
    else:
        from resource_predict.settings import settings as _settings
        cfg = _settings.generation
        all_items = provider(resources=cfg.resources, n=cfg.n, freq=cfg.freq)
        split = split_items_by_scope(all_items)
        for scope in ("vm", "k8s"):
            scoped = split[scope]
            if not scoped:
                print(f"No {scope} resources from mock provider, skipping.")
                continue
            out_dir = scope_dirs[scope]
            out = generate_forecasts(
                out_dir=str(out_dir),
                data_provider=lambda resources, n, freq, _items=scoped: _items,
            )
            print(f"Generated {scope} predictions for {len(out)} resources, dir: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
