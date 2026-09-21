# Final Integration V0.1 Implementation Report

## 1. Repository Inspection

Repair C 基于已提交的 Repair A/B HEAD `f4ccd0b1e59b942b9b323b6ba25c1faa5ea367de`
继续。实际检查了工作区、现有 Path B 接口、Structured Content、Source credibility 和 SQLite
持久化能力；现有契约足以在 Integration 层接入，不需要修改 Frozen Event Core。

保留现有实现，仅在 Integration 层补齐配置失败检查点、官方域身份传递及重定向后的可信度边界。原有 `tmp/` 保持原样。Source Discovery、Stage 1、Event Core、Knowledge、Memory、Intelligence 及 `llm/` 的既有文件未修改。

## 2. Integration Architecture

采用显式 Workflow：`ProductPipeline` 管流转与检查点，`ModuleAdapters` 调用冻结公共入口，`PipelineStore` 保存结构化状态，`output.py` 投影用户结果。没有新增模型、Prompt、业务推理规则、Agent 框架或依赖。

```text
ProductInput → 配置 → Source Discovery / 已确认来源
  → Stage 1 → Snapshot
      → Path A：首次 Baseline / 后续 Contextual Diff
      → Path B：首次 Limited Historical Backfill / 后续新增 Explicit Statement
  → Event Core → confirmed: Event Dedup → Triage → recommendation / monitor / no_action
                               → 用户显式 start_research → Deep Research
               → uncertain: Verification → 正式检测 / 结束 / Pending
  → ProductResult + 独立 Audit
```

## 3. Files Changed

Final Integration 及 Repair A/B/C 的当前相关交付文件：

| 文件 | 职责 |
| --- | --- |
| `pipeline/__init__.py` | 产品契约与状态仓库导出 |
| `pipeline/schemas.py` | 统一输入、产品结果、受控错误类别 |
| `pipeline/store.py` | SQLite 状态及原子快照历史 |
| `pipeline/adapters.py` | 冻结模块的薄适配 |
| `pipeline/orchestrator.py` | 显式流程、路由、恢复、任务级去重及用户触发 Research 生命周期 |
| `pipeline/output.py` | 产品级 Event 标识、用户字段投影、引用命名空间、内部信息隔离 |
| `pipeline/statements.py` | Structured Statement 候选、可靠日期识别及 Backfill 有界筛选 |
| `pipeline/documents.py` | 独立 Own Product Document Add/Replace、Registry 与产品结果投影 |
| `tests/test_pipeline.py` | 68 项集成控制测试 |
| `experiments/pia_e2e.py` | Baseline + 真实事件 recommendation / 显式 Research / 幂等复用验收入口 |
| `experiments/final_integration_repair_a_e2e.py` | watermark、Event persistence 与 Verification 控制验收 |
| `experiments/final_integration_repair_b_e2e.py` | 用户显式 Research 生命周期控制验收 |
| `experiments/final_integration_repair_c_e2e.py` | Historical Backfill、后续 Statement 与去重控制验收 |
| `experiments/own_product_document_integration_e2e.py` | 文件配置、原子 Replace、重复与重开控制验收 |
| `docs/final_integration_v0.1.md` | 本实现报告 |

`experiments/output/` 中的真实运行 JSON、审计和数据库是本地验收产物，受既有忽略规则保护，不属于交付文件。

## 4. Product State

`ProductInput` 支持 workspace_id、competitor_name、可选 official_url / additional_urls / product_profile / current_focus / own_product_documents。无需填写 event_id、candidate_id、chunk_id 或 memory_id。

工作区保存配置及三个 store 引用；来源保存已确认 URL 与官方域资格；快照保存 observed/processed 引用；事件、Pending、分诊与研究生命周期分别保存。产品使用由 confirmed Event 业务事实生成的稳定 `evt_...` 标识；内部 candidate、artifact 和 SQLite 键不进入产品结果。

最小调用入口：

