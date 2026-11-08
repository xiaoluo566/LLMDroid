from dataclasses import dataclass
from dataclasses import replace
from enum import Enum
from typing import Optional

from .data_agent import DataProposalError
from .decision_backend import ResponseFormatError
from .schemas import (
    ActionKind,
    ActionProposal,
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
    model_input_tokens: Optional[int] = None
    model_output_tokens: Optional[int] = None


class DecisionMode(str, Enum):
    MULTI_AGENT = "MULTI_AGENT"
    SINGLE_AGENT = "SINGLE_AGENT"


def decide_once(
    state, snapshot, planner, data_agent, mode=DecisionMode.MULTI_AGENT, single_agent=None
) -> DecisionOutcome:
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

    if state.budget.events_left <= 0:
        return DecisionOutcome(
            result=DecisionResult(kind=DecisionKind.STOP, reason="设备事件预算已耗尽"),
            state=state,
        )

    next_state = state
    plan = None
    try:
        if mode == DecisionMode.SINGLE_AGENT:
            if single_agent is None:
                raise ValueError("SINGLE_AGENT 模式缺少 single_agent")
            next_state = replace(
                next_state,
                budget=next_state.budget.consume_model_call(),
                revision=next_state.revision + 1,
            )
            proposal = single_agent.decide(next_state, snapshot)
            plan = proposal.plan
            next_state = next_state.with_plan(plan)
            return _accept_action(next_state, snapshot, proposal.action, plan)
        if next_state.current_subgoal is None:
            if getattr(planner, "uses_model", False):
                next_state = replace(
                    next_state,
                    budget=next_state.budget.consume_model_call(),
                    revision=next_state.revision + 1,
                )
            plan = planner.plan(next_state, snapshot)
            next_state = next_state.with_plan(plan)
        action = data_agent.propose(next_state, snapshot)
        if not isinstance(action, ActionProposal):
            raise ValueError("Data 输出不是有效的动作提案")
        widget = snapshot.find_widget(action.widget_id)
        if action.kind == ActionKind.TEXT and widget.widget_type not in {
            "text", "input", "editable"
        }:
            raise ValueError("目标控件不可输入文本")
        if action.kind == ActionKind.CLICK and widget.widget_type not in {
            "text", "button", "clickable"
        }:
            raise ValueError("目标控件不可点击")
        return _accept_action(next_state, snapshot, action, plan)
    except ResponseFormatError as error:
        return DecisionOutcome(
            result=DecisionResult(kind=DecisionKind.STOP, reason="模型响应未通过校验"),
            state=next_state,
            model_input_tokens=error.input_tokens,
            model_output_tokens=error.output_tokens,
        )
    except (DataProposalError, KeyError, ValueError):
        return DecisionOutcome(
            result=DecisionResult(
                kind=DecisionKind.STOP,
                reason="动作或计划未通过校验",
            ),
            state=next_state,
            model_input_tokens=plan.model_input_tokens if plan else None,
            model_output_tokens=plan.model_output_tokens if plan else None,
        )
    except Exception:
        return DecisionOutcome(
            result=DecisionResult(kind=DecisionKind.STOP, reason="决策调用失败"),
            state=next_state,
            model_input_tokens=plan.model_input_tokens if plan else None,
            model_output_tokens=plan.model_output_tokens if plan else None,
        )


def _accept_action(state, snapshot, action, plan):
    widget = snapshot.find_widget(action.widget_id)
    if action.kind == ActionKind.TEXT and widget.widget_type not in {
        "text", "input", "editable"
    }:
        raise ValueError("目标控件不可输入文本")
    if action.kind == ActionKind.CLICK and widget.widget_type not in {
        "text", "button", "clickable"
    }:
        raise ValueError("目标控件不可点击")
    state = state.with_pending_action(action)
    return DecisionOutcome(
        result=DecisionResult(
            kind=DecisionKind.ACTION,
            action=action,
            reason="生成第二周支持的单步动作",
        ),
        state=state,
        model_input_tokens=plan.model_input_tokens if plan else None,
        model_output_tokens=plan.model_output_tokens if plan else None,
    )
