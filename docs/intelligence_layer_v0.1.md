# 分诊与研究层 V0.1 实现报告

> 历史记录：本文保留原阶段计划、验收数字、代码字段与输出，不代表当前待办。当前产品能力见[实现事实索引](PIA_MVP_Implementation_Fact_Index.md)，面试阅读入口见[产品决策](product_decisions.md)。

<details>
<summary>历史记录阅读术语（代码字段与原始输出保留原样）</summary>

MVP（最小可行产品）；Prompt（提示词）；Gold（金标准答案）；Agent（智能体）；RAG（检索增强生成）；Memory（长期记忆）；Knowledge（产品知识）；Triage（事件分诊）；Research（研究）；Event Core（事件核心）；Event（竞争事件）；Evidence（证据）；Citation（引用）；Schema（数据结构）；Pending（待确认候选）；Verification（核验）；Embedding（向量化）；Rerank（重排序）；Source Discovery（来源发现）；Stage 1（采集与状态比较阶段）；E2E（端到端验收）；Fixture（测试材料）；Commit（代码提交）；HEAD（当前提交）；API（程序接口）；Tool（工具）；Fast（快速模型角色）；Reasoning（推理模型角色）；Deep Research（深入研究）；Done（完成）。

</details>

## 1. 仓库检查

仓库：`E:\Product-Intelligence-Agent`。本轮开始时 HEAD 为 `ae4c556`，只有既有 `tmp/` 未跟踪；没有 tracked 修改。已检查 AGENTS.md、冻结模块接口、模型选型与模型可见面决策文档、现有测试和真实验收脚本。

冻结基线：Source Discovery `0925b2a`、Event Core `777d54b`、Knowledge `76c8560`、Memory `ae4c556`；原全量测试基线 352 项。新层通过现有 Python 公共接口读取这些模块，不改变其业务行为。

## 2. 架构

```text
confirmed Event → 程序组装双域 RAG / confirmed Memory → Reasoning Triage
                                                       ├ research → Deep Research
                                                       ├ monitor
                                                       └ no_action
uncertain Candidate ───────────────────────────────────→ Verification

同一个 Research Agent：SEARCH_OPEN → SEARCH_CLOSED → FINALIZATION → COMPLETE
                       五个只读工具                 一次无工具结构化请求
```

Verification 和 Deep Research 共用循环、工具、来源登记与公共 Research 模型角色。不存在第二个 Agent、Planner/Critic、图递归、MCP Server 或新增框架。结果由调用方消费；未接最终 Integration/UI。

## 3. 文件变更

全部为本轮新增文件，原 tracked 文件未修改：

| 文件 | 职责 |
| --- | --- |
| `llm/research.py` | Research 配置、原生工具会话、独立最终输出、脱敏运行记录 |
| `triage/__init__.py` | 分诊公共入口 |
| `triage/schemas.py` | 三态路由、关注/事实分离、程序绑定事件 ID |
| `triage/prompts.py` | 有限输入的优先级判断规则 |
| `triage/workflow.py` | 固定双域检索、Memory 召回及一次 Reasoning 判断 |
| `research/__init__.py` | 单 Agent 包说明 |
| `research/schemas.py` | 声明、比较、核验和程序来源契约 |
| `research/evidence.py` | 稳定 ID、逐字引用校验、来源 Materialization |
| `research/tools.py` | 五个冻结能力的只读包装和范围限制 |
| `research/prompts.py` | 研究与最终输出的业务规则 |
| `research/agent.py` | 双模式有界循环、停止状态、运行审计 |
| `tests/test_intelligence.py` | 分诊、工具、证据、角色及停止边界控制测试 |
| `experiments/intelligence_e2e.py` | 三个最小真实案例及检查点报告 |
| `docs/intelligence_layer_v0.1.md` | 本报告 |

## 4. 事件分诊

仅接受 `decision=event` 的 EventCandidate。路由为 research / monitor / no_action，输出 reason、focus_relevance、own_product_relevance、evidence_refs、memory_context_refs、uncertainty、missing_context；无数字置信度。

`event_id` 由程序填回，不允许模型修改事件归属。无自身直接证据时用 `insufficient_own_product_context` 和空比较列表，并显式记录 missing_context。未知能力不能表述为“不支持”。

## 5. 分诊上下文组装

程序从竞品名、事件主体、变化动作及 change_items 构建查询；依次检索自身域、竞品域、Memory。自身域固定 product_id，竞品域固定 competitor_id；记忆固定 scope/scope_id，且必须 active + confirmed。模型无权改变这些范围。

输入明确分成 event、knowledge_context.own_product、knowledge_context.competitor、memory_context，另接收可选 product_profile 和 current_focus。Profile 不替代自身知识证据。审计用量与内部存储路径不进入模型上下文。整个分诊只有一次无工具 `decide`。

