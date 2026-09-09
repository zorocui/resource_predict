"""Readable error/cost comparison using existing private replay records only."""
import argparse
import json
from pathlib import Path

from benchmarks.routing_policy_suite import policy_suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    output = {'schema': 'routing-results-v1', 'runs': []}
    for index, directory in enumerate(args.run_dir, 1):
        report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
        rows = [json.loads(line) for line in (directory/'pairs.jsonl').read_text(encoding='utf-8').splitlines()]
        if report['metadata'].get('index_unchanged') is False or report['summary']['attempts']!=len(rows):
            parser.error('Snapshot or row count mismatch')
        suite = policy_suite(rows)
        for delay in suite['delays']:
            if 'policies' not in delay:
                continue
            delay['error_cost_comparison'] = {name: {'effects': policy['prediction_effects'],
                'behavior': delay['policy_overview'][name]} for name, policy in delay['policies'].items()}
            delay.pop('policies')
            delay.pop('policy_overview')
        output['runs'].append({'run_number': index, **suite})
        print(f'Completed scoring {index}', flush=True)
    output['limits'] = ('Existing retrospective development data. Same fixed gate suite and timing assumptions, no model changes. '
                        'fast_only and all_enhanced are paired forecasting baselines; ungated still uses Q90 budget routing. '
                        'Overrun counts in behavior concern incremental budget, not full estimated workflow cost. '
                        'No measured end-to-end cost available; no total-production savings claim. '
                        'Grouping is per metric, not independent resource weighting. No raw IDs/paths/dates exported; internal review required.')
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(output, stream, indent=2, allow_nan=False)


if __name__ == '__main__':
    main()
