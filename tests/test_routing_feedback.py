import copy
import json

import pandas as pd

from benchmarks.routing_feedback import compare
from benchmarks.routing_gate_replay import replay_policy
from benchmarks.routing_stress import synthetic_records
from test_routing_gate_replay import batches


def test_cost_can_pause_before_gain_but_not_prevent_initial_overrun():
    stream = batches()
    for b in stream:
        b['actual'][0] = 2
        b['due'] = [pd.Timestamp('2027-01-01')]*2
    coupled = replay_policy(stream, True)
    separate = replay_policy(stream, True, early_cost=True)
    assert coupled['summary']['paused_batches']==0
    assert separate['batches'][0]['action']=='active'
    assert separate['batches'][1]['action']=='baseline'
    assert separate['summary']['overrun_batches'] > 0
    assert separate['delivered_feedback']==[]
    assert separate['delivered_cost_feedback'][0]['delivered_batch']==2


def test_healthy_cost_does_not_erase_negative_gain_streak():
    stream = batches()
    result = replay_policy(stream, True, early_cost=True)
    assert result['batches'][2]['action']=='shadow'
    assert result['batches'][2]['reason']=='negative_gain_streak'


def test_delay_variants_keep_models_and_budgets_fixed_and_private():
    result = compare(synthetic_records())
    variants = result['delays']
    assert all(r['status']=='ok' for r in variants)
    for r in variants[1:]:
        assert r['train_rows']==variants[0]['train_rows']
        assert r['ungated']['batches']==variants[0]['ungated']['batches']
    assert 'synthetic-' not in json.dumps(result)
    assert '2026-' not in json.dumps(result)


def test_unselected_actual_cost_cannot_trigger_early_gate():
    stream = batches()
    altered = copy.deepcopy(stream)
    for b in altered:
        b['actual'][1] = 10000
    assert replay_policy(stream, True, early_cost=True)==replay_policy(altered, True, early_cost=True)
