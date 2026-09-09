from benchmarks.routing_batch import batch_analysis
from benchmarks.routing_stress import scenarios, synthetic_records


def test_failed_origins_remain_visible():
    name, rows = next((name, rows) for name, rows in scenarios() if name=='all_models_fail')
    assert name == 'all_models_fail'
    result = batch_analysis(rows)
    assert result['batch_count'] == 20
    assert result['excluded_rows'] == 60
    assert all(b['reason']=='no_successful_pairs' for b in result['batches'][-5:])


def test_missing_and_unassigned_rows_are_counted():
    rows = synthetic_records()
    rows.append({'status': 'skipped', 'reason': 'short_history'})
    result = batch_analysis(rows)
    assert result['unassigned_rows'] == 1
    assert result['excluded_rows'] == 1
