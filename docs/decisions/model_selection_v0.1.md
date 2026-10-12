# 模型选型 V0.1｜模型选型记录

> 历史记录：本文保留原阶段计划、验收数字、代码字段与输出，不代表当前待办。当前产品能力见[实现事实索引](../PIA_MVP_Implementation_Fact_Index.md)，面试阅读入口见[产品决策](../product_decisions.md)。

<details>
<summary>历史记录阅读术语（代码字段与原始输出保留原样）</summary>

PIA（产品智能研究助手）；Prompt（提示词）；Gold（金标准答案）；Evaluation（评测）；Agent（智能体）；Schema（数据结构）；Source Discovery（来源发现）；Baseline（基线）；State（状态）；Tool（工具）；Tool Calling（工具调用）；Structured Output（结构化输出）。

</details>

## 1. 背景

Product Intelligence Agent（产品智能研究助手）当前进入 Source Discovery Agent（来源发现智能体）V0.1 开发阶段。

该 Agent 需要通过 Tool Calling（工具调用）自主调用：

- `web_search（网页搜索）`
- `read_page（网页读取）`

并根据中间结果决定：

- 下一步搜索什么
- 哪些页面值得进一步读取
- 当前来源覆盖是否足够
- 是否继续搜索
- 最终输出哪些建议监控来源

因此，模型选型不能只参考通用 Benchmark（基准测试）或模型排行榜，而需要结合 PIA 的真实 Agent 任务进行评测。

---

## 2. 当前阶段目标

当前阶段不是确定 PIA 的最终模型，而是选择一个 Baseline Model（基线模型），用于：

1. 完成 Source Discovery Agent V0.1 开发；
2. 跑通完整 Agent Loop（智能体循环）；
3. 建立 Gold Set（金标集）和 Evaluation（评测）体系；
4. 为后续不同模型的任务级对比提供基准。

最终模型需要在真实 Agent Evaluation（智能体评测）完成后确定。

---

## 3. 模型硬性要求

候选模型至少需要满足以下条件。

### 3.1 国内可用性

PIA 默认以中国国内市场与可用条件为基准。

模型服务应能够在国内环境中稳定调用，不依赖特殊网络条件。

### 3.2 Tool Calling（工具调用）

模型需要能够稳定：

- 选择正确工具
- 生成合法工具参数
- 根据工具返回结果继续决策
- 支持连续多轮工具调用

这是 Source Discovery Agent 的核心能力。

### 3.3 Structured Output（结构化输出）

模型需要能够按照固定 Schema（数据结构约束）返回结构化结果，供后续程序稳定消费。

### 3.4 智能体多轮决策能力

模型需要能够根据：

- 已执行的搜索
- 已发现的候选网址
- 已读取页面
- 已确认来源
- 当前覆盖缺口

决定下一步行动，而不是只完成单轮问答。

### 3.5 接口与框架接入

需要能够较稳定地接入当前技术栈：

- LangChain（大语言模型应用开发框架）
- Tool Calling（工具调用）
- MCP（Model Context Protocol，模型上下文协议）

### 3.6 成本与延迟

Source Discovery Agent 可能在单个任务内产生多轮模型调用，因此：

- 单次 Token（词元）价格不能过高
- 延迟需要满足交互和批量任务需求
- 模型成本需要能够支撑后续真实评测和持续使用

---

## 4. 当前候选方向

当前优先考虑国内可稳定使用、具备 Agent 能力的模型平台与模型。

第一阶段暂不追求覆盖大量模型。

候选模型应满足：

> 能力有代表性 + 成本差异明显 + 具备真实比较价值。

初步计划至少比较：

- 一个低成本模型
- 一个能力更强的同系列模型
- 如有必要，再加入一个其他供应商模型作为横向对照

具体候选列表将在 Source Discovery Agent 跑通后，根据当时实际可用模型和价格确定。

---

## 5. 当前 Baseline Model（基线模型）

### 模型

`qwen3.7-flash`

### 平台

阿里云百炼。

### 当前选择原因

当前将其作为 Baseline Model，而不是最终模型，主要考虑：

1. 国内可直接使用；
2. 支持 Tool Calling（工具调用）；
3. 支持 Structured Output（结构化输出）；
4. 能满足当前 Source Discovery Agent 的上下文需求；
5. 接入 LangChain 相对成熟；
6. 模型调用成本较低，适合开发阶段大量调试；
7. 后续可以与同系列更强模型进行相对公平的模型对比。

---

## 6. 当前结论边界

当前结论仅为：

