# PIA MVP Implementation Fact Index V2

> Freeze Commit: `f94195a1094d9a5d3bd712b56ed7ab4f479e0dd2`
>
> 审计日期: 2026-09-24
>
> 用途: 快速恢复当前实现事实。生产代码优先于 Tests、Schema / API / Persistence、Controlled E2E 与历史文档。

## 1. 当前主链路

1. 用户通过 Frontend / API 配置竞品名称与**必填官方 URL**。
2. `source_discovery` Agent 搜索、读取并确认正式监控来源；模型只能选择 Program 已登记候选。
3. 用户在首页手动扫描；当前**没有 Scheduler**。
4. Monitoring 建立 Snapshot 与 Contextual Diff；首次 Path A 只建 baseline，Path B 可做官方声明的有限历史回溯。
5. Event Core 输出 `event | no_event | uncertain`；正式结果先持久化，再推进 `processed_latest`。
6. confirmed Event 自动进入 competitor Knowledge 与 Triage；Triage 输出 `research | monitor | no_action`。
7. `research` 只是建议；用户决定是否启动 Initial Research。
8. Initial Research 完成后，用户可在同一 Event 下继续追问；系统可按需继续检索或查证，返回 Follow-up Answer 与来源。
9. Follow-up 用户原文若含长期关注信息，只生成 pending 候选；用户确认后才进入后续 Triage / Research。

## 2. V2 新增事实

### 关注与偏好

- 自身产品页分为：产品信息、产品资料、关注与偏好。
- “当前关注重点”是 Product Configuration；“关注与偏好”是 Memory 产品服务。
- 主动添加 → confirmed → 可被 Triage / Research 召回。
- 编辑使用稳定 key supersede 旧记录；移除和忽略使用 deactivate。
- 系统只分析 Follow-up 的**用户原文**；Assistant Answer、Event、Evidence、网页和 Knowledge 不进入抽取入口。
- 自动候选只能 pending；用户可确认或忽略。确认后新建 confirmed 并停用原 pending。
- 当前产品入口把关注与偏好保存为 workspace scope；API 的 `product_id` 不会进一步改变 Memory scope。

### Event-scoped Follow-up Research

- 只有 Initial Research `completed` 后可用；始终绑定单个 Competition Event，不是通用 Chatbot。
- 输入包括 Event、Initial Research Report、Existing Citations、Conversation History、Current User Question、Current Focus 与 Triage 路由；Own Product Knowledge、Competitor Knowledge、confirmed Memory、Web Search、Page Read 通过工具按需读取。
- 复用 `ResearchAgent`、`ResearchTools`、`EvidenceRegistry`；默认预算仍为 7 轮、10 次工具、2 次搜索、3 次读页。
- 输出 `FollowUpAnswer(answer, uncertainties, evidence_refs)`；Fact 必须使用已登记 `result_id` 和 exact excerpt，且不得生成 URL。
- Follow-up 不重跑或改写 Initial Research Report。

## 3. Conversation Persistence

- Store: `pipeline.sqlite3`，表仍为 `records(namespace,key,body)`；没有独立对话数据库。
- namespace: `research_follow_up:` + `identity(workspace_id, product_id, event_id)`。
- Turn 保存 `user_message`、`running | completed | failed`、attempts、candidate_count 和 artifact 引用。
- Answer / citations 保存在同 namespace 的 `:artifacts` 子空间。
- 用户问题先通过 `BEGIN IMMEDIATE` 原子持久化；`request_id` 派生稳定 turn_id，重复请求幂等返回已有 Turn。
- 完成时 Artifact 与 Turn completed 同事务提交；失败保留用户问题，可按 turn_id 重试。
- 刷新可恢复对话；不同 workspace / product / event 隔离。
- Initial lifecycle (`not_started/running/completed/failed` + 内部 interrupted 恢复语义) 与 Follow-up Turn lifecycle 分开。

## 4. Evidence 责任

- Initial 与 Follow-up 共用 Evidence Registry 和 Citation Validation。
- Fact support 必须指向已登记来源，`excerpt` 必须逐字存在于 content。
- 搜索摘要、Memory、pending 来源不能成为产品 Fact Evidence。
- Program 生成/登记 ID、URL 与来源关系；LLM 负责语义归纳，不能生成可信来源。
- Frontend 只展示自然语言答案与轻量来源索引，不展示内部 ID。

