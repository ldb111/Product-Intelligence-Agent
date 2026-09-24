# Event Detection Fresh V0.1 Contamination Manifest

## 目的

本清单记录候选 Case 的来源和污染核查结论。污染按 Case 具体材料判断：产品名称在
仓库中被一般性提及，不等于该版本、该原文或该事件参与过 Prompt、Rule、Fixture 或
Acceptance 开发。只有相同来源材料、相同事件事实或其答案被开发过程使用，才判定污染。

## 核查方法与结果

- 核查日期：2026-09-24
- 当前 tracked tree：逐条搜索完整 Source URL 和来源短摘录，命中 0。
- Git 历史：逐条以完整 Source URL 执行历史内容搜索，命中 0。
- 定向范围复核：`tests/`、`experiments/`、Event Prompt 与 Repair A/B/C 中没有这些
  具体版本来源或 Case 答案。
- 从候选集中移除的污染 Case：0。
- 所有保留记录：`evaluation_use=independent_holdout`、`contaminated=false`。

## Case 清单

表中的 Source 是每个 Case 的当前来源；Path A 的前一版本来源同时保存在 JSONL 的
`provenance.previous_source`，并已纳入同一轮当前树与 Git 历史污染检查。

| Case ID | Product | Path | Source | Source Date | tests/ | experiments/ | Prompt History | Conclusion |
|---|---|---|---|---|---|---|---|---|
| `EDF-A-001` | Dify | `state_comparison` | [官方来源](https://github.com/langgenius/dify/releases/tag/1.17.1) | 2026-09-10T10:17:09Z | NO | NO | NO | CLEAN |
| `EDF-A-002` | FastGPT | `state_comparison` | [官方来源](https://github.com/labring/FastGPT/releases/tag/v4.17.0) | 2026-09-11T10:11:32Z | NO | NO | NO | CLEAN |
| `EDF-A-003` | MaxKB | `state_comparison` | [官方来源](https://github.com/1Panel-dev/MaxKB/releases/tag/v2.10.6-lts) | 2026-09-03T07:43:16Z | NO | NO | NO | CLEAN |
| `EDF-A-004` | NocoBase | `state_comparison` | [官方来源](https://github.com/nocobase/nocobase/releases/tag/v2.2.17) | 2026-09-24T00:58:55Z | NO | NO | NO | CLEAN |
| `EDF-A-005` | APITable | `state_comparison` | [官方来源](https://github.com/apitable/apitable/releases/tag/v1.14.0-beta) | 2026-09-06T14:15:13Z | NO | NO | NO | CLEAN |
| `EDF-A-006` | AFFiNE | `state_comparison` | [官方来源](https://github.com/toeverything/AFFiNE/releases/tag/v2026.9.23-canary.909) | 2026-09-23T09:37:23Z | NO | NO | NO | CLEAN |
| `EDF-A-007` | Chat2DB | `state_comparison` | [官方来源](https://github.com/OtterMind/Chat2DB/releases/tag/v5.3.7) | 2026-09-22T09:50:50Z | NO | NO | NO | CLEAN |
| `EDF-A-008` | Sealos | `state_comparison` | [官方来源](https://github.com/labring/sealos/releases/tag/v5.1.2-rc6) | 2026-08-16T14:09:09Z | NO | NO | NO | CLEAN |
| `EDF-A-009` | Higress | `state_comparison` | [官方来源](https://github.com/higress-group/higress/releases/tag/v2.2.4) | 2026-08-14T00:42:07Z | NO | NO | NO | CLEAN |
| `EDF-A-010` | OceanBase | `state_comparison` | [官方来源](https://github.com/oceanbase/oceanbase/releases/tag/v4.4.2_CE_BP3) | 2026-09-15T07:35:36Z | NO | NO | NO | CLEAN |
| `EDF-A-011` | TiDB | `state_comparison` | [官方来源](https://github.com/pingcap/tidb/releases/tag/v8.5.8-20260924-54acfe3) | 2026-09-24T06:18:10Z | NO | NO | NO | CLEAN |
| `EDF-A-012` | Milvus | `state_comparison` | [官方来源](https://github.com/milvus-io/milvus/releases/tag/v3.0.2) | 2026-09-21T02:08:24Z | NO | NO | NO | CLEAN |
| `EDF-A-013` | PaddlePaddle | `state_comparison` | [官方来源](https://github.com/PaddlePaddle/Paddle/releases/tag/v3.4.0) | 2026-09-14T10:16:32Z | NO | NO | NO | CLEAN |
| `EDF-A-014` | MindSpore | `state_comparison` | [官方来源](https://github.com/mindspore-ai/mindspore/releases/tag/v2.3.0) | 2024-07-16T13:45:34Z | NO | NO | NO | CLEAN |
| `EDF-A-015` | Nacos | `state_comparison` | [官方来源](https://github.com/alibaba/nacos/releases/tag/3.3.0-RC) | 2026-09-21T13:11:20Z | NO | NO | NO | CLEAN |
| `EDF-A-016` | Apache APISIX | `state_comparison` | [官方来源](https://github.com/apache/apisix/releases/tag/3.18.0) | 2026-08-20T07:57:26Z | NO | NO | NO | CLEAN |
| `EDF-A-017` | Apache RocketMQ | `state_comparison` | [官方来源](https://github.com/apache/rocketmq/releases/tag/rocketmq-all-5.5.1) | 2026-08-20T07:35:53Z | NO | NO | NO | CLEAN |
| `EDF-A-018` | DataEase | `state_comparison` | [官方来源](https://github.com/dataease/dataease/releases/tag/v3.1.0) | 2026-09-24T01:14:34Z | NO | NO | NO | CLEAN |
| `EDF-B-001` | 1Panel | `explicit_statement` | [官方来源](https://github.com/1Panel-dev/1Panel/releases/tag/v2.3.2) | 2026-09-24T03:42:39Z | NO | NO | NO | CLEAN |
| `EDF-B-002` | JumpServer | `explicit_statement` | [官方来源](https://www.jumpserver.com/blog/case-study-tencent-music) | 2026-01-21 | NO | NO | NO | CLEAN |
| `EDF-B-003` | KubeSphere | `explicit_statement` | [官方来源](https://kubesphere.io/docs/v4.1/02-quickstart/) | undated; retrieved 2026-09-24 | NO | NO | NO | CLEAN |
| `EDF-B-004` | Element Plus | `explicit_statement` | [官方来源](https://element-plus.org/en-US/guide/quickstart) | undated; retrieved 2026-09-24 | NO | NO | NO | CLEAN |
| `EDF-B-005` | Ant Design | `explicit_statement` | [官方来源](https://ant.design/docs/spec/research-navigation/) | undated; retrieved 2026-09-24 | NO | NO | NO | CLEAN |
| `EDF-B-006` | Apache SeaTunnel | `explicit_statement` | [官方来源](https://github.com/apache/seatunnel/releases/tag/v3.0.0) | 2026-09-23T11:50:38Z | NO | NO | NO | CLEAN |
| `EDF-B-007` | Apache Dubbo | `explicit_statement` | [官方来源](https://github.com/apache/dubbo/releases/tag/dubbo-3.2.20) | 2026-07-19T06:09:51Z | NO | NO | NO | CLEAN |
| `EDF-B-008` | Sentinel | `explicit_statement` | [官方来源](https://github.com/alibaba/Sentinel/releases/tag/1.8.10) | 2026-05-21T08:11:42Z | NO | NO | NO | CLEAN |
| `EDF-B-009` | Cherry Markdown | `explicit_statement` | [官方来源](https://github.com/Tencent/cherry-markdown/releases/tag/cherry-markdown-vscode-plugin%400.3.2) | 2026-08-24T09:13:17Z | NO | NO | NO | CLEAN |
| `EDF-B-010` | ModelScope | `explicit_statement` | [官方来源](https://github.com/modelscope/modelscope/releases/tag/v1.40.1) | 2026-09-15T15:47:59Z | NO | NO | NO | CLEAN |
| `EDF-B-011` | MMEngine | `explicit_statement` | [官方来源](https://github.com/open-mmlab/mmengine/releases/tag/v0.11.0rc3) | 2026-07-13T07:30:05Z | NO | NO | NO | CLEAN |
| `EDF-B-012` | DeePMD-kit | `explicit_statement` | [官方来源](https://github.com/deepmodeling/deepmd-kit/releases/tag/v3.2.0) | 2026-08-22T16:02:20Z | NO | NO | NO | CLEAN |

## 独立性边界

候选收集和结构整理没有调用 Event Detector 或 LLM，也没有读取当前 Event Prompt
来推断答案。Manifest 的 CLEAN 只表示开发污染检查通过，不表示 Gold Label 已经完成。
人工标注前如发现来源无法复核、事实重复或曾用于开发，必须把 Case 移出 Fresh Holdout，
而不是把 `contaminated` 改回 false 继续使用。
