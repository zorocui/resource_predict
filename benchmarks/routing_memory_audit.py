"""Frozen-model diagnostic of missed benefits and simple memory-first controls."""
import argparse
import json
from pathlib import Path

import numpy as np

from benchmarks.routing_budget import allocate
from benchmarks.routing_effects import METRICS, score_effects, summarize_effects
from benchmarks.routing_gate_replay import analyze_gate
from benchmarks.routing_share import resource_group


def metric_name(row):
    try:
        parts = json.loads(row['series_id'])
        return parts[2] if isinstance(parts, list) and len(parts)==3 and parts[2] in METRICS else 'other'
    except (TypeError, ValueError):
        return 'other'


def audit_batches(batches):
    names = ('current_route', 'memory_first_positive', 'memory_first_blind', 'random_blind')
    ledgers = {name: [] for name in names}
    misses, resource_gains = {}, {}
    for batch in batches:
        rows, prediction, cost, budget = batch['records'], batch['prediction'], batch['cost'], batch['budget']
        metrics = np.array([metric_name(r) for r in rows])
        memory = np.array([m.startswith('memory') for m in metrics])
        ties = np.random.default_rng(42).random(len(rows))
        orders = {'current_route': np.lexsort((ties, -prediction/cost)),
                  'memory_first_positive': np.lexsort((ties, -prediction/cost, ~memory)),
                  'memory_first_blind': np.lexsort((ties, ~memory)),
                  'random_blind': np.argsort(ties)}
        masks = {}
        for name, order in orders.items():
            if name in ('current_route', 'memory_first_positive'):
                order = order[prediction[order] > 0]
            mask = allocate(order, cost, budget)
            masks[name] = mask
            actual = float(batch['actual'][mask].sum())
            ledgers[name].append({'prediction_effects': score_effects(rows, mask, 'active'),
                'budget': budget, 'estimated_spend': float(cost[mask].sum()), 'observed_spend': actual,
                'overrun': max(0., actual-budget), 'selected': int(mask.sum())})
        for i, row in enumerate(rows):
            metric = metrics[i]
            group = misses.setdefault(metric, {'candidates': 0, 'selected': 0, 'positive_actual': 0,
                'missed_nonpositive_prediction': 0, 'missed_cost_above_batch_budget': 0,
                'missed_ranking_or_remaining_budget': 0, 'selected_negative_actual': 0,
                'positive_delta_rmse_sum': 0., 'captured_positive_delta_rmse_sum': 0.,
                'missed_nonpositive_delta_rmse_sum': 0., 'missed_expensive_delta_rmse_sum': 0.,
                'missed_ranking_delta_rmse_sum': 0., 'selected_negative_delta_rmse_sum': 0.,
                'predicted_cost_sum': 0., 'actual_cost_sum': 0.})
            chosen, gain = bool(masks['current_route'][i]), float(row['delta_rmse'])
            group['candidates'] += 1
            group['selected'] += int(chosen)
            group['predicted_cost_sum'] += float(cost[i])
            group['actual_cost_sum'] += float(batch['actual'][i])
            if gain > 1e-12:
                group['positive_actual'] += 1
                group['positive_delta_rmse_sum'] += gain
                rg = resource_gains.setdefault(metric, {})
                key = resource_group(row)
                rg[key] = rg.get(key, 0.)+gain
                if chosen:
                    group['captured_positive_delta_rmse_sum'] += gain
                elif prediction[i] <= 0:
                    group['missed_nonpositive_prediction'] += 1
                    group['missed_nonpositive_delta_rmse_sum'] += gain
                elif cost[i] > budget:
                    group['missed_cost_above_batch_budget'] += 1
                    group['missed_expensive_delta_rmse_sum'] += gain
                else:
                    group['missed_ranking_or_remaining_budget'] += 1
                    group['missed_ranking_delta_rmse_sum'] += gain
            elif gain < -1e-12 and chosen:
                group['selected_negative_actual'] += 1
                group['selected_negative_delta_rmse_sum'] += gain
    for metric, group in misses.items():
        gains = sorted(resource_gains.get(metric, {}).values(), reverse=True)
        total = sum(gains)
        group['resources_with_positive_gain'] = len(gains)
        group['top_resource_positive_gain_share'] = gains[0]/total if total else None
        group['top_three_resources_positive_gain_share'] = sum(gains[:3])/total if total else None
        group['captured_positive_gain_fraction'] = group['captured_positive_delta_rmse_sum']/total if total else None
    return {'missed_benefits_by_metric': misses, 'controls': {name: {
        'effects': summarize_effects(ledger), 'selected': sum(r['selected'] for r in ledger),
        'nominal_budget_sum': sum(r['budget'] for r in ledger),
        'predicted_incremental_spend': sum(r['estimated_spend'] for r in ledger),
        'observed_incremental_spend': sum(r['observed_spend'] for r in ledger),
        'overrun_batches': sum(r['overrun'] > 1e-9 for r in ledger)} for name, ledger in ledgers.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    result = {'schema': 'routing-memory-audit-v1', 'runs': [],
        'limits': 'Diagnostic on development data, not an independent validation or selected optimal policy. '
        'Common frozen models and per-origin 50% Q90 nominal budgets, no pause rules. '
        'memory_first_positive changes priority but keeps gain-sign filter; memory_first_blind drops filter and uses fixed random order within metric category. '
        'random_blind is a single seeded control, no significance claim. True gains used only after selection for diagnosis. '
        'Miss reasons are mutually exclusive in order: predicted nonpositive, predicted cost above budget, remaining-budget/ranking. '
        'Positive-benefit capture excludes negative effects; read signed errors and negative counts too. '
        'Top-resource shares aggregate per metric, no identity exported; tiny groups may still require internal suppression. '
        'All-success cohort, metric units and windows as archived; cost is stage estimate, not measured end-to-end. '
        'Memory priority does not prove memory inherently deserves more compute. Review before sharing.'}
    for index, directory in enumerate(args.run_dir, 1):
        report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
        rows = [json.loads(line) for line in (directory/'pairs.jsonl').read_text(encoding='utf-8').splitlines()]
        if report['metadata'].get('index_unchanged') is False or report['summary']['attempts']!=len(rows):
            parser.error('Snapshot or row count mismatch')
        result['runs'].append({'run_number': index, **analyze_gate(rows, compare_feedback=True, diagnostic=audit_batches)})
        print(f'Completed diagnosis {index}', flush=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)


if __name__ == '__main__':
    main()
