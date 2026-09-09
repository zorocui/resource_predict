import copy

import pytest

from benchmarks.routing_gate_options import fixtures
from benchmarks.routing_gate_replay import replay_policy
from benchmarks.routing_pause import PauseGate


def test_magnitude_uses_selected_mean_and_requires_two_material_losses():
    gate = PauseGate(.001)
    event = {'epoch': 0, 'kind': 'active', 'status': 'ok', 'gain': -.001,
             'selected': 10, 'actual_cost': .1, 'budget': 1.}
    gate.observe(event)
    gate.observe(event)
    assert not gate.paused
    event['gain'] = -.02
    gate.observe(event)
    assert not gate.paused
    gate.observe(event)
    assert gate.paused


def test_probe_floor_respects_batch_budget_and_can_restore():
    stream = next(s for name, s in fixtures() if name=='large_loss_then_recovery')
    old = replay_policy(stream, True, early_cost=True)
    new = replay_policy(stream, True, early_cost=True, probe_floor=True)
    assert old['summary']['no_affordable_probe_batches'] > 0
    assert new['summary']['no_affordable_probe_batches'] == 0
    assert new['summary']['shadow_cost'] > 0
    assert new['batches'][-1]['action']=='active'
    assert all(b['planned_cost_seconds'] <= b['nominal_budget_seconds'] for b in new['batches'])
    assert all(b['applied_gain']==0 for b in new['batches'] if b['action']=='shadow')


def test_floor_does_not_force_unaffordable_or_guarantee_actual_cost():
    streams = dict(fixtures())
    blocked = replay_policy(streams['unaffordable_probe'], True, early_cost=True, probe_floor=True)
    assert blocked['summary']['no_affordable_probe_batches'] > 0
    underestimated = replay_policy(streams['probe_cost_underestimated'], True, early_cost=True, probe_floor=True)
    assert underestimated['summary']['overrun_batches'] > 0


def test_future_prefix_invariance_with_options():
    stream = dict(fixtures())['tiny_loss_then_recovery']
    altered = copy.deepcopy(stream)
    for b in altered[20:]:
        b['gain'][:] = -999
        b['actual'][:] = 999
    options = {'early_cost': True, 'min_mean_loss': .001, 'probe_floor': True}
    assert replay_policy(stream, True, **options)['batches'][:20] == replay_policy(altered, True, **options)['batches'][:20]


def test_invalid_threshold_rejected():
    with pytest.raises(ValueError):
        PauseGate(float('nan'))
