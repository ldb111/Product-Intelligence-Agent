# Event Detection Fresh Supplement V0.1 Manifest

## 目的与状态

为原始 30 条 Gold 尚未覆盖的五个类型各准备一条选材候选，独立保存在补充 JSONL 中。
`target_coverage_type` 仅用于 Sample Selection，不是 Gold event_type；不得据此自动标注。

- 创建与核查日期：2026-09-27
- 核查基线：`35cc1ffb6d265d2c8a11829be209173a9d8bfd5b`
- Candidate：5；Path A：0；Path B：5
- 中国产品：4；海外产品 Notion：1
- `annotation_status=ready`：5/5
- `annotated_by=ai_assisted_human_review`、`annotation_method=ai_assisted_human_review`：5/5
- 人工最终确认：`event` 5/5
- Gold Event Type：pricing、plan_packaging、access_permission、commercialization、positioning 各 1 条
- `evaluation_use=independent_holdout`、`contaminated=false`：5/5
- `created_before_model_evaluation=true`：5/5
- 未调用 Event Detector、Event Prompt、LLM Evaluation 或正式 Runner。

## Case 清单

| Case ID | Product | Target Coverage Type（选材） | Final Gold Event Type（人工确认） | Path | Source | Source Date | Freshness | Contamination |
|---|---|---|---|---|---|---|---|---|
| EDS-001 | DeepSeek API | pricing | pricing | explicit_statement | [官方来源](https://api-docs.deepseek.com/zh-cn/news/news0802/) | 2024-08-02 | 0 命中 | CLEAN |
| EDS-002 | Notion | plan_packaging | plan_packaging | explicit_statement | [官方来源](https://www.notion.com/en-gb/releases/2025-05-13) | 2025-05-13 | 0 命中 | CLEAN |
| EDS-003 | NocoBase | access_permission | access_permission | explicit_statement | [官方来源](https://www.nocobase.com/en/blog/2.2.0) | 2026-08-23 | 0 命中 | CLEAN |
| EDS-004 | MaxKB | commercialization | commercialization | explicit_statement | [官方来源](https://blog.fit2cloud.com/?p=6b2f8c92-74a8-4129-acc2-1c696ca5e63a) | 2024-08-01 | 0 命中 | CLEAN |
| EDS-005 | WPS 365 | positioning | positioning | explicit_statement | [官方来源](https://www.wps.cn/learning/question/detail/id/334018.html) | 2024-09-19 | 0 命中 | CLEAN |

## Freshness 核查方法

在写入补充文件前，搜索当前 tracked tree（git grep）、工作区 tests/、experiments/、
evals/ 与 Prompt 所在的 event_core/、source_discovery/、research/、memory/、triage/
模块（rg），并用 git log --all --no-textconv -S 搜索所有可达 Git 分支历史。
使用 --no-textconv 避免本机 DOCX 转换器缺失影响历史搜索，未修改 Git 配置。
搜索内容包括来源 URL 的稳定路径或文章 ID、关键原文及事件同义特征。完整来源地址
以这些唯一 URL 子串覆盖；元数据保留各 Case 搜索词与结果。

- EDS-001：`news0802`、`硬盘缓存`、`0.1元 每百万`、`context caching`；各搜索范围及历史均 0 命中。
- EDS-002：`2025-05-13`、`unlimited usage of Notion AI`、`all-in-one AI pricing`；各搜索范围及历史均 0 命中。
- EDS-003：`nocobase.com/en/blog/2.2.0`、`NocoBase-managed files`、`File access upgrade`、`file-record permission checks`、`文件访问升级`；各搜索范围及历史均 0 命中。
- EDS-004：`6b2f8c92-74a8-4129-acc2-1c696ca5e63a`、`MaxKB正式推出专业版`、`MaxKB专业版`、`MaxKB Professional`；各搜索范围及历史均 0 命中。
- EDS-005：`334018.html`、`2023年品牌全新升级`、`文档创作管理的一体化`、`WPS 365`、`WPS365`；各搜索范围及历史均 0 命中。

MaxKB 和 NocoBase 在原始 30 条中已有产品名称，但原集为近期 GitHub 修复摘要；
本补充材料分别是 v1.4.0 专业版推出及 2.2 文件访问机制升级，来源与事件均不同。
一般产品提及、通用“专业版”词汇或其他产品价格 Fixture 不作为本 Case 污染。
核查没有发现相同具体材料或事件用于开发；CLEAN 是仓库可见证据结论，
不保证不存在仓库外未记录的使用。后续如发现污染，应移出 Holdout，不能改备注掩盖。

## Path 与证据边界

本轮获取的是可定位的官方发布、升级或品牌回顾声明，没有取得可独立复核的历史
前后页面快照，因此全部使用 Path B。pricing、plan_packaging、access_permission
也没有虚构旧价、旧权益或旧权限，previous_state/current_state/contextual_diff 均为 null。
Fresh 指未参与本项目开发与模型评测，不要求公告发布于最近几天。

- EDS-001：历史公告价格，不代表当前价格；声明证据来自该公告，没有独立旧价格快照。
- EDS-002：官方发布说明中的套餐声明；没有采集独立旧套餐快照，不用当前动态定价页面补造旧权益。
- EDS-003：官方 2.2 文件访问机制升级声明；截取权限相关小节，不把原集 2.2.16/2.2.17 的修复摘要当作旧权限状态。
- EDS-004：官方厂商专业版推出声明；不自动推断价格、付费规则或社区版权益变化。
- EDS-005：官方说明回顾 2023 年品牌升级；2024-09-19 是文档日期，不是升级日期。需人工判断是否证明定位变化或仅补充既有定位。

EDS-001 来源日期由官方新闻导航中的 2024/08/02 核对；其他日期来自页面公开日期。
EDS-001 至 EDS-004 的冻结官方发布或公告日期写入 event_time；EDS-005 只回顾 2023 年
品牌升级，没有精确事件日期，因此 event_time 保持 null。采集时间没有用作事件时间。
短摘录只定位所选声明，人工复核须打开官方原文阅读上下文，并独立决定是否构成事件。

## 人工队列与后续边界

5 条已完成 AI-assisted annotation + human final review。`target_coverage_type` 原本只解释
为什么选择候选，并未自动成为 Gold；最终 decision 与 event_type 是后续人工复核确认结果。
被评测 Event Detector、Event Prompt、正式 LLM Evaluation 与 Runner 均未参与 Gold 生成。

动态标注卡仍位于 `tmp/evals/event_detection_supplement_annotation_queue.md`，作为未提交的
工作过程文件保留，不属于最终 Gold。最终 35 条独立留出集继续分为 Original 与 Supplement
两份 JSONL，以保留清晰 provenance；本轮不合并文件。
