import copy
import json

from benchmarks.routing_signal_audit import diagnose, history_signals
from test_routing_memory_audit import batch


def test_history_matching_does_not_export_identity():
    b = batch()
    rows = b['records']
    rows[0]['delta_rmse'] = -.1
    report = history_signals(rows, [b])
    assert 'SECRET' not in json.dumps(report)
    assert next(r for r in report if r['metric']=='memory_request')['top_test_resources_with_positive_train_mean']==0


def test_restoration_uses_history_scale_not_test_outcomes():
    b = batch()
    for i, row in enumerate(b['records']):
        row['features'] = {'mean': .1+i}
    changed = copy.deepcopy(b)
    for row in changed['records']:
        row['delta_rmse'] = 999
    before, after = diagnose(b['records'], [b]), diagnose(b['records'], [changed])
    assert before['selection_disagreement']==after['selection_disagreement']
    for name in before['ranking_controls']:
        assert before['ranking_controls'][name]['selected']==after['ranking_controls'][name]['selected']
        assert before['ranking_controls'][name]['predicted_cost'] <= b['budget']