> `qwen3.7-flash` 作为 Source Discovery Agent V0.1 的 Baseline Model（基线模型）。

当前不能得出：

> `qwen3.7-flash` 是 PIA 的最佳模型。

原因是目前：

- Source Discovery Agent 尚未完成；
- Gold Set 尚未建立；
- Agent Evaluation 尚未执行；
- 尚未对不同模型进行同条件比较。

因此最终模型选型必须等待真实任务级 Evaluation。

---

## 7. 后续模型评测原则

Source Discovery Agent 跑通后，模型比较必须尽可能保持以下变量一致：

- 同一 Gold Set（金标集）
- 同一 System Prompt（系统提示词）
- 同一 User Input（用户输入）
- 同一 Tool Schema（工具结构）
- 同一 MCP Tool（MCP 工具）
- 同一 Agent State（智能体状态）
- 同一 Hard Stop（硬停止规则）
- 同一输出 Schema

原则：

> 尽量只改变 Model（模型）本身。

避免同时修改 Prompt、工具或业务规则后再比较模型，导致无法判断差异来源。

---

## 8. Evaluation Metrics（评测指标）

后续模型评测至少关注以下指标。

### 8.1 Task Success Rate（任务成功率）

Agent 是否成功完成一次来源发现任务。

### 8.2 Source Precision（来源准确率）

最终推荐来源中，真正值得长期监控的来源占比。

### 8.3 Source Coverage（来源覆盖度）

重要官方来源是否被发现。

重点关注漏掉关键来源的情况。

### 8.4 Tool Call Validity（工具调用有效性）

检查：

- 是否调用正确工具
- 参数是否合法
- 是否存在明显无意义调用

### 8.5 Premature Stop Rate（过早停止率）

Agent 是否在仍存在明显来源覆盖缺口时提前停止。

### 8.6 Repeated / Invalid Tool Calls（重复 / 无效工具调用）

检查是否存在：

- 重复搜索相同 Query（查询词）
- 重复读取同一 URL
- 明显没有价值的搜索
- 工具调用死循环

### 8.7 Structured Output Success Rate（结构化输出成功率）

最终输出是否稳定符合 Schema，能够由程序直接解析。

### 8.8 Latency（延迟）

记录单个来源发现任务的总体耗时。

### 8.9 Token Usage（词元使用量）

记录输入、输出 Token 消耗。

### 8.10 Cost（成本）

换算单个完整任务的真实模型调用成本。

---

## 9. 模型选择原则

最终模型不以单一最高得分决定。

基本原则：

> 先满足质量门槛，再在达到门槛的模型中综合考虑成本、延迟和稳定性。

例如：

如果低成本模型和高成本模型在核心任务成功率、来源准确率和覆盖度上差距很小，则优先低成本模型。

如果低成本模型在关键指标上存在明显问题，例如：

- 经常提前停止
- 漏掉关键来源
- 工具调用明显不稳定

则考虑升级至更强模型。

---

## 10. Bad Case（失败案例）分析

模型评测过程中需要记录真实 Bad Case。

失败不能简单归因于“模型能力不足”，需要进一步判断属于：

- Model Problem（模型问题）
- Prompt Problem（提示词问题）
- Tool Problem（工具问题）
- State Design Problem（状态设计问题）
- Product Rule Problem（产品规则问题）
- External Data Problem（外部数据问题）

只有确认问题主要来自模型本身，才能作为模型选型依据。

---

## 11. 评测留痕

每轮正式模型评测至少记录：

- `evaluation_date（评测日期）`
- `provider（模型供应商）`
- `model_id（模型标识）`
- `prompt_version（提示词版本）`
- `tool_schema_version（工具结构版本）`
- `case_id（金标案例编号）`
- Tool Call Trace（工具调用轨迹）
- Final Output（最终输出）
- Latency（延迟）
- Token Usage（词元消耗）
- Cost（成本）
- Evaluation Result（评测结果）

如果供应商提供具体模型版本，也应同时记录。

---

## 12. 后续计划

当前阶段：

> 使用 `qwen3.7-flash` 完成 Source Discovery Agent V0.1。

Agent 最小链路跑通后：

1. 建立 Source Discovery Gold Set；
2. 定义自动评测与人工评测指标；
3. 固定 Prompt / Tool / State / Stop Condition；
4. 使用 Baseline Model 跑第一轮评测；
5. 选择至少一个更强模型进行对照；
6. 分析 Bad Case；
7. 比较质量、延迟与成本；
8. 输出最终 Model Selection Decision（模型选型决策）。

在上述步骤完成前，不将当前 Baseline Model 描述为最终模型。
