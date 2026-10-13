# 文件解析 V0.1 实现报告

> 历史记录：本文记录对应阶段的开发与验收状态，保留原阶段计划、验收数字、代码字段与输出，不代表当前产品状态、待办或 Git（版本管理）状态。当前产品能力见[实现事实索引](PIA_MVP_Implementation_Fact_Index.md)，设计取舍见[产品决策](product_decisions.md)。

<details>
<summary>历史记录阅读术语（代码字段与原始输出保留原样）</summary>

PIA（产品智能研究助手）；LLM（大语言模型）；Prompt（提示词）；Evaluation（评测）；RAG（检索增强生成）；Knowledge（产品知识）；Schema（数据结构）；Pipeline（产品工作流）；Artifact（成果资产）；Embedding（向量化）；Rerank（重排序）；E2E（端到端验收）；Fixture（测试材料）；Commit（代码提交）；HTTP（超文本传输协议）；SQLite（本地数据库）；Runtime（运行时）；Tool（工具）。

</details>

## 1. 仓库检查

实现基于冻结 Commit `15782f23688d926b895a519d7312fb4c3283e8cd`。开始时 tracked 工作区干净，仅有既存未跟踪 `tmp/`。已核对 `knowledge/`、`pipeline/`、requirements、现有 ingestion 接口、测试和相关报告。

现有 `own_product_file()` 只接收 UTF-8 `.md/.txt`，但 `KnowledgeDocument` 允许可信调用方提供正文和 metadata，`KnowledgeIngestor` 公开接收该契约。因此本轮通过新 Adapter 生成现有 KnowledgeDocument，无需修改冻结 Knowledge、Pipeline、Prompt、Schema、Chunking、Embedding、Rerank 或 Retrieval。

## 2. 架构

链路如下：

```text
User File
  → parse_document(path)
  → ParsingResult / ParsedDocument / ParsedUnit
  → parse_and_prepare(...)
  → existing KnowledgeDocument[]
  → existing KnowledgeIngestor
  → existing structure-aware Chunking
  → existing Embedding / Retrieval
```

`file_parsing/parser.py` 按后缀做五种确定性本地解析；`knowledge_adapter.py` 只负责契约转换。PDF 每页、XLSX 每个有效 Sheet、DOCX/Markdown 每个有效 Section 成为独立 KnowledgeDocument，从而让既有切片继承位置 provenance。没有 Parser Factory、插件系统、OCR、LLM 解析或新的 RAG。

## 3. 依赖

项目运行环境为 Python **3.13.0**。新增三个最小运行依赖并实际安装、导入和运行 Fixture：

| 依赖 | 实际版本 | 用途 | Python 要求 |
| --- | --- | --- | --- |
| python-docx | 1.2.0 | DOCX 段落、标题、列表、表格 | >= 3.9 |
| pypdf | 6.19.0 | PDF 文本层与页结构 | >= 3.9，官方列出 Python 3.13 |
| openpyxl | 3.1.5 | XLSX 单元格、公式、合并范围 | >= 3.8 |

依赖均为成熟的纯 Python 或提供 CPython 3.13 Windows wheel 的库。`reportlab` 和 Codex Artifact Tool 只用于生成、渲染本轮模拟 Fixture，不是应用运行依赖。

## 4. 文件变更

| 文件 | 作用 |
| --- | --- |
| `requirements.txt` | 增加三个解析依赖 |
| `file_parsing/__init__.py` | 稳定公共入口 |
| `file_parsing/schemas.py` | 统一状态、文档、单元、准备结果契约 |
| `file_parsing/parser.py` | TXT/MD/DOCX/PDF/XLSX 本地解析与资源限制 |
| `file_parsing/knowledge_adapter.py` | 现有 KnowledgeDocument 适配器 |
| `tests/test_file_parsing.py` | 37 项控制测试 |
| `experiments/file_parsing_e2e.py` | 五格式真实 RAG E2E |
| `experiments/fixtures/file_parsing/demo.*` | 5 个公开模拟 Fixture |
| `docs/file_parsing_v0.1.md` | 本报告 |

冻结业务模块没有修改。

## 5. 统一解析数据结构

`ParsingResult` 返回 `pending / parsing / parsed / failed / unsupported / scanned_or_no_text` 状态集合；同步解析入口实际返回终态，`pending/parsing` 留给后续 UI 任务生命周期使用。

`ParsedDocument` 包含 file_name、file_type、title、normalized_text、units、source_metadata、warnings。`ParsedUnit` 包含 text、heading_path、page_number、sheet_name、cell_range 和格式特有 metadata。

公开解析元数据只含文件名、格式、大小、SHA-256 和单元数，不暴露绝对路径。Knowledge Adapter 内部保留 source_reference，并在转换前重新计算 SHA-256，防止解析结果被误标到另一源文件。

