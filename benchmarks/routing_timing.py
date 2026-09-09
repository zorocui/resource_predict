"""Synthetic independent workflow timing vs stage-composed estimates."""
import argparse
import json
from pathlib import Path
import time

from benchmarks.routing_pilot import FAST, METHODS, measure, replay, synthetic
from benchmarks.routing_share import distribution
from resource_predict.pipeline.forecasting import forecast_by_method


def workflow(series, methods, horizon=24):
    start = time.perf_counter()
    validation = {m: measure(m, series.iloc[:-2*horizon], series.iloc[-2*horizon:-horizon], forecast_by_method)
                  for m in methods}
    available = [m for m in methods if validation[m]['status']=='ok']
    if not available:
        return {'status': 'failed', 'wall_seconds': time.perf_counter()-start}
    selected = min(available, key=lambda m: validation[m]['rmse'])
    scored = measure(selected, series.iloc[:-horizon], series.iloc[-horizon:], forecast_by_method)
    return {'status': scored['status'], 'selected': selected, 'wall_seconds': time.perf_counter()-start,
            'candidate_failures': sum(v['status']!='ok' for v in validation.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 2 or args.output.exists():
        parser.error('Need >=2 repeats and a new output filename')
    records = []
    for name, series in synthetic(42):
        for repeat in range(args.repeats):
            pair = {}
            operations = ['baseline', 'enhanced', 'estimate']
            # Cycle all three measurement orders; each workflow independently fits candidates.
            operations = operations[repeat % 3:] + operations[:repeat % 3]
            for operation in operations:
                if operation == 'estimate':
                    pair[operation] = replay(series, 24, 1)[0]
                else:
                    pair[operation] = workflow(series, FAST if operation=='baseline' else METHODS)
            estimate = pair['estimate']
            valid = estimate['status']=='paired' and all(pair[k]['status']=='ok' for k in ('baseline', 'enhanced'))
            row = {'scenario': name, 'repeat': repeat, 'order': operations, 'status': 'ok' if valid else 'failed'}
            if valid:
                measured = pair['enhanced']['wall_seconds']-pair['baseline']['wall_seconds']
                row.update(independent_delta_seconds=measured, composed_delta_seconds=estimate['delta_wall_seconds'],
                           composition_error_seconds=estimate['delta_wall_seconds']-measured,
                           selections_match=all(pair[k]['selected']==estimate[k]['selected'] for k in ('baseline', 'enhanced')))
            records.append(row)
        print(f'Completed {name}', flush=True)
    result = {'schema': 'routing-timing-v1', 'records': records,
              'composition_error_seconds': distribution(r['composition_error_seconds'] for r in records if r['status']=='ok'),
              'limits': 'Synthetic workload only, shared interpreter, not isolated cold starts. Independent serial baseline and enhanced fits; '
              'wall time includes validation scoring; no CPU accounting or production all-model output/I-O. Stage estimate orders rotated. '
              'Does not prove production savings; no acceptance threshold inferred from these data.'}
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)


if __name__ == '__main__':
    main()
