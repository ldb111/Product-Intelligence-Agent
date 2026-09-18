# Knowledge Layer V0.1 Implementation Report

状态：**READY FOR REVIEW**。实现、自动化检查和最小真实 RAG 验收通过；不是正式 Done 或新的冻结声明。仍需项目本人实际运行、验收并能解释核心代码。本轮未 Commit。

## 1. Repository Inspection

已读取 AGENTS.md、仓库结构、Event Core Schema/Input/Evidence、Source Discovery Schema/配置与候选边界、公共 llm 配置及客户端、requirements、模型选型与模型可见面 decision docs、已有测试和上轮真实验收报告。

- HEAD：`777d54b644a4eec37b15ec3bfae966390cca53b3`，Event Core 冻结提交。
- Source Discovery 冻结提交：`0925b2a6d2d4203ebe6aa712f8042a0610f33f27`。
- 新增测试前实际发现 279 个测试，与任务基线一致。
- Windows Python **3.13.0**，SQLite **3.45.3**，requests **2.34.2**，pydantic **2.13.5**，python-dotenv **1.2.3**。
- 仓库没有向量数据库依赖。使用 Python 标准库 SQLite 和数学运算，**无新增依赖**，requirements 不变；不需要安装 FAISS/Chroma 或大型服务。
- 开始时已有未跟踪 `tmp/`，本轮未修改。

## 2. Architecture

```text
显式产品资料 / 已核验的竞品事实
    → KnowledgeDocument → 入库资格检查 → normalize
    → structure-aware chunking → 公共 Embedding Role
    → SQLite 原子持久化

query + 显式知识域
    → 公共 Embedding Role
    → SQL 域过滤 + 程序元数据过滤 → 余弦 Top-N
    → 公共 Rerank Role → Top-K + 原始来源
```

`knowledge/` 只依赖 EmbeddingRole/RerankRole 契约。`llm/retrieval_models.py` 负责配置、HTTP 协议和脱敏运行审计；没有 Chat fallback、System Prompt 或 Agent 循环。独立增加公共角色文件，未重构冻结的 Chat/Event/Discovery 配置。

## 3. Files Changed

全部为新增文件，原有受跟踪文件没有修改：

| 文件 | 职责 |
| --- | --- |
| `knowledge/__init__.py` | 公共知识层入口 |
| `knowledge/schemas.py` | 文档、切片、检索返回与错误契约 |
| `knowledge/chunking.py` | 规范化、结构切片、精确原文区间 |
| `knowledge/inputs.py` | 用户文件与正式 Event/Evidence 的只读适配 |
| `knowledge/ingestion.py` | 入库资格、去重、向量化工作流 |
| `knowledge/store.py` | SQLite 持久化、事务替换、精确向量召回 |
| `knowledge/retrieval.py` | 显式知识域、候选召回、排序与最终结果 |
| `llm/retrieval_models.py` | 公共专用模型角色及真实 HTTP 调用 |
| `tests/test_knowledge.py` | 29 项知识层控制测试 |
| `tests/test_retrieval_models.py` | 13 项配置与 HTTP 契约测试 |
| `experiments/fixtures/knowledge_own_product.md` | 明确标注的自身产品测试 Fixture |
| `experiments/knowledge_e2e.py` | 最小真实 RAG 验收脚本 |
| `docs/knowledge_layer_v0.1.md` | 本报告和运行说明 |

本地验收 JSON/SQLite 位于已有忽略目录 `experiments/output/`，不作为正式产品数据。

## 4. Knowledge Domains

文档必须属于 `own_product` 或 `competitor`。数据库文档主键为 `(domain, document_id)`，chunk ID 也包含域身份。相同文本、相同文档 ID 在两个域不会碰撞。

`retrieve(query, knowledge_domain=...)` 没有默认域；必须指定 `own_product`、`competitor` 或 `both`。域过滤发生在向量比较之前。both 表示允许两域参加本次召回，**不保证每次 Top-K 都包含两域**；排名仍按相关性，没有人为分配域配额。

Own Product 只接收调用方明确提供的产品资料，可标记 PRD、定位、能力、定价、Release Notes、Roadmap 等来源类型。不扫描目录或生成正式产品事实。竞品只接收已确认的官方内容、事件证据、Evidence 和确认产品状态；要求 `verification_reference`。未验证候选、pending/conflict、Search Result 和当前尚未实现的 Research Result 会明确拒绝。

## 5. Knowledge Schema

