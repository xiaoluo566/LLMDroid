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
