# 长期记忆 V0.1 实现报告

> 历史记录：本文记录对应阶段的开发与验收状态，保留原阶段计划、验收数字、代码字段与输出，不代表当前产品状态、待办或 Git（版本管理）状态。当前产品能力见[实现事实索引](PIA_MVP_Implementation_Fact_Index.md)，设计取舍见[产品决策](product_decisions.md)。

<details>
<summary>历史记录阅读术语（代码字段与原始输出保留原样）</summary>

LLM（大语言模型）；Prompt（提示词）；Gold（金标准答案）；Evaluation（评测）；Agent（智能体）；RAG（检索增强生成）；Memory（长期记忆）；Knowledge（产品知识）；Triage（事件分诊）；Research（研究）；Event Core（事件核心）；Event（竞争事件）；Evidence（证据）；Schema（数据结构）；Pending（待确认候选）；Snapshot（页面快照）；Store（存储）；Embedding（向量化）；Rerank（重排序）；Source Discovery（来源发现）；Stage 1（采集与状态比较阶段）；E2E（端到端验收）；Fixture（测试材料）；Commit（代码提交）；HEAD（当前提交）；API（程序接口）；SQLite（本地数据库）；State（状态）；Structured Output（结构化输出）；Fast（快速模型角色）；Reasoning（推理模型角色）；Done（完成）。

</details>

状态：**READY FOR REVIEW**。实现、自动化测试与最小真实 E2E 已通过；不是正式 Done 或冻结声明。仍需项目本人实际运行、验收并能够解释核心代码。本轮不 Commit，不进入 Triage / Research。

## 1. 仓库检查

开始前已读取 AGENTS.md、公共 llm Chat/Embedding 能力、Knowledge Layer、Event Core、Source Discovery、模型选型与模型可见面 decision docs、现有测试和 requirements。

- 当前 HEAD：`76c856016031c5675bc9dbaeb22cc36630336506`，与任务指定的 Knowledge Layer 冻结提交一致。
- Stage 1、Source Discovery、Event Core、Knowledge Layer 及公共 llm 文件与 HEAD 一致，没有 tracked 修改。
- 开发前实际发现 **321** 项测试，与任务基线一致。
- Fast Role 实际配置：`aliyun_bailian / qwen3.7-flash`；Embedding Role：`aliyun_bailian / text-embedding-v4`。
- Python 3.13 与现有 SQLite/requests/pydantic 能力足够；没有新增依赖或大型服务。
- 开始前仅有既存未跟踪 `tmp/`，本轮未修改。

## 2. 架构

```text
有限用户/研究文本
    → Secret / size pre-check
    → Fast LLM 提出逐字 Memory Candidate
    → Program Write Policy
    → pending（不得默认召回）

明确“记住”请求
    → Program Write Policy
    → Existing exact/key check
    → Existing Embedding Role
    → SQLite transaction
    → confirmed active / supersede old history

query + explicit scope + scope_id
    → SQL: active + confirmed + scope filter
    → Existing Embedding Role
    → exact cosine Top-K
    → context_type=memory
```

Memory 是固定 Workflow，不是 Agent。LLM 无工具、无数据库访问权、不能决定 scope、身份、status、supersession 或绕过策略。Memory 使用独立数据库，不与 Knowledge 向量表混合，也不调用 Rerank。

## 3. 记忆、知识与状态的区别

| 层 | 保存内容 | 本轮行为 |
| --- | --- | --- |
| Structured State | Source、Snapshot、Event、Evidence、竞品配置 | 只允许 Memory 保存稳定 ID 引用，不复制对象 |
| Knowledge / RAG | PRD、Pricing、产品能力、已核验事实 | `knowledge/pricing/web_page` 来源明确拒绝进入 Memory |
| Memory | 长期关注、偏好、约束、目标、决策、假设、开放问题 | 独立 schema、store 与 retrieval |

“产品支持文档级权限”是 Knowledge；“后续研究优先关注文档权限”才是 Memory。调用方必须提供来源类型，程序不会让模型猜数据层。Memory 可用 `event_id/document_id/competitor_id/research_id` 引用事实身份，不复制完整 Event、Evidence 或 Knowledge。

## 4. 文件变更

本轮只新增以下 11 个交付文件：

