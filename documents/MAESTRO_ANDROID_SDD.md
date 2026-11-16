# MAESTRO Android 动态探索接入设计

通过 LLMDROID_MAESTRO=1 启用。保留 DroidBot 的探索、FunctionList、AndroLog 低增长门和 UTG 导航。低增长发生后，Planner 从原 StateCluster 候选选 Goal，拒绝非法来源和不可执行步骤。到达后用真实快照运行 MAESTRO，每轮只发送一个事件，发送前在 EventLog 新采集的页面复核控件，再经原设备通路发送。导航失败可结束当前 Goal 后回到探索，不能替换活动 Goal。

冻结 Oracle 的观测规则后发送事件。设备回调必须包含动作 ID、发送状态和 EventLog 的前后页面。只接受来自目标 APK 的页面。匹配页面、控件和值后才确认变量。点击引起的界面变化只说明有可观测效果。缺证据只补观察，不能重发不确定动作。输入错误允许有限次数修正。步骤 PASS 和整个 Goal 的结论分别记录。

Trace 逐条记录 Planner 候选及提案、Data 请求及动作、Oracle 规则及评估、EventLog 引用和 Goal 结论。凭据不写入 trace。真实验收使用专用 MuMu1、复现插桩 APK 与模型配置。记录 APK 哈希、设备、覆盖率和时长。短跑用于验证接入，正式一小时覆盖率另外记录。

## 输入与接管验证

新增 enter:widget，要求目标是已经聚焦的输入控件，生成原 KeyEvent(ENTER)。文本与 Enter 是两个单独事件，每次都重新观察和校验。TimePlanner 的创建类别引导需要输入后 Enter，这类界面只有点击与文本提案时无法保证推进。

独立 ModelAndroidOracle 在发送前提出限定规则，确定性检查器依据设备结果求值。每次真实 Oracle 模型调用计入预算。JSON 接口启用 json_object 返回格式，同时继续执行 schema 校验。

LLMDROID_MAESTRO_CANARY=1 用于真实设备接管连通性验收，进入目标 App 并取得原页面摘要后提前触发一次候选选择。LLMDROID_MAESTRO_CANARY_GOALS 可以设置 1–5 次，默认 1。每次仍从真实候选选目标，达到上限后停止。trace 和 manifest 标记 early_guidance_canary，该批次不进入原低增长实验分母。常规运行保留原 AndroLog 低增长门。

设备回调或证据写入异常记录 UNKNOWN。发送结果不明确时保留 uncertain_write，policy 在下一轮停止本场运行，避免恢复探索后重新提交。原生 StateCluster 合同测试直接验证 FunctionList 字段及其页面映射。

## 真机验证结果

复现 TimePlanner APK SHA-256 为 ce1cf73980b2f8e5093963168f5f5a93515a15bab3c485253adc3b3f1852fbf0。专用设备为 127.0.0.1:16416，模型为复现配置中的 deepseek-v4.1-flash。本次验收协议是 early_guidance_canary，最多五个自动选择的候选 Goal，保留当前应用数据。该批次有 97 条 trace、8 个已发送并观察到的动作，独立请求为 Planner 5 次、Data 3 次、Oracle 8 次。三个文本输入变量经设备页面确认，一个 Enter 步骤具有原生 key 事件和界面变化证据。

原 AndroLog 门曾在诊断轮真实触发，但当时候选字段适配错误，未完成整条新链路。修复后的接管批次提前触发，不能作为原门槛下的完整一小时实验。所有 Goal 最终 UNKNOWN，尚未实现业务语义级成功确认。

真实运行中还修复了逻辑 viewport 1080×2280 与原 physical 1080×1920 不一致的问题，控件有效范围优先使用截图像素尺寸。模型请求启用 JSON mode，限制提案的控件类型。设备禁用了 ime enable，因此这次按原 adb 输入回退能力将新数据限制为 ASCII。常规协议不使用 canary 的输入优先选择提示。

## 验证与交付

先建立适配、模型规则、Enter 和发送失败的 RED 用例，再实现并回归。复现虚拟环境运行 164 项测试全部通过，maestro 模块语句覆盖率约 90%。原生 StateCluster、EventLog 和 InputManager 的合同测试使用复现依赖执行。git diff --check 与 Markdown 解析通过。

独立审查确认执行、证据关联和不确定写入的门禁。真实短跑的 8 条设备证据链均可追溯。补观察的首版 JSON 曾通过前一观察间接指向事件，当前实现及回归测试已改为直接保留原生 EventLog 引用，历史原始证据保留。

代码和设计文档进入分支，输出、凭据、运行时副本和 reports 继续在本地保留。若使用复现环境，需要叠加该环境既有的 Windows/API35 兼容改动，具体 Python 文件哈希在运行 manifest 中保存。

复现虚拟环境的依赖审计报告 gdown、Pillow、pytest、setuptools、soupsieve 五个存量包共 84 条公告，环境安全审计未通过。本次未增加依赖或升级复现环境。该结果单独保留，不能将代码测试通过表述为环境无已知漏洞。

验收场景包括低增长触发后自动选择、同页或 UTG 到达后发送、过期页面拒发、未知回调不重发，以及至少一个从真实设备证据得到的 Oracle 评估。

## 运行和证据

在复现运行时的 Windows/API35 兼容层上叠加本分支的 MAESTRO 模块和 policy、事件发送钩子。使用复现虚拟环境和模型配置，从环境变量读取凭据。DroidBot 原入口继续传入 APK、设备、输出路径、dfs_greedy 和 androlog。复现记录的 TimePlanner 配置为 3600 秒、间隔 3 秒、事件上限 100000、窗口 80。APK/设备信息与源码哈希记录在运行 manifest。

maestro-trace.jsonl 中先关联候选与 Planner 请求，再按 action_id 关联动作提案、冻结的 Oracle 规则、InputManager 回调、EventLog JSON 和页面快照。EventLog 文件必须可解析并与前后页面匹配。Oracle 使用确定性规则；文本匹配表示指定输入字段经观察达到预期值，点击变化表示观察到 UI 效果。功能级成功仍需对应目标的语义判定。
