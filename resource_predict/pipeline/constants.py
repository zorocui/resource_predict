from resource_predict.data.raw_store import RAW_INDEX_FILENAME, RAW_RESOURCES_DIRNAME
from resource_predict.resource_types import METRIC_NAMES

MANIFEST_FILENAME = "manifest.json"
SUMMARY_INDEX_FILENAME = "summary_index.json"
DETAILS_DIRNAME = "details"
GENERATION_STATS_FILENAME = "generation_stats.json"
FORECAST_ERROR_REPORT_FILENAME = "forecast_error_report.json"

__all__ = [
    "METRIC_NAMES",
    "MANIFEST_FILENAME",
    "SUMMARY_INDEX_FILENAME",
    "DETAILS_DIRNAME",
    "RAW_INDEX_FILENAME",
    "RAW_RESOURCES_DIRNAME",
    "GENERATION_STATS_FILENAME",
    "FORECAST_ERROR_REPORT_FILENAME",
]
