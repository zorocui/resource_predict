from benchmarks.routing_pause import PauseGate, simulate


def test_future_change_does_not_affect_earlier_actions():
    stable = simulate('stable')['history']
    changed = simulate('gain_reversal')['history']
    assert stable[:11] == changed[:11]
    # First two adverse reports arrive at batches 15 and 16.
    assert changed[14]['action'] == 'active'
    assert changed[15]['action'] == 'baseline'


def test_shadow_feedback_required_for_recovery_and_cost_charged():
    report = simulate('recovery')
    history = report['history']
    assert any(r['action']=='baseline' for r in history)
    assert history[-1]['action'] == 'active'
    assert all(r['applied_gain']==0 for r in history if r['action']=='shadow')
    assert report['summary']['shadow_cost'] > 0
    assert report['summary']['extra_cost_including_shadow'] == sum(r['actual_extra_cost'] for r in history)


def test_stale_epoch_cannot_resume_gate():
    gate = PauseGate()
    event = {'epoch': 0, 'kind': 'active', 'status': 'ok', 'gain': .3, 'actual_cost': 3., 'budget': 1.}
    gate.observe(event)
    assert gate.paused
    event.update(kind='shadow', actual_cost=.01)
    gate.observe(event)
    gate.observe(event)
    assert gate.paused


def test_failure_is_unknown_and_first_cost_shock_is_not_prevented():
    failed = simulate('failure')
    assert failed['summary']['unknown_active_outcomes'] > 0
    assert simulate('cost_shock')['summary']['overrun_batches'] > 0
    assert simulate('cost_shock')['summary']['overrun_batches'] < simulate('cost_shock', enabled=False)['summary']['overrun_batches']
