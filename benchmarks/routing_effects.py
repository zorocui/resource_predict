"""Read-only scoring of selected policies; never used in admission decisions."""
import json
import math

METRICS = {'cpu', 'memory', 'disk', 'cpu_request', 'cpu_limit', 'memory_request', 'memory_limit'}


def score_effects(records, mask, action):
    groups = {}
    costs = {name: 0. for name in ('fast_only', 'all_enhanced', 'policy')}
    unavailable = 0
    for row, selected in zip(records, mask):
        try:
            before, after = row['baseline'], row['enhanced']
            values = [float(block[key]) for block in (before, after)
                      for key in ('rmse', 'mae', 'estimated_workflow_wall_seconds')]
            if not all(math.isfinite(v) and v >= 0 for v in values):
                raise ValueError('Invalid score')
        except (KeyError, ValueError, TypeError):
            unavailable += 1
            continue
        try:
            identity = json.loads(row['series_id'])
            metric = identity[2] if isinstance(identity, list) and len(identity)==3 and identity[2] in METRICS else 'other'
        except (ValueError, TypeError):
            metric = 'other'
        group = groups.setdefault(metric, {'count': 0, **{f'{name}_{error}_sum': 0.
            for name in ('fast_only', 'all_enhanced', 'policy') for error in ('rmse', 'mae')}})
        group['count'] += 1
        chosen = after if selected and action=='active' else before
        for name, block in (('fast_only', before), ('all_enhanced', after), ('policy', chosen)):
            for error in ('rmse', 'mae'):
                group[f'{name}_{error}_sum'] += float(block[error])
        base_cost, enhanced_cost = values[2], values[5]
        costs['fast_only'] += base_cost
        costs['all_enhanced'] += enhanced_cost
        costs['policy'] += enhanced_cost if selected and action=='active' else base_cost
        if selected and action=='shadow':
            costs['policy'] += max(0., enhanced_cost-base_cost)
    return {'groups': groups, 'estimated_cost_seconds': costs, 'unavailable_rows': unavailable}


def summarize_effects(ledgers):
    groups, costs, unavailable = {}, {n: 0. for n in ('fast_only', 'all_enhanced', 'policy')}, 0
    for ledger in ledgers:
        effects = ledger.get('prediction_effects')
        if effects is None:
            unavailable += ledger['candidates']
            continue
        unavailable += effects['unavailable_rows']
        for name, value in effects['estimated_cost_seconds'].items():
            costs[name] += value
        for metric, row in effects['groups'].items():
            target = groups.setdefault(metric, {k: 0 for k in row})
            for key, value in row.items():
                target[key] += value
    metrics = []
    for metric, group in sorted(groups.items()):
        row = {'metric': metric, 'scored_windows': group['count']}
        for error in ('rmse', 'mae'):
            for name in costs:
                row[f'{name}_mean_window_{error}'] = group[f'{name}_{error}_sum']/group['count']
            for ref in ('fast_only', 'all_enhanced'):
                base = group[f'{ref}_{error}_sum']
                row[f'{error}_increase_pct_vs_{ref}'] = 100*(group[f'policy_{error}_sum']-base)/base if base else None
        metrics.append(row)
    return {'status': 'partial' if unavailable else ('ok' if metrics else 'unavailable'),
            'unavailable_rows': unavailable, 'metrics': metrics, 'estimated_cost_seconds': costs,
            'estimated_cost_saving_pct_vs_all_enhanced': 100*(costs['all_enhanced']-costs['policy'])/costs['all_enhanced'] if costs['all_enhanced'] else None,
            'measured_end_to_end_cost_seconds': None,
            'limits': 'Equal-window mean RMSE/MAE, not pooled pointwise RMSE; same successful rows per comparison. '
            'Metric groups may still have differing baselines; other group is not comparable without unit checks. '
            'Full workflow costs are stage-composed serial estimates, not measured end-to-end time or production CPU. '
            'Includes baseline and nonnegative shadow increment; excludes route training/inference, original feature/I-O costs. '
            'Old budget ledger uses nonnegative marginal proxy, so differs when negative cost deltas exist. '
            'Positive error increase is worse; null percentage when reference error is zero. Partial data cannot support whole-run savings.'}