每个切片保存 `knowledge_domain`、`document_id`、`chunk_id`、`product_id`、`competitor_id`、`source_type`、`source_url`、`source_reference`、`title`、`content`、`observed_at`、`document_time`、`evidence_status`、`content_hash`、`metadata`。

另保存 `source_credibility`、`verification_reference`、`document_content_hash`、`chunk_index`、`heading_path`、`table_context`、`start_offset`、`end_offset`。不适用字段保持 null。`observed_at` 必须带时区；不推算事件时间。

Research Result 后续可复用文档/来源契约；需要先明确验证规则再扩展入库类型，本轮不实现 Research。

## 6. Ingestion

`KnowledgeIngestor.ingest(document)` 是确定性工作流，返回 `inserted`、`updated` 或 `unchanged`。

正文统一换行和首尾空白后计算 SHA-256。完整入库指纹还覆盖元数据、标题、切片版本/长度和向量空间，防止正文相同时漏更新来源信息。完全重复入库不调用 Embedding、不增加切片；同一域的同一文档 ID 更新会原子替换全部旧切片。

Embedding 全部成功后才写入；写入事务失败会回滚，旧知识仍可读取。不同 document_id 即使正文相同也保留各自来源，不自动合并可能不同的知识身份。

## 7. Chunking

优先识别 Markdown ATX 标题、章节、空行段落、列表和表格。相同章节的小段可以合并，不同章节分开；围栏代码里的 `#` 不当作标题。单个超长结构块先按换行/句子边界拆分，再按字符长度兜底。

默认正文上限 1,200 字符，可设 100–2,000。标题、章节和表头单独保存为上下文，不改写原文；超长表格切片重复表头上下文。正文保持规范化文档的精确子串，方便按区间核对引用。

不是完整 Markdown 解析器；不支持复杂 HTML、PDF/DOCX 解析、Setext 标题或完整嵌套列表语义。超长标题/表头明确拒绝，不静默丢失上下文。字符上限不是 Token 精确上限，模型输入超限由 API 明确失败。

## 8. Embedding

模型名和供应商来自环境：

```dotenv
EMBEDDING_PROVIDER=aliyun_bailian
EMBEDDING_MODEL=text-embedding-v4
RERANK_PROVIDER=aliyun_bailian
RERANK_MODEL=qwen3-rerank
```

现有本地环境已包含以上四项，本轮未改 `.env`。当角色供应商与 `LLM_PROVIDER` 匹配时复用 `LLM_API_KEY` 和 `LLM_BASE_URL`；也支持既有供应商级凭据配置。不会复制保存 Key，也不会借用不匹配供应商的凭据。

已按[阿里云 Embedding 官方接口](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)核对 HTTP 请求。每批最多 10 条，不传 dimensions，真实返回 **1024 维**。校验响应数量、index、有限非零向量以及维度一致性；不能用 Chat 输出替代向量。

## 9. Vector Storage

SQLite 保存规范化完整文档、切片 JSON 和真实向量。向量以单位向量保存，查询通过余弦相似度排序；没有字符串匹配冒充向量检索。

一个数据库固定 `provider/model + dimension` 向量空间。查询或写入使用不同模型/维度时明确失败，需新建库重新入库。该身份只用于程序存储校验，不进入业务检索返回。

每次操作及时关闭连接，避免 Windows 文件占用。写入使用事务和外键级联删除；数据库重开及独立 Python 进程读取已真实验证。当前是线性精确扫描，适合小库；未来大规模语料可替换专用向量索引，无需本轮提前引入。

## 10. Retrieval

默认 `top_n=10`、`top_k=5`，要求 `1 <= top_k <= top_n <= 100`。支持 `product_id`、`competitor_id`、`source_type`、`evidence_status` 的精确过滤；过滤在 Top-N 截断前完成，避免其他产品占用候选名额。这是元数据筛选，不是完整访问控制。

最小调用示例，`document` 必须来自用户提供资料或已核验证据：

```python
from knowledge import KnowledgeIngestor, KnowledgeRetriever, KnowledgeStore
from llm.retrieval_models import get_embedding, get_rerank

store = KnowledgeStore("experiments/output/my_knowledge.sqlite3")
embedding = get_embedding()
ingestor = KnowledgeIngestor(store, embedding)
# ingestor.ingest(document)

retriever = KnowledgeRetriever(store, embedding, get_rerank())
response = retriever.retrieve("免费套餐如何补充积分？", knowledge_domain="competitor")
for result in response.results:
    print(result.content, result.source_url, result.source_reference)
```

