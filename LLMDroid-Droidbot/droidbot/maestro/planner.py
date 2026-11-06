from .decision_backend import ModelRequest, ResponseFormatError
from .schemas import GoalState, PageSnapshot, PlanProposal


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
