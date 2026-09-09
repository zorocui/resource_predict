"""Paired feedback-timing sensitivity with identical frozen models and budgets."""
import argparse
import json
from pathlib import Path

from benchmarks.routing_gate_replay import analyze_gate


def compare(rows):
    return {'delays': [analyze_gate(rows, feedback_delay_hours=d, compare_feedback=True) for d in (0, 4, 24)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    result = {'schema': 'routing-feedback-v1', 'runs': [],
              'limits': 'Hypothetical timing only: old records have no actual completion timestamps. '
              'Cost delivered once before next scorable batch; gain waits for selected test_end plus 0/4/24 hours and feature cutoff. '
              'Warm-start training always uses zero extra historical delay, so all arms share frozen models, budgets and initial choices. '
              'Added delay begins at evaluation, not a reconstruction of historically delayed training. '
              'Compared arms: no gate, coupled cost/gain gate, next-batch cost gate. Healthy cost cannot recover or reset gain streak. '
              'No early interruption of current job; no hard budget guarantee. Unscorable batches excluded, all-success conditional cohort. '
              'Exact-origin batches not validated scheduler IDs; relative gaps included without dates. '
              'All costs remain stage-composed marginal proxies, not actual completion time or CPU. '
              'No private IDs/paths/exact dates exported; internal review required.'}
    for index, directory in enumerate(args.run_dir):
        report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
        rows = [json.loads(line) for line in (directory/'pairs.jsonl').read_text(encoding='utf-8').splitlines()]
        if report['metadata'].get('index_unchanged') is False or report['summary']['attempts'] != len(rows):
            parser.error('Snapshot or row count mismatch')
        result['runs'].append({'run_number': index+1, **compare(rows)})
        print(f'Completed comparison {index+1}', flush=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)


if __name__ == '__main__':
    main()
