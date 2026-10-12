# PIA MVP 最终收尾报告

> 验收基线：本文记录2026-09-29的最终收尾，数字、模型结果与机器结论保留原样。招聘阅读摘要见[评测方法](evaluation.md)与[产品决策](product_decisions.md)。

<details>
<summary>历史记录阅读术语（代码字段与原始输出保留原样）</summary>

PIA（产品智能研究助手）；MVP（最小可行产品）；AI（人工智能）；Prompt（提示词）；Gold（金标准答案）；Evaluation（评测）；RAG（检索增强生成）；Memory（长期记忆）；Knowledge（产品知识）；Triage（事件分诊）；Research（研究）；Follow-up（事件内继续追问）；Initial Research（初始研究）；Event（竞争事件）；Evidence（证据）；Citation（引用）；Schema（数据结构）；Pending（待确认候选）；Verification（核验）；Pipeline（产品工作流）；Artifact（成果资产）；Conversation（对话历史）；Store（存储）；Path A（前后状态比较路径）；Path B（官方明确声明路径）；Dedup（去重）；E2E（端到端验收）；Regression（回归测试）；Commit（代码提交）；API（程序接口）；SQLite（本地数据库）；PC Web（电脑网页端）。

</details>

验收日期：2026-09-29。范围：本地单用户、PC Web 的作品级 MVP；不表示企业公网生产就绪。

## 1. 最终状态

PIA MVP Final Closure = PASS。核心产品与证据、状态恢复、研究会话链路已收口，正式实现
在最终留出之前冻结。最终评测后没有调 Prompt、改 Gold、重跑错例或修改生产实现。
尚存模型分类与字段语义误差，以下如实列出；软件通过不代表模型全部判断正确。

## 2. 最终产品

用户可以配置自身产品与竞品、管理资料和长期关注、手动监控、查看可追溯事件与分诊，
主动启动研究，并围绕同一事件继续追问。初始报告与对话分别持久保存，可刷新恢复。

## 3. 最终产品流程

自身资料/当前关注 → 竞品与来源发现 → 手动扫描 → 快照及局部状态 → Path A/B 检测 →
证据验证与去重 → 必要的 Pending Verification → 双域知识与 Memory → Triage →
用户启动 Initial Research → 同页 Follow-up → 查看引用与恢复历史。

普通首次扫描只建立基线；有限官方声明回溯是历史事件，不冒充刚发生的变化。

## 4. 最终架构

React/TypeScript 前端与 FastAPI 产品 API；Pipeline、SQLite 与各领域 Store 管理状态。
来源、事件、知识、记忆、分诊、研究各有明确职责。模型做语义判断与有限工具计划；程序
管比较资格、身份、引用、范围、持久化、幂等和执行预算。详见 `final_architecture.md`。

observed_latest 与 processed_latest 分离。正式 Event/dedup、Pending 或 no_event outcome
先可靠保存，才推进 comparison watermark；下游失败恢复自己的检查点。Initial Artifact
独立于 Conversation，Follow-up 不覆盖它。没有新增分布式引擎或多智能体框架。

## 5. 审计发现

| 优先级 | 发现与处置 |
|---|---|
| P0 | 复核旧漂移审计的状态/事件丢失、Path B、核验回流、研究身份边界；既有 Repair A/B/C 已修复，受控回归通过，无待处理 P0 |
| P1 | 同步模型阻塞 API 查询、刷新运行状态不继续查询、报告/追问互斥、固定答案卡片、独立引用段、局部变化隐藏及观察日期误称发生日期；已修复并验证 |
| P1 | 旧35条开发后仍混称独立成绩、日期计分无证据资格、失败诊断不足、冻结顺序缺少约束；已修复并保留历史 |
| P2/边界 | 模型主类型分歧、字段角色混淆、短摘录下语义过推、无企业安全/调度/OCR；明确记录，不伪装已解决 |

旧 `full_implementation_drift_audit_v1.md` 保留未跟踪，未覆盖原文；其关键发现已在事实索引
逐项说明处置，当前文档替代其作为现状依据。

## 6. 主要产品变化

