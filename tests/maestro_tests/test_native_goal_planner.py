import unittest
from dataclasses import replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))

from maestro.cooperation import collaborate_once
from maestro.data_agent import RuleBasedDataAgent
from maestro.goal_selection import GoalCandidate, ModelGoalSelector
from maestro.planner import FixedGoalPlanner
from maestro.schemas import ActionKind, ActionProposal, Budget, GoalSpec, PageSnapshot, WidgetSnapshot
from maestro.session import create_goal_state
from tests.maestro_tests.test_goal_selection import Backend


class NativeGoalPlanningTests(unittest.TestCase):
    def candidate(self, actions, function, widget_type='gesture'):
        target = WidgetSnapshot('target', widget_type, supported_actions=actions)
        other = replace(target, widget_id='other')
        return GoalCandidate('run', 'candidate', 'page', function, 'states/page.json', 'cluster',
                             (target, other), target_widget_id='target')

    def select(self, candidate, *steps):
        backend = Backend({'candidate_id': 'candidate', 'subgoals': list(steps)})
        result = ModelGoalSelector(backend).select('run', (candidate,), Budget(), 'request')
        return result, backend.requests[0].payload['candidates'][0]

    def test_native_operations_are_advertised_planned_and_routed_to_data(self):
        for name, function in (
            ('LONG_PRESS', 'long press a plan'),
            ('SWIPE_LEFT', 'scroll through category tabs'),
            ('SWIPE_RIGHT', 'swipe right to reveal previous categories'),
            ('SWIPE_UP', 'scroll up through task list'),
            ('SWIPE_DOWN', 'scroll down through task list'),
        ):
            with self.subTest(action=name):
                kind = ActionKind(name)
                candidate = self.candidate((kind,), function)
                step = f'{name.lower()}:target'
                result, request = self.select(candidate, step)
                self.assertIsNotNone(result.selection, result.validation_error)
                self.assertEqual(request['widgets'][0]['supported_actions'], [name])
                page = PageSnapshot('run', 1, 'page', candidate.widgets)
                outcome = collaborate_once(result.create_state(1), page, FixedGoalPlanner(),
                                           RuleBasedDataAgent())
                self.assertIsNotNone(outcome.result.action, outcome.result.reason)
                self.assertEqual(outcome.result.action.kind, kind)
                self.assertEqual(outcome.result.action.widget_id, 'target')

    def test_selection_rejects_an_unavailable_direction_and_anchor_substitution(self):
        kind = ActionKind('SWIPE_LEFT')
        candidate = self.candidate((kind,), 'scroll through category tabs')
        for steps in (
            ('swipe_right:target',),
            ('swipe_left:other',),
            ('swipe_left:other', 'swipe_left:target'),
            ('swipe_left:target=arbitrary',),
        ):
            with self.subTest(steps=steps):
                result, _ = self.select(candidate, *steps)
                self.assertIsNone(result.selection)

    def test_clickable_long_press_anchor_can_choose_observed_long_press(self):
        press = ActionKind('LONG_PRESS')
        candidate = self.candidate((ActionKind.CLICK, press), 'long press plan options', 'button')
        result, _ = self.select(candidate, 'long_press:target')
        self.assertIsNotNone(result.selection, result.validation_error)
        result, _ = self.select(candidate, 'click:target')
        self.assertIsNone(result.selection)

    def test_explicit_direction_is_preserved_when_both_directions_are_possible(self):
        candidate = self.candidate((ActionKind('SWIPE_LEFT'), ActionKind('SWIPE_RIGHT')),
                                   'swipe right to reveal previous categories')
        result, _ = self.select(candidate, 'swipe_left:target')
        self.assertIsNone(result.selection)
        result, _ = self.select(candidate, 'swipe_right:target')
        self.assertIsNotNone(result.selection, result.validation_error)

    def test_content_scroll_direction_does_not_force_the_same_finger_direction(self):
        candidate = self.candidate((ActionKind.SWIPE_UP,), 'scroll down through plans')
        result, _ = self.select(candidate, 'swipe_up:target')
        self.assertIsNotNone(result.selection, result.validation_error)

    def test_input_anchor_can_be_clicked_when_native_state_exposes_that_click(self):
        candidate = self.candidate((ActionKind.CLICK, ActionKind.TEXT), 'enter plan title', 'input')
        result, _ = self.select(candidate, 'click:target', 'text:target=Plan')
        self.assertIsNotNone(result.selection, result.validation_error)

    def test_visible_gesture_can_prepare_ordinary_click_goal_but_not_navigation(self):
        candidate = self.candidate((ActionKind.CLICK,), 'activate plan', 'button')
        list_widget = WidgetSnapshot('other', 'gesture',
                                     supported_actions=(ActionKind('SWIPE_UP'),))
        candidate = replace(candidate, widgets=(candidate.widgets[0], list_widget))
        steps = ('swipe_up:other', 'click:target')
        result, _ = self.select(candidate, *steps)
        self.assertIsNotNone(result.selection, result.validation_error)
        result, _ = self.select(replace(candidate, function='navigate to plan'), *steps)
        self.assertIsNone(result.selection)

    def test_collaboration_rejects_data_substituting_a_different_allowed_action(self):
        left, right = ActionKind('SWIPE_LEFT'), ActionKind('SWIPE_RIGHT')
        target = WidgetSnapshot('target', 'gesture', supported_actions=(left, right))
        page = PageSnapshot('run', 1, 'page', (target,))
        state = create_goal_state('run', GoalSpec('goal', 'scroll tabs', ('swipe_left:target',)), 1)

        class WrongDirection(RuleBasedDataAgent):
            def propose(self, current, snapshot):
                return ActionProposal('wrong', right, 'target', snapshot.state_version)

        outcome = collaborate_once(state, page, FixedGoalPlanner(), WrongDirection())
        self.assertIsNone(outcome.result.action)
        self.assertIsNone(outcome.state.pending_action_id)
