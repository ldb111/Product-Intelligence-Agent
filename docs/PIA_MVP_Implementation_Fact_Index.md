# PIA MVP 当前实现事实索引

> 本文用于核查实现；招聘阅读建议从[产品决策](product_decisions.md)和[AI（人工智能）架构](ai_architecture.md)开始。

<details>
<summary>历史记录阅读术语（代码字段与原始输出保留原样）</summary>

PIA（产品智能研究助手）；MVP（最小可行产品）；AI（人工智能）；Prompt（提示词）；Gold（金标准答案）；Agent（智能体）；RAG（检索增强生成）；Memory（长期记忆）；Knowledge（产品知识）；Triage（事件分诊）；Research（研究）；Follow-up（事件内继续追问）；Initial Research（初始研究）；Initial Report（初始报告）；Event Core（事件核心）；Event（竞争事件）；Evidence（证据）；Pending（待确认候选）；Verification（核验）；Pipeline（产品工作流）；Snapshot（页面快照）；Artifact（成果资产）；Store（存储）；Embedding（向量化）；Rerank（重排序）；Path A（前后状态比较路径）；Path B（官方明确声明路径）；Dedup（去重）；E2E（端到端验收）；Baseline（基线）；Commit（代码提交）；API（程序接口）；SQLite（本地数据库）；PC Web（电脑网页端）；Reasoning（推理模型角色）。

</details>

审计日期：2026-09-29。本文记录 Final Closure 实现；冻结 Commit 与评测数值见评测历史文档及 Git 历史。旧 V2 对应 f94195a 的历史事实已由本文更新。

## 1 产品流程

用户配置自身产品与竞品 → 来源发现 → 手动扫描 → Snapshot/局部比较或官方声明 → Event Core → Pending Verification 或正式事件 → 竞品知识入库与 Triage → 用户启动 Initial Research → 同事件 Follow-up。

首次普通扫描只建立基线。有限官方历史回溯显示为历史事件，不混入本次新发现。分诊建议深入研究不自动调用研究 Agent。研究页同时保留初始成果和追问，页内导航仅改变滚动位置。

## 2 模块与职责

| 目录 | 已实现职责 | 主要入口 |
|---|---|---|
| backend | 静态/浏览器采集、质量门槛、Snapshot、结构化局部状态与差异 | snapshot.py、structured_content.py |
| source_discovery | 搜索、读页、候选登记和正式来源选择 | agent.py、mcp_server.py |
| event_core | 双路径检测、证据校验、事件语义去重 | detector.py、evidence.py、dedup.py |
| pipeline | 工作流、状态恢复、声明生命周期、产品配置、资料服务 | orchestrator.py、store.py、statements.py |
| file_parsing | 五种格式解析，保留页/表/节来源 | parser.py |
| knowledge | 双域知识、分块、Embedding、检索与 Rerank | store.py、retrieval.py |
| memory | 用户长期信息、候选确认、范围过滤与召回 | product.py、store.py |
| triage | 路由与关注理由，双域比较证据 | workflow.py |
| research | 核验、初始研究、事件追问，共用工具与证据登记 | agent.py、tools.py、evidence.py |
| api | 面向产品的入口与安全错误投影 | main.py、application.py |
| frontend | 首页、竞品配置、自身产品、研究会话 | App.tsx、ResearchAnswer.tsx |
| evals | 冻结数据、无泄漏适配、评分与单次执行 | event_metrics.py、runners/ |

## 3 历史状态与可靠终点

observed_latest 表示最新抓取，processed_latest 表示正式结果已经可靠保存。Event/dedup outcome 先保存；uncertain 先保存 Pending；no_event 以 comparison outcome 保存，然后推进 processed watermark。下游 Triage/Research 失败不会回退发现阶段。新 run 可恢复未完成比较或下游动作。

Path A 只用真正可比较的前后同项原文；模型不许由新文字补造过去不存在。Path B 使用官方明确变化声明，程序控制首次回溯范围、后续新声明、重试与去重。

## 4 研究与独立成果

Initial Report 保存在 `pipeline.sqlite3` 的 `research:<runtime_workspace>` namespace，研究状态保存 Artifact 引用。再次进入从持久化成果恢复，不依赖前端或聊天历史，不重跑已完成研究。

Follow-up 的 namespace 为 `research_follow_up:` 加 workspace/product/event 的身份哈希；用户问题先原子保存，回答与 completed 状态同事务提交，失败保留回合。request_id 保证幂等；重试沿用 turn_id。追问只读 Initial Artifact，既有四回合重开测试证明不会覆盖它。

前端同时渲染成果与对话。回答支持自然段、短列表、编号、简单表格；不强制固定章节或 Fact 卡片。证据编号附在被支持正文末尾，按需展开原文；内部 kind/support 仍完整保留在后端。刷新时运行中任务通过只读查询恢复，不重复启动。

