import sys
from pathlib import Path
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "LLMDroid-Droidbot" / "droidbot"
sys.path.insert(0, str(PACKAGE_ROOT))

from maestro.data_agent import RuleBasedDataAgent  # noqa: E402
from maestro.graph import decide_once  # noqa: E402
from maestro.planner import FixedGoalPlanner  # noqa: E402
from maestro.schemas import (  # noqa: E402
    DecisionKind,
    GoalSpec,
    PageSnapshot,
    WidgetSnapshot,
)
from maestro.session import create_goal_state  # noqa: E402


class GraphTests(unittest.TestCase):
    def setUp(self):
        goal = GoalSpec(
            goal_id="goal-1",
            description="输入标题",
            subgoals=("text:title=hello",),
        )
        self.state = create_goal_state("run-1", goal, state_version=1)
        self.snapshot = PageSnapshot(
            run_id="run-1",
            state_version=1,
            state_id="state-1",
            widgets=(
                WidgetSnapshot(widget_id="title", widget_type="text"),
            ),
        )

    def test_fixed_goal_returns_one_text_action(self):
        outcome = decide_once(
            self.state,
            self.snapshot,
            FixedGoalPlanner(),
            RuleBasedDataAgent(),
        )
        self.assertEqual(outcome.result.kind, DecisionKind.ACTION)
        self.assertEqual(outcome.result.action.kind.value, "TEXT")
        self.assertEqual(outcome.result.action.value, "hello")
        self.assertIsNotNone(outcome.state.pending_action_id)

    def test_stale_snapshot_requests_observation(self):
        stale = PageSnapshot(
            run_id="run-1",
            state_version=2,
            state_id="state-2",
            widgets=self.snapshot.widgets,
        )
        outcome = decide_once(
            self.state,
            stale,
            FixedGoalPlanner(),
            RuleBasedDataAgent(),
        )
        self.assertEqual(outcome.result.kind, DecisionKind.OBSERVE_ONLY)
        self.assertIsNone(outcome.result.action)

    def test_finished_goal_stops(self):
        outcome = decide_once(
            self.state.finish("PASS"),
            self.snapshot,
            FixedGoalPlanner(),
            RuleBasedDataAgent(),
        )
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)

    def test_fixed_goal_returns_one_click_action(self):
        goal = GoalSpec(
            goal_id="goal-2",
            description="点击保存",
            subgoals=("click:save",),
        )
        state = create_goal_state("run-2", goal, state_version=1)
        snapshot = PageSnapshot(
            run_id="run-2",
            state_version=1,
            state_id="state-1",
            widgets=(
                WidgetSnapshot(widget_id="save", widget_type="clickable"),
            ),
        )
        outcome = decide_once(
            state,
            snapshot,
            FixedGoalPlanner(),
            RuleBasedDataAgent(),
        )
        self.assertEqual(outcome.result.kind, DecisionKind.ACTION)
        self.assertEqual(outcome.result.action.kind.value, "CLICK")

    def test_missing_widget_stops_with_reason(self):
        goal = GoalSpec(
            goal_id="goal-3",
            description="点击保存",
            subgoals=("click:missing",),
        )
        state = create_goal_state("run-3", goal, state_version=1)
        snapshot = PageSnapshot(
            run_id="run-3",
            state_version=1,
            state_id="state-1",
            widgets=self.snapshot.widgets,
        )
        outcome = decide_once(
            state,
            snapshot,
            FixedGoalPlanner(),
            RuleBasedDataAgent(),
        )
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)
        self.assertIn("找不到控件", outcome.result.reason)


if __name__ == "__main__":
    unittest.main()
