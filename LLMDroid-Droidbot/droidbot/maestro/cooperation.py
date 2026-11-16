"""A 侧单轮协作；设备发送与规则求值由 B 注入。"""

from dataclasses import dataclass, replace
from typing import Optional, Tuple

from .decision_backend import ResponseFormatError
from .graph import DecisionOutcome, decide_once, _accept_action
from .schemas import ActionKind, DecisionKind, DecisionResult, GoalState, GoalStatus
from .stage_steps import execution_step


@dataclass(frozen=True)
class Feedback:
    feedback_id: str
    run_id: str
    goal_id: str
    action_id: str
    state_version: int
    category: str
    reason: str
    evidence_refs: Tuple[str, ...]
    constraints: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'evidence_refs', tuple(self.evidence_refs))
        object.__setattr__(self, 'constraints', tuple(tuple(p) for p in self.constraints))
        if not all((self.feedback_id, self.run_id, self.goal_id, self.action_id, self.reason)):
            raise ValueError('反馈缺少标识或原因')
        if self.state_version < 0 or not self.evidence_refs or not all(self.evidence_refs):
            raise ValueError('反馈缺少页面版本或证据')
        if self.category not in {'PATH_FAILURE', 'DATA_FAILURE', 'CONSTRAINT_CONFLICT',
                                 'ORACLE_UNCERTAIN', 'ENV_FAILURE', 'AGENT_FAILURE'}:
            raise ValueError('未知反馈类别')
        if any(len(pair) != 2 or not all(isinstance(v, str) for v in pair)
               for pair in self.constraints):
            raise ValueError('约束必须是字符串键值对')


@dataclass(frozen=True)
class StepAssessment:
    """B 的规则求值结果，A 仅验证关联；不是模型自行声称的 PASS。"""
    run_id: str
    goal_id: str
    action_id: str
    state_version: int
    rule_id: str
    oracle_revision: int
    status: str
    evidence_refs: Tuple[str, ...]
    confirmed_variables: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'evidence_refs', tuple(self.evidence_refs))
        object.__setattr__(self, 'confirmed_variables', tuple(self.confirmed_variables))


@dataclass(frozen=True)
class ObservationRequest:
    run_id: str
    goal_id: str
    state_version: int
    evidence_requests: Tuple[str, ...]


@dataclass(frozen=True)
class EvidenceProposal:
    evidence_requests: Tuple[str, ...]
    model_input_tokens: Optional[int] = None
    model_output_tokens: Optional[int] = None


@dataclass(frozen=True)
class CollaborativeOutcome:
    result: DecisionResult
    state: GoalState
    route: str
    observation: Optional[ObservationRequest] = None
    plan: object = None
    model_input_tokens: Optional[int] = None
    model_output_tokens: Optional[int] = None


def merge_assessment(state: GoalState, assessment: StepAssessment) -> GoalState:
    if (state.status != GoalStatus.ACTIVE or state.pending_action_id is not None
            or assessment.run_id != state.run_id or assessment.goal_id != state.goal_id
            or assessment.state_version != state.state_version
            or not state.completed_action_ids
            or assessment.action_id != state.completed_action_ids[-1]
            or assessment.action_id not in state.observed_action_ids
            or dict(state.action_subgoals).get(assessment.action_id) != state.current_subgoal
            or assessment.action_id in state.evaluated_action_ids):
        raise ValueError('评估与当前已观察动作不匹配')
    if (not assessment.rule_id or assessment.oracle_revision < 1
            or not assessment.evidence_refs or not all(assessment.evidence_refs)
            or assessment.status not in {'PASS', 'FAIL', 'UNKNOWN'}):
        raise ValueError('评估缺少规则、证据或结果')
    candidates = tuple(v for v in state.proposed_variables if v.action_id == assessment.action_id)
    names = tuple(v.name for v in candidates)
    if assessment.status == 'PASS' and set(assessment.confirmed_variables) != set(names):
        raise ValueError('候选变量未全部确认，不能推进步骤')
    if (len(set(assessment.confirmed_variables)) != len(assessment.confirmed_variables)
            or any(name not in names for name in assessment.confirmed_variables)
            or (assessment.status != 'PASS' and assessment.confirmed_variables)):
        raise ValueError('确认变量不属于通过验证的动作')
    if assessment.status == 'UNKNOWN':
        return state._updated(last_feedback='ORACLE_UNCERTAIN')
    values = dict(state.variables)
    for item in candidates:
        if item.name in assessment.confirmed_variables:
            values[item.name] = item.value
    state = state._updated(
        variables=tuple(sorted(values.items())),
        proposed_variables=tuple(v for v in state.proposed_variables if v not in candidates),
        evaluated_action_ids=state.evaluated_action_ids + (assessment.action_id,),
        last_feedback=assessment.status,
    )
    if assessment.status == 'PASS':
        if state.current_subgoal not in state.subgoals:
            raise ValueError('当前子目标无效')
        index = state.subgoals.index(state.current_subgoal) + 1
        next_step = state.subgoals[index] if index < len(state.subgoals) else None
        state = state.advance_subgoal(next_step, assessment.evidence_refs[0])
    return state


