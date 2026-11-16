"""Runtime integration for goal preconditions and staged postconditions."""
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))
from maestro.android_bridge import snapshot_from_device
from maestro.android_runtime import AndroidRuntime
from maestro.decision_backend import FakeDecisionBackend
from maestro.goal_selection import GoalCandidate


def control(name, text='', **props):
    return dict(resource_id=f'example.app:id/{name}', text=text, visible=True,
                enabled=True, bounds=[[0, 0], [50, 50]],
                **{'class': 'android.widget.Button'}, **props)


def page(name, *views):
    return SimpleNamespace(foreground_activity='example.app/.Main', state_str=name,
                           get_id=lambda: 1, views=list(views))


class GoalExtensionsTests(unittest.TestCase):
    def candidate(self, runtime, native, function, name):
        widgets = snapshot_from_device(runtime.run_id, runtime.version, native).widgets
        target = next(w.widget_id for w in widgets if w.resource_id == f'example.app:id/{name}')
        return GoalCandidate(runtime.run_id, 'c1', '1', function, 'states/1.json', '1',
                             widgets, target_widget_id=target)

    def select(self, runtime, native, candidate):
        with patch('maestro.android_runtime.candidates_from_utg', return_value=(candidate,)):
            return runtime.select(SimpleNamespace(clusters=(), last_state=native))

    def rows(self, folder):
        return [json.loads(line) for line in (Path(folder) / 'maestro-trace.jsonl').read_text(
            encoding='utf-8').splitlines()]

    def test_already_satisfied_navigation_is_skipped_without_a_model_request_or_action(self):
        with TemporaryDirectory() as folder:
            backend = FakeDecisionBackend()
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'), backend)
            native = page('log', control('log', selected=True, clickable=True))
            candidate = self.candidate(runtime, native, "navigate to 'Log'", 'log')
            self.assertIsNone(self.select(runtime, native, candidate))
            self.assertFalse(backend.requests)
            rows = self.rows(folder)
            skipped = [r for r in rows if r['event'] == 'goal_already_satisfied']
            self.assertEqual(len(skipped), 1)
            self.assertEqual(rows[-2]['grounded_candidate_count'], 1)
            self.assertEqual(rows[-2]['candidate_count'], 0)
            self.assertEqual(rows[-1]['reason'], 'all_bound_candidates_already_satisfied')
            self.assertEqual(runtime.canary_goals, 0)
            self.assertIsNone(runtime.pending)

    def test_navigation_that_becomes_satisfied_before_prepare_is_not_clicked_again(self):
        with TemporaryDirectory() as folder:
            native = page('log', control('log', selected=False, clickable=True))
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'),
                                     FakeDecisionBackend())
            candidate = self.candidate(runtime, native, "navigate to 'Log'", 'log')
            runtime.selector.backend.backend.responses['goal_selection'] = {
                'candidate_id': 'c1', 'subgoals': [f'click:{candidate.target_widget_id}']}
            self.assertIsNotNone(self.select(runtime, native, candidate))
            satisfied = page('log', control('log', selected=True, clickable=True))
            self.assertIsNone(runtime.prepare(satisfied))
            rows = self.rows(folder)
            self.assertTrue(any(r['event'] == 'goal_already_satisfied' for r in rows))
            self.assertFalse(any(r['event'] == 'action_proposed' for r in rows))
            self.assertEqual([r for r in rows if r['event'] == 'goal_finished'][-1]['status'], 'SKIPPED')

    def test_live_page_overrides_an_old_selected_utg_snapshot(self):
        with TemporaryDirectory() as folder:
            live = page('live', control('log', selected=False, clickable=True))
            stale = page('old', control('log', selected=True, clickable=True))
            backend = FakeDecisionBackend()
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'), backend)
            candidate = self.candidate(runtime, stale, "navigate to 'Log'", 'log')
            backend.responses['goal_selection'] = {
                'candidate_id': 'c1', 'subgoals': [f'click:{candidate.target_widget_id}']}
            with patch('maestro.android_runtime.candidates_from_utg', return_value=(candidate,)):
                result = runtime.select(SimpleNamespace(clusters=(), last_state=stale), current_state=live)
            self.assertIsNotNone(result)
            self.assertFalse(any(r['event'] == 'goal_already_satisfied' for r in self.rows(folder)))

    def dispatch(self, runtime, before, after, folder, tag):
        touch = lambda view: SimpleNamespace(get_views=lambda: [view])
        with patch('maestro.android_bridge._event_classes', return_value=(touch, None)):
            event = runtime.prepare(before)
        self.assertIsNotNone(event)
        events = Path(folder) / 'events'
        events.mkdir(exist_ok=True)
        (events / f'event_{tag}.json').write_text(json.dumps({
            'start_state': before.state_str, 'stop_state': after.state_str}), encoding='utf-8')
        runtime.on_event_result(event, SimpleNamespace(
            to_state=after, from_state=before, dispatched=True, tag=tag,
            device=SimpleNamespace(get_current_state=lambda: after)))

    def test_open_view_pass_requires_a_new_destination_title(self):
        with TemporaryDirectory() as folder:
            before = page('home', control('settings', 'Settings', clickable=True))
            after = page('settings', control('title', 'Settings'))
            backend = FakeDecisionBackend()
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'), backend)
            candidate = self.candidate(runtime, before, "open 'Settings'", 'settings')
            backend.responses = {
                'goal_selection': {'candidate_id': 'c1', 'subgoals': [f'click:{candidate.target_widget_id}']},
                'oracle': {'kind': 'ui_change', 'widget_id': candidate.target_widget_id},
            }
            self.select(runtime, before, candidate)
            self.dispatch(runtime, before, after, folder, 'settings')
            self.assertEqual([r for r in self.rows(folder) if r['event'] == 'goal_finished'][-1]['status'], 'PASS')

    def test_observation_screenshot_survives_original_droidbot_temp_cleanup(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / 'temp/screen.png'
            raw.parent.mkdir()
            Image.new('RGB', (100, 100), 'white').save(raw)
            native = page('records', control('filter', 'Filter', clickable=True))
            native.screenshot_path = str(raw)
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'),
                                     FakeDecisionBackend())
            snapshot = snapshot_from_device(runtime.run_id, 1, native)
            observation = runtime._save_observation(SimpleNamespace(action_id='a'), snapshot,
                                                  'events/event_a.json', native)
            saved = json.loads((root / observation).read_text(encoding='utf-8'))
            raw.unlink()
            self.assertTrue((root / saved['screenshot_ref']).is_file())

    def test_tutorial_uses_two_bound_screenshots_and_saves_the_visual_proof(self):
        rows, reads = self.tutorial_trace()
        self.assertEqual(reads, 1)
        self.assertEqual([r for r in rows if r['event'] == 'goal_finished'][-1]['status'], 'PASS')

    def test_tutorial_animation_gets_one_bounded_extra_read_before_final_verification(self):
        rows, reads = self.tutorial_trace(first_incomplete=True)
        self.assertEqual(reads, 2)
        self.assertEqual([r['visual']['status'] for r in rows if r['event'] == 'visual_assessed'],
                         ['UNKNOWN', 'PASS'])
        self.assertEqual([r for r in rows if r['event'] == 'goal_finished'][-1]['status'], 'PASS')

    def test_tutorial_observation_stops_when_the_complete_visual_pattern_never_appears(self):
        rows, reads = self.tutorial_trace(always_incomplete=True)
        self.assertEqual(reads, 2)
        self.assertEqual([r for r in rows if r['event'] == 'goal_finished'][-1]['status'], 'UNKNOWN')
        self.assertEqual(len([r for r in rows if r['event'] == 'action_proposed']), 1)

    def tutorial_trace(self, first_incomplete=False, always_incomplete=False):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            before = page('tutorials', control('title', 'Tutorials'),
                          control('tutorial_item', 'Create category', clickable=True))
            after = page('guide', control('title', 'Records'))
            confirmed = page('guide', control('title', 'Records'))
            for native, filename, guide in ((before, 'before.png', False),
                                            (after, 'after.png', True),
                                            (confirmed, 'confirmed.png', True)):
                img = Image.new('RGB', (240, 480), (45, 45, 45) if guide else 'white')
                if guide:
                    draw = ImageDraw.Draw(img)
                    if not always_incomplete and not (first_incomplete and native is after):
                        draw.ellipse((60, 310, 180, 430), fill=(65, 110, 55))
                    if not (first_incomplete and native is after):
                        draw.ellipse((80, 330, 160, 410), fill='white')
                img.save(root / filename)
                native.screenshot_path = str(root / filename)
            backend = FakeDecisionBackend()
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'), backend)
            candidate = self.candidate(runtime, before, "open 'create category' tutorial", 'tutorial_item')
            backend.responses = {
                'goal_selection': {'candidate_id': 'c1', 'subgoals': [f'click:{candidate.target_widget_id}']},
                'oracle': {'kind': 'ui_change', 'widget_id': candidate.target_widget_id},
            }
            self.select(runtime, before, candidate)
            touch = lambda view: SimpleNamespace(get_views=lambda: [view])
            with patch('maestro.android_bridge._event_classes', return_value=(touch, None)):
                event = runtime.prepare(before)
            events = root / 'events'
            events.mkdir()
            (events / 'event_guide.json').write_text(
                '{"start_state":"tutorials","stop_state":"guide"}', encoding='utf-8')
            captures = []
            def capture():
                captures.append(1)
                return confirmed
            device = SimpleNamespace(get_current_state=capture)
            runtime.on_event_result(event, SimpleNamespace(
                from_state=before, to_state=after, device=device, tag='guide', dispatched=True))
            rows = self.rows(folder)
            visual = next(r['visual'] for r in rows if r['event'] == 'visual_assessed')
            self.assertEqual(visual['status'], 'UNKNOWN' if first_incomplete or always_incomplete else 'PASS')
            self.assertEqual(visual['state_version'], 2)
            self.assertTrue((root / 'maestro-observations/visual-2.json').is_file())
            self.assertEqual(len([r for r in rows if r['event'] == 'action_proposed']), 1)
            return rows, len(captures)

    def test_filter_keeps_one_goal_through_choose_apply_and_reopen(self):
        with TemporaryDirectory() as folder:
            start = page('records', control('filter', 'Filter', clickable=True))
            form = page('form', control('condition', 'One time', clickable=True, checkable=True, checked=False),
                        control('apply_button', 'Apply', clickable=True))
            chosen = page('chosen', control('condition', 'One time', clickable=True, checkable=True, checked=True),
                          control('apply_button', 'Apply', clickable=True))
            backend = FakeDecisionBackend()
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='example.app'), backend)
            candidate = self.candidate(runtime, start, 'filter records', 'filter')
            form_widgets = snapshot_from_device(runtime.run_id, 1, form).widgets
            condition, apply = form_widgets[0].widget_id, form_widgets[1].widget_id
            backend.responses = {
                'goal_selection': {'candidate_id': 'c1', 'subgoals': [f'click:{candidate.target_widget_id}']},
                'planner_stage': {'condition_id': condition, 'apply_widget_id': apply},
            }
            # The limited step Oracle always binds the currently proposed control.
            original = backend.complete
            def complete(request):
                if request.role == 'oracle':
                    backend.responses['oracle'] = {'kind': 'ui_change',
                                                    'widget_id': request.payload['action']['widget_id']}
                return original(request)
            backend.complete = complete
            self.assertIsNotNone(self.select(runtime, start, candidate))
            goal_id = runtime.state.goal_id
            for before, after, tag in ((start, form, 'open'), (form, chosen, 'choose'),
                                       (chosen, start, 'apply'), (start, chosen, 'reopen')):
                self.dispatch(runtime, before, after, folder, tag)
                if runtime.state is not None:
                    self.assertEqual(runtime.state.goal_id, goal_id)
                    self.assertEqual(runtime.state.goal_description, 'filter records')
            rows = self.rows(folder)
            self.assertEqual(len([r for r in rows if r['event'] == 'goal_selected']), 1)
            self.assertEqual(len([r for r in rows if r['event'] == 'action_proposed']), 4)
            self.assertEqual([r for r in rows if r['event'] == 'goal_finished'][-1]['status'], 'PASS')


if __name__ == '__main__':
    unittest.main()
