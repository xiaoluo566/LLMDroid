import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))

from maestro.stage_steps import execution_step


class StageStepTests(unittest.TestCase):
    def test_original_steps_are_unchanged_and_stages_have_unique_names(self):
        self.assertEqual(execution_step('generate:field=value'), 'generate:field=value')
        for phase in ('choose', 'apply', 'reopen'):
            self.assertEqual(execution_step(f'stage/{phase}/click:button'), 'click:button')

    def test_stage_grammar_cannot_inject_other_actions_or_nest_stages(self):
        for step in ('stage/skip/click:x', 'stage/choose/text:x=a',
                     'stage/choose/click:', 'stage/reopen/click:x=y',
                     'stage/reopen/stage/choose/click:x', 'stage/apply/click:x y'):
            with self.subTest(step=step), self.assertRaises(ValueError):
                execution_step(step)


if __name__ == '__main__':
    unittest.main()
