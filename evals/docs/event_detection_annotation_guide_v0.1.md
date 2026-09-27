# Event Detection Fresh V0.1 Annotation Guide

## 1. 评测目标与范围

本评测只衡量 Event Detection Core 是否能依据给定证据，把输入判断为 `event`、
`no_event` 或 `uncertain`，并在确认事件时提取结构化事实。它不评估 Verification、Triage、
Research、Memory，也不评价网页采集或来源发现质量。

本数据集是 Fresh Holdout。Case 在收集时没有调用 Event Detector 或 LLM，没有依据当前
Prompt 自动写答案。人工标注者必须独立阅读输入与 provenance 后作答。

## 2. 两条评测路径

### Path A：State Comparison

输入同时给出真实的 `previous_state`、`current_state` 和比较范围。两份状态必须来自可追溯
的官方历史资料。标注者判断两者之间是否存在有竞争意义的产品状态变化。

不要把“版本号不同”直接视为事件。发布说明可能只有修复、文档调整、重复信息或页面模板
变化；这些仍需按业务事实判断。

### Path B：Explicit Change Statement

输入给出官方页面中的一段声明。它可能是发布说明，也可能是教程、客户案例、设计指南、
市场文章或预告。标注者判断这段声明是否明确证明新的产品状态变化。

Path B 没有可靠历史状态时，`previous_state` 保持 `null`。不得为了让变化描述更完整而补造
旧价格、旧能力、旧权限或旧定位。

## 3. 决策定义

### `event`

现有证据足以表明产品状态发生了具有竞争意义的变化。变化对象和变化动作可以从来源中
可靠提取；网页或文案发生变化本身并不满足条件。

### `no_event`

当前输入不构成新的 Competition Event。例如：文案润色、导航或顺序调整、重复宣传、
普通教程、客户案例、仅有修复性或非业务状态更新，且没有证据证明新的产品状态变化。

### `uncertain`

输入存在合理的变化迹象，但证据不足以可靠确认 `event` 或 `no_event`。常见原因包括历史
状态不完整、多个证据冲突、时间或影响范围不清、预告与正式上线无法区分，以及当前来源
只能支持局部事实。

不要用 `uncertain` 代替认真判断；也不要在证据不足时为了满足类别数量强行选择另一类。

## 4. 冻结业务语义

以下规则与当前 Event Core 一致，标注时必须逐条遵守：

- **Current Fact ≠ Event**：当前页面存在某能力，只能证明当前事实，不能自动证明发生过变化。
- **First Observed ≠ Newly Launched**：第一次看到某事实，不代表它刚刚上线。
- **Not Observed ≠ Absent**：旧材料没有写到某能力，不等于旧状态明确没有该能力。
- **Page Change ≠ Competition Event**：页面文字、布局、模板或顺序变化不等于产品状态变化。
- **Announcement ≠ Launch**：宣布、预告、测试或候补计划不自动等于正式可用。
- **Event Time ≠ Observation Time**：采集时间、发布时间和真正事件时间是不同概念。
- **Detail Enrichment ≠ New Event**：新页面补充既有事实细节，不自动构成新事件。

## 5. 推荐标注步骤

1. 先阅读 Case 输入，不看任何目标分布数字来猜标签。
2. 打开 provenance 中的官方原文，确认标题、日期、版本与短摘录属于同一来源。
3. 对 Path A 分别核对前后状态；对 Path B 只判断声明实际能证明的事实。
4. 应用第 4 节的冻结语义，先决定 `decision`。
5. 只有 `decision=event` 时，填写完整事件字段。
6. 证据不支持的时间、范围或旧状态保留 `null`，不要用常识补齐。
7. 在 `annotation_notes` 记录关键判断依据、缺口和冲突，不复制整页正文。
8. 由最终人工复核者确认决策；分歧未解决时保持 `pending_human_review`。

## 6. Event Type

`decision=event` 时，`event_type` 只能选择以下一个主类型：

| 类型 | 标注含义 |
|---|---|
| `capability` | 产品能力新增、移除或实质变化 |
| `availability` | 上线、开放、区域或平台可用性变化 |
| `pricing` | 明确价格、费率或计费数值变化 |
| `plan_packaging` | 套餐结构、权益组合或额度归属变化 |
| `access_permission` | 角色、权限、访问条件或开放范围变化 |
| `commercialization` | 免费转付费、商业版本或商业模式变化 |
| `deprecation` | 下线、废弃、停止支持或迁移要求 |
| `positioning` | 官方目标用户、核心场景或产品定位变化 |

一个 Case 涉及多个方面时，选择最能代表竞争意义的主类型，并在 `change_items` 记录其余
被证据支持的变化。不要为了多标签覆盖把同一事件拆成多个 Case。

