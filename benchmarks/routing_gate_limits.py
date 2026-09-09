"""Local cumulative-loss and pause-episode probe-reservation ablations."""
import argparse
import json
from pathlib import Path

from benchmarks.routing_gate_options import fixtures
from benchmarks.routing_gate_replay import replay_policy

OPTIONS = {'combined_previous': {}, 'cumulative_only': {'cumulative_loss_limit': .001},
           'probe_cap_only': {'probe_episode_ratio': .6},
           'both': {'cumulative_loss_limit': .001, 'probe_episode_ratio': .6}}


def run_limits():
    runs = []
    for name, stream in fixtures():
        runs.append({'scenario': name, 'options': {label: replay_policy(
            stream, True, early_cost=True, min_mean_loss=.001, probe_floor=True, **options)
            for label, options in OPTIONS.items()}})
    return {'schema': 'routing-gate-limits-v1', 'runs': runs,
            'limits': 'Synthetic local ablations, not production or independent confirmation. '
            'Loss balance=max(0,balance-mean selected gain) on delivered epoch-valid feedback; positive gains repay balance. '
            'Default cumulative threshold .001 is exploratory. Probe episode cap=.6 times budget at pause entry; '
            'reserve predicted costs on issuance, reconcile only on delivered cost, do not reset cap each batch. '
            'Actual costs can exceed cap from underestimated or outstanding work; cap is admission accounting, not interruption. '
            'Recovery starts a new episode; exhausted episode can stay paused indefinitely. No global lifetime quota. '
            'All old options default disabled; no production mutation.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    result = run_limits()
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print('Seven local scenarios, four ablation configurations completed.')


if __name__ == '__main__':
    main()
