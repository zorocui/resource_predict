from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from tools.build_deployment_package import (
    PackageBuildError,
    build_deployment_package,
    collect_runtime_files,
    validate_archive_names,
)


def _write(root: Path, relative: str, content: bytes = b"content") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def project_tree(tmp_path: Path) -> tuple[Path, bytes]:
    runtime_bytes = b'{"collection":{"history_days":7}}\n'
    included = {
        "app.py": b"print('app')\n",
        "generate_forecasts.py": b"print('forecast')\n",
        "ingest_k8s_workloads.py": b"print('ingest')\n",
        "requirements.txt": b"Flask\n",
        "resource_predict/__init__.py": b"",
        "resource_predict/core.py": b"VALUE = 1\n",
        "templates/index.html": b"<html></html>\n",
        "static/js/index.js": b"console.log('app');\n",
        "static/css/index.css": b"body {}\n",
        "static/vendor/echarts/echarts.min.js": b"/* echarts */\n",
        "deploy/runtime_config.json": runtime_bytes,
        "deploy/clusters.example.json": b"{}\n",
    }
    excluded = {
        "resource_predict/__pycache__/core.cpython-311.pyc": b"bytecode",
        "resource_predict/app.log": b"log",
        "resource_predict/editor.tmp": b"tmp",
        "tests/test_app.py": b"def test_app(): pass\n",
        "outputs/resources.json": b"{}\n",
        "docs/readme.md": b"docs\n",
        ".venv/pyvenv.cfg": b"home=x\n",
        "deploy/clusters.json": b'{"secret":"value"}\n',
        "deploy/k8s_prometheus_clusters.json": b'[{"bearer_token":"secret"}]\n',
        "deploy/forecast_config.json": b"{}\n",
    }
    for relative, content in {**included, **excluded}.items():
        _write(tmp_path, relative, content)
    return tmp_path, runtime_bytes


def test_collect_runtime_files_uses_explicit_allowlist(project_tree):
    root, _runtime_bytes = project_tree

    relative_paths = {
        path.relative_to(root).as_posix()
        for path in collect_runtime_files(root)
    }

    assert relative_paths == {
        "app.py",
        "deploy/clusters.example.json",
        "deploy/runtime_config.json",
        "generate_forecasts.py",
        "ingest_k8s_workloads.py",
        "requirements.txt",
        "resource_predict/__init__.py",
        "resource_predict/core.py",
        "static/css/index.css",
        "static/js/index.js",
        "static/vendor/echarts/echarts.min.js",
        "templates/index.html",
    }


def test_build_package_preserves_structure_and_runtime_config(project_tree, tmp_path):
    root, runtime_bytes = project_tree
    output_dir = tmp_path / "packages"

    result = build_deployment_package(
        root,
        output_dir=output_dir,
        now=datetime(2026, 8, 4, 12, 34, 56),
    )

    assert result.path.name == "resource_predict_20260804_123456.zip"
    assert result.file_count == 12
    assert result.size_bytes == result.path.stat().st_size
    with ZipFile(result.path) as archive:
        names = set(archive.namelist())
        assert "resource_predict/app.py" in names
        assert "resource_predict/deploy/runtime_config.json" in names
        assert "resource_predict/static/vendor/echarts/echarts.min.js" in names
        assert "resource_predict/deploy/clusters.json" not in names
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
        assert archive.read("resource_predict/deploy/runtime_config.json") == runtime_bytes
        validate_archive_names(names)


def test_missing_required_file_fails_without_archive(project_tree, tmp_path):
    root, _runtime_bytes = project_tree
    (root / "templates" / "index.html").unlink()
    output_dir = tmp_path / "packages"

    with pytest.raises(PackageBuildError, match="缺少运行必需文件: templates/index.html"):
        build_deployment_package(root, output_dir=output_dir)

    assert not output_dir.exists()


def test_existing_archive_is_not_overwritten(project_tree, tmp_path):
    root, _runtime_bytes = project_tree
    output_dir = tmp_path / "packages"
    fixed_time = datetime(2026, 8, 4, 12, 34, 56)
    first = build_deployment_package(root, output_dir=output_dir, now=fixed_time)
    original = first.path.read_bytes()

    with pytest.raises(PackageBuildError, match="部署包已存在"):
        build_deployment_package(root, output_dir=output_dir, now=fixed_time)

    assert first.path.read_bytes() == original


@pytest.mark.parametrize(
    "name",
    [
        "resource_predict/../secret.txt",
        "/resource_predict/app.py",
        "other/app.py",
        "resource_predict/resource_predict/__pycache__/module.pyc",
        "resource_predict/deploy/clusters.json",
    ],
)
def test_validate_archive_names_rejects_forbidden_paths(name):
    with pytest.raises(PackageBuildError):
        validate_archive_names([name])


def test_validation_failure_removes_temporary_and_final_files(project_tree, tmp_path):
    root, _runtime_bytes = project_tree
    output_dir = tmp_path / "packages"

    with patch(
        "tools.build_deployment_package.validate_archive_names",
        side_effect=PackageBuildError("invalid archive"),
    ):
        with pytest.raises(PackageBuildError, match="invalid archive"):
            build_deployment_package(root, output_dir=output_dir)

    assert list(output_dir.iterdir()) == []
