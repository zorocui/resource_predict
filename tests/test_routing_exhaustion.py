import copy

import pandas as pd
import pytest

from benchmarks.routing_gate_options import fixtures
from benchmarks.routing_gate_replay import replay_policy

OPTIONS = {'early_cost': True, 'min_mean_loss': .001, 'probe_floor': True,
           'cumulative_loss_limit': .001, 'probe_episode_ratio': .6}


def test_review_marks_shortfall_without_adding_credit():
    stream = dict(fixtures())['large_loss_then_recovery']
    result = replay_policy(stream, True, exhausted_policy='review', **OPTIONS)
    assert result['summary']['review_required_batches'] > 0
    assert result['summary']['extension_grant_events']==0
    assert all(r['probe_episode_cap'] is None or r['probe_episode_cap']==.6 for r in result['batches'])


def test_retry_grants_are_bounded_and_spaced_and_costs_counted():
    stream = dict(fixtures())['persistent_small_loss']
    result = replay_policy(stream, True, exhausted_policy='bounded_retry', **OPTIONS)
    grants = [r for r in result['batches'] if r['budget_disposition']=='bounded_grant']
    assert 0 < len(grants) <= 2
    assert all(b['batch_number']-a['batch_number'] >= 6 for a,b in zip(grants, grants[1:]))
    assert all(r['probe_episode_cap'] is None or r['probe_episode_cap'] <= 1.+1e-12 for r in result['batches'])
    assert result['summary']['cost_including_shadow']==sum(r['actual_cost_seconds'] for r in result['batches'])


def test_future_cost_cannot_change_current_grants():
    stream = dict(fixtures())['persistent_small_loss']
    altered = copy.deepcopy(stream)
    for b in altered[24:]:
        b['actual'] *= 999
    a = replay_policy(stream, True, exhausted_policy='bounded_retry', **OPTIONS)
    b = replay_policy(altered, True, exhausted_policy='bounded_retry', **OPTIONS)
    assert a['batches'][:24]==b['batches'][:24]


def test_pending_probe_blocks_new_credit():
    stream = dict(fixtures())['large_loss_then_recovery']
    # First active feedback still arrives; probe gains issued later remain pending.
    for i,b in enumerate(stream):
        if i >= 10:
            b['due'] = [pd.Timestamp('2027-01-01')]*2
    options = {**OPTIONS, 'probe_episode_ratio': .2}
    result = replay_policy(stream, True, exhausted_policy='bounded_retry', **options)
    assert any(r['budget_disposition']=='awaiting_probe_feedback' for r in result['batches'])
    assert result['summary']['extension_grant_events']==0


def test_exhaustion_requires_valid_policy_and_budget():
    with pytest.raises(ValueError):
        replay_policy([], True, exhausted_policy='unlimited')
    with pytest.raises(ValueError):
        replay_policy([], True, exhausted_policy='review')
