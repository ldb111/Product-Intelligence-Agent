# Product Intelligence Agent 开发路线图

## 文档用途

本文档记录 Product Intelligence Agent（产品智能研究助手）的全局开发方向，用于快速了解项目当前阶段、已完成内容、后续阶段，以及各阶段需要解决的问题和核心产出。

具体近期任务、执行状态和验收记录见 [current-sprint.md](current-sprint.md)。

> **当前执行说明：** 2026-09-02 至 2026-09-06 因求职硬节点进入 Job-search Sprint（求职冲刺迭代），执行顺序以 [docs/current-sprint.md](./current-sprint.md) 为准；长期 Roadmap 仍保留，冲刺结束后继续按长期路线演进。

## 当前进度概览

| 阶段 | 名称 | 状态 |
| --- | --- | --- |
| 阶段 0 | 开发准备 | 基本完成 |
| 阶段 1 | Data Acquisition（数据采集） | 进行中 |
| 阶段 2 | AI Event Detection（AI 事件识别） | 未开始 |
| 阶段 3 | Competitor State（竞品状态） | 未开始 |
| 阶段 4 | Evidence（证据） | 未开始 |
| 阶段 5 | Multi-source Monitoring（多来源监控） | 未开始 |
| 阶段 6 | Ranking（排序） | 未开始 |
| 阶段 7 | Scheduled Monitoring（定时监控） | 未开始 |
| 阶段 8 | Deep Research Agent（深入研究智能体） | 未开始 |
| 阶段 9 | Backend API（后端接口） | 未开始 |
| 阶段 10 | PC Web（电脑网页端） | 未开始 |
| 阶段 11 | Online Demo（在线演示） | 未开始 |
| 阶段 12 | Project Proof（项目证明材料） | 未开始 |

## 阶段 0：开发准备

**状态：基本完成**

**目标：** 完成本地开发环境、Python、`.venv`、Git、PyCharm（Python 集成开发环境）和 Codex 基础协作方式准备。

**核心产出：**

- 可用的本地 Python 开发环境与虚拟环境
- 基础 Git 项目
- PyCharm 与 Codex 的基本开发协作流程

## 阶段 1：Data Acquisition（数据采集）

**状态：进行中**

**目标：** 建立真实竞品网页的数据读取与变化检测能力。

**主要能力：**

- Fixed Source（固定来源）读取
- Web Reading（网页读取）
- Content Normalization（内容标准化）
- Snapshot（页面快照）
- Content Hash（内容哈希摘要）
- Change Detection（变化检测）
- Diff（差异比较）

**核心产出：** 能够读取指定竞品网页，保存可比较的页面快照，并识别、展示前后内容变化的基础数据链路。

## 阶段 2：AI Event Detection（AI 事件识别）

**状态：未开始**

**目标：** 使用 LLM（大语言模型）判断网页变化是否具有真实的产品、商业或竞争意义。

**主要能力：**

- LLM API（大语言模型应用程序接口）
- Prompt（提示词）
- Structured Output（结构化输出）
- Candidate Event（候选事件）
- 第一轮 Evaluation（评测）
- Gold Set（金标集）
- Bad Case（失败案例）

**核心产出：** 从网页变化中生成结构化 Candidate Event（候选事件），并通过首轮 Evaluation（评测）、Gold Set（金标集）和 Bad Case（失败案例）验证识别质量。具体模型与接口待验证。

## 阶段 3：Competitor State（竞品状态）

**状态：未开始**

**目标：** 让系统能够区分 Current Fact（当前事实）和真实 State Transition（状态迁移）。

**主要能力：**

- 首次扫描建立 Baseline（基线）
- Latest Confirmed State（最新已确认状态）
- State History（状态历史）
- Previous State（历史状态）
- Current State（当前状态）
- State Transition（状态迁移）

**核心规则：**

- Current Fact（当前事实）不能直接视为 Event（事件）
- First Observed（首次观察到）不等于 Newly Launched（新上线）
- Not Observed（未观察到）不等于 Absent（不存在）
- 状态变化必须与最新有效状态比较，不能永远与第一次 Baseline（基线）比较

**核心产出：** 可追踪的竞品状态历史，以及基于最新有效状态判断的真实状态迁移记录。

## 阶段 4：Evidence（证据）

**状态：未开始**

**目标：** 建立事件证据和核验机制。

**主要能力：**