```python
import asyncio
from pipeline.schemas import ProductInput
from pipeline.orchestrator import ProductPipeline

pipeline = ProductPipeline("experiments/output/demo.state")
product = ProductInput(
    workspace_id="demo",
    competitor_name="待监控竞品",
    official_url="https://example.com",
    current_focus="关注公开产品能力变化",
)
output = asyncio.run(pipeline.run(product))
user_result = output["product"]
developer_audit = output["audit"]
```

示例域名只是输入格式说明；实际运行应提供真实竞品。再次调用 `run(product)` 创建下一次扫描；失败时使用保存的内部 run_id 调用 `resume(run_id)`。该 ID 由调用程序管理，无需产品使用者填写。

## 5. Main Workflow

`run()` 是 Monitoring 入口。它只消费已经持久化的 Own Product Knowledge，不读取或解析上传文件；首次无来源时调用真实 `run_source_discovery()` 并保存结果，此后复用已确认来源，再逐来源扫描、比较及处理候选。自身文件通过独立 `OwnProductDocumentService` 的 Add/Replace Workspace Configuration 生命周期入库。显式可信调用方也可用 `confirm_monitoring_sources()` 确认来源。

`run_events()` 是已有正式 Event/Evidence 的回放入口，要求标注 provenance；阶段中明确说明没有重新发现或采集。本轮真实 E2E 使用此入口，不冒充从零重新采集产生的新事件。

Triage 的 `research` 只创建 `not_started` recommendation，主 Pipeline 正常完成且不会调用 Research Agent。用户另行调用 `start_research(product, event_id)` 后，程序从 SQLite 恢复同一 confirmed Event 与对应 Triage，才执行现有 `deep_research`。客户端不能指定模型、Prompt、工具轨迹或内部 candidate / artifact 键。

## 6. Baseline / Subsequent Scan

扫描通过冻结 Quality Gate 后调用 `create_snapshot()`。Snapshot 是不可回滚的真实观察历史：当前观察和 `observed_latest` 会立即保存；`processed_latest` 只表示最近已经完成事件判断、可安全用于下一次 State Comparison 的 Snapshot。每一组 previous/current 引用及处理状态保存在 `comparisons:<workspace>`，不需要修改冻结的 Stage 1 Snapshot Schema。

首次普通扫描仍只建立 Baseline，不调用 Event Detection，并原子推进 processed baseline。后续复用 `build_state_comparison_input()` 和 `match_interactive_states()`，页面及变化的交互状态分别进入正式 Event Core；unchanged 由 Event Core 的确定性边界生成 `no_event`，不会调用 LLM。只有 Event Core 完整返回 `event`、`no_event` 或 `uncertain` 后才推进 processed watermark；`uncertain` 会先保存 Pending Candidate。

Event Detection 构造、调用或解析失败时，当前 Snapshot 仍保留在 observed history，comparison 保持 pending，processed baseline 保持 Previous。恢复同一 run 会复用原 Snapshot 和历史对；创建新 run 时仍以 processed baseline 比较当前页面，因此页面保持不变也会重新处理原来的 A → B。成功后 B 成为新 baseline，下一次 C 正确比较 B → C。

Path B 与 Path A 独立运行。首次扫描仍保持 Path A 模型调用为 0，但会从成功读取的官方
Monitoring Source 的 Structured Content 中形成有限局部 statement 候选，再调用现有
`build_explicit_statement_input()` 和 Event Core。程序不按动作关键词认定 Event；
“支持 SSO”这类 Current Fact 仍须由 Event Core 判为 no_event，不能因为首次看到而成为事件。

首次 Path B 只执行 Limited Historical Backfill：statement 自身正文或相邻结构上下文必须
包含可验证的完整日期，先筛选采集参考日之前最近 90 天，再按声明日期从新到旧取全竞品
最多 20 条。`captured_at/observed_at` 只表示系统观察时间和窗口参考点，绝不补作 event_time。
无可靠日期的首次声明只随页面保存为 Baseline，不进入 Historical Backfill。

