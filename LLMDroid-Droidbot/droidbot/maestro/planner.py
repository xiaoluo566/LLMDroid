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
