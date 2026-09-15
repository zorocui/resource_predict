"""利用率验证与页面统计共用的容差规则。"""
import math

RATIO_MODES = {"cpu_usage/cpu_limit", "cpu_usage/cpu_request",
               "memory_working_set/memory_limit", "memory_working_set/memory_request"}


def tolerance_hit(actual: float, predicted: float) -> bool:
    error = abs(predicted - actual)
    tolerance = max(0.05, abs(actual) * 0.05)
    return error <= tolerance or math.isclose(error, tolerance, rel_tol=1e-12, abs_tol=1e-15)
