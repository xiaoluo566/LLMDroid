import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))
from maestro.android_oracle import AndroidOracle, assess, propose_rule
from maestro.data_agent import RuleBasedDataAgent
from maestro.decision_backend import FakeDecisionBackend, ResponseFormatError
from maestro.graph import decide_once
from maestro.planner import FixedGoalPlanner, SingleAgentDecisionAgent
from maestro.schemas import (
    ActionKind, ActionProposal, DecisionKind, ExecutionRecord, GoalSpec, PageSnapshot, WidgetSnapshot,
)
from maestro.session import create_goal_state


GESTURES = ('LONG_PRESS', 'SWIPE_UP', 'SWIPE_DOWN', 'SWIPE_LEFT', 'SWIPE_RIGHT')


class NativeActionSchemaTests(unittest.TestCase):
    def test_native_gestures_are_dispatched_as_one_action_and_request_ui_evidence(self):
        for name in GESTURES:
            with self.subTest(name=name):
                kind = ActionKind(name)
                page = PageSnapshot('run', 1, 'page', (
                    WidgetSnapshot('target', 'label', supported_actions=(kind,)),
                ))
                step = f'{name.lower()}:target'
                state = create_goal_state('run', GoalSpec('goal', 'Test control', (step,)), 1)
                outcome = decide_once(state, page, FixedGoalPlanner(), RuleBasedDataAgent())
                self.assertEqual(outcome.result.kind, DecisionKind.ACTION)
                action = outcome.result.action
                self.assertEqual((action.kind, action.widget_id), (kind, 'target'))
                self.assertEqual(propose_rule(action, outcome.state, page).kind, 'ui_change')
                request = AndroidOracle().evidence_requests(
                    outcome.state, page, SimpleNamespace(action_id=action.action_id),
                )
                self.assertIn(name, request.evidence_requests[0])

    def test_explicit_empty_capabilities_override_legacy_widget_type(self):
        legacy = WidgetSnapshot('target', 'text')
        self.assertEqual(legacy.allowed_actions, (ActionKind.CLICK, ActionKind.TEXT))
        self.assertFalse(WidgetSnapshot('field', 'input').supports(ActionKind.CLICK))
        unavailable = WidgetSnapshot('target', 'button', supported_actions=())
        state = create_goal_state('run', GoalSpec('goal', 'Open control', ('click:target',)), 1)
        page = PageSnapshot('run', 1, 'page', (unavailable,))
        forced = SimpleNamespace(propose=lambda *_: ActionProposal('a', ActionKind.CLICK, 'target', 1))
        outcome = decide_once(state, page, FixedGoalPlanner(), forced)
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)
        self.assertEqual(outcome.state.budget.events_left, state.budget.events_left)

    def test_gestures_reject_literal_and_reference_text(self):
        for name in GESTURES:
            with self.subTest(name=name):
                kind = ActionKind(name)
                for payload in ({'value': 'wrong'}, {'value_ref': 'wrong'}):
                    with self.assertRaises(ValueError):
                        ActionProposal('a', kind, 'target', 1, **payload)

    def test_text_observation_is_still_evidence_if_field_cannot_be_edited_afterward(self):
        state = create_goal_state('run', GoalSpec('goal', 'Enter title', ('text:field=Work',)), 1)
        action = ActionProposal('a', ActionKind.TEXT, 'field', 1, value='Work')
        pending = state.with_pending_action(action)
        before = PageSnapshot('run', 1, 'before', (WidgetSnapshot('field', 'input'),))
        rule = propose_rule(action, pending, before)
        observed = pending.apply_execution(ExecutionRecord('a', True, 1, 2, 'OBSERVED'))
        after = PageSnapshot('run', 2, 'after', (
            WidgetSnapshot('field', 'input', text='Work', supported_actions=()),
        ))
        self.assertEqual(assess(rule, observed, after, 'observed.json').status, 'PASS')

    def test_single_agent_only_accepts_observed_actions_and_sees_capabilities(self):
        step = 'swipe_left:target'
        state = create_goal_state('run', GoalSpec('goal', 'Browse tabs', (step,)), 1)
        widget = WidgetSnapshot('target', 'label', supported_actions=(ActionKind('SWIPE_LEFT'),))
        page = PageSnapshot('run', 1, 'page', (widget,))
        backend = FakeDecisionBackend({'single_agent': {
            'current_subgoal': step, 'action': {'kind': 'SWIPE_LEFT', 'widget_id': 'target'},
        }})
        proposal = SingleAgentDecisionAgent(backend).decide(state, page)
        self.assertEqual(proposal.action.kind.value, 'SWIPE_LEFT')
        self.assertEqual(backend.requests[0].payload['widgets'][0]['actions'], ['SWIPE_LEFT'])
        invalid_backend = FakeDecisionBackend({'single_agent': {
            'current_subgoal': step, 'action': {'kind': 'SWIPE_UP', 'widget_id': 'target'},
        }})
        with self.assertRaises(ResponseFormatError):
            SingleAgentDecisionAgent(invalid_backend).decide(state, page)
