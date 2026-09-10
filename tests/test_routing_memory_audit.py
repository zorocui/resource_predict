import copy
import json

import numpy as np

from benchmarks.routing_memory_audit import audit_batches


def batch():
    rows = []
    for i, metric in enumerate(('memory_request', 'memory_limit', 'cpu_limit', 'cpu_request')):
        rows.append({'series_id': json.dumps([f'SECRET-{i}', 'app', metric]), 'delta_rmse': .1,
                     'baseline': {'rmse': .3, 'mae': .2, 'estimated_workflow_wall_seconds': .1},
                     'enhanced': {'rmse': .2, 'mae': .1, 'estimated_workflow_wall_seconds': 1.1}})
    return {'records': rows, 'prediction': np.array([-.1, .01, 1., .1]),
            'cost': np.array([1., 2., 1., 1.]), 'actual': np.ones(4), 'budget': 1.}


def test_disjoint_miss_reasons_and_signed_cost_scoring():
    r = audit_batches([batch()])
    groups = r['missed_benefits_by_metric']
    assert groups['memory_request']['missed_nonpositive_prediction']==1
    assert groups['memory_limit']['missed_cost_above_batch_budget']==1
    assert groups['cpu_request']['missed_ranking_or_remaining_budget']==1
    assert groups['cpu_limit']['captured_positive_gain_fraction']==1
    for c in r['controls'].values():
        assert c['predicted_incremental_spend'] <= c['nominal_budget_sum']
    assert 'SECRET' not in json.dumps(r)


def test_actual_gains_and_costs_cannot_change_admission():
    b = batch()
    changed = copy.deepcopy(b)
    changed['actual'] *= 100
    for row in changed['records']:
        row['delta_rmse'] = -99
    before, after = audit_batches([b]), audit_batches([changed])
    for name in before['controls']:
        assert before['controls'][name]['selected']==after['controls'][name]['selected']
        assert before['controls'][name]['predicted_incremental_spend']==after['controls'][name]['predicted_incremental_spend']
