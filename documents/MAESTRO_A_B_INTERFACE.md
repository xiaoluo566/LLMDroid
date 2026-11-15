# MAESTRO A/B 接口说明

## 目的

第一周把 A 的纯 Python 决策内核和 B 的 Droidbot 执行桥接分开。A 只生成经过校验的决策，B 负责把动作绑定到论文仓库已有的 InputEvent 并执行。

## A 提供给 B

~~~python
decide_once(
    state: GoalState,
    snapshot: PageSnapshot,
    planner: FixedGoalPlanner,
    data_agent: RuleBasedDataAgent,
) -> DecisionOutcome
~~~

返回的 DecisionResult 只有四种：

- ACTION：携带一个 ActionProposal。
- OBSERVE_ONLY：当前页面无效或过期，只能重新观察。
- WAIT：已有动作等待执行结果。
- STOP：本轮不能继续，必须携带 reason。

第一周 ActionProposal 只支持 CLICK 和 TEXT。

## B 提供给 A

~~~python
observe() -> PageSnapshot
execute(action: ActionProposal) -> ExecutionRecord
~~~

B 的 execute 必须继续使用论文仓库原有 InputManager 和 EventLog，不在桥接层另写 ADB 驱动。

## 版本和状态规则

- PageSnapshot.state_version 表示页面观察版本。
- ActionProposal.source_state_version 必须等于当前 GoalState.state_version。
- revision 表示 GoalState 自身更新次数，与页面版本分开。
- 一个 pending_action_id 只能接受一次 ExecutionRecord。
- 发送成功不等于 Goal 成功；第一周只记录执行证据。
- before_state_version 必须与待处理动作所在页面版本一致。
- 若发送结果不确定或发送后未取得页面观察，execution_status 使用 EXECUTION_UNKNOWN，after_state_version 为空；A 保留待处理动作并等待后续观察，不自动重发。
- 第一周 ExecutionRecord 不接受 next_subgoal 来推进任务；仅凭观察不能判断子目标已完成。PASS 需要后续 RuleChecker/Oracle 提供验证机制，目前不支持直接标记。
- 模型规划可选用 ModelGoalPlanner 和注入的 OpenAICompatibleBackend；模型调用前扣减次数预算。DecisionOutcome 的 model_input_tokens 和 model_output_tokens 在服务返回用量时保留，包括响应格式错误或计划校验失败；未返回时为 None。
- OpenAI SDK 客户端会通过 with_options 关闭自动重试；其他注入客户端由调用者确保超时与不重试。当前未实现 Token 总额、Goal 总时限和重规划次数的完整门禁。

## 第一周固定示例

~~~text
Goal: 输入标题并保存
子目标:
  text:title=hello
  click:save
~~~

A 返回：

~~~json
{
  "kind": "TEXT",
  "widget_id": "title",
  "value": "hello",
  "source_state_version": 1
}
~~~

B 执行后返回：

~~~json
{
  "action_id": "run-1-action-1",
  "dispatched": true,
  "before_state_version": 1,
  "after_state_version": 2,
  "execution_status": "OBSERVED"
}
~~~

## 不属于第一周接口

- Oracle 真实模型调用。
- 复杂输入类型。
- 自动重规划和多轮恢复。
- 跨进程 checkpoint。
- Humanoid/Fastbot 接入。

## 第二周 A 侧接口

~~~python
advance_subgoal(next_subgoal, evidence_ref) -> GoalState
planner.replan(state, snapshot, reason) -> PlanProposal
decide_once(..., mode=DecisionMode.SINGLE_AGENT,
            single_agent=SingleAgentDecisionAgent(...))
~~~

advance_subgoal 只接受固定 Goal 中的相邻子目标和非空证据引用。ExecutionRecord 不能直接推进子目标，设备观察也不能直接产生 PASS。

replan 保留原 Goal 和子目标集合，先记录原因；当前版本不承担 UTG 查询，真实路径候选仍由 B 的底座接口提供。

Single-Agent 模式用一次模型请求同时提出当前子目标和动作，用于后续对照实验；它与多角色模式共用状态、版本和动作校验。

## 第三周 A 侧接口

候选入口位于 goal_selection：B 将真实页面/功能映射为 GoalCandidate，提供 run_id、candidate_id、state_id、function、observation_ref、target_cluster_id 和已观察 widgets。模型只能从候选选择并生成这些控件上的局部步骤；不允许编造来源或控件。选择成功和失败都返回模型预算与可用 usage。

~~~python
selected = ModelGoalSelector(backend).select(run_id, candidates, budget, request_id)
state = selected.create_state(state_version=current_page_version)
outcome = collaborate_once(state, snapshot, planner, ModelDataAgent(backend),
                           oracle=oracle_adapter, feedback=feedback)
~~~

create_state 保留候选来源且沿用扣减后预算。GoalCandidate 的具体来源真实性由 B 保证；A 不直接调用 FunctionList。目标页与当前页不同时先由 B 导航，不能把目标页控件当成当前页控件。

新增 generate:widget=variable 步骤：Data 模型提出文本动作和 VariableProposal，编排层只暂存候选，不开放 value_ref。执行结果经 state.apply_execution 合并后，B 提供 StepAssessment：run_id、goal_id、action_id、当前观察版本、聚合 rule_id、冻结 oracle_revision、PASS/FAIL/UNKNOWN、evidence_refs 和 confirmed_variables。

StepAssessment 必须是 B 对当前步骤所有必需规则的聚合结果，不能直接转发模型自行宣布的 PASS。A 校验最近实际发送并观察到的动作及所属子目标；PASS 必须确认该动作所有候选变量才推进相邻步骤。FAIL 废弃候选但不推进；UNKNOWN 保留候选等待证据。最后步骤通过仍保留 UNKNOWN，尚未开放最终 Goal PASS。完整目标判定与独立评价待 B 接入。

~~~python
state = state.apply_execution(execution_record)
state = merge_assessment(state, assessment)
~~~

Feedback 带 feedback_id、run/goal/action 标识、state_version、类别、reason、evidence_refs、constraints。类别支持 PATH_FAILURE、CONSTRAINT_CONFLICT、DATA_FAILURE、ORACLE_UNCERTAIN、ENV_FAILURE、AGENT_FAILURE。只接受当前步骤最近返回动作的反馈，重复 ID 不重试；旧步骤反馈不能作用到新步骤。

DATA_FAILURE 可携带 date_format=YYYY-MM-DD、min_date/max_date、min_length/max_length。已拒值由 A 从对应动作推导，约束按子目标保留。固定文本/已有引用不能自行变成新值，无法修正则明确停止；需要生成和修正的数据应使用 generate。

Oracle 适配器提供 evidence_requests(state,snapshot,feedback)，返回 EvidenceProposal(evidence_requests,model_input_tokens,model_output_tokens)，若使用模型须声明 uses_model=True。兼容只返回需求元组，但使用模型且无 usage 时记未知。Planner.request_observation 接收需求并产生 ObservationRequest，B 仅采集只读证据；之后通过 refresh_observation(state,new_snapshot,action_id) 更新页面版本，再提交评估。pending 为执行不明时仍必须经原执行回调确认，不能借补观察自动重发。

新入口输出 CollaborativeOutcome，保留四类决策、route、可选 plan/observation 和合计用量；STOP 写入 BLOCKED。它不操作设备、不求值 Oracle 规则，不修改旧 Single-Agent 入口。详细边界和验收见 MAESTRO_WEEK3_SDD.md。