## 7. 事件字段标注

### `event_subject`

写发生变化的产品、套餐、能力或服务对象。使用来源中的正式名称，不写推测对象。

### `change_action`

用简短动词短语表示变化，例如“新增”“正式开放”“调整价格”“停止支持”。不要只复制
“更新”“优化”这类无法说明业务变化的泛化词。

### `change_items`

每一项尽量表达：

```text
subject + previous_state + current_state
```

Path A 中，只有来源明确支持时才能写旧状态。Path B 如果只证明当前状态，可以写：

```json
{
  "subject": "某能力",
  "previous_state": null,
  "current_state": "官方声明中明确的新状态"
}
```

实际 V0.1 JSONL 的 `change_items` 使用与 Event Core 一致的对象结构：`item` 表示变化项，
`previous_state` 可以为 `null`，`current_state` 必须是来源明确支持的状态。

### `event_time` 与 `effective_time`

只有来源明确说明事件发生或生效时间时才填写。发布日期只能写入 provenance 的
`published_at`，除非原文明确证明它也是事件时间。不能用 `observed_at`、`retrieved_at` 或
版本采集时间替代 `event_time`。

### `affected_scope`

只写证据明确支持的用户、区域、版本、套餐或部署形态。无法确认时保留 `null`。

## 8. 不同决策的字段规则

| decision | event_type | event_subject / action | change_items | event_time |
|---|---|---|---|---|
| `event` | 必填且必须合法 | 必填 | 至少一项 | 可为 `null` |
| `no_event` | `null` | `null` | `[]` | `null` |
| `uncertain` | `null` | `null` | `[]` | `null` |

`uncertain` 不携带已确认 Event 字段；`annotation_notes` 必须明确缺失什么证据。这与当前
Event Core 的冻结输出语义保持一致，也避免把不完整字段当作半个事件结论。

## 9. Provenance 规则

每条来源包含真实 URL、标题、来源日期、必要短摘录和采集日期。短摘录只用于定位，标注者
仍需打开原文核对上下文。

- Path A 必须同时存在 `previous_source` 与 `current_source`。
- Path B 的 `previous_source` 应为 `null`，`current_source` 指向声明原文。
- 页面没有公开日期时，使用 `undated; retrieved YYYY-MM-DD`，不得猜测发布日期。
- 来源失效、内容与摘录不符或无法确认官方身份时，暂停标注并标记待处理。
- 不要把第三方转载、搜索摘要或模型概括当作 Gold Evidence。

## 10. Freshness 与污染处理

如果发现 Case 的具体来源、事件事实或标签曾用于 Event Prompt、Event Core、测试 Fixture、
E2E 或历史 Acceptance 开发，该 Case 已被污染，必须移出 Fresh Holdout。不要仅修改备注后
继续使用。

产品名称曾在仓库中出现不一定构成污染；判断对象是本 Case 的具体材料和事实。如果无法
确定，记录证据并交由复核者决定。

## 11. 类别分布目标

V0.1 的目标分布是：Path A 约 `8 event / 6 no_event / 4 uncertain`，Path B 约
`6 event / 4 no_event / 2 uncertain`。这是选材覆盖目标，不是标注配额。人工真实标签与目标
不一致时保留真实结果，不重新解释 Case，也不为了凑数改变标签。

## 12. 完成条件

Case 只有在以下条件全部满足后才能把 `annotation_status` 改为 `ready`：

- 来源可重新打开并与保存的 provenance 一致；
- 初标分析完成，适用字段只取自保存的官方证据；
- 最终人工复核者确认 decision 或解决分歧；
- `annotated_by` 与 `annotation_method` 如实记录标注流程；
- 本地数据校验通过。

在全部 Case 完成人工标注前，不实现或运行正式 Event Detection Evaluation Runner。

## 13. Annotation Method / 标注方式

原始 30 条 Candidate 在 Event Detector 运行前已冻结于
`35cc1ffb6d265d2c8a11829be209173a9d8bfd5b`。本轮 AI 协助第一轮语义分析，用户对最终
decision（以及指定的主 event_type）进行人工确认；事件字段只从 Candidate 已保存证据
整理，未知旧状态、时间和范围留空。被评测 Event Detector 没有参与 Gold 生成。

因此记录 `annotated_by=ai_assisted_human_review` 与
`annotation_method=ai_assisted_human_review`，不是纯人工标注，也不是模型自动 Gold。
Gold 不根据 Evaluation Output 反向修改。补充集的 `target_coverage_type` 只是选材元数据，
不能自动成为答案；5 条补充记录继续保持 `pending_human_review`、空 Gold 与空标注人。
