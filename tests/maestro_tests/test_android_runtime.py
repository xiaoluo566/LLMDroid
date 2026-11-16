import sys
import json
import os
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))
from maestro.android_runtime import AndroidRuntime, observed_record, verified_event_ref
from maestro.android_bridge import snapshot_from_device
from maestro.decision_backend import FakeDecisionBackend
from maestro.schemas import GoalSpec, ActionProposal, ActionKind
from maestro.session import create_goal_state
from maestro.schemas import ExecutionRecord
from maestro.planner import FixedGoalPlanner
from maestro.android_oracle import propose_rule


class AndroidRuntimeTest(unittest.TestCase):
    def test_supplemental_observation_keeps_direct_native_event_reference(self):
        with TemporaryDirectory() as folder:
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'),
                                     FakeDecisionBackend())
            before = snapshot_from_device(runtime.run_id, 1, self.page('before', ''))
            widget = before.widgets[0].widget_id
            action = ActionProposal('a', ActionKind.TEXT, widget, 1, value='entry')
            state = create_goal_state(runtime.run_id, GoalSpec('g', 'Input', (f'text:{widget}=entry',)), 1)
            state = state.with_plan(FixedGoalPlanner().plan(state, before)).with_pending_action(action)
            rule = propose_rule(action, state, before)
            runtime.state = state.apply_execution(ExecutionRecord('a', True, 1, 2, 'OBSERVED'))
            runtime.version = 2
            page = snapshot_from_device(runtime.run_id, 2, self.page('before', ''))
            initial = runtime._save_observation(action, page, 'events/event_a.json')
            device = SimpleNamespace(get_current_state=lambda: self.page('after', 'entry'))
            result = runtime._observe_again(action, rule, page, device, initial)
            saved = json.loads((Path(folder) / result.evidence_refs[0]).read_text(encoding='utf-8'))
            self.assertEqual(saved['event_log'], 'events/event_a.json')

    def test_canary_can_validate_a_bounded_sequence_of_candidate_goals(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {
                'LLMDROID_MAESTRO_CANARY': '1', 'LLMDROID_MAESTRO_CANARY_GOALS': '3'}):
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'),
                                     FakeDecisionBackend())
            runtime.canary_goals = 1
            runtime.canary_fired = True
            runtime.finish('UNKNOWN', 'Observed local step')
            self.assertFalse(runtime.canary_finished)
            self.assertTrue(runtime.early_guidance_ready(self.page('p', '')))
            runtime.canary_goals = 3
            runtime.finish('UNKNOWN', 'Observed local step')
            self.assertTrue(runtime.canary_finished)

    def test_canary_is_explicit_and_triggers_once_only_inside_target_app(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {'LLMDROID_MAESTRO_CANARY': '1'}):
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'),
                                     FakeDecisionBackend())
            self.assertTrue(runtime.early_guidance_ready(self.page('p', '')))
            self.assertFalse(runtime.early_guidance_ready(
                SimpleNamespace(foreground_activity='com.android.launcher/.Main')))
            runtime.canary_fired = True
            self.assertFalse(runtime.early_guidance_ready(self.page('p', '')))

    def test_unknown_dispatch_blocks_return_to_exploration(self):
        with TemporaryDirectory() as folder:
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'),
                                     FakeDecisionBackend())
            action = ActionProposal('a', ActionKind.TEXT, 'name', 0, value='entry')
            runtime.state = create_goal_state(runtime.run_id, GoalSpec('g', 'Input', ('text:name=entry',)), 0)
            runtime.state = runtime.state.with_pending_action(action)
            event = object()
            runtime.pending = (event, action, None)
            runtime.on_event_result(event, SimpleNamespace(to_state=None, dispatched=True))
            self.assertTrue(runtime.uncertain_write)

    def test_observation_failure_ends_goal_without_exiting_policy(self):
        with TemporaryDirectory() as folder:
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'),
                                     FakeDecisionBackend())
            runtime.state = create_goal_state(runtime.run_id, GoalSpec('g', 'Input', ('text:x=test',)), 0)
            with patch('maestro.android_runtime.snapshot_from_device', side_effect=OSError('capture failed')):
                self.assertIsNone(runtime.prepare(self.page('p', '')))
            self.assertIsNone(runtime.state)
            last = json.loads((Path(folder) / 'maestro-trace.jsonl').read_text(encoding='utf-8').splitlines()[-1])
            self.assertEqual(last['status'], 'UNKNOWN')

    def test_result_processing_failure_ends_goal_without_exiting_policy(self):
        with TemporaryDirectory() as folder:
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'),
                                     FakeDecisionBackend())
            runtime.state = create_goal_state(runtime.run_id, GoalSpec('g', 'Input', ('text:x=test',)), 0)
            event = object()
            runtime.pending = (event, SimpleNamespace(source_state_version=0), None)
            with patch('maestro.android_runtime.snapshot_from_device', side_effect=OSError('capture failed')):
                runtime.on_event_result(event, SimpleNamespace(to_state=self.page('p', ''), dispatched=True))
            self.assertIsNone(runtime.state)
            self.assertIsNone(runtime.pending)

    def test_callback_distinguishes_rejection_and_missing_after_page(self):
        rejected = observed_record('a', 4, SimpleNamespace(dispatched=False), None)
        self.assertFalse(rejected.dispatched)
        self.assertEqual(rejected.execution_status, 'NOT_DISPATCHED')
        unknown = observed_record('a', 4, SimpleNamespace(dispatched=True), None)
        self.assertEqual(unknown.execution_status, 'EXECUTION_UNKNOWN')
        observed = observed_record('a', 4, SimpleNamespace(dispatched=True), 5)
        self.assertEqual(observed.execution_status, 'OBSERVED')

    def test_event_evidence_must_parse_and_match_observed_pages(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            events = root / 'events'
            events.mkdir()
            event = events / 'event_round.json'
            log = SimpleNamespace(tag='round', from_state=SimpleNamespace(state_str='a'),
                                  to_state=SimpleNamespace(state_str='b'))
            event.write_text('{', encoding='utf-8')
            self.assertIsNone(verified_event_ref(root, log))
            event.write_text('{"start_state":"b","stop_state":"a"}', encoding='utf-8')
            self.assertIsNone(verified_event_ref(root, log))
            event.write_text('{"start_state":"a","stop_state":"b"}', encoding='utf-8')
            self.assertEqual(verified_event_ref(root, log), 'events/event_round.json')

    def test_candidate_to_verified_input_has_one_correlated_trace(self):
        with TemporaryDirectory() as folder:
            output = Path(folder)
            before = self.page('a', '')
            after = self.page('b', 'Test entry')
            widget = snapshot_from_device('preview', 0, before).widgets[0].widget_id
            backend = FakeDecisionBackend({
                'goal_selection': {'candidate_id': 'c1-f1',
                                   'subgoals': [f'generate:{widget}=entry']},
                'data': {'action': {'kind': 'TEXT', 'widget_id': widget, 'value': 'Test entry'},
                         'variable': {'name': 'entry', 'value': 'Test entry'}},
                'oracle': {'kind': 'text_equals', 'widget_id': widget},
            })
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'), backend)
            cluster = SimpleNamespace(get_id=lambda: 1,
                                      has_untested_function=lambda: True,
                                      update_tested_function=Mock(),
                                      get_target_state=lambda _: before,
                                      write_overview_top5_tojson=lambda out, **_:
                                      out.update({'State1': {'FunctionList': ['name an entry']}}))
            self.assertIsNotNone(runtime.select(SimpleNamespace(clusters=(cluster,))))
            with patch('maestro.android_bridge._event_classes', return_value=(None, self.event)):
                event = runtime.prepare(before)
                self.assertIsNotNone(event)
                self.assertTrue(runtime.preflight(event, before))
                self.assertFalse(runtime.preflight(event, after))
            (output / 'events').mkdir()
            (output / 'events/event_sample.json').write_text(
                '{"start_state":"a","stop_state":"b"}', encoding='utf-8')
            runtime.on_event_result(event, SimpleNamespace(to_state=after, from_state=before,
                                                            dispatched=True, tag='sample'))
            trace = [json.loads(line) for line in (output / 'maestro-trace.jsonl').read_text(
                encoding='utf-8').splitlines()]
            result = next(row for row in trace if row['event'] == 'step_assessed')
            self.assertEqual(result['assessment']['status'], 'PASS')
            self.assertEqual(result['assessment']['confirmed_variables'], ['entry'])
            finished = next(row for row in reversed(trace) if row['event'] == 'goal_finished')
            self.assertEqual(finished['status'], 'UNKNOWN')
            cluster.update_tested_function.assert_called_once_with('name an entry')
            self.assertIsNone(runtime.state)
            self.assertEqual([r.role for r in backend.requests], ['goal_selection', 'data', 'oracle'])

    @staticmethod
    def page(fingerprint, value):
        return SimpleNamespace(
            foreground_activity='example.app/.Main', state_str=fingerprint,
            get_id=lambda: 1,
            views=[{'resource_id': 'example.app:id/name', 'class': 'android.widget.EditText',
                    'text': value, 'visible': True, 'enabled': True, 'editable': True,
                    'bounds': [[0, 0], [50, 50]]}],
        )

    @staticmethod
    def event(view, text):
        return SimpleNamespace(get_views=lambda: [view], text=text)