- Evidence（证据）
- Evidence Verification（证据核验）
- Evidence Chain（证据链）
- 已确认、待确认、信息冲突
- Evidence Conflict（证据冲突）

**核心产出：** 与事件关联的证据、核验状态和证据链，并能够明确记录证据冲突。

## 阶段 5：Multi-source Monitoring（多来源监控）

**状态：未开始**

**目标：** 从单一网页扩展到真实竞品的多个来源。

**主要能力：**

- 多 Source（来源）
- Content Deduplication（内容级去重）
- Event Deduplication（事件级去重）
- Public Search（公开搜索）补漏

**核心产出：** 面向同一竞品的多来源监控链路，以及内容级、事件级去重和公开搜索补漏能力。

## 阶段 6：Ranking（排序）

**状态：未开始**

**目标：** 根据用户自己的产品情况判断哪些竞争事件更值得关注。

**主要能力：**

- Product Profile（产品画像）
- Current Focus（当前关注重点）
- Competitor Relation（竞品关系）
- Relevance（相关性）
- Potential Impact（潜在影响）
- Attention Level（关注级别）
- Ranking Record（排序记录）
- Ranking Evaluation（排序评测）

**核心产出：** 结合产品画像和当前关注重点的竞争事件排序结果、排序记录与评测结果。具体排序方法待验证。

## 阶段 7：Scheduled Monitoring（定时监控）

**状态：未开始**

**目标：** 把手动运行升级为持续监控。

**主要能力：**

- Scheduled Scan（定时扫描）
- Source Health（来源健康）
- Failure Handling（失败处理）
- Retry（重试）
- Partial Failure（部分失败）
- 基础 Observability（可观测性）

**核心产出：** 可定时执行、可识别来源健康状态，并能处理重试和部分失败的持续监控流程。调度方式待技术选型。

## 阶段 8：Deep Research Agent（深入研究智能体）

**状态：未开始**

**目标：** 允许用户基于已经发现的 Competition Event（竞争事件）进行进一步研究。

**主要能力：**

- Search（搜索）
- Web Reading（网页读取）
- Tool Calling（工具调用）
- Agent Loop（智能体循环）
- Stop Condition（停止条件）
- Research Evaluation（研究评测）

**核心产出：** 能围绕指定竞争事件开展搜索、网页读取和工具调用，并根据停止条件输出研究结果的智能体流程及评测结果。具体工具和循环策略待验证。

## 阶段 9：Backend API（后端接口）

**状态：未开始**

**目标：** 把核心 Python 能力提供给前端调用。

**可能涉及：**

- FastAPI（Python Web 接口开发框架）
- Competitor API（竞品接口）
- Event API（事件接口）
- Scan API（扫描接口）
- Deep Research API（深入研究接口）

**核心产出：** 可供前端调用的后端接口。具体接口边界和技术方案需要根据前面阶段的实际开发结果进行技术选型，不在当前路线图中写死。

## 阶段 10：PC Web（电脑网页端）

**状态：未开始**

**目标：** 形成可以真实使用和展示的产品界面。

**主要能力：**

- 产品画像
- 竞品管理
- 来源管理
- 首页竞争信号
- Event Detail（事件详情）
- Ranking（排序）
- Deep Research（深入研究）入口

**核心产出：** 能够管理产品、竞品和来源，并查看竞争信号、事件详情、排序及深入研究入口的电脑网页端。

## 阶段 11：Online Demo（在线演示）

**状态：未开始**

**目标：** 让面试官或测试用户可以直接体验。

**主要内容：**

- 部署
- 最小登录
- 最小数据隔离
- API Key（接口密钥）安全
- 错误提示
- 使用额度控制

**核心产出：** 可供面试官或测试用户访问的在线演示环境，并具备最低限度的访问、数据和密钥安全保障。部署方案待技术选型。

## 阶段 12：Project Proof（项目证明材料）

**状态：未开始**

**目标：** 形成完整的求职 Proof of Work（工作证明）。

**主要产出：**

- GitHub（代码托管平台）项目
- README（项目说明）
- Technical Architecture（技术架构）
- Evaluation（评测）结果
- Gold Set（金标集）
- Bad Case（失败案例）
- 实验记录
- 技术决策记录
- 项目面试题库
- 面试展示材料

## Evaluation（评测）原则

Evaluation（评测）不是最后一个独立阶段。从阶段 2 第一条 LLM（大语言模型）链路开始，Evaluation（评测）应贯穿后续 AI 能力开发，用于持续验证能力质量、记录失败案例并指导改进。
