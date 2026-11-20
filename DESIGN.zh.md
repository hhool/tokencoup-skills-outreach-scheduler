# outreach-scheduler 设计讨论稿

日期：2026-11-18 · 状态：讨论稿（no coding，待确认后进入架构决策）
本稿落盘于 outreach-scheduler/ 项目文件夹，后期独立于任何外部工具链运行。

---

## 0. 定位结论

4 条需求 = 一个「外联计划调度中心」。建议独立仓库 `outreach-scheduler`，职责四层：

1. **导入层**：外链库 → 外链池（单向导入，只进不出，不写回源）
2. **池**：SQLite 单文件，自动分类 + DR 权重
3. **计划层**：项目（域名）→ 自动匹配 → **人工确认一道闸** → 可配置发送计划
4. **执行层**：周期自动执行（wave1 表单直投 + wave2 邮件自动发）+ 每日邮件汇报

与现有系统的关系：
- **导入源 = WDC 收割数据及其他同类外链数据**（本系统不写回源）
- workbench targets.jsonl / prospector **不作为导入源**，也不再是本系统的上游；workbench 保留其自身评审/台账角色不变
- 旧邮件系统的 R-520（人工发送）惯例仅适用于该系统本身；本系统独立实施自动发送，但保留「失败/可疑转人工队列」逃生门

关键设计影响：WDC/同类数据是**原始竞品反链**（指向竞品的外部链接），不是现成的「可投递平台清单」。因此导入层必须包含**探测阶段**（probe）：对每条 URL 做 HTTP 探测，识别「是否可投递」——有提交表单/注册入口/联系邮箱的才升级为机会条目，其余沉淀为普通池条目。

---

## 1. 需求拍板结果（2026-11-18 用户确认，两轮）

| # | 问题 | 结论 |
|---|------|------|
| 1 | 外链库来源 | **WDC 数据 + 其他类似外链收割数据**。不使用 workbench targets.jsonl + prospector 导出 |
| 1a | 导入源设计 | **先定义抽象接口 + 统一输出数据模型**（§4b SourceAdapter/SourceRecord），新源以适配器接入，下游零改动 |
| 1b | WDC 代码复制 | 内部收割仓库的 WDC 处理逻辑 4 文件已原样 cp 到 `import/`（harvest_competitor_links.py、competitor_backlinks.py、export_wdc_fast.py、dl.sh + README） |
| 1b | 验证码识别 | **追加**。执行层需具备验证码识别能力（分级策略，见 §7），过不了的转人工队列 |
| 2 | DR 权重 | **加**。免费源可用即启用：Keywords Everywhere Open PageRank（免费 30k 域名/月，0-10 分）；Moz 免费层 50 rows/月需信用卡，仅作补充不依赖 |
| 3 | 执行边界 | **全部自动**（wave1 表单直投自动、wave2 邮件自动发），不保留「只出清单」模式 |
| 4 | 邮件汇报 | 自动发；**收件人默认空 = 不发送**，配置后启用 |
| 5 | 匹配后人工确认 | **需要**。自动匹配 → 待确认队列 → 人工勾选 → 进入已排计划 → 执行器只跑已确认项 |
| 6 | 文档语言 | 英文主文档 `.md` / 中文 `.zh.md` |
| 7 | 提交路径（第五轮） | **自己发现的 submit 路径要记录、后续复用**；路径变化及时校验更新（§4c） |
| 8 | 项目资产（第五轮） | **新建项目输入 = 域名。** 域名下站点 TDK、一级二级地址、博文文章、外联邮件内容按项目归集：无则探测，探测也无则通用模板兜底（§5a）。同日拍板：TDK 缺失保持标记不降级；文章兜底=站点摘要生成；页面地址仅人工输入（无 sitemap 候选）；路径巡检 30 天 |

---

## 2. 架构与数据流

