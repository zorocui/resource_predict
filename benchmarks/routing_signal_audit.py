"""Training-history persistence and gain-scale/cost-ranking diagnostic."""
import argparse
import json
from pathlib import Path

import numpy as np

from benchmarks.routing_budget import allocate
from benchmarks.routing_effects import score_effects, summarize_effects
from benchmarks.routing_gate_replay import analyze_gate
from benchmarks.routing_memory_audit import metric_name
from benchmarks.routing_share import resource_group


def history_signals(train, batches):
    train_groups, test_groups = {}, {}
    for records, target in ((train, train_groups), ([r for b in batches for r in b['records']], test_groups)):
        for row in records:
            key = (metric_name(row), resource_group(row))
            target.setdefault(key, []).append(float(row['delta_rmse']))
    output = []
    for metric in sorted({k[0] for k in test_groups}):
        keys = [k for k in test_groups if k[0]==metric]
        # Retrospective group characterization only; never passed to allocation.
        top_test = sorted([k for k in keys if any(v > 0 for v in test_groups[k])],
                          key=lambda k: sum(max(0., v) for v in test_groups[k]), reverse=True)[:3]
        train_keys = [k for k in train_groups if k[0]==metric]
        top_train = sorted(train_keys, key=lambda k: np.mean(train_groups[k]), reverse=True)[:3]
        positive_test_total = sum(max(0., v) for k in keys for v in test_groups[k])
        captured = sum(max(0., v) for k in top_train for v in test_groups.get(k, []))
        comparable = [k for k in keys if k in train_groups]
        summary = {'metric': metric, 'test_resources': len(keys), 'history_matched_resources': len(comparable),
                   'top_test_resources_with_history': sum(k in train_groups for k in top_test),
                   'top_test_resources_with_positive_train_mean': int(sum(k in train_groups and np.mean(train_groups[k]) > 0 for k in top_test)),
                   'top_three_overlap_count': len(set(top_train)&set(top_test)),
                   'train_top_three_test_positive_gain_share': captured/positive_test_total if positive_test_total else None,
                   'resource_mean_sign_agreement': float(np.mean([np.sign(np.mean(train_groups[k]))==np.sign(np.mean(test_groups[k])) for k in comparable])) if comparable else None,
                   'top_test_historical_profiles': []}
        for k in top_test:
            values = train_groups.get(k, [])
            summary['top_test_historical_profiles'].append({
                'test_rank': len(summary['top_test_historical_profiles'])+1,
                'train_windows': len(values), 'train_mean_delta_rmse': float(np.mean(values)) if values else None,
                'train_positive_fraction': float(np.mean(np.array(values)>0)) if values else None,
                'test_windows': len(test_groups[k]), 'test_mean_delta_rmse': float(np.mean(test_groups[k]))})
        output.append(summary)
    return output


def diagnose(train, batches):
    names = ('normalized_gain', 'normalized_gain_per_cost', 'restored_raw_gain', 'restored_raw_gain_per_cost')
    ledgers = {name: [] for name in names}
    disagreement = {key: 0 for key in ('cost_changes_normalized_selection', 'scale_changes_selection', 'candidates')}
    for b in batches:
        rows, normalized, costs = b['records'], b['prediction'], b['cost']
        raw = normalized*np.array([max(abs(r['features']['mean']), .01) for r in rows])
        scores = dict(zip(names, (normalized, normalized/costs, raw, raw/costs)))
        ties = np.random.default_rng(42).random(len(rows))
        masks = {}
        for name, score in scores.items():
            order = np.lexsort((ties, -score))
            masks[name] = allocate(order[normalized[order]>0], costs, b['budget'])
            ledgers[name].append({'prediction_effects': score_effects(rows, masks[name], 'active'),
                                 'selected': int(masks[name].sum()), 'budget': b['budget'],
                                 'predicted_cost': float(costs[masks[name]].sum()),
                                 'actual_cost': float(b['actual'][masks[name]].sum())})
        disagreement['candidates'] += len(rows)
        disagreement['cost_changes_normalized_selection'] += int(np.count_nonzero(masks[names[0]] != masks[names[1]]))
        disagreement['scale_changes_selection'] += int(np.count_nonzero(masks[names[1]] != masks[names[3]]))
    return {'history_persistence': history_signals(train, batches), 'selection_disagreement': disagreement,
            'ranking_controls': {name: {'effects': summarize_effects(rows), 'selected': sum(r['selected'] for r in rows),
                'predicted_cost': sum(r['predicted_cost'] for r in rows), 'actual_incremental_cost': sum(r['actual_cost'] for r in rows),
                'overrun_batches': sum(r['actual_cost']>r['budget']+1e-9 for r in rows)} for name, rows in ledgers.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    result = {'schema': 'routing-signal-audit-v1', 'runs': [],
              'limits': 'Development diagnostics only. No refit or parameter search; restored_raw_gain multiplies the same normalized forecast '
              'by history-only scale, it is not a separately trained raw-target model. Four rankings share sign filter, cost estimates and budgets. '
              'Raw scales across metrics are not economically comparable; read per-metric effects, do not declare global optimality. '
              'Training top-three uses signed mean; test top-three uses positive sum and is retrospective description only, never admission input. '
              'No IDs/dates/paths, but top-three profiles describe small groups and require internal review. '
              'All-success rows, fixed 70% origin split, frozen historical models; serial stage cost estimates, not CPU or end-to-end measurements. '
              'This already-inspected dataset is not independent validation.'}
    for i, directory in enumerate(args.run_dir, 1):
        report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
        rows = [json.loads(line) for line in (directory/'pairs.jsonl').read_text(encoding='utf-8').splitlines()]
        if report['metadata'].get('index_unchanged') is False or report['summary']['attempts']!=len(rows):
            parser.error('Snapshot or record count mismatch')
        result['runs'].append({'run_number': i, **analyze_gate(rows, compare_feedback=True, history_diagnostic=diagnose)})
        print(f'Completed signal diagnosis {i}', flush=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)


if __name__ == '__main__':
    main()
