"""Synthetic event-level pause/recovery experiment; not a production controller."""
import argparse
import json
import math
from pathlib import Path


class PauseGate:
    """Consume only delivered outcomes from actions actually issued in this epoch."""

    def __init__(self, min_mean_loss=0., cumulative_loss_limit=None):
        if not math.isfinite(min_mean_loss) or min_mean_loss < 0:
            raise ValueError('min_mean_loss must be finite and nonnegative')
        self.min_mean_loss = min_mean_loss
        if cumulative_loss_limit is not None and (not math.isfinite(cumulative_loss_limit) or cumulative_loss_limit <= 0):
            raise ValueError('cumulative_loss_limit must be finite and positive')
        self.cumulative_loss_limit = cumulative_loss_limit
        self.loss_balance = 0.
        self.paused = False
        self.epoch = 0
        self.negative_streak = 0
        self.healthy_streak = 0
        self.reason = 'initial'

    def observe(self, event):
        if event['epoch'] != self.epoch:
            return
        healthy = (event['status'] == 'ok' and event['gain'] > 0
                   and event['actual_cost'] <= event['budget']*1.1)
        if self.paused:
            if event['kind'] != 'shadow':
                return
            self.healthy_streak = self.healthy_streak+1 if healthy else 0
            if self.healthy_streak >= 2:
                self.paused = False
                self.reason = 'two_healthy_probes'
                self.epoch += 1
                self.negative_streak = self.healthy_streak = 0
                self.loss_balance = 0.
            return
        if event['status'] != 'ok':
            reason = 'missing_or_failed_feedback'
        elif event['actual_cost'] > event['budget']*1.1:
            reason = 'cost_overrun'
        else:
            mean_gain = event['gain']/max(1, event.get('selected', 1))
            self.loss_balance = max(0., self.loss_balance-mean_gain)
            self.negative_streak = self.negative_streak+1 if mean_gain < -self.min_mean_loss else 0
            reason = 'negative_gain_streak' if self.negative_streak >= 2 else None
            if self.cumulative_loss_limit is not None and self.loss_balance >= self.cumulative_loss_limit:
                reason = 'cumulative_loss'
        if reason:
            self.paused = True
            self.reason = reason
            self.epoch += 1
            self.healthy_streak = 0


def simulate(scenario, *, enabled=True, delay=3, batches=40):
    if delay < 1 or batches < 1:
        raise ValueError('delay and batches must be positive')
    names = ('stable', 'cost_shock', 'gain_reversal', 'failure', 'missing', 'recovery')
    if scenario not in names:
        raise ValueError('Unknown scenario')
    gate, pending, history = PauseGate(), [], []
    for batch in range(1, batches+1):
        arrived = [e for e in pending if e['due'] <= batch]
        pending = [e for e in pending if e['due'] > batch]
        for event in arrived:
            if enabled:
                gate.observe(event)
        # Action decided before generating this batch's outcome.
        kind = 'active' if not gate.paused else ('shadow' if batch % 3 == 0 else 'baseline')
        nominal_cost = .8 if kind == 'active' else (.08 if kind == 'shadow' else 0.)
        status, multiplier, gain = 'ok', 1., .3 if kind=='active' else .03
        adverse = batch >= 12 and (scenario != 'recovery' or batch <= 22)
        if adverse:
            if scenario == 'cost_shock':
                multiplier = 3.
            elif scenario in ('gain_reversal', 'recovery'):
                gain *= -1
            elif scenario in ('failure', 'missing'):
                status, gain = scenario, None
        if kind == 'baseline':
            gain, status = 0., 'not_issued'
        actual = nominal_cost*multiplier
        history.append({'batch': batch, 'action': kind, 'reason': gate.reason, 'epoch': gate.epoch,
                        'planned_extra_cost': nominal_cost, 'actual_extra_cost': actual,
                        'candidate_gain': gain if kind!='baseline' else None,
                        'applied_gain': gain if kind=='active' and status=='ok' else 0.,
                        'unknown_applied_outcome': kind=='active' and status!='ok',
                        'overrun': actual > 1., 'status': status})
        if kind != 'baseline':
            pending.append({'due': batch+delay, 'epoch': gate.epoch, 'kind': kind, 'status': status,
                            'gain': gain, 'actual_cost': actual,
                            'budget': 1. if kind=='active' else .1})
    return {'enabled': enabled, 'feedback_delay_batches': delay, 'history': history,
            'summary': {'active_batches': sum(r['action']=='active' for r in history),
                        'shadow_batches': sum(r['action']=='shadow' for r in history),
                        'baseline_batches': sum(r['action']=='baseline' for r in history),
                        'known_applied_gain': sum(r['applied_gain'] for r in history),
                        'unknown_active_outcomes': sum(r['unknown_applied_outcome'] for r in history),
                        'extra_cost_including_shadow': sum(r['actual_extra_cost'] for r in history),
                        'shadow_cost': sum(r['actual_extra_cost'] for r in history if r['action']=='shadow'),
                        'overrun_batches': sum(r['overrun'] for r in history),
                        'pending_feedback': len(pending)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    report = {'schema': 'routing-pause-simulation-v1',
              'protocol': {'budget': 1., 'negative_feedback_confirmations': 2, 'cost_trip_ratio': 1.1,
                           'shadow_every_batches': 3, 'shadow_budget': .1, 'healthy_recovery_confirmations': 2},
              'runs': [{'scenario': s, 'delay': d, 'gated': simulate(s, delay=d),
                        'ungated': simulate(s, enabled=False, delay=d)}
                       for s in ('stable', 'cost_shock', 'gain_reversal', 'failure', 'missing', 'recovery')
                       for d in (1, 3, 6)],
              'limits': 'Event-level synthetic fixture, not fitted forecasts, resource allocation or real execution. '
              'Probe is assumed representative of active payoff; this assumption is unverified. Failed/missing active gains are unknown, '
              'not zero successes; known_applied_gain excludes them. Missing feedback produces an explicit timeout notice at due time; '
              'no silent-loss/staleness handling. Shadow gains never applied; all costs charged including failed actions. '
              'Cost feedback is deliberately delayed like gain (conservative fixture); production may know runtime sooner. '
              'Thresholds fixed for mechanics testing, not validated risk policy. No hard budget guarantee, no production activation.'}
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print('18 gated/ungated synthetic comparisons completed.')


if __name__ == '__main__':
    main()
