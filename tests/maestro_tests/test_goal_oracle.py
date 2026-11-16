import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))

from maestro.goal_oracle import (
    apply_goal_assessment, evaluate_goal, freeze_goal_contract,
)
from maestro.goal_selection import GoalCandidate
from maestro.schemas import (
    ActionKind, ActionProposal, ExecutionRecord, GoalSpec, GoalStatus,
    PageSnapshot, WidgetSnapshot,
)
from maestro.session import create_goal_state


class GoalOracleTests(unittest.TestCase):
    def setUp(self):
        self.target = WidgetSnapshot('log', 'button', 'Log', 'pkg:id/log')
        self.before = PageSnapshot('run', 1, 'before', (self.target,))
        self.candidate = GoalCandidate('run', 'c1', '1', "navigate to 'Log'",
                                       'states/1.json', '1', (self.target,),
                                       target_widget_id='log')
        self.state = replace(create_goal_state('run', GoalSpec(
            'goal', self.candidate.function, ('click:log',)), 1),
            current_subgoal='click:log')
        self.action = ActionProposal('click', ActionKind.CLICK, 'log', 1)

    def observed(self, action=None, dispatched=True):
        action = action or self.action
        return self.state.with_pending_action(action).apply_execution(ExecutionRecord(
            action.action_id, dispatched, 1, 2, 'OBSERVED' if dispatched else 'REJECTED'))

    def after(self, **changes):
        return replace(self.before, state_version=2,
                       widgets=(replace(self.target, selected=True, **changes),))

    def test_navigation_pass_requires_selected_destination_and_real_click(self):
        contract = freeze_goal_contract(self.candidate, 'goal', self.before)
        result = evaluate_goal(contract, self.observed(), self.after(), 'after.json')
        self.assertEqual(result.status, 'PASS')
        self.assertEqual(result.evidence_refs, ('after.json',))
        self.assertEqual((result.run_id, result.goal_id, result.action_id),
                         ('run', 'goal', 'click'))

    def test_checked_filter_can_pass_but_typing_category_cannot(self):
        candidate = replace(self.candidate, function='select category filter')
        state = replace(self.observed(), goal_description=candidate.function)
        contract = freeze_goal_contract(candidate, 'goal', self.before)
        after = replace(self.before, state_version=2,
                        widgets=(replace(self.target, checked=True),))
        self.assertEqual(evaluate_goal(contract, state, after, 'after.json').status, 'PASS')
        typed = replace(after, widgets=(replace(self.target, text='category'),))
        self.assertEqual(evaluate_goal(contract, state, typed, 'after.json').status, 'UNKNOWN')

    def test_ui_change_alone_is_insufficient(self):
        contract = freeze_goal_contract(self.candidate, 'goal', self.before)
        after = replace(self.before, state_version=2, state_id='different')
        self.assertEqual(evaluate_goal(contract, self.observed(), after, 'after.json').status,
                         'UNKNOWN')

    def test_previously_selected_target_is_not_new_achievement(self):
        before = replace(self.before, widgets=(replace(self.target, selected=True),))
        contract = freeze_goal_contract(self.candidate, 'goal', before)
        self.assertEqual(evaluate_goal(contract, self.observed(), self.after(), 'after.json').status,
                         'UNKNOWN')

    def test_unsupported_function_and_missing_anchor_remain_unknown(self):
        for candidate in (replace(self.candidate, function='add as filter'),
                          replace(self.candidate, function='open editor'),
                          replace(self.candidate, target_widget_id=None)):
            state = replace(self.observed(), goal_description=candidate.function)
            contract = freeze_goal_contract(candidate, 'goal', self.before)
            self.assertEqual(evaluate_goal(contract, state, self.after(), 'after.json').status,
                             'UNKNOWN')

    def test_text_enter_wrong_click_or_undispatched_action_cannot_pass(self):
        contract = freeze_goal_contract(self.candidate, 'goal', self.before)
        for action in (replace(self.action, kind=ActionKind.TEXT, value='category'),
                       replace(self.action, kind=ActionKind.ENTER),
                       replace(self.action, widget_id='search')):
            self.assertEqual(evaluate_goal(contract, self.observed(action), self.after(),
                                           'after.json').status, 'UNKNOWN')
        self.assertEqual(evaluate_goal(contract, self.observed(dispatched=False), self.after(),
                                       'after.json').status, 'UNKNOWN')

    def test_same_resource_cannot_rebind_a_different_stable_widget(self):
        contract = freeze_goal_contract(self.candidate, 'goal', self.before)
        after = self.after(widget_id='new-log')
        # Stable IDs include the original activity/class. A matching Android
        # resource and label on another screen does not identify that target.
        self.assertEqual(evaluate_goal(contract, self.observed(), after, 'after.json').status,
                         'UNKNOWN')
        for widgets in ((replace(after.widgets[0], text='Search'),),
                        (after.widgets[0], replace(after.widgets[0], widget_id='duplicate'))):
            ambiguous = replace(after, widgets=widgets)
            self.assertEqual(evaluate_goal(contract, self.observed(), ambiguous,
                                           'after.json').status, 'UNKNOWN')

    def test_changed_resource_or_kind_cannot_reuse_widget_id(self):
        contract = freeze_goal_contract(self.candidate, 'goal', self.before)
        for after in (self.after(resource_id='pkg:id/search'), self.after(widget_type='input')):
            self.assertEqual(evaluate_goal(contract, self.observed(), after, 'after.json').status,
                             'UNKNOWN')

    def test_stale_missing_or_foreign_observation_cannot_pass(self):
        contract = freeze_goal_contract(self.candidate, 'goal', self.before)
        for after, ref in ((None, 'after.json'), (self.after(), ''),
                           (replace(self.after(), state_version=1), 'after.json'),
                           (replace(self.after(), run_id='foreign'), 'after.json')):
            self.assertEqual(evaluate_goal(contract, self.observed(), after, ref).status,
                             'UNKNOWN')

    def test_foreign_goal_source_or_description_cannot_reuse_contract(self):
        contract = freeze_goal_contract(self.candidate, 'goal', self.before)
        for state in (replace(self.observed(), goal_id='different'),
                      replace(self.observed(), goal_description='open Search'),
                      replace(self.observed(), goal_source=(('candidate_id', 'other'),)),
                      replace(self.observed(), pending_action_id='pending')):
            self.assertEqual(evaluate_goal(contract, state, self.after(), 'after.json').status,
                             'UNKNOWN')

    def test_freeze_rejects_foreign_missing_or_modified_page_target(self):
        for before in (replace(self.before, run_id='foreign'),
                       replace(self.before, widgets=()),
                       replace(self.before, widgets=(replace(self.target, resource_id='other'),))):
            with self.assertRaises(ValueError):
                freeze_goal_contract(self.candidate, 'goal', before)

    def test_goal_pass_transition_is_bound_to_completed_verified_plan(self):
        contract = freeze_goal_contract(self.candidate, 'goal', self.before)
        state = self.observed()
        result = evaluate_goal(contract, state, self.after(), 'after.json')
        complete = replace(state, status=GoalStatus.UNKNOWN, current_subgoal=None,
                           evaluated_action_ids=('click',))
        finished = apply_goal_assessment(complete, result)
        self.assertEqual(finished.status, GoalStatus.PASS)
        self.assertEqual(complete.status, GoalStatus.UNKNOWN)
        self.assertEqual(finished.revision, complete.revision + 1)
        for invalid in (state, replace(complete, state_version=3),
                        replace(complete, evaluated_action_ids=()),
                        replace(complete, status=GoalStatus.FAIL)):
            with self.assertRaises(ValueError):
                apply_goal_assessment(invalid, result)
        with self.assertRaises(ValueError):
            apply_goal_assessment(complete, replace(result, status='UNKNOWN'))


if __name__ == '__main__':
    unittest.main()