def _out(state, kind, reason, route='control', **kwargs):
    if kind == DecisionKind.STOP and state.status == GoalStatus.ACTIVE:
        state = state.finish(GoalStatus.BLOCKED)
    return CollaborativeOutcome(DecisionResult(kind, reason), state, route, **kwargs)


def _charge(state):
    return state._updated(budget=state.budget.consume_model_call())


def _add_usage(previous, current):
    return tuple(None if a is None or b is None else a + b
                 for a, b in zip(previous, current))


def _validate_binding(state, action, variable=None):
    kind, _, expression = execution_step(state.current_subgoal or '').partition(':')
    widget_id, separator, value = expression.partition('=')
    if action.kind != ActionKind.TEXT:
        valid = action.kind.value.lower() == kind and action.widget_id == expression
    else:
        valid = separator and action.widget_id == widget_id
        if kind == 'text':
            valid = valid and action.value == value
        elif kind == 'text_ref':
            valid = valid and action.value_ref == value
        elif kind == 'generate':
            valid = valid and variable is not None and variable.name == value
        else:
            valid = False
    if not valid or (kind != 'generate' and variable is not None):
        raise ValueError('Data 提案与当前步骤不匹配')


def refresh_observation(state, snapshot, action_id):
    """B 只读补观察后更新页面绑定；不确认变量，也不解除不明写操作。"""
    if (state.status != GoalStatus.ACTIVE or state.pending_action_id is not None
            or snapshot.run_id != state.run_id or snapshot.state_version <= state.state_version
            or not state.completed_action_ids or action_id != state.completed_action_ids[-1]):
        raise ValueError('补观察未关联当前已返回动作')
    ready = state.last_feedback in {'PATH_FAILURE', 'CONSTRAINT_CONFLICT'} and not state.proposed_variables
    feedback = 'READY_AFTER_PATH' if ready else state.last_feedback
    return state._updated(state_version=snapshot.state_version, last_feedback=feedback)


def _feedback_state(state, feedback):
    if (feedback.run_id != state.run_id or feedback.goal_id != state.goal_id
            or feedback.state_version != state.state_version
            or not state.completed_action_ids
            or feedback.action_id != state.completed_action_ids[-1]
            or dict(state.action_subgoals).get(feedback.action_id) != state.current_subgoal):
        raise ValueError('反馈与当前执行不匹配')
    key = f'{feedback.category}:{state.current_subgoal}'
    counts = dict(state.recovery_counts)
    limit = 1 if feedback.category == 'ORACLE_UNCERTAIN' else 2
    if counts.get(key, 0) >= limit:
        raise ValueError('恢复次数已耗尽')
    counts[key] = counts.get(key, 0) + 1
    return state._updated(processed_feedback_ids=state.processed_feedback_ids + (feedback.feedback_id,),
                          recovery_counts=tuple(sorted(counts.items())), last_feedback=feedback.category)


