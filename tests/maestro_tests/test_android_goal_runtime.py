"""Goal acceptance must follow the native event and the frozen target contract."""
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))
from maestro.android_bridge import snapshot_from_device
from maestro.android_runtime import AndroidRuntime
from maestro.decision_backend import FakeDecisionBackend, ModelResponse
from maestro.goal_selection import GoalCandidate


class AndroidGoalRuntimeTests(unittest.TestCase):
    @staticmethod
    def page(selected, fingerprint='same-native-hash'):
        return SimpleNamespace(
            foreground_activity='example.app/.Main', state_str=fingerprint,
            get_id=lambda: 1,
            views=[dict(resource_id='example.app:id/log', text='Log',
                        visible=True, enabled=True, clickable=True, selected=selected,
                        bounds=[[0, 0], [50, 50]], **{'class': 'android.widget.Button'})],
        )

    def run_goal(self, selected, event_present=True):
        with TemporaryDirectory() as folder:
            before, after = self.page(False), self.page(selected)
            widget = snapshot_from_device('preview', 0, before).widgets[0].widget_id
            backend = FakeDecisionBackend({
                'goal_selection': {'candidate_id': 'c1', 'subgoals': [f'click:{widget}']},
                'oracle': {'kind': 'ui_change', 'widget_id': widget},
            })
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'), backend)
            candidate = GoalCandidate(runtime.run_id, 'c1', '1', "navigate to 'Log'",
                                      'states/state_1.json', '1',
                                      snapshot_from_device(runtime.run_id, 0, before).widgets,
                                      target_widget_id=widget)
            with patch('maestro.android_runtime.candidates_from_utg', return_value=(candidate,)):
                self.assertIsNotNone(runtime.select(SimpleNamespace(clusters=())))
            touch = lambda view: SimpleNamespace(get_views=lambda: [view])
            with patch('maestro.android_bridge._event_classes', return_value=(touch, None)):
                event = runtime.prepare(before)
            self.assertIsNotNone(event)
            if event_present:
                events = Path(folder) / 'events'
                events.mkdir()
                (events / 'event_sample.json').write_text(json.dumps({
                    'start_state': before.state_str, 'stop_state': after.state_str,
                }), encoding='utf-8')
            runtime.on_event_result(event, SimpleNamespace(
                to_state=after, from_state=before, dispatched=True, tag='sample',
                device=SimpleNamespace(get_current_state=lambda: after),
            ))
            return [json.loads(line) for line in (Path(folder) / 'maestro-trace.jsonl').read_text(
                encoding='utf-8').splitlines()]

    def test_selected_target_can_pass_even_when_native_hash_ignores_selected(self):
        trace = self.run_goal(True)
        frozen = next(r for r in trace if r['event'] == 'goal_rule_frozen')
        action = next(r for r in trace if r['event'] == 'action_proposed')
        self.assertLess(frozen['seq'], action['seq'])
        result = next(r for r in trace if r['event'] == 'goal_assessed')
        self.assertEqual(result['assessment']['status'], 'PASS')
        self.assertTrue(result['assessment']['evidence_refs'])
        self.assertEqual([r for r in trace if r['event'] == 'goal_finished'][-1]['status'], 'PASS')

    def test_unselected_target_does_not_pass(self):
        trace = self.run_goal(False)
        self.assertEqual([r for r in trace if r['event'] == 'goal_finished'][-1]['status'], 'UNKNOWN')

    def test_selected_target_without_native_event_cannot_pass(self):
        trace = self.run_goal(True, event_present=False)
        self.assertEqual([r for r in trace if r['event'] == 'goal_finished'][-1]['status'], 'UNKNOWN')

    def run_menu(self, *, status='PASS', recapture=False, forged=False, timeout=False,
                 missing_event=False):
        with TemporaryDirectory() as folder:
            before, after, later = self.page(False, 'closed'), self.page(False, 'opened'), self.page(False, 'ready')
            menu = {**after.views[0], 'resource_id': 'example.app:id/new_task', 'text': 'New task'}
            later.views.append(menu)
            after.views.append({**menu, 'text': 'Opening…'} if recapture else menu)
            widget = snapshot_from_device('preview', 0, before).widgets[0].widget_id
            item = snapshot_from_device('preview', 0, later).widgets[-1].widget_id
            class MenuBackend(FakeDecisionBackend):
                def complete(self, request):
                    if request.role != 'goal_oracle':
                        return super().complete(request)
                    self.requests.append(request)
                    if timeout:
                        raise TimeoutError('Provider unavailable')
                    if recapture and request.payload['allow_recapture']:
                        return ModelResponse(request.request_id, dict(
                            status='UNKNOWN', reason='Menu is still opening', evidence_ids=[], recapture=True))
                    return ModelResponse(request.request_id, dict(
                        status=status, reason='The observed new task menu item establishes the verdict',
                        evidence_ids=[f'after:0:{"invented" if forged else item}'], recapture=False))

            backend = MenuBackend({
                'goal_selection': {'candidate_id': 'c1', 'subgoals': [f'click:{widget}']},
                'oracle': {'kind': 'ui_change', 'widget_id': widget},
            })
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'), backend)
            candidate = GoalCandidate(runtime.run_id, 'c1', '1', 'expand floating action menu',
                                      'states/state_1.json', '1',
                                      snapshot_from_device(runtime.run_id, 0, before).widgets,
                                      target_widget_id=widget)
            with patch('maestro.android_runtime.candidates_from_utg', return_value=(candidate,)):
                self.assertIsNotNone(runtime.select(SimpleNamespace(clusters=())))
            touch = lambda view: SimpleNamespace(get_views=lambda: [view])
            with patch('maestro.android_bridge._event_classes', return_value=(touch, None)):
                event = runtime.prepare(before)
            self.assertIsNotNone(event)
            if not missing_event:
                events = Path(folder) / 'events'
                events.mkdir()
                (events / 'event_sample.json').write_text(json.dumps({
                    'start_state': 'closed', 'stop_state': 'opened',
                }), encoding='utf-8')
            reads = []
            def read_page():
                reads.append('capture')
                return later

            runtime.on_event_result(event, SimpleNamespace(
                to_state=after, from_state=before, dispatched=True, tag='sample',
                device=SimpleNamespace(get_current_state=read_page),
            ))
            trace = [json.loads(line) for line in (Path(folder) / 'maestro-trace.jsonl').read_text(
                encoding='utf-8').splitlines()]
            return trace, [r for r in backend.requests if r.role == 'goal_oracle'], reads

    def test_unlisted_goal_has_independent_evidence_backed_model_verdict(self):
        trace, requests, reads = self.run_menu()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].payload['goal'], 'expand floating action menu')
        row = next(r for r in trace if r['event'] == 'goal_assessed')
        self.assertEqual(row['verdict_source'], 'model')
        verdict = row['assessment']
        self.assertEqual(verdict['status'], 'PASS')
        self.assertIn('events/event_sample.json', verdict['evidence_refs'])
        self.assertIn('maestro-observations/before-0.json', verdict['evidence_refs'])
        self.assertEqual(trace[-1]['status'], 'PASS')
        self.assertEqual(reads, [])

    def test_model_goal_fail_is_terminal_only_with_grounded_evidence(self):
        trace, _, _ = self.run_menu(status='FAIL')
        self.assertEqual(trace[-1]['status'], 'FAIL')

    def test_targeted_read_only_recapture_updates_the_version_and_budget(self):
        trace, requests, reads = self.run_menu(recapture=True)
        self.assertEqual(reads, ['capture'])
        self.assertEqual([r.payload['allow_recapture'] for r in requests], [True, False])
        result = next(r for r in trace if r['event'] == 'goal_assessed')
        self.assertEqual(result['assessment']['state_version'], 2)
        self.assertIn('maestro-observations/page-2.json', result['assessment']['evidence_refs'])
        self.assertEqual(len([r for r in trace if r['event'] == 'execution_record']), 1)
        self.assertEqual(result['budget']['model_calls_left'], 8)
        self.assertEqual(trace[-1]['status'], 'PASS')

    def test_evidence_forgery_timeout_and_missing_native_event_fail_closed(self):
        for options, expected_requests in (({'forged': True}, 1), ({'timeout': True}, 1),
                                           ({'missing_event': True}, 0)):
            with self.subTest(options=options):
                trace, requests, reads = self.run_menu(**options)
                self.assertEqual(len(requests), expected_requests)
                self.assertEqual(trace[-1]['status'], 'UNKNOWN')
                self.assertFalse(reads)

    def test_runtime_rejects_unanchored_candidates_from_legacy_compatibility_layer(self):
        with TemporaryDirectory() as folder:
            backend = FakeDecisionBackend()
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'), backend)
            candidate = GoalCandidate(runtime.run_id, 'c1', '1', "navigate to 'Log'",
                                      'states/state_1.json', '1',
                                      snapshot_from_device(runtime.run_id, 0, self.page(False)).widgets)
            with patch('maestro.android_runtime.candidates_from_utg', return_value=(candidate,)):
                self.assertIsNone(runtime.select(SimpleNamespace(clusters=())))
            self.assertFalse(backend.requests)

    def test_finish_cannot_log_an_unverified_pass(self):
        with TemporaryDirectory() as folder:
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'),
                                     FakeDecisionBackend())
            with self.assertRaises(ValueError):
                runtime.finish('PASS', 'No evidence')

    def test_invalid_plan_is_repaired_once_without_dispatching_it(self):
        with TemporaryDirectory() as folder:
            before = self.page(False)
            widgets = snapshot_from_device('preview', 0, before).widgets
            widget = widgets[0].widget_id
            class RepairBackend(FakeDecisionBackend):
                def complete(self, request):
                    self.requests.append(request)
                    steps = ['click:missing'] if len(self.requests) == 1 else [f'click:{widget}']
                    return ModelResponse(request.request_id, {'candidate_id': 'c1', 'subgoals': steps})
            backend = RepairBackend()
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'), backend)
            candidate = GoalCandidate(runtime.run_id, 'c1', '1', "navigate to 'Log'",
                                      'states/state_1.json', '1', widgets, target_widget_id=widget)
            with patch('maestro.android_runtime.candidates_from_utg', return_value=(candidate,)):
                self.assertIsNotNone(runtime.select(SimpleNamespace(clusters=())))
            self.assertEqual(len(backend.requests), 2)
            self.assertEqual(runtime.state.budget.model_calls_left, 10)
            self.assertIn('步骤控件不属于候选', backend.requests[-1].payload['selection_hint'])
            self.assertIsNone(runtime.pending)


if __name__ == '__main__':
    unittest.main()
