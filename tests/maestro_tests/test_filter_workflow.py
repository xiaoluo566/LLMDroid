import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))

from maestro.cooperation import collaborate_once, merge_assessment
from maestro.data_agent import RuleBasedDataAgent
from maestro.decision_backend import FakeDecisionBackend, ModelResponse, ResponseFormatError
from maestro.filter_workflow import (
    ModelFilterPlanner, append_filter_stage, assess_filter_goal, observe_filter_stage,
)
from maestro.android_oracle import assess, propose_rule
from maestro.goal_oracle import apply_goal_assessment
from maestro.planner import FixedGoalPlanner
from maestro.schemas import ActionKind, ActionProposal, ExecutionRecord, GoalSpec, GoalStatus, PageSnapshot, WidgetSnapshot
from maestro.session import create_goal_state


class FilterWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.entry = WidgetSnapshot('filter', 'button', 'Filter', 'pkg:id/filter_button')
        self.condition = WidgetSnapshot('once', 'button', 'One time', 'pkg:id/one_time', checkable=True)
        self.apply = WidgetSnapshot('apply', 'button', 'Save', 'pkg:id/apply_button')
        self.form = PageSnapshot('run', 1, 'form', (self.condition, self.apply))
        self.home = PageSnapshot('run', 0, 'home', (self.entry, WidgetSnapshot('old', 'label', 'Filters applied')))
        state = create_goal_state('run', GoalSpec('g', 'filter records', ('click:filter',)), 0)
        state = replace(state, goal_source=(('function', 'filter records'), ('target_widget_id', 'filter')))
        self.initial = self.execute(state, self.home, self.form)
        self.backend = FakeDecisionBackend({'planner_stage': {'condition_id': 'once', 'apply_widget_id': 'apply'}})

    @staticmethod
    def execute(state, before, after):
        outcome = collaborate_once(state, before, FixedGoalPlanner(), RuleBasedDataAgent())
        action = outcome.result.action
        if action is None:
            raise AssertionError(outcome.result.reason)
        rule = propose_rule(action, outcome.state, before)
        state = outcome.state.apply_execution(ExecutionRecord(action.action_id, True, before.state_version,
                                                              after.state_version, 'OBSERVED'))
        result = assess(rule, state, after, f'page-{after.state_version}.json')
        return merge_assessment(state, result)

    def progress(self):
        return ModelFilterPlanner(self.backend).choose(self.initial, self.form, 'filter', 'page-1.json')

    def next(self, state, progress, before, after):
        active = append_filter_stage(state, progress, before)
        complete = self.execute(active, before, after)
        updated = observe_filter_stage(progress, complete, after, f'page-{after.state_version}.json')
        return complete, updated

    def test_full_filter_applies_new_condition_and_reads_it_back_without_new_goal(self):
        progress = self.progress()
        selected = replace(self.form, state_version=2, widgets=(replace(self.condition, checked=True), self.apply))
        chosen, progress = self.next(self.initial, progress, self.form, selected)
        applied = replace(self.home, state_version=3)
        saved, progress = self.next(chosen, progress, selected, applied)
        reopened = replace(selected, state_version=4)
        done, progress = self.next(saved, progress, applied, reopened)
        verdict = assess_filter_goal(progress, done, reopened, 'page-4.json')
        self.assertEqual(verdict.status, 'PASS')
        final = apply_goal_assessment(done, verdict)
        self.assertEqual(final.status, GoalStatus.PASS)
        self.assertEqual(final.goal_id, self.initial.goal_id)
        self.assertEqual(final.goal_description, self.initial.goal_description)
        self.assertEqual(final.goal_source, self.initial.goal_source)
        self.assertEqual(final.subgoals[:1], self.initial.subgoals)
        self.assertEqual(len(final.subgoals), 4)
        self.assertEqual(final.budget.events_left, self.initial.budget.events_left - 3)
        self.assertEqual(final.budget.model_calls_left, self.initial.budget.model_calls_left)
        self.assertEqual(progress.evidence_refs, ('page-1.json', 'page-2.json', 'page-3.json', 'page-4.json'))
        self.assertEqual(self.backend.requests[0].role, 'planner_stage')

    def test_existing_filter_message_and_opening_form_cannot_pass(self):
        progress = self.progress()
        result = assess_filter_goal(progress, self.initial, self.form, 'page-1.json')
        self.assertEqual(result.status, 'UNKNOWN')
        self.assertIn('not verified', result.reason)

    def test_preselected_or_noncheckable_condition_and_unrelated_submit_are_rejected(self):
        for widgets in ((replace(self.condition, checked=True), self.apply),
                        (replace(self.condition, checkable=False), self.apply),
                        (self.condition, replace(self.apply, text='Delete', resource_id='pkg:id/delete'))):
            page = replace(self.form, widgets=widgets)
            with self.subTest(widgets=widgets), self.assertRaises(ResponseFormatError):
                ModelFilterPlanner(self.backend).choose(self.initial, page, 'filter', 'page-1.json')

    def test_model_cannot_add_keys_or_switch_the_request_or_controls(self):
        for payload in ({'condition_id': 'missing', 'apply_widget_id': 'apply'},
                        {'condition_id': 'once', 'apply_widget_id': 'apply', 'goal': 'different'}):
            backend = FakeDecisionBackend({'planner_stage': payload})
            with self.assertRaises(ResponseFormatError):
                ModelFilterPlanner(backend).choose(self.initial, self.form, 'filter', 'page-1.json')
        class WrongRequest(FakeDecisionBackend):
            def complete(self, request):
                return ModelResponse('other', {'condition_id': 'once', 'apply_widget_id': 'apply'})
        with self.assertRaises(ResponseFormatError):
            ModelFilterPlanner(WrongRequest()).choose(self.initial, self.form, 'filter', 'page-1.json')

    def test_append_preserves_history_and_refuses_skipped_phase_and_wrong_page_or_goal(self):
        progress = self.progress()
        active = append_filter_stage(self.initial, progress, self.form)
        self.assertEqual(active.completed_action_ids, self.initial.completed_action_ids)
        self.assertEqual(active.current_subgoal, 'stage/choose/click:once')
        self.assertEqual(active.budget, self.initial.budget)
        for state, candidate, page in (
            (self.initial, replace(progress, phase='apply'), self.form),
            (replace(self.initial, goal_id='other'), progress, self.form),
            (replace(self.initial, goal_description='open form'), progress, self.form),
            (active, progress, self.form),
            (self.initial, progress, replace(self.form, state_version=0)),
            (replace(self.initial, evaluated_action_ids=()), progress, self.form),
        ):
            with self.subTest(state=state, candidate=candidate), self.assertRaises(ValueError):
                append_filter_stage(state, candidate, page)

    def test_choosing_without_selected_result_does_not_advance(self):
        progress = self.progress()
        active = append_filter_stage(self.initial, progress, self.form)
        # Another visible label changed, but the chosen condition did not.
        other_change = replace(self.form, state_version=2,
                               widgets=self.form.widgets + (WidgetSnapshot('unrelated', 'label', 'new'),))
        complete = self.execute(active, self.form, other_change)
        with self.assertRaises(ValueError):
            observe_filter_stage(progress, complete, other_change, 'page-2.json')

    def test_apply_without_closing_form_and_reopen_without_persistence_fail(self):
        progress = self.progress()
        selected = replace(self.form, state_version=2, widgets=(replace(self.condition, checked=True), self.apply))
        chosen, progress = self.next(self.initial, progress, self.form, selected)
        # A changed text cannot substitute for successfully applying the form.
        still_form = replace(selected, state_version=3,
                             widgets=selected.widgets + (WidgetSnapshot('changed', 'label', 'new'),))
        state = self.execute(append_filter_stage(chosen, progress, selected), selected, still_form)
        with self.assertRaises(ValueError):
            observe_filter_stage(progress, state, still_form, 'page-3.json')
        applied = replace(self.home, state_version=3)
        saved, progress = self.next(chosen, progress, selected, applied)
        unpersisted = replace(self.form, state_version=4)
        state = self.execute(append_filter_stage(saved, progress, applied), applied, unpersisted)
        with self.assertRaises(ValueError):
            observe_filter_stage(progress, state, unpersisted, 'page-4.json')

    def test_choose_requires_actual_entry_click_and_evidence(self):
        for state, entry, ref in ((replace(self.initial, last_action=replace(self.initial.last_action, widget_id='other')), 'filter', 'p'),
                                  (self.initial, 'other', 'p'), (self.initial, 'filter', ''),
                                  (replace(self.initial, pending_action_id='pending'), 'filter', 'p')):
            with self.subTest(state=state, entry=entry, ref=ref), self.assertRaises(ValueError):
                ModelFilterPlanner(self.backend).choose(state, self.form, entry, ref)

    def test_changed_labels_cannot_disguise_a_form_that_remains_open(self):
        progress = self.progress()
        selected = replace(self.form, state_version=2, widgets=(replace(self.condition, checked=True), self.apply))
        chosen, progress = self.next(self.initial, progress, self.form, selected)
        changed = replace(self.form, state_version=3, widgets=(
            replace(self.condition, widget_id='changed-condition', text='Changed option', checked=True),
            replace(self.apply, widget_id='changed-submit', text='Done'), self.entry,
        ))
        done = self.execute(append_filter_stage(chosen, progress, selected), selected, changed)
        with self.assertRaises(ValueError):
            observe_filter_stage(progress, done, changed, 'page-3.json')

    def test_final_goal_cannot_reuse_foreign_goal_or_stale_readback(self):
        progress = self.progress()
        selected = replace(self.form, state_version=2, widgets=(replace(self.condition, checked=True), self.apply))
        chosen, progress = self.next(self.initial, progress, self.form, selected)
        applied = replace(self.home, state_version=3)
        saved, progress = self.next(chosen, progress, selected, applied)
        reopened = replace(selected, state_version=4)
        done, progress = self.next(saved, progress, applied, reopened)
        for state, page, evidence in ((replace(done, goal_id='other'), reopened, 'page-4.json'),
                                      (done, replace(reopened, state_version=3), 'page-4.json'),
                                      (done, reopened, ''),
                                      (replace(done, subgoals=self.initial.subgoals), reopened, 'page-4.json'),
                                      (replace(done, observed_action_ids=()), reopened, 'page-4.json')):
            self.assertEqual(assess_filter_goal(progress, state, page, evidence).status, 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
