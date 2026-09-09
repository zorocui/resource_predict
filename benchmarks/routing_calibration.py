"""One-shot offline cost-calibration suite with a private-data-free aggregate report."""
import argparse
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from benchmarks.routing_budget import allocate, predict_cost, score
from benchmarks.routing_share import distribution, resource_group
from benchmarks.routing_validation import analyze, predict_gain

MODELS = ('constant_median', 'constant_mean', 'log_ridge', 'mean_calibrated', 'conservative_q90')
POLICIES = ('random', 'random_positive_gain', 'predicted_gain', 'predicted_gain_per_cost')
FRACTIONS = (.1, .25, .5, .75)
TRIALS = 20


def calibration_split(train):
    """Last 30% of outer-training origins calibrate; purge unavailable fitting labels."""
    origins = sorted({pd.Timestamp(r['origin_time']) for r in train})
    if len(origins) < 5:
        return [], []
    cutoff = origins[int(.7*len(origins))]
    calibration = [r for r in train if pd.Timestamp(r['origin_time']) >= cutoff]
    known_by = min(pd.Timestamp(r['feature_end']) for r in calibration)
    fit = [r for r in train if pd.Timestamp(r['test_end']) < known_by]
    return fit, calibration


def calibrated_costs(fit, calibration, test):
    predictions, _ = predict_cost(fit, calibration + test)
    cal_pred, test_pred = predictions[:len(calibration)], predictions[len(calibration):]
    fit_actual = np.maximum([r['delta_wall_seconds'] for r in fit], 0.)
    cal_actual = np.maximum([r['delta_wall_seconds'] for r in calibration], 0.)
    mean_factor = max(1e-6, float(cal_actual.sum()/cal_pred.sum()))
    conservative_factor = max(1., mean_factor, float(np.quantile(cal_actual/cal_pred, .9)))
    mean_cost = max(float(fit_actual.mean()), 1e-6)
    models = {'constant_median': np.full(len(test), max(float(np.median(fit_actual)), 1e-6)),
              'constant_mean': np.full(len(test), mean_cost), 'log_ridge': test_pred,
              'mean_calibrated': test_pred*mean_factor,
              'conservative_q90': test_pred*conservative_factor}
    return models, mean_cost, {'mean_factor': mean_factor, 'conservative_factor': conservative_factor}


def policy_metrics(order, predicted_cost, budget, gain, actual):
    mask = allocate(order, predicted_cost, budget)
    metrics = score(mask, gain, predicted_cost, actual, budget)
    # Numerical tolerance only; not an operational overrun allowance.
    metrics['overrun_rate'] = float(metrics['observed_overrun_seconds'] > max(1e-9, budget*1e-10))
    metrics['actual_budget_utilization'] = metrics['observed_spend_seconds']/budget
    metrics['gain_per_budget_second'] = metrics['sum_normalized_gain']/budget
    metrics['positive_gain'] = float(metrics['sum_normalized_gain'] > 1e-12)
    return metrics


