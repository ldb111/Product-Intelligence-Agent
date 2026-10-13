# PIA 评测资产与使用边界

本目录保存可版本管理的评测数据基础。不同资产的证据强度必须明确区分，不能把软件测试或开发验收包装成独立模型质量证明。

## 资产分类

### A. 历史回归金标

历史回归金标（Legacy Regression Gold）是在开发过程中已经使用过的标准答案。它们适合防止既有能力回退，但不适合作为独立评测（Independent Evaluation）或模型真实准确率证明。

当前正式资产：

- [来源发现回归数据](gold/source_discovery_legacy_regression_v0.2.jsonl)：4条。
- [采集阶段回归数据](gold/stage1_legacy_regression_v0.2.jsonl)：8条。
- [原始金标文档](docs/PIA_Gold_Set_Legacy_v0.2.md)：上述数据的人工文档归档。

这些记录保留以下用途标记：

```text
evaluation_use = regression_only
```

### B. 事件识别数据与用途变化

事件识别（Event Detection）的历史基线、开发回归与最终留出须分别解释：

| 数据 | 当前用途与完成状态 | 依据 |
|---|---|---|
| 原30条与补充5条 | 2026-09-27完成首次独立基线；随后参与开发，当前仅作开发回归（`development_regression`） | [原30条](gold/event_detection_fresh_v0.1.jsonl)、[补充5条](gold/event_detection_fresh_supplement_v0.1.jsonl)、[用途变化](docs/event_detection_evaluation_lineage.md) |
| 最终新12条 | 发布候选（Release Candidate，RC）冻结后另行准备，2026-09-29一次性独立评测已完成 | [冻结规则](docs/event_detection_final_holdout_v1.md)、[冻结数据](gold/event_detection_final_holdout_v1.jsonl)、[已完成结果](docs/event_detection_evaluation_lineage.md#最终新留出2026-09-29-已完成) |

留出集（Holdout）用于验证冻结系统在未参与本项目开发的材料上的表现。最终12条按“系统冻结 → 空候选与规则冻结 → 金标准答案（Gold）冻结 → 正式执行”的顺序完成，每条只执行一次。旧35条文件中的原始留出标记，以及清单中的“未运行”说明，记录的是首次执行前的状态，不代表今天仍是未使用的新留出集。

最终12条决策为12/12、事件类型为4/7，仍有日期与归纳错误。样本非随机、由独立专家模型标注且未经真人复核，不能解释为整体准确率100%。[评测方法与失败案例](../docs/evaluation.md)

### C. 测试材料

测试材料（Test Fixture）用于验证程序边界、数据结构（Schema）、错误处理和确定性业务规则，不是独立金标准数据集。

### D. 受控端到端验收

受控端到端验收（Controlled E2E）用于确认调用链和持久化边界可以运行，不等于模型质量评测（Evaluation）。

## 当前边界

来源发现（Source Discovery）与采集阶段（Stage 1）有历史回归资产；事件识别有冻结数据、防答案泄漏适配、执行器及已完成的独立评测记录。核验（Verification）、事件分诊（Triage）、深入研究（Research）、记忆抽取（Memory Extraction）和事件内追问（Follow-up）的独立质量评测尚未建立。

## 历史回归执行器 V0.1

执行器（Runner）复用正式来源发现智能体（Agent）与采集入口，但把动态报告写入
`tmp/evals/<run_id>/`，不会写入正式页面历史。旧金标已参与提示词（Prompt）、规则或代码开发，
所以结果只用于发现既有能力回退，不得解释为独立准确率、泛化能力或模型质量证明。

先校验执行计划，不访问网页或模型：

```powershell
python -X utf8 -m evals.runners.legacy_regression --target all --dry-run
```

分别执行 Stage 1 或 Source Discovery：

```powershell
python -X utf8 -m evals.runners.legacy_regression --target stage1
python -X utf8 -m evals.runners.legacy_regression --target source_discovery
```

也可以用 `--case S1-01` 只运行一个案例，或用 `--output <目录>` 指定本次动态结果目录。
默认输出包含 `results.json` 和 `report.md`。

状态定义：

- `PASS`：本次自动检查未发现与旧 Gold 的差异，也没有待人工结构断言。
- `REVIEW`：发现内容差异、新来源、缺失来源或必须人工判断的结构断言。它不是失败；执行器
  无法自动区分真实网页变化和实现回归。
- `BLOCKED`：外部模型、认证、网络、浏览器或供应商服务阻止了本次运行。
- `ERROR`：生产返回结构或执行器内部契约出现非预期错误。

来源发现使用每个旧案例的第一个核心 `product_page` 作为唯一初始页面，不把其余
金标来源注入智能体。采集阶段 V0.1 只检查当前页面采集、内容锚点与内存快照（Snapshot）；旧资产未
提供 `previous_snapshot`、状态匹配（State Matching）、上下文差异（Contextual Diff）或交互状态上下文差异（Contextual State Diff）的答案，
执行器不会补造这些结论。报告中的每个比率均保留分子和分母，来源发现与采集阶段
分别汇总，不生成混合总分。