后续扫描只处理相对上一 Snapshot 新出现、或先前失败仍为 pending 的 statement；新声明即使
没有可靠 event_time 仍可进入正常 Path B，由 Event Core 保持 event_time 为 null。Path A
unchanged 不会阻止 Path B 处理新的 Explicit Statement。

## 7. Event Routing

| 输入/结果 | 后续行为 |
| --- | --- |
| confirmed event | 正式去重 → 原始证据入库 → Triage |
| research | 保存 recommendation 和 `not_started` 状态；不自动调用 Deep Research |
| monitor / no_action | 保存分诊，不调用 Deep Research |
| 用户显式 start_research | 校验 confirmed Event + research Triage 后执行单 Event Deep Research |
| uncertain | Research Agent verification |
| still_pending | 保留 Pending |
| verification event / no_event | 都只代表核验证据方向；用新增官方引用原文和观察时间构造正式输入，重新调用 Event Core |
| Event Core event | Candidate 标记 `resolved_event`，正式 Event 进入后续链路 |
| Event Core no_event | Candidate 标记 `resolved_no_event`，不生成 Event |
| Event Core uncertain / 正式输入不足 | Candidate 保持 Pending，不伪造否定结论 |

Verification Agent / Research Agent 的自然语言 decision 不能直接改写正式 Event Truth。event 与 no_event 都必须携新官方原文进入 Event Core；只有 Event Core 的正式结果能解决 Candidate。证据不足、无法构造 DetectionInput 或正式结果仍 uncertain 时保留 Pending，且不会循环核验直到成功。事件事实已确认但语义去重给出同一性 UNCERTAIN 时单独保留待处理，不把它伪装成事实不确定候选。

## 8. Knowledge / Memory Integration

Structured State、Knowledge、Memory 分开。两知识域复用冻结 KnowledgeStore 的 domain / product / competitor 过滤，Memory 使用独立数据库及 workspace scope。

自身文档只走 `parse_and_prepare()` + 文件级 KnowledgeIngestor；确认事件只走 `event_documents()` + KnowledgeIngestor。Triage 和 Research 使用已有双域检索与 MemoryRetriever，不重写 RAG。普通 current_focus 仅是当前输入；只有另行明确调用 `remember_explicit()` 才通过 MemoryWriter 写入。文件 Add/Replace 与研究结果都不会自动进入 Memory。

自身资料采用 current-version only。Add 生成稳定产品文档 ID；Replace 以该 ID 为锚点，在完成全部 Embedding 后通过单一 SQLite 事务删除旧 unit set、写入新 set 并更新当前 Registry。相同 bytes 跨路径复用已有文档；同名不同内容不会自动覆盖。Monitoring 不调用该服务，因此重复扫描不会重复解析或向量化自身文件。

没有提供自身文档时记录上下文缺口；不能由未检索到资料推断某项能力不存在。

## 9. Persistence

简单 SQLite records 表保存命名空间、键与 JSON；每次操作提交并关闭连接。结构化运行、步骤、事件、产物引用可重新打开读取。

Snapshot 持久化区分 `observed_latest:<workspace>`、`processed_latest:<workspace>` 和 `comparisons:<workspace>`。Event Detection 失败后，observed Snapshot、未完成 comparison 和旧 processed baseline 都可在进程重启后恢复；Pending verification 继续保存在 `pending:<workspace>`。旧数据库无需修改 records 表：首次读取旧 `latest` 时结合既有 comparison/detection 检查点确定安全 baseline；旧格式 history 在恢复完成时补齐 comparison 元数据，不删除或回滚真实 Snapshot。

成功步骤跳过，失败步骤可恢复；已完成运行直接返回保存结果。同一个 run 恢复不会重新扫描或重复创建 Snapshot，新 run 能恢复尚未处理的变化。Research 使用 `research_status:<workspace>` 独立持久化 `not_started/running/completed/failed`；completed 重复启动直接复用结果，failed 只有显式 retry 才重试。进程重启后遇到旧 process token 的 running 会标记为 `research_interrupted`，再由用户 retry。Own Product Document Registry 与对应 unit set 位于同一个 Knowledge SQLite，Replace 时一起提交或回滚；不需要 Retrieval current-version filter。

