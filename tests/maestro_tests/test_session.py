import sys
from pathlib import Path
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "LLMDroid-Droidbot" / "droidbot"
sys.path.insert(0, str(PACKAGE_ROOT))

from maestro.schemas import (  # noqa: E402
    ActionKind,
    ActionProposal,
    ExecutionRecord,
    GoalSpec,
    PlanProposal,
)
from maestro.session import create_goal_state  # noqa: E402


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.goal = GoalSpec(
            goal_id="goal-1",
            description="输入标题并保存",
            subgoals=("text:title=hello", "click:save"),
        )
        self.state = create_goal_state("run-1", self.goal, state_version=3)

    def test_initial_model_budget_matches_week_plan(self):
        self.assertEqual(self.state.budget.model_calls_left, 12)

    def test_plan_and_pending_action_return_new_state(self):
        planned = self.state.with_plan(
            PlanProposal(
                plan_revision=1,
                subgoals=self.goal.subgoals,
                current_subgoal=self.goal.subgoals[0],
                reason="固定 Goal 的第一步",
            )
        )
        self.assertEqual(self.state.current_subgoal, None)
        self.assertEqual(planned.current_subgoal, "text:title=hello")
        self.assertGreater(planned.revision, self.state.revision)

        action = ActionProposal(
            action_id="action-1",
            kind=ActionKind.TEXT,
            widget_id="title",
            value="hello",
            source_state_version=3,
        )
        pending = planned.with_pending_action(action)
        self.assertEqual(pending.pending_action_id, "action-1")
        self.assertEqual(pending.budget.events_left, 39)

    def test_execution_updates_page_version_and_cannot_be_applied_twice(self):
        action = ActionProposal(
            action_id="action-1",
            kind=ActionKind.CLICK,
            widget_id="save",
            source_state_version=3,
        )
        pending = self.state.with_pending_action(action)
        record = ExecutionRecord(
            action_id="action-1",
            dispatched=True,
            before_state_version=3,
            after_state_version=4,
            execution_status="OBSERVED",
        )
        completed = pending.apply_execution(record)
        self.assertEqual(completed.state_version, 4)
        self.assertIsNone(completed.pending_action_id)
        self.assertIsNone(completed.current_subgoal)

        with self.assertRaises(ValueError):
            completed.apply_execution(record)

    def test_terminal_state_does_not_accept_new_action(self):
        finished = self.state.finish("UNKNOWN")
        action = ActionProposal(
            action_id="action-1",
            kind=ActionKind.CLICK,
            widget_id="save",
            source_state_version=3,
        )
        with self.assertRaises(ValueError):
            finished.with_pending_action(action)

    def test_variable_update_returns_new_state_and_can_be_read(self):
        updated = self.state.with_variable("item_title", "hello")
        self.assertIsNone(self.state.get_variable("item_title"))
        self.assertEqual(updated.get_variable("item_title"), "hello")
        self.assertGreater(updated.revision, self.state.revision)

    def test_stale_execution_record_does_not_clear_pending_action(self):
        action = ActionProposal(action_id="a", kind=ActionKind.CLICK,
                                widget_id="save", source_state_version=3)
        state = self.state.with_pending_action(action)
        record = ExecutionRecord(action_id="a", dispatched=True,
                                 before_state_version=2, after_state_version=3,
                                 execution_status="OBSERVED")
        with self.assertRaises(ValueError):
            state.apply_execution(record)
        self.assertEqual(state.pending_action_id, "a")

    def test_uncertain_execution_remains_pending_until_observed(self):
        action = ActionProposal(action_id="a", kind=ActionKind.CLICK,
                                widget_id="save", source_state_version=3)
        state = self.state.with_pending_action(action)
        uncertain = ExecutionRecord(action_id="a", dispatched=True,
                                    before_state_version=3, after_state_version=None,
                                    execution_status="EXECUTION_UNKNOWN")
        state = state.apply_execution(uncertain)
        self.assertEqual(state.pending_action_id, "a")
        with self.assertRaises(ValueError):
            state.with_pending_action(action)
        observed = ExecutionRecord(action_id="a", dispatched=True,
                                   before_state_version=3, after_state_version=4,
                                   execution_status="OBSERVED")
        self.assertIsNone(state.apply_execution(observed).pending_action_id)

    def test_completed_action_id_cannot_be_reused(self):
        action = ActionProposal(action_id="a", kind=ActionKind.CLICK,
                                widget_id="save", source_state_version=3)
        state = self.state.with_pending_action(action)
        record = ExecutionRecord(action_id="a", dispatched=True,
                                 before_state_version=3, after_state_version=3,
                                 execution_status="OBSERVED")
        state = state.apply_execution(record)
        with self.assertRaises(ValueError):
            state.with_pending_action(action)

    def test_unsent_action_cannot_advance_goal(self):
        state = self.state.with_pending_action(ActionProposal(
            "a", ActionKind.CLICK, "save", 3))
        record = ExecutionRecord("a", False, 3, None, "NOT_SENT", "click:save")
        with self.assertRaises(ValueError):
            state.apply_execution(record)

    def test_observed_action_alone_cannot_advance_subgoal(self):
        state = self.state.with_pending_action(ActionProposal(
            "a", ActionKind.CLICK, "save", 3))
        record = ExecutionRecord("a", True, 3, 4, "OBSERVED", "click:save")
        with self.assertRaises(ValueError):
            state.apply_execution(record)

    def test_goal_cannot_be_marked_pass_without_verification(self):
        with self.assertRaises(ValueError):
            self.state.finish("PASS")

    def test_planner_cannot_replace_fixed_goal_subgoals(self):
        with self.assertRaises(ValueError):
            self.state.with_plan(PlanProposal(1, ("click:other",),
                                              "click:other", "改目标"))


if __name__ == "__main__":
    unittest.main()