| 文件 | 职责 |
| --- | --- |
| `memory/__init__.py` | 公共入口 |
| `memory/schemas.py` | scope、类型、状态、候选、记录、结果契约 |
| `memory/prompts.py` | 独立 Candidate Extraction 指令 |
| `memory/policy.py` | Secret、来源、长期价值和事实边界 |
| `memory/extraction.py` | Fast Role 调用与候选后验证 |
| `memory/writing.py` | explicit / extracted 两条写入路径 |
| `memory/store.py` | 独立 SQLite、事务、历史、删除/失效 |
| `memory/retrieval.py` | scope 强过滤与语义 Top-K |
| `tests/test_memory.py` | 31 项控制测试 |
| `experiments/memory_e2e.py` | 最小真实 E2E |
| `docs/long_term_memory_v0.1.md` | 本报告 |

## 5. 记忆数据结构

`MemoryRecord` 包含任务要求的 `memory_id`、`scope`、`scope_id`、`memory_type`、可选稳定 `memory_key`、`content`、`status`、`source_reference`、`source_text`、`created_at`、`updated_at`、`is_active`、可选 `supersedes_memory_id` 和 `metadata`。

额外字段：

- `source_kind`：程序识别 direct_user、research_context 与事实型来源边界；
- `references`：可选 event/document/competitor/research ID，不复制事实正文；
- Retrieval Result：固定 `context_type="memory"`、余弦分数和 rank。

scope 只允许 `user / workspace / research`；类型只允许 `focus / preference / constraint / goal / decision / hypothesis / open_question`；status 只允许 `pending / confirmed / superseded`。没有 numeric confidence。时间必须带时区，`memory_key` 只允许稳定的小写标识字符。

## 6. 写入策略

程序策略依次检查：

1. 来源文本非空且不超过 4,000 字符；
2. source reference 与 source text 不包含常见 Secret / API Key / Token / Password / Bearer / JWT 形态；
3. Candidate content 是 source_text 中连续存在的逐字片段；
4. 来源不是 Event、Evidence、Snapshot、Knowledge、Pricing 或原始网页；
5. 来源直接包含相应长期语义线索；
6. 不是一次性运行、导出、安装、测试等临时操作；
7. 不是缺少关注/偏好语义的明显产品事实。

Secret 检查发生在 Fast LLM 调用之前，因此命中内容既不写库，也不进入外部模型。策略有意保守：无法验证时拒绝或 pending，不自动补字段、不把事实改写为偏好。

## 7. 显式记忆

`MemoryWriter.remember_explicit(...)` 只接收调用方明确标记的记忆请求。通过策略后可写为 confirmed。调用方必须显式给出 scope 和 scope_id；模型不参与身份判断。

完全相同的 active Memory 返回 unchanged，不重复调用 Embedding。`source_text` 保留用户直接表述，`source_reference` 指向具体会话/研究位置。引用 IDs 独立保存。

## 8. 模型记忆抽取

`extract_candidates(...)` 调用现有 `get_llm(role="fast")`。模型输入只有有限 `source_text`；Prompt 和 Structured Output Schema 不包含 Provider、Key、环境变量、Endpoint、scope 或用户身份。

LLM 只能输出 `memory_type + 逐字 content`。无长期信息返回空列表。程序逐项再次执行 Write Policy；非法候选只计入 rejected，不写库。Extracted Candidate 通过 `remember_extracted(...)` 只能保存为 pending，不进行 Embedding，也不能覆盖 active Memory。若调用方明确确认，应重新走 explicit 路径，不能让模型自行晋升。

真实第一次外部运行到达 Fast 模型后，候选校验结果为空且报告未保存原始候选，因此不能把原因虚构为某种确定分类错误；数据库仍为空。随后用相同公开 Fixture 做只读诊断，模型返回合法 focus，说明结构化能力可用。Prompt 增加了有限类型边界，“优先”同时允许作为最近合法的 focus/preference 线索，定向测试通过后重新执行完整 E2E。

## 9. 新旧记忆替代

只有 explicit confirmed 写入且满足相同 `scope + scope_id + memory_key` 时，程序才认为调用方明确更新同一语义槽位：

- 旧值相同：unchanged；
- 新值不同：事务内把旧记录设为 superseded/inactive，再插入新 confirmed/active；
- 新记录通过 `supersedes_memory_id` 指向旧记录；
- 旧记录保留来源和时间历史，不物理覆盖。

Extracted Candidate 即使使用相同 key，也只保存 pending，原 confirmed 保持 active。Embedding 失败发生在事务前；数据库 insert 失败会回滚旧状态。两条路径均有测试。