```
导入源                          本仓库（独立运行）
┌──────────────────┐   ┌──────────────────────────────────────────────┐
│ WDC 收割数据      │──►│ ①导入+探测层 ──► ②外链池(SQLite)              │
│ 其他同类外链数据  │   │  去重→HTTP探测(入口/表单/验证码类型/邮箱)      │
│ (原始竞品反链)    │   │  →分类+DR                                    │
└──────────────────┘   └──────────────────────┬───────────────────────┘
                                    ┌─────────▼──────────┐
                                    │ ③计划层             │
   人工确认闸 ────────────────────►  │ 项目(域名)→自动匹配  │
   （待确认队列勾选）                │ →评分→待确认队列     │
                                    └─────────┬──────────┘
                                    ┌─────────▼──────────┐
                                    │ ④执行层             │
                                    │ cron 拉起 CLI：      │
                                    │ 取当日计划→自动执行   │
                                    │ (含验证码分级识别)    │
                                    │ →结果回写→SMTP 汇报  │
                                    └─────────────────────┘
```

要点：
- 池是本系统的唯一真相源；导入按「域名+页面 URL」去重，source 字段保留出处
- WDC/同类数据是原始竞品反链，**探测阶段决定「是否可投递」**：有提交入口/联系方式的升级为机会条目，其余沉淀为普通池条目
- 匹配与执行之间强制一道人工确认闸（拍板 #5）
- 执行器只消费「已确认且已排期」的计划项，绝不碰 pending 状态

---

## 3. 数据模型（SQLite 表草案）

```
pool        池条目
  id, source(wdc|similar:<name>), source_id, url, domain,
  anchor_text, target_domain(反链指向的竞品域),
  entry_url(探测出的提交/注册入口),
  has_submit_form, form_fields(JSON), captcha_type(none|recaptcha_v2|
    recaptcha_v3|turnstile|hcaptcha|image|email_code|unknown),
  contact_email, probe_state(unprobed|probed|unreachable),
  site_type(目录站|书签站|SaaS目录|启动平台|博客|论坛|…),
  submit_type(auto|register|free|listing|posting|guest_post|pitch|…),
  wave(1|2|3), category(gaming|parenting|tools|general|…),
  dr(0-10 Open PageRank), language, geo,
  status(active|dead|bot-blocked|rejected),
  first_seen, last_seen, raw(JSON 原始字段)

project     投放项目
  id, domain, tags(JSON), status, created_at

match       匹配结果
  id, project_id, pool_id, score, score_detail(JSON),
  state(pending|confirmed|rejected), confirmed_at

plan        排期与执行记录
  id, project_id, match_id, scheduled_date, channel(form|email),
  template_id, state(queued|sent|failed|blocked|skipped),
  captcha_solved(0|1), captcha_cost, executed_at, result_note

run_log     执行日志
  id, ts, action, target, result, note

probe_cache  提交路径缓存（记录的发现，可复用；§4c）
  id, domain, entry_url, form_signature(表单字段名 hash), captcha_type,
  contact_email, success_count, last_probed, last_verified,
  state(valid|changed|gone|blocked),
  history(JSON: [{ts, old_entry, new_entry, reason}])

project_asset  项目站点资产（§5a）
  id, project_id, kind(tdk|page|article|email_copy),
  source(provided|probed|template), content, url(可空), fetched_at
```

配置不走数据库，全部 YAML 文件（见 §6），改配置即改行为。

---

## 4. 自动分类流水线（导入时执行）

**第一步：HTTP 探测（probe）**——WDC/同类数据是原始反链，必须先探测才能投递：
- 抓取页面，识别提交/注册入口（submit.php/add-link/register 等模式 + 表单 DOM 特征）
- 提取表单字段、**验证码类型**（recaptcha_v2/v3、turnstile、hcaptcha、图形码、邮箱验证码、无）、联系邮箱
- 无入口的条目标 unprobed→沉淀，不进投递匹配；探测结果缓存，避免重复抓

**第二步：三层分类**，全部自动：

1. **site_type**：按 submit_type + DOM/URL 特征归目录站/书签站/SaaS 目录/启动平台/博客/论坛
2. **wave 派生**：按主 submit_type 判定；wave 字段显式指定优先于派生
3. **category**：关键词规则表（gaming/parenting/tools/…），先规则后人工修正，规则表放配置文件可扩充

