"""
数据层：原始序列读写与增量更新。

请从子模块显式导入，避免 `import resource_predict.data` 时拉起 updater 等重依赖：

    from resource_predict.data.io import read_raw_dataset, write_raw_dataset
    from resource_predict.data.updater import start_background_updater, run_update_with_data
"""

__all__ = ("io", "updater")
