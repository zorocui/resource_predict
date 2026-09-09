"""One private-record replay, four fixed policies, three feedback delays."""
import argparse
import json
from pathlib import Path

from benchmarks.routing_gate_replay import analyze_gate


def summarize_policy(report, reference):
    summary = dict(report['summary'])
    batches = report['batches']
    recovery_epochs = {b['epoch'] for b in batches if b['action']=='active' and b['reason']=='two_healthy_probes'}
    summary.update(recoveries=len(recovery_epochs),
                   first_pause_batch=next((b['batch_number'] for b in batches if b['action']!='active'), None),
                   final_action=batches[-1]['action'] if batches else None,
                   gain_delta_vs_ungated=summary['applied_gain']-reference['applied_gain'],
                   cost_delta_vs_ungated=summary['cost_including_shadow']-reference['cost_including_shadow'],
                   negative_active_batches=sum(b['action']=='active' and b['applied_gain'] < -1e-12 for b in batches))
    return summary


def policy_suite(rows):
    delays = []
    for hours in (0, 4, 24):
        result = analyze_gate(rows, feedback_delay_hours=hours, compare_feedback=True, compare_exhaustion=True)
        if result['status'] in ('ok', 'partial'):
            policies = {'ungated': result['ungated'], 'original_gate': result['separate_cost_gate'],
                        **result['exhaustion_policies']}
            result['policy_overview'] = {name: summarize_policy(report, result['ungated']['summary'])
                                         for name, report in policies.items()}
            # Keep one copy of each full event ledger; avoid repeating old coupled comparisons.
            result['policies'] = policies
            for key in ('gated', 'ungated', 'comparison', 'separate_cost_gate', 'separate_vs_coupled', 'exhaustion_policies'):
                result.pop(key, None)
        delays.append(result)
    return {'delays': delays}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; choose a new name')
    if len({p.resolve() for p in args.run_dir}) != len(args.run_dir):
        parser.error('Duplicate directory')
    bundle = {'schema': 'routing-policy-suite-v1', 'runs': [],
              'protocol': {'policies': ['ungated', 'original_gate', 'review', 'bounded_retry'],
                           'mean_loss_threshold': .001, 'cumulative_loss_limit': .001,
                           'initial_probe_episode_ratio': .6, 'extension_ratio': .2,
                           'max_extensions': 2, 'extension_min_batch_gap': 6,
                           'budget_fraction': .5, 'cost_model': 'conservative_q90',
                           'feedback_delays_hours': [0, 4, 24]},
              'limits': 'Fixed development candidates, not final independent validation. Same frozen training/models and per-origin budgets. '
              'Cost known before next scorable decision is an assumption, no completion timestamps. '
              'review and bounded_retry differ only in authorized extension credit; additional spending is explicit. '
              'Relative to original_gate, they also change magnitude/cumulative triggers and probe floor, so do not attribute all differences to exhaustion policy. '
              'Epoch reservations reconcile only delivered costs; underestimation may exceed cap, no hard interruption. '
              'review_required is a label, no external message or execution. Exact-origin global scope is not verified business/scheduler scope. '
              'All-success cohort; unscorable batches partial, not real failure recovery. Unaffordable or pending probes may prevent recovery. '
              'No thresholds selected from test report. Old 14-day data is development data after repeated inspection. '
              'No private paths, identifiers or exact dates exported; internal review required.'}
    for number, directory in enumerate(args.run_dir, 1):
        report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
        rows = [json.loads(line) for line in (directory/'pairs.jsonl').read_text(encoding='utf-8').splitlines()]
        if report['metadata'].get('index_unchanged') is False or report['summary']['attempts'] != len(rows):
            parser.error('Snapshot or record count mismatch')
        bundle['runs'].append({'run_number': number, **policy_suite(rows)})
        print(f'Completed run {number}/{len(args.run_dir)}', flush=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(bundle, stream, indent=2, allow_nan=False)


if __name__ == '__main__':
    main()
