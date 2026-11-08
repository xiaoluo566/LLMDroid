from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional, Tuple, Union


class ActionKind(str, Enum):
    CLICK = "CLICK"
    TEXT = "TEXT"


class DecisionKind(str, Enum):
    ACTION = "ACTION"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    WAIT = "WAIT"
    STOP = "STOP"


class GoalStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class WidgetSnapshot:
    widget_id: str
    widget_type: str
    text: str = ""
    resource_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.widget_id:
            raise ValueError("widget_id 不能为空")
        if not self.widget_type:
            raise ValueError("widget_type 不能为空")


@dataclass(frozen=True)
class PageSnapshot:
    run_id: str
    state_version: int
    state_id: str
    widgets: Tuple[WidgetSnapshot, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.run_id or not self.state_id:
            raise ValueError("run_id 和 state_id 不能为空")
        if self.state_version < 0:
            raise ValueError("state_version 不能为负数")
        object.__setattr__(self, "widgets", tuple(self.widgets))
        ids = [widget.widget_id for widget in self.widgets]
        if len(ids) != len(set(ids)):
            raise ValueError("页面控件 ID 不能重复")

    def find_widget(self, widget_id: str) -> WidgetSnapshot:
        for widget in self.widgets:
            if widget.widget_id == widget_id:
                return widget
        raise KeyError(f"找不到控件: {widget_id}")


@dataclass(frozen=True)
class GoalSpec:
    goal_id: str
    description: str
    subgoals: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.goal_id or not self.description:
            raise ValueError("Goal 必须有 goal_id 和 description")
        subgoals = tuple(self.subgoals)
        if not subgoals or any(not item for item in subgoals):
            raise ValueError("Goal 至少需要一个非空子目标")
        object.__setattr__(self, "subgoals", subgoals)


@dataclass(frozen=True)
class NavigationRequest:
    run_id: str
    goal_id: str
    current_state_id: str
    target_state_id: str
    blocked_edges: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not all((self.run_id, self.goal_id, self.current_state_id, self.target_state_id)):
            raise ValueError("导航请求缺少状态标识")
        object.__setattr__(self, "blocked_edges", tuple(self.blocked_edges))


@dataclass(frozen=True)
class PlanProposal:
    plan_revision: int
    subgoals: Tuple[str, ...]
    current_subgoal: str
    reason: str
    model_input_tokens: Optional[int] = None
    model_output_tokens: Optional[int] = None

    def __post_init__(self) -> None:
        if self.plan_revision < 0:
            raise ValueError("plan_revision 不能为负数")
        subgoals = tuple(self.subgoals)
        if not subgoals or self.current_subgoal not in subgoals:
            raise ValueError("current_subgoal 必须属于 subgoals")
        object.__setattr__(self, "subgoals", subgoals)


@dataclass(frozen=True)
class ActionProposal:
    action_id: str
    kind: ActionKind
    widget_id: str
    source_state_version: int
    value: Optional[str] = None
    value_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.action_id or not self.widget_id:
            raise ValueError("action_id 和 widget_id 不能为空")
        if self.source_state_version < 0:
            raise ValueError("source_state_version 不能为负数")
        if self.kind == ActionKind.TEXT:
            has_value = self.value is not None
            has_reference = self.value_ref is not None
            if has_value == has_reference:
                raise ValueError("TEXT 动作必须且只能有 value 或 value_ref")
        elif self.kind == ActionKind.CLICK:
            if self.value is not None or self.value_ref is not None:
                raise ValueError("CLICK 动作不能携带文本值")
        else:
            raise ValueError(f"不支持的动作类型: {self.kind}")


@dataclass(frozen=True)
class DecisionResult:
    kind: DecisionKind
    reason: str
    action: Optional[ActionProposal] = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DecisionKind):
            object.__setattr__(self, "kind", DecisionKind(self.kind))
        if not self.reason:
            raise ValueError("DecisionResult 必须说明 reason")
        if self.kind == DecisionKind.ACTION and self.action is None:
            raise ValueError("ACTION 必须携带 action")
        if self.kind != DecisionKind.ACTION and self.action is not None:
            raise ValueError("非 ACTION 结果不能携带 action")


@dataclass(frozen=True)
class ExecutionRecord:
    action_id: str
    dispatched: bool
    before_state_version: int
    after_state_version: Optional[int]
    execution_status: str
    next_subgoal: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.action_id or not self.execution_status:
            raise ValueError("ExecutionRecord 缺少 action_id 或 execution_status")
        if self.before_state_version < 0:
            raise ValueError("before_state_version 不能为负数")
        if (
            self.after_state_version is not None
            and self.after_state_version < self.before_state_version
        ):
            raise ValueError("after_state_version 不能早于 before_state_version")


@dataclass(frozen=True)
class Budget:
    events_left: int = 40
    model_calls_left: int = 12
    replans_left: int = 2

    def __post_init__(self) -> None:
        if min(self.events_left, self.model_calls_left, self.replans_left) < 0:
            raise ValueError("预算不能为负数")

    def consume_event(self) -> "Budget":
        if self.events_left <= 0:
            raise ValueError("设备事件预算已耗尽")
        return replace(self, events_left=self.events_left - 1)

    def consume_model_call(self) -> "Budget":
        if self.model_calls_left <= 0:
            raise ValueError("模型调用预算已耗尽")
        return replace(self, model_calls_left=self.model_calls_left - 1)