运行中查询不再被同步模型调用阻塞，同 workspace 重复扫描有门禁。部分扫描失败仍展示
已经保存的事件。事件卡片同时保留前后比较项和明确声明项，未知发生日期明确显示观察日期。
初始研究与追问整合为一个持续会话体验，保留现有后端成果存储。

## 7. 前端与体验变化

同页上方报告、下方追问，页内导航只滚动、不切换互斥 Tab。回答按实际内容展示段落、列表、
编号、简单表格和引用，不预设卡片数量或展示每条内部 kind。无机械字符串删改模型正文。

Citation 放入所支持 Claim 的末尾正文节点，普通段落、列表项、表格单元格和引用块均有测试；
多个编号连续，点击展开真实来源与逐字摘录。没有仅用 CSS 掩盖独立段落。Evidence Registry、
引用契约和事实/推断/建议语义保留。运行时刷新只查询状态，不重复发起任务。

## 8. 首次基线根因

历史35条首次执行31成功、4证据验证错误。模型把当前版本新增说明当作过去缺少该能力的
证明，漏写必需引用，并对维护更新、类型与时间有语义误判。部分 Gold 使用 Path B 的变化
声明语义，却冻结为严格 Path A，形成真实标注/路径冲突。未改旧标签或路径以提高分数。

## 9. 评测执行工具修复

分开 Execution、Validated Decision、End-to-End Decision 与 Raw Diagnostic。失败仍进入
端到端分母。保留完整原始候选集和安全验证诊断；旧失败仅有单个候选时不假装完整覆盖。
时间先检查是否在实际正文有证据，再做确定性日历标准化。

新运行器验证 RC→空候选→Gold 的祖先顺序、候选输入不变、RC 实现无差异和全量泄漏检查。
Gold Commit 解析为规范哈希，并以独占 receipt 阻止同批再次正式运行。不把模型错误转成
伪造成功，也不把自动化测试当作质量评测。

## 10. 证据校验错误根因

| 旧 Case | 真实原因 | 修复与结果 |
|---|---|---|
| EDF-A-002 | 两项变化没有 previous_state | 明确同项双侧证据要求，开发复测为合法 uncertain |
| EDF-A-007 | 部分新增能力没有旧状态 | 不能用旧版本号代替能力旧状态，开发复测合法 uncertain |
| EDF-A-013 | 首先缺 event_type 引用，另缺旧状态 | 要求每个非空事实引用，证据不足安全 uncertain |
| EDF-A-017 | 多项变化没有旧状态，混有维护/弃用 | 区分业务动作与维护，保留严格路径，开发复测合法 uncertain |

Validator 原拒绝正确；没有降低双侧状态、逐字引文或引用字段要求。

## 11. 事件检测变化

- Prompt：增加通用路径资格、维护边界、类型区分、日期角色及引用完整性说明；无 Case ID、
  特定 URL、产品名或单样本关键词分支。
- Program：错误保留完整 raw decisions 与结构化安全诊断，正常业务流程不变。
- Validator：增加诊断字段，不改变接受/拒绝标准。
- Schema：生产事件 Schema 未改；仅评测 Schema 如实支持专家模型标注生命周期。

## 12. 开发回归

旧35条当前用途明确为 development_regression，**不是新的 Independent Score**。先运行
8条诊断子集一次，再运行完整35条一次；后续未重跑。

| 指标 | 开发回归 |
|---|---:|
| SUCCESS / ERROR / BLOCKED | 34 / 1 / 0 |
| Validated Decision Accuracy | 24/34（70.59%） |
| End-to-End Decision Correct Rate | 24/35（68.57%） |
| Macro F1 | 0.6795 |
| Event Precision / Recall | 1.0000 / 0.5294 |
| Path A / Path B Accuracy | 9/18 / 15/16 |
| Type Strict / Conditional | 8/17 / 8/9 |
| EvidenceValidationError | 0 |
| 有资格的日期匹配 | 0/1 |

ERROR 是 EDF-B-002 的结构化输出错误，未重跑。Path A 没有输出 event，包含旧材料路径
资格冲突与模型保守性，不能全部解释为质量改善。EDS-005 误取品牌年份，保留为字段风险。

## 13. 发布候选

