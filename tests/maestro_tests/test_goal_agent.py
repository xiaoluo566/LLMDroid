import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))
from maestro.decision_backend import FakeDecisionBackend, ModelResponse, ResponseFormatError
from maestro.goal_agent import GoalObservation, ModelGoalOracle
from maestro.goal_oracle import apply_goal_assessment
from maestro.schemas import (ActionKind, ActionProposal, ExecutionRecord, GoalState,
                             GoalStatus, PageSnapshot, WidgetSnapshot)


class ModelGoalOracleTests(unittest.TestCase):
    def setUp(self):
        self.widget = WidgetSnapshot('plus', 'button', '+')
        self.start = PageSnapshot('run', 0, 'closed', (self.widget,))
        self.new = WidgetSnapshot('new-task', 'button', 'New task')
        self.open = PageSnapshot('run', 1, 'open', (self.widget, self.new))
        self.action = ActionProposal('tap', ActionKind.CLICK, 'plus', 0)
        self.record = ExecutionRecord('tap', True, 0, 1, 'OBSERVED')
        self.state = GoalState(
            'run', 'expand', 1, status=GoalStatus.UNKNOWN, subgoals=('click:plus',),
            goal_description='Expand floating action menu', variables=(('task', 'Reading'),),
            goal_source=(('candidate_id', 'original'), ('function', 'Expand floating action menu')),
            last_action=self.action, completed_action_ids=('tap',), observed_action_ids=('tap',),
            evaluated_action_ids=('tap',), action_subgoals=(('tap', 'click:plus'),),
        )
        self.observation = GoalObservation(
            'run', 'expand', self.action, self.record, self.start, self.open,
            'maestro-observations/before-0.json', 'maestro-observations/page-1.json',
            'events/event_a.json',
        )
        self.backend = FakeDecisionBackend()
        self.oracle = ModelGoalOracle(self.backend)

    def response(self, status, ids=(), *, reason='The requested menu opened', recapture=False):
        self.backend.responses['goal_oracle'] = {
            'status': status, 'reason': reason, 'evidence_ids': list(ids), 'recapture': recapture,
        }

    def assess(self, *, state=None, observations=None, allow_recapture=True):
        return self.oracle.assess(state or self.state,
                                  (self.observation,) if observations is None else observations,
                                  allow_recapture=allow_recapture)

    def test_unlisted_goal_can_pass_and_reuses_goal_state(self):
        self.response('PASS', ('after:0:new-task',))
        verdict = self.assess().assessment
        request = self.backend.requests[0]
        self.assertEqual(request.role, 'goal_oracle')
        self.assertEqual(request.payload['goal'], self.state.goal_description)
        self.assertEqual(request.payload['variables'], {'task': 'Reading'})
        self.assertEqual(set(verdict.evidence_refs), {'maestro-observations/before-0.json',
                                                       'maestro-observations/page-1.json',
                                                       'events/event_a.json'})
        self.assertEqual(apply_goal_assessment(self.state, verdict).status, GoalStatus.PASS)

    def test_pass_needs_registered_latest_new_evidence(self):
        for ids in ((), ('not-real',), ('before:0:plus',), ('after:0:plus',),
                    ('after:0:new-task', 'not-real'), ('after:0:new-task', 'after:0:new-task')):
            with self.subTest(ids=ids):
                self.response('PASS', ids)
                with self.assertRaises(ResponseFormatError):
                    self.assess()

    def test_renamed_ids_or_page_hash_without_changed_ui_do_not_pass(self):
        self.response('PASS', ('after:0:plus-elsewhere',))
        same_view = replace(self.widget, widget_id='plus-elsewhere')
        after = replace(self.open, widgets=(same_view,))
        with self.assertRaises(ResponseFormatError):
            self.assess(observations=(replace(self.observation, after=after),))

    def test_observed_removal_can_be_goal_evidence(self):
        self.response('PASS', ('removed:0:plus',))
        blank = replace(self.observation, after=replace(self.open, widgets=()))
        with self.assertRaises(ResponseFormatError):
            self.assess(observations=(blank,))
        result = self.assess(observations=(replace(self.observation, after=replace(self.open, widgets=(self.new,))),))
        self.assertEqual(result.assessment.status, 'PASS')
        self.assertEqual(set(result.assessment.evidence_refs), {
            'maestro-observations/before-0.json', 'maestro-observations/page-1.json', 'events/event_a.json',
        })

    def test_hidden_text_suffix_cannot_be_cited_as_visible_change(self):
        prefix = 'x' * 200
        before = replace(self.start, widgets=(replace(self.widget, text=prefix + 'before'),))
        after = replace(self.open, widgets=(replace(self.widget, text=prefix + 'after'),))
        self.response('PASS', ('after:0:plus',))
        with self.assertRaises(ResponseFormatError):
            self.assess(observations=(replace(self.observation, before=before, after=after),))

    def test_fail_needs_a_real_post_action_fact(self):
        self.response('FAIL', ('after:0:plus',), reason='The app reports the menu cannot open')
        self.assertEqual(self.assess().assessment.status, 'FAIL')
        self.response('FAIL', ('before:0:plus',))
        with self.assertRaises(ResponseFormatError):
            self.assess()

    def test_unknown_can_have_no_citations_and_ask_for_a_bounded_recapture(self):
        self.response('UNKNOWN', recapture=True, reason='The overlay may still be animating')
        decision = self.assess()
        self.assertTrue(decision.recapture)
        self.assertEqual(decision.assessment.status, 'UNKNOWN')
        self.assertTrue(self.backend.requests[0].payload['allow_recapture'])
        with self.assertRaises(ResponseFormatError):
            self.assess(allow_recapture=False)
        self.response('PASS', ('after:0:new-task',), recapture=True)
        with self.assertRaises(ResponseFormatError):
            self.assess()

    def test_goal_and_native_action_provenance_checked_before_request(self):
        for bad in (replace(self.observation, goal_id='different'),
                    replace(self.observation, before=replace(self.start, run_id='different')),
                    replace(self.observation, record=replace(self.record, dispatched=False)),
                    replace(self.observation, record=replace(self.record, before_state_version=1)),
                    replace(self.observation, event_ref='events/../foreign.json'),
                    replace(self.observation, after=replace(self.open, state_version=2))):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.assess(observations=(bad,))
        for bad_state in (replace(self.state, current_subgoal='click:plus'),
                          replace(self.state, pending_action_id='next'),
                          replace(self.state, evaluated_action_ids=()),
                          replace(self.state, observed_action_ids=())):
            with self.assertRaises(ValueError):
                self.assess(state=bad_state)
        self.assertFalse(self.backend.requests)

    def test_recaptured_page_refs_are_bound_to_same_native_action(self):
        self.response('PASS', ('after:0:new-task',))
        observation = replace(self.observation, after=replace(self.open, state_version=2),
                              after_ref='maestro-observations/page-2.json')
        result = self.assess(state=replace(self.state, state_version=2), observations=(observation,),
                             allow_recapture=False)
        self.assertEqual(result.assessment.state_version, 2)
        self.assertEqual(result.assessment.action_id, 'tap')
        self.assertIn('maestro-observations/page-2.json', result.assessment.evidence_refs)

    def test_all_actions_remain_in_the_goal_history_and_persisted_results_can_pass(self):
        tap_item = ActionProposal('tap-item', ActionKind.CLICK, 'new-task', 1)
        final_page = replace(self.open, state_id='form', state_version=2)
        second = GoalObservation(
            'run', 'expand', tap_item, ExecutionRecord('tap-item', True, 1, 2, 'OBSERVED'),
            self.open, final_page, 'maestro-observations/before-1.json',
            'maestro-observations/page-2.json', 'events/event_b.json',
        )
        both = ('tap', 'tap-item')
        state = replace(self.state, state_version=2, last_action=tap_item,
                        subgoals=('click:plus', 'click:new-task'),
                        completed_action_ids=both, observed_action_ids=both,
                        evaluated_action_ids=both,
                        action_subgoals=(('tap', 'click:plus'), ('tap-item', 'click:new-task')))
        self.response('PASS', ('after:1:new-task',))
        result = self.assess(state=state, observations=(self.observation, second)).assessment
        self.assertEqual(result.status, 'PASS')
        self.assertIn('maestro-observations/before-0.json', result.evidence_refs)
        self.assertIn('maestro-observations/before-1.json', result.evidence_refs)
        self.response('PASS', ('after:0:new-task',))
        with self.assertRaises(ResponseFormatError):
            self.assess(state=state, observations=(self.observation, second))
        sent = len(self.backend.requests)
        for invalid in ((second,), (self.observation, replace(second, event_ref=self.observation.event_ref))):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.assess(state=state, observations=invalid)
                self.assertEqual(len(self.backend.requests), sent)

    def test_pass_cannot_cite_a_truncated_fact(self):
        widgets = tuple(WidgetSnapshot(f'extra{i}', 'button', f'Item {i}') for i in range(200))
        self.response('UNKNOWN')
        observation = replace(self.observation, after=replace(self.open, widgets=widgets))
        self.assess(observations=(observation,))
        facts = self.backend.requests[0].payload['observations'][-1]['after']
        presented = {fact['id'] for fact in facts}
        omitted = next(f'after:0:{widget.widget_id}' for widget in widgets
                       if f'after:0:{widget.widget_id}' not in presented)
        self.response('PASS', (omitted,))
        with self.assertRaises(ResponseFormatError):
            self.assess(observations=(observation,))

    def test_schema_validation_rejects_instructions_and_stale_responses(self):
        for response in ({'status': 'PASS', 'reason': '', 'evidence_ids': [], 'recapture': False},
                         {'status': 'UNKNOWN', 'reason': 'Missing', 'evidence_ids': [], 'recapture': 1},
                         {'status': 'UNKNOWN', 'reason': 'Missing', 'evidence_ids': [], 'recapture': False,
                          'command': 'tap'}):
            self.backend.responses['goal_oracle'] = response
            with self.assertRaises(ResponseFormatError):
                self.assess()
        self.backend.complete = lambda request: ModelResponse('stale', {'status': 'UNKNOWN'})
        with self.assertRaises(ResponseFormatError):
            self.assess()


if __name__ == '__main__':
    unittest.main()
