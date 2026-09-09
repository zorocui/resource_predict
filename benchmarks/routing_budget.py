"""Offline predicted-budget allocation; test costs never influence selection."""
import argparse
import json
from pathlib import Path
import time

import numpy as np

from benchmarks.routing_share import FEATURES, distribution, resource_group
from benchmarks.routing_validation import analyze, predict_gain


def predict_cost(train, test):
    x = np.array([[r['features'].get(f, 0.) for f in FEATURES] for r in train])
    xt = np.array([[r['features'].get(f, 0.) for f in FEATURES] for r in test])
    # Nonnegative marginal-cost proxy; no rows removed based on test cost.
    costs = np.maximum([r['delta_wall_seconds'] for r in train], 1e-6)
    y = np.log(costs)
    mean, scale = x.mean(0), x.std(0)
    scale[scale < 1e-12] = 1
    x, xt = (x-mean)/scale, (xt-mean)/scale
    w = np.linalg.solve(x.T@x+np.eye(len(FEATURES)), x.T@(y-y.mean()))
    return np.exp(np.clip(xt@w+y.mean(), y.min(), y.max())), float(np.median(costs))


def allocate(order, predicted_cost, budget):
    mask = np.zeros(len(predicted_cost), dtype=bool)
    spent = 0.
    for index in order:
        if spent + predicted_cost[index] <= budget:
            mask[index] = True
            spent += predicted_cost[index]
    return mask


def score(mask, gain, estimated, actual, budget):
    used = float(actual[mask].sum())
    planned = float(estimated[mask].sum())
    return {'selected': int(mask.sum()), 'sum_normalized_gain': float(gain[mask].sum()),
            'predicted_spend_seconds': planned, 'observed_spend_seconds': used,
            'predicted_unused_seconds': max(0., budget-planned),
            'observed_unused_seconds': max(0., budget-used),
            'observed_overrun_seconds': max(0., used-budget),
            'observed_overrun_fraction': max(0., used-budget)/budget if budget else 0.}


def evaluate_budget(train, test, seed):
    result = {'status': 'insufficient_data', 'train_rows': len(train), 'test_rows': len(test),
              'test_resources': len({resource_group(r) for r in test})}
    if len(train) < 30 or len(test) < 10:
        return result
    start = time.perf_counter()
    predicted_gain, gain, _ = predict_gain(train, test)
    costs, constant_cost = predict_cost(train, test)
    model_seconds = time.perf_counter()-start
    actual = np.maximum([r['delta_wall_seconds'] for r in test], 0.)
    rng = np.random.default_rng(seed)
    ties = rng.random(len(test))
    orders = {
        'predicted_gain': np.lexsort((ties, -predicted_gain)),
        'predicted_gain_per_cost': np.lexsort((ties, -predicted_gain/costs))}
    orders = {name: order[predicted_gain[order] > 0] for name, order in orders.items()}
    curves = []
    allocation_start = time.perf_counter()
    for fraction in (.1, .25, .5, .75):
        budget = float(fraction*costs.sum())
        policies = {name: score(allocate(order, costs, budget), gain, costs, actual, budget)
                    for name, order in orders.items()}
        for name, positive_only in (('random', False), ('random_positive_gain', True)):
            trials = []
            for trial in range(20):
                order = np.random.default_rng(seed*1000+trial).permutation(len(test))
                if positive_only:
                    order = order[predicted_gain[order] > 0]
                trials.append(score(allocate(order, costs, budget), gain, costs, actual, budget))
            policies[name] = {'trials': len(trials), 'overrun_trials': sum(t['observed_overrun_seconds'] > 0 for t in trials),
                              'metrics': {key: distribution(t[key] for t in trials) for key in trials[0]}}
        curves.append({'budget_fraction': fraction, 'budget_seconds': budget, 'policies': policies})
    result.update(status='ok', cost_mae_seconds=float(np.mean(abs(costs-actual))),
                  constant_cost_mae_seconds=float(np.mean(abs(constant_cost-actual))),
                  predicted_all_cost_seconds=float(costs.sum()), observed_all_cost_seconds=float(actual.sum()),
                  negative_original_cost_rows=sum(r['delta_wall_seconds'] < 0 for r in test),
                  all_enhanced_sum_normalized_gain=float(gain.sum()),
                  prediction_fit_and_inference_seconds=model_seconds,
                  all_policy_analysis_seconds=time.perf_counter()-allocation_start,
                  budget_curve=curves)
    return result


def budget_analysis(rows):
    result = analyze(rows, evaluator=evaluate_budget)
    result['schema'] = 'routing-budget-v1'
    result['limits'] = (
        'Development replay, not final validation. Same offered predicted budget, not guaranteed equal actual spend. '
        'Budget fractions multiply predicted all-test cost; never actual test cost. Cost model uses training-only '
        'standardized log Ridge alpha=1, predictions clipped to training log-cost range. Actual cost proxy is '
        'max(delta_wall_seconds,0); negative values counted, not dropped. Gain-ranked policies abstain on nonpositive '
        'predicted gains; random_positive_gain controls for this. First-fit scan skips unaffordable items; not an optimal '
        'knapsack solver. Twenty random permutations are algorithmic variability, not confidence intervals. '
        'Model timing is this analysis host, excludes original feature extraction and is not deducted from candidate budget. '
        'Repeated splits reuse observations; no significance claim or causal service/cost claim. '
        'No resource IDs, dates, paths or raw errors exported. Review internally before sharing.')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; choose a new filename')
    report = json.loads((args.run_dir/'report.json').read_text(encoding='utf-8'))
    if report['metadata'].get('index_unchanged') is False:
        parser.error('Snapshot changed; cannot validate this run')
    rows = [json.loads(line) for line in (args.run_dir/'pairs.jsonl').read_text(encoding='utf-8').splitlines()]
    if report['summary']['attempts'] != len(rows):
        parser.error('Report and pair counts disagree')
    result = budget_analysis(rows)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print('Budget aggregate written. Review internally before sharing.')


if __name__ == '__main__':
    main()
