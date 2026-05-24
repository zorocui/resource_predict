from resource_predict.resource_types import METRIC_NAMES, POD_METRIC_NAMES, metric_names_for_resource, resource_type_of

MANIFEST_FILENAME = "manifest.json"
SUMMARY_INDEX_FILENAME = "summary_index.json"
DETAILS_DIRNAME = "details"
RAW_DATA_FILENAME = "raw_data.json"
GENERATION_STATS_FILENAME = "generation_stats.json"

__all__ = [
    "METRIC_NAMES",
    "POD_METRIC_NAMES",
    "metric_names_for_resource",
    "resource_type_of",
    "MANIFEST_FILENAME",
    "SUMMARY_INDEX_FILENAME",
    "DETAILS_DIRNAME",
    "RAW_DATA_FILENAME",
    "GENERATION_STATS_FILENAME",
]