RC：`1516eae1a51fe66a4b327477612789383e5601fd`。
冻结前完成软件验收、受控失败边界与真实浏览器检查。两条额外合成开发压力输入（明确同项
调价、普通维护）真实执行2/2通过，证明严格 Path A 没有被整体禁用；不计独立成绩。
冻结之后才开始新来源采集。

## 14. 完整产品回归

| 验证 | 结果 |
|---|---|
| 最终 Backend Full Regression | 670/670 PASS |
| Frontend Tests | 47/47 PASS |
| TypeScript typecheck | PASS |
| Frontend production build | PASS |
| Repair A/B/C Controlled E2E | 全部 PASS |
| Own Product Documents Controlled E2E | PASS |
| 既有真实主链路、Intelligence、五格式RAG、Memory、Knowledge E2E | 历史成功结果核对保留，未重复模型运行 |
| 当前真实 Follow-up | 新增一次调用、运行中刷新、完成后恢复及来源核对 PASS |
| Whitespace / Secret / 禁止路径 | PASS / 0 / 0 |

RC 前全量665，RC 后新增5条数据边界测试，最终全量670。新增测试不改变生产实现。

## 15. 演示走查

真实前端5173、后端8000；核对事件、分诊、初始研究、既有两轮追问，并新增一次三点式追问。
运行中刷新后回答完成，报告与三轮历史独立恢复。同页导航、自然段、连续多个行内编号、
来源展开及逐字摘录均在真实浏览器确认。自身产品资料空态、确认/待确认偏好、竞品来源页
均正常。用户现有数据库未重置、未提交。

最后一次可选重复导航被自动审批服务以额度不足拒绝，未执行、未绕过；此前必需验收与
截图已完成，临时视口也已恢复。该工具限制不作为产品失败。

## 16. 最终新留出集

Candidate Freeze：`ffd58c6e924f7178ee8307273822df0df424e6be`。
Gold Freeze：`d1172a5eabbf64088fff3e05c8960d10c53b314c`。

12条，Path A 4 / Path B 8；event 7 / no_event 4 / uncertain 1。来源以国内产品为主，
补充 Vite/Redis 的明确历史状态。5类 event：商业化2、弃用2、能力1、公测可用1、价格1。
其他类型未覆盖，不能据此得出该类型质量结论。

`annotation_method=independent_expert_model_review`；无真人复核，无生产预测辅助标注。
没有 consumed holdout、替换难例或标签回改。公开材料可能存在于模型预训练中，本任务只能
证明其未参与本项目开发与本次预测前的调整，不能宣称训练数据层面绝对无污染。

## 17. 最终独立评测

DeepSeek / deepseek-flash，运行 `20260929T060649661220Z`，12条各一次。Leakage Guard PASS。

| 指标 | 结果 |
|---|---:|
| Execution Success | 12/12 |
| ERROR / BLOCKED / EvidenceValidationError | 0 / 0 / 0 |
| Validated Decision Accuracy | 12/12 |
| End-to-End Decision Correct Rate | 12/12 |
| Decision Macro F1 | 1.0000 |
| Event Precision / Recall / F1 | 1 / 1 / 1 |
| Path A / Path B Accuracy | 4/4 / 8/8 |
| Type Strict / Conditional Accuracy | 4/7 / 4/7（57.14%） |
| 有资格事件日期匹配 | 1/1 |
| 日期留空一致 | 10/11 |
| Unsupported event_time | 1 |
| Raw Diagnostic 一致 / 覆盖 | 12/12 / 12/12 |

混淆矩阵对角为7、4、1，其余0。端到端分数是决策级，**不是字段整体准确率100%**。
所有原始预测、引用、Token/延时与错例队列留在本地动态结果中。

## 18. 最终失败案例

三个主类型分歧：Vite停止旧运行时支持（availability/deprecation）、飞书公测
（capability/availability）、NocoBase免费化（pricing/commercialization）。按冻结Gold
计错，不事后重标。千问把生效时间兼作发生时间；Redis归纳多出未在短摘录出现的“BSD式”；
腾讯生命周期漏掉两个后续计划子项；ECharts待确认解释仍偶尔出现内部上下文字段名。

