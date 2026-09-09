import copy
import json

import numpy as np
import pandas as pd

from benchmarks.routing_batch import batch_analysis
from benchmarks.routing_budget import allocate
from test_routing_share import records


def test_future_observations_cannot_change_current_decisions():
    rows = records()
    before = batch_analysis(rows)
    future = copy.deepcopy(rows)
    for row in future:
        row['origin_time'] = str(pd.Timestamp(row['origin_time'])+pd.Timedelta(days=100))
        row['test_end'] = str(pd.Timestamp(row['test_end'])+pd.Timedelta(days=100))
        row['feature_end'] = str(pd.Timestamp(row['feature_end'])+pd.Timedelta(days=100))
        row['delta_rmse'] = 100
        row['delta_wall_seconds'] = 100
    after = batch_analysis(rows+future)
    for a, b in zip(before['batches'], after['batches']):
        a.pop('suite_analysis_wall_seconds', None)
        b.pop('suite_analysis_wall_seconds', None)
        assert a == b
    assert before['valid_batches'] > 0
    assert 'SECRET' not in json.dumps(before)


def test_each_batch_train_labels_precede_feature_cutoff(monkeypatch):
    def check(train, test, seed):
        assert seed == 42
        assert len({r['origin_time'] for r in test}) == 1
        assert all(pd.Timestamp(r['test_end']) < min(pd.Timestamp(t['feature_end']) for t in test) for r in train)
        return {'status': 'insufficient_data'}
    monkeypatch.setattr('benchmarks.routing_batch.evaluate_calibration', check)
    batch_analysis(records())


def test_batch_budget_counterexample_and_cost_shock():
    costs = np.ones(4)
    pooled = allocate([0, 1, 2, 3], costs, 2.)
    batches = np.r_[allocate([0, 1], costs[:2], 1.), allocate([0, 1], costs[2:], 1.)]
    assert pooled.tolist() == [True, True, False, False]
    assert batches.tolist() == [True, False, True, False]
    # A forecast-only gate does not guarantee actual-budget feasibility after a shock.
    assert (costs*3)[batches].sum() == 6
