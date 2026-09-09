"""Deterministic synthetic record-level stress tests, not forecasting benchmarks."""
import argparse
import copy
import json
from pathlib import Path

import pandas as pd

from benchmarks.routing_batch import batch_analysis
from benchmarks.routing_pilot import METHODS


def synthetic_records():
    rows = []
    for day in range(20):
        origin = pd.Timestamp('2026-01-01')+pd.Timedelta(days=day)
        for resource in range(12):
            rows.append({'series_id': json.dumps([f'synthetic-{resource}', 'app', 'cpu']),
                         'status': 'paired', 'origin_time': str(origin),
                         'feature_end': str(origin-pd.Timedelta(days=2)),
                         'test_end': str(origin+pd.Timedelta(hours=23)),
                         'features': {'mean': .5, 'std': .1, 'points': 500+day*24,
                                      'range': .2, 'slope': resource/100, 'step_seconds': 3600},
                         'delta_rmse': (resource-3)/100, 'delta_wall_seconds': .2+resource*.01,
                         'methods': {m: {p: {'status': 'ok'} for p in ('validation', 'test')} for m in METHODS}})
    return rows


def scenarios():
    original = synthetic_records()
    yield 'stable', original
    for name in ('cost_triples', 'gain_reverses', 'all_models_fail', 'missing_observations'):
        rows = copy.deepcopy(original)
        for row in rows:
            if pd.Timestamp(row['origin_time']).day < 16:
                continue
            if name == 'cost_triples':
                row['delta_wall_seconds'] *= 3
            elif name == 'gain_reverses':
                row['delta_rmse'] *= -1
            elif name == 'all_models_fail':
                row['status'] = 'failed'
                for phases in row['methods'].values():
                    for phase in phases.values():
                        phase['status'] = 'failed'
            else:
                row['status'] = 'skipped'
                row['reason'] = 'missing_or_irregular'
        yield name, rows


def run_stress():
    results = []
    for name, rows in scenarios():
        result = batch_analysis(rows)
        batches = []
        for b in result['batches']:
            out = {key: b[key] for key in ('batch_number', 'status', 'input_rows', 'excluded_rows')}
            if b['status'] == 'ok':
                model = next(m for m in b['models'] if m['model']=='conservative_q90')
                curve = next(c for c in model['budget_curve'] if c['budget_fraction']==.5)
                out['primary'] = curve['policies']['predicted_gain_per_cost']
            batches.append(out)
        results.append({'scenario': name, 'valid_batches': result['valid_batches'],
                        'batch_count': result['batch_count'], 'excluded_rows': result['excluded_rows'],
                        'batches': batches})
    return {'schema': 'routing-stress-v1', 'scenarios': results,
            'limits': 'Synthetic replay labels/costs injected; no actual Prophet fits, runtime failures or missing-data ingestion simulated. '
            'Change begins at batch 16. Early insufficient history is expected. Primary fixed Q90/ratio/50%. '
            'Measures evaluator behavior and failure visibility, not production risk probabilities or recovery guarantees.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    result = run_stress()
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print('Five record-level stress scenarios completed.')


if __name__ == '__main__':
    main()