## 5 知识与资料

Own Product 显式 Add/Replace，与监控分离。logical document_id 稳定，content_hash 只标识内容。相同内容跨路径幂等；整份替换原子提交并移除旧单元；失败保留旧版；并发新增去重，并发替换拒绝陈旧写入。

每个 workspace 独立 Knowledge SQLite，产品以 scope ID 过滤，own_product 与 competitor 领域分开。仅当前版本进入检索。五种格式为 DOCX、文本型 PDF、XLSX、MD、TXT；扫描 PDF 无 OCR。

## 6 当前关注重点与长期记忆

当前关注重点是产品配置，不需语义抽取。长期关注与偏好支持主动添加、编辑、移除、确认或忽略。主动添加立即 confirmed；Follow-up 仅从用户原文抽取 pending 候选，用户确认后才参与后续判断。模型回答、网页与 Event 不进入 Memory 写入入口。

Memory 是用户意图，不能证明产品事实。当前产品界面的偏好为 workspace scope，product_id 不进一步细分该偏好范围；不能宣称跨团队多租户权限能力。

## 7 产品接口与运行

| 路径 | 用途 |
|---|---|
| /api/health | 服务健康 |
| /api/workspaces/{workspace}/overview | 产品事件总览 |
| /competitors | 竞品配置与来源发现 |
| /scan | 手动扫描 |
| /events/{event}/research | 获取或启动独立研究成果 |
| /events/{event}/research/follow-ups | 获取会话或新增追问 |
| /events/{event}/research/follow-ups/{turn}/retry | 重试失败回合 |
| /own-product | 产品名称与当前重点 |
| /own-product/documents | 五格式新增与替换 |
| /own-product/preferences | 关注偏好生命周期 |

表内除 health、overview 外均承接 `/api/workspaces/{workspace}`。同步模型任务使用线程池，状态查询不会被模型等待阻塞；同 workspace 扫描有单进程并发门闩。SQLite 每次操作独立连接，原子操作使用事务。不是跨进程任务队列。

## 8 证据责任

Program 登记真实来源与 ID、验证逐字 excerpt、两侧状态、引用字段与作用域。模型负责事实含义、竞争意义、推断与建议。未读搜索摘要、Memory 和待确认材料不能成为产品 Fact。自身比较必须同时引用自身与竞品事实；无资料时明确不足，不能推断不支持。

来源可信度与证明能力分开；同源 RAG/页面/Event 不当作独立交叉验证。自然语言由 Prompt 约束，前端仅映射固定状态，不机械删改模型正文。确切日期只取证据，不用 observed_at 补 event_time。

## 9 实现与评测状态

Implemented：Monitoring、来源发现、双路径事件、去重/核验、RAG、Memory、五格式资料、Triage、Initial/Follow-up、持久会话、中文 PC Web。

Evaluated：软件回归与受控故障注入；真实五格式入库、历史主链路和当前真实浏览器追问；Event Detection 另有冻结 Gold、历史独立 Baseline、开发回归和一次性新留出流程。质量结论以 `evals/docs/event_detection_evaluation_lineage.md` 为准。

未作独立质量评测：Research、Follow-up、Memory Extraction。历史 E2E 曾参与 Prompt 开发，不能当无污染质量数据。当前 Memory E2E 仅证明链路和边界；正式评测应使用新 Gold，覆盖 Write Precision、Recall、False Memory Rate、Supersession Accuracy、Scope Leakage。

## 10 已知限制与可选增强

本地单进程、手动扫描、小规模 SQLite 检索，无鉴权/角色权限、分布式执行、通知、OCR。公网部署须补充出口/DNS 安全和身份控制。模型引用验证不等于语义完全正确，长对话会增加上下文成本，外部网页及模型可失败。

内部 PRD、路线图、价格与能力资料可能经 Embedding/Rerank/Reasoning 外发。企业使用需要数据分级、安全策略、供应商合规及协议评估，可选择脱敏、租户隔离或私有模型；当前没有实现这些企业能力。

未来可选：更多独立 Gold、研究与记忆质量评测、调度与通知、企业安全部署。它们不改变已完成本地 MVP 的能力声明。

## 11 历史文档处置

`docs/full_implementation_drift_audit_v1.md` 为既有未跟踪历史审计，保留原样供追溯，不进入成品提交。其主要发现已逐项复核：自动深研、Path B 缺失、核验回流、watermark、研究身份/恢复、自身资料入口已由正式 Repair A/B/C 及后续提交解决；生产级出口安全保留为部署边界。README 和旧 V2 的实现/评测漂移在本轮修正。
