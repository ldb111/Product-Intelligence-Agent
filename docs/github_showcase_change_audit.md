# PIA GitHub 展示优化变更审计报告

审计日期：2026-10-12。实施基线：`cd62e2ae210a46ee1dba4ff36fc4828f5dedcb6b`。

PIA（Product Intelligence Agent，产品智能研究助手）本次仅优化中国 AI（人工智能）产品经理招聘阅读体验，没有开发新功能。README（仓库首页说明）负责快速理解；专题文档解释取舍并链接已有事实与验收依据，不重复编写完整技术验收。

## 完成内容与真实性

- 首页明确个人 MVP（最小可行产品）、目标用户、问题观察与产品假设，没有虚构商业用户、收益或团队。
- 将信息采集、事件识别、分诊、用户启动研究和同事件追问连成一条产品路径；说明当前手动触发、本地单用户运行。
- 新增产品决策、AI 分工、评测、演示和个人贡献说明。英文技术术语附中文解释；历史机器输出与代码字段保留原样，附中文导读和术语。
- Evaluation（评测）说明保留旧35条数据从独立基线转为开发回归的身份变化。新12条决策12/12同时披露类型4/7、日期问题、模型标注未经真人复核及非随机小样本限制。
- `.env.example` 仅列代码实际读取的配置；19个有效配置名、5个注释形式可选项。凭据为空，不指定默认模型。研究角色供应商凭据与排序地址推导均按真实加载器说明。
- 增加5张真实界面截图，分别展示首页、事件筛选、初始研究、事件内追问和引用展开；后两张是同一页面区域截图。没有构造新数据或拼接虚构界面。

## 修改文件：18个

| 文件 | 修改范围 |
|---|---|
| [.gitignore](../.gitignore) | 仅允许公开提交无凭据的 .env.example；运行产物忽略规则保留。 |
| [README.md](../README.md) | 改为简短招聘入口，加入定位、问题假设、流程、设计取舍与诚实评测摘要。 |
| [docs/PIA_MVP_Implementation_Fact_Index.md](PIA_MVP_Implementation_Fact_Index.md) | 增加招聘导读和术语说明，中文化章节标题；事实正文保留。 |
| [docs/current-sprint.md](current-sprint.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/decisions/model_selection_v0.1.md](decisions/model_selection_v0.1.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/decisions/model_visible_surface_v0.1.md](decisions/model_visible_surface_v0.1.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/demo_guide.md](demo_guide.md) | 标为历史演示记录，启动说明转向新演示文档，真实走查和测试记录保留。 |
| [docs/development-roadmap.md](development-roadmap.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/event_core_v0.1.md](event_core_v0.1.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/file_parsing_v0.1.md](file_parsing_v0.1.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/final_architecture.md](final_architecture.md) | 用中文解释当前状态、证据、研究会话和模型分工；复用现有详细架构入口。 |
| [docs/final_closure_report.md](final_closure_report.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/final_integration_v0.1.md](final_integration_v0.1.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/intelligence_layer_v0.1.md](intelligence_layer_v0.1.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/knowledge_layer_v0.1.md](knowledge_layer_v0.1.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/long_term_memory_v0.1.md](long_term_memory_v0.1.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/own_product_document_ingestion_v0.1.md](own_product_document_ingestion_v0.1.md) | 历史导读、中文标题和折叠术语说明；正文、代码块、数字和输出保留。 |
| [docs/stage1_acceptance.md](stage1_acceptance.md) | 历史导读、术语与标题；本地忽略产物链接改为历史路径说明，不改验收记录。 |

历史文档正文差异检查只发现两项预期调整：历史演示的启动导读、阶段一的本地忽略产物链接。全部历史代码块保持一致，没有改写测试数字或模型结果。

## 新增文件：13个

| 文件 | 用途 |
|---|---|
| [.env.example](../.env.example) | 严格依据实际加载器的空白配置模板。 |
| [docs/ai_architecture.md](ai_architecture.md) | 一页解释模型与程序、知识与记忆的职责。 |
| [docs/assets/screenshots/README.md](assets/screenshots/README.md) | 记录截图来源、日期、区域与只读边界。 |
| [docs/assets/screenshots/citation.png](assets/screenshots/citation.png) | 真实运行页面截图；既存数据，不触发模型或扫描。 |
| [docs/assets/screenshots/events.png](assets/screenshots/events.png) | 真实运行页面截图；既存数据，不触发模型或扫描。 |
| [docs/assets/screenshots/follow-up.png](assets/screenshots/follow-up.png) | 真实运行页面截图；既存数据，不触发模型或扫描。 |
| [docs/assets/screenshots/home.png](assets/screenshots/home.png) | 真实运行页面截图；既存数据，不触发模型或扫描。 |
| [docs/assets/screenshots/research.png](assets/screenshots/research.png) | 真实运行页面截图；既存数据，不触发模型或扫描。 |
| [docs/demo.md](demo.md) | 五类界面、真实配置与本地体验入口。 |
| [docs/evaluation.md](evaluation.md) | 解释数据身份、分母、失败案例与独立性边界。 |
| [docs/github_showcase_change_audit.md](github_showcase_change_audit.md) | 本次文件变更、验证与保留项的完整审计。 |
| [docs/product_decisions.md](product_decisions.md) | 问题、选择与代价；链接真实实现，避免复制验收过程。 |
| [docs/product_manager_contribution.md](product_manager_contribution.md) | 个人产品职责、可核查依据与 AI 辅助编程说明。 |

