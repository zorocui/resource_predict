"""Frozen-model, selected-feedback gate replay; all-success offline cohort only."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from benchmarks.routing_budget import allocate
from benchmarks.routing_calibration import calibrated_costs, calibration_split
from benchmarks.routing_pause import PauseGate
from benchmarks.routing_pilot import METHODS
from benchmarks.routing_validation import predict_gain
from benchmarks.routing_effects import score_effects, summarize_effects


def replay_policy(batches, enabled, *, early_cost=False, min_mean_loss=0., probe_floor=False,
                  cumulative_loss_limit=None, probe_episode_ratio=None, exhausted_policy=None):
    if exhausted_policy not in (None, 'review', 'bounded_retry'):
        raise ValueError('Unknown exhausted policy')
    if exhausted_policy is not None and probe_episode_ratio is None:
        raise ValueError('Exhaustion policy requires an episode budget')
    if probe_episode_ratio is not None and (not np.isfinite(probe_episode_ratio) or probe_episode_ratio <= 0):
        raise ValueError('probe_episode_ratio must be finite and positive')
    gate = PauseGate(min_mean_loss, cumulative_loss_limit)
    pending, ledger, events, cost_events = [], [], [], []
    probe_epoch, probe_charge, episode_cap = None, 0., None
    pause_budget, last_grant, grants = 0., 0, 0
    for number, batch in enumerate(batches, 1):
        if early_cost:
            for event in pending:
                if event.get('cost_delivered') or event['issued'] >= number:
                    continue
                if event['kind']=='shadow' and event['epoch']==probe_epoch:
                    probe_charge += event['actual_cost']-event['planned_cost']
                # Explicit counterfactual: selected work completed before next decision.
                # Cost-only healthy feedback must not reset gain streaks or resume a gate.
                abnormal = event['actual_cost'] > event['budget']*1.1
                if enabled and abnormal and event['epoch'] == gate.epoch:
                    if gate.paused:
                        gate.healthy_streak = 0
                    else:
                        gate.observe({**event, 'gain': 0.})
                event['cost_delivered'] = True
                cost_events.append({'issued_batch': event['issued'], 'delivered_batch': number,
                                    'actual_cost': event['actual_cost'], 'budget': event['budget'],
                                    'abnormal': abnormal})
        delivered = sorted([e for e in pending if e['due'] < batch['known_by']], key=lambda e: (e['due'], e['issued']))
        pending = [e for e in pending if e['due'] >= batch['known_by']]
        for event in delivered:
            if not event.get('cost_delivered') and event['kind']=='shadow' and event['epoch']==probe_epoch:
                probe_charge += event['actual_cost']-event['planned_cost']
            if enabled:
                gate.observe(event)
            events.append({'issued_batch': event['issued'], 'delivered_batch': number,
                           'kind': event['kind'], 'epoch': event['epoch'], 'gain': event['gain'],
                           'actual_cost': event['actual_cost'], 'status': event['status']})
        cost, predicted = batch['cost'], batch['prediction']
        budget = batch['budget']
        if gate.paused and probe_epoch != gate.epoch:
            probe_epoch, probe_charge = gate.epoch, 0.
            episode_cap = probe_episode_ratio*budget if probe_episode_ratio is not None else None
            pause_budget, last_grant, grants = budget, number, 0
        action = 'active' if not gate.paused else ('shadow' if number % 3 == 0 else 'baseline')
        mask = np.zeros(len(cost), dtype=bool)
        probe_budget = .1*budget
        budget_disposition = 'normal'
        if action == 'active':
            order = np.lexsort((np.random.default_rng(42).random(len(cost)), -predicted/cost))
            mask = allocate(order[predicted[order] > 0], cost, budget)
        elif action == 'shadow':
            affordable = cost[(predicted > 0) & (cost <= budget)]
            if probe_floor and len(affordable):
                # Increase only the probe envelope, never the current batch's total budget.
                probe_budget = min(budget, max(probe_budget, float(affordable.min())))
            if episode_cap is not None:
                remaining = max(0., episode_cap-probe_charge)
                blocked_by_cap = len(affordable) and float(affordable.min()) > remaining
                pending_probe = any(e['kind']=='shadow' and e['epoch']==probe_epoch for e in pending)
                if blocked_by_cap and exhausted_policy is not None:
                    if pending_probe:
                        budget_disposition = 'awaiting_probe_feedback'
                    elif exhausted_policy=='bounded_retry' and grants < 2:
                        if number-last_grant >= 6:
                            episode_cap += .2*pause_budget
                            grants += 1
                            last_grant = number
                            budget_disposition = 'bounded_grant'
                        else:
                            budget_disposition = 'retry_wait'
                    else:
                        budget_disposition = 'review_required'
                probe_budget = min(probe_budget, max(0., episode_cap-probe_charge))
            # One random member of the current positive-gain pool, affordable within 10% budget.
            pool = np.flatnonzero((predicted > 0) & (cost <= probe_budget))
            if len(pool):
                mask[np.random.default_rng(42+number).choice(pool)] = True
        used = float(batch['actual'][mask].sum())
        candidate_gain = float(batch['gain'][mask].sum())
        ledger.append({'batch_number': number, 'action': action, 'reason': gate.reason,
                       'epoch': gate.epoch, 'selected': int(mask.sum()), 'candidates': len(cost),
                       'nominal_budget_seconds': budget, 'planned_cost_seconds': float(cost[mask].sum()),
                       'actual_cost_seconds': used, 'shadow_cost_seconds': used if action=='shadow' else 0.,
                       'applied_gain': candidate_gain if action=='active' else 0.,
                       'overrun_seconds': max(0., used-budget),
                       'feedback_issued': bool(mask.any()), 'no_affordable_probe': action=='shadow' and not mask.any()})
        ledger[-1]['probe_budget_seconds'] = probe_budget if action=='shadow' else 0.
        if 'records' in batch:
            ledger[-1]['prediction_effects'] = score_effects(batch['records'], mask, action)
        if action=='shadow':
            probe_charge += float(cost[mask].sum())
        ledger[-1].update(loss_balance=gate.loss_balance, probe_episode_cap=episode_cap,
                          probe_spent_or_reserved=probe_charge,
                          probe_episode_exhausted=bool(gate.paused and episode_cap is not None and probe_charge >= episode_cap),
                          budget_disposition=budget_disposition, extension_grants=grants,
                          extension_budget_seconds=grants*.2*pause_budget)
        if mask.any():
            pending.append({'issued': number, 'due': max(batch['due'][i] for i in np.flatnonzero(mask)),
                            'epoch': gate.epoch, 'kind': action, 'status': 'ok', 'gain': candidate_gain,
                            'actual_cost': used, 'selected': int(mask.sum()),
                            'planned_cost': float(cost[mask].sum()),
                            'budget': probe_budget if action=='shadow' else budget})
    return {'batches': ledger, 'delivered_feedback': events, 'delivered_cost_feedback': cost_events,
            'prediction_effects': summarize_effects(ledger),
            'summary': {'applied_gain': sum(r['applied_gain'] for r in ledger),
                        'cost_including_shadow': sum(r['actual_cost_seconds'] for r in ledger),
                        'shadow_cost': sum(r['shadow_cost_seconds'] for r in ledger),
                        'overrun_batches': sum(r['overrun_seconds'] > 1e-9 for r in ledger),
                        'paused_batches': sum(r['action']!='active' for r in ledger),
                        'no_affordable_probe_batches': sum(r['no_affordable_probe'] for r in ledger),
                        'review_required_batches': sum(r['budget_disposition']=='review_required' for r in ledger),
                        'extension_grant_events': sum(r['budget_disposition']=='bounded_grant' for r in ledger),
                        'pending_feedback': len(pending)}}


def analyze_gate(rows, *, start_origin=None, feedback_delay_hours=0, compare_feedback=False,
                 compare_exhaustion=False):
    if compare_exhaustion and not compare_feedback:
        raise ValueError('Exhaustion comparison requires a common feedback warm start')
    if feedback_delay_hours < 0:
        raise ValueError('Feedback delay must be nonnegative')
    complete = [r for r in rows if r.get('status') == 'paired' and all(
        r.get('methods', {}).get(m, {}).get(p, {}).get('status') == 'ok'
        for m in METHODS for p in ('validation', 'test'))]
    result = {'status': 'insufficient_data', 'input_rows': len(rows), 'eligible_rows': len(complete),
              'excluded_rows': len(rows)-len(complete), 'feedback_delay_hours': feedback_delay_hours}
    origins = sorted({pd.Timestamp(r['origin_time']) for r in rows if r.get('origin_time')})
    if len(origins) < 10:
        return result
    for r in complete:
        if not np.isfinite([r['delta_rmse'], r['delta_wall_seconds'], *r['features'].values()]).all():
            raise ValueError('Nonfinite record')
    start = pd.Timestamp(start_origin) if start_origin is not None else origins[int(.7*len(origins))]
    test_origins = [o for o in origins if o >= start]
    if not test_origins:
        return result
    delay = pd.Timedelta(hours=feedback_delay_hours)
    # Controlled comparison: perturb feedback only after a common historical warm start.
    training_delay = pd.Timedelta(0) if compare_feedback else delay
    first = [r for r in rows if r.get('origin_time') and pd.Timestamp(r['origin_time']) == test_origins[0]]
    if not first or any(not r.get('feature_end') for r in first):
        result['reason'] = 'missing_first_batch_feature_cutoff'
        return result
    cutoff = min(pd.Timestamp(r['feature_end']) for r in first)
    train = [r for r in complete if pd.Timestamp(r['test_end'])+training_delay < cutoff]
    fit, calibration = calibration_split(train)
    # Inner fitting also respects the same synthetic ingestion delay.
    if calibration:
        inner_cutoff = min(pd.Timestamp(r['feature_end']) for r in calibration)
        fit = [r for r in fit if pd.Timestamp(r['test_end'])+training_delay < inner_cutoff]
    result.update(train_rows=len(train), fit_rows=len(fit), calibration_rows=len(calibration))
    if len(train)<30 or len(fit)<30 or len(calibration)<10:
        return result
    batches, unscorable = [], []
    for origin_number, origin in enumerate(test_origins, 1):
        test = sorted([r for r in complete if pd.Timestamp(r['origin_time'])==origin], key=lambda r: r['series_id'])
        if not test:
            unscorable.append(origin_number)
            continue
        prediction, gain, _ = predict_gain(train, test)
        models, reference, _ = calibrated_costs(fit, calibration, test)
        batches.append({'records': test, 'known_by': min(pd.Timestamp(r['feature_end']) for r in test),
                        'due': [pd.Timestamp(r['test_end'])+delay for r in test],
                        'prediction': prediction, 'gain': gain, 'cost': models['conservative_q90'],
                        'actual': np.maximum([r['delta_wall_seconds'] for r in test], 0.),
                        'budget': .5*reference*len(test)})
    gated, ungated = replay_policy(batches, True), replay_policy(batches, False)
    result.update(status='partial' if unscorable else 'ok', evaluation_batches=len(batches),
                  unscorable_origin_numbers=unscorable, gated=gated, ungated=ungated,
                  comparison={'applied_gain_delta': gated['summary']['applied_gain']-ungated['summary']['applied_gain'],
                              'cost_delta': gated['summary']['cost_including_shadow']-ungated['summary']['cost_including_shadow'],
                              'overrun_batch_delta': gated['summary']['overrun_batches']-ungated['summary']['overrun_batches']})
    if compare_feedback:
        separate = replay_policy(batches, True, early_cost=True)
        result['separate_cost_gate'] = separate
        result['separate_vs_coupled'] = {
            'gain_delta': separate['summary']['applied_gain']-gated['summary']['applied_gain'],
            'cost_delta': separate['summary']['cost_including_shadow']-gated['summary']['cost_including_shadow'],
            'overrun_batch_delta': separate['summary']['overrun_batches']-gated['summary']['overrun_batches']}
        counts = [len(b['cost']) for b in batches]
        gaps = [float((b['known_by']-a['known_by']).total_seconds()/3600) for a,b in zip(batches,batches[1:])]
        result['batch_diagnostics'] = {'candidate_counts': counts, 'decision_gap_hours': gaps,
                                       'scheduler_batch_identity_verified': False}
    if compare_exhaustion:
        common = {'early_cost': True, 'min_mean_loss': .001, 'probe_floor': True,
                  'cumulative_loss_limit': .001, 'probe_episode_ratio': .6}
        result['exhaustion_policies'] = {
            policy: replay_policy(batches, True, exhausted_policy=policy, **common)
            for policy in ('review', 'bounded_retry')}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    result = {'schema': 'routing-gate-replay-v1', 'runs': [],
              'limits': 'All-success conditional cohort; excludes failed/missing rows before admission, not a failure-policy test. '
              'Initial 70% origin boundary selects frozen historical training, no online retraining. '
              'Only issued active/probe feedback affects gate; ungated reference is independent retrospective scoring. '
              'Fresh run boundary may shift with extra data; use fixed start_origin API for prefix invariance tests. '
              'Gain and costs delivered together at latest selected test_end plus delay, then conservative feature cutoff. '
              'One affordable random positive-predicted candidate per third paused batch, budget 10% of regular batch. '
              'Probe representativeness unproven; unaffordable probe can leave gate paused indefinitely. '
              'No actual deadline enforcement, failure recovery or production control; all costs are marginal stage proxies. '
              'No IDs, dates or paths exported; review internally.'}
    for index, directory in enumerate(args.run_dir):
        report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
        rows = [json.loads(line) for line in (directory/'pairs.jsonl').read_text(encoding='utf-8').splitlines()]
        if report['metadata'].get('index_unchanged') is False or report['summary']['attempts']!=len(rows):
            parser.error('Snapshot or row count mismatch')
        result['runs'].append({'run_number': index+1, 'delays': [analyze_gate(rows, feedback_delay_hours=d) for d in (0, 4, 24)]})
        print(f'Completed run {index+1}', flush=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)


if __name__ == '__main__':
    main()