DR 补齐：
- Open PageRank 批量查询（按域名去重、本地缓存、永不重复查）
- **已知坑**：免费钥绑定单一出口 IP，换网络会 403 address_error（claude-seo 已处理过该错误识别），需在账户页绑定本机出口 IP，每周可改一次
- DR 进匹配打分权重（§5），也用于池内分层统计

存活判定沿用现有口径：403/412/429 = Cloudflare 反爬，不是死链，保留并标 bot-blocked，不误杀。

---

## 4b. 导入源抽象接口与统一数据模型（拍板：先定接口，后接源）

导入层采用**适配器模式**：每个数据源实现一个 SourceAdapter，全部输出统一 SourceRecord；探测/分类/DR 只面向统一模型，与具体源解耦。

### 统一输出模型：SourceRecord

| 字段 | 类型 | 说明 |
|------|------|------|
| `url` | str，必填 | 收割到的页面/站点 URL |
| `domain` | str | 派生（url → pay-level-domain），适配器不填由管线归一 |
| `source` | 溯源对象 | `{kind, file, row_no}`——可追溯到源文件与行 |
| `evidence` | 证据对象 | `{via_competitors: [域名…], inbound: n, corpus: "wdc-2012-08"}`——WDC 图的证据先验；其他源有对应证据就填，没有为空 |
| `hints` | 提示对象 | `{category, language}` 可空——源自带的分类/语言提示，仅作管线输入不作结论 |
| `raw` | JSON | 原始行透传，永不丢失 |

### 抽象接口：SourceAdapter

```
kind: str                                 源类型标识（"wdc" / "similar:<name>"）
discover() -> [SourceFile]                枚举该源当前可用的数据文件（含 mtime/大小，支持增量）
parse(file) -> Iterator[SourceRecord]     逐条解析为统一模型（惰性，大文件不整载）
validate(rec) -> [Issue]                  记录级校验：URL 合法、必填字段、明显脏数据
```

不变式（适配器纪律）：
1. **只读、无副作用**：不探测、不写库、不改任何文件
2. 探测/分类/DR 全部在管线下游统一执行——**新增导入源 = 写一个适配器 + 一段 YAML，下游零改动**
3. 适配器不 import 下游模块（单向依赖：adapter → 统一模型）
4. 大文件惰性逐条产出（WDC 产物单文件可数万行）

### 内置适配器

| kind | 输入 | 状态 |
|------|------|------|
| `wdc_competitor_links` | `competitor-links-<center>.json`（import/harvest_competitor_links.py 产物）；evidence 映射 via_competitors→evidence.via_competitors、inbound→evidence.inbound | 已定义，随基线代码就绪 |
| `wdc_discovery_snapshot` | `wdc-discovery-<version>.json`（v0.3 共链/出链发现层产物）；inlink_support→evidence.inbound、via_competitors 恒空、图谱字段进 hints/raw | 2026-11-20 已定义；仅发现层，仍需 probe |
| `similar_csv` / `similar_json` | 其他同类收割数据的通用映射适配器（列名/字段映射进 YAML） | 预留，待样例后定义映射 |

数据流：`SourceAdapter → SourceRecord → probe(探测) → classify(分类) → DR 补齐 → PoolEntry(SQLite 池)`

---

## 4c. 提交路径缓存：记录、复用、保鲜（第五轮需求）

探测阶段发现的 submit 入口是**一等资产**，不是探测的临时输出——持久化到 `probe_cache` 并复用：

**记录**
- 每个发现的入口（URL + 表单签名 + 验证码类型 + 联系邮箱）在探测时即入 `probe_cache`
- `form_signature` = 表单字段名的 hash——字段映射本身就是可复用的部分
- 提交成功递增 `success_count`；被验证过的链路（入口 → 字段 → 验证码处理）才是复用的对象

**复用**
- 同域名再次匹配/投递（新项目、新一轮）→ 直接取缓存路径，跳过全量重探测（省时省请求配额）
- 同 CMS / 同模板站（`form_signature` 相同）→ 复用字段映射，只校验 URL

