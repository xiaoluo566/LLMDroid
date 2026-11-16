import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))

from maestro import goal_selection
from maestro.decision_backend import FakeDecisionBackend
from maestro.goal_selection import GoalCandidate, ModelGoalSelector
from maestro.schemas import ActionKind, Budget, WidgetSnapshot


def candidate(candidate_id, function, state='state1', widget=None, *, run='run'):
    target = widget or WidgetSnapshot('misc', 'button', 'Misc', 'app:id/misc')
    return GoalCandidate(run, candidate_id, state, function, f'states/{state}.json',
                         f'cluster-{candidate_id}', (target,), target.widget_id)


class GoalCandidateDedupTests(unittest.TestCase):
    def test_binding_dedup_keeps_first_rank_and_reports_each_removed_source(self):
        first = candidate('c1', 'Edit entry')
        unrelated = candidate('c2', 'Open settings')
        alias = candidate('c3', '  Edit   entry  ')
        kept, duplicates = goal_selection.deduplicate_candidates(iter((first, unrelated, alias, first)))
        self.assertEqual((first, unrelated), kept)
        self.assertEqual(((alias, first), (first, first)), duplicates)

    def test_same_tab_navigation_can_deduplicate_across_states(self):
        first = candidate('c1', "navigate to 'Misc'")
        alias = candidate('c2', "Navigate to 'Misc'", state='state6')
        alias = replace(alias, widgets=(replace(alias.widgets[0], selected=True),))
        different_destination = candidate('c3', "navigate to 'Log'", state='state6')
        kept, duplicates = goal_selection.deduplicate_candidates((first, alias, different_destination))
        self.assertEqual((first, different_destination), kept)
        self.assertEqual(((alias, first),), duplicates)

    def test_distinct_business_states_targets_capabilities_and_runs_are_preserved(self):
        first = candidate('c1', 'Edit entry')
        other_state = candidate('c2', 'Edit entry', state='state6')
        anchor = first.widgets[0]
        other_target = candidate('c3', 'Edit entry', widget=replace(anchor, widget_id='duplicate-resource'))
        long_press = candidate('c4', 'Edit entry', widget=replace(
            anchor, supported_actions=(ActionKind.CLICK, ActionKind.LONG_PRESS)))
        other_run = candidate('c5', 'Edit entry', run='other')
        candidates = (first, other_state, other_target, long_press, other_run)
        self.assertEqual((candidates, ()), goal_selection.deduplicate_candidates(candidates))

    def test_business_target_can_contain_case_sensitive_literal_values(self):
        first = candidate('c1', 'Enter code AbC')
        second = candidate('c2', 'Enter code abc')
        self.assertEqual(((first, second), ()), goal_selection.deduplicate_candidates((first, second)))

    def test_conflicting_candidate_id_reaches_selector_validation_without_model_call(self):
        first = candidate('c1', 'Edit entry')
        alias = candidate('c2', 'Edit entry')
        conflict = replace(alias, target_cluster_id='another-source')
        kept, duplicates = goal_selection.deduplicate_candidates((first, alias, conflict))
        self.assertEqual((first, alias, conflict), kept)
        self.assertEqual((), duplicates)
        backend = FakeDecisionBackend()
        outcome = ModelGoalSelector(backend).select('run', kept, Budget(), 'request')
        self.assertIsNone(outcome.selection)
        self.assertIsNotNone(outcome.error)
        self.assertEqual([], backend.requests)

    def test_missing_anchor_cannot_establish_equivalence(self):
        first, second = (replace(candidate(f'c{i}', 'Edit entry'), target_widget_id=None)
                         for i in range(2))
        self.assertEqual(((first, second), ()), goal_selection.deduplicate_candidates((first, second)))


if __name__ == '__main__':
    unittest.main()