def evaluate_calibration(train, test, seed):
    fit, calibration = calibration_split(train)
    result = {'status': 'insufficient_data', 'train_rows': len(train), 'test_rows': len(test),
              'cost_fit_rows': len(fit), 'cost_calibration_rows': len(calibration),
              'purged_train_rows': len(train)-len(fit)-len(calibration),
              'test_resources': len({resource_group(r) for r in test})}
    if min(len(fit), len(train)) < 30 or min(len(calibration), len(test)) < 10:
        result['reason'] = 'nested_fit_or_calibration_history_insufficient'
        return result
    start = time.perf_counter()
    # Gain model stays on all outer training data; cost variants all use the same cost fit subset.
    predicted_gain, gain, _ = predict_gain(train, test)
    models, reference_cost, factors = calibrated_costs(fit, calibration, test)
    actual = np.maximum([r['delta_wall_seconds'] for r in test], 0.)
    ties = np.random.default_rng(seed).random(len(test))
    positive = predicted_gain > 0
    random_orders = [np.random.default_rng(seed*1000+i).permutation(len(test)) for i in range(TRIALS)]
    model_reports = []
    for model in MODELS:
        prediction = models[model]
        orders = {'predicted_gain': np.lexsort((ties, -predicted_gain)),
                  'predicted_gain_per_cost': np.lexsort((ties, -predicted_gain/prediction))}
        curves = []
        for fraction in FRACTIONS:
            # This common budget is independent of calibration and every test cost/label.
            budget = float(reference_cost*len(test)*fraction)
            policies = {name: policy_metrics(order[positive[order]], prediction, budget, gain, actual)
                        for name, order in orders.items()}
            for name in ('random', 'random_positive_gain'):
                trials = [policy_metrics(order if name == 'random' else order[positive[order]],
                                         prediction, budget, gain, actual) for order in random_orders]
                policies[name] = {key: float(np.mean([t[key] for t in trials])) for key in trials[0]}
                policies[name]['random_gain_p05'] = float(np.quantile([t['sum_normalized_gain'] for t in trials], .05))
                policies[name]['random_gain_p95'] = float(np.quantile([t['sum_normalized_gain'] for t in trials], .95))
            curves.append({'budget_fraction': fraction, 'budget_seconds': budget, 'policies': policies})
        model_reports.append({'model': model, 'mae_seconds': float(np.mean(abs(prediction-actual))),
                              'total_prediction_bias_seconds': float(prediction.sum()-actual.sum()),
                              'predicted_to_actual_total_ratio': float(prediction.sum()/actual.sum()) if actual.sum() else None,
                              'pointwise_underprediction_rate': float(np.mean(prediction < actual)),
                              'budget_curve': curves})
    result.update(status='ok', calibration_factors=factors, budget_reference_mean_seconds=reference_cost,
                  negative_original_test_cost_rows=sum(r['delta_wall_seconds'] < 0 for r in test),
                  negative_original_fit_cost_rows=sum(r['delta_wall_seconds'] < 0 for r in fit),
                  negative_original_calibration_cost_rows=sum(r['delta_wall_seconds'] < 0 for r in calibration),
                  all_enhanced_sum_normalized_gain=float(gain.sum()),
                  all_enhanced_actual_cost_seconds=float(actual.sum()),
                  suite_analysis_wall_seconds=time.perf_counter()-start, models=model_reports)
    return result


def overview(result):
    """Descriptive split distributions, not pooled independent trials or confidence intervals."""
    tables = []
    for kind in ('resource_splits', 'time_splits'):
        splits = [s for s in result[kind] if s['status'] == 'ok']
        for model in MODELS:
            for fraction in FRACTIONS:
                curves = [next(c for m in s['models'] if m['model'] == model
                               for c in m['budget_curve'] if c['budget_fraction'] == fraction) for s in splits]
                for policy in POLICIES:
                    values = [c['policies'][policy] for c in curves]
                    if not values:
                        continue
                    tables.append({'split_kind': kind, 'model': model, 'budget_fraction': fraction,
                                   'policy': policy, 'valid_splits': len(values),
                                   'overrun_rate': distribution(v['overrun_rate'] for v in values),
                                   'overrun_fraction': distribution(v['observed_overrun_fraction'] for v in values),
                                   'budget_utilization': distribution(v['actual_budget_utilization'] for v in values),
                                   'normalized_gain': distribution(v['sum_normalized_gain'] for v in values),
                                   'gain_per_budget_second': distribution(v['gain_per_budget_second'] for v in values),
                                   'splits_above_random_mean': sum(c['policies'][policy]['sum_normalized_gain'] >
                                       c['policies']['random']['sum_normalized_gain'] + 1e-9 for c in curves),
                                   'splits_above_positive_random_mean': sum(c['policies'][policy]['sum_normalized_gain'] >
                                       c['policies']['random_positive_gain']['sum_normalized_gain'] + 1e-9 for c in curves)})
    return tables