**保鲜（路径变化及时更新）**
1. 提交前轻校验：HEAD/GET 缓存的 `entry_url`；404/跳转 → 先重探测再提交
2. 因表单不匹配（字段缺失/改名）导致的提交失败 → 标记 `suspect`，立即重探测
3. 后台低频巡检（默认 30 天/次）保证长尾条目持续有效
4. 每次变更追加进 `history`（旧路径 → 新路径 → 原因 → 时间），不静默覆盖，全程可审计

---

## 5. 匹配与打分

```
score = w1×类别匹配(0/1)
      + w2×语言/地区匹配
      + w3×wave 优先(wave1=1, wave2=0.6, wave3=0)
      + w4×DR 归一化(dr/10)
      + w5×先验 = max(竞品证据, 人工 fit, 发现层图谱)
      - 排除项（已投递/已拒绝/排除域名列表 → 直接不入队）
```

发现层图谱先验（仅 `wdc_discovery` 行）上限 0.75，用 `max()` 合并、绝不累加（2026-11-20 新增）。

- 权重和阈值进 YAML，可按项目覆盖
- **新鲜度加权（2026-11-18 拍板）**：新加入的 `page` 资产条目（如二期产品/游戏页）作为落地 URL 获得新鲜度加分，二期推广优先于整站条目；具体因子随 Phase 3 打分落地
- 产出按 score 降序进入该项目「待确认队列」，人工勾选确认后才生成排期
- 排期生成规则：按 daily_quota 从确认池依次填充未来日期

---

## 5a. 项目资产：TDK、页面、文章、邮件内容（第五轮需求）

新建项目只输入**一个域名**。系统随后归集四类资产，每类三级降级：
`provided`（用户给）→ `probed`（自动探测）→ `template`（通用模板）。

| 资产 | 用途 | provided | probed | 兜底 |
|------|------|----------|--------|------|
| **TDK**（title/description/keywords） | 目录表单 title/desc 字段、外联文案 | 用户填写 | 抓首页 meta + og: 标签 | 无——标记 `missing`，日报提醒人工补（TDK 无法通用化；拍板：不降级占位） |
| **一级/二级地址**（域名下具体路径） | 每次提交选择落地 URL | 用户列出（**仅人工输入**，拍板——无 sitemap 候选发现） | — | 仅用首页 |
| **博文文章** | guest_post / 投稿正文 | 站点文章列表 | sitemap/RSS 收割 | **站点摘要文章**：通用模板由站点自身摘要生成的文本填充（拍板：无人工输入） |
| **外联邮件内容** | wave2 邮件 | 项目专属模板 | — | **通用外联邮件模板** |

规则：
- 模板放 `templates/`，带占位符（`{domain}` `{title}` `{description}` `{url}` `{page}`）；渲染时注入项目自身资产——有真实资产的项目永远不会看到模板占位文本
- 探测到的资产（TDK/页面/文章）**缓存进 `project_asset` 并复用**——不随提交重抓，只在过期时重探
- 每条资产记录 `source`（provided/probed/template），日报可呈现「该项目有 N 项资产靠模板兜底」作为质量信号
- 一级/二级地址仅来自人工输入（拍板）——不做自动发现；缺失时提交一律落在首页

---

## 6. 配置文件样例（YAML，非代码）

