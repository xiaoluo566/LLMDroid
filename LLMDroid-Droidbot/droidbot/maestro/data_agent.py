from .schemas import ActionKind, ActionProposal, GoalState, PageSnapshot
from .stage_steps import execution_step


_ACTIONS_WITHOUT_VALUE = {kind.value.lower(): kind for kind in ActionKind if kind != ActionKind.TEXT}


class DataProposalError(ValueError):
    """当前页面不能满足 Data 提案。"""


class RuleBasedDataAgent:
    """从固定步骤生成当前页面上的单个动作提案。"""

    def propose(self, state: GoalState, snapshot: PageSnapshot) -> ActionProposal:
        subgoal = execution_step(state.current_subgoal)
        if not subgoal:
            raise DataProposalError("当前没有可执行子目标")

        operation, _, widget_id = subgoal.partition(":")
        kind = _ACTIONS_WITHOUT_VALUE.get(operation)
        if kind is not None:
            widget = snapshot.find_widget(widget_id)
            if not widget.supports(kind):
                raise DataProposalError(f"控件 {widget_id} 不支持 {kind.value} 动作")
            return ActionProposal(
                action_id=f"{state.run_id}-{state.goal_id}-action-{state.revision + 1}",
                kind=kind,
                widget_id=widget.widget_id,
                source_state_version=snapshot.state_version,
            )

        if subgoal.startswith("text:"):
            expression = subgoal[len("text:") :]
            if "=" not in expression:
                raise DataProposalError("文本子目标必须使用 text:widget=value 格式")
            widget_id, value = expression.split("=", 1)
            widget = snapshot.find_widget(widget_id)
            if not widget.supports(ActionKind.TEXT):
                raise DataProposalError(f"控件不可输入文本: {widget_id}")
            return ActionProposal(
                action_id=f"{state.run_id}-{state.goal_id}-action-{state.revision + 1}",
                kind=ActionKind.TEXT,
                widget_id=widget.widget_id,
                value=value,
                source_state_version=snapshot.state_version,
            )

        if subgoal.startswith("text_ref:"):
            expression = subgoal[len("text_ref:") :]
            if "=" not in expression:
                raise DataProposalError("引用子目标必须使用 text_ref:widget=variable 格式")
            widget_id, variable_name = expression.split("=", 1)
            if state.get_variable(variable_name) is None:
                raise DataProposalError(f"变量不存在: {variable_name}")
            widget = snapshot.find_widget(widget_id)
            if not widget.supports(ActionKind.TEXT):
                raise DataProposalError(f"控件不可输入文本: {widget_id}")
            return ActionProposal(
                action_id=f"{state.run_id}-{state.goal_id}-action-{state.revision + 1}",
                kind=ActionKind.TEXT,
                widget_id=widget.widget_id,
                value_ref=variable_name,
                source_state_version=snapshot.state_version,
            )

        raise DataProposalError(f"不支持的子目标: {subgoal}")
