import copy
import json

import numpy as np

from benchmarks.routing_budget import allocate, budget_analysis, evaluate_budget, predict_cost, score
from test_routing_share import records


def test_allocation_skips_unaffordable_and_reports_actual_overrun():
    estimated = np.array([10., 2., 1.])
    mask = allocate([0, 1, 2], estimated, 3.)
    assert mask.tolist() == [False, True, True]
    result = score(mask, np.ones(3), estimated, np.array([10., 4., 2.]), 3.)
    assert result['observed_overrun_seconds'] == 3
    assert result['predicted_unused_seconds'] == 0


def test_test_costs_and_labels_do_not_change_budget_or_selection():
    rows = records()
    train, test = rows[:100], rows[100:]
    altered = copy.deepcopy(test)
    for row in altered:
        row['delta_wall_seconds'] = 100
        row['delta_rmse'] = -100
    assert np.array_equal(predict_cost(train, test)[0], predict_cost(train, altered)[0])
    before, after = evaluate_budget(train, test, 1), evaluate_budget(train, altered, 1)
    for left, right in zip(before['budget_curve'], after['budget_curve']):
        assert left['budget_seconds'] == right['budget_seconds']
        for name in ('predicted_gain', 'predicted_gain_per_cost'):
            assert left['policies'][name]['selected'] == right['policies'][name]['selected']
            assert left['policies'][name]['predicted_spend_seconds'] <= left['budget_seconds']
            assert right['policies'][name]['observed_overrun_seconds'] > 0


def test_budget_report_is_private_and_handles_negative_cost_without_dropping():
    rows = records()
    rows[0]['delta_wall_seconds'] = -.1
    result = budget_analysis(rows)
    assert result['eligible_rows'] == len(rows)
    assert len(result['resource_splits']) == 10
    assert len(result['time_splits']) == 3
    payload = json.dumps(result, allow_nan=False)
    assert 'SECRET' not in payload
    assert '2026-01' not in payload
