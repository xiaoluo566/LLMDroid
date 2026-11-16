"""A baseline function reaches a DroidBot gesture and a verified Goal."""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot'))

try:
    from droidbot.desc.state_cluster import StateCluster
    from droidbot.input_event import ScrollEvent
except ModuleNotFoundError as error:
    raise unittest.SkipTest(f'DroidBot dependency missing: {error.name}') from error
from droidbot.maestro.android_bridge import snapshot_from_device
from droidbot.maestro.android_runtime import AndroidRuntime
from droidbot.maestro.decision_backend import FakeDecisionBackend, ModelResponse

from tests.maestro_tests.test_native_android_actions import native_state, view


class NativeGuidanceRuntimeTests(unittest.TestCase):
    def test_swipe_candidate_reaches_native_event_and_goal_oracle(self):
        tabs = view(1, 'tabs', scrollable=True, children=[2],
                    **{'class': 'android.widget.HorizontalScrollView'})
        first = view(2, 'week', parent=1, text='Week')
        before = native_state(tabs, first)
        before.set_id(8)
        after = native_state(tabs, dict(first, text='Month'))
        target = next(w for w in snapshot_from_device('preview', 0, before).widgets
                      if w.resource_id == 'app:id/tabs')
        function = 'swipe left to reveal the Month category'
        cluster = StateCluster(before, 3)
        native_id = before.views[1]['widget'].get_id()
        cluster.update_from_overview({'Overview': 'Tabs', 'Function List': {function: native_id}})

        class Backend(FakeDecisionBackend):
            def complete(self, request):
                if request.role != 'goal_oracle':
                    return super().complete(request)
                self.requests.append(request)
                observation = request.payload['observations'][-1]
                self_case.assertEqual(observation['kind'], 'SWIPE_LEFT')
                fact = next(item for item in observation['after'] if item['text'] == 'Month')
                return ModelResponse(request.request_id, {
                    'status': 'PASS', 'reason': 'Month category became visible',
                    'evidence_ids': [fact['id']], 'recapture': False,
                })

        self_case = self
        backend = Backend({
            'goal_selection': {'candidate_id': 'c3-f1',
                               'subgoals': [f'swipe_left:{target.widget_id}']},
            'oracle': {'kind': 'ui_change', 'widget_id': target.widget_id},
        })
        with TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = AndroidRuntime(SimpleNamespace(output_dir=folder, package_name='app'), backend)
            self.assertEqual(function, runtime.select(SimpleNamespace(clusters=(cluster,))).function)
            offered = backend.requests[0].payload['candidates'][0]['widgets']
            self.assertEqual('horizontal', next(w['scroll_axis'] for w in offered
                                                if w['id'] == target.widget_id))
            event = runtime.prepare(before)
            self.assertIsInstance(event, ScrollEvent)
            self.assertTrue(runtime.preflight(event, before))
            transport = Mock()
            event.send(transport)
            transport.view_drag.assert_called_once_with((100, 120), (20, 120), 500)
            folder_events = root / 'events'
            folder_events.mkdir()
            (folder_events / 'event_sample.json').write_text(json.dumps({
                'start_state': before.state_str, 'stop_state': after.state_str,
                'event': {'event_type': event.event_type, 'direction': event.direction},
            }), encoding='utf-8')
            runtime.on_event_result(event, SimpleNamespace(
                dispatched=True, from_state=before, to_state=after, tag='sample',
                device=SimpleNamespace(get_current_state=lambda: after),
            ))
            trace = [json.loads(line) for line in
                     (root / 'maestro-trace.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(1, next(r for r in trace if r['event'] == 'low_growth')['guidance_candidate_count'])
            self.assertEqual('PASS', next(r for r in trace if r['event'] == 'goal_finished')['status'])
            verified = next(r for r in trace if r['event'] == 'goal_assessed')['assessment']
            self.assertIn('events/event_sample.json', verified['evidence_refs'])