def calibration_analysis(rows):
    result = analyze(rows, evaluator=evaluate_calibration)
    result['schema'] = 'routing-calibration-v1'
    result['protocol'] = {'cost_models': list(MODELS), 'policies': list(POLICIES),
                          'budget_fractions': list(FRACTIONS), 'random_trials': TRIALS,
                          'inner_calibration_fraction': .3, 'conservative_quantile': .9,
                          'budget_reference': 'cost_fit_mean_times_test_count',
                          'gain_model': 'unchanged_outer_train_ridge_alpha_1',
                          'cost_model': 'inner_fit_log_ridge_alpha_1'}
    result['overview'] = overview(result)
    paired = [r for r in rows if r.get('status') == 'paired']
    result['input_profile'] = {
        'resources': len({resource_group(r) for r in rows}),
        'series': len({r['series_id'] for r in rows}),
        'failed_rows': sum(r.get('status') == 'failed' for r in rows),
        'skipped_rows': sum(r.get('status') == 'skipped' for r in rows),
        'feature_history_days': distribution(r['features']['points']*r['features']['step_seconds']/86400 for r in paired),
        'horizon_hours': distribution((pd.Timestamp(r['test_end'])-pd.Timestamp(r['origin_time'])).total_seconds()/3600
                                      + r['features']['step_seconds']/3600 for r in paired)}
    result['cost_diagnostics'] = []
    for kind in ('resource_splits', 'time_splits'):
        for model in MODELS:
            metrics = [m for s in result[kind] if s['status'] == 'ok' for m in s['models'] if m['model'] == model]
            result['cost_diagnostics'].append({'split_kind': kind, 'model': model, 'valid_splits': len(metrics),
                'mae_seconds': distribution(m['mae_seconds'] for m in metrics),
                'total_bias_seconds': distribution(m['total_prediction_bias_seconds'] for m in metrics),
                'total_prediction_ratio': distribution(m['predicted_to_actual_total_ratio'] for m in metrics
                                                       if m['predicted_to_actual_total_ratio'] is not None)})
    result['checks'] = {
        'valid_resource_splits': sum(s['status']=='ok' for s in result['resource_splits']),
        'valid_time_splits': sum(s['status']=='ok' for s in result['time_splits']),
        'insufficient_splits': sum(s['status']!='ok' for key in ('resource_splits', 'time_splits') for s in result[key])}
    result['limits'] = (
        'Development diagnostics; no automatic winner or final validation claim. Inner calibration labels belong only '
        'to outer training. Inner fitting labels precede the earliest calibration feature cutoff. All variants share '
        'a budget from inner fitting mean cost and test count; calibrated variants cannot enlarge their budget. '
        'Calibration multiplier changes allocation, not true runtime. q90 is an empirical per-row ratio quantile, '
        'not a batch overrun guarantee. Costs are max(delta_wall_seconds,0) proxies; training prediction floor=1e-6. '
        'Reference budget excludes base forecasts and routing/feature overhead. Suite timing is analysis-host wall time. '
        'Random metrics are means over 20 orders, not independent business trials; split distributions are not confidence '
        'intervals. Resource splits overlap and are not business splits. Compare gain AND actual budget utilization, '
        'not just MAE or overrun reduction. All runs remain separate; no pooled inference across snapshots. '
        'No IDs, hashes, paths, exact dates, raw errors or curves exported. Internal review required.')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; choose another filename')
    if len({p.resolve() for p in args.run_dir}) != len(args.run_dir):
        parser.error('Duplicate run directory')
    bundle = {'schema': 'routing-calibration-bundle-v1', 'runs': []}
    for index, directory in enumerate(args.run_dir):
        report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
        if report['metadata'].get('index_unchanged') is False:
            parser.error('Snapshot changed; rerun on a frozen copy')
        rows = [json.loads(line) for line in (directory/'pairs.jsonl').read_text(encoding='utf-8').splitlines()]
        if report['summary']['attempts'] != len(rows):
            parser.error('Report and pair counts disagree')
        result = calibration_analysis(rows)
        result['run_number'] = index+1
        result['source'] = (report['metadata'].get('source') if report['metadata'].get('source') in
                            ('synthetic', 'unverified_local_snapshot') else 'unknown')
        bundle['runs'].append(result)
        print(f'Analysis {index+1}/{len(args.run_dir)} completed: {result["status"]}', flush=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(bundle, stream, indent=2, allow_nan=False)
    print('One aggregate bundle written. Review internally before sharing.')


if __name__ == '__main__':
    main()