```yaml
# global.yaml —— 全局
data_root: ~/outreach-scheduler-data   # 用户数据根目录：只允许以本项目命名的文件夹（不涉及其他私人文件夹）
sources:
  competitor_links_glob: "data/sources/competitor-links-*.json"   # 产物已复制进本项目（gitignored）
  similar: []          # 其他同类数据源，逐个列出（格式/字段待样例确认）
  exclude: [workbench_jsonl, prospector_store]   # 明确不用
scheduler:
  run_at: "09:30"
  timezone: Asia/Shanghai
report:
  email: ""            # 默认空=不发；配置后启用
  smtp_env_file: config/secrets/smtp.env   # 项目内路径，gitignored
dr:
  provider: openpagerank
  cache_days: 30
captcha:
  channel: cua         # 默认 CUA 免费通道（本机 Chrome）
  solver_api: ""       # 可选兜底，默认空=纯免费模式
  api_key_env: SOLVER_API_KEY
  monthly_budget_usd: 0    # 默认 0 = 不启用付费
quotas:                  # 护栏，沿用 R-522/R-530 口径
  per_domain_per_day: 100
  per_project_per_day: 10

# projects/<project-domain>.yaml —— 每项目一份（放 data_root/projects/ 下，gitignored——真实域名属用户数据，不入仓库）
domain: <project-domain>
tags: [gaming, browser-games, english]
daily_quota: 10
channels: [form, email]
templates: [dir-submit, outreach-mail]
sender:                # 可选：按项目发件身份（多客户）
  name: ""             # 不填 -> 共享运营邮箱（全局 smtp_env_file）
  email: ""
  smtp_env_file: ""    # 不填 -> 全局 report.smtp_env_file
report:
  email: ""            # 按项目的客户报告收件人；不填 -> 全局
scoring:
  min_score: 0.55
  weights: {category: 0.35, geo: 0.15, wave: 0.20, dr: 0.15, via_competitors: 0.15}
captcha:
  auto_types: [none, recaptcha_v3, turnstile, recaptcha_v2, image, email_code]
exclusions: []
start_date: 2026-11-20
```

---

## 7. 执行层

- **调度方式**：系统 cron 拉起 CLI（`scheduler run --date today`），无常驻进程、无外部工具链依赖——这是「独立运行」的关键选择
- **wave1 表单直投**：HTTP 表单通道优先（requests + 表单规则）；需要真实浏览器的站点属 CUA 类，依赖本机 Chrome，**第一阶段不自动**，进人工队列（独立性 vs 自动化的取舍：HTTP 优先）
- **wave2 邮件自动发**：SMTP 发送，复用 Gmail 应用密码（identity.py 的 env 模式），配额键=域名（R-530 口径）；支持按项目发件身份（项目 YAML 的 `sender` 块，client-intake §5）——不填回落共享运营邮箱
- **失败处理**：失败重试 ≤3 次（仅 5xx/网络类，4xx 不重试），重试耗尽转人工队列并在日报列出

### 7.1 验证码识别（追加需求，通道确认：CUA 免费优先）

探测阶段已标注每个入口的 captcha_type，执行时按类型分级处理。**免费通道优先，付费打码 API 仅为可选兜底**：

| 类型 | 策略 | 成本 |
|------|------|------|
| none | 直接提交 | 0 |
| recaptcha_v3 / turnstile / hcaptcha 隐形 | CUA（本机真实 Chrome）加载页面，真实指纹自然通过 | 0 |
| recaptcha_v2 / hcaptcha 图像挑战 | **CUA 通道**：真实浏览器内完成挑战识别与点击 | 0 |
| 图形验证码（自绘字符） | 先本地 OCR（免费）；失败走 CUA 通道 | 0 |
| email_code（邮箱验证码） | IMAP 自动取码回填（mail_verify.py 模式） | 0 |
| unknown / CUA 失败 | 转人工队列 | 0 |
| （可选兜底）第三方打码 API | 仅当 CUA 通道吞吐不足时启用，默认不配 | ~$1-3/千次 |

CUA 通道说明：
- 依赖本机 Chrome + 浏览器自动化，是全系统**唯一的本机环境依赖**（用户已确认接受）
- 验证码成功的浏览器会话可复用于同一站点的表单提交（指纹一致，过审率高）
- 成功率/耗时进 run_log，日报呈现；连续失败的站点自动降级人工队列
- 付费打码 API 配置默认为空 = 完全免费模式运行

护栏：
1. CUA 通道并发=1（单机单浏览器），当日队列按站点串行，避免并发挑战触发风控
2. 验证码失败不重试（浪费配额），直接转人工
3. 若未来启用付费 API：月预算上限进配置，超限自动降级

### 7.2 全自动护栏（wave2 邮件直发的风险控制）
1. 每域名/每项目双层每日配额
2. 发送预热期（新模板前 N 天低量）
3. 退信/拒收域名自动进排除列表
4. 所有已发内容进 run_log 可审计

---

## 8. 每日邮件汇报（可配置）

内容五段：
1. 今日计划（条目数、分项目、分通道）
2. 昨日完成 / 失败明细
3. 待人工确认队列（新匹配数量，附 top10 预览）
4. 池变化（新增、分类变化、DR 更新、死链）
5. 待人工处理项（转人工队列、需拍板事项）

