import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))

from maestro.android_oracle import AndroidOracle, ModelAndroidOracle, assess, propose_rule
from maestro.decision_backend import FakeDecisionBackend, ModelResponse, ResponseFormatError
from maestro.cooperation import Feedback, merge_assessment
from maestro.schemas import (
    ActionKind, ActionProposal, ExecutionRecord, GoalSpec, PageSnapshot,
    VariableProposal, WidgetSnapshot,
)
from maestro.session import create_goal_state
from maestro.trace import JsonlTrace


class AndroidOracleTests(unittest.TestCase):
    def setUp(self):
        self.state = replace(
            create_goal_state('run', GoalSpec('goal', 'Enter and search', (
                'generate:title=item', 'text_ref:search=item',
            )), 1),
            current_subgoal='generate:title=item',
        )
        self.before = PageSnapshot('run', 1, 'screen', (
            WidgetSnapshot('title', 'input', '', 'pkg:id/title'),
            WidgetSnapshot('continue', 'button', 'Continue'),
        ))
        self.action = ActionProposal('action', ActionKind.TEXT, 'title', 1, value='expected')

    def observed(self, action=None, candidate=True):
        action = action or self.action
        state = self.state.with_pending_action(action)
        if candidate:
            state = state.stage_variable(VariableProposal('item', 'expected', action.action_id, 1))
        return state.apply_execution(ExecutionRecord(action.action_id, True, 1, 2, 'OBSERVED'))

    def test_exact_observed_text_confirms_the_variable_for_later_steps(self):
        rule = propose_rule(self.action, self.state, self.before)
        after = replace(self.before, state_version=2, widgets=(
            WidgetSnapshot('title', 'input', 'expected', 'pkg:id/title'),
        ))
        state = self.observed()
        outcome = assess(rule, state, after, 'states/after.json')
        self.assertEqual((outcome.status, outcome.confirmed_variables), ('PASS', ('item',)))
        self.assertEqual(merge_assessment(state, outcome).get_variable('item'), 'expected')

    def test_the_wrong_text_cannot_confirm_a_variable(self):
        rule = propose_rule(self.action, self.state, self.before)
        after = replace(self.before, state_version=2, widgets=(
            WidgetSnapshot('title', 'input', 'wrong', 'pkg:id/title'),
            WidgetSnapshot('notice', 'text', 'expected'),
        ))
        outcome = assess(rule, self.observed(), after, 'states/after.json')
        self.assertEqual((outcome.status, outcome.confirmed_variables), ('FAIL', ()))

    def test_missing_or_ambiguous_target_remains_unknown(self):
        rule = propose_rule(self.action, self.state, self.before)
        for widgets in (
            (WidgetSnapshot('title', 'text', 'expected', 'pkg:id/other'),),
            (WidgetSnapshot('a', 'input', 'expected', 'pkg:id/title'),
             WidgetSnapshot('b', 'input', 'expected', 'pkg:id/title')),
            (WidgetSnapshot('a', 'input', 'expected', 'pkg:id/other'),),
            (WidgetSnapshot('other-activity', 'input', 'expected', 'pkg:id/title'),),
        ):
            after = replace(self.before, state_version=2, widgets=widgets)
            self.assertEqual(assess(rule, self.observed(), after, 'states/after.json').status, 'UNKNOWN')

    def test_unknown_without_valid_post_action_observation(self):
        rule = propose_rule(self.action, self.state, self.before)
        state = self.observed()
        after = replace(self.before, state_version=2)
        for observation, evidence in ((after, None), (None, 'event.json'), (self.before, 'before.json')):
            outcome = assess(rule, state, observation, evidence)
            self.assertEqual((outcome.status, outcome.confirmed_variables), ('UNKNOWN', ()))

    def test_foreign_run_or_unrelated_action_rejected(self):
        self.assertRaises(ValueError, propose_rule, self.action, self.state,
                          replace(self.before, run_id='foreign'))
        rule = propose_rule(self.action, self.state, self.before)
        with self.assertRaises(ValueError):
            assess(rule, replace(self.observed(), goal_id='foreign'), None, 'event.json')
        with self.assertRaises(ValueError):
            assess(rule, self.observed(replace(self.action, action_id='other')), None, 'event.json')

    def test_click_cannot_pass_without_real_dispatch(self):
        click = ActionProposal('click', ActionKind.CLICK, 'continue', 1)
        rule = propose_rule(click, self.state, self.before)
        state = self.state.with_pending_action(click).apply_execution(
            ExecutionRecord('click', False, 1, 2, 'REJECTED'))
        after = replace(self.before, state_version=2, state_id='other')
        self.assertEqual(assess(rule, state, after, 'after.json').status, 'UNKNOWN')

    def test_reference_rule_freezes_confirmed_text(self):
        action = replace(self.action, value=None, value_ref='item')
        with self.assertRaises(ValueError):
            propose_rule(action, self.state, self.before)
        state = self.state.with_variable('item', 'confirmed')
        self.assertEqual(propose_rule(action, state, self.before).expected_text, 'confirmed')

    def test_click_requires_observable_ui_change(self):
        click = ActionProposal('click', ActionKind.CLICK, 'continue', 1)
        rule = propose_rule(click, self.state, self.before)
        self.assertEqual(rule.kind, 'ui_change')
        for screen, expected in (('screen', 'UNKNOWN'), ('details', 'PASS')):
            after = replace(self.before, state_version=2, state_id=screen)
            self.assertEqual(assess(rule, self.observed(click, False), after, 'states/after.json').status, expected)

    def test_oracle_requests_a_read_only_observation(self):
        state = self.observed()
        feedback = Feedback('f', 'run', 'goal', 'action', 2, 'ORACLE_UNCERTAIN',
                            'No target field observed', ('events/1.json',))
        evidence = AndroidOracle().evidence_requests(state, replace(self.before, state_version=2), feedback)
        self.assertTrue(evidence.evidence_requests)
        self.assertTrue(any('title' in request for request in evidence.evidence_requests))

    def test_model_oracle_requests_a_frozen_rule_in_its_own_context(self):
        backend = FakeDecisionBackend({'oracle': {'kind': 'text_equals', 'widget_id': 'title'}})
        proposal = ModelAndroidOracle(backend, timeout_seconds=7).propose_rule(
            self.action, self.state, self.before)
        self.assertEqual((proposal.rule.kind, proposal.rule.expected_text), ('text_equals', 'expected'))
        request, = backend.requests
        self.assertEqual((request.role, request.timeout_seconds), ('oracle', 7))
        self.assertEqual((request.payload['run_id'], request.payload['goal_id']), ('run', 'goal'))
        self.assertEqual(request.payload['action']['action_id'], 'action')
        self.assertEqual(request.payload['widgets'][0]['id'], 'title')
        self.assertTrue(ModelAndroidOracle.uses_model)

    def test_model_oracle_keeps_usage_without_changing_budget(self):
        class Backend:
            def complete(self, request):
                return ModelResponse(request.request_id, {'kind':'text_equals', 'widget_id':'title'}, 11, 3)
        proposal = ModelAndroidOracle(Backend()).propose_rule(self.action, self.state, self.before)
        self.assertEqual((proposal.model_input_tokens, proposal.model_output_tokens), (11, 3))
        self.assertEqual(self.state.budget.model_calls_left, 12)

    def test_model_oracle_rejects_rules_outside_the_action_with_usage(self):
        for answer in (
            {'kind':'ui_change', 'widget_id':'title'},
            {'kind':'text_equals', 'widget_id':'continue'},
            {'kind':'text_equals', 'widget_id':'title', 'expected_text':'injected'},
            {'kind':'text_equals', 'widget_id':'title', 'status':'PASS'},
            {'kind':'text_equals'},
            ['text_equals', 'title'],
        ):
            class Backend:
                def complete(self, request):
                    return ModelResponse(request.request_id, answer, 11, 3)
            with self.subTest(answer=answer), self.assertRaises(ResponseFormatError) as raised:
                ModelAndroidOracle(Backend()).propose_rule(self.action, self.state, self.before)
            self.assertEqual((raised.exception.input_tokens, raised.exception.output_tokens), (11, 3))

    def test_model_oracle_rejects_unrelated_response(self):
        class Backend:
            def complete(self, request):
                return ModelResponse('other', {'kind':'text_equals', 'widget_id':'title'}, 11, 3)
        with self.assertRaises(ResponseFormatError):
            ModelAndroidOracle(Backend()).propose_rule(self.action, self.state, self.before)

    def test_model_oracle_click_only_proposes_ui_change_rule(self):
        action = ActionProposal('click', ActionKind.CLICK, 'continue', 1)
        backend = FakeDecisionBackend({'oracle': {'kind':'ui_change', 'widget_id':'continue'}})
        rule = ModelAndroidOracle(backend).propose_rule(action, self.state, self.before).rule
        self.assertEqual((rule.kind, rule.expected_text), ('ui_change', None))

    def test_invalid_binding_does_not_call_model(self):
        backend = FakeDecisionBackend()
        with self.assertRaises(ValueError):
            ModelAndroidOracle(backend).propose_rule(self.action, self.state,
                                                    replace(self.before, run_id='other'))
        self.assertFalse(backend.requests)

    def test_model_oracle_requires_finite_positive_timeout(self):
        for timeout in (0, -1, float('inf'), float('nan')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                ModelAndroidOracle(FakeDecisionBackend(), timeout_seconds=timeout)


class JsonlTraceTests(unittest.TestCase):
    def test_safe_serialization_and_request_action_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'runs' / 'trace.jsonl'
            trace = JsonlTrace(path)
            trace.write('Planner', 'request', request_id='request-1',
                        headers={'Authorization': 'secret-bearer'}, api_key='secret-key',
                        raw_payload='private-prompt')
            trace.write('Data', 'action', action=ActionProposal('action-1', ActionKind.CLICK, 'button', 1))
            text = path.read_text(encoding='utf-8')
            one, two = map(json.loads, text.splitlines())
            self.assertEqual((one['seq'], one['role'], one['request_id']), (1, 'Planner', 'request-1'))
            self.assertEqual((two['seq'], two['action']['kind'], two['action']['action_id']), (2, 'CLICK', 'action-1'))
            for secret in ('secret-bearer', 'secret-key', 'private-prompt'):
                self.assertNotIn(secret, text)
            with self.assertRaises(FileExistsError):
                JsonlTrace(path)

    def test_nonserializable_detail_does_not_emit_partial_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'trace.jsonl'
            trace = JsonlTrace(path)
            with self.assertRaises(TypeError):
                trace.write('Oracle', 'assessment', foreign=object())
            trace.write('Oracle', 'assessment', status='UNKNOWN')
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['seq'], 1)


if __name__ == '__main__':
    unittest.main()
