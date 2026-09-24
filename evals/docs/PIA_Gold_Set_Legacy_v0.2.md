# 迁移说明

- 原始版本：v0.2
- 原核验日期：2026-09-16
- 原文件路径：`C:\Users\Administrator\.codex\.chatgpt-projects\g-p-6a9fb834a61c8191b80c395258aeb166\PIA_Gold_Set.md`
- 当前用途：Regression Only
- 本资产已被后续 Prompt、规则、代码开发及验收使用，不适合作为 Independent Evaluation。

---

# PIA Gold Set｜产品智能研究助手统一金标集

> **PIA = Product Intelligence Agent（产品智能研究助手）**
>
> **Gold Set（金标集）= 人工独立确定的标准答案。**
>
> 版本：v0.2　｜　核验日期：2026-09-16　｜　市场：中国大陆
>
> 当前范围：Source Discovery（来源发现）与 Stage 1（阶段一监控底座）

---

## 目录

1. [这份金标集怎么用](#use)
2. [统一对照规则](#rules)
3. [Source Discovery 来源发现金标](#source-discovery)
   - [B01｜CaSee 凯见](#b01)
   - [B02｜OctoReport 智报](#b02)
   - [B03｜Kimi](#b03)
   - [T01｜腾讯云智能体开发平台 ADP](#t01)
4. [Stage 1 监控底座金标](#stage1)
5. [当前证据缺口](#gaps)
6. [未来阶段占位](#future)

---

<a id="use"></a>
## 1. 这份金标集怎么用

```text
人工独立核验官方公开信息
            ↓
      Gold Set 标准答案

测试输入 → PIA 程序 → 实际输出
                         ↓
              与 Gold Set 逐项对照
```

本文件只回答两个问题：

1. 对固定竞品输入，Source Discovery 应该找到哪些官方来源？
2. 对固定页面，Stage 1 应该采到什么内容和结构？

> **证据边界**
>
> - 官方网页可能更新。发现页面已变化时，应先人工复核并升级金标，再评价程序。
> - 本版没有保存同一官网两个真实历史时点的完整页面快照，因此不提供人为构造的 Previous Snapshot（上一快照）或变化答案。
> - 程序运行表现与评测结论应写入独立的 Evaluation Report（评测报告），不回填本文件。

---

<a id="rules"></a>
## 2. 统一对照规则

### 2.1 Source Discovery 对照字段

| 程序字段 | 中文解释 | 人工对照内容 |
|---|---|---|
| `competitor_name` | 竞品名称 | 是否与 Case（案例）的测试输入和产品边界一致 |
| `official_domains` | 官方域名 | 是否属于目标产品官方站点；托管平台还要限制具体路径 |
| `sources[].url` | 来源地址 | 是否命中本文件的标准官方来源或允许的等价地址 |
| `sources[].title` | 页面标题 | 是否与真实页面标题一致 |
| `sources[].source_type` | 来源类型 | 是否符合页面真实用途 |
| `sources[].monitoring_reason` | 监控理由 | 是否说明该页面持续提供什么竞争情报价值 |
| `discovery_complete` | 来源发现是否充分 | 核心来源齐备且没有已知核心缺口时才可为 `true`（真） |
| `remaining_gaps` | 剩余缺口 | 未完成时是否明确写出缺少的来源类型或信息面 |

### 2.2 重要级别

| 级别 | 判断方式 |
|---|---|
| **核心来源** | 未发现通常构成明显 Coverage Gap（覆盖缺口） |
| **补充来源** | 有持续价值，但缺失不一定导致整个 Case 失败 |

### 2.3 `source_type` 来源类型

| 值 | 中文含义 | 典型内容 |
|---|---|---|
| `product_page` | 产品页 | 产品定位、能力、适用对象 |
| `pricing` | 定价页 | 套餐、价格、额度、计费规则 |
| `changelog` | 更新日志 | 持续追加的产品功能变化 |
| `release_notes` | 版本说明 | 围绕版本号组织的新增、变更和修复 |
| `docs` | 产品文档 | 当前能力、限制与使用说明 |
| `blog` | 官方博客 | 持续发布的官方文章 |
| `announcement` | 官方公告 | 重要发布、政策或服务变更公告 |
| `marketplace` | 应用市场 | 官方插件、集成或应用目录 |
| `model_catalog` | 模型目录 | 官方模型清单、能力和可用范围 |
| `status_page` | 服务状态页 | 可用性、故障与维护状态 |
| `other` | 其他 | 现有类型无法准确表达时使用，并在理由中说明具体用途 |

### 2.4 允许的合理差异

以下差异不应直接判错：

- 已核实的重定向前地址与最终地址；
- URL（网页地址）末尾 `/` 的差异；
- 不影响页面内容的跟踪参数；
- 页面标题中的官网后缀、空格或中英文标点差异；
- 同一内容的中文页与英文页只推荐其中一个；
- 新发现的官方来源经人工独立核验后，先升级金标再纳入评分。

以下不属于合理差异：

- 错产品、错地区或错版本；
- 把文档导航当成具体定价正文；
- 把第三方转载当作官方来源；
- 编造无法从页面正文支持的监控理由；
- 用多个等价入口重复增加覆盖数量。

### 2.5 明确不应推荐的来源

| 页面类型 | 处理原则 |
|---|---|
| 登录、注册、支付、订单、个人控制台 | 不推荐 |
| 联系销售、招聘、隐私政策、服务协议、备案页 | 不推荐 |
| 搜索结果、聚合站、第三方教程、转载、用户问答 | 不作为官方来源 |
| 一次性促销、单篇入门教程、单个版本详情 | 通常不作为持续监控来源 |
| 国际站价格或海外版文档 | 不能替代中国大陆产品规则 |
| 图片、脚本、下载文件和内部接口 | 本版不作为公开页面来源 |

---

<a id="source-discovery"></a>
## 3. Source Discovery（来源发现）金标

### 3.1 Case 分组

| 分组 | Case | 与 PIA 的关系 |
|---|---|---|
| **业务竞品样本** | B01 CaSee 凯见 | 企业竞争情报平台 |
| **业务竞品 / 替代方案样本** | B02 OctoReport 智报 | 内容聚合、竞品监测与定时报告 |
| **业务替代方案样本** | B03 Kimi | 通用研究、知识工作与深度研究 Agent（智能体） |
| **技术验证样本** | T01 腾讯云智能体开发平台 ADP | 只验证复杂官网、文档、定价和更新日志结构，不视为 PIA 直接竞品 |

业务样本和技术样本分别评价。技术样本不能补偿业务竞品的覆盖缺口。

---

<a id="b01"></a>
### B01｜CaSee 凯见

#### 测试输入与产品边界

| 字段 | 标准答案 |
|---|---|
| `competitor_name` | `CaSee 凯见` |
| `official_domains` | `casee.me`；`www.casee.me` 视为同一官方域名的主机别名 |
| 产品边界 | 面向企业的竞争情报、市场洞察和业务决策平台 |
| 不包含 | 第三方竞争情报咨询公司、转载文章、微信公众号页面、需登录的工作台 |

#### 标准官方来源

| 编号 | URL 与页面标题 | `source_type` | 级别 | Monitoring Value（监控价值）与入选理由 |
|---|---|---|---|---|
| B01-S01 | [https://casee.me/](https://casee.me/)<br>CaSee凯见 - 智能竞争情报分析平台 | `product_page` | **核心** | 官方产品定位、目标团队、竞争情报能力，以及同页“凯见动态”中的产品与市场更新 |

#### 人工确认的页面事实

- 面向中大型企业的市场、销售、产品与战略团队；
- 主要能力包括竞争态势感知、竞争威胁分析、竞争策略评估和多源情报采集；
- 页面包含“凯见动态”；其中可确认“CaSee 2.0 竞争情报平台全新上线”，元数据为“新品发布 / 产品团队 / 2026-06-03”。

#### 不应推荐

- 试用申请、登录入口、公众号二维码、备案和政策页面；
- 单条动态的第三方转载；
- 只因包含“竞争情报”关键词而命中的无关网站。

#### 允许的合理差异

- `https://casee.me/` 与其已核实的 `www` 最终地址视为同一来源；
- 标题是否带空格不影响同页判断；
- “凯见动态”位于首页，不应把同一 URL 再重复登记为一个 `changelog` 来源。

#### 完成标准

```text
B01-S01 缺失 → 明显核心缺口

B01-S01 已覆盖，但未找到独立定价 / 独立更新日志 / 持续文档入口
→ discovery_complete = false
→ remaining_gaps 明确写出尚未确认的公开来源类型
```

---

<a id="b02"></a>
### B02｜OctoReport 智报

#### 测试输入与产品边界

| 字段 | 标准答案 |
|---|---|
| `competitor_name` | `OctoReport 智报` |
| `official_domains` | `z-report.cn`；`www.z-report.cn` 视为主机别名 |
| 产品边界 | AI（人工智能）情报流水线、内容聚合、竞品监测、报告编排与定时投递 |
| 不包含 | 页面中用于比较的 ChatGPT、传统舆情系统、n8n、Dify、Coze 等外部产品 |

#### 标准官方来源

| 编号 | URL 与页面标题 | `source_type` | 级别 | Monitoring Value（监控价值）与入选理由 |
|---|---|---|---|---|
| B02-S01 | [https://www.z-report.cn/](https://www.z-report.cn/)<br>智报 · AI 驱动的智能报告平台 | `product_page` | **核心** | 产品定位、采集器、报告编排、证据链、调度与投递能力 |
| B02-S02 | [https://www.z-report.cn/pricing](https://www.z-report.cn/pricing)<br>定价方案 · 智报 | `pricing` | **核心** | 套餐价格、额度、功能矩阵与年付规则 |
| B02-S03 | [https://www.z-report.cn/changelog](https://www.z-report.cn/changelog)<br>更新日志 - 智报 OctoReport · 智报 | `changelog` | **核心** | 按日期和主题持续记录产品、定价、集成与定位变化 |
| B02-S04 | [https://www.z-report.cn/changelog.xml](https://www.z-report.cn/changelog.xml)<br>OctoReport Changelog RSS | `other` | 补充 | RSS（简易信息聚合）订阅源；为更新日志提供更结构化的持续监控入口 |
| B02-S05 | [https://www.z-report.cn/docs/features/sources/overview](https://www.z-report.cn/docs/features/sources/overview)<br>数据源管理 - 总览 · 文档 · 智报 | `docs` | 补充 | 采集器类型、调度、去重与清洗等当前能力 |
| B02-S06 | [https://www.z-report.cn/compare](https://www.z-report.cn/compare)<br>对比选型 - 智报 OctoReport · 智报 | `other` | 补充 | 官方公开的竞争对象和差异化；理由中应明确“产品对比 / 替代方案页” |

#### 不应推荐

- `/login`、`/dashboard`、注册、订阅、付款和联系方式页面；
- 单个样例报告；
- 对比页中出现的外部产品官网。

#### 允许的合理差异

- RSS 页面标题可采用订阅源自身标题；
- B02-S03 与 B02-S04 是同一更新信息面的网页与订阅形式，不能重复算成两个核心来源；
- `other` 是当前类型枚举对“对比页”和 RSS 的合理承载方式。

#### 完成标准

```text
B02-S01 + B02-S02 + B02-S03 全部覆盖
→ 核心来源覆盖完整
→ discovery_complete = true
→ remaining_gaps = []

任一核心来源缺失
→ discovery_complete = false
→ remaining_gaps 写明缺失的信息面
```

---

<a id="b03"></a>
### B03｜Kimi

#### 测试输入与产品边界

| 字段 | 标准答案 |
|---|---|
| `competitor_name` | `Kimi` |
| `official_domains` | `kimi.com`；开放平台补充域名为 `platform.kimi.com` |
| 产品边界 | 面向中国市场的通用研究、内容生产、编程与知识工作 Agent 产品族 |
| 在 PIA 中的关系 | 通用研究与深度研究替代方案，不是狭义竞争情报软件 |

#### 标准官方来源

| 编号 | URL 与页面标题 | `source_type` | 级别 | Monitoring Value（监控价值）与入选理由 |
|---|---|---|---|---|
| B03-S01 | [https://www.kimi.com/products/](https://www.kimi.com/products/)<br>Kimi 产品中心：创作、编程、自动化，一站搞定 | `product_page` | **核心** | 产品族边界、各产品定位和入口 |
| B03-S02 | [https://www.kimi.com/help/membership/membership-pricing](https://www.kimi.com/help/membership/membership-pricing)<br>Kimi 会员价格 - Kimi 帮助中心 | `pricing` | **核心** | 会员档位、价格、共享额度与跨产品权益 |
| B03-S03 | [https://www.kimi.com/help/kimi-work/release-notes](https://www.kimi.com/help/kimi-work/release-notes)<br>Kimi Work 发布日志 - Kimi 帮助中心 | `release_notes` | **核心** | 按版本和日期持续记录 Kimi Work 的新增、变更与修复 |
| B03-S04 | [https://www.kimi.com/help/kimi-work/overview](https://www.kimi.com/help/kimi-work/overview)<br>Kimi Work 是什么？面向知识工作者的本地 Agent - Kimi 帮助中心 | `docs` | 补充 | 产品定位、核心能力、权限方式与系统要求 |
| B03-S05 | [https://www.kimi.com/products/kimi-webbridge](https://www.kimi.com/products/kimi-webbridge)<br>Kimi 浏览器扩展 - 让 AI Agent 使用浏览器 | `product_page` | 补充 | 浏览器自动化能力；页面明确说明其原名为 Kimi WebBridge |
| B03-S06 | [https://www.kimi.com/help](https://www.kimi.com/help)<br>帮助中心 - Kimi 帮助中心 | `docs` | 补充 | 稳定的产品文档目录，可发现深度研究、会员、Kimi Code 与 Kimi Work 等栏目 |

#### 不应推荐

- 登录、支付、用户对话、分享会话和下载文件；
- 同一帮助文章的语言重复页；
- 与产品无直接关系的公司新闻；
- 第三方 Kimi 教程和插件介绍。

#### 允许的合理差异

- 中文与英文帮助页内容等价时只计一个来源；
- `kimi.com` 是中国市场首选官方域名；`kimi.ai` 下属于 Kimi Help Center 的页面，如果与 `kimi.com` 标准页面的主题、产品对象和内容明确等价，可记为“官方等价入口 / 非标准主 URL”，并按实质等价关系判断是否命中对应 Gold Source，不能把 `kimi.ai` 的任意页面自动视为命中；
- `moonshot.ai` 是 Moonshot AI 公司 / 模型层入口；除非具体页面本身是 Kimi 产品持续状态的直接官方入口，否则不计入 Kimi Core Gold，也不因其属于官方域名就自动成为标准监控来源；
- `Kimi 浏览器扩展` 是当前名称；`Kimi WebBridge` 只作为已核实的原名，不应另建重复来源；
- 产品中心的外部开放平台链接可以作为补充发现，但不影响本 Case 的三个核心来源判定。

#### 完成标准

```text
B03-S01 + B03-S02 + B03-S03 全部覆盖
→ discovery_complete = true
→ remaining_gaps = []

任一核心来源缺失
→ 对应列出“产品族 / 会员计费 / 版本更新”缺口
```

---

<a id="t01"></a>
### T01｜腾讯云智能体开发平台 ADP

> **样本类型：技术验证样本。** 用于验证跨域名官网、文档、Pricing（定价）和 Changelog（更新日志）结构，不参与 PIA 业务竞品结论。

#### 测试输入与产品边界

| 字段 | 标准答案 |
|---|---|
| `competitor_name` | `腾讯云智能体开发平台 ADP` |
| `official_domains` | `adp.tencent.com`、`cloud.tencent.com` |
| 产品边界 | 中国站 ADP 的智能体应用构建、知识库、平台计费和产品动态 |
| 不包含 | 腾讯云全部产品、海外版、控制台登录和无关云服务动态 |

#### 标准官方来源

| 编号 | URL 与页面标题 | `source_type` | 级别 | Monitoring Value（监控价值）与入选理由 |
|---|---|---|---|---|
| T01-S01 | [https://adp.tencent.com/](https://adp.tencent.com/)<br>腾讯云 ADP - 企业级 AI Agent 智能体开发平台_腾讯云 | `product_page` | **核心** | 官方定位、构建能力、部署方式与集成能力 |
| T01-S02 | [https://cloud.tencent.com/document/product/1759/104191](https://cloud.tencent.com/document/product/1759/104191)<br>腾讯云智能体开发平台 产品动态_腾讯云 | `changelog` | **核心** | 按年月组织功能变化、发布时间与关联文档 |
| T01-S03 | [https://cloud.tencent.com/document/product/1759/127342](https://cloud.tencent.com/document/product/1759/127342)<br>腾讯云智能体开发平台 计费概述（套餐订阅）_腾讯云 | `pricing` | **核心** | 套餐价格、权益、增购规则与订阅条件 |
| T01-S04 | [https://adp.tencent.com/zh/document/216860253834891264](https://adp.tencent.com/zh/document/216860253834891264)<br>产品概述 | `docs` | 补充 | 应用模式与产品能力边界 |

#### 不应推荐

- 控制台登录、联系销售、腾讯云全产品动态和国际站价格；
- 产品目录中的每一篇操作教程；
- 尚未取得充分正文证据的营销页。

#### 允许的合理差异

- 腾讯云页面标题可带统一官网后缀；
- 位于文档站的计费正文仍应分类为 `pricing`；
- 同一文档的已核实重定向入口只计一次。

#### 完成标准

T01-S01、T01-S02、T01-S03 全部覆盖时，核心来源覆盖完整。该结论只用于技术能力验证。

---

<a id="stage1"></a>
## 4. Stage 1（阶段一监控底座）金标

### 4.1 对照方法

Stage 1 每个 Case 都按以下顺序检查：

```text
测试 URL
  ↓
采集方式与 Quality Gate（质量门槛）
  ↓
Structured Content（结构化内容）
  ↓
Interactive State（交互状态，如有）
  ↓
Snapshot（快照）
  ↓
仅在有真实上一快照时比较变化
```

| 对照对象 | 标准判断 |
|---|---|
| Static First（静态优先） | 静态页面已包含完整正文时，应优先使用静态结果 |
| Browser Fallback / Enrichment（浏览器回退 / 增强） | 静态结果不足或需要补充真实交互状态时，浏览器结果必须达到同一内容标准 |
| Quality Gate | 只有正文和结构足以支持人工核对时才应通过；通过不等于内容一定正确 |
| Structured Content | 标题、段落、列表、表格、卡片及嵌套关系不能被错误压平 |
| Safe Interactive State Traversal（安全交互状态遍历） | 只遍历具有可靠 ARIA Tab（无障碍标签页）语义的控件 |
| Local Scope（局部作用域） | 一个交互状态只拥有其局部可见内容，不混入全页或兄弟区域 |
| Parent / Child（父子结构） | 父状态与子状态不能重复拥有同一内容 |
| 删除线视觉语义 | 只有页面真实存在删除线时才保留标记，不自动解释商业含义 |
| Snapshot | 保存本次页面事实、结构与采集信息；时间和哈希值不预先写死 |
| Previous Snapshot（上一快照） | 只能使用真实保存的上一期快照 |
| State Matching（状态匹配） | 只有真实前后状态都存在时才对照 |
| Contextual Diff（带上下文差异） | 差异必须携带标题、表头、父项或卡片归属 |
| Contextual State Diff（带上下文状态差异） | 只对真实匹配且确认发生变化的交互状态生成 |

### 4.2 Stage 1 Case 总览

| 编号 | 页面 | 主要验证能力 |
|---|---|---|
| S1-01 | CaSee 首页 | 动态页面最终内容、轮播去重、安全遍历边界 |
| S1-02 | OctoReport 定价页 | 价格卡片、复杂表格、响应式重复控制 |
| S1-03 | OctoReport 更新日志 | 日期、主题、列表及 RSS 链接归属 |
| S1-04 | Kimi 产品中心 | 产品卡片、描述和入口链接归属 |
| S1-05 | Kimi 会员页 | 多列套餐表与权益关系 |
| S1-06 | Kimi Work 发布日志 | 版本、分类和嵌套列表 |
| S1-07 | ADP 套餐计费文档 | 文档内套餐价格表 |
| S1-08 | ADP 产品动态 | 年月、功能名称、描述与相关文档表格 |

---

### S1-01｜CaSee 首页

| 项目 | 标准答案 |
|---|---|
| 测试 URL | `https://casee.me/` |
| 页面标题 | `CaSee凯见 - 智能竞争情报分析平台` |
| 最终内容质量 | `PASS`（通过）；必须取得产品主体与“凯见动态”，只有页面外壳时不得通过 |
| 主要标题 | “我们擅长”“可信竞争情报全链支撑平台”“商业价值”“信源可信情报保障”“竞争态势感知分析”“竞争策略推理预判”“竞争情报采集汇聚”“专家竞争分析服务” |
| 动态内容锚点 | “CaSee 2.0 竞争情报平台全新上线”归属于“新品发布 / 产品团队 / 2026-06-03” |
| 结构标准 | 标题层级保留；每条动态的标题与类型、发布方、日期保持同一记录归属 |
| 去重标准 | 无限轮播产生的 DOM（文档对象模型）副本不得被当成不同动态 |
| 安全交互标准 | 横向轮播不是 ARIA Tab；不得因为可点击或会移动就创建标签页状态 |
| Snapshot 标准 | 保存最终 URL、标题、完整正文、结构块、采集方式和质量结论 |

**本 Case 暂不固定：** 静态响应与浏览器响应的分支选择。核验日没有保存可复核的原始静态响应，因此只评价最终内容是否达到上述标准。

---

### S1-02｜OctoReport 定价页

| 项目 | 标准答案 |
|---|---|
| 测试 URL | `https://www.z-report.cn/pricing` |
| 页面标题 | `定价方案 · 智报` |
| Static First | 官方初始页面内容已包含套餐和表格，默认应由静态采集取得完整正文 |
| Quality Gate | 关键套餐、价格和功能表均存在时为 `PASS` |
| 套餐卡片 | 免费版 `¥0/月`；专业版 `¥129/月`；企业版 `¥529/月` |
| 年付规则 | 年付按 10 个月计费 |
| 主表表头 | `功能｜免费版｜专业版｜企业版` |
| 关键额度 | 活跃数据源 `10｜100｜500`；月度积分 `2,000｜30,000｜120,000` |
| Structured Content | 三个套餐各自的名称、价格和权益不串位；功能矩阵保留表头、行名和列值 |
| 重复控制 | 桌面表格与响应式详情表达同一信息时，不得重复生成两套业务事实 |
| Safe Traversal | 普通套餐按钮不自动视为 ARIA Tab；只有当前 DOM 完整满足标准标签语义时才允许遍历 |

页面中的登录、订阅、购买、联系邮箱、导航和页脚不是价格变化的核心内容。

---

### S1-03｜OctoReport 更新日志

| 项目 | 标准答案 |
|---|---|
| 测试 URL | `https://www.z-report.cn/changelog` |
| 页面标题 | `更新日志 - 智报 OctoReport · 智报` |
| Static First | 官方初始页面内容包含完整日志，静态采集应足以取得正文 |
| Quality Gate | 日期、主题与更新列表完整时为 `PASS` |
| 页面结构 | `日期段落 → 二级主题标题 → 更新列表` |
| 日期组 | `2026-07-14`、`2026-07-10`、`2026-07-09`、`2026-06-20`、`2026-04-15` |
| 条目数 | 各组依次为 `4、5、4、2、2`，合计 `17` 条 |
| 关键归属 | `2026-07-10 → 自助订阅与证据链产品化 → 5 条更新` |
| RSS | `/changelog.xml` 是来源入口，不是更新事件正文 |
| Structured Content | 每个列表与最近的日期、主题保持归属；页脚法律链接不得混入更新列表 |

`2026-07-10` 下人工确认的 5 条更新：

1. 自助结账上线；
2. 结论级证据链开放；
3. 竞品监测向导；
4. 飞书增强；
5. 学术场景支持。

---

### S1-04｜Kimi 产品中心

| 项目 | 标准答案 |
|---|---|
| 测试 URL | `https://www.kimi.com/products/` |
| 页面标题 | `Kimi 产品中心：创作、编程、自动化，一站搞定` |
| Static First | 官方初始页面内容包含产品卡片，静态采集应取得主体 |
| Quality Gate | 五张产品卡片的名称和描述齐备时为 `PASS` |
| 产品卡片 | `Kimi`、`Kimi Work`、`Kimi Code`、`Kimi 浏览器扩展`、`Kimi Platform` |
| 入口归属 | Kimi → `/`；Kimi Work → `/products/kimi-work`；Kimi Code → `/code`；浏览器扩展 → `/products/kimi-webbridge`；Kimi Platform → `https://platform.kimi.com/` |
| Structured Content | 每张卡片的产品名、描述和入口链接保持同一归属，不把相邻卡片内容串联 |
| 当前命名 | 页面使用“Kimi 浏览器扩展”，并在详情页注明原名“Kimi WebBridge” |

导航、企业版入口、页脚产品列表和法律链接不应被识别成第二套产品卡片。

---

### S1-05｜Kimi 会员页

| 项目 | 标准答案 |
|---|---|
| 测试 URL | `https://www.kimi.com/help/membership/membership-overview` |
| 页面标题 | `Kimi 会员体系介绍 - Kimi 帮助中心` |
| 正文标题 | `会员订阅服务权益介绍` |
| Static First | 官方初始页面内容包含套餐表，静态采集应取得主体 |
| Quality Gate | 四档套餐表头、价格与主要权益关系完整时为 `PASS` |
| 套餐列 | Andante `¥49/月`；Moderato `¥99/月`；Allegretto `¥199/月`；Allegro `¥699/月` |
| 定位行 | 日常使用｜效率升级｜专业优选｜全能尊享 |
| 项目数 | `20｜20｜20｜100` |
| 定时任务 | `6｜10｜15｜20` |
| Structured Content | 所有行保留四个套餐列头；空值与不支持标记不得自动复制相邻值 |

同一组套餐在多个权益表中重复出现时，应保留各表独立标题和行关系，不能合并成无上下文的长值列表。

---

### S1-06｜Kimi Work 发布日志

| 项目 | 标准答案 |
|---|---|
| 测试 URL | `https://www.kimi.com/help/kimi-work/release-notes` |
| 页面标题 | `Kimi Work 发布日志 - Kimi 帮助中心` |
| Static First | 官方初始页面内容包含版本日志，静态采集应取得主体 |
| Quality Gate | 版本、日期、分类和条目层级完整时为 `PASS` |
| 当前顶部版本 | `3.2.9（2026-09-14）` |
| 分类 | `新增`、`变更`、`修复` |
| 3.2.9 条目结构 | 新增 4 个顶层条目，其中首项含 2 个子项；变更 2 条；修复 3 条 |
| Structured Content | 版本标题 → 分类 → 列表；嵌套子项不能被压平成独立顶层更新 |
| 噪声排除 | 帮助中心导航、语言选择、文章反馈、页脚和版本目录不属于发布内容 |

首个“新增”条目下的两个子项分别是“分享给 Kimi”和“用 Kimi 打开”，必须保持父子关系。

---

### S1-07｜ADP 套餐计费文档

| 项目 | 标准答案 |
|---|---|
| 测试 URL | `https://cloud.tencent.com/document/product/1759/127342` |
| 页面标题 | `腾讯云智能体开发平台 计费概述（套餐订阅）_腾讯云` |
| Quality Gate | 套餐名称、价格、单位和权益表存在时为 `PASS` |
| 套餐月价 | 免费版 `0`；Skill Plan 版 `88`；专业版 `188`；企业版 `4880`；单位为元/月 |
| Structured Content | 套餐列、价格、权益和计费说明保持表格关系 |
| 禁止推断 | 文档价格不能回填到未核实正文的其他营销页面 |

---

### S1-08｜ADP 产品动态

| 项目 | 标准答案 |
|---|---|
| 测试 URL | `https://cloud.tencent.com/document/product/1759/104191` |
| 页面标题 | `腾讯云智能体开发平台 产品动态_腾讯云` |
| Quality Gate | 年月、功能名称、描述、发布时间和相关文档关系完整时为 `PASS` |
| 结构 | 年月标题 → 更新表格 |
| 内容锚点 | `2026年09月` 下存在“新增记忆库功能”，发布时间为 `2026-09` |
| 表格关系 | 功能名称、功能描述、发布时间、相关文档不得错列 |
| 禁止推断 | `2026-09` 不扩写成某个具体日期；页面更新时间不等于功能发布时间 |

### 4.3 Snapshot 与差异对照

对 S1-01—S1-08 的首次基线采集，人工只固定以下内容：

| Snapshot 字段 | 标准答案 |
|---|---|
| `requested_url` | 对应 Case 的测试 URL |
| `final_url` | 实际最终官方地址；只接受已核实重定向 |
| `status_code` | 成功页面应为 HTTP（超文本传输协议）成功状态 |
| `title` | 对应 Case 的标准标题 |
| `content` / `blocks` | 包含本 Case 的全部内容锚点和结构关系 |
| `acquisition_method` | 按 Case 的 Static First / Browser Fallback 规则判断 |
| `quality_gate` | 满足本 Case 内容标准时为 `PASS` |
| `captured_at` | 实际采集时间，不预先固定 |
| `content_hash` | 由实际标准化内容计算，不预先固定 |

本版没有真实的上一期官网快照，因此统一为：

```text
Previous Snapshot：暂无标准答案
State Matching：暂无标准答案
Contextual Diff：暂无标准答案
Contextual State Diff：暂无标准答案
```

不得用同一时刻切换套餐、人工改写页面文字或测试夹具来冒充竞品真实历史变化。

---

<a id="gaps"></a>
## 5. 当前证据缺口

| 能力或来源 | 当前状态 | 后续补充条件 |
|---|---|---|
| CaSee 独立定价、独立更新日志、持续文档入口 | 未取得稳定、可核验的公开 URL | 找到官方正文后人工核验并升版 |
| Browser Fallback / Enrichment 的精确触发分支 | CaSee 最终渲染内容可核验，但未保存核验日原始静态响应 | 保存同一时刻静态响应与浏览器 DOM 后补充 |
| Safe Interactive State Traversal / ARIA Tab | 当前 Case 未确认到适合作为正例的稳定官方标签页 | 选定真实页面并逐状态人工核验 |
| Local Scope 与 Parent / Child | 没有稳定的真实父子交互页面金标 | 取得真实页面及完整状态归属证据后补充 |
| 删除线视觉语义 | 当前 Case 没有确认到稳定删除线业务状态 | 找到真实公开页面并核验可见样式后补充 |
| Previous Snapshot / State Matching | 没有同一页面两个真实历史时点的完整快照 | 从正式基线开始持续保存，下一次真实变化后补充 |
| Contextual Diff / Contextual State Diff | 没有可核验的真实变化对 | 只在真实变化发生并完成人工复核后补充 |

这些空缺不使用模拟答案填充，也不影响已有 Source Discovery 和当前页面采集金标的有效性。

---

<a id="future"></a>
## 6. 未来阶段占位

### Event Detection（事件检测）

> 待实现后再定义事件 / 非事件、事件类型、主体和变化动作标准答案。

### Evidence（证据）

> 待实现后再定义结论与原文证据、URL、标题和上下文的对应关系。

### Triage（分诊）

> 待实现后再定义重要性、紧急性、相关产品线与研究建议标准答案。

### Deep Research（深入研究）

> 待实现后再定义研究问题、来源范围、冲突证据处理和引用完整性标准答案。

---

> **维护原则：** 页面变化、来源新增或字段契约改变时，先由人工独立核验，再升级 Gold Set。程序输出不能反向决定标准答案。
