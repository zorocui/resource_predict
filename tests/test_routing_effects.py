import json

import pytest

from benchmarks.routing_effects import score_effects, summarize_effects


def row(metric, rmse, mae, cost):
    return {'series_id': json.dumps(['SECRET', 'SECRET', metric]),
            'baseline': {'rmse': 2., 'mae': 1., 'estimated_workflow_wall_seconds': 1.},
            'enhanced': {'rmse': rmse, 'mae': mae, 'estimated_workflow_wall_seconds': cost}}


def test_exact_paired_scoring_and_shadow_cost():
    records = [row('cpu_limit', 1., .5, 3.), row('memory_limit', 3., 2., 4.)]
    active = score_effects(records, [True, False], 'active')
    assert active['estimated_cost_seconds']=={'fast_only': 2., 'all_enhanced': 7., 'policy': 4.}
    shadow = score_effects(records, [True, False], 'shadow')
    assert shadow['groups']['cpu_limit']['policy_rmse_sum']==2.
    assert shadow['estimated_cost_seconds']['policy']==4.
    report = summarize_effects([{'prediction_effects': active}])
    assert report['metrics'][0]['rmse_increase_pct_vs_fast_only']==-50.
    assert report['estimated_cost_saving_pct_vs_all_enhanced']==pytest.approx(300/7)
    assert report['measured_end_to_end_cost_seconds'] is None
    assert 'SECRET' not in json.dumps(report)


def test_missing_scores_and_zero_reference_do_not_invent_results():
    record = row('cpu', 0., 0., .5)
    record['baseline']['rmse']=0.
    result = summarize_effects([{'prediction_effects': score_effects([record, {}], [True, False], 'active')}])
    assert result['status']=='partial'
    assert result['unavailable_rows']==1
    assert result['metrics'][0]['rmse_increase_pct_vs_fast_only'] is None
