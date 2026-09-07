# Stage 1 Final Acceptance Record（阶段一最终验收记录）

> 验收日期：2026-09-07  
> Stage 1 Final Acceptance（阶段一最终验收）：**PASS**  
> Stage 1（阶段一）：**Frozen（已冻结）**

## 1. Stage 1 目标

Stage 1（阶段一）负责把公开网页转化为可追踪、可比较的历史页面快照与交互状态观察结果，为 Stage 2 Event Detection（阶段二事件检测）提供可信、结构化、可审计的输入。

本阶段解决的是网页内容能否被可靠采集、历史状态能否被保存、前后观察结果能否被安全比较。页面文字变化或交互状态变化仍只是观察证据，不在 Stage 1 直接解释为 Competition Event（竞争事件）。

## 2. 最终状态

- **Stage 1 Final Acceptance：PASS**
- **Stage 1：Frozen**
- 冻结含义：当前 Stage 1 范围已通过自动化回归和端到端验收，可作为 Stage 2 的输入底座；不代表系统能够无条件覆盖任意网站或已经具备 Stage 2 的业务判断能力。

## 3. 已实现能力

| 能力 | 已实现范围 |
| --- | --- |
| Static Acquisition（静态采集） | 使用 HTTP 请求读取公开网页，并处理 URL 校验、超时、异常状态码和请求错误。 |
| Browser Fallback / Enrichment（浏览器兜底 / 补充采集） | 静态采集失败、质量不可信或存在明确交互结构时，使用浏览器渲染补充采集。 |
| Content Quality Gate（内容质量门槛） | 通过确定性规则输出 PASS / WARNING / FAIL；只有可信结果可以进入后续历史链路。 |
| Structured Content（结构化内容） | 将网页保留为 heading、paragraph、list、table、code、group/card 等结构化内容块，并生成兼容的规范文本。 |
| Snapshot / Previous Snapshot（页面快照 / 上一快照） | 持久化页面内容、结构化内容、采集时间、内容哈希、抽取版本和采集方式，并按 URL 与时间查找最近的上一份快照。 |
| Page-level Change Detection（页面级变化检测） | 比较同一抽取版本快照的内容哈希，保留首次采集、未变化、已变化三态语义。 |
| Contextual Diff（带上下文差异） | 在页面发生变化时生成包含标题路径、邻近上下文、列表、表格等结构信息的差异。 |
| Safe Interactive State Discovery（安全交互状态发现） | 发现当前可见、未禁用、状态表达完整且恰好一个选中项的标准 ARIA Tab（无障碍标签页）组。 |
| Safe Click + Post-click Validation（安全点击与点击后校验） | 点击后校验选中状态、URL、页面/窗口数量和下载副作用，并支持恢复原始默认状态。 |
| Local Scope（局部范围） | 优先利用 ARIA 关系，必要时通过已验证切换前后的非控件内容变化，定位交互状态实际控制的最小可信业务区域。 |
| Interactive State Capture（交互状态采集） | 从 Local Scope 抽取当前状态的 blocks、语义 state identity（状态身份）、内容哈希和采集时间。 |
| Presentation Semantics Preservation（展示语义保留） | 保留显式 HTML 语义及浏览器 computed style（计算样式）中的 strikethrough（删除线）等中性展示事实。 |
| Top-level Traversal（顶层遍历） | 按 DOM 顺序遍历顶层安全 Tab Group（标签组），采集非默认状态并最终恢复默认状态。 |
| Nested Traversal（嵌套遍历） | 仅在父状态 Local Scope 内发现并遍历真实子标签组，按父到子顺序构建 state_path（状态路径），不做全局笛卡尔积。 |
| Traversal Bounds（遍历边界） | 对嵌套深度、单组可操作选项和单页非默认状态数量设置硬上限，并记录跳过或截断原因。 |
| Nested State Content Ownership（嵌套状态内容归属） | 子 Local Scope 已独立建模时，从父状态可比较内容中排除对应范围；无独立内容的父状态标记为导航节点。 |
| Interactive State Snapshot（交互状态快照） | 将 Interactive States（交互状态）及遍历完整性信息写入正式 Snapshot，并使用独立 Schema Version（结构版本）保护历史兼容性。 |
| State Matching（状态匹配） | V1 按完全相同的 state_key（状态键）匹配历史状态，并依据已保存的内容哈希分类。 |
| State-aware Change Detection（状态感知变化检测） | 在遍历完整性和可比较资格约束下，输出 baseline、unchanged、modified、current_only、previous_only、unresolved、missing_unconfirmed 等观察分类及 true / false / null 三态结果。 |
| Contextual State Diff（带上下文状态差异） | 仅对可比较且 modified 的同一状态生成详细差异，保留 state_path、卡片标题、表格、列表和 text_marks（文本标记）等上下文。 |

## 4. 核心数据与判断原则

