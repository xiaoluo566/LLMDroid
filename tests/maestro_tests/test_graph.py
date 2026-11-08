import sys
from pathlib import Path
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "LLMDroid-Droidbot" / "droidbot"
sys.path.insert(0, str(PACKAGE_ROOT))

from maestro.data_agent import RuleBasedDataAgent  # noqa: E402
from maestro.graph import decide_once  # noqa: E402
from maestro.planner import FixedGoalPlanner  # noqa: E402
from maestro.planner import ModelGoalPlanner  # noqa: E402
from maestro.planner import SingleAgentDecisionAgent  # noqa: E402
from maestro.graph import DecisionMode  # noqa: E402
from maestro.decision_backend import FakeDecisionBackend  # noqa: E402
from maestro.decision_backend import ModelResponse  # noqa: E402
from maestro.schemas import (  # noqa: E402
    ActionKind,
    ActionProposal,
    Budget,
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
            self.state.finish("UNKNOWN"),
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
        self.assertTrue(outcome.result.reason)

    def test_injected_agent_cannot_target_missing_widget(self):
        class InvalidAgent:
            def propose(self, state, snapshot):
                return ActionProposal("bad", ActionKind.CLICK, "不存在",
                                      snapshot.state_version)
        outcome = decide_once(self.state, self.snapshot, FixedGoalPlanner(), InvalidAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)
        self.assertIsNone(outcome.state.pending_action_id)

    def test_planner_exception_stops_without_exposing_secret(self):
        class FailedPlanner:
            def plan(self, state, snapshot):
                raise TimeoutError("secret-value")
        outcome = decide_once(self.state, self.snapshot, FailedPlanner(), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)
        self.assertNotIn("secret-value", outcome.result.reason)

    def test_zero_event_budget_does_not_call_planner(self):
        class UnreachablePlanner:
            def plan(self, state, snapshot):
                raise AssertionError("预算耗尽时不应调用")
        state = create_goal_state("run-1", GoalSpec("g", "目标", ("click:title",)),
                                  state_version=1, budget=Budget(events_left=0))
        outcome = decide_once(state, self.snapshot, UnreachablePlanner(), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)

    def test_duplicate_widget_id_is_rejected(self):
        with self.assertRaises(ValueError):
            PageSnapshot("run-1", 1, "state", (
                WidgetSnapshot("x", "text"), WidgetSnapshot("x", "button")))

    def test_model_planner_uses_shared_backend_and_charges_budget(self):
        backend = FakeDecisionBackend({"planner": {"current_subgoal": "text:title=hello"}})
        outcome = decide_once(self.state, self.snapshot,
                              ModelGoalPlanner(backend), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.ACTION)
        self.assertEqual(outcome.state.budget.model_calls_left, 11)
        self.assertEqual(len(backend.requests), 1)

    def test_failed_model_planning_still_charges_call(self):
        backend = FakeDecisionBackend({"planner": {"current_subgoal": "not-in-goal"}})
        outcome = decide_once(self.state, self.snapshot,
                              ModelGoalPlanner(backend), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)
        self.assertEqual(outcome.state.budget.model_calls_left, 11)

    def test_model_budget_exhaustion_prevents_call(self):
        state = create_goal_state("run-1", GoalSpec("g", "目标", ("text:title=hello",)),
                                  state_version=1, budget=Budget(model_calls_left=0))
        backend = FakeDecisionBackend()
        outcome = decide_once(state, self.snapshot, ModelGoalPlanner(backend), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)
        self.assertEqual(len(backend.requests), 0)

    def test_external_value_error_does_not_leak_in_reason(self):
        class FailedPlanner:
            def plan(self, state, snapshot):
                raise ValueError("secret-value")
        outcome = decide_once(self.state, self.snapshot, FailedPlanner(), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)
        self.assertNotIn("secret-value", outcome.result.reason)

    def test_model_cannot_skip_fixed_goal_first_subgoal(self):
        goal = GoalSpec("g", "目标", ("text:title=hello", "click:save"))
        state = create_goal_state("run-1", goal, state_version=1)
        snapshot = PageSnapshot("run-1", 1, "state", (
            WidgetSnapshot("title", "text"), WidgetSnapshot("save", "button")))
        backend = FakeDecisionBackend({"planner": {"current_subgoal": "click:save"}})
        outcome = decide_once(state, snapshot, ModelGoalPlanner(backend), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)

    def test_model_usage_and_goal_description_reach_decision_caller(self):
        class UsageBackend:
            def complete(self, request):
                self.payload = request.payload
                return ModelResponse(request.request_id,
                                     {"current_subgoal": "text:title=hello"}, 12, 4)
        backend = UsageBackend()
        outcome = decide_once(self.state, self.snapshot,
                              ModelGoalPlanner(backend), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.ACTION)
        self.assertEqual((outcome.model_input_tokens, outcome.model_output_tokens), (12, 4))
        self.assertEqual(backend.payload["goal_description"], "输入标题")

    def test_model_usage_is_preserved_when_action_validation_fails(self):
        class UsageBackend:
            def complete(self, request):
                return ModelResponse(request.request_id,
                                     {"current_subgoal": "text:missing=hello"}, 12, 4)
        goal = GoalSpec("g", "目标", ("text:missing=hello",))
        state = create_goal_state("run-1", goal, state_version=1)
        outcome = decide_once(state, self.snapshot,
                              ModelGoalPlanner(UsageBackend()), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)
        self.assertEqual((outcome.model_input_tokens, outcome.model_output_tokens), (12, 4))

    def test_usage_is_preserved_when_model_selects_invalid_subgoal(self):
        class UsageBackend:
            def complete(self, request):
                return ModelResponse(request.request_id, {"current_subgoal": "invalid"}, 12, 4)
        outcome = decide_once(self.state, self.snapshot,
                              ModelGoalPlanner(UsageBackend()), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)
        self.assertEqual((outcome.model_input_tokens, outcome.model_output_tokens), (12, 4))

    def test_variable_reference_generates_reference_action(self):
        goal = GoalSpec("g", "搜索条目", ("text_ref:search=item_title",))
        state = create_goal_state("run-1", goal, 1).with_variable("item_title", "测试计划")
        page = PageSnapshot("run-1", 1, "state", (WidgetSnapshot("search", "text"),))
        outcome = decide_once(state, page, FixedGoalPlanner(), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.ACTION)
        self.assertEqual(outcome.result.action.value_ref, "item_title")
        self.assertIsNone(outcome.result.action.value)

    def test_missing_variable_reference_stops(self):
        goal = GoalSpec("g", "搜索条目", ("text_ref:search=item_title",))
        state = create_goal_state("run-1", goal, 1)
        page = PageSnapshot("run-1", 1, "state", (WidgetSnapshot("search", "text"),))
        outcome = decide_once(state, page, FixedGoalPlanner(), RuleBasedDataAgent())
        self.assertEqual(outcome.result.kind, DecisionKind.STOP)

    def test_single_agent_mode_uses_one_decision_backend(self):
        goal = GoalSpec("g", "输入标题", ("text:title=hello",))
        state = create_goal_state("run-1", goal, 1)
        backend = FakeDecisionBackend({"single_agent": {
            "current_subgoal": "text:title=hello",
            "action": {"kind": "TEXT", "widget_id": "title", "value": "hello"},
        }})
        page = PageSnapshot("run-1", 1, "state", (WidgetSnapshot("title", "text"),))
        outcome = decide_once(
            state, page, FixedGoalPlanner(), RuleBasedDataAgent(),
            mode=DecisionMode.SINGLE_AGENT,
            single_agent=SingleAgentDecisionAgent(backend),
        )
        self.assertEqual(outcome.result.kind, DecisionKind.ACTION)
        self.assertEqual(len(backend.requests), 1)

    def test_replan_keeps_fixed_goal_and_records_reason(self):
        planner = FixedGoalPlanner()
        plan = planner.replan(
            self.state, self.snapshot, "当前路径边失效"
        )
        self.assertEqual(plan.subgoals, self.state.subgoals)
        self.assertIn("当前路径边失效", plan.reason)

    def test_planner_emits_navigation_request_for_b_bridge(self):
        request = FixedGoalPlanner().navigation_request(
            self.state, self.snapshot, target_state_id="state-target",
            blocked_edges=("state-1->state-old",)
        )
        self.assertEqual(request.current_state_id, "state-1")
        self.assertEqual(request.target_state_id, "state-target")
        self.assertEqual(request.blocked_edges, ("state-1->state-old",))


if __name__ == "__main__":
    unittest.main()
