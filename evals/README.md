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

未来使用未参与 Prompt、Rule 或 Code 开发的新案例建立，用于独立质量 Evaluation。当前尚未建立。

### C. Test Fixture

用于验证程序边界、Schema、错误处理和确定性业务规则。Test Fixture 不是 Gold Set。

### D. Controlled E2E

用于确认真实调用链和持久化边界可以运行。Controlled E2E 不等于模型质量 Evaluation。

## 当前边界

本目录当前只完成 Source Discovery 与 Stage 1 的 Legacy Regression 数据基础。Event Detection、Verification、Triage、Research、Memory Extraction 和 Follow-up Research 的独立 Evaluation 尚未建立。

