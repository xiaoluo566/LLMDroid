import sys
from dataclasses import replace
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))

from maestro.cooperation import (Feedback, StepAssessment, collaborate_once, merge_assessment)
from maestro.schemas import (GoalSpec, PageSnapshot, WidgetSnapshot, ExecutionRecord, VariableProposal, ActionProposal, ActionKind, Budget)
from maestro.session import create_goal_state
from maestro.planner import FixedGoalPlanner
from maestro.data_agent import RuleBasedDataAgent


class CooperationTests(unittest.TestCase):
    def setUp(self):
        self.goal = GoalSpec('g', '创建并搜索', ('generate:title=item', 'text_ref:search=item'))
        self.state = create_goal_state('r', self.goal, 1)
        self.page = PageSnapshot('r', 1, 'p', (WidgetSnapshot('title', 'input'), WidgetSnapshot('search', 'input')))

    def observed(self):
        state = self.state.with_plan(FixedGoalPlanner().plan(self.state, self.page))
        action = ActionProposal('a', ActionKind.TEXT, 'title', 1, value='标题')
        state = state.with_pending_action(action).stage_variable(VariableProposal('item', '标题', 'a', 1))
        return state.apply_execution(ExecutionRecord('a', True, 1, 2, 'OBSERVED'))

    def feedback(self, category='DATA_FAILURE', **kwargs):
        args = dict(feedback_id='f', run_id='r', goal_id='g', action_id='a', state_version=2,
                    category=category, reason='页面拒绝', evidence_refs=('page:2',))
        args.update(kwargs)
        return Feedback(**args)

    def assessment(self, **kwargs):
        args = dict(run_id='r', goal_id='g', action_id='a', state_version=2,
                    rule_id='rule', oracle_revision=1, status='PASS', evidence_refs=('page:2',),
                    confirmed_variables=('item',))
        args.update(kwargs)
        return StepAssessment(**args)

    def test_proposed_variable_is_not_available_until_verified(self):
        observed = self.observed()
        self.assertIsNone(observed.get_variable('item'))

        confirmed = merge_assessment(observed, self.assessment())
        self.assertEqual(confirmed.get_variable('item'), '标题')
        self.assertEqual(confirmed.current_subgoal, 'text_ref:search=item')
        page = replace(self.page, state_version=2)
        out = collaborate_once(confirmed, page, FixedGoalPlanner(), RuleBasedDataAgent())
        self.assertEqual(out.result.action.value_ref, 'item')
        self.assertIsNone(observed.get_variable('item'))

    def test_observed_but_unassessed_action_cannot_repeat(self):
        out = collaborate_once(self.observed(), replace(self.page, state_version=2), None, None)
        self.assertEqual(out.result.kind.value, 'WAIT')
        self.assertIsNone(out.result.action)

    def test_assessment_rejects_wrong_binding_missing_evidence_and_replay(self):
        state = self.observed()
        for changes in ({'run_id':'other'}, {'goal_id':'other'}, {'action_id':'other'},
                        {'state_version':1}, {'evidence_refs':()}, {'rule_id':''},
                        {'confirmed_variables':('other',)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                merge_assessment(state, self.assessment(**changes))
        state = merge_assessment(state, self.assessment())
        with self.assertRaises(ValueError):
            merge_assessment(state, self.assessment())

    def test_failed_assessment_does_not_confirm_or_advance(self):
        state = merge_assessment(self.observed(), self.assessment(status='FAIL', confirmed_variables=()))
        self.assertIsNone(state.get_variable('item'))
        self.assertEqual(state.current_subgoal, 'generate:title=item')
        self.assertFalse(state.proposed_variables)

    def test_path_failure_routes_to_planner_without_action(self):
        state = self.observed()
        out = collaborate_once(state, replace(self.page, state_version=2), FixedGoalPlanner(),
                               RuleBasedDataAgent(), feedback=self.feedback('PATH_FAILURE'))
        self.assertEqual(out.route, 'planner')
        self.assertEqual(out.result.kind.value, 'OBSERVE_ONLY')
        self.assertEqual(out.state.budget.replans_left, 1)
        self.assertIsNone(out.state.pending_action_id)
        replay = collaborate_once(out.state, replace(self.page, state_version=2), FixedGoalPlanner(),
                                  RuleBasedDataAgent(), feedback=self.feedback('PATH_FAILURE'))
        self.assertEqual(replay.state.budget, out.state.budget)

    def test_oracle_gap_reaches_planner_observation(self):
        class Oracle:
            def evidence_requests(self, state, page, feedback):
                return ('detail:title',)
        out = collaborate_once(self.observed(), replace(self.page, state_version=2),
                               FixedGoalPlanner(), RuleBasedDataAgent(), oracle=Oracle(),
                               feedback=self.feedback('ORACLE_UNCERTAIN'))
        self.assertEqual(out.route, 'oracle')
        self.assertEqual(out.observation.evidence_requests, ('detail:title',))
        self.assertEqual(out.result.kind.value, 'OBSERVE_ONLY')

    def test_unknown_write_only_observes_without_calling_agents(self):
        state = self.observed()
        state = replace(state, pending_action_id='unknown', last_feedback='EXECUTION_UNKNOWN')
        out = collaborate_once(state, replace(self.page, state_version=2), None, None)
        self.assertEqual(out.result.kind.value, 'OBSERVE_ONLY')
        self.assertEqual(out.state.pending_action_id, 'unknown')

    def test_cross_run_or_stale_feedback_never_routes(self):
        for changes in ({'run_id':'other'}, {'goal_id':'other'}, {'state_version':1}, {'action_id':'other'}):
            out = collaborate_once(self.observed(), replace(self.page, state_version=2), None, None,
                                   feedback=self.feedback(**changes))
            self.assertEqual(out.result.kind.value, 'STOP')
            self.assertEqual(out.state.status.value, 'BLOCKED')

    def test_recovery_exhaustion_and_environment_failure_stop(self):
        state = replace(self.observed(), budget=Budget(replans_left=0))
        for category in ('PATH_FAILURE', 'ENV_FAILURE', 'AGENT_FAILURE'):
            out = collaborate_once(state, replace(self.page, state_version=2), FixedGoalPlanner(),
                                   RuleBasedDataAgent(), feedback=self.feedback(category))
            self.assertEqual(out.result.kind.value, 'STOP')
            self.assertNotEqual(out.state.status.value, 'ACTIVE')

    def test_end_to_end_generated_variable_confirmation_and_reference(self):
        from maestro.model_data import ModelDataAgent
        from maestro.decision_backend import FakeDecisionBackend
        backend = FakeDecisionBackend({'data': {'action': {'kind':'TEXT', 'widget_id':'title', 'value':'标题'},
                                                'variable': {'name':'item', 'value':'标题'}}})
        first = collaborate_once(self.state, self.page, FixedGoalPlanner(), ModelDataAgent(backend))
        self.assertEqual(first.result.kind.value, 'ACTION')
        self.assertIsNone(first.state.get_variable('item'))
        action_id = first.result.action.action_id
        observed = first.state.apply_execution(ExecutionRecord(action_id, True, 1, 2, 'OBSERVED'))
        confirmed = merge_assessment(observed, self.assessment(action_id=action_id))
        second = collaborate_once(confirmed, replace(self.page, state_version=2), FixedGoalPlanner(), ModelDataAgent(backend))
        self.assertEqual(second.result.action.value_ref, 'item')
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(second.state.budget.model_calls_left, 11)

    def test_rejected_value_is_inferred_without_b_repeating_value(self):
        from maestro.model_data import ModelDataAgent
        from maestro.decision_backend import FakeDecisionBackend
        backend = FakeDecisionBackend({'data': {'action': {'kind':'TEXT', 'widget_id':'title', 'value':'标题'},
                                                'variable': {'name':'item', 'value':'标题'}}})
        out = collaborate_once(self.observed(), replace(self.page, state_version=2), FixedGoalPlanner(),
                               ModelDataAgent(backend), feedback=self.feedback())
        self.assertEqual(out.result.kind.value, 'STOP')
        self.assertEqual(out.state.budget.model_calls_left, 11)

    def test_non_dispatched_action_cannot_confirm_variable(self):
        state = self.observed()
        fresh = self.state.with_plan(FixedGoalPlanner().plan(self.state, self.page))
        action = ActionProposal('a', ActionKind.TEXT, 'title', 1, value='标题')
        fresh = fresh.with_pending_action(action).stage_variable(VariableProposal('item', '标题', 'a', 1))
        state = fresh.apply_execution(ExecutionRecord('a', False, 1, 2, 'NOT_DISPATCHED'))
        with self.assertRaises(ValueError):
            merge_assessment(state, self.assessment())

    def test_generated_value_must_be_confirmed_before_advancing(self):
        with self.assertRaises(ValueError):
            merge_assessment(self.observed(), self.assessment(confirmed_variables=()))

    def test_rule_based_data_cannot_repeat_rejected_text(self):
        goal = GoalSpec('g', '输入', ('text:title=旧值',))
        state = create_goal_state('r', goal, 1)
        first = collaborate_once(state, self.page, FixedGoalPlanner(), RuleBasedDataAgent())
        observed = first.state.apply_execution(ExecutionRecord(first.result.action.action_id, True, 1, 2, 'OBSERVED'))
        out = collaborate_once(observed, replace(self.page, state_version=2), FixedGoalPlanner(),
                               RuleBasedDataAgent(), feedback=self.feedback(action_id=first.result.action.action_id))
        self.assertEqual(out.result.kind.value, 'STOP')

    def test_old_step_feedback_cannot_affect_next_step(self):
        state = merge_assessment(self.observed(), self.assessment())
        out = collaborate_once(state, replace(self.page, state_version=2), FixedGoalPlanner(), RuleBasedDataAgent(),
                               feedback=self.feedback())
        self.assertEqual(out.result.kind.value, 'STOP')

    def test_data_failure_can_propose_different_value(self):
        from maestro.model_data import ModelDataAgent
        from maestro.decision_backend import FakeDecisionBackend
        backend = FakeDecisionBackend({'data': {'action': {'kind':'TEXT', 'widget_id':'title', 'value':'新标题'},
                                                'variable': {'name':'item', 'value':'新标题'}}})
        out = collaborate_once(self.observed(), replace(self.page, state_version=2), FixedGoalPlanner(),
                               ModelDataAgent(backend), feedback=self.feedback())
        self.assertEqual(out.result.kind.value, 'ACTION')
        self.assertEqual(out.result.action.value, '新标题')
        self.assertEqual(out.state.proposed_variables[0].value, '新标题')

    def test_variable_cannot_be_staged_twice_or_overwrite_confirmed(self):
        observed = self.observed()
        candidate = observed.proposed_variables[0]
        state = replace(observed, pending_action_id='a', state_version=1)
        with self.assertRaises(ValueError):
            state.stage_variable(candidate)
        with self.assertRaises(ValueError):
            replace(state, proposed_variables=()).with_variable('item', 'old').stage_variable(candidate)

    def test_candidate_value_cannot_differ_from_actual_input(self):
        state = self.state.with_plan(FixedGoalPlanner().plan(self.state, self.page))
        state = state.with_pending_action(ActionProposal('a', ActionKind.TEXT, 'title', 1, value='实际'))
        for proposal in (VariableProposal('item', '另一值', 'a', 1),
                         VariableProposal('other', '实际', 'a', 1)):
            with self.assertRaises(ValueError):
                state.stage_variable(proposal)

    def test_planner_and_data_usage_are_summed_in_one_round(self):
        from maestro.planner import ModelGoalPlanner
        from maestro.model_data import ModelDataAgent
        from maestro.decision_backend import ModelResponse
        class Backend:
            def complete(self, request):
                data = ({'current_subgoal':'generate:title=item'} if request.role == 'planner' else
                        {'action':{'kind':'TEXT', 'widget_id':'title', 'value':'标题'},
                         'variable':{'name':'item', 'value':'标题'}})
                return ModelResponse(request.request_id, data, 10, 3)
        backend = Backend()
        out = collaborate_once(self.state, self.page, ModelGoalPlanner(backend), ModelDataAgent(backend))
        self.assertEqual(out.result.kind.value, 'ACTION')
        self.assertEqual((out.model_input_tokens, out.model_output_tokens), (20, 6))
        self.assertEqual(out.state.budget.model_calls_left, 10)

    def test_oracle_usage_and_read_only_observation_are_retained(self):
        from maestro.cooperation import EvidenceProposal
        class Oracle:
            uses_model = True
            def evidence_requests(self, state, page, feedback):
                return EvidenceProposal(('title',), 8, 2)
        out = collaborate_once(self.observed(), replace(self.page, state_version=2), FixedGoalPlanner(),
                               None, oracle=Oracle(), feedback=self.feedback('ORACLE_UNCERTAIN'))
        self.assertEqual((out.model_input_tokens, out.model_output_tokens), (8, 2))
        self.assertEqual(out.state.budget.model_calls_left, 11)

    def test_repeated_observation_gap_is_bounded(self):
        class Oracle:
            def evidence_requests(self, state, page, feedback):
                return ('title',)
        out = collaborate_once(self.observed(), replace(self.page, state_version=2), FixedGoalPlanner(),
                               None, oracle=Oracle(), feedback=self.feedback('ORACLE_UNCERTAIN'))
        again = collaborate_once(out.state, replace(self.page, state_version=2), FixedGoalPlanner(),
                                 None, oracle=Oracle(), feedback=self.feedback('ORACLE_UNCERTAIN', feedback_id='f2'))
        self.assertEqual(again.result.kind.value, 'STOP')

    def test_unknown_assessment_does_not_discard_candidate(self):
        state = merge_assessment(self.observed(), self.assessment(status='UNKNOWN', confirmed_variables=()))
        self.assertTrue(state.proposed_variables)
        self.assertEqual(state.current_subgoal, 'generate:title=item')
        confirmed = merge_assessment(state, self.assessment())
        self.assertEqual(confirmed.get_variable('item'), '标题')

    def test_generated_action_must_have_candidate_variable(self):
        from maestro.model_data import DataProposal
        class IncompleteData:
            def needs_model(self, state):
                return False
            def propose(self, state, page, feedback):
                return DataProposal(ActionProposal('a', ActionKind.TEXT, 'title', 1, value='标题'))
        out = collaborate_once(self.state, self.page, FixedGoalPlanner(), IncompleteData())
        self.assertEqual(out.result.kind.value, 'STOP')
        self.assertIsNone(out.state.pending_action_id)

    def test_read_only_observation_can_supply_new_version_for_assessment(self):
        from maestro.cooperation import refresh_observation
        state = refresh_observation(self.observed(), replace(self.page, state_version=3), 'a')
        self.assertEqual(state.state_version, 3)
        confirmed = merge_assessment(state, self.assessment(state_version=3))
        self.assertEqual(confirmed.get_variable('item'), '标题')
        for page, action in ((self.page, 'a'), (replace(self.page, run_id='other', state_version=4), 'a'),
                             (replace(self.page, state_version=4), 'other')):
            with self.assertRaises(ValueError):
                refresh_observation(state, page, action)

    def test_path_reobservation_allows_next_planner_cycle(self):
        from maestro.cooperation import refresh_observation
        from maestro.model_data import ModelDataAgent
        from maestro.decision_backend import FakeDecisionBackend
        routed = collaborate_once(self.observed(), replace(self.page, state_version=2), FixedGoalPlanner(), None,
                                  feedback=self.feedback('PATH_FAILURE'))
        state = refresh_observation(routed.state, replace(self.page, state_version=3), 'a')
        backend = FakeDecisionBackend({'data': {'action':{'kind':'TEXT', 'widget_id':'title', 'value':'标题'},
                                                'variable':{'name':'item', 'value':'标题'}}})
        out = collaborate_once(state, replace(self.page, state_version=3), FixedGoalPlanner(), ModelDataAgent(backend))
        self.assertEqual(out.result.kind.value, 'WAIT')
        self.assertEqual(len(backend.requests), 0)
        # 上一步候选先等待评估，不能以新action覆盖它。

    def test_persistent_constraints_apply_without_new_feedback(self):
        from maestro.model_data import ModelDataAgent
        from maestro.decision_backend import FakeDecisionBackend
        state = replace(self.state, input_constraints=(('generate:title=item', 'rejected_value', 'bad'),))
        backend = FakeDecisionBackend({'data': {'action':{'kind':'TEXT', 'widget_id':'title', 'value':'bad'},
                                                'variable':{'name':'item', 'value':'bad'}}})
        out = collaborate_once(state, self.page, FixedGoalPlanner(), ModelDataAgent(backend))
        self.assertEqual(out.result.kind.value, 'STOP')
        self.assertEqual(backend.requests[0].payload['constraints']['rejected_value'], 'bad')

    def test_candidate_to_two_steps_preserves_origin_budget_and_variable(self):
        from maestro.goal_selection import GoalCandidate, ModelGoalSelector
        from maestro.model_data import ModelDataAgent
        from maestro.decision_backend import FakeDecisionBackend
        backend = FakeDecisionBackend({
            'goal_selection': {'candidate_id':'c', 'subgoals':list(self.goal.subgoals)},
            'data': {'action':{'kind':'TEXT', 'widget_id':'title', 'value':'标题'},
                     'variable':{'name':'item', 'value':'标题'}},
        })
        candidate = GoalCandidate('r', 'c', 'p', '创建并搜索', 'states/p', 'cluster', self.page.widgets)
        selected = ModelGoalSelector(backend).select('r', (candidate,), Budget(), 'choose')
        state = selected.create_state(1)
        sent = []
        for version in (1, 2):
            out = collaborate_once(state, replace(self.page, state_version=version),
                                   FixedGoalPlanner(), ModelDataAgent(backend))
            self.assertEqual(out.result.kind.value, 'ACTION')
            sent.append(out.result.action)
            state = out.state.apply_execution(ExecutionRecord(sent[-1].action_id, True,
                                                              version, version + 1, 'OBSERVED'))
            assessment = StepAssessment('r', state.goal_id, sent[-1].action_id, version + 1,
                                        'aggregate-step', 1, 'PASS', (f'page:{version+1}',),
                                        ('item',) if version == 1 else ())
            state = merge_assessment(state, assessment)
        self.assertEqual(sent[1].value_ref, 'item')
        self.assertEqual(state.get_variable('item'), '标题')
        self.assertEqual(dict(state.goal_source)['candidate_id'], 'c')
        self.assertEqual(state.budget.model_calls_left, 10)
        self.assertEqual(state.status.value, 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