Path B 的观察和处理使用两个独立命名空间。`statement_observations:<workspace>` 保存每次成功
扫描看见的全部 statement identity，包括未入选的第 21–25 条、无日期及超过 90 天的首次声明；
`statement_processing:<workspace>` 只属于真正进入 Event Core 的声明。两者都以来源 URL、规范
statement 原文和相邻结构上下文的稳定指纹作为 key，并与 Path A comparison watermark、
Event candidate ID 和 Event Dedup 分离。

后续 normal_monitoring 只接受 observation state 中此前从未 seen 的 identity，不能把“没有
processing record”等同于新声明。每条入选 statement 在检测前保存 pending；event 必须先完成
Event/Dedup outcome 持久化，uncertain 必须先保存 Pending Candidate，之后才标记 processed。
因此一个声明可以同时是 baseline seen 和 processing pending；seen 不会阻止失败重试。单条失败
保持 pending，同 run 或 new run 可重试；同批其他成功 statement 不回滚，也不会重复检测。

这是本地单进程任务级保护；不提供跨数据库事务或供应商请求的 exactly-once。外部请求完成而本地检查点尚未保存时崩溃，恢复可能再次请求。

## 10. Error Handling

区分 configuration_error、source_discovery_error、scan_error、event_detection_error、retrieval_error、triage_error、research_error。配置和检索异常在相关步骤识别；失败保存阶段、异常类别和尝试次数，产品结果只给受控错误说明。

已完成快照、事件、分诊保留，失败后不生成后续成功结果。多来源部分扫描失败时返回 partial。用户触发的 Deep Research 失败只把独立 Research action 标为 failed，主 Pipeline、confirmed Event 和 Triage 继续保持 complete；底层异常正文不进入产品输出。

## 11. Product Output vs Audit

ProductResult 包含 competitor、monitoring_sources、scan_status、baseline_created、带产品级
`event_id` 的 detected_events、单独的 historical_events、带独立 `pending_id` 的
pending_candidates、triage_results、research_actions、completed research_results、
verification_results、citations、current_focus、uncertainties、status / error。

`historical_events` 的每项明确带 `discovery_origin=historical_backfill`，不进入正常
`detected_events`，因此未来 Dashboard 可以把首次配置时回溯发现的近期历史事件与本次新发现
事件分开展示。正常后续 Path B Event 标记为 `normal_monitoring`。产品输出不暴露 statement
hash、processing key、checkpoint 或内部 candidate_id。

用户 JSON 排除 candidate_id、pipeline checkpoint、内部 artifact key、Provider、模型运行配置、SQLite 地址、源文件引用和原始工具轨迹；明显凭据及本机路径再做保守遮蔽。产品级 citation/result reference 保留，并使用产品 Event 或稳定展示分组限定命名空间，避免合并后串引用。

审计单独保存真实步骤、模型运行数据和工具轨迹；不直接用作用户 Demo 数据。

## 12. Targeted Tests

`python -X utf8 -m unittest tests.test_pipeline -q`：**68/68 PASS**，26.735 秒。覆盖 Repair A/B 全部语义，
并加入 Repair C 的首次 Baseline/Backfill 分离、90 天与 20 条顺序、可靠日期、官方来源、
historical/normal 产品语义、statement/Event 两层去重、后续无日期声明、partial failure、
same-run/new-run 恢复和 Store reopen。

Repair B 新增覆盖 research recommendation 不自动执行、not_started/running/completed/failed 持久化、running/completed 幂等、interrupted recovery、failed retry、monitor/no_action 拒绝启动、Research failure 不污染主 Pipeline，以及产品 Event/Pending 标识、citation 与 Developer Audit 分离。Repair A 的 watermark、Event persistence 故障边界、下游恢复和 Verification Event Core re-entry 全部继续通过。

