"""Regression for a historical welcome overlay missing on the live page."""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))
from maestro.android_bridge import CandidatePool, GuidanceFunction, snapshot_from_device
from maestro.android_runtime import AndroidRuntime
from maestro.decision_backend import FakeDecisionBackend
from maestro.goal_selection import GoalCandidate


def view(resource, text, **attrs):
    return dict(resource_id=f'app:id/{resource}', text=text, visible=True, enabled=True,
                clickable=True, bounds=[[0, 0], [50, 50]], **{'class': 'android.widget.Button', **attrs})


def page(*widgets, fingerprint='log', index=1):
    return SimpleNamespace(views=widgets, foreground_activity='app/.Main', state_str=fingerprint,
                           get_id=lambda: index)


class AndroidArrivalRuntimeTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.output = Path(folder.name)
        self.ok = view('tutorial_ok_button', 'OK')
        self.misc = view('misc_button', 'Misc')
        self.historical = page(self.ok, self.misc, fingerprint='with-tutorial')
        self.live = page(self.misc, fingerprint='without-tutorial', index=11)
        self.backend = FakeDecisionBackend()
        self.runtime = AndroidRuntime(SimpleNamespace(output_dir=folder.name, package_name='app'), self.backend)
        self.cluster = SimpleNamespace(get_id=lambda: 1, update_tested_function=Mock())
        self.utg = SimpleNamespace(clusters=(self.cluster,))

    def trace(self):
        return [json.loads(line) for line in (self.output / 'maestro-trace.jsonl').read_text(
            encoding='utf-8').splitlines()]

    def candidate(self, device, candidate_id, function, widget=0, cluster='1'):
        snapshot = snapshot_from_device(self.runtime.run_id, 0, device)
        return GoalCandidate(self.runtime.run_id, candidate_id, str(device.get_id()), function,
                             f'states/state_{device.get_id()}.json', cluster, snapshot.widgets,
                             snapshot.widgets[widget].widget_id)

    def select(self, *candidates, current=None, plan=None):
        choice = candidates[0]
        self.backend.responses['goal_selection'] = {'candidate_id': choice.candidate_id,
                                                    'subgoals': plan or [f'click:{choice.target_widget_id}']}
        pool = CandidatePool(tuple(GuidanceFunction(c.candidate_id, c.target_cluster_id, c.function)
                                   for c in candidates), candidates)
        with patch('maestro.android_runtime.read_candidate_pool', return_value=pool):
            return self.runtime.select(self.utg, current_state=current)

    def test_missing_original_anchor_blocks_before_data_and_oracle(self):
        candidate = self.candidate(self.historical, 'c1-f1', 'confirm tutorial')
        self.assertIsNotNone(self.select(candidate))
        self.assertEqual('goal_anchor_missing', self.runtime.arrival_problem(self.live))
        self.assertIsNone(self.runtime.prepare(self.live))
        self.assertEqual(['goal_selection'], [request.role for request in self.backend.requests])
        finished = next(row for row in self.trace() if row['event'] == 'goal_finished')
        self.assertEqual('BLOCKED', finished['status'])
        self.assertIn('goal_anchor_missing', finished['reason'])
        self.cluster.update_tested_function.assert_not_called()
        self.assertIsNone(self.runtime.pending)

    def test_exact_live_control_and_capability_are_required_even_with_same_state_id(self):
        self.assertIsNotNone(self.select(self.candidate(self.historical, 'c1-f1', 'confirm tutorial')))
        observed = page(self.ok, self.misc, index=11)
        self.assertIsNone(self.runtime.arrival_problem(observed))
        same_id = page(dict(self.ok, enabled=False), self.misc, index=1)
        self.assertEqual('goal_anchor_action_unavailable', self.runtime.arrival_problem(same_id))

    def test_missing_first_step_blocks_even_if_original_anchor_exists(self):
        field = view('field', '', editable=True)
        source = page(self.ok, field)
        candidate = self.candidate(source, 'c1-f1', 'submit data')
        steps = [f'generate:{candidate.widgets[1].widget_id}=entry', f'click:{candidate.target_widget_id}']
        self.assertIsNotNone(self.select(candidate, plan=steps))
        self.assertEqual('first_step_target_missing', self.runtime.arrival_problem(page(self.ok)))
        self.runtime.block_navigation('first_step_target_missing')
        self.assertEqual(candidate, self.select(candidate, plan=[f'click:{candidate.target_widget_id}'],
                                                current=page(self.ok)))

    def test_initial_enter_needs_focused_live_input(self):
        field = view('field', '', editable=True, focused=True)
        source = page(field)
        candidate = self.candidate(source, 'c1-f1', 'submit input')
        self.assertIsNotNone(self.select(candidate, plan=[f'enter:{candidate.target_widget_id}']))
        self.assertIsNone(self.runtime.arrival_problem(source))
        live = page(dict(field, focused=False), index=11)
        self.assertEqual('first_step_enter_not_focused', self.runtime.arrival_problem(live))

    def test_blocked_candidate_is_deferred_by_origin_until_anchor_reappears(self):
        candidate = self.candidate(self.historical, 'c1-f1', 'confirm tutorial')
        self.assertIsNotNone(self.select(candidate))
        self.runtime.block_navigation('goal_anchor_missing')
        renumbered = self.candidate(self.historical, 'c1-f4', 'confirm tutorial')
        self.assertIsNone(self.select(renumbered, current=self.live))
        self.assertEqual(1, len(self.backend.requests))
        self.assertEqual(renumbered, self.select(renumbered, current=self.historical))
        self.cluster.update_tested_function.assert_not_called()

    def test_duplicate_navigation_bindings_keep_an_auditable_single_choice(self):
        first = self.candidate(self.historical, 'c1-f1', 'navigate to Misc', widget=1)
        duplicate = self.candidate(self.live, 'c2-f2', 'navigate to Misc', cluster='2')
        self.assertEqual(first, self.select(first, duplicate, current=self.live))
        request = self.backend.requests[0]
        self.assertEqual(['c1-f1'], [item['candidate_id'] for item in request.payload['candidates']])
        trigger = next(row for row in self.trace() if row['event'] == 'low_growth')
        self.assertEqual(2, trigger['grounded_candidate_count'])
        self.assertEqual(1, trigger['deduplicated_candidate_count'])
        self.assertEqual([{'discarded_candidate_id': 'c2-f2', 'kept_candidate_id': 'c1-f1'}],
                         trigger['duplicate_candidates'])
