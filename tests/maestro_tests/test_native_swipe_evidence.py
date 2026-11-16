import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))
from maestro.android_oracle import assess, propose_rule
from maestro.cooperation import merge_assessment
from maestro.decision_backend import FakeDecisionBackend, ResponseFormatError
from maestro.goal_agent import GoalObservation, ModelGoalOracle
from maestro.schemas import ActionKind, ActionProposal, ExecutionRecord, GoalState, PageSnapshot, WidgetSnapshot


class NativeSwipeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.action = ActionProposal('event', ActionKind.SWIPE_LEFT, 'tabs', 0)
        self.record = ExecutionRecord('event', True, 0, 1, 'OBSERVED')
        self.state = GoalState(
            'run', 'tabs', 0, subgoals=('swipe_left:tabs',), current_subgoal='swipe_left:tabs',
            goal_description='Scroll through category tabs',
        ).with_pending_action(self.action)
        self.target = WidgetSnapshot(
            'tabs', 'scroller', supported_actions=(ActionKind.SWIPE_LEFT,),
            bounds=(0, 10, 200, 60), scroll_axis='horizontal',
        )
        self.label = WidgetSnapshot('category', 'label', 'Work', bounds=[60, 17, 100, 47])
        self.outside = WidgetSnapshot('status', 'label', 'Status', bounds=(90, 70, 140, 120))
        self.before = PageSnapshot('run', 0, 'same-page', (self.target, self.label, self.outside))
        self.rule = propose_rule(self.action, self.state, self.before)
        self.executed = self.state.apply_execution(self.record)

    def _page(self, *, label=None, outside=None, target=None):
        return PageSnapshot('run', 1, 'same-page', (
            target or self.target, label or self.label, outside or self.outside,
        ))

    def _assess(self, after):
        return assess(self.rule, self.executed, after, 'maestro-observations/after.json')

    def test_same_widgets_shift_left_within_the_target(self):
        after = self._page(label=replace(self.label, bounds=(35, 17, 75, 47)))
        self.assertEqual(self.label.bounds, (60, 17, 100, 47))
        self.assertEqual(self._assess(after).status, 'PASS')

    def test_background_wrong_direction_jitter_and_whole_page_motion_are_not_swipes(self):
        for after in (
            self._page(),
            self._page(outside=replace(self.outside, bounds=(65, 70, 115, 120))),
            self._page(label=replace(self.label, bounds=(80, 17, 120, 47))),
            self._page(label=replace(self.label, bounds=(59, 17, 99, 47))),
            self._page(label=replace(self.label, bounds=(35, 17, 100, 47))),
            self._page(
                target=replace(self.target, bounds=(-25, 10, 175, 60)),
                label=replace(self.label, bounds=(35, 17, 75, 47)),
                outside=replace(self.outside, bounds=(65, 70, 115, 120)),
            ),
        ):
            with self.subTest(after=after.widgets):
                self.assertEqual(self._assess(after).status, 'UNKNOWN')

    def test_duplicate_label_has_no_stable_positional_match(self):
        duplicate = replace(self.label, widget_id='other', bounds=(65, 80, 100, 110))
        before = replace(self.before, widgets=self.before.widgets + (duplicate,))
        page = self._page(label=replace(self.label, bounds=(35, 17, 75, 47)))
        after = replace(page, widgets=page.widgets + (duplicate,))
        rule = propose_rule(self.action, self.state, before)
        self.assertEqual(assess(rule, self.executed, after, 'observed.json').status, 'UNKNOWN')

    def test_goal_can_cite_position_change_but_not_the_moving_outside_control(self):
        after = self._page(label=replace(self.label, bounds=(35, 17, 75, 47)),
                           outside=replace(self.outside, bounds=(65, 70, 115, 120)))
        reviewed = merge_assessment(self.executed, self._assess(after))
        observation = GoalObservation(
            'run', 'tabs', self.action, self.record, self.before, after,
            'maestro-observations/before.json', 'maestro-observations/after.json', 'events/swipe.json',
        )
        result = {'status': 'PASS', 'reason': 'The category tab visibly moved left',
                  'evidence_ids': ['after:0:category'], 'recapture': False}
        backend = FakeDecisionBackend({'goal_oracle': result})
        oracle = ModelGoalOracle(backend)
        verdict = oracle.assess(reviewed, (observation,), allow_recapture=False)
        self.assertEqual(verdict.assessment.status, 'PASS')
        facts = {fact['widget_id']: fact for fact in backend.requests[0].payload['observations'][0]['after']}
        self.assertEqual(facts['category']['change'], 'moved')
        self.assertEqual(facts['category']['motion']['delta_px'], -25)
        self.assertEqual(facts['status']['change'], 'unchanged')
        backend.responses['goal_oracle'] = {**result, 'evidence_ids': ['after:0:status']}
        with self.assertRaises(ResponseFormatError):
            oracle.assess(reviewed, (observation,), allow_recapture=False)
