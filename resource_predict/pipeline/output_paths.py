from __future__ import annotations

from pathlib import Path
from typing import Iterable, Literal

from resource_predict.resource_types import resource_type_of
from resource_predict.settings import settings

OutputScope = Literal["vm", "k8s"]

VM_SCOPE: OutputScope = "vm"
K8S_SCOPE: OutputScope = "k8s"
SCOPES: tuple[OutputScope, OutputScope] = (VM_SCOPE, K8S_SCOPE)


def scoped_out_dir(scope: OutputScope, root: str | Path | None = None) -> Path:
    """Return the isolated output directory for one resource family."""
    base = Path(root or settings.app.out_dir)
    return base / scope


def all_scoped_out_dirs(root: str | Path | None = None) -> list[tuple[OutputScope, Path]]:
    return [(scope, scoped_out_dir(scope, root)) for scope in SCOPES]


def scope_for_resource(item: dict) -> OutputScope:
    resource_id = str(item.get("resource_id") or "").strip().lower()
    if resource_id.startswith("k8s:"):
        return K8S_SCOPE
    return K8S_SCOPE if resource_type_of(item).startswith("k8s_") else VM_SCOPE


def split_items_by_scope(items: Iterable[dict]) -> dict[OutputScope, list[dict]]:
    split: dict[OutputScope, list[dict]] = {VM_SCOPE: [], K8S_SCOPE: []}
    for item in items:
        split[scope_for_resource(item)].append(item)
    return split
