from dataclasses import dataclass

from .data_agent import DataProposalError
from .schemas import (
    DecisionKind,
    DecisionResult,
    GoalState,
    GoalStatus,
    PageSnapshot,
)


@dataclass(frozen=True)
class DecisionOutcome:
    result: DecisionResult
    state: GoalState


def decide_once(state, snapshot, planner, data_agent) -> DecisionOutcome:
    if state.status != GoalStatus.ACTIVE:
        return DecisionOutcome(
            result=DecisionResult(
                kind=DecisionKind.STOP,
                reason=f"Goal 已处于终态: {state.status.value}",
            ),
            state=state,
        )

    if snapshot.run_id != state.run_id:
        return DecisionOutcome(
            result=DecisionResult(
                kind=DecisionKind.OBSERVE_ONLY,
                reason="页面来自其他 run，必须重新观察",
            ),
            state=state,
        )

    if snapshot.state_version != state.state_version:
        return DecisionOutcome(
            result=DecisionResult(
                kind=DecisionKind.OBSERVE_ONLY,
                reason="页面版本已过期，拒绝使用旧页面动作",
            ),
            state=state,
        )

    if state.pending_action_id is not None:
        return DecisionOutcome(
            result=DecisionResult(
                kind=DecisionKind.WAIT,
                reason="已有动作等待执行结果",
            ),
            state=state,
        )

    next_state = state
    try:
        if next_state.current_subgoal is None:
            next_state = next_state.with_plan(
                planner.plan(next_state, snapshot)
            )
        action = data_agent.propose(next_state, snapshot)
        next_state = next_state.with_pending_action(action)
        return DecisionOutcome(
            result=DecisionResult(
                kind=DecisionKind.ACTION,
                action=action,
                reason="生成第一周支持的单步动作",
            ),
            state=next_state,
        )
    except (DataProposalError, KeyError, ValueError) as error:
        return DecisionOutcome(
            result=DecisionResult(
                kind=DecisionKind.STOP,
                reason=f"动作提案失败: {error}",
            ),
            state=next_state,
        )
