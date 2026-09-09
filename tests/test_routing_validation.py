import json

import numpy as np

from benchmarks.routing_validation import analyze, evaluate, selection_stats
from test_routing_share import records


def test_repeated_analysis_is_private_deterministic_and_has_rolling_tests():
    rows = records()
    result = analyze(rows)
    assert result == analyze(rows)
    assert len(result['resource_splits']) == 10
    assert len(result['time_splits']) == 3
    assert 'SECRET' not in json.dumps(result)
    assert '2026-01' not in json.dumps(result)
    for split in result['resource_splits']:
        assert split['status'] == 'ok'
        assert len(split['selection_curve']) == 4
        assert split['auto_rule']['status'] == 'unavailable_missing_archived_rule'


def test_time_splits_purge_future_labels_and_have_disjoint_test_rows(monkeypatch):
    import pandas as pd
    seen = set()
    calls = []

    def check(train, test, seed):
        calls.append((train, test))
        return {'status': 'ok'}

    monkeypatch.setattr('benchmarks.routing_validation.evaluate', check)
    analyze(records())
    for train, test in calls[10:]:
        assert max(pd.Timestamp(r['test_end']) for r in train) < min(pd.Timestamp(r['feature_end']) for r in test)
        keys = {(r['series_id'], r['origin_time']) for r in test}
        assert not seen.intersection(keys)
        seen.update(keys)


def test_equal_count_cost_and_conditional_cluster_interval():
    result = selection_stats(np.arange(10.), np.full(10, 2.), np.arange(10) >= 5,
                             np.array([str(i) for i in range(10)]), 1)
    assert result['observed_extra_wall_seconds'] == 10
    assert result['random_equal_count_expected_extra_wall_seconds'] == 10
    assert result['mean_gain'] == 7
    assert len(result['conditional_resource_bootstrap_95_interval']) == 2


def test_archived_rule_and_excluded_candidates():
    rows = records()
    for row in rows:
        row['auto_rule_run'] = False
    assert evaluate(rows[:100], rows[100:], 0)['auto_rule']['status'] == 'no_selection'
    rows[0]['methods']['prophet']['test']['status'] = 'failed'
    assert analyze(rows)['excluded_rows'] == 1