## 10. 持久化

使用独立 SQLite 文件和 schema，不复用 Knowledge 表。confirmed 记录保存单位向量；pending 不向量化。数据库固定一个 `provider/model + dimension` 空间，模型或维度变化时明确失败，避免混用旧向量。

短连接保证进程结束后可重开。E2E 使用独立 Python 子进程读取同一数据库，成功读到 1 条 confirmed Memory。数据层提供：

- `deactivate(memory_id, updated_at)`：软失效，保留历史；
- `delete(memory_id)`：按精确 ID 物理删除，供明确用户删除请求使用。

## 11. 检索

```python
from llm.retrieval_models import get_embedding
from memory import MemoryRetriever, MemoryStore

store = MemoryStore("experiments/output/memory.sqlite3")
retriever = MemoryRetriever(store, get_embedding())
result = retriever.retrieve_memory(
    "分析企业版竞争事件时应关注什么？",
    scope="workspace",
    scope_id="pia-workspace",
)
```

SQL 先过滤 scope、scope_id、active、confirmed 和可选 memory_type，再生成查询向量。无合格数据时返回 `no_results`，不调用 Embedding。默认 Top-K=5，上限 50；无相关性阈值。只进行精确余弦排名，不做 Rerank。返回中明确 `context_type=memory`，后续 Agent 可以与 Knowledge Context 分开注入。

## 12. 隐私与安全

- 常见认证信息在 LLM/Embedding 前拒绝，异常和日志不回显输入或环境值；
- Embedding 只发送通过 Secret 检查的 query 和 confirmed Memory content，不发送 source_text、来源引用、scope、身份、metadata 或数据库记录；
- Fast Extraction 只发送调用方给出的有限 source_text；事实型来源不允许抽取；
- Memory 来源、状态和历史均可追溯；支持精确失效和删除；
- 业务结果不包含 Provider、模型、Token、Endpoint 或 Key，运行审计独立保存；
- `.env` 未修改，Secret 不复制进数据库、报告或代码。

真实企业场景仍需单独确定数据最小化、保留周期、用户访问与删除流程、权限隔离、审计、备份删除、静态加密，以及向 Fast/Embedding 外部模型发送文本是否符合数据分级、企业安全策略和供应商协议。本版 scope 是逻辑过滤，不是认证或多租户权限边界；Secret 正则不是完整敏感信息/个人信息识别系统。

## 13. 定向测试

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_memory -q
```

结果：**31/31 PASS**。覆盖任务列出的 explicit focus/preference、Fast Candidate、空候选、临时操作、事实/Secret/无来源推断拒绝、来源追踪、三种状态、scope/scope_id 隔离、语义检索、跨进程持久化、supersession、模糊冲突、事务失败、公共 Embedding、Fast Role、删除/失效及模型可见面边界。

Memory 定向测试的最终结果为 31/31 PASS；完整最终证据见全量回归。

Mock 只证明控制逻辑，真实模型调用见下一节。

## 14. 真实端到端验收

```powershell
.\.venv\Scripts\python.exe -X utf8 experiments/memory_e2e.py
```

成功记录：`experiments/output/memory_v01_live_03.json`；数据库为同名 `.sqlite3`。两者位于既有忽略目录，不作为正式用户 Memory。

全部文本是脚本中明确标注的 Fixture，不含真实用户资料：

- Session 1：`后续竞品研究优先关注中国市场和企业权限能力。`
- Session 2 Query：`分析新的企业版竞争事件时应该重点考虑什么？`
- Update：`后续竞品研究改为优先关注日韩市场和商业化能力。`

结果：

- Fast 返回一个原文支持的 focus Candidate，程序校验通过；
- explicit 写入 confirmed/active，并保留 fixture 来源；
- 独立 Python 进程重开后计数仍为 1 confirmed；
- Session 2 返回中国市场和企业权限 Focus，`context_type=memory`，没有 Pricing/上线/发布等事实；
- 更新后新值 confirmed/active，旧值 superseded/inactive，总计 2 条历史记录；
- 完整运行 26.9905 秒，4 次真实模型调用，API 全部提供 usage，共 1,988 tokens。

受限网络中的第一次尝试在 Fast 调用处安全失败，记录 `memory_v01_live_01.json`；没有写库。第一次获准联网的完整运行在 Candidate 验收处失败，记录 `memory_v01_live_02.json`；没有写库，不能称为成功。类型指令调整后 `live_03` 通过。

## 15. 实际运行模型核验

| Role | 配置模型 | API 返回模型 | 调用与延迟 |
| --- | --- | --- | --- |
| Fast Extraction | aliyun_bailian / qwen3.7-flash | qwen3.7-flash | 1 次；20.603 s；1,947 tokens |
| Embedding | aliyun_bailian / text-embedding-v4 | text-embedding-v4 | 3 次；0.723–1.101 s；共 41 tokens |

Embedding 分别用于初始 confirmed 写入、Session 2 Query 和显式更新。没有 Rerank 或 Reasoning 调用。报告保存非敏感 provider/model、latency、usage 和 request ID；不保存 Secret 或认证头。

## 16. 全量回归

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
```