@dataclass(frozen=True)
class GoalState:
    run_id: str
    goal_id: str
    state_version: int
    revision: int = 0
    status: GoalStatus = GoalStatus.ACTIVE
    subgoals: Tuple[str, ...] = field(default_factory=tuple)
    current_subgoal: Optional[str] = None
    variables: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    budget: Budget = field(default_factory=Budget)
    pending_action_id: Optional[str] = None
    last_feedback: Optional[str] = None
    completed_action_ids: Tuple[str, ...] = field(default_factory=tuple)
    goal_description: str = ""

    def __post_init__(self) -> None:
        if not self.run_id or not self.goal_id:
            raise ValueError("run_id 和 goal_id 不能为空")
        if self.state_version < 0 or self.revision < 0:
            raise ValueError("版本号不能为负数")
        if not isinstance(self.status, GoalStatus):
            object.__setattr__(self, "status", GoalStatus(self.status))
        object.__setattr__(self, "subgoals", tuple(self.subgoals))
        object.__setattr__(self, "variables", tuple(self.variables))
        object.__setattr__(self, "completed_action_ids", tuple(self.completed_action_ids))
        if (
            self.current_subgoal is not None
            and self.subgoals
            and self.current_subgoal not in self.subgoals
        ):
            raise ValueError("current_subgoal 必须属于 subgoals")

    def _updated(self, **changes) -> "GoalState":
        changes["revision"] = self.revision + 1
        return replace(self, **changes)

    def with_plan(self, plan: PlanProposal) -> "GoalState":
        if self.status != GoalStatus.ACTIVE:
            raise ValueError("已结束的 Goal 不能更新计划")
        if plan.subgoals != self.subgoals:
            raise ValueError("计划不能替换固定 Goal 的子目标")
        return self._updated(
            subgoals=plan.subgoals,
            current_subgoal=plan.current_subgoal,
        )

    def get_variable(self, name: str) -> Optional[str]:
        for variable_name, value in self.variables:
            if variable_name == name:
                return value
        return None

    def with_variable(self, name: str, value: str) -> "GoalState":
        if self.status != GoalStatus.ACTIVE:
            raise ValueError("已结束的 Goal 不能更新变量")
        if not name:
            raise ValueError("变量名不能为空")
        values = dict(self.variables)
        values[name] = value
        return self._updated(variables=tuple(sorted(values.items())))

    def advance_subgoal(self, next_subgoal: Optional[str], evidence_ref: str) -> "GoalState":
        if self.status != GoalStatus.ACTIVE:
            raise ValueError("已结束的 Goal 不能推进子目标")
        if not evidence_ref:
            raise ValueError("推进子目标必须提供证据引用")
        if next_subgoal is not None:
            if next_subgoal not in self.subgoals:
                raise ValueError("下一子目标不属于固定 Goal")
            current_index = (
                self.subgoals.index(self.current_subgoal)
                if self.current_subgoal in self.subgoals
                else -1
            )
            if self.subgoals.index(next_subgoal) != current_index + 1:
                raise ValueError("只能推进到固定 Goal 的相邻子目标")
            return self._updated(
                current_subgoal=next_subgoal,
                last_feedback=f"VERIFIED:{evidence_ref}",
            )
        return self._updated(
            current_subgoal=None,
            status=GoalStatus.UNKNOWN,
            last_feedback=f"VERIFIED:{evidence_ref}",
        )

    def with_pending_action(self, action: ActionProposal) -> "GoalState":
        if self.status != GoalStatus.ACTIVE:
            raise ValueError("已结束的 Goal 不能生成动作")
        if self.pending_action_id is not None:
            raise ValueError("已有动作等待执行")
        if action.action_id in self.completed_action_ids:
            raise ValueError("动作 ID 已经使用")
        if action.source_state_version != self.state_version:
            raise ValueError("动作来自过期页面")
        return self._updated(
            pending_action_id=action.action_id,
            budget=self.budget.consume_event(),
        )

    def apply_execution(self, record: ExecutionRecord) -> "GoalState":
        if self.pending_action_id != record.action_id:
            raise ValueError("ExecutionRecord 与 pending_action_id 不匹配")
        if record.before_state_version != self.state_version:
            raise ValueError("执行记录的前置页面版本不匹配")
        if record.next_subgoal is not None:
            raise ValueError("执行记录不能直接推进子目标，需先验证")
        if record.execution_status == "EXECUTION_UNKNOWN" or (
            record.dispatched and record.after_state_version is None
        ):
            return self._updated(last_feedback="EXECUTION_UNKNOWN")
        new_version = (
            self.state_version
            if record.after_state_version is None
            else record.after_state_version
        )
        return self._updated(
            state_version=new_version,
            pending_action_id=None,
            last_feedback=record.execution_status,
            completed_action_ids=self.completed_action_ids + (record.action_id,),
        )

    def finish(self, status: Union[GoalStatus, str]) -> "GoalState":
        terminal = GoalStatus(status)
        if terminal == GoalStatus.ACTIVE:
            raise ValueError("ACTIVE 不是终态")
        if terminal == GoalStatus.PASS:
            raise ValueError("PASS 需要结果验证，第一周暂不支持")
        if self.pending_action_id is not None:
            raise ValueError("存在待确认动作时不能结束 Goal")
        return self._updated(status=terminal, pending_action_id=None)
