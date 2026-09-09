import copy

from benchmarks.routing_gate_options import fixtures
from benchmarks.routing_gate_replay import replay_policy
from benchmarks.routing_pause import PauseGate


def test_small_losses_accumulate_and_positive_feedback_repays():
    gate = PauseGate(.001, .001)
    event = {'epoch': 0, 'kind': 'active', 'status': 'ok', 'gain': -.0004,
             'selected': 2, 'actual_cost': .1, 'budget': 1.}
    for _ in range(4):
        gate.observe(event)
    assert not gate.paused
    gate.observe(event)
    assert gate.paused and gate.reason=='cumulative_loss'
    other = PauseGate(.001, .001)
    other.observe(event)
    other.observe({**event, 'gain': .01})
    assert other.loss_balance==0


def test_probe_cap_limits_repeated_shocks_but_cannot_prevent_first_overshoot():
    stream = dict(fixtures())['probe_cost_underestimated']
    options = {'early_cost': True, 'min_mean_loss': .001, 'probe_floor': True}
    old = replay_policy(stream, True, **options)
    limited = replay_policy(stream, True, probe_episode_ratio=.6, **options)
    assert limited['summary']['shadow_cost'] < old['summary']['shadow_cost']
    assert limited['summary']['overrun_batches'] > 0
    assert any(b['probe_episode_exhausted'] for b in limited['batches'])


def test_pending_probe_predictions_are_reserved_without_future_actual_cost():
    stream = dict(fixtures())['large_loss_then_recovery']
    options = {'min_mean_loss': .001, 'probe_floor': True, 'probe_episode_ratio': .4}
    result = replay_policy(stream, True, **options)
    for b in result['batches']:
        if b['action']=='shadow' and b['selected']:
            assert b['probe_spent_or_reserved'] <= b['probe_episode_cap']+1e-9
    changed = copy.deepcopy(stream)
    for b in changed[25:]:
        b['actual'][:] = 999
    assert result['batches'][:25]==replay_policy(changed, True, **options)['batches'][:25]
