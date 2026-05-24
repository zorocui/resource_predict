from __future__ import annotations

from typing import Any, Callable, Dict, List

from flask import Flask, render_template

from resource_predict.pipeline.constants import SUMMARY_INDEX_FILENAME
from resource_predict.settings import settings


def register_page_routes(app: Flask, helpers: Dict[str, Callable[..., Any]]) -> None:
    get_summary = helpers["get_summary"]

    @app.get("/")
    def index():
        error = None
        resources: List[dict] = []
        try:
            summary = get_summary()
            resources = summary.get("resources", []) if isinstance(summary, dict) else []
        except Exception as e:
            error = str(e)
        if not resources and not error:
            error = f"尚未检测到预测结果，请先生成 {settings.app.out_dir}/{SUMMARY_INDEX_FILENAME}。"
        return render_template(
            "index.html",
            resources=[],
            top_n_default=settings.generation.top_n_default,
            api_page_size_default=settings.generation.api_page_size_default,
            error=error,
        )