def collaborate_once(state, snapshot, planner, data_agent, oracle=None, feedback=None):
    """一次只产生一个动作或只读观察需求；调用方通过原执行器完成动作。"""
    if state.status != GoalStatus.ACTIVE:
        return _out(state, DecisionKind.STOP, 'Goal 已结束')
    if snapshot.run_id != state.run_id or snapshot.state_version != state.state_version:
        return _out(state, DecisionKind.OBSERVE_ONLY, '页面不匹配，重新观察')
    if state.pending_action_id:
        kind = DecisionKind.OBSERVE_ONLY if state.last_feedback == 'EXECUTION_UNKNOWN' else DecisionKind.WAIT
        return _out(state, kind, '先确认上一动作，禁止重发')
    if feedback and feedback.feedback_id in state.processed_feedback_ids:
        return _out(state, DecisionKind.WAIT, '反馈已处理')
    if not feedback and state.completed_action_ids:
        action_id = state.completed_action_ids[-1]
        if (dict(state.action_subgoals).get(action_id) == state.current_subgoal
                and state.last_feedback != 'READY_AFTER_PATH'):
            return _out(state, DecisionKind.WAIT, '等待当前步骤评估或定向反馈')
    plan = None
    route = 'data'
    usage = (0, 0)
    try:
        if feedback:
            state = _feedback_state(state, feedback)
            if feedback.category in {'ENV_FAILURE', 'AGENT_FAILURE'}:
                return _out(state, DecisionKind.STOP, '执行环境或 Agent 失败')
            if feedback.category in {'PATH_FAILURE', 'CONSTRAINT_CONFLICT'}:
                if state.budget.replans_left <= 0:
                    raise ValueError('重规划预算耗尽')
                state = state._updated(budget=replace(state.budget, replans_left=state.budget.replans_left - 1))
                if getattr(planner, 'uses_model', False):
                    state = _charge(state)
                plan = planner.replan(state, snapshot, feedback.reason)
                state = state.with_plan(plan)
                return _out(state, DecisionKind.OBSERVE_ONLY, '路径反馈已交 Planner', 'planner', plan=plan,
                            model_input_tokens=plan.model_input_tokens, model_output_tokens=plan.model_output_tokens)
            if feedback.category == 'ORACLE_UNCERTAIN':
                if oracle is None:
                    raise ValueError('缺少 Oracle 接口')
                if getattr(oracle, 'uses_model', False):
                    state = _charge(state)
                evidence = oracle.evidence_requests(state, snapshot, feedback)
                if isinstance(evidence, EvidenceProposal):
                    requirements = tuple(evidence.evidence_requests)
                    usage = (evidence.model_input_tokens, evidence.model_output_tokens)
                else:
                    requirements = tuple(evidence)
                    usage = (None, None) if getattr(oracle, 'uses_model', False) else (0, 0)
                if not requirements or any(not isinstance(r, str) or not r for r in requirements):
                    raise ValueError('Oracle 观察需求为空或非法')
                observation = planner.request_observation(state, snapshot, requirements)
                if (not isinstance(observation, ObservationRequest)
                        or observation.run_id != state.run_id or observation.goal_id != state.goal_id
                        or observation.state_version != snapshot.state_version
                        or tuple(observation.evidence_requests) != requirements):
                    raise ValueError('Planner 观察提案未保留 Oracle 需求和页面绑定')
                return _out(state, DecisionKind.OBSERVE_ONLY, 'Oracle 需求已交 Planner 只读观察',
                            'oracle', observation=observation,
                            model_input_tokens=usage[0], model_output_tokens=usage[1])
            # 明确输入拒绝后废弃该动作的候选值；保留在反馈中的字段约束。
            constraints = {key: value for step, key, value in state.input_constraints
                           if step == state.current_subgoal}
            constraints.update(dict(feedback.constraints))
            if state.last_action is not None:
                rejected = state.last_action.value
                if state.last_action.value_ref:
                    rejected = state.get_variable(state.last_action.value_ref)
                if rejected is not None:
                    constraints['rejected_value'] = rejected
            feedback = replace(feedback, constraints=tuple(sorted(constraints.items())))
            state = state._updated(input_constraints=tuple(
                item for item in state.input_constraints if item[0] != state.current_subgoal
            ) + tuple((state.current_subgoal, key, value) for key, value in sorted(constraints.items())))
            state = state._updated(proposed_variables=tuple(
                v for v in state.proposed_variables if v.action_id != feedback.action_id))
        if state.budget.events_left <= 0:
            return _out(state, DecisionKind.STOP, '事件预算耗尽')
        if state.current_subgoal is None:
            if getattr(planner, 'uses_model', False):
                state = _charge(state)
            plan = planner.plan(state, snapshot)
            if getattr(planner, 'uses_model', False):
                usage = (plan.model_input_tokens, plan.model_output_tokens)
            state = state.with_plan(plan)
        if not hasattr(data_agent, 'needs_model'):
            outcome = decide_once(state, snapshot, planner, data_agent)
            if outcome.result.action:
                _validate_binding(state, outcome.result.action)
            if feedback and outcome.result.action:
                action = outcome.result.action
                value = action.value if action.value_ref is None else state.get_variable(action.value_ref)
                if value == dict(feedback.constraints).get('rejected_value'):
                    return _out(state, DecisionKind.STOP, '规则 Data 无法修正被拒输入')
            if outcome.result.kind == DecisionKind.STOP:
                return _out(outcome.state, DecisionKind.STOP, outcome.result.reason)
            return CollaborativeOutcome(outcome.result, outcome.state, route,
                                        model_input_tokens=usage[0], model_output_tokens=usage[1])
        uses_data_model = data_agent.needs_model(state)
        if uses_data_model:
            state = _charge(state)
        proposal = data_agent.propose(state, snapshot, feedback)
        if uses_data_model:
            usage = _add_usage(usage, (proposal.model_input_tokens, proposal.model_output_tokens))
        _validate_binding(state, proposal.action, proposal.variable)
        if feedback:
            action = proposal.action
            value = action.value if action.value_ref is None else state.get_variable(action.value_ref)
            if value == dict(feedback.constraints).get('rejected_value'):
                raise ValueError('Data 仍然返回被拒输入')
        accepted = _accept_action(state, snapshot, proposal.action, plan)
        next_state = accepted.state
        if proposal.variable:
            next_state = next_state.stage_variable(proposal.variable)
        return CollaborativeOutcome(accepted.result, next_state, route,
                                    model_input_tokens=usage[0], model_output_tokens=usage[1])
    except ResponseFormatError as error:
        usage = _add_usage(usage, (error.input_tokens, error.output_tokens))
        return _out(state, DecisionKind.STOP, '角色提案未通过校验', route,
                    model_input_tokens=usage[0], model_output_tokens=usage[1])
    except Exception:
        return _out(state, DecisionKind.STOP, '协作输入或角色调用失败', route,
                    model_input_tokens=usage[0], model_output_tokens=usage[1])
