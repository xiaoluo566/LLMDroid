import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'
sys.path.insert(0, str(PACKAGE_ROOT))

from maestro.decision_backend import FakeDecisionBackend, ModelResponse, ResponseFormatError
from maestro.model_data import ModelDataAgent
from maestro.planner import FixedGoalPlanner
from maestro.schemas import GoalSpec, PageSnapshot, WidgetSnapshot
from maestro.session import create_goal_state


class ModelDataTests(unittest.TestCase):
    def test_ascii_only_transport_rejects_unrepresentable_text(self):
        backend = FakeDecisionBackend({'data': self.response('家务')})
        state = replace(self.state, input_constraints=((self.state.current_subgoal, 'ascii_only', 'true'),))
        with self.assertRaises(ResponseFormatError):
            ModelDataAgent(backend).propose(state, self.page())

    def setUp(self):
        goal = GoalSpec('goal', '创建条目', ('generate:title=item_title',))
        self.state = create_goal_state('run', goal, 1)
        self.state = self.state.with_plan(FixedGoalPlanner().plan(self.state, self.page()))

    def page(self, widget_type='input'):
        return PageSnapshot('run', 1, 'page', (WidgetSnapshot('title', widget_type),))

    def response(self, value='测试计划'):
        return {'action': {'kind': 'TEXT', 'widget_id': 'title', 'value': value},
                'variable': {'name': 'item_title', 'value': value}}

    def test_generate_returns_candidate_and_bound_action_without_mutating_state(self):
        backend = FakeDecisionBackend({'data': self.response()})
        proposal = ModelDataAgent(backend).propose(self.state, self.page())
        self.assertEqual(proposal.action.value, '测试计划')
        self.assertEqual(proposal.variable.name, 'item_title')
        self.assertEqual(proposal.variable.action_id, proposal.action.action_id)
        self.assertEqual(proposal.variable.source_state_version, 1)
        self.assertIsNone(self.state.get_variable('item_title'))
        self.assertEqual(backend.requests[0].role, 'data')
        self.assertTrue(ModelDataAgent(backend).needs_model(self.state))

    def test_confirmed_reference_uses_no_model(self):
        goal = GoalSpec('goal', '搜索', ('text_ref:title=item_title',))
        state = create_goal_state('run', goal, 1).with_variable('item_title', '已有标题')
        state = replace(state, current_subgoal=goal.subgoals[0])
        backend = FakeDecisionBackend()
        proposal = ModelDataAgent(backend).propose(state, self.page())
        self.assertEqual(proposal.action.value_ref, 'item_title')
        self.assertIsNone(proposal.variable)
        self.assertFalse(ModelDataAgent(backend).needs_model(state))
        self.assertEqual(backend.requests, [])

    def test_missing_reference_does_not_generate_substitute(self):
        goal = GoalSpec('goal', '搜索', ('text_ref:title=item_title',))
        state = replace(
            create_goal_state('run', goal, 1), current_subgoal=goal.subgoals[0])
        backend = FakeDecisionBackend()
        with self.assertRaises(ValueError):
            ModelDataAgent(backend).propose(state, self.page())
        self.assertEqual(backend.requests, [])

    def test_fixed_click_and_text_do_not_call_model(self):
        for step in ('click:title', 'text:title=固定值'):
            with self.subTest(step=step):
                goal = GoalSpec('goal', '固定输入', (step,))
                state = replace(
                    create_goal_state('run', goal, 1), current_subgoal=step)
                backend = FakeDecisionBackend()
                self.assertIsNotNone(ModelDataAgent(backend).propose(state, self.page('text')).action)
                self.assertEqual(backend.requests, [])

    def test_generation_passes_only_public_context_and_feedback(self):
        state = self.state.with_variable('confirmed', '可信值')
        feedback = SimpleNamespace(category='DATA_FAILURE', reason='日期必须晚于今天',
                                   constraints=(('format', 'YYYY-MM-DD'),
                                                ('rejected_value', '2020-01-01')))
        backend = FakeDecisionBackend({'data': self.response('2026-12-01')})
        ModelDataAgent(backend).propose(state, self.page(), feedback)
        payload = backend.requests[0].payload
        self.assertEqual(payload['variables'], {'confirmed': '可信值'})
        self.assertIn('日期必须晚于今天', str(payload['feedback']))
        self.assertIn('YYYY-MM-DD', str(payload['feedback']))
        self.assertNotIn('history', payload)

    def test_rejected_value_cannot_be_resubmitted(self):
        feedback = SimpleNamespace(category='DATA_FAILURE', reason='字段拒绝',
                                   constraints=(('rejected_value', '测试计划'),))
        backend = FakeDecisionBackend({'data': self.response()})
        with self.assertRaises(ResponseFormatError):
            ModelDataAgent(backend).propose(self.state, self.page(), feedback)

    def test_invalid_generated_outputs_are_rejected(self):
        mutations = [
            {'action': {'kind': 'CLICK', 'widget_id': 'title'}},
            {'action': {'kind': 'TEXT', 'widget_id': 'other', 'value': 'x'}},
            {'action': {'kind': 'TEXT', 'widget_id': 'title', 'value': 3}},
            {'action': {'kind': 'TEXT', 'widget_id': 'title', 'value': 'x', 'value_ref': 'x'}},
            {'variable': {'name': 'other', 'value': '测试计划'}},
            {'variable': {'name': 'item_title', 'value': '不一致'}},
            {'variable': None},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                response = self.response()
                response.update(mutation)
                with self.assertRaises(ResponseFormatError):
                    ModelDataAgent(FakeDecisionBackend({'data': response})).propose(
                        self.state, self.page())

    def test_usage_survives_invalid_response_and_id_mismatch(self):
        for mismatch in (True, False):
            class Backend:
                def complete(self, request):
                    return ModelResponse('other' if mismatch else request.request_id,
                                         {}, 20, 5)
            with self.assertRaises(ResponseFormatError) as error:
                ModelDataAgent(Backend()).propose(self.state, self.page())
            self.assertEqual((error.exception.input_tokens, error.exception.output_tokens), (20, 5))

    def test_usage_is_returned_on_success(self):
        response = self.response()
        class Backend:
            def complete(self, request):
                return ModelResponse(request.request_id, response, 20, 5)
        proposal = ModelDataAgent(Backend()).propose(self.state, self.page())
        self.assertEqual((proposal.model_input_tokens, proposal.model_output_tokens), (20, 5))

    def test_invalid_context_is_rejected_before_call(self):
        backend = FakeDecisionBackend()
        agent = ModelDataAgent(backend)
        for page in (self.page('button'), PageSnapshot('other', 1, 'page'),
                     PageSnapshot('run', 2, 'page')):
            with self.subTest(page=page):
                with self.assertRaises(ValueError):
                    agent.propose(self.state, page)
        self.assertEqual(backend.requests, [])

    def test_invalid_generate_expression_is_rejected(self):
        for step in ('generate:title', 'generate:title=', 'generate:=variable'):
            state = replace(
                self.state, subgoals=(step,), current_subgoal=step)
            with self.subTest(step=step):
                with self.assertRaises(ValueError):
                    ModelDataAgent(FakeDecisionBackend()).propose(state, self.page())

    def test_timeout_must_be_positive(self):
        with self.assertRaises(ValueError):
            ModelDataAgent(FakeDecisionBackend(), timeout_seconds=0)

    def test_program_checks_date_and_length_constraints(self):
        cases = (
            ('2026-02-30', (('date_format', 'YYYY-MM-DD'),)),
            ('2026-2-03', (('date_format', 'YYYY-MM-DD'),)),
            ('2025-12-01', (('min_date', '2026-01-01'),)),
            ('2027-01-01', (('max_date', '2026-12-31'),)),
            ('abcd', (('max_length', '3'),)),
            ('ab', (('min_length', '3'),)),
        )
        for value, constraints in cases:
            with self.subTest(value=value, constraints=constraints):
                feedback = SimpleNamespace(reason='字段约束', constraints=constraints)
                with self.assertRaises(ResponseFormatError):
                    ModelDataAgent(FakeDecisionBackend({'data': self.response(value)})).propose(
                        self.state, self.page(), feedback)

    def test_date_bounds_and_length_boundaries_are_inclusive(self):
        constraints = (('date_format', 'YYYY-MM-DD'), ('min_date', '2026-01-01'),
                       ('max_date', '2026-12-31'), ('min_length', '10'), ('max_length', '10'))
        for value in ('2026-01-01', '2026-12-31'):
            feedback = SimpleNamespace(reason='字段约束', constraints=constraints)
            proposal = ModelDataAgent(FakeDecisionBackend({'data': self.response(value)})).propose(
                self.state, self.page(), feedback)
            self.assertEqual(proposal.action.value, value)

    def test_invalid_constraint_configuration_fails_closed(self):
        for constraints in ((('date_format', 'dd/mm/yyyy'),), (('min_date', 'bad'),),
                            (('max_length', '-1'),), (('min_length', 'abc'),)):
            feedback = SimpleNamespace(reason='字段约束', constraints=constraints)
            with self.subTest(constraints=constraints):
                with self.assertRaises(ResponseFormatError):
                    ModelDataAgent(FakeDecisionBackend({'data': self.response()})).propose(
                        self.state, self.page(), feedback)


if __name__ == '__main__':
    unittest.main()
