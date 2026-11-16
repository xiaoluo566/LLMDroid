"""Real UTG policy transitions for MAESTRO's live arrival gate."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot'))
try:
    from droidbot.desc.action_type import ActionType
    from droidbot.desc.utg import Path as UtgPath, Step
    from droidbot.policy.utg_based_policy import Mode, UtgBasedInputPolicy
except ImportError:
    UtgBasedInputPolicy = None


@unittest.skipIf(UtgBasedInputPolicy is None, 'Native DroidBot dependencies unavailable')
class NavigationArrivalTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {'LLMDROID_MAESTRO': '0'}), patch(
            'droidbot.policy.utg_based_policy.LLMAgent',
        ):
            self.policy = UtgBasedInputPolicy(SimpleNamespace(humanoid=None), Mock(), False, 'time')
        self.policy.logger = Mock()
        self.runtime = self.policy.maestro_runtime = Mock()
        self.runtime.prepare.return_value = Mock()
        self.runtime.canary_finished = self.runtime.uncertain_write = False
        self.policy.current_state = Mock(get_id=Mock(return_value=11))
        self.policy._live_state = Mock()
        self.policy._UtgBasedInputPolicy__current_mode = Mode.NAVIGATE
        self.policy._UtgBasedInputPolicy__navigate_target = 1
        self.policy._UtgBasedInputPolicy__total_guide_times = 1
        self.policy._UtgBasedInputPolicy__current_path = self.path(ActionType.START)

    @staticmethod
    def path(kind):
        event = Mock(get_action_type=Mock(return_value=kind))
        return UtgPath(1, 0, [Step(1, event, 0)])

    def evolve(self):
        self.policy._UtgBasedInputPolicy__switch_mode()

    def mode(self):
        return self.policy._UtgBasedInputPolicy__current_mode

    def test_terminal_start_tries_alternate_then_accepts_live_anchor(self):
        self.runtime.arrival_problem.side_effect = ['anchor_missing', None]
        alternate = self.path(ActionType.START)
        alternate_event = alternate.steps[0].event
        self.policy._UtgBasedInputPolicy__paths = [alternate]

        self.evolve()

        self.runtime.arrival_problem.assert_called_once_with(self.policy._live_state)
        self.runtime.write.assert_called_once_with(
            'Planner', 'navigation_anchor_rejected', reason='anchor_missing',
            target_state_id=1, observed_state_id=11,
        )
        self.runtime.prepare.assert_not_called()
        self.assertEqual(self.mode(), Mode.NAVIGATE)
        self.assertIs(self.policy._UtgBasedInputPolicy__resolve_new_action(), alternate_event)

        self.evolve()

        self.runtime.prepare.assert_called_once_with(self.policy._live_state)
        self.runtime.write.assert_called_with(
            'Planner', 'navigation_result', arrived=True, target_state_id=1, observed_state_id=11,
        )
        self.assertEqual(self.mode(), Mode.TEST_FUNCTION)

    def test_exhausted_terminal_start_is_blocked_before_action_preparation(self):
        self.runtime.arrival_problem.return_value = 'anchor_missing'

        self.evolve()

        self.runtime.write.assert_called_with(
            'Planner', 'navigation_result', arrived=False, target_state_id=1,
            observed_state_id=11, reason='anchor_missing',
        )
        self.runtime.block_navigation.assert_called_once_with('anchor_missing')
        self.runtime.prepare.assert_not_called()
        self.assertEqual(self.mode(), Mode.EXPLORE)

    def test_alternate_route_failure_preserves_previous_anchor_rejection(self):
        self.runtime.arrival_problem.return_value = 'anchor_missing'
        self.policy._UtgBasedInputPolicy__paths = [self.path(ActionType.CLICK)]
        self.policy.current_state.compute_similarity.return_value = 0.2
        self.policy.utg.find_state_by_id = Mock(return_value=Mock())

        self.evolve()
        self.evolve()

        self.runtime.block_navigation.assert_called_once_with('anchor_missing')
        self.runtime.prepare.assert_not_called()
        self.assertEqual(self.mode(), Mode.EXPLORE)

    def test_matching_historical_id_still_checks_the_independent_live_page(self):
        self.runtime.select.return_value = SimpleNamespace(state_id='1', function='Confirm tutorial')
        self.runtime.arrival_problem.return_value = 'anchor_missing'
        self.policy.current_state.get_id.return_value = 1
        self.policy.utg.get_paths = Mock(return_value=[self.path(ActionType.START)])
        self.policy._UtgBasedInputPolicy__current_mode = Mode.ASK_GUIDANCE

        self.evolve()

        self.runtime.arrival_problem.assert_called_once_with(self.policy._live_state)
        self.policy.utg.get_paths.assert_called_once_with(target_state_id=1)
        self.runtime.prepare.assert_not_called()
        self.assertEqual(self.mode(), Mode.NAVIGATE)

    def test_maestro_tries_only_first_three_nonempty_paths_in_utg_order(self):
        self.runtime.select.return_value = SimpleNamespace(state_id='1', function='Confirm tutorial')
        self.runtime.arrival_problem.return_value = 'anchor_missing'
        paths = [self.path(ActionType.START) for _ in range(4)]
        first_events = [path.steps[0].event for path in paths[:3]]
        self.policy.utg.get_paths = Mock(return_value=[UtgPath(0, 0, []), *paths])
        self.policy._UtgBasedInputPolicy__current_mode = Mode.ASK_GUIDANCE

        self.evolve()
        for event in first_events:
            self.assertIs(self.policy._UtgBasedInputPolicy__resolve_new_action(), event)
            self.evolve()

        self.assertEqual(self.mode(), Mode.EXPLORE)
        self.assertEqual(self.runtime.arrival_problem.call_count, 3)
        self.runtime.block_navigation.assert_called_once_with('anchor_missing')
        self.runtime.prepare.assert_not_called()

    def test_missing_utg_path_blocks_without_marking_an_attempt(self):
        self.runtime.select.return_value = SimpleNamespace(state_id='1', function='Confirm tutorial')
        self.policy.utg.get_paths = Mock(return_value=[])
        self.policy._UtgBasedInputPolicy__current_mode = Mode.ASK_GUIDANCE

        self.evolve()

        self.runtime.write.assert_called_once_with(
            'Planner', 'navigation_result', arrived=False, target_state_id=1,
            observed_state_id=11, reason='no_known_utg_path',
        )
        self.runtime.block_navigation.assert_called_once_with('no_known_utg_path')
        self.runtime.finish.assert_not_called()
        self.runtime.prepare.assert_not_called()
        self.assertEqual(self.mode(), Mode.EXPLORE)

    def test_exhausted_routes_without_arrival_check_block_as_utg_failure(self):
        self.policy._UtgBasedInputPolicy__current_path = self.path(ActionType.CLICK)
        self.policy.current_state.compute_similarity.return_value = 0.2
        self.policy.utg.find_state_by_id = Mock(return_value=Mock())

        self.evolve()

        self.runtime.write.assert_called_once_with(
            'Planner', 'navigation_result', arrived=False, target_state_id=1,
            observed_state_id=11, reason='utg_paths_exhausted',
        )
        self.runtime.block_navigation.assert_called_once_with('utg_paths_exhausted')
        self.runtime.arrival_problem.assert_not_called()
        self.runtime.finish.assert_not_called()
        self.assertEqual(self.mode(), Mode.EXPLORE)

    def test_original_guidance_keeps_accepting_terminal_start(self):
        self.policy.maestro_runtime = None
        with patch.object(self.policy, '_UtgBasedInputPolicy__prepare_test_function') as prepare:
            self.evolve()

        prepare.assert_called_once_with()
        self.assertEqual(self.mode(), Mode.TEST_FUNCTION)
