# MAESTRO-LLMDroid 第一周 SDD

## 目标

在论文仓库已有的 Droidbot 执行链之上，增加一个可独立测试的 A 侧决策内核。第一周只打通固定 Goal 的最小闭环：

~~~text
PageSnapshot
  -> GoalState
  -> Planner/Data
  -> ActionProposal
  -> DecisionResult
  -> B 的执行桥接
~~~

第一周不修改覆盖率触发器、UTG 算法、InputManager、原 LLMAgent 主循环、Humanoid/Fastbot，也不实现真实 Oracle 推理和严格回放。

## 边界

### A 负责

- schemas：双方共享的不可变消息结构和校验。
- session：GoalState、预算、变量和动作去重。
- planner：固定 Goal 的最小子目标规划。
- data_agent：点击和文本输入提案。
- decision_backend：模型适配协议、JSON 解析和 FakeBackend。
- graph：一轮决策，只返回 ACTION、OBSERVE_ONLY、WAIT 或 STOP。
- 纯 Python 单元测试和脱敏 fixture。

### B 负责

- 从论文仓库的 policy、DeviceState、UTG、InputManager 和 EventLog 提供观察与执行桥接。
- 将 ActionProposal 绑定为原 InputEvent。
- 返回真实 ExecutionRecord。

## 核心对象

| 对象 | 作用 |
|---|---|
| PageSnapshot | B 对当前页面的一次观察 |
| GoalSpec | 固定 Goal 和子目标 |
| GoalState | A 侧唯一任务状态 |
| PlanProposal | Planner 给出的子目标计划 |
| ActionProposal | Data 提议的一个动作 |
| DecisionResult | Graph 返回给 B 的结果 |
| ExecutionRecord | B 返回的真实执行证据 |

## 版本规则

- state_version 表示页面观察版本，必须与 ActionProposal 的 source_state_version 相等才允许执行。
- revision 表示 GoalState 自身的更新次数，与页面版本分开。
- 状态更新返回新对象，不直接修改旧对象。
- 一个 action_id 只能被合并一次。
- 发送不等于业务成功；第一周只记录执行结果，不判定完整业务 Goal。

## 第一周验收

1. 固定 Goal 能生成一个合法点击或文本动作。
2. 页面版本过期时返回 OBSERVE_ONLY，不返回 ACTION。
3. 缺控件、非法 JSON、预算耗尽和重复 action_id 都有确定结果。
4. Graph 不导入 Android 依赖，不直接调用 ADB 或 InputManager。
5. 原论文仓库代码不被改写；B 后续通过 bridge 接入。
