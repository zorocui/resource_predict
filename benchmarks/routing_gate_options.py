"""Local ablations of magnitude gating and within-batch one-probe allowance."""
import argparse
import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd

from benchmarks.routing_gate_replay import replay_policy

OPTIONS = {'original': (0., False), 'magnitude_only': (.001, False),
           'probe_only': (0., True), 'combined': (.001, True),
           'combined_low_threshold': (.0001, True), 'combined_high_threshold': (.01, True)}


def fixtures():
    base = []
    for i in range(30):
        origin = pd.Timestamp('2026-01-01')+pd.Timedelta(days=i)
        base.append({'known_by': origin, 'due': [origin+pd.Timedelta(days=2)]*2,
                     'cost': np.array([.2, .3]), 'actual': np.array([.2, .3]),
                     'prediction': np.array([.1, .1]), 'gain': np.array([.02, .01]), 'budget': 1.})
    yield 'stable', base
    for scenario in ('tiny_loss_then_recovery', 'persistent_small_loss', 'large_loss_then_recovery',
                     'cost_shock', 'unaffordable_probe', 'probe_cost_underestimated'):
        stream = copy.deepcopy(base)
        for i, b in enumerate(stream):
            if i >= 6:
                if scenario == 'persistent_small_loss' or (scenario=='tiny_loss_then_recovery' and i<10):
                    b['gain'][:] = -.0002
                elif scenario in ('large_loss_then_recovery', 'unaffordable_probe', 'probe_cost_underestimated') and i<14:
                    b['gain'][:] = -.02
                elif scenario == 'cost_shock':
                    b['actual'] *= 4
            if scenario=='unaffordable_probe' and i>=10:
                b['cost'][:] = 2
            if scenario=='probe_cost_underestimated' and i>=14:
                b['actual'][:] = 2
        yield scenario, stream


def analyze_options():
    runs = []
    for name, stream in fixtures():
        reports = {}
        for label, (threshold, floor) in OPTIONS.items():
            r = replay_policy(stream, True, early_cost=True, min_mean_loss=threshold, probe_floor=floor)
            reports[label] = r
        runs.append({'scenario': name, 'ungated': replay_policy(stream, False), 'options': reports})
    return {'schema': 'routing-gate-options-v1', 'runs': runs,
            'protocol': {'mean_normalized_loss_thresholds': [0., .0001, .001, .01],
                         'confirmations': 2, 'probe_rule': 'max(10% budget, cheapest positive-predicted affordable candidate), capped at batch budget'},
            'limits': 'Local synthetic candidate arrays, not new production evidence. Thresholds are exploratory after observing prior results; '
            'no automatic best setting. Mean loss weights selected tasks equally, not workloads. Ignored small losses can accumulate. '
            'Probe floor consumes more shadow budget and biases toward cheap candidates; no representation/recovery guarantee. '
            'Actual cost may exceed planned probe or batch budget. Gate scope still global; no inferred business grouping. '
            'Default replay remains unchanged unless options explicitly enabled. No production mutation.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    result = analyze_options()
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print('Seven scenarios and six gate variants completed locally.')


if __name__ == '__main__':
    main()