结果：**352/352 PASS**，运行 40.045 秒。包括原有 321 项与新增 31 项。冻结 Knowledge、Event Core、Source Discovery 和 Stage 1 测试均通过。已有 LangChain beta/异步调试提示以及故障注入日志不属于失败；未重跑其他模块的真实 E2E。

## 17. 代码质量

核心函数用中文说明职责、输入输出、Memory/Knowledge 边界、策略、supersession 和失败语义。使用现有 Pydantic、SQLite、Fast Role 与 Embedding Role，无新依赖、Agent、Factory、Manager、MCP Server 或动态路由。

新增文件 AST 解析通过，`git diff --check` 通过。冻结目录、公共 llm 和 requirements 与 `76c8560` 保持一致。模型可见 Prompt/Schema 未出现 Provider、模型名、Key、环境变量或 Endpoint。现有环境 Secret 值扫描将在最终检查中只报告命中数量，不输出值。

## 18. 已知限制

1. Write Policy 使用有限、保守的中英文语义线索和 Secret 正则；可能漏掉新的秘密格式，也可能拒绝合法表达。它不是 DLP、个人信息识别或完整事实分类器。
2. LLM 可能返回空候选、错误类型或不符合逐字规则的内容；程序会拒绝，不自动修复。真实 E2E 只有一个提取样例，不代表 Memory Write Precision/Recall 已达生产门槛。
3. Extracted Candidate 只能 pending，本版没有 pending 审核 UI 或晋升 API；明确确认需重新走 explicit 写入。
4. `memory_key` 由可信调用方提供。没有 key 时只做精确内容去重，不做语义 supersession；相同 key 的 explicit 新值被视为明确更新。
5. SQLite 文件没有应用层静态加密、用户认证、多租户权限、自动保留周期或备份擦除。本版 scope/scope_id 只是程序过滤条件。
6. confirmed Memory content 与查询会发送给配置的外部 Embedding 服务；Extraction source_text 会发送给 Fast 服务。企业环境必须先完成外发合规评估。
7. Memory 小库使用线性精确余弦，没有 ANN、Rerank、阈值或大规模并发测试。Top-K 命中不代表一定有用。
8. 同一模型名背后的供应商版本漂移无法由 `provider/model` 空间身份发现；需要人工新建库并重新向量化。
9. 删除 superseded 历史会按数据库外键规则清空新记录的历史指针；这是明确隐私删除优先于完整审计链的取舍。
10. source_text 为了可追溯会在本地保留直接来源，可能比 content 包含更多信息；调用方仍应在提交前最小化文本。本版不保存网页/Event/Evidence 全文。
11. 本轮没有 Triage、Research Agent、最终回答、Graph Memory、Knowledge Graph、人格画像或敏感属性推断。
12. Memory V0.1 的真实 E2E Fixture 曾用于 Prompt 调整，因此当前 E2E 只证明真实运行链路和边界机制可用，不能作为独立、无污染的 Memory Extraction 质量评测。后续正式 Evaluation 必须使用未参与 Prompt 开发的新 Gold Cases，至少评测 Memory Write Precision、Memory Recall、False Memory Rate、Supersession Accuracy 和 Scope Leakage；本轮不创建 Gold Set。

## 19. Git（版本管理）状态

本轮新增 `memory/` 八个文件、一个测试、一个 E2E 脚本和本报告，共 11 个交付文件。已有 tracked 文件没有修改，冻结业务逻辑不变。`tmp/` 保持既存未跟踪；`.env` 和 `experiments/output/` 受现有规则忽略。

本轮不暂存、不 Commit。最终须以 `git diff --check`、Secret 扫描和 `git status` 结果为准。

**Long-term Memory V0.1 = READY FOR REVIEW**。