这些是语义、字段覆盖与展示误差；引用来源可追溯，但归纳内容仍可能超出证据。本次不增加字符串黑名单、
不放宽验证、不针对最终案例修 Prompt。来源范围的自动复核标记已人工式专家读审：
NocoBase插件名在原文中，不能因Gold范围为空就断言幻觉。

## 19. 最终交付物

- `README.md`：当前能力与真实启动方式。
- `docs/final_architecture.md`：产品与AI架构、设计理由。
- `docs/PIA_MVP_Implementation_Fact_Index.md`：源码、API、状态与限制。
- `docs/PIA_MVP_Current_Implementation_Report.docx`：五页可讲解报告，逐页视觉核验。
- `docs/demo_guide.md`：演示步骤与本轮实际记录。
- `evals/docs/event_detection_evaluation_lineage.md`：不可变历史、开发及最终指标。
- `evals/docs/event_detection_final_holdout_v1.md` 和对应 JSONL：冻结规则、来源、Gold。
- 新增评测/运行/持久化/前端引用测试与开发压力实验。

## 20. Git 提交

1. `c1d799811c7a37445bcb3e1d93692c57de346319` — 事件证据推理边界与分层冻结评测。
2. `6996248f171e1ec75c02f06d9a59e540d72e824f` — 研究同页追问、行内引用、运行体验。
3. `1516eae1a51fe66a4b327477612789383e5601fd` — RC 与实现/演示基线文档。
4. `ffd58c6e924f7178ee8307273822df0df424e6be` — 空候选与规则冻结。
5. `d1172a5eabbf64088fff3e05c8960d10c53b314c` — 独立专家 Gold 冻结。
6. 本报告所在的最终文档提交 — `docs: 完成MVP最终验收与独立留出结果记录`。

## 21. 已知限制

本地单用户、单进程、手动触发，无鉴权/RBAC、定时调度、OCR、通知或生产SLA。公网部署
需额外身份与出口/DNS安全。长对话增加模型上下文成本。模型与外部网页仍可能失败。
严格逐字引用不等于自动证明全部语义；类型和日期角色仍有误差。新12条样本非随机、规模小、
厂商与类型分布有限，Research/Memory没有独立质量评测。内部产品资料使用外部模型服务
需要企业数据分级与供应商策略审查，当前没有实现这些企业能力。

## 22. 未来可选增强

更广泛的独立Gold与专家复核、研究/记忆质量评测、企业身份和部署安全、定时与通知可以
作为未来产品阶段。本地作品MVP不以这些扩展为完成前提，也没有遗留必须继续开发的阶段任务。

## 23. 最终 Git（版本管理）状态

正式成果均进入本轮提交；tracked工作区干净、暂存区为空。保留既有未跟踪历史审计：

```text
?? docs/full_implementation_drift_audit_v1.md
```

`tmp/` 已加入忽略规则，原文件与动态结果均保留，未提交。`.env`、运行数据库、截图、
experiments/output、node_modules、dist 均不在本轮提交中。

```text
PIA MVP Final Closure = PASS
Core Product Flow Complete = YES
Monitoring Complete = YES
Event Detection Complete = YES
Evidence Validation Stable = YES
Verification Complete = YES
Triage Complete = YES
Initial Research Complete = YES
Follow-up Research Complete = YES
Knowledge / RAG Complete = YES
Memory Complete = YES
Frontend Finalized = YES
Frontend Demo Walkthrough = PASS
Backend Full Regression = PASS
Frontend Tests = PASS
TypeScript Typecheck = PASS
Production Build = PASS
Event Detection Baseline Preserved = YES
35-case Set Reclassified as Development Regression = YES
Development Regression Completed = YES
EvidenceValidationError Root Causes Resolved = YES
Release Candidate Frozen Before Final Holdout = YES
Case-specific Hacks = NO
Gold Leakage = PASS
Final Candidate Frozen Before Annotation = YES
Final Gold Frozen Before Production Evaluation = YES
Final Holdout Annotation Method Honest = YES
Final Evaluation Executed Once Per Holdout = YES
Same Final Holdout Retuned Against = NO
Documentation Matches Production = YES
Git State Explainable = YES
Production Ready for Portfolio Demo = YES
Ready for Project Explanation = YES
Ready for Interview Preparation = YES
```