## 6. 研究智能体

`ResearchAgent.run` 使用一个模式参数选择目标 Schema。模型实际返回 function tool calls；程序验证、执行并送回工具结果，模型根据新证据决定下一步。自由文本中的“工具调用”不会被执行。

每次研究必须新建 Agent 和工具上下文，防止复用旧任务的来源 ID。输出包含业务 result、程序 citations、evidence_registry 和 audit；审计含工具轨迹、停止原因、实际模型、用量及耗时，不含原始请求或隐藏推理。

## 7. 核验模式

仅接受 uncertain 候选，输出 event / no_event / still_pending、reason、new_evidence_refs、support、unresolved_questions。不修改 Event Core 候选或 Schema。

定论要求本轮新增的直接官方证据；未读摘要、旧证据重贴新标签、Memory、空搜索结果均不能支撑定论。仍不充分时输出 still_pending 并保留问题。校验失败明确报错，不自动转换成 no_event。

## 8. 深入研究模式

要求已确认 Event 以及同一 event_id 的 research 分诊结果。输出 event_summary、background、competitor_context、own_product_comparison、implications、watch_next、uncertainties、evidence_refs。

每条自身比较同时引用自身知识和竞品事实；两边不能拆成无证据联系的条目。影响与下一步行动只能标记 inference/recommendation。Fixture 仅作模拟比较，不当作真实产品能力。

## 9. 工具

| 工具 | 复用接口 | 关键边界 |
| --- | --- | --- |
| web_search | TencentWsaSearchProvider.search | 最多返回 5 个线索，摘要不可引用 |
| read_page | read_page_for_discovery | 只接登记的 candidate_id，采集 PASS 后才产生证据 |
| retrieve_competitor_knowledge | KnowledgeRetriever.retrieve | 固定 competitor 域及 competitor_id |
| retrieve_own_product_knowledge | KnowledgeRetriever.retrieve | 固定 own_product 域及 product_id |
| retrieve_memory | MemoryRetriever.retrieve_memory | 固定范围，只读 active confirmed 上下文 |

工具描述单独定义，仅包括业务用途、输入、输出和边界，不直接暴露开发函数 Docstring。模型参数禁止额外字段。页面正文最多 16,000 字符并标记截断；不能将截断内容或未找到内容当作不存在。所有工具异常终止搜索，技术异常正文不传回模型。

## 10. 证据与引用落地

登记 ID 按任务稳定：event_evidence、candidate、research_evidence、knowledge、memory 分别编号。重复相同来源/正文复用 ID。模型输出 Schema 没有 URL/title/source/domain 字段，只能选择已存在 result_id，并提供待校验的逐字支持片段。

程序检查 ID 存在、来源可作事实、excerpt 是对应原文连续子串、总引用与逐项支持一致，然后从不可变登记对象补齐 URL/title/source/content/domain、原始 source_id 和观察时间。候选搜索结果一直保持 unread，不因有 URL 就变成已读证据；分诊和最终生成阶段不接收这些不可引用线索。

同一原始 URL/来源引用的材料带相同 source_group；Event、RAG 和重新读取的页面不能被模型称为独立来源的交叉印证。分组由程序计算，不暴露底层服务配置。

Memory 永远不能出现在产品事实 support 中。新读取的第三方页面只可用于待核实推论，不能升级为 confirmed fact 或覆盖官方证据。引用链机械校验不等同于证明文字结论与原文在语义上完全一致，详见限制。

## 11. 模型角色

Triage 调用冻结的 `get_llm(role="reasoning")`。Research 使用等价独立公共入口 `get_research_llm()`，只读 `LLM_RESEARCH_PROVIDER / BASE_URL / MODEL`，复用对应供应商的 `*_API_KEY`；当前配置复用已有 DEEPSEEK_API_KEY。缺配置直接失败，没有 Fast/Reasoning 回退。

