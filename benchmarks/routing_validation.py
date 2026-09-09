"""Repeated offline ranking validation using existing private replay records."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from benchmarks.routing_share import FEATURES, resource_group
from benchmarks.routing_pilot import METHODS


def predict_gain(train, test):
    def arrays(rows):
        return (np.array([[r['features'].get(f, 0.) for f in FEATURES] for r in rows]),
                np.array([r['delta_rmse']/max(abs(r['features']['mean']), .01) for r in rows]))
    x, y = arrays(train)
    xt, yt = arrays(test)
    mean, scale = x.mean(0), x.std(0)
    scale[scale < 1e-12] = 1
    x, xt = (x-mean)/scale, (xt-mean)/scale
    weights = np.linalg.solve(x.T@x+np.eye(len(FEATURES)), x.T@(y-y.mean()))
    return xt@weights+y.mean(), yt, float(y.mean())


def selection_stats(gain, costs, mask, groups, seed):
    n, k = len(gain), int(mask.sum())
    if not k:
        return {"selected": 0, "status": "no_selection"}
    fraction = k/n
    # Fixed fitted policy; bootstrap independent resource groups, not individual rows.
    contrasts = np.array([np.mean((mask[groups == g]/fraction-1)*gain[groups == g])
                          for g in sorted(set(groups))])
    interval = None
    if len(contrasts) >= 5:
        rng = np.random.default_rng(seed)
        samples = rng.choice(contrasts, size=(500, len(contrasts)), replace=True).mean(1)
        interval = [float(v) for v in np.quantile(samples, [.025, .975])]
    return {"status": "ok", "selected": k,
            "mean_gain": float(gain[mask].mean()), "sum_gain": float(gain[mask].sum()),
            "random_equal_count_expected_mean_gain": float(gain.mean()),
            "observed_extra_wall_seconds": float(costs[mask].sum()),
            "random_equal_count_expected_extra_wall_seconds": float(fraction*costs.sum()),
            "resource_macro_utility_advantage": float(contrasts.mean()),
            "conditional_resource_bootstrap_95_interval": interval}


def evaluate(train, test, seed):
    result = {"status": "insufficient_data", "train_rows": len(train), "test_rows": len(test),
              "test_resources": len({resource_group(r) for r in test})}
    if len(train) < 30 or len(test) < 10:
        return result
    predictions, gain, constant = predict_gain(train, test)
    costs = np.array([r['delta_wall_seconds'] for r in test])
    groups = np.array([resource_group(r) for r in test])
    # Randomized tie-break avoids dependence on file/resource ordering.
    rng = np.random.default_rng(seed)
    order = np.lexsort((rng.random(len(test)), -predictions))
    curves = []
    for fraction in (.1, .25, .5, .75):
        mask = np.zeros(len(test), dtype=bool)
        mask[order[:max(1, int(len(test)*fraction))]] = True
        curves.append({"requested_fraction": fraction,
                       **selection_stats(gain, costs, mask, groups, seed)})
    rule = {"status": "unavailable_missing_archived_rule"}
    if all(isinstance(r.get('auto_rule_run'), bool) for r in test):
        mask = np.array([r['auto_rule_run'] for r in test])
        rule = selection_stats(gain, costs, mask, groups, seed)
    result.update(status="ok", ridge_mae=float(np.mean(abs(predictions-gain))),
                  constant_mae=float(np.mean(abs(constant-gain))),
                  selection_curve=curves, auto_rule=rule,
                  all_enhanced_sum_gain=float(gain.sum()),
                  all_enhanced_extra_wall_seconds=float(costs.sum()),
                  negative_measured_cost_rows=int((costs < 0).sum()))
    return result


def analyze(rows, evaluator=None):
    evaluator = evaluator or evaluate
    complete = [r for r in rows if r.get('status') == 'paired' and all(
        r.get('methods', {}).get(m, {}).get(p, {}).get('status') == 'ok'
        for m in METHODS for p in ('validation', 'test'))]
    for row in complete:
        numeric = [row['delta_rmse'], row['delta_wall_seconds']]+list(row['features'].values())
        if not np.isfinite(np.asarray(numeric, dtype=float)).all():
            raise ValueError('Nonfinite replay values')
    result = {"schema": "routing-validation-v1", "input_rows": len(rows),
              "eligible_rows": len(complete), "excluded_rows": len(rows)-len(complete),
              "resource_splits": [], "time_splits": [],
              "limits": "Fixed Ridge alpha=1; training-only standardization. Equal-count, not equal-budget comparisons. Costs are retrospective signed workflow estimates. Bootstrap is conditional on fitted policy and fixed selection, resource-macro weighted, not retraining uncertainty. Splits reuse observations: do not pool as independent experiments. No identifiers, paths, hashes, dates or raw errors exported. Internal review required."}
    groups = sorted({resource_group(r) for r in complete})
    if len(groups) >= 10:
        for seed in range(10):
            shuffled = np.random.default_rng(seed).permutation(groups)
            held = set(shuffled[int(.7*len(groups)):])
            train = [r for r in complete if resource_group(r) not in held]
            test = [r for r in complete if resource_group(r) in held]
            result['resource_splits'].append({"split": seed, **evaluator(train, test, seed)})
    origins = sorted({pd.Timestamp(r['origin_time']) for r in complete})
    if len(origins) >= 8:
        edges = [int(len(origins)*v) for v in (.5, .65, .8)] + [len(origins)]
        for split, (start, end) in enumerate(zip(edges, edges[1:])):
            selected_origins = set(origins[start:end])
            test = [r for r in complete if pd.Timestamp(r['origin_time']) in selected_origins]
            known_by = min(pd.Timestamp(r['feature_end']) for r in test)
            train = [r for r in complete if pd.Timestamp(r['test_end']) < known_by]
            result['time_splits'].append({"split": split, **evaluator(train, test, split)})
    result['status'] = 'ok' if any(s['status']=='ok' for key in ('resource_splits', 'time_splits')
                                  for s in result[key]) else 'insufficient_data'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; choose another filename')
    report = json.loads((args.run_dir/'report.json').read_text(encoding='utf-8'))
    if report['metadata'].get('index_unchanged') is False:
        parser.error('Snapshot changed; cannot validate this run')
    rows = [json.loads(line) for line in (args.run_dir/'pairs.jsonl').read_text(encoding='utf-8').splitlines()]
    if report['summary']['attempts'] != len(rows):
        parser.error('Report and pair counts disagree')
    result = analyze(rows)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print('Offline aggregate analysis complete. Review before sharing.')


if __name__ == '__main__':
    main()