- **Current Fact（当前事实）≠ Event（事件）**：当前页面存在某项内容，不代表该内容最近才发生。
- **First Observed（首次观察到）≠ Newly Launched（新上线）**：首次采集只建立 Baseline（基线），不能生成“最近新增”的产品结论。
- **Not Observed（未观察到）≠ Absent（不存在）**：本次没有采集到某个状态，不足以证明该状态不存在。
- **Page Change（页面变化）≠ Competition Event（竞争事件）**：网页文字变化是待解释的观察证据，不等同于具有竞争意义的事件。
- **Navigation State（导航状态）≠ Comparable State（可比较状态）**：只负责组织父子路径、没有独立业务内容的状态，不应参与内容变化比较。
- `comparison_eligible=false` 的状态完全排除在变化比较之外；字段缺省时按 `true` 兼容处理。
- `partial` / `aborted` 表示遍历覆盖不完整，此时不得把未采集状态判断为消失，只能标记为 `unresolved` 或 `missing_unconfirmed`。
- V1 的 `state_key` 只做完全相同的精确匹配，不进行模糊匹配或标题改名推断。
- 抽取版本或 Interactive State Schema Version（交互状态结构版本）不一致时跳过直接比较，避免技术升级制造虚假变化。

## 5. 最终验收证据

### 5.1 全量 Regression Test（回归测试）

- 最终回归结果：**168 / 168 PASS**。
- 覆盖静态与浏览器采集、质量门槛、结构化内容、快照、页面变化、交互发现与点击、局部范围、状态采集、嵌套遍历、状态匹配和状态差异等 Stage 1 链路。

### 5.2 阿里云真实复杂网页 E2E（端到端验收）

- 结论：**PASS**。
- 发现 2 个安全 Tab Group（标签组）。
- 采集 5 个 Interactive State（交互状态）。
- `page_restored=true`。
- `errors=[]`。
- 年付 Pro 中 `¥5600` 与 `¥5988` 均被保留，删除线语义正确保存在结构化数据中。
- 全模型包季状态中的 `27`、`135`、`675` 内容正确。
- 人工验收入口：[manual_v033_acceptance.py](../experiments/manual_v033_acceptance.py)。

### 5.3 Nested Traversal Fixture E2E（嵌套遍历夹具端到端验收）

- 结论：**PASS**。
- 父子 `state_path` 关系正确，Personal / Team 被识别为 `navigation_only`。
- 独立顶层 A / B 状态没有被错误组合进 Personal / Team 的父子路径。
- `page_restored=true`。
- `truncations=[]`。
- 验收入口：[manual_v033d2_nested_traversal.py](../experiments/manual_v033d2_nested_traversal.py)。
- 本地页面夹具：[v033d2_nested_traversal.html](../experiments/fixtures/v033d2_nested_traversal.html)。

### 5.4 三轮历史状态变化 E2E（端到端验收）

- 结论：**PASS**。
- Scan 1：建立 `baseline`，不生成最近变化结论。
- Scan 2：页面内容不变，状态结果为 `unchanged`。
- Scan 3：仅 `Personal → Yearly` 状态变为 `modified`，`Price 100` 变为 `Price 120`。
- Scan 3 的 `contextual_state_diff` 只包含 Yearly；Monthly、Reference 和不可比较的 Personal 导航节点均未生成详细差异。
- 验收入口：[manual_v034a_state_matching_e2e.py](../experiments/manual_v034a_state_matching_e2e.py)。
- 留存结果：[acceptance_summary.json](../experiments/output/v034a_state_matching_e2e/20260907_231207_301762/acceptance_summary.json)。

## 6. Known Limitations（已知限制）

- 当前只自动遍历符合安全规则的标准 ARIA Tab（无障碍标签页），不会主动操作无法可靠判断副作用的控件。
- 最大嵌套深度为 2。
- 单个 Tab Group 最多处理 6 个可见、未禁用选项。
- 单页面最多采集 12 个非默认新状态。
- `state_key` V1 只做精确匹配；语义标题改名可能形成新的状态身份。
- Card（卡片）对齐采用保守的标题 / 顺序策略，无法可靠对齐时不会强行推断。
- 遍历为 `partial` / `aborted` 时不会确认历史状态已经消失。
- 不保证覆盖 Shadow DOM（影子文档对象模型）、非标准复杂交互或任意网站。
- 当前目标是 **validated extraction + visible failure（经过验证的抽取 + 可见失败）**，而不是承诺任意网站永不失败。

## 7. Stage 1 明确未包含的能力

以下能力尚未实现，属于后续阶段：

- LLM Event Detection（大语言模型事件检测）
- Evidence Verification（证据核验）
- Event Deduplication（事件去重）
- Ranking / Triage（排序 / 分诊）
- Deep Research Agent（深入研究智能体）
- Evaluation（评测）

