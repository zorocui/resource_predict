"""Causal per-origin evaluation; no budget transfer between forecast origins."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from benchmarks.routing_calibration import evaluate_calibration, overview
from benchmarks.routing_pilot import METHODS


def batch_analysis(rows):
    complete = [r for r in rows if r.get('status') == 'paired' and all(
        r.get('methods', {}).get(m, {}).get(p, {}).get('status') == 'ok'
        for m in METHODS for p in ('validation', 'test'))]
    for row in complete:
        if not np.isfinite([row['delta_rmse'], row['delta_wall_seconds'], *row['features'].values()]).all():
            raise ValueError('Nonfinite replay data')
    origins = sorted({pd.Timestamp(r['origin_time']) for r in rows if r.get('origin_time')})
    batches = []
    for index, origin in enumerate(origins):
        test = sorted([r for r in complete if pd.Timestamp(r['origin_time']) == origin], key=lambda r: r['series_id'])
        input_count = sum(pd.Timestamp(r['origin_time']) == origin for r in rows if r.get('origin_time'))
        if not test:
            batches.append({'batch_number': index+1, 'status': 'insufficient_data',
                            'reason': 'no_successful_pairs', 'input_rows': input_count,
                            'excluded_rows': input_count, 'test_rows': 0})
            continue
        known_by = min(pd.Timestamp(r['feature_end']) for r in test)
        train = [r for r in complete if pd.Timestamp(r['test_end']) < known_by]
        # Fixed seed: adding future origins cannot change current randomized decisions.
        result = evaluate_calibration(train, test, 42)
        batches.append({'batch_number': index+1, 'input_rows': input_count,
                        'excluded_rows': input_count-len(test), **result})
    table = overview({'resource_splits': [], 'time_splits': batches})
    for entry in table:
        entry['split_kind'] = 'forecast_batches'
    return {'schema': 'routing-batch-v2', 'input_rows': len(rows), 'eligible_rows': len(complete),
            'unassigned_rows': sum(not r.get('origin_time') for r in rows),
            'excluded_rows': len(rows)-len(complete), 'batch_count': len(batches),
            'valid_batches': sum(b['status']=='ok' for b in batches), 'batches': batches, 'overview': table,
            'limits': 'Exact origin_time groups, not verified scheduler batch IDs. Features/labels of future batches never compete for current budget. '
            'Labels assumed available at test_end, no measured ingestion delay. All-success conditional analysis; excluded rows counted. '
            'Costs remain retrospective stage estimates, not end-to-end production CPU. Nested calibration and nominal budgets unchanged. '
            'All origins evaluated, early insufficient batches retained; no frozen future confirmation subset. '
            'Adjacent batches correlated, distributions are descriptive. No identifiers/dates/paths exported. Internal review required.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists')
    bundle = {'schema': 'routing-batch-bundle-v1', 'runs': []}
    for index, directory in enumerate(args.run_dir):
        report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
        rows = [json.loads(line) for line in (directory/'pairs.jsonl').read_text(encoding='utf-8').splitlines()]
        if report['metadata'].get('index_unchanged') is False or report['summary']['attempts'] != len(rows):
            parser.error('Snapshot or record count mismatch')
        bundle['runs'].append({'run_number': index+1, **batch_analysis(rows)})
        print(f'Completed run {index+1}', flush=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(bundle, stream, indent=2, allow_nan=False)


if __name__ == '__main__':
    main()
