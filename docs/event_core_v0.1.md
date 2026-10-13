# 事件核心 V0.1：实现与验收说明

> 历史记录：本文记录对应阶段的开发与验收状态，保留原阶段计划、验收数字、代码字段与输出，不代表当前产品状态、待办或 Git（版本管理）状态。当前产品能力见[实现事实索引](PIA_MVP_Implementation_Fact_Index.md)，设计取舍见[产品决策](product_decisions.md)。

<details>
<summary>历史记录阅读术语（代码字段与原始输出保留原样）</summary>

LLM（大语言模型）；Prompt（提示词）；Agent（智能体）；Research（研究）；Event Core（事件核心）；Event（竞争事件）；Evidence（证据）；Pending（待确认候选）；Verification（核验）；Source Discovery（来源发现）；Stage 1（采集与状态比较阶段）；Path A（前后状态比较路径）；Path B（官方明确声明路径）；Dedup（去重）；Fixture（测试材料）；Commit（代码提交）；API（程序接口）；HTTP（超文本传输协议）；Runtime（运行时）；Fast（快速模型角色）；Reasoning（推理模型角色）；Done（完成）。

</details>

Event Core 是固定 Workflow + LLM Semantic Decision。它接收 Stage 1 的局部观察证据，
返回事件候选及可追踪 Evidence，再做事件语义去重。没有 Agent、MCP、Research、数据库
或动态模型路由。Stage 1 和 Source Discovery 的源码、Prompt、规则均保持冻结版本。

## 运行入口

```python
from event_core import detect_event, deduplicate_event
from event_core.inputs import build_state_comparison_input, build_explicit_statement_input

# history 为正式 Stage 1 Snapshot History，包含前后快照和 is_first_scan。
input_data = build_state_comparison_input(
    history, competitor="竞品名称", source_type="pricing",
    source_credibility="official",  # 由调用方依据已确认来源填写，不能凭域名猜测。
)
result = detect_event(input_data)
for candidate in result.candidates:
    if candidate.decision != "no_event":
        dedup = deduplicate_event(candidate, existing_events=[])
```

Path B 使用 `build_explicit_statement_input(snapshot, excerpt, ...)`：excerpt 必须逐字存在
于快照正文，可提供同一正文中的有限 `relevant_source_context`。首次普通扫描属于 Path A
基线，返回 no_event；官方明确变化声明可独立进入 Path B。

输入需要包含带时区的观察时间。页面级差异复用 `detect_change → build_diff_result`；
交互状态通过 `state_key` 复用 `match_interactive_states → build_contextual_state_diff`。
仅把差异两侧值、相邻上下文及状态路径交给模型，不发送两份整页正文。超过输入上限时
明确拒绝；调用方可用 `diff_indexes` 选择业务范围。该选择不是 Event Atomicity：一组局部
证据仍可产生多个独立业务动作，相关子能力也可放在同一个事件中。

## 程序和模型分工

程序负责来源、观察时间、Candidate ID、比较资格、字段枚举、原文引用校验、Evidence
状态以及合并。模型负责变化的竞争意义、事件类型、业务动作拆分和事件同一性。

`prompts.py` 分别存放 Detection 与 Dedup 指令。模型没有工具，不能搜索补证据。所有
输入原文视为数据，不执行其中指令。Provider、Key、环境变量和客户端实现不进入 Prompt。

模型对每个非空事实字段提供 `citations`。引用只能指向已有 previous_state、current_state
或 relevant_source_context；不能引用 observed_at。状态值、事件/生效时间、适用范围
必须逐字摘取。event_type、event_subject、change_action、item 可以做有证据的语义归纳。
原文引用不属于输入或字段缺少支持时抛出 EvidenceValidationError，不静默纠正成事件。

Path A 的 event 必须有两侧状态与对应观察时间，单侧 current_only/previous_only、
unresolved/missing_unconfirmed 不证明产品新增或消失。Path B 的历史可来自官方原文明说的
历史，而不能由当前状态反推。宣布、Beta/Preview 与 GA 的区别由 Prompt 约束并由模型判断；
模型改造 current_state 为原文不存在的 GA 文本会被逐字校验拒绝。

## 数据结构与待确认候选

- 输入：竞品、来源身份、可信度、采集证据状态、路径、状态上下文、前后局部原文、差异、
  两侧观察时间和可选补充原文。
- 决策：event/no_event/uncertain，固定八种 event_type，自然语言 change_action，
  change_items、event_time、effective_time、affected_scope、理由及逐字段 citations。
- 候选：程序附加 competitor、candidate_id、detection_path、Evidence。
- no_event/uncertain：所有事件事实字段为 null、change_items/citations 为空；uncertain
  必须有 uncertainty_reason。这样 Pending 不会伪装成已确认 Event。
- 时间没有直接证据就保持 null，不用 observed_at 或发布时间推算。时间值保留原文，
  本版不做日期归一化或相对日期解析。

