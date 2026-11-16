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

    def goal_page(self, function, target_text='入门', extra_before=(), resource_id=None):
        target = replace(self.target, text=target_text, resource_id=resource_id or self.target.resource_id)
        before = replace(self.before, widgets=(target,) + extra_before)
        candidate = replace(self.candidate, function=function, widgets=before.widgets)
        state = replace(self.observed(), goal_description=function)
        return freeze_goal_contract(candidate, 'goal', before), state

    @staticmethod
    def tutorial_widgets():
        return (WidgetSnapshot('title', 'label', '教程', 'pkg:id/title'),
                WidgetSnapshot('one', 'button', '创建分类', 'pkg:id/tutorial_list_field'),
                WidgetSnapshot('two', 'button', '管理类别', 'pkg:id/tutorial_list_field'))

    def test_get_started_arrival_requires_new_tutorial_title_and_entry_structure(self):
        contract, state = self.goal_page('get started (入门)')
        self.assertEqual(contract.kind, 'tutorial_list')
        after = replace(self.before, state_version=2, widgets=self.tutorial_widgets())
        self.assertEqual(evaluate_goal(contract, state, after, 'after.json').status, 'PASS')
        for widgets in (self.tutorial_widgets()[:1], self.tutorial_widgets()[1:],
                        (replace(self.tutorial_widgets()[0], widget_type='input'),)
                        + self.tutorial_widgets()[1:]):
            self.assertEqual(evaluate_goal(contract, state, replace(after, widgets=widgets),
                                           'after.json').status, 'UNKNOWN')

    def test_explicit_tutorial_list_wording_and_localized_entry_label(self):
        for function in ('open tutorial list', 'show tutorials', 'open getting started'):
            contract, state = self.goal_page(function)
            self.assertEqual(contract.kind, 'tutorial_list', function)
        contract, _ = self.goal_page('complete all tutorials')
        self.assertEqual(contract.kind, 'unsupported')

    def test_onboarding_alias_keeps_original_goal_and_requires_tutorial_arrival(self):
        after = replace(self.before, state_version=2, widgets=self.tutorial_widgets())
        wizard = replace(after, widgets=(
            WidgetSnapshot('wizard-title', 'label', 'Welcome', 'pkg:id/title'),
            WidgetSnapshot('next', 'button', 'Next', 'pkg:id/next_button')))
        for function, label in (('start onboarding', '入门'), ('open onboarding', 'Get started')):
            contract, state = self.goal_page(function, target_text=label)
            self.assertEqual(contract.function, function)
            self.assertEqual(contract.kind, 'tutorial_list')
            self.assertEqual(evaluate_goal(contract, state, after, 'after.json').status, 'PASS')
            self.assertEqual(evaluate_goal(contract, state, wizard, 'after.json').status, 'UNKNOWN')

    def test_onboarding_alias_without_grounded_entry_label_is_unsupported(self):
        contract, _ = self.goal_page('open onboarding', target_text='Continue')
        self.assertEqual(contract.kind, 'unsupported')

    def test_existing_tutorial_title_does_not_establish_new_arrival(self):
        contract, state = self.goal_page('open tutorials', extra_before=self.tutorial_widgets())
        after = replace(self.before, state_version=2, widgets=self.tutorial_widgets())
        self.assertEqual(evaluate_goal(contract, state, after, 'after.json').status, 'UNKNOWN')

    def test_same_title_with_a_different_resource_is_still_preexisting(self):
        contract, state = self.goal_page('open Settings', target_text='Settings', extra_before=(
            WidgetSnapshot('old-title', 'label', 'Settings', 'pkg:id/old_title'),))
        after = replace(self.before, state_version=2, widgets=(
            WidgetSnapshot('new-title', 'label', 'Settings', 'pkg:id/title'),))
        self.assertEqual(evaluate_goal(contract, state, after, 'after.json').status, 'UNKNOWN')

    def test_open_view_requires_new_matching_title_not_input_or_unrelated_title(self):
        contract, state = self.goal_page('open Settings', target_text='Settings')
        after = replace(self.before, state_version=2, widgets=(
            WidgetSnapshot('new-title', 'label', 'Settings', 'pkg:id/toolbar_label'),))
        self.assertEqual(contract.kind, 'open_view')
        self.assertEqual(evaluate_goal(contract, state, after, 'after.json').status, 'PASS')
        for widget in (replace(after.widgets[0], text='Search'),
                       replace(after.widgets[0], widget_type='input'),
                       replace(after.widgets[0], resource_id='pkg:id/value'),
                       replace(after.widgets[0], widget_id='log')):
            self.assertEqual(evaluate_goal(contract, state, replace(after, widgets=(widget,)),
                                           'after.json').status, 'UNKNOWN')

    def test_tutorial_entry_cannot_pass_without_matching_visual_evidence(self):
        contract, state = self.goal_page("open 'create category' tutorial", target_text='创建分类',
                                         extra_before=self.tutorial_widgets()[:1],
                                         resource_id='pkg:id/tutorial_list_field')
        self.assertEqual(contract.kind, 'tutorial_open')
        self.assertEqual(evaluate_goal(contract, state, self.after(), 'after.json').status, 'UNKNOWN')

    def test_tutorial_open_requires_real_tutorial_list_source(self):
        contract, state = self.goal_page("open 'create category' tutorial", target_text='创建分类')
        self.assertEqual(contract.kind, 'unsupported')
        self.assertEqual(evaluate_goal(contract, state, self.after(), 'after.json').status, 'UNKNOWN')

    def test_filter_intent_is_frozen_but_needs_its_own_workflow_evidence(self):
        contract, state = self.goal_page('filter records', target_text='过滤')
        self.assertEqual(contract.kind, 'apply_filter')
        self.assertEqual(evaluate_goal(contract, state, self.after(), 'after.json').status, 'UNKNOWN')

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