`parse_and_prepare()` 返回状态、文档元数据、规范化正文、现有 KnowledgeDocument 列表和 warnings。后续 UI 不需要理解 python-docx、pypdf、openpyxl、Chunk 或 Embedding。

## 6. TXT（纯文本）与 Markdown（标记文本）

TXT 只按 UTF-8 with BOM / UTF-8 读取。编码失败返回 `failed + encoding_not_utf8`，不猜测系统编码。空文件返回 parsed 并记录 `empty_file`，不会制造入库正文。

Markdown 的 normalized_text 保留原始 Heading、List、Table 和围栏代码结构。Parser 只识别围栏代码外的 ATX 标题来建立 Section；生成入库单元时补齐完整父子标题路径，让冻结结构切片继续识别 Heading 和 Table。

## 7. DOCX（Word 文档）

使用 `Document.iter_inner_content()` 按正文顺序读取 Paragraph 与 Table。Heading 1–6 转成 Markdown 标题，可靠识别的 List 样式转成列表，表格转成 Markdown Table。

空 DOCX 返回 parsed 并记录 `empty_document`。图片、页眉页脚、浮动文本框分别记录 warning，不做 OCR 或复杂布局还原。`.doc`、`.docm` 和其他 Office 格式不支持，因此不会执行宏。

模拟 DOCX 已通过本机 Word 只读导出和 PNG 视觉检查：标题、列表、正文、表格完整，无截断或重叠。Codex bundled runtime 未提供 LibreOffice，故未使用缺失的 bundled renderer；没有改用 DOCX 文本抽取冒充视觉检查。

## 8. PDF（便携式文档）

每个含文字页面输出独立 ParsedUnit，page_number 从 1 开始，正文以 `# Page N` 标记。KnowledgeDocument metadata 和最终 Chunk metadata 均保留页码。

总可见文字少于 20 个非空白字符时返回 `scanned_or_no_text`，不进入 Knowledge 输入；空页单独记录 warning。加密、损坏或不可读 PDF 返回受控状态。没有 OCR、外部服务或标题猜测。

两页模拟 PDF 已使用 Poppler 实际渲染并逐页检查；中文正文、页序和布局正常。

## 9. XLSX（电子表格）

每个有效 Sheet 转成一个 Markdown Table 单元，保留 sheet_name、有效 cell_range、formula_cells 和 merged_ranges。空 Sheet 跳过并记录 warning；全空工作簿不会制造内容。

公式优先读取文件中已有 cached value，并将来源标为 `cached_value`。没有 cached value 时显示 `[公式: =...]` 并记录 `formula_without_cached_value`，不会把表达式当作计算结果。Parser 不启动 Excel，不执行宏、不重新计算公式，也不打开 Hyperlink 或外部引用。

模拟 XLSX 含两个 Sheet 和公式，使用 Artifact Tool 创建、重算、导出并逐 Sheet 渲染检查。表头、行、数值与公式结果可读，无截断。

## 10. 知识接入

`prepare_for_knowledge()` 只为 parsed 且有正文的单元生成 own_product KnowledgeDocument，沿用 `source_credibility=user_provided` 和现有 admission 规则。每个单元拥有稳定的调用方 document_id 前缀和 unit 序号；原文件绝对路径只保存在内部 source_reference。

PDF 页码、XLSX Sheet/Range、DOCX/Markdown heading_path 位于 metadata；对应 Markdown 标题仍进入 content，因此冻结 Chunking 可继续生成 heading_path/table_context。非 parsed 状态返回空 ingestion_input，不伪造内容。

现有 Knowledge 核心无需修改；E2E 直接把生成的 KnowledgeDocument 交给 KnowledgeIngestor。

## 11. 安全与限制

全部解析在本地完成；代码中没有 HTTP、浏览器、OCR、LLM、宏执行、外部链接读取或自动下载入口。外链测试将 URL 作为普通单元格数据，并验证网络请求和子进程均未调用。

默认限制：单文件 20 MiB、规范化正文 2,000,000 字符；DOCX/XLSX 最多 5,000 个 ZIP 成员、50 MiB 解压体积、1,000 倍压缩比；XLSX 单 Sheet 有效矩形最多 100,000 个单元格。压缩容器在解析库打开前完成基础预检，并拒绝加密 Office 容器。

错误只返回稳定 warning code，不返回解析库异常正文或无关本机信息。公开 source_metadata 不含绝对路径；内部 Knowledge provenance 通过文件名和 SHA-256 二次核对。

## 12. 定向测试

```text
python -X utf8 -m unittest tests.test_file_parsing -q
Ran 37 tests in 1.471s
OK
```

覆盖 TXT 正常/空/编码错误；Markdown Heading/Table/List/围栏；DOCX 段落/标题/表格/列表/顺序/空文档；PDF 文本/多页/页码/无文本/损坏；XLSX 单/多 Sheet、表头、空 Sheet、公式、合并单元格；不支持后缀、缺失、文件/正文超限、损坏容器、压缩比、provenance、来源错配、外链不执行、现有 Chunking 兼容及稳定 UI 入口。