Evidence 的 source_credibility 与 evidence_capability 分开。官方身份不证明全量可用、
历史权限或其他未陈述字段。capability 是该引用可支持的字段清单；evidence_context 保存
原文、字段映射、状态路径及原始 claim_snapshot，便于多个来源合并后追踪。

confirmed 表示该条有限证据通过程序引用校验，不是完整事实核验认证；uncertain 对应
pending，输入冲突保留 conflict。后续 Evidence Verification/Research 在本轮范围之外。

## 去重

`content_fingerprint` 只识别完全相同内容，用于 ID 和 Evidence 精确去重。
`deduplicate_event` 比较实际业务变化，返回 SAME_EVENT/NEW_EVENT/UNCERTAIN。
不同来源可以是同一事件；同主题后续出现新的状态迁移可以是新事件；只补时间、范围或
证据而没有新迁移则仍可为 SAME_EVENT。无法安全判断或有多个候选匹配时不自动合并。

只有唯一明确 SAME_EVENT 才附加 Evidence。已有事件事实不被自动覆盖，补充的日期、范围
留在新增 Evidence 的 claim_snapshot 中。函数返回新对象，不修改调用方已有集合。
no_event 不进入去重；uncertain 返回 pending_candidate，等待后续核验。

## 静态模型角色

公共 `llm/` 使用已安装 ChatOpenAI 的 OpenAI-compatible 调用方式。Default/Fast 可解析
已有 LLM_PROVIDER/LLM_BASE_URL/LLM_MODEL/LLM_API_KEY；冻结的 Source Discovery 仍保留
原入口，没有为本轮做重构。

Reasoning 使用 LLM_REASONING_PROVIDER/LLM_REASONING_BASE_URL/LLM_REASONING_MODEL。
密钥优先读取 LLM_REASONING_API_KEY，否则按供应商标识读取对应的大写 *_API_KEY。
例如当前配置需要 DEEPSEEK_API_KEY。业务代码只请求 reasoning，不硬编码品牌、模型或地址。
缺配置、调用失败或结构化输出非法都明确失败，无 Fast fallback，也没有隐藏重试循环。

审计只记录 role、provider、配置 model、服务返回 model、latency 与 API 提供的 token usage。
密钥不进入 repr、Prompt、错误消息或报告；不保存原始模型响应和隐藏推理。

## 验证与实际限制

本轮验收使用 `.venv/Scripts/python.exe`：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_event_core -v
.\.venv\Scripts\python.exe -m unittest tests.test_llm_roles -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff --check
```

最小真实调用脚本：

```powershell
.\.venv\Scripts\python.exe experiments/event_core_e2e.py
```

A 为受控本地 HTTP 前后页面 → 真实 Stage 1 → 真实模型，标为半真实 Fixture；B 读取
一个已知官方 Changelog，C 复用同次页面采集。可用 `--official-snapshot` 明确选择历史
真实网页回放；这与实时互联网采集有不同 kind 标记。脚本不执行 Source Discovery。

2026-09-17 的实际成功记录保存在 `experiments/output/event_core_v01_live_02.json`（按现有
规则忽略的本地验收产物）：A capability/event，B plan_packaging/event，C no_event；A/B
重放均 SAME_EVENT。配置与实际返回均为 deepseek / deepseek-flash。Reasoning 是角色名称，
不是要求模型标识含有 reasoner；未覆盖用户环境配置。

这次成功运行共 5 次模型调用，14,793 tokens；各调用延迟与输入/输出 Token 均保存在报告。
此前第一次运行在 EvidenceValidationError 处安全失败，未创建事件；因当时没有失败输出
审计，无法确认具体字段和该次 Token。已补充后续失败的业务决策与 runtime 审计，未改变
判定规则。首轮失败记录仍保留在 `experiments/output/event_core_v01_live.json`。

真实限制：

1. 字符串引用校验能证明出处，不能完整证明语义蕴含、事件类型或官方身份；这些分别依赖
   模型判断与调用方确认。摘要字段仍可能被模型错误解释，不能宣称已消除幻觉。
2. 真实模型可能产出无法通过引用校验的输出，本版明确失败，不自动修正或换模型。
3. A 是人工控制页面，不证明真实互联网历史回溯；真实多来源语义去重、Announcement→GA
   等复杂语义边界只有 Mock 控制逻辑覆盖，本轮没有做大规模语义质量评测。
4. 不补采、自动搜索或自动消解 conflict；输入过大需要调用方选择上下文。时间只保留
   原文，Evidence 合并不自动填回已有事件的空事实字段。
5. SAME_EVENT 按调用方给定集合逐对比较，无数据库、召回索引或长期存储。

本轮交付状态应结合最终回归结果评为 READY FOR REVIEW；不是正式 Done 或新的冻结声明。
后续仍需要项目本人实际运行、验收并理解核心代码。本轮不创建 Git Commit。