## 5. Frontend

- 一级导航仍为：首页、竞品配置、自身产品。
- 自身产品页新增“关注与偏好”：关注点、偏好、限制条件、目标、决策、待验证判断、待确认问题；已确认 / 待确认；添加、编辑、移除、确认、忽略。
- Research 详情页新增“研究结果 / 继续追问”二级视图；继续追问不是一级导航。
- 提供固定建议问题、输入框、Enter 发送（Shift+Enter 换行）、running、failed/retry、来源折叠和刷新持久化。

## 6. API Map 增量

| 能力 | Method | Path |
|---|---|---|
| Follow-up Conversation | GET / POST | `/api/workspaces/{workspace_id}/events/{event_id}/research/follow-ups` |
| Follow-up Retry | POST | `/api/workspaces/{workspace_id}/events/{event_id}/research/follow-ups/{turn_id}/retry` |
| Preferences List / Add | GET / POST | `/api/workspaces/{workspace_id}/own-product/preferences` |
| Preference Edit / Remove | PUT / DELETE | `/api/workspaces/{workspace_id}/own-product/preferences/{item_id}` |
| Preference Confirm | POST | `/api/workspaces/{workspace_id}/own-product/preferences/{item_id}/confirm` |
| Preference Ignore | POST | `/api/workspaces/{workspace_id}/own-product/preferences/{item_id}/ignore` |

## 7. Persistence Map

| Store | 当前责任 |
|---|---|
| `pipeline.sqlite3` | Config、Snapshot、watermark、Event、Pending、Triage、Initial Research、Follow-up Turn / Artifact |
| `<workspace-hash>.knowledge.sqlite3` | documents、chunks、vectors、vector_space、Own Product document registry |
| `<workspace-hash>.memory.sqlite3` | confirmed/pending/superseded Memory、active 状态与向量 |

## 8. 能力状态

- **IMPLEMENTED:** Monitoring、Manual Scan、Source Discovery、State Comparison、Explicit Change Statement、Historical Backfill、Triage、Initial Deep Research、Follow-up Research、RAG、Memory / 关注与偏好、Own Product、File Parsing。
- **PARTIALLY IMPLEMENTED:** Production SSRF Hardening、Research Rerun（failed 可重试，completed 初始研究不能主动重跑）。
- **TEST-ONLY:** Test Reset。
- **NOT IMPLEMENTED:** Scheduler、OCR、Notifications、RBAC、Multi-user、Distributed Jobs。
- **DEFERRED:** 正式 Deployment。

## 9. 测试与 Evaluation

- Freeze acceptance: Backend `578/578 PASS`; Frontend `30/30 PASS`; TypeScript typecheck PASS; Production Build PASS。
- `tests/` 有 33 个 tracked 文件；代码静态统计有 578 个 `test_` 方法。`experiments/` 有 25 个 tracked 文件。
- Gold Set: NOT IMPLEMENTED。
- Event Detection Evaluation: NOT IMPLEMENTED。
- Prompt Evaluation: PARTIAL。
- Research Quality Evaluation: NOT IMPLEMENTED。
- Memory Extraction Evaluation: NOT IMPLEMENTED。
- Follow-up Quality Evaluation: NOT IMPLEMENTED。
- 软件回归与受控 E2E 证明链路和边界可运行，不等于 AI 质量评分。

## 10. Interview-critical Facts

- Memory 已产品化为关注与偏好；系统识别内容不会自动成为 confirmed。
- Follow-up 只从用户原文提取 Memory 候选。
- Follow-up Research 锚定单 Event，可继续调用既有研究工具并维持 Fact Evidence Grounding。
- Conversation 持久化、request id 幂等、失败重试和 Event isolation 均由 Program 负责。
- Initial Report 不被 Follow-up 修改。
- 当前不能证明 AI 准确率、Follow-up 质量、无限轮对话、多用户协作、生产 SLA 或企业级安全。

详细说明见 `docs/PIA_MVP_Current_Implementation_Report.docx`。
