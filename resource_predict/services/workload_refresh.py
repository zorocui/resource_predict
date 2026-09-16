"""单 Workload 定向重新采集与预测，复用更新锁和进度。"""
import logging
import json
import time

from resource_predict.data import updater
from resource_predict.pipeline.output_paths import scoped_out_dir
from resource_predict.providers.k8s_prometheus import fetch_single_k8s_workload
from resource_predict.services.update_tasks import start_update_task_async
from resource_predict.services.update_history import append_update_history
from resource_predict.settings import settings

logger = logging.getLogger(__name__)


def start_workload_refresh(resource):
    if not updater._update_exclusive.acquire(blocking=False):
        raise updater.UpdateBusyError("已有采集预测任务正在运行，请稍后重试")
    started = False
    try:
        if updater.get_update_status().get("running"):
            raise updater.UpdateBusyError("已有采集预测任务正在运行，请稍后重试")
        updater.mark_external_update_started("fetching_k8s_prometheus", f"正在重新拉取 {resource['resource_id']}",
            metadata={"task_source": "单 Workload 重新拉取预测", "fetch_window_label": "当前 Workload 完整配置历史窗口"})
        started = True
        start_update_task_async(_run, resource, busy_error_cls=updater.UpdateBusyError,
                                logger=logger, thread_name="workload-refresh")
    except Exception as exc:
        if started:
            updater.mark_external_update_failed(str(exc))
        updater._update_exclusive.release()
        raise


def _run(resource):
    started = updater.get_update_status().get("last_started_at") or time.time()
    result = {}
    error = None
    try:
        item = fetch_single_k8s_workload(resource)
        result = updater._do_update(new_data_list=[item], allow_create=True, force_predict=True,
            out_dir=scoped_out_dir("k8s", settings.app.out_dir), _exclusive_already_acquired=True,
            record_history=False, keep_running=True, freq_hint=f"{int(settings.k8s_prometheus.step_seconds)}s")
        if not result.get("success"):
            error = str(result.get("error") or "重新拉取预测失败")
        else:
            stats_path = scoped_out_dir("k8s", settings.app.out_dir) / "generation_stats.json"
            if stats_path.exists():
                stats = json.loads(stats_path.read_text(encoding="utf-8"))
                skips = [entry for entry in stats.get("prediction_skips", []) if entry.get("resource_id") == resource["resource_id"]]
                result["predicted_resources"] = int(stats.get("predicted_resources", 0))
                result["prediction_skips"] = skips
                if skips:
                    result["status"] = "partial_success"
                    result["message"] = "数据已更新；以下指标连续数据不足，沿用旧预测或显示暂无预测：" + ", ".join(entry.get("metric", "") for entry in skips)
        return result
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        finished = time.time()
        history_saved = append_update_history({
            **result, "started_at": started, "finished_at": finished,
            "status": result.get("status", "success") if result.get("success") and not error else "failed",
            "task_source": "单 Workload 重新拉取预测",
            "fetch_window_label": f"{resource['resource_id']} · 完整配置历史窗口",
            "message": f"{resource['resource_id']}：{error or result.get('message') or '重新拉取预测完成'}",
            "error": error, "elapsed_seconds": finished-started,
        }, out_dir=settings.app.out_dir)
        if not history_saved:
            error = f"{error + '；' if error else ''}更新历史保存失败，请检查输出目录和服务日志"
        if error:
            updater.mark_external_update_failed(error, record_history=False)
        else:
            updater.mark_external_update_finished(result, record_history=False)
        updater._update_exclusive.release()
