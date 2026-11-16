"""Data 只提出输入和候选变量，由编排层合并并确认。"""

from dataclasses import dataclass
from datetime import date
from math import isfinite
import re
from typing import Optional

from .data_agent import DataProposalError, RuleBasedDataAgent
from .decision_backend import ModelRequest, ResponseFormatError
from .schemas import ActionKind, ActionProposal, GoalState, PageSnapshot, VariableProposal


@dataclass(frozen=True)
class DataProposal:
    action: ActionProposal
    variable: Optional[VariableProposal] = None
    model_input_tokens: Optional[int] = None
    model_output_tokens: Optional[int] = None


class ModelDataAgent:
    """固定动作沿用规则；generate 步骤使用独立 Data 模型上下文。"""

    uses_model = True

    def __init__(self, backend, timeout_seconds=30.0):
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('模型超时时间必须大于零')
        self.backend = backend
        self.timeout_seconds = timeout_seconds

    def needs_model(self, state: GoalState) -> bool:
        return bool(state.current_subgoal and state.current_subgoal.startswith('generate:'))

    def propose(self, state: GoalState, snapshot: PageSnapshot, feedback=None) -> DataProposal:
        if snapshot.run_id != state.run_id or snapshot.state_version != state.state_version:
            raise DataProposalError('页面与当前任务不匹配')
        if not self.needs_model(state):
            return DataProposal(RuleBasedDataAgent().propose(state, snapshot))
        widget_id, variable_name = self._binding(state, snapshot)
        request_id = f'{state.run_id}-{state.goal_id}-data-{state.revision}'
        response = self.backend.complete(ModelRequest(
            role='data', request_id=request_id, timeout_seconds=self.timeout_seconds,
            payload=self._payload(state, snapshot, feedback),
        ))
        try:
            if response.request_id != request_id:
                raise ValueError('响应请求 ID 不匹配')
            action = self._action(response.data, state, snapshot, widget_id, variable_name)
            self._validate_constraints(action.value, self._constraints(state, feedback))
            variable = VariableProposal(variable_name, action.value, action.action_id,
                                        snapshot.state_version)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ResponseFormatError('Data 输出未通过校验', response.input_tokens,
                                      response.output_tokens) from error
        return DataProposal(action, variable, response.input_tokens, response.output_tokens)

    @staticmethod
    def _binding(state, snapshot):
        expression = state.current_subgoal[len('generate:'):]
        if '=' not in expression:
            raise DataProposalError('生成步骤必须使用 generate:widget=variable 格式')
        widget_id, variable_name = expression.split('=', 1)
        if not widget_id.strip() or not variable_name.strip():
            raise DataProposalError('控件 ID 和变量名不能为空')
        try:
            widget = snapshot.find_widget(widget_id)
        except KeyError as error:
            raise DataProposalError('生成步骤找不到目标控件') from error
        if not widget.supports(ActionKind.TEXT):
            raise DataProposalError('目标控件不可输入文本')
        return widget_id, variable_name

    @staticmethod
    def _constraints(state, feedback):
        values = {key: value for step, key, value in state.input_constraints
                  if step == state.current_subgoal}
        values.update(dict(getattr(feedback, 'constraints', ())))
        return values

    @staticmethod
    def _payload(state, snapshot, feedback):
        feedback_view = None
        if feedback is not None:
            feedback_view = {
                'reason': getattr(feedback, 'reason', ''),
                'constraints': dict(getattr(feedback, 'constraints', ())),
            }
        return {
            'run_id': state.run_id,
            'goal_id': state.goal_id,
            'instruction': ('仅为当前 generate 步骤生成文本。返回 JSON：'
                            '{action:{kind:TEXT,widget_id,value},variable:{name,value}}。'
                            '控件和变量名必须来自当前步骤；两个 value 相同。'
                            '遵守页面约束与反馈，不重复 rejected_value，不调用设备。'),
            'goal_description': state.goal_description,
            'current_subgoal': state.current_subgoal,
            'state_id': snapshot.state_id,
            'state_version': snapshot.state_version,
            'widgets': [{'id': w.widget_id, 'type': w.widget_type, 'text': w.text,
                         'resource_id': w.resource_id}
                        for w in snapshot.widgets],
            'variables': dict(state.variables),
            'constraints': ModelDataAgent._constraints(state, feedback),
            'feedback': feedback_view,
        }

    @staticmethod
    def _action(data, state, snapshot, widget_id, variable_name):
        raw = data['action']
        variable = data['variable']
        if (raw['kind'] != 'TEXT' or raw['widget_id'] != widget_id
                or 'value_ref' in raw or not isinstance(raw['value'], str)
                or variable['name'] != variable_name or variable['value'] != raw['value']):
            raise ValueError('动作与当前生成步骤不匹配')
        return ActionProposal(
            action_id=f'{state.run_id}-{state.goal_id}-data-action-{state.revision + 1}',
            kind=ActionKind.TEXT, widget_id=widget_id,
            source_state_version=snapshot.state_version, value=raw['value'],
        )

    @staticmethod
    def _validate_constraints(value, constraints):
        if constraints.get('ascii_only') == 'true' and not value.isascii():
            raise ValueError('当前设备输入通路仅支持 ASCII 文本')
        if value == constraints.get('rejected_value'):
            raise ValueError('不能重复已被页面拒绝的输入')
        for key in ('min_length', 'max_length'):
            if key in constraints:
                limit = int(constraints[key])
                if limit < 0 or (key == 'min_length' and len(value) < limit):
                    raise ValueError('文本长度不满足约束')
                if key == 'max_length' and len(value) > limit:
                    raise ValueError('文本长度不满足约束')
        if any(key in constraints for key in ('date_format', 'min_date', 'max_date')):
            if constraints.get('date_format', 'YYYY-MM-DD') != 'YYYY-MM-DD':
                raise ValueError('不支持的日期格式')
            actual = ModelDataAgent._iso_date(value)
            if 'min_date' in constraints and actual < ModelDataAgent._iso_date(constraints['min_date']):
                raise ValueError('日期早于允许范围')
            if 'max_date' in constraints and actual > ModelDataAgent._iso_date(constraints['max_date']):
                raise ValueError('日期晚于允许范围')

    @staticmethod
    def _iso_date(value):
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('日期必须使用 YYYY-MM-DD 格式')
        return date.fromisoformat(value)
