# Event Detection 评测用途与历史

## 不可变历史

Gold 在 `ab0a64ced811f7cd686c66c7e2943e0aa6f8e49c` 冻结。
Baseline #1 于 2026-09-27 首次调用生产 DeepSeek / deepseek-flash，35 次调用、31 SUCCESS、
4 EvidenceValidationError、0 BLOCKED。历史 Validated Decision Accuracy 19/31，Macro F1 51.62%。
原始文件保存在本地 `tmp/evals/event_detection/20260927T093830303930Z/`，不进入 Git。

## 从最终收尾开始的用途

这 35 条已经用于根因分析、Prompt 和评测开发，当前用途是 **development_regression**。
JSONL 内的 independent_holdout / contaminated=false 描述首次运行前的冻结历史，保留原样，
不能据此把后续回归称为独立评测。标签和所有历史输出均不改写。

使用 `python -m evals.runners.event_detection_closure` 执行开发回归。
使用 `--rescore <历史 results.json>` 离线重算，不调用模型，不覆写历史文件。
新的独立结论必须来自 RC 冻结后准备、先冻结候选再冻结 Gold 的新数据。

## 已证实的评测与标注边界

- 历史五条有日期的 Gold 中，仅 EDF-A-012 的完整日期出现在真实模型正文。
  其 September 20, 2026 与 Gold 2026-09-20 属于同一天，应用确定性日期标准化。
  四条仅来源元数据有日期的补充样本不进入时间抽取分母。
- EDF-A-002/007/017 的失败是 Path A 缺少具体旧状态；EDF-A-013 首先缺少 event_type 引用，
  且同样缺少旧状态。Validator 拒绝正确，不能拿当前“新增”声明反推过去没有该能力。
- 这些 Gold 把官方新增声明视为 Event，但冻结路径为严格 Path A；这是历史标注与路径契约
  的冲突。保留标签、不把样本改成 Path B、不放宽校验。回归应如实保留分歧。
- ERROR/BLOCKED 不进入 Validated Accuracy，但必须进入 End-to-End Correct Rate 的分母。
  完整校验前输出只能用于 Raw Semantic Diagnostic；旧错误仅保存一个失败候选，不能冒充完整输出。
- 多个独立 Event 的主类型与单标签 Gold 可能有粒度分歧；保留全部候选供复核，不能根据 Gold 挑选命中的候选。

## 收尾开发回归

2026-09-28 首先执行8条诊断子集，8/8 SUCCESS；随后完整35条执行一次。开发结果目录为
`tmp/evals/closure/20260928T073217911652Z/`，共35次调用，无重试，无 Gold 修改。

| 指标 | 历史独立 Baseline | 当前开发回归 |
|---|---:|---:|
| Execution Success | 31/35 | 34/35 |
| Validated Decision Accuracy | 19/31 | 24/34 |
| End-to-End Correct Rate | 19/35 | 24/35 |
| Macro F1 | 0.5162 | 0.6795 |
| Event Precision / Recall | 0.6000 / 0.9231 | 1.0000 / 0.5294 |
| Path A Accuracy | 6/14 | 9/18 |
| Path B Accuracy | 13/17 | 15/16 |
| Type Strict / Conditional | 10/13 / 10/12 | 8/17 / 8/9 |
| EvidenceValidationError | 4 | 0 |
| ERROR | 4 | 1 |

历史路径与类型原始细项以保存结果为准；所有执行失败仍进入端到端分母。
当前 ERROR 为 EDF-B-002 的 LLMStructuredOutputError，未针对错误重跑。开发集中 Path A
没有输出正式事件：旧样本大量以相邻 Release 条目代替同项前后状态，这与严格路径契约冲突。
不能把保守处理全部解释为模型进步，也不能把该开发分数当新的独立分数。

时间抽取按真正正文资格仅1条（EDF-A-012），当前0/1；EDS-005仍误取品牌升级描述中的
年份，计为不支持的时间填充。保留为字段风险，未为单样本添加关键字例外。
新增两条合成开发压力输入，明确同项调价与纯维护，真实检测2/2通过，证明 Path A
并未被程序或 Prompt 整体关闭。合成数据不进入任何独立指标。

完整原始语义输出只作 Diagnostic：当前有效覆盖34/35，一致24/34；旧失败仅保存单个
候选，不能恢复整组原始输出。所有输入先经过 allowlist 投影与 Leakage Guard。

## 软件与产品验收

后端665/665、前端47/47、TypeScript与生产构建通过。Repair A/B/C 与自身资料受控E2E通过；
真实浏览器新增一次事件追问，运行时刷新后报告和回答恢复，来源可核对。
软件通过率不是模型质量指标。历史已完成的真实E2E保留，不重复调用。

## 最终留出执行规则

RC 后才收集新的公开来源；先提交全部空候选与标注规则，再由专家模型完成 Gold。
`annotation_method=independent_expert_model_review`，不声称真人复核。最终使用
`event_detection_closure --dataset ... --rc-commit ... --candidate-commit ... --freeze-commit ...`。
运行器校验冻结顺序、输入不变、实现与 RC 一致，并为 Gold Commit 写独占运行凭条，
阻止第二次生产执行。一次性最终结果和冻结哈希将在执行后追加；不因普通错误调系统。
