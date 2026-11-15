import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))

from maestro.decision_backend import ModelResponse, ResponseFormatError
from maestro.goal_selection import GoalCandidate, ModelGoalSelector
from maestro.schemas import Budget, WidgetSnapshot
from maestro.session import create_goal_state


class Backend:
    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return ModelResponse(request.request_id, self.data, 20, 10)


class GoalSelectionTests(unittest.TestCase):
    def setUp(self):
        self.candidate = GoalCandidate(
            'run', 'candidate', 'page', '创建条目', 'states/page.json', 'cluster',
            (WidgetSnapshot('title', 'input'), WidgetSnapshot('save', 'button')),
        )

    def select(self, data, **kwargs):
        return ModelGoalSelector(Backend(data, **kwargs)).select(
            'run', (self.candidate,), Budget(), 'request-1')

    def test_selection_is_grounded_and_usable_by_session(self):
        result = self.select({'candidate_id': 'candidate', 'subgoals': [
            'generate:title=item_title', 'click:save']})
        self.assertIsNone(result.error)
        self.assertEqual(result.selection.candidate, self.candidate)
        self.assertEqual(result.selection.goal.description, '创建条目')
        state = create_goal_state('run', result.selection.goal, budget=result.budget)
        self.assertEqual(state.budget.model_calls_left, 11)
        self.assertEqual(result.model_input_tokens, 20)
        with self.assertRaises(FrozenInstanceError):
            result.selection.candidate.function = '换目标'

    def test_model_cannot_invent_candidate_or_state(self):
        for data in (
            {'candidate_id': 'fake', 'subgoals': ['click:save']},
            {'candidate_id': 'candidate', 'state_id': 'fake', 'subgoals': ['click:save']},
            {'candidate_id': 'candidate', 'target_cluster_id': 'fake', 'subgoals': ['click:save']},
        ):
            with self.subTest(data=data):
                result = self.select(data)
                self.assertIsNone(result.selection)
                self.assertEqual(result.budget.model_calls_left, 11)
                self.assertIsNotNone(result.error)

    def test_subgoals_must_be_supported_and_grounded(self):
        for steps in ([], 'click:save', ['navigate:anywhere'], ['click:missing'],
                      ['text:save=no'], ['text:title'], ['text_ref:title='],
                      ['generate:title=bad name'], ['click:title'], [None]):
            with self.subTest(steps=steps):
                result = self.select({'candidate_id': 'candidate', 'subgoals': steps})
                self.assertIsNone(result.selection)
                self.assertIsNotNone(result.error)

    def test_text_and_existing_references_are_supported(self):
        result = self.select({'candidate_id': 'candidate', 'subgoals': [
            'generate:title=item_title', 'text:title=测试计划',
            'text_ref:title=item_title', 'click:save']})
        self.assertEqual(len(result.selection.goal.subgoals), 4)

    def test_unknown_variable_and_forward_reference_are_rejected(self):
        for steps in (
            ['text_ref:title=item_title'],
            ['text_ref:title=item_title', 'generate:title=item_title'],
        ):
            result = self.select({'candidate_id': 'candidate', 'subgoals': steps})
            self.assertIsNone(result.selection)

    def test_duplicate_steps_and_variable_declarations_are_rejected(self):
        for steps in (
            ['click:save', 'click:save'],
            ['generate:title=item_title', 'generate:title=item_title'],
            ['generate:title=item_title', 'generate:save=item_title'],
        ):
            result = self.select({'candidate_id': 'candidate', 'subgoals': steps})
            self.assertIsNone(result.selection)

    def test_duplicate_declaration_on_different_editable_widget_is_rejected(self):
        candidate = GoalCandidate(
            'run', 'candidate', 'page', '创建条目', 'states/page.json', 'cluster',
            (WidgetSnapshot('title', 'input'), WidgetSnapshot('search', 'input')),
        )
        backend = Backend({'candidate_id': 'candidate', 'subgoals': [
            'generate:title=item_title', 'generate:search=item_title']})
        result = ModelGoalSelector(backend).select('run', (candidate,), Budget(), 'request')
        self.assertIsNone(result.selection)

    def test_create_state_preserves_origin_and_charged_budget(self):
        result = self.select({'candidate_id': 'candidate', 'subgoals': ['click:save']})
        state = result.create_state(state_version=7)
        self.assertEqual(state.run_id, 'run')
        self.assertEqual(state.state_version, 7)
        self.assertEqual(state.budget.model_calls_left, 11)
        self.assertEqual(dict(state.goal_source), {
            'candidate_id': 'candidate', 'state_id': 'page', 'cluster': 'cluster',
            'function': '创建条目', 'observation_ref': 'states/page.json',
            'request_id': 'request-1',
        })

    def test_failed_selection_cannot_create_state(self):
        with self.assertRaises(ValueError):
            self.select({}).create_state()

    def test_timeout_and_format_failure_keep_debited_budget(self):
        for error in (TimeoutError('sensitive'), ResponseFormatError('invalid', 3, 4)):
            backend = Backend(error=error)
            result = ModelGoalSelector(backend).select(
                'run', (self.candidate,), Budget(), 'request')
            self.assertEqual(result.budget.model_calls_left, 11)
            self.assertIsNone(result.selection)
            self.assertNotIn('sensitive', result.error)
            if isinstance(error, ResponseFormatError):
                self.assertEqual(result.model_output_tokens, 4)

    def test_invalid_inputs_do_not_call_model(self):
        backend = Backend()
        selector = ModelGoalSelector(backend)
        for run, candidates, budget, request in (
            ('other', (self.candidate,), Budget(), 'request'),
            ('run', (), Budget(), 'request'),
            ('run', (self.candidate, self.candidate), Budget(), 'request'),
            ('run', (self.candidate,), Budget(model_calls_left=0), 'request'),
            ('run', (self.candidate,), Budget(), ''),
        ):
            result = selector.select(run, candidates, budget, request)
            self.assertIsNone(result.selection)
            self.assertEqual(result.budget, budget)
        self.assertFalse(backend.requests)

    def test_response_request_must_match(self):
        class WrongBackend:
            def complete(self, request):
                return ModelResponse('wrong', {}, 2, 3)
        result = ModelGoalSelector(WrongBackend()).select(
            'run', (self.candidate,), Budget(), 'request')
        self.assertIsNone(result.selection)
        self.assertEqual(result.model_output_tokens, 3)

    def test_candidate_rejects_missing_metadata_and_duplicate_widgets(self):
        with self.assertRaises(ValueError):
            GoalCandidate('run', 'c', 's', '', 'obs', 'cluster')
        with self.assertRaises(ValueError):
            GoalCandidate('run', 'c', 's', 'function', 'obs', 'cluster',
                          (WidgetSnapshot('x', 'button'), WidgetSnapshot('x', 'button')))

    def test_request_carries_origin_and_timeout(self):
        backend = Backend({'candidate_id': 'candidate', 'subgoals': ['click:save']})
        budget = Budget()
        result = ModelGoalSelector(backend, timeout_seconds=4).select(
            'run', (self.candidate,), budget, 'request')
        request = backend.requests[0]
        self.assertEqual(request.payload['run_id'], 'run')
        self.assertEqual(request.payload['candidates'][0]['observation_ref'], 'states/page.json')
        self.assertEqual(request.timeout_seconds, 4)
        self.assertEqual(result.request_id, 'request')
        self.assertEqual(budget.model_calls_left, 12)

    def test_constructor_rejects_invalid_timeouts(self):
        for value in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                ModelGoalSelector(Backend(), value)

    def test_non_object_model_response_is_refused(self):
        result = self.select(['candidate'])
        self.assertIsNone(result.selection)
        self.assertEqual(result.model_input_tokens, 20)


if __name__ == '__main__':
    unittest.main()
