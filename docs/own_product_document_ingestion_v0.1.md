# 自身产品资料接入 V0.1

> 历史记录：本文记录对应阶段的开发与验收状态，保留原阶段计划、验收数字、代码字段与输出，不代表当前产品状态、待办或 Git（版本管理）状态。当前产品能力见[实现事实索引](PIA_MVP_Implementation_Fact_Index.md)，设计取舍见[产品决策](product_decisions.md)。

<details>
<summary>历史记录阅读术语（代码字段与原始输出保留原样）</summary>

Evaluation（评测）；RAG（检索增强生成）；Memory（长期记忆）；Knowledge（产品知识）；Triage（事件分诊）；Research（研究）；Event（竞争事件）；Evidence（证据）；Schema（数据结构）；Verification（核验）；Pipeline（产品工作流）；Store（存储）；Embedding（向量化）；Rerank（重排序）；E2E（端到端验收）；Regression（回归测试）；API（程序接口）；SQLite（本地数据库）；State（状态）；Runtime（运行时）。

</details>

## 产品资料生命周期

自身产品资料属于 Workspace Configuration，不属于 Monitoring：

```text
Workspace / Product Configuration
  → add_own_product_document / replace_own_product_document
  → parse_and_prepare
  → KnowledgeIngestor
  → KnowledgeStore
  → persisted own_product Knowledge

Monitoring / Triage / Research
  → 只检索已经持久化的 Knowledge
```

`OwnProductDocumentService` 是正式产品服务；`ProductPipeline` 提供同名薄入口，方便未来 UI
调用。Add 或 Replace 不会自动启动 Monitoring、Event Detection、Triage、Verification、Research
或 Long-term Memory 写入。

```python
from pipeline import OwnProductDocumentService
from pipeline.schemas import ProductInput

documents = OwnProductDocumentService("data/workspace-state")
added = documents.add_own_product_document(
    workspace_id="workspace-a",
    product_id="my-product",
    file="uploads/product-manual.docx",
)

# Monitoring 使用同一个 own_product_id，只消费上面已持久化的 Knowledge。
monitoring = ProductInput(
    workspace_id="workspace-a",
    own_product_id="my-product",
    competitor_name="competitor-a",
)
```

## 资料身份与当前版本

每次 Add 成功会生成并持久化稳定的 `doc_...` 产品文档 ID。它不包含绝对路径，不依赖
Python runtime hash，也不会随内容变化。完整源文件 SHA-256 是独立的 current content identity。

V0.1 采用 **CURRENT VERSION ONLY**：Replace 保持产品文档 ID 不变，并原子切换当前
KnowledgeDocument set；不保存 Version 1/2/3、历史检索、回滚或版本对比。

Add 使用完整源文件哈希在当前 workspace/product 内判断精确重复。相同 bytes 即使来自不同路径，
也复用已有文档，不重新解析、Embedding 或增加 chunks。文件名只用于展示和同名冲突提醒；同名、
不同内容不会自动覆盖，必须显式 Replace。

## 原子替换

一个文件可生成多个 `<document_id>:unit:N` KnowledgeDocument。Replace 先在 SQLite 事务之外
完成解析、Chunking 与全部 Embedding，然后在一个 KnowledgeStore 事务内：

1. 校验当前 Registry content hash，避免并发覆盖；
2. 删除当前 unit documents，外键级联删除旧 chunks；
3. 写入完整的新 unit documents/chunks/vectors；
4. 更新同库中的当前 Document Registry；
5. 提交事务。

任一步失败都会回滚，旧 Registry、旧 units 和旧 Retrieval 保持完整。结构缩减时被删除的页、
Sheet 或 Section 不再存在于 Store、Retrieval candidate 或 Rerank candidate。外部 Embedding API
不会在持有 SQLite 写事务时调用。

## 并发登记规则

