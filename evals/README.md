# PIA Evaluation Assets

本目录保存可版本管理的评测数据基础。不同资产的证据强度必须明确区分，不能把软件测试或开发验收包装成独立模型质量证明。

## 资产分类

### A. Legacy Regression Gold

开发过程中已经使用过的人工标准答案。它们适合防止既有能力回退，但不适合作为 Independent Evaluation（独立评测）或模型真实准确率证明。

当前正式资产：

- `gold/source_discovery_legacy_regression_v0.2.jsonl`
- `gold/stage1_legacy_regression_v0.2.jsonl`
- `docs/PIA_Gold_Set_Legacy_v0.2.md`：上述数据的原始人工文档归档

所有 Legacy Regression Gold 记录必须包含：

```text
evaluation_use = regression_only
```

### B. Fresh Holdout Gold

Event Detection 旧35条已完成首次独立 Baseline，随后参与开发，当前仅作 development_regression。最终新留出必须在 RC 之后采集，空候选和 Gold 分别冻结。详见 [评测历史](docs/event_detection_evaluation_lineage.md)。

### C. Test Fixture

用于验证程序边界、Schema、错误处理和确定性业务规则。Test Fixture 不是 Gold Set。

### D. Controlled E2E

用于确认真实调用链和持久化边界可以运行。Controlled E2E 不等于模型质量 Evaluation。

## 当前边界

Source Discovery 与 Stage 1 有 Legacy Regression；Event Detection 有正式数据、无泄漏适配、执行器和独立基线。Verification、Triage、Research、Memory Extraction、Follow-up 的独立质量评测尚未建立。

## Legacy Regression Runner V0.1

Runner 复用正式 Source Discovery Agent 与 Stage 1 页面采集入口，但把动态报告写入
`tmp/evals/<run_id>/`，不会写入正式页面历史。旧 Gold 已参与 Prompt、Rule 或 Code 开发，
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

也可以用 `--case S1-01` 只运行一个 Case，或用 `--output <目录>` 指定本次动态结果目录。
默认输出包含 `results.json` 和 `report.md`。

状态定义：

- `PASS`：本次自动检查未发现与旧 Gold 的差异，也没有待人工结构断言。
- `REVIEW`：发现内容差异、新来源、缺失来源或必须人工判断的结构断言。它不是失败；Runner
  无法自动区分真实网页变化和实现回归。
- `BLOCKED`：外部模型、认证、网络、浏览器或供应商服务阻止了本次运行。
- `ERROR`：生产返回结构或 Runner 内部契约出现非预期错误。

Source Discovery 使用每个旧 Case 的第一个核心 `product_page` 作为唯一 bootstrap，不把其余
Gold 来源注入 Agent。Stage 1 V0.1 只检查当前页面采集、内容锚点与内存 Snapshot；旧资产未
提供 `previous_snapshot`、State Matching、Contextual Diff 或 Contextual State Diff 的答案，
Runner 不会补造这些结论。报告中的每个比率均保留分子和分母，Source Discovery 与 Stage 1
分别汇总，不生成混合总分。
