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

## A 侧补全：预算、反馈与模型接口

- 设备动作和模型请求在发出前分别计入对应预算；失败请求也消耗模型调用次数。
- 模型调用通过注入的 OpenAI 兼容客户端执行，显式设置超时；对支持 with_options 的 SDK 禁用自动重试。其他注入客户端由调用者保证不重试。
- 模型调用次数在请求前扣减。模型响应成功解析后，若提供 usage 则返回调用者；缺失时标记未知。尚未实现 Token 总额和 Goal 时间上限。
- Planner 只从固定 Goal 已给出的子目标中选择；Data 继续生成当前可见控件的点击或输入提案。
- 决策出口重新校验控件、动作类型和页面版本；模型异常或预算不足返回 STOP，不执行动作。
- 执行反馈的动作 ID 和执行前页面版本必须匹配；发送后观察结果不确定时保留待处理动作，等待进一步观察，不自动重发。
- 单纯的设备观察不能证明子目标完成：第一周不接受执行记录直接推进子目标，也不提供未经过验证的 PASS 终态。
- 真实设备发送和执行前页面复核由 B 桥接负责；本阶段验证不替代双方真实设备验收。

## 第一周验收

1. 固定 Goal 能生成一个合法点击或文本动作。
2. 页面版本过期时返回 OBSERVE_ONLY，不返回 ACTION。
3. 缺控件、非法 JSON、预算耗尽和重复 action_id 都有确定结果。
4. Graph 不导入 Android 依赖，不直接调用 ADB 或 InputManager。
5. 原论文仓库代码不被改写；B 后续通过 bridge 接入。

## 第二周 A 侧 SDD 补充

### 目标

在不修改 B 侧 policy、UTG、InputManager 的前提下，让 A 侧能够管理固定 Goal 的连续步骤，并提供 Single-Agent 对照和有限重规划接口。

### 状态推进

- 设备观察只能更新页面版本和反馈，不能直接推进子目标。
- 只有显式调用 advance_subgoal(next_subgoal, evidence_ref) 才能推进固定 Goal 的相邻子目标。
- 证据不足时只能进入 UNKNOWN 或继续等待，不能标记 PASS。
- 未发送、发送不明或前置页面版本不匹配的 ExecutionRecord 不能推进任务。

### 变量引用

- Data 支持 text_ref:控件ID=变量名 子目标。
- 变量不存在、变量为空或控件类型不匹配时返回受控失败。
- ActionProposal 只携带 value 或 value_ref 其中一个；设备侧如何解析引用由后续 bridge 约定。

### 有限重规划

- Planner 提供 replan(state, snapshot, reason) 接口。
- 重规划只能保留原 Goal 的子目标集合，不能静默换 Goal。
- 第一阶段只记录重规划原因并保留当前子目标；真实 UTG 路径查询由 B 后续提供。

### Single-Agent 对照

- DecisionMode.SINGLE_AGENT 通过一次模型请求同时返回计划和动作。
- 与三角色模式共用 GoalState、动作校验、预算和唯一设备执行边界。
- 第一周和第二周只实现对照入口，不据此宣称 Single-Agent 或多 Agent 的效果差异。
