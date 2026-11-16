import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "LLMDroid-Droidbot" / "droidbot"))

from maestro.android_bridge import bind_event, candidates_from_utg, snapshot_from_device
from maestro.schemas import ActionKind, ActionProposal, GoalSpec, WidgetSnapshot
from maestro.session import create_goal_state


def view(resource, **changes):
    return dict(
        dict(
            resource_id=resource,
            text="",
            visible=True,
            enabled=True,
            bounds=[[0, 0], [50, 50]],
            **{"class": "android.widget.TextView"},
        ),
        **changes,
    )


def page(*views, activity="example.app/.Main", fingerprint="abc", index=4):
    return SimpleNamespace(
        views=views,
        foreground_activity=activity,
        state_str=fingerprint,
        get_id=lambda: index,
    )


class Cluster:
    def __init__(self, identifier, entries, untested=True):
        self.identifier = identifier
        self.entries = entries
        self.untested = untested
        self.ignore_importance = []

    def get_id(self):
        return self.identifier

    def has_untested_function(self):
        return self.untested

    def write_overview_top5_tojson(self, output, ignore_importance=False):
        self.ignore_importance.append(ignore_importance)
        output[f"State{self.identifier}"] = {
            "FunctionList": list(self.entries) if self.untested or ignore_importance else []
        }

    def get_target_state(self, function):
        return self.entries[function]


