import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot'))

try:
    from droidbot.desc.device_state import DeviceState
    from droidbot.input_event import LongTouchEvent, ScrollEvent, SetTextEvent, TouchEvent
except ModuleNotFoundError as error:
    raise unittest.SkipTest(f'DroidBot dependency missing: {error.name}') from error
from droidbot.maestro.android_bridge import bind_event, snapshot_from_device
from droidbot.maestro.schemas import ActionKind, ActionProposal, GoalSpec
from droidbot.maestro.session import create_goal_state


def view(index, resource, **changes):
    return {'temp_id': index, 'parent': 0, 'children': [], 'enabled': True,
            'visible': True, 'resource_id': f'app:id/{resource}', 'text': '',
            'class': 'android.view.View', 'bounds': [[10, 20], [110, 220]], **changes}


def native_state(*rows):
    device = SimpleNamespace(humanoid=None, get_width=lambda **_: 320, get_height=lambda **_: 480)
    root = view(0, 'root', parent=-1, children=list(range(1, len(rows) + 1)))
    return DeviceState(device, [root, *rows], 'app/.Main', [], [])


class NativeAndroidActionsTests(unittest.TestCase):
    def setUp(self):
        self.goal = create_goal_state('run', GoalSpec('g', 'exercise native controls', ('step',)), 1)

    def bind(self, snapshot, fresh, widget, kind, **values):
        action = ActionProposal('action', kind, widget.widget_id, 1, **values)
        return bind_event(action, fresh, snapshot, self.goal.with_pending_action(action))

    def test_real_droidbot_inventory_sets_capabilities_even_without_a_clickable_flag(self):
        source = native_state(view(1, 'row', long_clickable=True),
                              view(2, 'checkbox', checkable=True),
                              view(3, 'field', clickable=True, editable=True, focused=True))
        fresh = native_state(*source.views[1:])
        snapshot = snapshot_from_device('run', 1, source)
        widgets = {widget.resource_id: widget for widget in snapshot.widgets}
        row, checkbox, field = (widgets[f'app:id/{name}'] for name in ('row', 'checkbox', 'field'))
        self.assertEqual(set(row.allowed_actions), {ActionKind.CLICK, ActionKind.LONG_PRESS})
        self.assertTrue(checkbox.supports(ActionKind.CLICK))
        self.assertEqual(set(field.allowed_actions), {ActionKind.CLICK, ActionKind.TEXT, ActionKind.ENTER})
        self.assertIsInstance(self.bind(snapshot, fresh, row, ActionKind.CLICK), TouchEvent)
        press = self.bind(snapshot, fresh, row, ActionKind.LONG_PRESS)
        self.assertIsInstance(press, LongTouchEvent)
        self.assertIs(press.view, fresh.views[1])
        self.assertEqual(2000, press.duration)
        text = self.bind(snapshot, fresh, field, ActionKind.TEXT, value='confirmed')
        self.assertEqual(text.text, 'confirmed')
        self.assertEqual(next(event for event in fresh.get_possible_input()
                              if isinstance(event, SetTextEvent)).text, 'Hello World')

    def test_swipe_binding_and_preflight_detect_lost_native_capability(self):
        # A container with a child is not a DroidBot fallback click target.
        container = view(1, 'tabs', scrollable=True, children=[2])
        child = view(2, 'tab', parent=1, clickable=True)
        source = native_state(container, child)
        snapshot = snapshot_from_device('run', 1, source)
        widget = next(widget for widget in snapshot.widgets if widget.resource_id == 'app:id/tabs')
        expected = {
            ActionKind.SWIPE_UP: 'up', ActionKind.SWIPE_DOWN: 'down',
            ActionKind.SWIPE_LEFT: 'left', ActionKind.SWIPE_RIGHT: 'right',
        }
        self.assertEqual(set(expected), set(widget.allowed_actions))
        fresh = native_state(container, child)
        for kind, direction in expected.items():
            with self.subTest(kind=kind):
                event = self.bind(snapshot, fresh, widget, kind)
                self.assertIsInstance(event, ScrollEvent)
                self.assertIs(event.view, fresh.views[1])
                self.assertEqual(event.direction, direction)
        changed = native_state(dict(container, scrollable=False), child)
        changed.state_str = source.state_str  # Treat other page signals as unchanged.
        with self.assertRaises(ValueError):
            self.bind(snapshot, changed, widget, ActionKind.SWIPE_LEFT)

    def test_observed_bounds_survive_in_evidence_and_do_not_block_safe_rebinding(self):
        source = native_state(view(1, 'save', clickable=True),
                              view(2, 'disabled', enabled=False),
                              view(3, 'invalid', bounds=[[0, 0], [0, 20]]))
        snapshot = snapshot_from_device('run', 1, source)
        widgets = {widget.resource_id: widget for widget in snapshot.widgets}
        self.assertEqual(widgets['app:id/save'].bounds, (10, 20, 110, 220))
        self.assertEqual(widgets['app:id/disabled'].bounds, (10, 20, 110, 220))
        self.assertIsNone(widgets['app:id/invalid'].bounds)
        shifted = native_state(dict(source.views[1], bounds=[[20, 20], [120, 220]]))
        shifted.state_str = source.state_str
        event = self.bind(snapshot, shifted, widgets['app:id/save'], ActionKind.CLICK)
        self.assertIs(event.view, shifted.views[1])

    def test_scroll_axis_is_a_hint_and_partial_viewport_only_masks_unsafe_endpoints(self):
        row = view(1, 'tabs', scrollable=True, **{'class': 'android.widget.HorizontalScrollView'})
        full = native_state(row)
        widgets = snapshot_from_device('run', 1, full).widgets
        widget = next(widget for widget in widgets if widget.resource_id == 'app:id/tabs')
        self.assertEqual(widget.scroll_axis, 'horizontal')
        self.assertTrue(widget.supports(ActionKind.SWIPE_UP))
        self.assertTrue(widget.supports(ActionKind.SWIPE_RIGHT))

        slight_clip = native_state(dict(row, bounds=[[150, 20], [330, 220]]))
        slightly_clipped = next(widget for widget in snapshot_from_device('run', 1, slight_clip).widgets
                                if widget.resource_id == 'app:id/tabs')
        self.assertTrue(slightly_clipped.supports(ActionKind.SWIPE_RIGHT))

        partial = native_state(dict(row, bounds=[[180, 20], [340, 220]]))
        snapshot = snapshot_from_device('run', 1, partial)
        clipped = next(widget for widget in snapshot.widgets if widget.resource_id == 'app:id/tabs')
        self.assertEqual(clipped.bounds, (180, 20, 340, 220))
        self.assertEqual(clipped.scroll_axis, 'horizontal')
        self.assertFalse(clipped.supports(ActionKind.SWIPE_RIGHT))
        self.assertFalse(clipped.supports(ActionKind.SWIPE_LEFT))
        self.assertTrue(clipped.supports(ActionKind.SWIPE_UP))
        self.assertTrue(clipped.supports(ActionKind.SWIPE_DOWN))
        with self.assertRaises(ValueError):
            self.bind(snapshot, partial, clipped, ActionKind.SWIPE_RIGHT)

    def test_original_lowercase_events_send_four_distinct_physical_directions(self):
        source = native_state(view(1, 'panel', scrollable=True))
        expected = {'up': ((60, 200), (60, 40)), 'down': ((60, 40), (60, 200)),
                    'left': ((100, 120), (20, 120)), 'right': ((20, 120), (100, 120))}
        events = {event.direction: event for event in source.get_possible_input()
                  if isinstance(event, ScrollEvent)}
        self.assertEqual(events.keys(), expected.keys())
        for direction, (start, end) in expected.items():
            with self.subTest(direction=direction):
                device = Mock()
                self.assertTrue(events[direction].send(device))
                device.view_drag.assert_called_once_with(start, end, 500)

    def test_legacy_uppercase_scroll_keeps_content_direction(self):
        source = native_state(view(1, 'panel', scrollable=True))
        device = Mock()
        ScrollEvent(view=source.views[1], direction='UP').send(device)
        device.view_drag.assert_called_once_with((60, 40), (60, 200), 500)
