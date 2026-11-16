"""Native DroidBot contract tests for dispatch and callbacks."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot'))
try:
    from droidbot.input_event import EventLog
    from droidbot.input_manager import InputManager
    from droidbot.policy.utg_based_policy import UtgBasedInputPolicy
    from droidbot.policy.input_policy import InputInterruptedException
except ImportError:
    EventLog = InputManager = None


@unittest.skipIf(EventLog is None, 'Native DroidBot dependencies unavailable')
class DispatchTests(unittest.TestCase):
    def test_failed_canary_selection_does_not_send_an_extra_exploration_event(self):
        policy = UtgBasedInputPolicy.__new__(UtgBasedInputPolicy)
        policy.maestro_runtime = SimpleNamespace(canary_finished=True, uncertain_write=False)
        with self.assertRaises(InputInterruptedException):
            policy._UtgBasedInputPolicy__resolve_new_action()

    def test_eventlog_rejection_does_not_send_or_start_profiling(self):
        event = Mock()
        device = Mock()
        log = EventLog.__new__(EventLog)
        log.device, log.event = device, event
        log.start_profiling = Mock()
        log.start(before_send=lambda event, before: False)
        self.assertFalse(log.dispatched)
        log.start_profiling.assert_not_called()
        device.send_event.assert_not_called()

    def test_eventlog_sets_dispatch_only_after_actual_send(self):
        event, device = Mock(), Mock()
        log = EventLog.__new__(EventLog)
        log.device, log.event = device, event
        log.start_profiling = Mock()
        log.start(before_send=lambda event, before: True)
        self.assertTrue(log.dispatched)
        device.send_event.assert_called_once_with(event)
        device.send_event.side_effect = RuntimeError('device unavailable')
        with self.assertRaises(RuntimeError):
            log.start(before_send=lambda event, before: True)
        self.assertFalse(log.dispatched)

    def manager(self, runtime):
        instance = InputManager.__new__(InputManager)
        instance.device = SimpleNamespace(pause_sending_event=False)
        instance.app = Mock()
        instance.events = []
        instance.event_interval = 0
        instance.profiling_method = None
        instance.policy = SimpleNamespace(maestro_runtime=runtime, last_event='event', last_state='state')
        return instance

    def test_rejected_action_does_not_pollute_utg_or_visit_count(self):
        runtime = Mock()
        runtime.owns_event.return_value = True
        manager = self.manager(runtime)
        log = Mock(dispatched=False)
        event = Mock()
        with patch('droidbot.input_manager.EventLog', return_value=log):
            manager.add_event(event)
        self.assertEqual(manager.events, [])
        self.assertIsNone(manager.policy.last_event)
        self.assertIsNone(manager.policy.last_state)
        event.visit.assert_not_called()
        runtime.on_event_result.assert_called_once_with(event, log)

    def test_callback_occurs_after_saved_eventlog(self):
        runtime = Mock()
        runtime.owns_event.return_value = True
        manager = self.manager(runtime)
        log = Mock(dispatched=True)
        order = []
        log.stop.side_effect = lambda: order.append('saved')
        runtime.on_event_result.side_effect = lambda *_: order.append('callback')
        event = Mock()
        with patch('droidbot.input_manager.EventLog', return_value=log):
            manager.add_event(event)
        self.assertEqual(order, ['saved', 'callback'])
        event.visit.assert_called_once()

    def test_dispatch_exception_reports_uncertain_outcome(self):
        runtime = Mock()
        manager = self.manager(runtime)
        log = Mock()
        log.start.side_effect = RuntimeError('device disconnected')
        event = Mock()
        with patch('droidbot.input_manager.EventLog', return_value=log), self.assertRaises(RuntimeError):
            manager.add_event(event)
        runtime.on_event_result.assert_called_once_with(event, None)