class AndroidBridgeTests(unittest.TestCase):
    def test_native_checkability_is_boolean_evidence_not_a_truthy_string(self):
        snapshot = snapshot_from_device('run', 0, page(
            view('example.app:id/condition', checkable=True),
            view('example.app:id/other', clickable=True, checkable='true')))
        self.assertTrue(snapshot.widgets[0].checkable)
        self.assertFalse(snapshot.widgets[1].checkable)
        for invalid in ('true', 1, None):
            with self.assertRaises(ValueError):
                WidgetSnapshot('test', 'button', checkable=invalid)

    def test_native_selected_checked_are_explicit_boolean_evidence(self):
        snapshot = snapshot_from_device('run', 0, page(
            view('example.app:id/log', clickable=True, selected=True, checked=False),
            view('example.app:id/filter', checkable=True, selected='false', checked=True)))
        self.assertTrue(snapshot.widgets[0].selected)
        self.assertFalse(snapshot.widgets[0].checked)
        self.assertFalse(snapshot.widgets[1].selected)
        self.assertTrue(snapshot.widgets[1].checked)
        for invalid in ('false', 1, None):
            with self.assertRaises(ValueError):
                WidgetSnapshot('test', 'button', selected=invalid)

    def test_candidate_anchor_uses_original_widget_identity_not_page_order(self):
        target, other = object(), object()
        raw = page(view('example.app:id/search', clickable=True, widget=other),
                   view('example.app:id/log', clickable=True, widget=target))
        cluster = Cluster(1, {"navigate to 'Log'": raw})
        cluster.get_function_target_widget = lambda function: target
        (candidate,) = candidates_from_utg('run', SimpleNamespace(clusters=(cluster,)), 'example.app')
        self.assertEqual(candidate.widgets[1].widget_id, candidate.target_widget_id)
        cluster.get_function_target_widget = lambda function: object()
        self.assertEqual((), candidates_from_utg('run', SimpleNamespace(clusters=(cluster,)), 'example.app'))
        cluster.get_function_target_widget = lambda function: None
        self.assertEqual((), candidates_from_utg('run', SimpleNamespace(clusters=(cluster,)), 'example.app'))

    def test_logical_screenshot_size_overrides_stale_physical_dimensions(self):
        from PIL import Image
        with TemporaryDirectory() as folder:
            screenshot = Path(folder) / 'screen.png'
            Image.new('RGB', (100, 220)).save(screenshot)
            bottom = page(view('example.app:id/bottom', clickable=True,
                               bounds=[[0, 150], [50, 200]]))
            bottom.width, bottom.height, bottom.screenshot_path = 100, 100, str(screenshot)
            snapshot = snapshot_from_device('run', 0, bottom)
            self.assertEqual(snapshot.widgets[0].widget_type, 'button')

    def test_native_state_cluster_overview_produces_a_candidate(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot'))
        try:
            from droidbot.desc.state_cluster import StateCluster
        except ImportError as error:
            self.skipTest(f'DroidBot dependency unavailable: {error.name}')
        finally:
            sys.path.pop(0)
        root = Mock()
        native_widget = Mock()
        root.find_widget_by_id.return_value = native_widget
        root.views = [view('example.app:id/save', clickable=True, text='Save', widget=native_widget)]
        root.state_str = 'observed-page'
        root.foreground_activity = 'example.app/.Main'
        root.width = root.height = 100
        root.get_id.return_value = 4
        root.get_possible_input.return_value = []
        root.get_all_widgets.return_value = []
        cluster = StateCluster(root, 2)
        cluster.update_from_overview({'Overview': 'Entry editor', 'Function List': {'Save entry': 1}})
        (candidate,) = candidates_from_utg('run', SimpleNamespace(clusters=(cluster,)), 'example.app')
        self.assertEqual(candidate.function, 'Save entry')
        self.assertEqual(candidate.state_id, '4')
        self.assertEqual(candidate.target_widget_id, candidate.widgets[0].widget_id)
        self.assertIs(cluster.get_function_target_widget('Save entry'), native_widget)
        self.assertIsNone(cluster.get_function_target_widget('Unknown'))
        cluster.update_tested_function('Not mapped')
        self.assertIsNone(cluster.get_function_target_widget('Not mapped'))

    def test_reanalysis_function_resolves_unique_original_widget_in_recorded_state(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot'))
        try:
            from droidbot.desc.state_cluster import StateCluster
            from droidbot.desc.widget import Widget
        except ImportError as error:
            self.skipTest(f'DroidBot dependency unavailable: {error.name}')
        finally:
            sys.path.pop(0)
        raw = view('example.app:id/tutorial', clickable=True, text='Tutorials', temp_id=17)
        original = Widget(raw)
        root, actual = Mock(), Mock()
        actual.views = [raw]
        actual.state_str = 'actual-page'
        actual.foreground_activity = 'example.app/.Main'
        actual.width = actual.height = 100
        actual.get_id.return_value = 7
        actual.get_all_widgets.return_value = [original]
        actual.find_events_by_widget.return_value = []
        root.get_all_widgets.return_value = []
        cluster = StateCluster(root, 2)
        cluster.update_from_reanalysis(
            {'17': 'open tutorials'}, {original.to_html(id=0): [17]},
            {17: {'widget': original, 'state': actual}},
        )
        self.assertIs(cluster.get_target_state('open tutorials'), actual)
        self.assertEqual(original.get_function(), 'open tutorials')
        self.assertIs(cluster.get_function_target_widget('open tutorials'), original)
        (candidate,) = candidates_from_utg('run', SimpleNamespace(clusters=(cluster,)), 'example.app')
        self.assertEqual(candidate.state_id, '7')
        self.assertEqual(candidate.target_widget_id, candidate.widgets[0].widget_id)

        duplicate = Widget(view('example.app:id/other', clickable=True,
                                text='Other', temp_id=18))
        duplicate.set_function('open tutorials')
        actual.get_all_widgets.return_value = [original, duplicate]
        self.assertIsNone(cluster.get_function_target_widget('open tutorials'))
        self.assertEqual((), candidates_from_utg('run', SimpleNamespace(clusters=(cluster,)), 'example.app'))
        actual.get_all_widgets.return_value = [duplicate]
        duplicate.set_function('different function')
        self.assertIsNone(cluster.get_function_target_widget('open tutorials'))

    def setUp(self):
        self.field = view("example.app:id/value", editable=True, text="original")
        self.button = view("example.app:id/save", clickable=True, text="Save")
        self.page = page(self.field, self.button, view(None, text="Confirmation"))
        self.snapshot = snapshot_from_device("run", 3, self.page)
        self.state = create_goal_state("run", GoalSpec("g", "test", ("click:save",)), 3)

    def action(self, kind, resource, **kwargs):
        widget = next(w for w in self.snapshot.widgets if w.resource_id == resource)
        return ActionProposal("action", kind, widget.widget_id, 3, **kwargs)

    def test_snapshot_distinguishes_controls_and_keeps_evidence_text(self):
        self.assertEqual("abc", self.snapshot.state_id)
        self.assertEqual(3, self.snapshot.state_version)
        self.assertEqual(["input", "button", "label"], [w.widget_type for w in self.snapshot.widgets])
        self.assertEqual("Confirmation", self.snapshot.widgets[2].text)

    def test_ids_are_stable_across_reordering_and_input_values(self):
        fresh = page(self.button, dict(self.field, text="updated"), index=-1)
        other = snapshot_from_device("run", 4, fresh)
        self.assertEqual(
            {w.resource_id: w.widget_id for w in self.snapshot.widgets if w.resource_id},
            {w.resource_id: w.widget_id for w in other.widgets},
        )

    def test_input_text_does_not_mistake_accessibility_hint_for_entered_text(self):
        native = view("android:id/search_src_text", editable=True, content_description="Search")
        (widget,) = snapshot_from_device("run", 3, page(native)).widgets
        self.assertEqual("input", widget.widget_type)
        self.assertEqual("", widget.text)

    def test_repeated_resources_with_distinct_text_get_stable_actionable_ids(self):
        row1 = dict(self.button, text="One")
        row2 = dict(self.button, text="Two")
        before = snapshot_from_device("run", 0, page(row1, row2))
        after = snapshot_from_device("run", 0, page(row2, row1))
        self.assertTrue(all(w.widget_type == "button" for w in before.widgets))
        self.assertEqual({w.text: w.widget_id for w in before.widgets},
                         {w.text: w.widget_id for w in after.widgets})

    def test_foreign_and_system_navigation_views_are_not_candidates(self):
        only_foreign = (
            view("another.app:id/value", editable=True),
            view("android:id/navigationBarBackground", clickable=True),
            view(None, text="keyboard", package="another.app", clickable=True),
        )
        self.assertFalse(snapshot_from_device("run", 3, page(*only_foreign)).widgets)

    def test_target_with_off_screen_center_is_read_only(self):
        raw = page(dict(self.button, bounds=[[80, 80], [140, 140]]))
        raw.width = 100
        raw.height = 100
        (widget,) = snapshot_from_device("run", 3, raw).widgets
        self.assertEqual("label", widget.widget_type)

    def test_ambiguous_targets_and_passwords_are_not_actionable(self):
        snapshot = snapshot_from_device("run", 0, page(
            self.button,
            dict(self.button, bounds=[[25, 25], [75, 75]]),
            view("example.app:id/password", editable=True, password=True, text="secret"),
            view("example.app:id/disabled", clickable=True, enabled=False, text="Disabled"),
            view("example.app:id/hidden", clickable=True, visible=False, text="Hidden"),
            view("example.app:id/empty", clickable=True, bounds=[[0, 0], [0, 0]]),
        ))
        self.assertTrue(snapshot.widgets)
        self.assertTrue(all(w.widget_type == "label" for w in snapshot.widgets))
        self.assertNotIn("secret", repr(snapshot))
        self.assertNotIn("Hidden", repr(snapshot))

    def test_native_click_uses_current_view_and_confirmed_text_resolves(self):
        with patch("maestro.android_bridge._event_classes", return_value=(SimpleNamespace, SimpleNamespace)):
            click = self.action(ActionKind.CLICK, self.button["resource_id"])
            shifted = page(self.field, dict(self.button, bounds=[[5, 5], [55, 55]]), view(None, text="Confirmation"))
            native = bind_event(click, shifted, self.snapshot, self.state.with_pending_action(click))
            self.assertEqual([[5, 5], [55, 55]], native.view["bounds"])
            text = self.action(ActionKind.TEXT, self.field["resource_id"], value_ref="value")
            pending = self.state.with_variable("value", "typed").with_pending_action(text)
            self.assertEqual("typed", bind_event(text, self.page, self.snapshot, pending).text)

    def test_refuses_stale_action_changed_observation_and_wrong_run(self):
        click = self.action(ActionKind.CLICK, self.button["resource_id"])
        pending = self.state.with_pending_action(click)
        changed = page(self.field, self.button, fingerprint="changed")
        for raw, snap, state in (
            (changed, self.snapshot, pending),
            (self.page, snapshot_from_device("other", 3, self.page), pending),
            (self.page, snapshot_from_device("run", 4, self.page), pending),
            (self.page, self.snapshot, self.state),
            (page(self.field, dict(self.button, enabled=False), view(None, text="Confirmation")),
             self.snapshot, pending),
        ):
            with self.subTest(raw=raw.state_str, snapshot=snap), self.assertRaises(ValueError):
                bind_event(click, raw, snap, state)

    def test_refuses_text_into_non_editable_and_click_on_label(self):
        for kind, resource, values in (
            (ActionKind.TEXT, self.button["resource_id"], {"value": "typed"}),
            (ActionKind.CLICK, None, {}),
        ):
            action = self.action(kind, resource, **values)
            with self.assertRaises(ValueError):
                bind_event(action, self.page, self.snapshot, self.state.with_pending_action(action))

    def test_candidates_only_use_registered_in_app_actionable_states(self):
        cluster = Cluster(7, {
            "Save": self.page,
            "Missing": None,
            "Unregistered": page(self.button, index=-1),
            "Other app": page(self.button, activity="example.app.other/.Main"),
            "Only evidence": page(view(None, text="label")),
        })
        utg = SimpleNamespace(clusters=[cluster])
        (candidate,) = candidates_from_utg("run", utg, "example.app")
        self.assertEqual(("4", "7", "Save"), (
            candidate.state_id, candidate.target_cluster_id, candidate.function))
        self.assertEqual("states/state_4.json", candidate.observation_ref)
        self.assertEqual(self.snapshot.widgets, candidate.widgets)

    def test_candidates_follow_provided_cluster_order_and_tested_fallback(self):
        ignored = Cluster(2, {"ignored": self.page})
        ranked = Cluster(6, {"tested": self.page}, untested=False)
        (candidate,) = candidates_from_utg("run", SimpleNamespace(clusters=[ignored]), "example.app", (ranked,))
        self.assertEqual("tested", candidate.function)
        self.assertEqual([True], ranked.ignore_importance)
        self.assertEqual([], ignored.ignore_importance)
