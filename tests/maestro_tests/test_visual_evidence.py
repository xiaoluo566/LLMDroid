import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))
from maestro.visual_evidence import inspect_tutorial_overlay
from maestro.goal_oracle import evaluate_goal, freeze_goal_contract
from maestro.goal_selection import GoalCandidate
from maestro.schemas import ActionKind, ActionProposal, ExecutionRecord, GoalSpec, PageSnapshot, WidgetSnapshot
from maestro.session import create_goal_state


class VisualEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.before = self.draw('before.png', 'plain')
        self.after = self.draw('after.png', 'spotlight')
        self.confirmed = self.draw('confirmed.png', 'spotlight', offset=1)

    def draw(self, name, mode, offset=0):
        image = Image.new('RGB', (240, 480), 'white' if mode == 'plain' else (45, 45, 45))
        draw = ImageDraw.Draw(image)
        if mode == 'spotlight':
            draw.ellipse((60+offset, 310, 180+offset, 430), fill=(65, 110, 55))
            draw.ellipse((80+offset, 330, 160+offset, 410), fill='white')
        elif mode == 'thin-spotlight':
            draw.ellipse((75+offset, 325, 165+offset, 415), fill=(65, 110, 55))
            draw.ellipse((80+offset, 330, 160+offset, 410), fill='white')
        elif mode == 'partial-halo':
            draw.arc((75, 325, 165, 415), 180, 270, fill=(65, 110, 55), width=5)
            draw.ellipse((80, 330, 160, 410), fill='white')
        elif mode == 'dialog':
            draw.rectangle((30, 100, 210, 350), fill='white')
        elif mode == 'loading':
            draw.ellipse((80, 200, 160, 280), outline='white', width=3)
        elif mode == 'white-circle':
            draw.ellipse((80+offset, 330, 160+offset, 410), fill='white')
        path = self.root / name
        image.save(path)
        return path

    def inspect(self, before=None, after=None, confirmed=None):
        return inspect_tutorial_overlay('run', 'goal', 'click', 2,
                                         before or self.before, after or self.after,
                                         confirmed or self.confirmed,
                                         ('page.json', 'after.png', 'confirmed.png'))

    def test_new_stable_spotlight_produces_bound_visual_evidence(self):
        result = self.inspect()
        self.assertEqual(result.status, 'PASS')
        self.assertEqual((result.run_id, result.goal_id, result.action_id, result.state_version),
                         ('run', 'goal', 'click', 2))
        self.assertEqual(len(result.after_sha256), 64)
        self.assertTrue(result.focus_bounds)
        self.assertTrue(result.evidence_refs)

    def test_thin_colored_halo_is_sampled_outside_the_white_focus(self):
        first = self.draw('thin-first.png', 'thin-spotlight')
        confirmed = self.draw('thin-confirmed.png', 'thin-spotlight', offset=1)
        self.assertEqual(self.inspect(after=first, confirmed=confirmed).status, 'PASS')

    def test_partial_halo_or_no_halo_in_either_frame_stays_unknown(self):
        partial = self.draw('partial.png', 'partial-halo')
        partial_confirmed = self.draw('partial-confirmed.png', 'partial-halo')
        self.assertEqual(self.inspect(after=partial, confirmed=partial_confirmed).status, 'UNKNOWN')
        no_halo = self.draw('not-ready.png', 'white-circle')
        no_halo_again = self.draw('not-ready-again.png', 'white-circle')
        self.assertEqual(self.inspect(after=no_halo, confirmed=no_halo_again).status, 'UNKNOWN')

    def test_stable_focus_with_a_pulsing_halo_can_be_confirmed_in_either_order(self):
        no_halo = self.draw('halo-faded.png', 'white-circle')
        ready = self.draw('ready.png', 'thin-spotlight')
        self.assertEqual(self.inspect(after=no_halo, confirmed=ready).status, 'PASS')
        self.assertEqual(self.inspect(after=ready, confirmed=no_halo).status, 'PASS')

    def test_preexisting_uncolored_focus_cannot_count_as_new_guide(self):
        self.assertEqual(self.inspect(before=self.draw('existing.png', 'white-circle')).status, 'UNKNOWN')

    def test_normal_darkening_dialog_and_loading_do_not_establish_tutorial(self):
        for mode in ('plain', 'dark', 'dialog', 'loading', 'white-circle'):
            image = self.draw(f'{mode}.png', mode)
            copy = self.draw(f'{mode}-confirmed.png', mode)
            self.assertEqual(self.inspect(after=image, confirmed=copy).status, 'UNKNOWN', mode)

    def test_existing_overlay_or_unstable_focus_is_not_new_tutorial(self):
        self.assertEqual(self.inspect(before=self.after).status, 'UNKNOWN')
        shifted = self.draw('shifted.png', 'white-circle', offset=40)
        self.assertEqual(self.inspect(confirmed=shifted).status, 'UNKNOWN')
        self.assertEqual(self.inspect(confirmed=self.after).status, 'UNKNOWN')

    def test_missing_or_incompatible_images_are_unknown(self):
        self.assertEqual(self.inspect(after=self.root/'missing.png').status, 'UNKNOWN')
        broken = self.root/'broken.png'
        broken.write_text('broken', encoding='utf-8')
        self.assertEqual(self.inspect(after=broken).status, 'UNKNOWN')
        resized = self.root/'resized.png'
        Image.new('RGB', (120, 120), 'black').save(resized)
        self.assertEqual(self.inspect(confirmed=resized).status, 'UNKNOWN')

    def test_missing_paths_identifiers_and_references_are_unknown(self):
        for before, refs, version in ((None, ('proof.json',), 2),
                                      (self.before, (), 2),
                                      (self.before, None, 2),
                                      (self.before, ('proof.json',), '2')):
            result = inspect_tutorial_overlay('run', 'goal', 'click', version, before,
                                               self.after, self.confirmed, refs)
            self.assertEqual(result.status, 'UNKNOWN')

    def test_goal_checks_visual_run_action_and_version_and_source(self):
        target = WidgetSnapshot('entry', 'button', '创建分类', 'pkg:id/tutorial_item')
        before = PageSnapshot('run', 1, 'before', (target, WidgetSnapshot(
            'title', 'label', '教程', 'pkg:id/title')))
        candidate = GoalCandidate('run', 'c', '1', "open 'create category' tutorial",
                                  'states/1.json', '1', before.widgets, target_widget_id='entry')
        state = create_goal_state('run', GoalSpec('goal', candidate.function, ('click:entry',)), 1)
        action = ActionProposal('click', ActionKind.CLICK, 'entry', 1)
        state = state.with_pending_action(action).apply_execution(ExecutionRecord('click', True, 1, 2, 'OBSERVED'))
        after = PageSnapshot('run', 2, 'after', ())
        contract = freeze_goal_contract(candidate, 'goal', before)
        visual = self.inspect()
        result = evaluate_goal(contract, state, after, 'page.json', visual_evidence=visual)
        self.assertEqual(result.status, 'PASS')
        self.assertIn('confirmed.png', result.evidence_refs)
        for invalid in (replace(visual, run_id='other'), replace(visual, goal_id='other'),
                        replace(visual, action_id='other'), replace(visual, state_version=1),
                        replace(visual, status='UNKNOWN'), replace(visual, evidence_refs=()),
                        replace(visual, after_sha256=''), replace(visual, focus_bounds=None)):
            self.assertEqual(evaluate_goal(contract, state, after, 'page.json',
                                           visual_evidence=invalid).status, 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