收件人空 = 跳过发送，仅落盘 run_log。发送时间随 scheduler 配置。

---

## 9. 独立性与数据隔离

### 9.1 无外部工具链依赖
- 纯 Python3 标准库 + PyYAML；HTTP 用 requests（唯一第三方，可降级为 urllib）
- 不依赖任何外部 agent 工具链、MCP、现有系统代码——只读外部**产物文件**（competitor-links-*.json 等）
- 凭据只从环境变量/env 文件读，不进代码库

### 9.2 用户数据与仓库代码隔离（硬规则）
- **GitHub 源码零私有信息**：仓库只含源码、文档、YAML 结构样例（占位值）
- **用户数据以「新建工程导入」方式进入**：`import` CLI 把外部数据文件（WDC 产物、收割 JSON/CSV）读入工程私有数据目录 `data/`（SQLite + 原始文件），`data/`、`config/secrets*`、`*.env` 全部进 .gitignore，永不上传
- 数据目录可整体迁出仓库（配置 `data_root` 指向以本项目命名的仓库外路径，如 `~/outreach-scheduler-data/`，不涉及其他私人文件夹），实现代码与数据彻底分离——换机器/重装只带配置与数据目录
- 排查清单（提交前自动检查）：private-scan 工具扫描待提交文件中的邮箱/密码/域名台账特征，命中即拒绝提交
- WDC 原始图数据（~3GB）不入库不入 git，由 dl.sh 按需下载到本地缓存

### 9.3 唯一本机依赖
- CUA 验证码通道依赖本机 Chrome + 浏览器自动化（用户拍板：CUA 免费）；除此之外无其他环境依赖，其余流程（导入/探测/匹配/计划/邮件）纯 HTTP + SQLite
- CUA 仅用于验证码与需要真实指纹的提交，普通 HTTP 表单不启浏览器

---

## 10. 待拍板 / 后续步骤

0. ~~第五轮拍板~~ **全部确认（2026-11-18）：**
   - TDK 探测失败 → 标记 `missing` + 日报提醒（不降级占位）
   - 通用文章模板 = 站点摘要生成的文本填充，无人工输入
   - 一级二级地址 = 仅人工输入（无 sitemap 候选）
   - 提交路径保鲜巡检 = 30 天

1. ~~WDC 数据的确切输入~~ **已确认（2026-11-18）**：
   - 输入产物 = 内部收割仓库的 `competitor-links-<center>.json`（路径不入库）（现有 4 份产物文件已复制进项目，文件名按内容哈希中性化，映射表仅存于本地 data/sources/index.json）
   - 处理代码已原样复制到 `outreach-scheduler/import/`（harvest_competitor_links.py、competitor_backlinks.py、export_wdc_fast.py、dl.sh + README 链路说明），无私有信息
   - 原始图数据 ~3GB 本地缓存不入 git
   - 「其他类似数据」仍待提供样例（JSON/CSV 各一份）
2. **打码服务商**：默认不启用（CUA 免费通道优先）；付费 API 是否完全放弃，待 CUA 实测吞吐后再定
3. 仓库命名确认：`outreach-scheduler`（备选 backlink-outreach-ops / bl-campaign-pilot）
4. git init + GitHub 建仓（hhool/outreach-scheduler）：**提交前先落 .gitignore（data/、*.env、config/secrets*）+ private-scan 钩子**（§9.2 硬规则）
5. Phase 1 架构决策清单：目录结构、建表 SQL、competitor-links→pool 字段映射表（via_competitors/inbound 保留为打分先验）、探测规则清单
6. wave2 邮件全自动的模板与预热策略需要一版细则
7. 首个项目建议用产物最全的现成站点（本地映射表可查），验证「导入→探测→匹配→确认→执行→汇报」全链路


> **路径规范（2026-11-18 用户规则）**：仓库内容中的路径一律指向本项目文件夹或以本项目命名的文件夹
> （如 `~/outreach-scheduler-data/`），不得出现其他私人文件夹（内部工具链/凭据目录等）。