控制测试使用脚本替身隔离真实模型副作用，并复用正式差异与事件构造接口；只证明控制流和边界，不代表模型语义质量评测。首次运行一项新增测试因测试数据未以 JSON 模式序列化日期失败，修复的是新增测试数据，未改冻结测试或业务规则。

## 13. Real E2E

本地验收产物：`experiments/output/pia_v01_live_01.json`（用户数据）、同名 `.audit.json`（技术审计）及 `.state/`（状态库）。

Case A 使用已有 FixtureHandler 提供受控 HTTP 页面，实际经过 Stage 1 采集、质量门槛和快照，Baseline 已通过：无 Event、无 Triage、无 Research。该来源由调用方明确确认，未伪造 Source Discovery 模型记录。

Case B 复用 `event_core_v01_live_02.json` 中已验收的公开官方 Case B：OctoReport 智报免费档由一次性积分改为每月补充 2,000 积分。自身文档 `knowledge_own_product.md` 是明确模拟 Fixture；Memory 是显式模拟用户关注免费套餐周期积分的操作。本轮真实调用现有 Embedding、Rerank、Reasoning、Research 角色，没有重跑历史 E2E，也没有强制 research 路由。

首次执行在 Memory 准备时因沙箱网络失败；Baseline 检查点保留。联网审批最初拒绝外发风险，经核实模拟/公开数据及既有服务目的地后获准恢复。首个主链尝试的分诊返回 LLMStructuredOutputError，保留失败记录；仅恢复失败步骤，未调整 Prompt 或 Schema。

最终 **Case A / Case B 均 PASS**，同一主链 run_id 为 `08da86fe-a6bd-4732-b390-1cc37b2a7fc7`。分诊首次失败后一次恢复成功，实际选择 research；研究第一次结构化输出也失败，单独恢复一次后成功。成功步骤没有重跑，审计保留两类失败记录，没有更改冻结 Prompt、Schema 或校验。

| 验收项 | 真实结果 |
| --- | --- |
| Baseline | 1 次扫描，0 Event，0 Triage，0 Research |
| Triage 上下文 | Own Product 3 条、Competitor 1 条、confirmed Memory 1 条 |
| 实际路由 | research（模型选择，未强制） |
| Deep Research | 完整报告，包含双域支持的模拟产品比较、引用及不确定性 |
| 最终产品数据 | status PASS；业务 product.status complete；6 个引用；未解析引用 0 |
| 最终持久化 | Knowledge 2 文档 / 5 chunks；Memory 1 条；未自动写入研究结果 |

研究阶段实际执行 7 次工具操作：两域检索及 Memory 检索各 1 次、搜索 2 次、读页 2 次。其中官方更新日志读取成功，另一读页返回 Stage1AdapterError；冻结 Research Agent 按已有规则关闭工具阶段并用已验证证据完成最终报告，未伪造失败页面内容。该失败使覆盖范围有限。模型在自然语言 uncertainties 中将其概括为“搜索因工具错误停止”；开发审计能明确区分实际失败的是读页。

人工检查产品 JSON 时发现 Integration 的路径正则会误遮蔽 HTTPS 地址；已最小修复并新增回归测试。Baseline 只从已保存原始状态重新投影显示字段，未重新采集；最终公开 URL 误遮蔽数为 0。

Repair B 没有重新消耗真实 Research 调用。既有真实 Agent E2E 继续证明 ResearchAgent 能完成真实工具链；更新后的 `pia_e2e.py` 把集成验收拆为 Phase A recommendation/not_started、Phase B 用户显式启动/completed、Phase C completed 结果复用。另以无网络控制 E2E 实际验证三阶段均 PASS。

Repair C 新增无网络控制 E2E：Case A 验证首次 Path A 仅建立 Baseline、近期官方 dated
statement 经 Path B 产生 historical_backfill；Case B 验证后续 Path A unchanged 时新增
Explicit Statement 仍产生 normal_monitoring Event；Case C 验证重复 statement 不再次调用
Event Core，也不产生重复 Event。本轮不要求、也未执行真实联网 Backfill。

