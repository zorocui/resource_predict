"""Build an experiment-only source ZIP; excludes data, credentials and wheels."""
import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED


def build(root, output):
    root = root.resolve()
    files = [root / p for p in (
        "benchmarks/__init__.py", "benchmarks/routing_pilot.py", "benchmarks/routing_share.py",
        "benchmarks/routing_validation.py",
        "benchmarks/routing_budget.py",
        "benchmarks/routing_calibration.py",
        "benchmarks/routing_batch.py", "benchmarks/routing_timing.py",
        "benchmarks/routing_stress.py",
        "benchmarks/routing_pause.py",
        "benchmarks/routing_gate_replay.py",
        "benchmarks/routing_feedback.py",
        "benchmarks/routing_policy_suite.py",
        "benchmarks/routing_effects.py", "benchmarks/routing_results.py",
        "requirements.txt", "docs/routing-offline.md", "docs/routing-research.md")]
    files += sorted((root / "resource_predict").rglob("*.py"))
    for path in files:
        if not path.is_file() or root not in path.resolve().parents or path.is_symlink():
            raise ValueError("Missing or unsafe package source")
    manifest = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "routing-experiment/" + path.relative_to(root).as_posix())
        archive.writestr("routing-experiment/MANIFEST.json", json.dumps(manifest, indent=2))
    with ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print("Packaged source files:", build(Path(__file__).resolve().parents[1], args.output))
