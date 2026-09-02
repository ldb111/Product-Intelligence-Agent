# 2026-09-02 ～ 2026-09-06 求职冲刺 Sprint

## Sprint 目标

在 9 月 6 日结束前，形成一个接近完成、可运行、可演示、可评测、可用于简历和面试展示的 MVP（最小可行产品）。

**硬节点：** 2026-09-07（周一）开始正式投递 AI 产品经理、AI 应用产品经理和 Agent 产品经理岗位。

**长期方向：** 本 Sprint（迭代）只调整近期执行顺序，不推翻长期开发路线。完整演进方向见 [development-roadmap.md](./development-roadmap.md)。

**状态说明：**

- Todo（待做）：尚未开始真实实现；
- In Progress（进行中）：已经开始，但尚未满足全部 Done（完成）标准；
- Done（完成）：已经满足代码、测试、用户验收、问题记录和 Git Commit（代码提交）等全部标准。

---

## 9 月 2 日｜完成 Data Acquisition（数据采集）历史链路

### 目标

完成 Stage 1（阶段 1）剩余任务，跑通从真实固定来源到页面差异结果的完整历史链路。

### 已完成基础

- [x] Task 1：真实 Fixed Source（固定来源）网页读取 — Done（完成）
- 说明：实现指定 URL（网页地址）的真实网页请求、状态码检查、标题与页面文本提取，并输出结构化 JSON（结构化数据格式）。
- [x] Task 2：Content Normalization（内容标准化）— Done（完成）
- 说明：在 HTML（网页结构代码）转纯文本前清理确定性结构噪声并标准化空白，为后续页面快照和变化检测提供更稳定的内容。

### 当天任务

- [ ] Task 3：Snapshot（页面快照）— In Progress（完成）
  - 将标准化后的页面结果增加 captured_at（快照采集时间） 后保存为独立 JSON 快照，保留同一页面不同时间的历史记录。
- [ ] Task 4：Content Hash（内容哈希）— Todo（完成）
- 说明：对标准化后的 content 使用 SHA-256（安全哈希算法 256 位）生成稳定的 content_hash，用于后续快速判断两份页面快照的内容是否一致。
- [ ] Task 5：Previous Snapshot（上一份页面快照）— Todo（完成）
- 说明：基于相同 URL 和 captured_at（采集时间） 从历史快照中找到当前快照之前最近的一份，并正确识别 First Scan（首次采集）场景。
- [ ] Task 6：Change Detection（变化检测）— Todo（完成）
- 说明：比较 Previous Snapshot（上一份页面快照）与 Current Snapshot（当前页面快照）的 content_hash，识别页面内容是否变化，并对 First Scan（首次采集）返回未知状态。
- [ ] Task 7：Diff（差异比较）— Todo（待做）
- [ ] Stage 1 验收 — Todo（待做）

### 当天完成标准

真实网页能够跑通：

```text
Fixed Source（固定来源）
→ Web Reader（网页读取）
→ Content Normalization（内容标准化）
→ Snapshot（页面快照）
→ Content Hash（内容哈希）
→ Previous Snapshot（上一份快照）
→ Change Detection（变化检测）
→ Diff（差异比较）
```

---

## 9 月 3 日｜AI Event Detection（AI 事件识别）+ 第一轮 Evaluation（评测）

### 目标

让项目第一次进入真正的 AI 核心链路。

### 需要完成

- [ ] 接入真实 LLM（大语言模型）— Todo（待做）
- [ ] 实现 Competition Event Detection（竞争事件识别）最小版本 — Todo（待做）
- [ ] 使用 Structured Output（结构化输出）— Todo（待做）
- [ ] 建立第一版 Gold Set（金标集）— Todo（待做）
- [ ] 完成 Prompt V0.1（提示词第一版）— Todo（待做）
- [ ] 跑第一轮 Evaluation（评测）— Todo（待做）
- [ ] 记录第一批真实 Bad Case（失败案例）— Todo（待做）

### 当天必须留下的可展示成果

- [ ] Gold Set（金标集）
- [ ] Evaluation Run Result（评测运行结果）
- [ ] 第一版 Evaluation Report（评测报告）
- [ ] 第一批真实 Bad Case（失败案例）

---

## 9 月 4 日｜Model Selection（模型选型）+ Prompt Tuning（提示词调优）+ Competitor State（竞品状态）

### 目标

形成真实 AI 实验和状态变化能力。

### 需要完成

- [ ] 至少对比两个模型或两个明确可比较的模型配置 — Todo（待做）
- [ ] 使用相同 Gold Set 对比以下指标 — Todo（待做）
  - Precision（精确率）
  - Recall（召回率）
  - F1（综合指标）
  - Latency（延迟）
  - Cost（成本，如可获取）