## 删除文件：0个

未清理临时目录、运行数据库或构建产物。没有删除既有文档。

## 未修改但审查过的重要文件

以下包括完整阅读、关键片段审查与实现入口定位，用于核对展示表述：

| 核对内容 | 文件 |
|---|---|
| 模型角色与凭据加载 | [llm/config.py](../llm/config.py)、[llm/client.py](../llm/client.py)、[llm/research.py](../llm/research.py)、[llm/retrieval_models.py](../llm/retrieval_models.py) |
| 服务配置与搜索凭据 | [api/main.py](../api/main.py)、[api/application.py](../api/application.py)、[腾讯搜索](../source_discovery/search/tencent_wsa.py) |
| 状态、资料与恢复 | [pipeline/orchestrator.py](../pipeline/orchestrator.py)、[pipeline/store.py](../pipeline/store.py)、[pipeline/documents.py](../pipeline/documents.py) |
| 模型判断与证据 | [事件输入](../event_core/inputs.py)、[事件证据](../event_core/evidence.py)、[分诊](../triage/workflow.py)、[研究智能体](../research/agent.py)、[研究工具](../research/tools.py)、[研究引用](../research/evidence.py) |
| 知识与用户偏好 | [knowledge/retrieval.py](../knowledge/retrieval.py)、[memory/product.py](../memory/product.py) |
| 页面与前端配置 | [App.tsx](../frontend/src/App.tsx)、[ResearchAnswer.tsx](../frontend/src/ResearchAnswer.tsx)、[接口客户端](../frontend/src/api/client.ts)、[Vite配置](../frontend/vite.config.ts) |
| 依赖与安装 | [requirements.txt](../requirements.txt)、[package.json](../frontend/package.json)、[锁文件](../frontend/package-lock.json) |
| 冻结评测依据 | [历史说明](../evals/docs/event_detection_evaluation_lineage.md)、[最终留出规则](../evals/docs/event_detection_final_holdout_v1.md)、[冻结数据](../evals/gold/event_detection_final_holdout_v1.jsonl)、[执行器](../evals/runners/event_detection_closure.py) |
| 测试依据 | [模型角色](../tests/test_llm_roles.py)、[检索配置](../tests/test_retrieval_models.py)、[追问](../tests/test_follow_up.py)、[前端](../frontend/src/App.test.tsx) |
| 项目约束 | [AGENTS.md](../AGENTS.md) |

## 生产实现与冻结内容

生产代码修改：**否**。Prompt（提示词）、评测 Gold（金标准答案）、测试逻辑、依赖及历史结果文件均未修改。

没有重新执行模型评测或真实端到端调用。配置核验使用独立合成映射，不向外部服务发送数据。`.env` 未改写，其文件修改时间仍为2026-09-18；只在内存中用于比对可能泄漏的凭据，不输出值。

## 本轮验证结果

| 检查 | 结果 |
|---|---|
| 既有后端模型角色与检索配置测试 | 23/23通过；外部调用已模拟 |
| 既有前端测试 | 47/47通过 |
| TypeScript（带类型检查的编程语言）类型检查 | 通过 |
| 前端生产构建 | 通过，产物仍被忽略 |
| 配置模板核对 | 有效配置名、空凭据、角色密钥规则与检索地址推导通过 |
| 本地文档与图片链接 | 161处链接、其中3处章节锚点通过 |
| 5张截图 | 人工检查通过，正式图片与截图原件字节一致 |
| 历史代码块与正文范围 | 通过，未改写历史验收输出 |
| Secret（敏感凭据）扫描 | 227个已有及本轮正式文件：已配置凭据匹配0，常见密钥和私钥模式0；截图人工检查未见敏感内容 |
| `git diff --check` | 通过 |
| `git diff --cached --check` | 通过；仅检查原有暂存内容 |

前端测试首次因沙箱临时缓存文件缺失中断；在正常本地环境重跑47/47通过，没有改动测试。桌面截图入口未能可靠识别地址，因此停止操作该窗口，改用隔离浏览器读取本地应用；只允许本机 GET/HEAD（读取）请求，禁止其他请求，未触发模型、扫描或外站访问。

后端670/670属于2026-09-29历史验收，本轮没有重跑全量后端。新检查结果与历史成绩分别记录，不覆盖原始验收记录。

## Git（版本管理）差异与保留状态

本轮修改18个已有文件，新增13个文件，删除0个文件。已有受跟踪文件差异为361行增加、268行删除；该统计不含尚未跟踪的新文件。

没有暂存或提交本轮文件。开始前已暂存的 `PIA项目完整知识包_通俗易懂终审版.docx` 保持原状，不计入本轮31个交付文件。既有 `docs/full_implementation_drift_audit_v1.md`、`tmp/` 和运行数据保留。

`.env`、运行数据库、前端依赖与构建产物、动态评测结果没有加入暂存区。当前暂存区仍只有原有 Word（文档）文件，不能将其误认为本次新增改动。

## 验收边界

30秒、3分钟、10分钟的阅读路径已按定位、流程与设计依据组织；尚未开展招聘方盲读或真实招聘转化测试，不能宣称已经验证阅读时长或求职效果。

文档展示优化完成；本轮不包含公开发布、推送或代码提交。