Own Product Document Controlled E2E 另行验证独立 Workspace Configuration 生命周期：DOCX
Add 后可经 Retrieval/Rerank 查询；3 units Replace 为 2 units 后旧 Section 完全删除；相同 bytes
跨路径复用已有文档且不重复 Embedding；Store reopen 后 ID、状态与 Retrieval 继续有效。

Own Product Document 的并发审计另以 4 项受控测试验证 Registry 原子边界：相同 bytes 并发 Add
只持久化一个逻辑文档；不同内容不会误合并；并发 A→B / A→C 中后提交的 stale request 被拒绝且
不留下部分 Knowledge，基于最新 B 的明确 retry 可完成 B→C；同目标 A→B 收敛为幂等结果。

首次运行命令：`python -X utf8 experiments/pia_e2e.py --output experiments/output/pia_v01_live_01.json`。已存在状态目录应走恢复入口；创建新的演示需要自行选择新的 output 路径，不清理旧验收数据。

恢复入口：准备阶段用 `--resume-setup` 复用已通过 Baseline；主链开始后用 `--resume-run <已保存的 run_id>`。不通过新建状态目录反复重跑成功步骤。

## 14. Runtime Metrics

以下计量来自最终保留的成功步骤与产物；重试前失败调用的完整用量未由冻结客户端返回，不计为 0，也不混称完整账单。

| 角色 | 已记录调用 | reported tokens |
| --- | ---: | ---: |
| Embedding | 8 | 501 |
| Rerank | 4 | 1,054 |
| Reasoning | 1 | 3,765 |
| Research | 5 | 28,025 |
| 主链成功记录合计 | 18 | 33,345 |

显式 Memory 准备另有 1 次 Embedding / 23 tokens / 0.7169 秒；以上加准备为 **19 次 / 33,368 reported tokens**。这些已保存记录缺失 usage 数为 0；分诊、研究失败尝试消耗的模型用量仍未知，不包含在此合计中。

主链成功检查点耗时之和 83.998 秒；Baseline 检查点之和 0.015 秒；最后一次仅恢复 Research 的命令耗时 68.811 秒，Research Agent 内部耗时 68.579 秒。以上范围不同，不能相加后声称全程连续执行时间。

现有实际配置：Reasoning / Research 分别使用独立角色入口，模型均为 `deepseek-flash`；Embedding 为 `text-embedding-v4`，Rerank 为 `qwen3-rerank`。这些信息只在技术报告/审计中，不进入用户产品结果的运行配置字段。

## 15. Full Regression

`python -X utf8 -m unittest discover -s tests -q`：**535/535 PASS**，82.399 秒。原模块、Repair A/B/C 及 16 项 Own Product Document Integration 测试全部通过；其中 4 项新增受控并发测试。日志中的无效 PDF、存储/响应故障和工具失败警告来自受控负向测试，最终结果为 OK。

## 16. Code Quality

核心操作按配置、扫描、候选处理、产物保存、输出投影分开，中文说明关键边界；没有 500 行 run、Manager、协调框架或多 Agent。既有公共函数的名称、返回结构及输入要求已按真实代码核对。

`git diff --check` PASS；两个新增文件的独立 trailing-whitespace 检查命中 0。Git 的
LF/CRLF 提示不是空白错误。本轮 9 个 Repair C 文件的高置信度 Secret 扫描命中 **0**；
Frozen 模块 tracked 修改 **0**，`.env` 和 `experiments/output/` 修改 **0**。扫描只报告数量。

## 17. Known Limitations