并发 Add 的事务外 duplicate 预检只用于节省常规路径的 Parsing/Embedding；最终唯一性由
`BEGIN IMMEDIATE` 事务内重检和 Registry 唯一约束共同保证。同一 workspace/product 中两个
相同 bytes、不同路径的请求即使都完成了预计算，也只会持久化一个 logical document、一套 units
和一套 chunks，并向两个调用返回同一个稳定 document ID。并发预计算可能重复一次 Embedding，
但不会产生重复 Knowledge。同名不同内容仍返回受控 conflict；不同名称、不同内容不会被误合并。

Replace 使用 optimistic concurrency：请求开始时读取的 current content hash 会作为 expected hash
传入 Store，并在正式写事务内与最新 Registry 再次比较。两个都基于 A 准备的 A→B、A→C 请求中，
先提交的 B 成为 current，后进入事务的 C 返回 stale conflict，不会静默覆盖 B，也不会留下 C 的
部分 chunks。用户重新读取 B 后再次明确 Replace，才可完成 B→C。两个并发 A→B 请求则收敛为
current 与 unchanged 等价结果。

Registry 始终保持两个约束：每个 logical document 只有一个 current content state；同一作用域内
不能有两个 current logical documents 使用相同完整源文件哈希。V0.1 仍不保存版本历史。

## 状态与产品输出

Parsing 沿用 File Parsing 状态：`pending/parsing/parsed/failed/unsupported/scanned_or_no_text`。
Knowledge ingestion 使用 `not_started/ingesting/completed/failed`。产品结果只返回稳定 document ID、
display name、format、两阶段状态、当前内容状态、Knowledge 可用状态、时间和 unit 数量；不返回本机
路径、SQLite key、unit ID、chunks、向量或异常堆栈。

Replace 解析或 Embedding 失败时，结果分别保留真实 parsing/ingestion 状态，并明确当前旧知识仍然
可用。`scanned_or_no_text` 的产品语义是：“未提取到足够的文本内容，可能是扫描型 PDF，当前版本
暂不支持 OCR。”它不是对扫描 PDF 的确定识别。

## 格式与来源追溯

统一入口支持 `.docx`、text-layer `.pdf`、`.xlsx`、`.md`、`.txt`。不支持 `.doc`、`.xls`、
PPT/PPTX 或扫描型 PDF OCR。

每个 unit 继续保留 PDF page、XLSX sheet/range、DOCX/Markdown heading path、source reference、
完整源文件 SHA-256，以及既有 chunk index、table context 和 offset。所有文档固定进入
`own_product`，并绑定 workspace/product scope；内容中出现竞品名称不会改变知识域。

## 知识契约例外

Knowledge V0.1 仅增加两个窄能力：当前 Document Registry，以及
`KnowledgeIngestor.replace_document_set()` → `KnowledgeStore.replace_document_set()` 的整组原子替换。
既有单文档 `ingest()`、双知识域、Chunk Schema、Embedding/Rerank、Retrieval ranking、过滤字段、
Top-N/Top-K 和 Event/Evidence admission 语义均未改变。

## 已知限制

- 当前是同步本地服务，不提供异步任务队列、进度订阅或取消。
- SQLite 事务保证单数据库内的整组替换；V0.1 不提供跨主机分布式锁。
- Registry 只保留当前版本，不提供历史恢复。
- Scope 过滤是数据隔离机制，不等同于完整用户认证或 RBAC。
- Own Product 原文会按现有 Embedding/Rerank 配置发送必要文本到外部模型服务；生产仍需执行数据分级、脱敏和供应商合规评估。
- 本轮没有 UI，也没有独立解析质量或 RAG 质量 Evaluation。

## 验证

- File Parsing + Knowledge + Own Product Integration + Pipeline：**150/150 PASS**。
- Own Product Document Controlled E2E：**PASS**。覆盖 DOCX Add/Retrieval/Rerank、3→2 units Replace、stale Section 删除、跨路径重复和 Store reopen。
- Own Product Document Tests：**16/16 PASS**，其中 4 项为受控并发测试，覆盖 exact duplicate、不同内容 Add、stale Replace/retry 和同目标幂等 Replace。
- Repair A / B / C Controlled E2E：全部 **PASS**。
- Full Regression：**535/535 PASS**，82.399 秒。
