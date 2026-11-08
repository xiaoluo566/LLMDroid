from dataclasses import dataclass

from .decision_backend import ModelRequest, ResponseFormatError
from .schemas import (
    ActionKind,
    ActionProposal,
    GoalState,
    NavigationRequest,
    PageSnapshot,
    PlanProposal,
)


class FixedGoalPlanner:
    """第一周使用固定 Goal，不新增覆盖率排序算法。"""

    def plan(self, state: GoalState, snapshot: PageSnapshot) -> PlanProposal:
        if not state.subgoals:
            raise ValueError("当前 Goal 没有子目标")
        current = state.current_subgoal or state.subgoals[0]
        return PlanProposal(
            plan_revision=state.revision + 1,
            subgoals=state.subgoals,
            current_subgoal=current,
            reason=f"基于页面 {snapshot.state_id} 执行固定 Goal",
        )

    def replan(self, state: GoalState, snapshot: PageSnapshot, reason: str) -> PlanProposal:
        if not reason:
            raise ValueError("重规划必须说明原因")
        return PlanProposal(
            plan_revision=state.revision + 1,
            subgoals=state.subgoals,
            current_subgoal=state.current_subgoal or state.subgoals[0],
            reason=f"从页面 {snapshot.state_id} 重新规划：{reason}",
        )

    def navigation_request(
        self,
        state: GoalState,
        snapshot: PageSnapshot,
        target_state_id: str,
        blocked_edges=(),
    ) -> NavigationRequest:
        return NavigationRequest(
            run_id=state.run_id,
            goal_id=state.goal_id,
            current_state_id=snapshot.state_id,
            target_state_id=target_state_id,
            blocked_edges=tuple(blocked_edges),
        )


class ModelGoalPlanner:
    """由模型从固定 Goal 的现有子目标中选择，不能创造新目标。"""

    uses_model = True

    def __init__(self, backend, timeout_seconds=30.0):
        if timeout_seconds <= 0:
            raise ValueError("模型超时时间必须大于零")
        self.backend = backend
        self.timeout_seconds = timeout_seconds

    def plan(self, state: GoalState, snapshot: PageSnapshot) -> PlanProposal:
        request_id = f"{state.run_id}-planner-{state.revision}"
        response = self.backend.complete(ModelRequest(
            role="planner",
            request_id=request_id,
            timeout_seconds=self.timeout_seconds,
            payload={
                "instruction": "从给定子目标中选择当前步骤，仅返回 JSON 对象 current_subgoal",
                "goal_id": state.goal_id,
                "goal_description": state.goal_description,
                "subgoals": list(state.subgoals),
                "state_id": snapshot.state_id,
                "state_version": snapshot.state_version,
                "widgets": [{"id": w.widget_id, "type": w.widget_type, "text": w.text}
                            for w in snapshot.widgets],
            },
        ))
        if response.request_id != request_id:
            raise ResponseFormatError("模型响应请求 ID 不匹配",
                                      response.input_tokens, response.output_tokens)
        selected = response.data.get("current_subgoal")
        if not isinstance(selected, str) or selected not in state.subgoals:
            raise ResponseFormatError("模型选择的子目标不在固定 Goal 中",
                                      response.input_tokens, response.output_tokens)
        if selected != state.subgoals[0]:
            raise ResponseFormatError("第一轮不能跳过固定 Goal 的首个子目标",
                                      response.input_tokens, response.output_tokens)
        return PlanProposal(
            plan_revision=state.revision + 1,
            subgoals=state.subgoals,
            current_subgoal=selected,
            reason="模型从固定 Goal 的子目标中选择",
            model_input_tokens=response.input_tokens,
            model_output_tokens=response.output_tokens,
        )


@dataclass(frozen=True)
class SingleAgentProposal:
    plan: PlanProposal
    action: ActionProposal


class SingleAgentDecisionAgent:
    """第二周对照用单 Agent：一次请求同时返回计划和动作。"""

    uses_model = True

    def __init__(self, backend, timeout_seconds=30.0):
        self.backend = backend
        self.timeout_seconds = timeout_seconds

    def decide(self, state: GoalState, snapshot: PageSnapshot) -> SingleAgentProposal:
        request_id = f"{state.run_id}-single-{state.revision}"
        response = self.backend.complete(ModelRequest(
            role="single_agent",
            request_id=request_id,
            timeout_seconds=self.timeout_seconds,
            payload={
                "goal_description": state.goal_description,
                "subgoals": list(state.subgoals),
                "current_subgoal": state.current_subgoal,
                "state_id": snapshot.state_id,
                "widgets": [{"id": w.widget_id, "type": w.widget_type, "text": w.text}
                            for w in snapshot.widgets],
            },
        ))
        if response.request_id != request_id:
            raise ResponseFormatError("单 Agent 响应请求 ID 不匹配",
                                      response.input_tokens, response.output_tokens)
        current = response.data.get("current_subgoal")
        raw_action = response.data.get("action")
        if current not in state.subgoals or not isinstance(raw_action, dict):
            raise ResponseFormatError("单 Agent 输出未通过 Goal 校验",
                                      response.input_tokens, response.output_tokens)
        if state.current_subgoal is None and current != state.subgoals[0]:
            raise ResponseFormatError("单 Agent 不能跳过首个子目标",
                                      response.input_tokens, response.output_tokens)
        try:
            kind = ActionKind(raw_action["kind"])
            action = ActionProposal(
                action_id=f"{state.run_id}-single-action-{state.revision + 1}",
                kind=kind,
                widget_id=raw_action["widget_id"],
                value=raw_action.get("value"),
                value_ref=raw_action.get("value_ref"),
                source_state_version=snapshot.state_version,
            )
        except (KeyError, ValueError) as error:
            raise ResponseFormatError("单 Agent 动作未通过校验",
                                      response.input_tokens, response.output_tokens) from error
        return SingleAgentProposal(
            plan=PlanProposal(
                plan_revision=state.revision + 1,
                subgoals=state.subgoals,
                current_subgoal=current,
                reason="单 Agent 同时生成计划和动作",
                model_input_tokens=response.input_tokens,
                model_output_tokens=response.output_tokens,
            ),
            action=action,
        )
