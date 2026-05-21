"""
预测批处理 CLI 入口（根目录兼容层）。

实现位于 resource_predict.pipeline；本文件保留原有命令：
  python generate_images.py
  python generate_images.py predict
"""

from __future__ import annotations

from resource_predict.pipeline import (
    ExternalProvider,
    generate_all_images,
    generate_predictions_only,
    simulate_curve,
)
from resource_predict.providers.mock import mock_provider

__all__ = [
    "ExternalProvider",
    "generate_all_images",
    "generate_predictions_only",
    "simulate_curve",
    "mock_provider",
]


def provider(resources: int, n: int, freq: str):
    return mock_provider(resources=resources, n=n, freq=freq)


if __name__ == "__main__":
    import sys

    from resource_predict.logging_setup import setup_application_logging
    from resource_predict.settings import settings

    setup_application_logging()

    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in {
        "predict",
        "predict-only",
        "predict_only",
    }:
        out = generate_predictions_only()
        print(f"已仅重算预测 {len(out)} 个资源，目录: {settings.generation.out_dir}")
    else:
        out = generate_all_images(data_provider=provider)
        print(f"已生成 {len(out)} 个云资源的预测结果，目录: {settings.generation.out_dir}")
