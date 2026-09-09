"""Two fixed offline policies for exhausted pause-episode probe budgets."""
import argparse
import json
from pathlib import Path

from benchmarks.routing_gate_options import fixtures
from benchmarks.routing_gate_replay import replay_policy


def compare_exhaustion():
    runs = []
    options = {'early_cost': True, 'min_mean_loss': .001, 'probe_floor': True,
               'cumulative_loss_limit': .001, 'probe_episode_ratio': .6}
    for name, stream in fixtures():
        runs.append({'scenario': name, 'policies': {
            policy: replay_policy(stream, True, exhausted_policy=policy, **options)
            for policy in ('review', 'bounded_retry')}})
    return {'schema': 'routing-exhaustion-v1', 'runs': runs,
            'protocol': {'initial_episode_ratio': .6, 'grant_ratio': .2, 'max_grants': 2,
                         'minimum_batch_gap': 6, 'max_episode_ratio': 1.0},
            'limits': 'Synthetic local comparison, not production approval. review labels insufficient episode credit, sends no messages. '
            'bounded_retry preauthorizes two grants at most per pause episode, each .2 of entry batch budget, '
            'with six scorable batches since pause/previous grant and only on every-third-batch probe opportunities. '
            'No new grant while a current-epoch probe awaits gain feedback. Current batch budget also applies. '
            'Outstanding actual cost may exceed authorized cap; no hard interruption. Recovery may remain impossible. '
            'After recovery a later pause creates a fresh episode, not a global lifetime cap. '
            'Additional resources are explicit, so gain comparisons are not equal spend. Global scope remains unverified. '
            'No automatic deployment winner or best-parameter selection.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(compare_exhaustion(), stream, indent=2, allow_nan=False)
    print('Two fixed exhaustion policies compared across seven scenarios.')


if __name__ == '__main__':
    main()