- [ ] 基于第一轮 Bad Case 分析问题 — Todo（待做）
- [ ] 形成 Prompt V0.2 — Todo（待做）
- [ ] 再次运行 Evaluation — Todo（待做）
- [ ] 对比 Prompt V0.1 和 V0.2 — Todo（待做）
- [ ] 做出真实 Model Selection 决策并记录原因 — Todo（待做）
- [ ] 实现最小 Competitor State（竞品状态）能力 — Todo（待做）

```text
Previous State（历史状态）
→ Current State（当前状态）
→ State Transition（状态迁移）
→ Candidate Event（候选事件）
```

- [ ] 实现最小 Evidence（证据）结构 — Todo（待做）

### 当天必须留下的可展示成果

- [ ] 模型对比结果
- [ ] Prompt V0.1 / V0.2
- [ ] 指标变化
- [ ] Bad Case → 修正 → 新结果
- [ ] Model Selection 决策记录

---

## 9 月 5 日｜产品主链闭环

### 目标

让 AI 实验真正进入产品工作流。

### 需要完成

- [ ] Ranking（排序）最小版本 — Todo（待做）
- [ ] Multi-source Monitoring（多来源监控）演示所需的最小能力 — Todo（待做）
- [ ] Event-level Deep Research Agent（事件级深入研究智能体）最小闭环 — Todo（待做）
- [ ] Backend API（后端接口）支撑 Demo 所需的最小接口 — Todo（待做）
- [ ] 联通核心链路 — Todo（待做）

```text
真实网页
→ Content Normalization（内容标准化）
→ Snapshot（页面快照）
→ Change Detection（变化检测）
→ AI Event Detection（AI 事件识别）
→ Competitor State（竞品状态）
→ Competition Event（竞争事件）
→ Evidence（证据）
→ Ranking（排序）
→ Deep Research Agent（深入研究智能体）
```

### 明确不要求

- 复杂调度平台
- 大规模来源覆盖
- Multi-Agent（多智能体）系统
- 复杂权限
- 企业级部署

---

## 9 月 6 日｜Demo（演示）+ Project Proof（项目证明材料）+ 简历冻结

### 目标

形成周一可以直接开始投递的求职版本。

### 需要完成

- [ ] 最小 PC Web（电脑网页端）或等价的可操作 Demo — Todo（待做）
- [ ] 本地完整链路联调 — Todo（待做）
- [ ] Demo 截图或录屏 — Todo（待做）
- [ ] 更新 README（项目说明文档）— Todo（待做）
- [ ] 整理技术架构图 — Todo（待做）
- [ ] 整理 Evaluation（评测）结果 — Todo（待做）
- [ ] 整理 Model Selection（模型选型）记录 — Todo（待做）
- [ ] 整理 Prompt Tuning（提示词调优）过程 — Todo（待做）
- [ ] 整理真实 Bad Case（失败案例）— Todo（待做）
- [ ] 确认 Git（版本控制）提交历史完整 — Todo（待做）
- [ ] 推送 GitHub（代码托管平台）— Todo（待做）
- [ ] 冻结第一版简历项目描述 — Todo（待做）

### 周日晚的最小求职证据包

- [ ] 可运行 MVP
- [ ] GitHub 仓库
- [ ] Demo 截图或录屏
- [ ] 架构图
- [ ] Gold Set
- [ ] Evaluation Report
- [ ] 模型对比结果
- [ ] Prompt V0.1 → V0.2 迭代记录
- [ ] 真实 Bad Case
- [ ] README
- [ ] 连续 Git Commit（代码提交）历史

周日晚冻结第一版简历项目描述。9 月 7 日开始投递后，不因后续普通功能迭代反复修改简历。

---

## 本周明确后置

以下内容不允许阻塞 9 月 7 日投简历：

- 完整 Scheduled Monitoring（定时监控）
- 完整 Public Search（公开搜索补漏）
- Online Demo（在线公网演示）
- 登录和复杂权限
- 企业级多租户
- 大规模来源覆盖
- RAG（检索增强生成）完整实验
- Memory（记忆）完整实验
- MCP（模型上下文协议）完整实验
- Multi-Agent（多智能体）架构
- 复杂部署和运维体系

这些能力可以在 9 月 7 日之后继续迭代，但不要求反复修改简历。

---

## 本 Sprint 的最高优先级

1. 真实实现，不虚构。
2. AI 评测证据优先于外围功能数量。
3. Bad Case 必须来自真实模型失败，不人工编造。
4. Model Selection 必须有真实实验依据。
5. Prompt Tuning 必须能看到前后版本和指标变化。
6. 每个 Task 仍遵循原有 Done 标准：
   - 代码完成；
   - 自动化测试；
   - 用户亲自运行；
   - 验收通过；
   - 用户能解释核心逻辑；
   - 记录实际问题和限制；
   - Git Commit（代码提交）。
7. 本周每一天都必须产生可运行增量或 Evaluation 结果，避免只讨论不产出。
