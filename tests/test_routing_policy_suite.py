import json

from benchmarks.routing_policy_suite import policy_suite
from benchmarks.routing_stress import synthetic_records


def test_suite_shares_budget_training_and_preserves_privacy():
    result = policy_suite(synthetic_records())
    assert len(result['delays']) == 3
    for delay in result['delays']:
        assert delay['status']=='ok'
        assert set(delay['policies'])=={'ungated', 'original_gate', 'review', 'bounded_retry'}
        ledgers = [r['batches'] for r in delay['policies'].values()]
        for items in zip(*ledgers):
            assert len({b['nominal_budget_seconds'] for b in items})==1
        assert delay['policy_overview']['ungated']['cost_delta_vs_ungated']==0
    assert result['delays'][0]['policies']['ungated']['batches']==result['delays'][2]['policies']['ungated']['batches']
    payload = json.dumps(result, allow_nan=False)
    assert 'synthetic-' not in payload and '2026-' not in payload


def test_short_input_is_not_treated_as_success():
    result = policy_suite(synthetic_records()[:12])
    assert all(d['status']=='insufficient_data' for d in result['delays'])
