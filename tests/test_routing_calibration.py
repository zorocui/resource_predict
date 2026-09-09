import copy
import json
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import pandas as pd

from benchmarks.routing_calibration import (
    MODELS, calibrated_costs, calibration_analysis, calibration_split, evaluate_calibration, main,
)
from test_routing_share import records
from tools.build_routing_package import build


def nested_records():
    # Distinct workload groups; each has its full origin history.
    rows = records()
    return rows[:112], rows[112:]


def test_inner_split_purges_labels_and_has_disjoint_fit_and_calibration():
    train, _ = nested_records()
    fit, calibration = calibration_split(train)
    assert fit and calibration
    assert max(pd.Timestamp(r['test_end']) for r in fit) < min(pd.Timestamp(r['feature_end']) for r in calibration)
    assert not {id(r) for r in fit} & {id(r) for r in calibration}
    assert len(fit)+len(calibration) < len(train)


def test_calibration_factors_use_only_calibration_and_share_one_budget():
    train, test = nested_records()
    fit, cal = calibration_split(train)
    costs, reference, factors = calibrated_costs(fit, cal, test)
    changed_cal = copy.deepcopy(cal)
    for row in changed_cal:
        row['delta_wall_seconds'] *= 3
    changed, new_reference, new_factors = calibrated_costs(fit, changed_cal, test)
    assert reference == new_reference
    assert np.array_equal(costs['log_ridge'], changed['log_ridge'])
    assert np.isclose(new_factors['mean_factor'], factors['mean_factor']*3)
    assert new_factors['conservative_factor'] >= new_factors['mean_factor']

    result = evaluate_calibration(train, test, 0)
    assert result['status'] == 'ok'
    for i in range(4):
        budgets = [m['budget_curve'][i]['budget_seconds'] for m in result['models']]
        assert len(set(budgets)) == 1


def test_test_labels_do_not_change_factors_budgets_or_selections():
    train, test = nested_records()
    changed = copy.deepcopy(test)
    for row in changed:
        row['delta_wall_seconds'] = 100
        row['delta_rmse'] = -100
    before, after = evaluate_calibration(train, test, 0), evaluate_calibration(train, changed, 0)
    assert before['calibration_factors'] == after['calibration_factors']
    for m1, m2 in zip(before['models'], after['models']):
        for c1, c2 in zip(m1['budget_curve'], m2['budget_curve']):
            assert c1['budget_seconds'] == c2['budget_seconds']
            for policy in c1['policies']:
                p1, p2 = c1['policies'][policy], c2['policies'][policy]
                assert p1['selected'] == p2['selected']
                assert p1['predicted_spend_seconds'] == p2['predicted_spend_seconds']
                assert p1['predicted_spend_seconds'] <= c1['budget_seconds']+1e-9
                assert p2['observed_overrun_seconds'] > 0


def test_complete_report_is_private_and_marks_short_splits():
    result = calibration_analysis(records())
    encoded = json.dumps(result, allow_nan=False)
    assert 'SECRET' not in encoded and '2026-01' not in encoded
    assert result['checks']['valid_resource_splits'] == 10
    assert result['checks']['insufficient_splits'] > 0
    assert len(result['overview']) > 0
    assert {m['model'] for m in result['resource_splits'][0]['models']} == set(MODELS)
    assert calibration_analysis(records()[:3])['status'] == 'insufficient_data'


def test_cli_bundles_multiple_runs_without_disclosing_paths(tmp_path, monkeypatch):
    directories = []
    for i in range(2):
        directory = tmp_path/f'SECRET-{i}'
        directory.mkdir()
        rows = records()[:3]
        (directory/'pairs.jsonl').write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
        (directory/'report.json').write_text(json.dumps({'metadata': {'source': 'SECRET-source', 'index_unchanged': True},
                                                        'summary': {'attempts': len(rows)}}), encoding='utf-8')
        directories.append(str(directory))
    output = tmp_path/'bundle.json'
    monkeypatch.setattr('sys.argv', ['routing_calibration', '--run-dir', *directories, '--output', str(output)])
    main()
    result = json.loads(output.read_text(encoding='utf-8'))
    assert len(result['runs']) == 2
    assert [r['run_number'] for r in result['runs']] == [1, 2]
    assert 'SECRET' not in json.dumps(result)


def test_updated_package_contains_calibration_entrypoint(tmp_path):
    archive_path = tmp_path/'suite.zip'
    build(Path(__file__).resolve().parents[1], archive_path)
    with ZipFile(archive_path) as archive:
        assert 'routing-experiment/benchmarks/routing_calibration.py' in archive.namelist()