- 本轮两例是链路验收：Case A 受控真实采集，Case B 既有公开真实事件回放；没有重新完成一次 Source Discovery → 后续扫描 → 新事件的全程联网验收。相应接线由控制测试和原冻结模块测试覆盖。
- 真实模型可能返回无效结构或引用；保留失败而不修改结果，也不自动无限重试。已用于开发的 Fixture 不构成独立、无污染的质量 Evaluation；本轮不创建 Gold Set。
- 正式报告包含模型推论，引用存在不等于推论已被证明。不同产品“积分”的单位价值和消耗规则尚未归一化，不能仅凭 2,000 与 500 推定真实获客或留存优势；模拟自身资料也不能代表用户实际产品。
- 跨运行产物复用键涵盖事件、产品配置与自身文件内容，但未包含整库知识及 Memory 版本。相同输入下更新历史库后不会自动重新分诊；当前不提供知识刷新、调度或自动重评能力。
- Research V1 只支持首次显式启动和 failed retry；completed 后不能重新研究，也不保存 Research Run 1/2/3 历史。monitor/no_action 不支持人工 override。
- running 使用本地进程 token 区分同进程重复调用与重启中断；这是单进程幂等边界，不是分布式租约或跨主机任务锁。
- 仅限可信本地调用方与单进程运行；scope 过滤不等于用户鉴权，没有并发任务锁、商业多租户隔离或分布式事务。数据库自身不可写时无法保证失败记录也能落盘。
- 沿用正式来源身份；重定向到未确认官方域不会继承官方资格。冻结采集器的 DNS、浏览器子资源和网络行为未更改，本实现不构成完整 SSRF 防护。
- Own Product Knowledge 可能包含内部 PRD、Roadmap、Pricing 等；检索和推理会把必要原文交给配置的外部模型。生产应按数据分级、企业安全策略及供应商合规要求评估外发，考虑敏感字段脱敏、企业协议、租户隔离或私有模型。本轮只沿用既有边界，没有实现这些能力。
- 部分步骤失败后没有完整模型用量可回收；持久化指标只代表可观察调用，不能当作账单总额。已确认来源会被复用，没有本轮范围外的自动来源刷新。
- 旧版本若只保留 `latest` 而对应运行检查点也已被外部删除，系统无法法证式判断该历史 Snapshot 是否曾处理完成，只能沿用旧指针；正常 PipelineStore 数据会保留检查点并自动选择安全 baseline。本轮不回滚或重建历史 Snapshot。
- E2E 依赖本地已验收的事件归档和既有配置；新克隆需提供真实归档，不能凭空生成替代。后续仍需用户实际运行、验收和解释核心代码。
- Historical Backfill 只覆盖当前 Source Reader 和 Structured Content 实际访问、抽取到的最近
  内容。分页、无限滚动、历史归档、登录后页面或当前读取边界之外的动态加载内容不会被自动
  穷举，因此不能宣称最近 90 天历史完整，也不能宣称全量 Changelog 或全网事件召回。
- V0.1 的可靠日期识别限于 Structured Content 正文或相邻 Heading/Card/Table 上下文中的完整
  日期；未进入 Snapshot 的 HTML metadata 不会被猜测补齐。每来源最多形成 60 个有界结构候选，
  Initial Backfill 再执行全竞品最近 90 天、最多 20 条限制。

## 18. Git Status

Repair C 未 Commit，HEAD 保持 Repair A/B 提交
`f4ccd0b1e59b942b9b323b6ba25c1faa5ea367de`。暂存区为空，审阅以工作区最新内容为准。

`.env` 未修改，既有 `tmp/` 未删除或清理；验收输出留在已忽略的 `experiments/output/`。没有进入 UI、Evaluation、Token 优化或下一阶段开发。

```text
 M docs/final_integration_v0.1.md
 M pipeline/adapters.py
 M pipeline/orchestrator.py
 M pipeline/output.py
 M pipeline/schemas.py
 M pipeline/store.py
 M tests/test_pipeline.py
?? docs/full_implementation_drift_audit_v1.md
?? experiments/final_integration_repair_c_e2e.py
?? pipeline/statements.py
?? tmp/
```

Final Integration V0.1 = READY FOR REVIEW

该状态表示实现、控制测试与规定的最小真实链路可供审阅，不替代用户本人运行和正式验收；遵照本轮要求未 Commit，未宣布项目正式 Done。