复用已安装的 OpenAI-compatible SDK，不新增依赖。当前通用 ChatOpenAI 不保留特定服务返回的 reasoning_content；Research 传输层仅在会话内原样回传该字段，不记录或作为业务证据。此协议要求已核对 [DeepSeek 官方工具调用说明](https://api-docs.deepseek.com/guides/thinking_mode/)。供应商与模型名称只进入开发 audit，不进入工具描述或业务提示词。

## 12. 智能体流程与强制停止

默认最多 10 次实际工具尝试、2 次搜索、3 次页面读取、7 次模型工具决策轮次，另加 1 次 Finalization；预算必须为正整数。同一模型批次中的多个调用也逐个检查预算。

模型结束、总预算耗尽、分项预算阻断、无效参数或工具失败均退出搜索。先标记 SEARCH_CLOSED，再调用独立无工具的 Finalization；不带旧规划文字、不再分派工具。最终 Schema/证据校验失败不重试。没有递归图或 blocked-call → continue 路径。

## 13. 幻觉约束

程序负责可机械判断的身份、范围、状态、ID、逐字片段和工具预算。提示词负责语义边界：记忆只影响关注，资料缺失不代表能力缺失，观察时间不等于事件日期，页面条目日期不直接视为生效时间，建议不得包装成事实。

Research 运行不调用写入接口，输出不自动进入 RAG/Memory。真实验收的显式 Fixture 准备与 Agent 运行分离，并比较运行前后两个库的记录数量。

## 14. 定向测试

`python -X utf8 -m unittest tests.test_intelligence -q`：62/62 PASS。

覆盖三态路由与事件不变、Reasoning 角色、分诊不联网、双域/Memory 范围、伪造 ID/地址/片段、无自身上下文、Memory 非事实、第三方来源、核验支持/否定/未找到/未解决、五个工具、批次与分项预算、停止后不再调用工具、无效参数终止、配置缺失/无回退和传输字段兼容。

这些是假模型控制测试，证明程序正确处理不同路由，不代表真实语义 Routing Accuracy 或 Verification Accuracy。

## 15. 真实端到端验收

验收数据：已有真实官方 Event（OctoReport 智报更新日志）+ 仓库自身产品 Fixture + 明确模拟的长期关注；不会将这些 Fixture 声称为用户真实产品数据。每次验收使用新的独立数据库。

开发过程记录保留在被忽略的 `experiments/output/`：

- live_01：沙箱网络导致准备阶段 Embedding 连接失败。
- live_02 / live_03：A 分诊把双边比较拆成单方条目，被程序拒绝；澄清每条比较的双域引用要求，未放宽校验。
- live_04：A/B 通过；C 实际调用了全部五类工具并触发搜索上限，但最终引用重排原文，被逐字校验拒绝。
- live_05：A/B/C 全部 PASS；人工复核后移除最终生成中的搜索摘要，并增加同源标识，防止重复来源被当作独立印证。
- live_06：最终代码 A/B/C 全部 PASS，完整报告为 `experiments/output/intelligence_v01_live_06.json`。

| Case | 实际结果 | 真实调用与检查 |
| --- | --- | --- |
| A Triage | research | Own/Competitor RAG、confirmed Memory、Reasoning 单次判断；自身比较明确属于 Fixture |
| B Verification | event | 2 次搜索、2 次页面读取；第三次搜索被预算拦截后成功 Finalization，引用 research_evidence_001 |
| C Deep Research | 完整报告 | Memory + 两域 RAG + 2 次搜索 + 3 次页面读取；第三次搜索被拦截后成功 Finalization |

Case C 引用 `event_evidence_001` 与 `research_evidence_002`，程序逐项核对原文并补齐来源。该次模型保守地把自身测试 Fixture 与“真实自身产品”区分，返回 `insufficient_own_product_context`、空比较列表并解释缺口；不能把这次结果声称为已完成真实产品能力比较。A 的 Fixture 比较与控制测试验证了有双域直接证据时的支持路径。

研究前后 Knowledge 均为 2 文档 / 5 chunks，Memory 均为 1 条 active confirmed；未将研究输出自动写库。事件原始对象及其未知生效时间保持不变。

复现入口：`python -X utf8 experiments/intelligence_e2e.py`。依赖本地已有真实事件归档 `experiments/output/event_core_v01_live_02.json` 和既有模型/搜索配置；新克隆仓库需自行提供通过验收的事件归档，不能伪造替代。脚本的失败记录只保留受控边界原因或异常类型。

## 16. 运行指标

最终 live_06 实际配置：Reasoning 与 Research 均为 `deepseek / deepseek-flash`，但使用两个独立角色入口；Embedding 为 `aliyun_bailian / text-embedding-v4`，Rerank 为 `aliyun_bailian / qwen3-rerank`。

| Case | 对话模型调用（含最终生成） | 检索模型调用 | 实际工具尝试 | 报告 tokens（含检索） | 耗时 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A | 1 | 5 | 0 Web/Agent tools | 4,778 | 14.196 s |
| B | 5 | 0 | 4，另拒绝 1 | 24,575 | 22.299 s |
| C | 5 | 5 | 8，另拒绝 1 | 41,757 | 58.585 s |

Fixture 准备额外 3 次 Embedding / 256 tokens。全程 24 次模型请求、71,366 reported tokens、97.454 秒；缺失 usage 的调用数为 0。表中只统计最终运行，不把先前开发失败的费用或耗时混入最终指标，也不据此推断长期平均成本。

运行报告逐次记录 provider、configured/response model、token usage、latency；搜索/读取单独记录工具次数，Embedding/Rerank 单独归入 retrieval_runtime。一般情况下缺失用量保持未知，不虚构为 0。

## 17. 全量回归

`python -X utf8 -m unittest discover -s tests -q`：414/414 PASS，耗时 43.045 秒（352 基线 + 62 项新控制测试）。原 Memory、Knowledge、Event Core、Source Discovery 与 Stage 1 测试无退化。日志中的故障告警来自已有负向测试，最终结果为 OK。

## 18. 代码质量

新增模块按真实职责拆分，核心函数用中文说明输入、处理、输出和业务边界。没有新框架、泛化 Manager、多 Agent 或业务层供应商硬编码。失败路径不透传敏感异常，不偷偷换模型，不使用不真实向量或伪造来源继续运行。

`git diff --check` PASS。因新增文件尚未暂存，另对全部 14 个新文件执行 `git diff --no-index --check`，无空白错误；Git 仅提示未来可能把 LF 转为 CRLF。

`git diff --name-status HEAD` 和 `git diff --cached --name-only` 均为空，冻结 tracked 文件未变化。扫描 14 个交付文件：已配置凭据原值命中 0，常见高置信度凭据模式命中 0；只报告数量，不输出凭据。

## 19. 已知限制

- MVP 机械校验保证引用存在、逐字片段真实、域/来源资格正确；不能自动证明每个自然语言结论都被引用完整蕴含，也不能彻底防止提示注入。实际含义、数字可比性、历史时间及推论仍需人工审阅。
- 真实 E2E 已用于 Prompt 调整，只证明真实工具链和边界机制，不能作为独立、无污染的质量评测。后续须用未参与开发的新 Gold Cases。
- 最终 Case C 对已召回的自身 Fixture 采取保守不足判断，且对部分官方页面内容使用 inference 标签；来源资格由程序记录，但语义标签和比较意愿仍可能过于保守。不能用三例成功推断研究质量或覆盖率已经达标。
- 已保留路由、逐声明支持、核验结果、未解决问题、工具和模型用量，可用于后续 Routing Accuracy / Over-escalation / Under-escalation、Verification Accuracy / False Confirmation Rate / Pending Resolution Rate、Citation Correctness / Evidence Coverage / Unsupported Claim Rate / Own Product Comparison Correctness / Tool Efficiency。未创建完整 Gold Set 或评分平台。
- 程序只信任调用方提供的官方域身份和上游 Event/Knowledge 来源资格，不通过模型或搜索排名猜官方身份。MVP 对 verification 定论采取保守门槛，只有第三方材料时保留待核验或拒绝无支撑定论。
- 页面检索有长度与次数上限；有界研究不能保证穷尽事实。工具失败立即进入最终阶段，牺牲部分恢复能力以保持简单明确的停止机制；最终校验失败须调用方处理。
- 输入地址限制排除明显私网字面地址与认证 URL，但冻结 Stage 1 的 DNS、跳转与浏览器子资源行为保持原样。这不是网络隔离；当前用于可信调用方的公开网页研究，生产环境仍需网络出口策略，不能宣称具备完整 SSRF 防护。
- Own Product Knowledge 可能含内部 PRD、Roadmap、Pricing 等资料；相关原文可能经 Embedding/Rerank 及 Reasoning/Research 发送到配置的外部模型。生产需按数据分级、企业安全策略、供应商要求评估外发，考虑脱敏、企业协议、租户隔离或私有部署。本轮未实现这些能力。
- 本轮没有自动刷新 Knowledge、写入 Memory、持久化研究报告产品能力、Scheduler、Integration 或 UI。工具层 scope 过滤不等同于完整的用户鉴权。

## 20. Git（版本管理）状态

未暂存、未 Commit；不进入最终 Integration/UI。既有 `tmp/` 保持原样，真实验收输出和独立数据库留在被忽略的 `experiments/output/`，`.env` 未修改。

最终 `git status --short`：

```text
?? docs/intelligence_layer_v0.1.md
?? experiments/intelligence_e2e.py
?? llm/research.py
?? research/
?? tests/test_intelligence.py
?? tmp/
?? triage/
```

其中 `tmp/` 为本轮开始前已有，不属于交付；`research/` 与 `triage/` 的展开文件见第 3 节。没有暂存文件、没有 Commit，HEAD 保持 `ae4c556`。

Intelligence Layer V0.1 = READY FOR REVIEW

此状态表示实现与本轮控制/真实链路检查可供验收，不替代用户本人运行、解释核心代码和最终验收；未宣布项目正式 Done。
