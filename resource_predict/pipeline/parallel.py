"""Bounded metric scheduling shared by resource and container forecasts."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
import multiprocessing
import importlib
import os
import time

from resource_predict.pipeline.fit import fit_one_metric
from resource_predict.settings import settings

_process_context = None
_process_settings = None


def _initialize_process(ctx, snapshot):
    global _process_context, _process_settings
    _process_context, _process_settings = ctx, snapshot
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    # Load lazy native libraries before threadpoolctl inspects their pools.
    if set(ctx.active_methods) & {"arima", "sarima"}:
        try:
            importlib.import_module("statsmodels.api")
        except ImportError:
            # The normal model-failure diagnostics/fallback handle missing models.
            pass


def _run_metric(job, ctx, snapshot):
    resource_index, container, metric, series = job
    started = time.perf_counter()
    with settings.use(snapshot):
        result = fit_one_metric(series.iloc[:-ctx.test_size], series.iloc[-ctx.test_size:], series, ctx=ctx)
    result[5]["execution"] = {"pid": os.getpid(), "wall_seconds": time.perf_counter() - started}
    return resource_index, container, metric, result


def _run_process_metric(job):
    from threadpoolctl import threadpool_limits
    with threadpool_limits(limits=1):
        return _run_metric(job, _process_context, _process_settings)


def execute_metric_jobs(jobs, ctx, settings_snapshot, plan, stats: dict):
    """Yield completed metrics with at most 2×workers jobs queued or running.

    Errors propagate to the caller. Pending jobs are cancelled; running fits are
    allowed to finish without silently retrying under a different backend.
    """
    stats.update(plan)
    stats.update(submitted=0, completed=0, max_in_flight=0, pids=[], model_fit_seconds_sum=0.0)

    def record(result):
        stats["completed"] += 1
        pid = result[3][5]["execution"]["pid"]
        stats["model_fit_seconds_sum"] += result[3][5]["execution"]["wall_seconds"]
        if pid not in stats["pids"]:
            stats["pids"].append(pid)
        return result

    if plan["backend"] == "serial":
        for job in jobs:
            stats["submitted"] += 1
            stats["max_in_flight"] = 1
            yield record(_run_metric(job, ctx, settings_snapshot))
        return

    if plan["backend"] == "process":
        executor = ProcessPoolExecutor(max_workers=plan["workers"],
                                       mp_context=multiprocessing.get_context("spawn"),
                                       initializer=_initialize_process,
                                       initargs=(ctx, settings_snapshot))
        submit_args = ()
        run = _run_process_metric
    else:
        executor = ThreadPoolExecutor(max_workers=plan["workers"])
        submit_args = (ctx, settings_snapshot)
        run = _run_metric
    pending = set()
    iterator = iter(jobs)
    exhausted = False
    try:
        while pending or not exhausted:
            while not exhausted and len(pending) < plan["max_in_flight"]:
                try:
                    job = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                pending.add(executor.submit(run, job, *submit_args))
                stats["submitted"] += 1
                stats["max_in_flight"] = max(stats["max_in_flight"], len(pending))
            if pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                # Surface an error before replenishing or returning partial results.
                results = [future.result() for future in done]
                for result in results:
                    yield record(result)
    finally:
        for future in pending:
            future.cancel()
        # Do not release the pipeline's update lock while abandoned workers still consume CPU/RAM.
        executor.shutdown(wait=True, cancel_futures=True)
