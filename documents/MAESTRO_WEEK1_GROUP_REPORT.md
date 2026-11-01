# MAESTRO-LLMDroid 第一周组会汇报

## 1. 本周目标

基于已经复现的 LLMDroid-Droidbot 仓库，开始多 Agent 二次开发。第一周不重写原论文流程，先增加 A 侧最小决策内核，让固定 Goal 可以被结构化表示、生成一个动作并交给后续 Droidbot 桥接层执行。

## 2. 本周完成内容

### 2.1 SDD 和接口

完成第一周 SDD，明确：

- A 负责状态、协议、Planner、Data 和决策图。
- B 负责 policy、UTG、InputManager 和 EventLog 接入。
- Agent 只返回结构化提案，不直接操作设备。
- 页面版本、任务修订版本和动作 ID 分开管理。

### 2.2 新增 A 侧模块

新增 LLMDroid-Droidbot/droidbot/maestro：

- schemas.py：PageSnapshot、GoalSpec、GoalState、ActionProposal、DecisionResult、ExecutionRecord 等对象和校验。
- session.py：创建 GoalState。
- planner.py：固定 Goal 的最小子目标规划。
- data_agent.py：点击和文本输入提案。
- graph.py：一轮决策，返回 ACTION、OBSERVE_ONLY、WAIT 或 STOP。
- decision_backend.py：统一模型请求协议、JSON 解析、可替换的 FakeBackend 和回调适配器。

### 2.3 测试和 fixture

新增标准库 unittest 测试和脱敏页面 fixture，覆盖：

- 合法点击和文本输入。
- 非法动作结构。
- 页面版本过期。
- 缺少控件。
- Goal 终态。
- 预算扣减。
- 重复 action_id。
- 非法模型 JSON。
- FakeBackend 请求记录。

## 3. 相比原论文新增了什么

原 LLMDroid 已经提供页面摘要、StateCluster、FunctionList、UTG、LLMAgent 和 InputManager。本周没有改动这些底座。

本周新增的是原论文中没有独立抽象出来的 A 侧控制层：

1. 用 GoalState 保存固定 Goal、当前子目标、变量、预算和待执行动作。
2. 用 ActionProposal 统一表达模型或规则产生的动作。
3. 用 state_version 拒绝基于旧页面生成的动作。
4. 用 decision graph 把规划、动作生成和停止条件分开。
5. 用 ExecutionRecord 为后续真实设备执行提供统一回调格式。

这些改动暂时不带来覆盖率提升，作用是为 Planner、Data、Oracle 的后续接入提供稳定边界。

## 4. 为什么这样开发

- 保留原论文执行链，降低对复现结果的影响。
- 让 A 和 B 通过固定对象并行开发，减少直接修改同一文件。
- 先验证状态、协议和单步动作，再接入真实设备，避免同时排查模型、Android 和状态机问题。
- 通过状态版本和动作 ID 防止旧页面动作、重复动作和预算重复扣减。
- 通过 FakeBackend 和脱敏 fixture 在不连接设备、不使用 API key 的情况下完成测试。

## 5. 测试结果

执行命令：

~~~text
python -m unittest discover -s tests -v
python -m compileall -q LLMDroid-Droidbot/droidbot/maestro tests/maestro_tests
~~~

结果：

- 16 个单元测试全部通过。
- A 侧模块整体覆盖率 85%。
- 测试未连接 Android 设备，也未调用真实模型。

## 6. 本周尚未完成

- 尚未修改原 policy、UTG、InputManager 和 EventLog。
- 尚未完成真实设备动作。
- 尚未接入真实 Oracle Agent。
- 尚未实现复杂失败恢复、严格回放和多应用实验。

这些内容留到下一阶段，避免第一周同时改变论文仓库的执行链和新增控制层。

## 7. 下一周计划

1. B 提供真实 PageSnapshot 和 ExecutionRecord，完成 bridge 接口。
2. 将一个固定 Goal 的 ActionProposal 接入原 InputManager。
3. 验证发送前页面版本检查和发送后状态回调。
4. 用一个真实 Droidbot 任务完成一次无人手动点击的单步或短任务闭环。
5. 根据真实页面字段补充 Data 输入约束。
6. 再决定是否把真实 LLMAgent 调用接入 decision_backend。

下一周的验收标准是：一个固定 Goal 能从 A 的状态/决策模块进入原 Droidbot 执行入口，并留下可关联的前后状态和事件证据。