没有可用候选时返回 `status="no_results"` 和空列表，不调用 Rerank。没有相关性阈值，存在候选不代表一定能够回答问题；本轮不生成答案。

## 11. Rerank

按[阿里云 qwen3-rerank 官方接口](https://help.aliyun.com/zh/model-studio/text-rerank-api)核对扁平请求体和顶层 `results`，未使用旧排序接口的 `input/parameters/output` 结构。现有环境使用 `/compatible-api/v1/reranks`；公共层从已配置的服务地址推导路径，可用非 Secret 的 `RERANK_BASE_URL` 显式覆盖。

只发送初步召回的候选正文，使用响应 index 关联本地结果。忽略响应里任何文档原文或 URL。返回不足、重复/越界 index、非法分数均失败；不会偷偷跳过排序。调用方可以在构造时显式传 `rerank=None` 做不排序的受控实验。

## 12. Provenance

检索结果携带来源元数据、正文区间、文档/切片哈希、retrieval score/rank、rerank score。`candidates` 保留向量排序，`results` 保留最终顺序，可为后续 Recall@K、Precision@K、MRR/NDCG、Domain Leakage 和 Provenance Correctness 提供数据；本轮没有创建 Gold Set 或评测框架。

Event 适配按 Evidence 分文档，避免把不同来源的事实归到同一 URL。保留 evidence ID、event ID、原 source_type、evidence_capability、evidence_context 和上游观察时间。confirmed 保持冻结 Event Core 的含义：有限引用已通过程序校验，不能扩大成全量事实核验。

Embedding/Rerank 输入只包含查询及标题/章节/表头/正文。Provider、Key、Endpoint、SDK、调用统计不拼入语义输入；运行审计通过独立的 `last_runtime` 提供。调用方提供的业务 metadata 仍需遵守模型可见面规范。

## 13. Targeted Tests

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_knowledge tests.test_retrieval_models -q
```

结果：**42/42 PASS**，29 项知识层 + 13 项公共模型角色测试。覆盖双域入库/隔离/both、结构切片和超长兜底、完整来源、精确去重/upsert、元数据更新、持久化、余弦排名、Top-N/Top-K、候选索引映射、配置、失败不伪造、空结果、向量空间冲突、事务回滚和 Secret 脱敏。

首轮 41 项通过，1 项在测试清理阶段失败：测试创建 SQLite 故障触发器后仅结束事务，未关闭连接，Windows 无法删除临时数据库。修正测试连接释放后 42 项通过；真实存储实现已使用显式 closing。

这些 Mock 测试只证明控制逻辑，真实模型证据见下一节。

## 14. Real E2E

```powershell
.\.venv\Scripts\python.exe -X utf8 experiments/knowledge_e2e.py
```

默认生成独立的带时间戳 JSON 和 SQLite。`--output`、`--database` 可指定新的目标路径；数据库非空时拒绝，避免把缓存误称为新调用。竞品默认依赖本地上轮 `experiments/output/event_core_v01_live_02.json`，可用 `--event-report` 显式指定相同契约的真实验收报告。

真实记录：`experiments/output/knowledge_v01_live_02.json`，数据库为同名 `.sqlite3`。

- Own：明确标注模拟产品的 Fixture，免费套餐每月 500 积分。
- Competitor：上轮真实官方 `https://z-report.cn/changelog` 的 Event/Evidence，免费档每月 2,000 积分；原观察时间为 `2026-09-17T21:17:36.429456+08:00`。本轮是**历史真实证据回放 + 实时专用模型调用**，未重新采集网页。
- 真实入库 2 份文档、5 个切片。重复入库均 unchanged，数量不增加。
- 独立 Python 进程重开后仍为 2 文档、5 切片。

| Query 域 | 候选数 | 最终 Top-K | 实际结果 | 延迟 |
| --- | ---: | ---: | --- | ---: |
| own_product | 4 | 1 | Fixture 每月 500 积分，仅自身域 | 1.3626 s |
| competitor | 1 | 1 | 官方证据每月 2,000 积分，仅竞品域 | 1.2861 s |
| both | 5 | 2 | 竞品 2,000 + 自身 Fixture 500，均保留来源 | 1.4001 s |

三条查询 **3/3 PASS**；候选和最终结果均检查 domain、来源一致性、原文区间和哈希。真实完整运行 **8.0975 s**，5 次 Embedding + 3 次 Rerank，API 报告 **1,019 total_tokens**，8 次均返回用量。未将用量估算为实际账单金额。

第一次受限环境运行在 Embedding 连接阶段失败，记录保留于 `knowledge_v01_live_01.json`，不计为成功或零费用证明。随后联网执行的自动审批曾因外发授权判断拒绝；补充用户第 20 节明确要求、具体 Fixture/公开证据以及实际目的地检查后，同一验收操作获准并通过。未绕过审批或改用其他执行通道。

## 15. Runtime Model Verification

| Role | 配置 provider/model | API 返回 model | 实际观察 |
| --- | --- | --- | --- |
| embedding | aliyun_bailian / text-embedding-v4 | text-embedding-v4，5/5 一致 | 1024 维；各调用 0.626–0.773 s |
| rerank | aliyun_bailian / qwen3-rerank | qwen3-rerank，3/3 一致 | 各调用 0.639–0.658 s |

运行 JSON 保存逐调用 request ID、model、response_model、latency、usage、input_count，以及全部候选与最终结果。不保存 Key 或认证头。

## 16. Full Regression

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -q
```

结果：**321/321 PASS**，包括原有 279 项与新增 42 项，运行 47.482 s。冻结 Stage 1、Source Discovery、Event Core 测试继续通过。运行中既有 LangChain beta/异步调试提示和故障注入测试日志不属于失败；未重跑 Source Discovery E2E。

## 17. Code Quality

核心函数有中文业务、输入输出和边界解释。优先直观函数、单层 Role 契约和 SQLite 事务，没有工厂链、Agent、复杂框架或新增依赖。

已检查 Python 语法、真实 requests.post 签名、`git diff --check` 和全部新增文件的空白格式。冻结目录及旧 llm/config.py、llm/client.py、requirements 与冻结提交保持一致。使用本地实际 Secret 值进行只返回命中数的检查，确认新增源码、测试、报告和验收产物未包含这些 Secret；没有读取展示完整环境文件。

## 18. Known Limitations

1. 小型本地 SQLite 精确扫描，没有 ANN、混合检索、大规模性能/并发压测或正式权限系统。
2. MVP 文件入口只支持 UTF-8 `.md/.txt`；其他资料需先提取正文并保留原来源。切片不是完整 Markdown/语义解析器。
3. 入库资格是可信调用方契约，不是自动事实核查或官方身份验证。Research Result 尚未开放入库；pending/conflict 明确拒绝。
4. Own Product Knowledge 可能包含 PRD、Roadmap、Pricing、产品能力等内部资料。Embedding / Rerank 会将完成检索所需的文本发送给配置的外部模型服务。生产环境需要根据数据分级、企业安全策略和模型供应商合规要求评估是否允许外发，并考虑敏感字段脱敏、企业级服务协议、租户隔离或私有化模型。本版只记录该边界，未实现权限、脱敏或私有化能力。
5. 当前只支持 aliyun_bailian 的已核实专用 HTTP 协议。未支持多个供应商或自动协议探测；不做隐藏重试或降级。
6. 真实 E2E 只有 2 文档、3 查询，只证明最小链路与这些样例，不能宣称达到通用检索质量指标或完整 RAG 质量验收。
7. 没有相关性阈值，both 不保证两域配额。没有生成最终问答。
8. upsert 替换旧版本，不保存知识版本历史。旧检索记录仍含内容/哈希，但库内不会保留已替换版本。模型名相同但供应商内部升级时，当前空间身份不能自动发现语义漂移。
9. 元数据变更会重新向量化；未做跨文档内容去重或 Embedding 缓存。超大文件、文档数和 Token 限额无自动调参。
10. 多批 API 调用中后续批次失败时，工作流不返回部分向量、不写入，但成功前序批次可能已计费；失败审计不保证汇总所有已计费用量。
11. 真实 Event/Evidence 回放文件和验收 SQLite/JSON 是本地忽略产物，新的仓库克隆需要用户提供已有真实报告或自行准备受支持的核验资料。没有将 Fixture 冒充正式产品数据。
12. 本轮没有 Memory、Triage、Research Agent、GraphRAG、Knowledge Graph、最终答案生成或前端。

## 19. Git Status

提交前核实新增 13 个交付文件；已有受跟踪文件没有修改。两个测试文件均不在 HEAD 或历史索引中，属于本轮真正新增；界面显示的增删行数不是 tracked 文件修改。原有 `tmp/` 保持未跟踪，未触碰。验收产物被既有 `experiments/output/` 规则忽略，`.env` 仍被忽略。

提交前 `git diff --check` 与暂存后的 `git diff --cached --check` 均须通过。本轮不更改冻结模块，不进入 Memory/Research。

**Knowledge Layer V0.1 = READY FOR REVIEW**。
