import copy
import json

import numpy as np
import pandas as pd

from benchmarks.routing_gate_replay import analyze_gate, replay_policy
from benchmarks.routing_stress import synthetic_records
from benchmarks.routing_stress import scenarios


def batches():
    result = []
    for i in range(16):
        origin = pd.Timestamp('2026-01-01')+pd.Timedelta(days=i)
        result.append({'known_by': origin, 'due': [origin+pd.Timedelta(hours=1)]*2,
                       'cost': np.array([.08, .08]), 'prediction': np.array([.1, -.1]),
                       'gain': np.array([-.3, 100.]), 'actual': np.array([.08, .08]), 'budget': 1.})
    return result


def test_unselected_outcomes_do_not_enter_feedback_or_recovery():
    original = batches()
    altered = copy.deepcopy(original)
    for b in altered:
        b['gain'][1] = -999
        b['actual'][1] = 999
    assert replay_policy(original, True) == replay_policy(altered, True)
    result = replay_policy(original, True)
    assert result['summary']['paused_batches'] > 0
    assert result['summary']['shadow_cost'] > 0
    assert all(e['gain']==-.3 for e in result['delivered_feedback'])


def test_recovery_requires_selected_probe_feedback_and_charges_it():
    stream = batches()
    for b in stream[4:]:
        b['gain'][0] = .3
    result = replay_policy(stream, True)
    assert result['batches'][-1]['action']=='active'
    assert all(b['applied_gain']==0 for b in result['batches'] if b['action']=='shadow')
    assert result['summary']['cost_including_shadow'] == sum(b['actual_cost_seconds'] for b in result['batches'])


def test_frozen_history_and_future_invariance_no_private_export():
    rows = synthetic_records()
    start = '2026-01-15'
    before = analyze_gate(rows, start_origin=start)
    assert before['status']=='ok'
    future = copy.deepcopy(rows)
    for r in future:
        for field in ('origin_time', 'feature_end', 'test_end'):
            r[field] = str(pd.Timestamp(r[field])+pd.Timedelta(days=100))
        r['delta_rmse'] = -500
    after = analyze_gate(rows+future, start_origin=start)
    assert before['gated']['batches'] == after['gated']['batches'][:len(before['gated']['batches'])]
    assert 'synthetic-' not in json.dumps(before)
    assert '2026-' not in json.dumps(before)


def test_delay_postpones_feedback_and_unaffordable_probes_stay_visible():
    stream = batches()
    for b in stream:
        b['cost'] = np.array([.2, .2])
    result = replay_policy(stream, True)
    assert result['summary']['no_affordable_probe_batches'] > 0
    assert result['batches'][-1]['action']!='active'
    for b in stream:
        b['due'] = [pd.Timestamp('2027-01-01')]*2
    delayed = replay_policy(stream, True)
    assert delayed['summary']['paused_batches']==0


def test_failed_future_does_not_move_training_boundary_backwards():
    rows = next(rows for name, rows in scenarios() if name=='all_models_fail')
    report = analyze_gate(rows)
    assert report['status']=='partial'
    assert report['unscorable_origin_numbers']==[2, 3, 4, 5, 6]
    assert report['evaluation_batches']==1
