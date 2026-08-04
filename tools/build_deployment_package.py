"""构建仅包含运行文件的内网部署 ZIP。"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence
from zipfile import ZIP_DEFLATED, ZipFile


TOP_LEVEL = "resource_predict"
ROOT_FILES = (
    "app.py",
    "generate_forecasts.py",
    "ingest_k8s_workloads.py",
    "requirements.txt",
)
RUNTIME_DIRS = ("resource_predict", "templates", "static")
REQUIRED_PATHS = (
    "app.py",
    "requirements.txt",
    "resource_predict/__init__.py",
    "templates/index.html",
    "static/js/index.js",
    "static/css/index.css",
    "static/vendor/echarts/echarts.min.js",
    "deploy/runtime_config.json",
)
EXCLUDED_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
EXCLUDED_FILE_NAMES = {".DS_Store", "Thumbs.db"}
EXCLUDED_ENDINGS = (".pyc", ".pyo", ".log", ".tmp", ".temp", ".bak", "~")
ALLOWED_ARCHIVE_ROOTS = set(ROOT_FILES) | set(RUNTIME_DIRS) | {"deploy"}


class PackageBuildError(ValueError):
    """部署包内容不完整或不安全。"""


@dataclass(frozen=True)
class PackageResult:
    path: Path
    file_count: int
    size_bytes: int


def _is_excluded(relative_path: Path | PurePosixPath) -> bool:
    parts = relative_path.parts
    if any(part in EXCLUDED_DIR_NAMES for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    return name in EXCLUDED_FILE_NAMES or name.lower().endswith(EXCLUDED_ENDINGS)


def _resolved_inside(path: Path, project_root: Path) -> Path:
    resolved = path.resolve()
    if resolved != project_root and project_root not in resolved.parents:
        raise PackageBuildError(f"运行文件指向项目目录外部: {path}")
    return resolved


def collect_runtime_files(project_root: Path) -> list[Path]:
    """按运行白名单收集文件，并返回稳定排序的绝对路径。"""
    root = project_root.resolve()
    for required in REQUIRED_PATHS:
        if not (root / required).is_file():
            raise PackageBuildError(f"缺少运行必需文件: {required}")

    candidates: list[Path] = []
    for relative in ROOT_FILES:
        path = root / relative
        if path.is_file():
            candidates.append(path)

    for relative in RUNTIME_DIRS:
        directory = root / relative
        if not directory.is_dir():
            continue
        candidates.extend(path for path in directory.rglob("*") if path.is_file())

    candidates.append(root / "deploy" / "runtime_config.json")
    deploy_dir = root / "deploy"
    if deploy_dir.is_dir():
        candidates.extend(path for path in deploy_dir.glob("*.example.json") if path.is_file())

    collected: dict[str, Path] = {}
    for source in candidates:
        relative = source.relative_to(root)
        if _is_excluded(relative):
            continue
        _resolved_inside(source, root)
        collected[relative.as_posix()] = source
    return [collected[key] for key in sorted(collected)]


def validate_archive_names(
    names: Iterable[str], top_level: str = TOP_LEVEL
) -> None:
    """验证 ZIP 清单只包含允许的运行路径。"""
    normalized_names: set[str] = set()
    for name in names:
        if "\\" in name:
            raise PackageBuildError(f"压缩包路径不是 POSIX 相对路径: {name}")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise PackageBuildError(f"压缩包包含不安全路径: {name}")
        if len(path.parts) < 2 or path.parts[0] != top_level:
            raise PackageBuildError(f"压缩包顶层目录不正确: {name}")
        relative = PurePosixPath(*path.parts[1:])
        if relative.parts[0] not in ALLOWED_ARCHIVE_ROOTS or _is_excluded(relative):
            raise PackageBuildError(f"压缩包包含禁止文件: {name}")
        if relative.parts[0] == "deploy":
            allowed_deploy = (
                relative.as_posix() == "deploy/runtime_config.json"
                or relative.name.endswith(".example.json")
            )
            if len(relative.parts) != 2 or not allowed_deploy:
                raise PackageBuildError(f"压缩包包含禁止配置: {name}")
        normalized_names.add(path.as_posix())

    if not normalized_names:
        raise PackageBuildError("部署包不能为空")
    for required in REQUIRED_PATHS:
        archive_path = f"{top_level}/{PurePosixPath(required).as_posix()}"
        if archive_path not in normalized_names:
            raise PackageBuildError(f"压缩包缺少运行必需文件: {required}")


def build_deployment_package(
    project_root: Path,
    output_dir: Path | None = None,
    now: datetime | None = None,
) -> PackageResult:
    """创建经过清单校验且不会覆盖已有产物的部署 ZIP。"""
    root = project_root.resolve()
    files = collect_runtime_files(root)
    destination = (output_dir or root / "dist").resolve()
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    final_path = destination / f"resource_predict_{timestamp}.zip"
    if final_path.exists():
        raise PackageBuildError(f"部署包已存在，不会覆盖: {final_path}")

    destination.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=".resource_predict_",
        suffix=".zip.tmp",
        dir=destination,
        delete=False,
    )
    temporary_path = Path(handle.name)
    handle.close()
    final_created = False
    completed = False
    try:
        with ZipFile(temporary_path, "w", compression=ZIP_DEFLATED) as archive:
            for source in files:
                relative = source.relative_to(root).as_posix()
                archive.write(source, f"{TOP_LEVEL}/{relative}")
        with ZipFile(temporary_path) as archive:
            validate_archive_names(archive.namelist())

        os.link(temporary_path, final_path)
        final_created = True
        temporary_path.unlink()
        result = PackageResult(
            path=final_path,
            file_count=len(files),
            size_bytes=final_path.stat().st_size,
        )
        completed = True
        return result
    except FileExistsError as exc:
        raise PackageBuildError(f"部署包已存在，不会覆盖: {final_path}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
        if final_created and not completed:
            final_path.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成精简的内网部署 ZIP")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="项目根目录，默认根据脚本位置自动识别",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="输出目录，默认使用项目根目录下的 dist",
    )
    args = parser.parse_args(argv)
    try:
        result = build_deployment_package(
            args.project_root,
            output_dir=args.output_dir,
        )
    except (PackageBuildError, OSError) as exc:
        print(f"打包失败：{exc}", file=sys.stderr)
        return 1

    size_mib = result.size_bytes / (1024 * 1024)
    print("打包成功")
    print(f"文件位置：{result.path}")
    print(f"文件数量：{result.file_count}")
    print(f"压缩包大小：{size_mib:.2f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