pypdf 在损坏 PDF 负向测试中输出 header/EOF warning，断言结果仍为受控 `failed + invalid_pdf`，不是测试失败。

## 13. 真实端到端验收

最终验收产物为被忽略的 `experiments/output/file_parsing_v01_live_03.json` 及对应 SQLite。五个 Fixture 均为模拟 PIA 产品能力资料，不含用户私人数据。

```text
demo.md / demo.txt / demo.docx / demo.pdf / demo.xlsx
→ parse_and_prepare
→ 7 KnowledgeDocument
→ existing KnowledgeIngestor
→ 9 existing chunks
→ real Embedding / Rerank
→ query: 产品是否支持竞争事件的证据追踪？
```

五种格式检索 **5/5 PASS**。每个结果都返回正确源文件；PDF 返回 Page 1，XLSX 返回 `产品能力` Sheet，DOCX 与 Markdown 返回 `竞争事件证据追踪` 标题路径；结果正文包含证据、来源或回溯相关内容，没有把额度示例等无关 Sheet 作为最终答案。

最终运行 13.0045 秒，真实模型调用 17 次：Embedding 12 次 / 673 reported tokens，Rerank 5 次 / 822 reported tokens；合计 1,495 reported tokens，缺失 usage 0。首次沙箱运行在 Embedding 网络连接阶段失败并保留 live_01；获批使用相同模拟资料联网后 live_02 PASS；加入来源指纹校验后的最终 live_03 再次 PASS。

## 14. 全量回归

```text
python -X utf8 -m unittest discover -s tests -q
Ran 487 tests in 54.709s
OK
```

即冻结基线 450 项 + 新增 37 项。原 Knowledge、Pipeline 与其余模块测试无退化。日志中的存储、模型响应和页面质量 warning 来自既有负向测试。

## 15. 代码质量

解析职责按 schema、格式解析、Knowledge 适配分开，保持单层函数和明确中文说明。没有大型解析框架、Factory、Manager、格式插件或重复 Knowledge 逻辑。

DOCX/PDF/XLSX Fixture 均做真实结构或视觉检查；依赖在 Python 3.13 环境中通过 import、解析、入库和检索验证。最终 `git diff --check`、Secret、禁止路径与冻结模块检查见 Git 收口状态。

## 16. 已知限制

- 文本型 PDF 的读取顺序取决于 PDF 内部文本对象；多栏、复杂定位或字符映射异常可能影响顺序。20 字符阈值是保守启发式，不能完整识别所有扫描件；没有 OCR。
- DOCX 不解析图片、页眉页脚、浮动文本框、复杂编号层级、批注、修订和复杂页面布局。列表只保留可靠的项目/编号语义。
- XLSX 不计算公式。cached value 可能由文件创建者留下且已经过时；metadata 会标明来源，但不能证明值新鲜。实现使用 openpyxl 已加载单元格集合来避免遍历纯格式大区域，该内部表示升级时需回归验证。
- Markdown 只识别 ATX 标题，不实现完整 CommonMark AST。TXT 不做编码探测或格式推断。
- ZIP 预检提供基础上限，不等同于完整恶意文档沙箱。解析库自身漏洞、XML 复杂度和极端但未超限的文件仍需进程隔离与运行资源限制才能达到生产安全等级。
- 五格式已通过 `OwnProductDocumentService` 接入正式 Workspace Configuration 生命周期。服务复用 `parse_and_prepare()` 和现有 KnowledgeIngestor；Monitoring 不会重新解析上传文件。旧 `own_product_documents` 仅保留 compatibility 字段，不再作为正式上传入口。本轮不进入 UI。
- Own Product 原文进入现有 Knowledge 后，Embedding/Rerank 的外发边界不变。生产仍需依据数据分级、企业策略和供应商合规要求处理脱敏、企业协议、租户隔离或私有模型。
- E2E 是 5 个小型模拟 Fixture 和一个查询，只证明链路与 provenance，不是通用解析准确率、RAG 质量或安全 Evaluation。

## 17. Git（版本管理）状态

冻结 Knowledge、Pipeline、LLM、Prompt、Schema 和既有测试文件未修改；仅修改 `requirements.txt` 并新增 File Parsing 源码、测试、Fixture、E2E 和报告。`.env` 未修改，既存 `tmp/` 保留，真实 E2E 输出位于既有忽略目录。

本轮不 Commit，不进入 Demo UI 或 Evaluation。

最终 `git status --short`（`tmp/` 展开内容均为本轮开始前既存文件）：

```text
 M requirements.txt
?? docs/file_parsing_v0.1.md
?? experiments/file_parsing_e2e.py
?? experiments/fixtures/file_parsing/
?? file_parsing/
?? tests/test_file_parsing.py
?? tmp/
```

**File Parsing V0.1 = READY FOR REVIEW**
