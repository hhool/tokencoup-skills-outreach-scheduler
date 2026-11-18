# 客户端门户 — 注册登录与看板设计

**范围**：商业化轨道的客户端 Web 门面。客户用邮箱注册/登录（含
Google / Facebook OAuth），然后只看两样东西：**额度消耗**（套餐买了
什么、消耗了什么）和**推广效果**（服务产出了什么）。门户对调度器
写入的同一个数据库只读；不绕任何人工闸，也不暴露内部机制（池子、
模板、发件凭据）。

英文版：client-portal.md。

## 1. 身份与访问模型

| 项 | 决策 |
|------|----------|
| 注册 | 邮箱 + 验证信（复用 B3 的 IMAP 收信机制） |
| OAuth | Google、Facebook 两个登录按钮；OAuth 身份首次登录时绑定到同一客户记录 |
| 单一身份 | 一份合同一个客户记录；一个客户多查看者是后期功能 |
| 映射 | 客户记录 ↔ 一个或多个 `project`（对应 client-intake §2） |
| 角色 | `client`（只读自己的数据）；`ops`（内部，沿用现有 CLI 权限） |
| 托管 | 扩展 fly.io 应用（HTTP 侧）；门户 API 对 sqlite 只读 |

反目标：门户**不是**自助投递工具。客户永不触发投递；只读台账、对
人工闸已确认的队列项做批准动作。

## 1.5 自助建项目 + 试用额度（2026-11-18 定稿）

注册用户可以自建推广项目并获得试用额度——这是 2B 服务的获客漏斗。

### 流程

1. **建项目**——向导收集：目标域名、站点标题、站点摘要、联系邮箱
  （与调度器已接入的 §5a 资产 schema 相同）、市场/标签。每个已验证
   域名一个项目。
2. **验证归属**——DNS TXT 记录或站点 HTML meta 标签（与搜索控制台
   验证同一约定）。不验证 → 不激活、不给试用。这是防滥用锚点：试用
   按**已验证域名**限量一次，不按邮箱（否则一次性邮箱能刷试用）。
3. **发放试用**——激活后项目获得试用额度（默认 3 条实链，一次性，
   `quota_type=trial`）。消耗走同一个 View A 台账；始终没到 live 的
   试用链不计费、不占试用额度。
4. **转化路径**——试用额度用完或到期，View A 切到「选套餐」状态；
   购买的套餐转为 `quota_type=paid` 额度。历史试用台账保持可见，
   作为潜在客户评估时的证据。

### 写入路径纪律

- 门户**唯一**允许的直接写入：客户身份记录、建项目请求、归属验证
  标记、套餐购买记录。所有投递侧表（pool、match、plan、link_verify）
  仍归调度器所有。
- 新建项目进入正常管线：probe → match → **人工 confirm** → schedule
  → execute。自助化不改变「谁扣动投递扳机」。
- 试用项目打标，让 B5 运营视图和 R-522 节奏把它们与付费客户放在
  同一域名级配额下（不允许饿死付费产能的免费层）。

### 试用参数（默认值，配置可调）

| 参数 | 默认 | 理由 |
|-----------|---------|-----------|
| 试用额度 | 3 条实链 | 覆盖多个目录的一次样本，足以展示台账+效果两个视图 |
| 试用窗口 | 14 天 | 提早进入节奏沟通 |
| 每域名试用 | 1 次 | 已验证域名才是身份 |
| 每 OAuth 身份试用 | 1 次 | 防同一人多域名刷试用 |

## 2. 视图 A — 额度消耗

两层：**套餐额度**（商业层）与**投递产能**（运营透明层）。

### 2.1 套餐额度（商业层）

按已定稿定价裁决（business.md §Pricing），2B 服务按交付实链计价、
打包为月度额度。

- `quota_total` = 套餐大小（如 30 条实链/月）
- `quota_used` = 当前账期内确认 `live` 的链接数
- 消耗规则：只有 `live` 消耗额度。`submitted`/`sent` 是在途工作，
  永不计费；自动重投 + 人工兜底后仍未上线的链接**不计费**（服务
  保障）。
- 数据源：`link_verify`（verdict=live）连接 `plan`/`match` →
  project → client。

### 2.2 投递产能（运营层）

给客户看节奏的原因：配额按域名计、受平台规则约束，所以提交刻意
按天错峰（R-522/R-530）。

- 每域名地址预算：100 / 域名 / 24h——渲染成每个目标域名的产能
  仪表：「今日已用」vs「预算」。
- 客户节奏：滚动窗口内已排期 vs 已交付计数；SLA 行（项目
  `sla.min_daily`）加达标/落后徽章——复用 B5 `ops.py` 告警，过滤到
  查看者自己的项目。
- 波次构成：哪些项在等（wave1 一次投递 vs wave2 多次交互），让延迟
  可读成「节奏」而非「停滞」。

### 2.3 额度视图线框

```
┌ 额度 ─────────────────────────────────────────────┐
│ 套餐额度     ▓▓▓▓▓▓▓▓░░░░░░░  8 / 30 实链        │
│ 在途         已提交 12 · 待确认 3                 │
│ 不计费       重试 4 · 人工队列 2                  │
│ 节奏         0.8 条/天（需 1.0）  ⚠ 落后          │
│ 预估         套餐约 11-30 用尽                    │
└───────────────────────────────────────────────────┘
┌ 投递产能 ─────────────────────────────────────────┐
│ example-dir.com   ▓▓▓░░  今日 31/100 地址         │
│ 每日节奏          [近 14 天柱状]                   │
└───────────────────────────────────────────────────┘
```

## 3. 视图 B — 推广效果

四层，按证据强度排序。第 1–3 层随门户 v1 交付；第 4 层是后期阶段
（需要 UTM/GSC 集成）。

### 第 1 层 — 实链台账（硬证据）

每条实链一行：目标 URL、锚文本、落地域名、**DR**、确认日期、verify
判定史。这是开票级证据：每一行都是一条已消耗额度带凭证（link_verify
时间戳）。筛选：账期、域名、DR 区间。

### 第 2 层 — 落地质量

- 实链落地 DR 分布（分桶：<30 / 30–60 / 60+）
- 来源类型构成（目录 / 博客 / 列表 / 问答）——来自 pool 分类
- 来源验证码档位（none / v2 / v3）作为耐久性代理：对提交设防的来源
  对删链也设防。

### 第 3 层 — 漏斗趋势

周度序列：pending → confirmed → submitted/sent → live，加**上线率**
（live ÷ submitted）与**通过率**（approved ÷ attempted）。数据源：
run_log + link_verify 聚合——与 B4 profile 同一套聚合，以趋势图形式
投给客户。

### 第 4 层 — 下游流量（后期）

通过 UTM 标记链接与 GSC 数据看实链带来的引荐访问。门户 v1 不含：
需要动被服务站点的分析配置，单独集成。

## 4. 数据契约（门户只读，调度器所有）

| 门户元素 | 来源表 | 说明 |
|----------------|-----------------|-------|
| 已消耗额度 | link_verify ⋈ plan ⋈ match | verdict='live'，当前账期 |
| 在途 | plan（state submitted/sent）、match（pending） | 只读 |
| 产能仪表 | run_log 按域名按日 | 对齐 R-530 配额键 |
| 实链台账行 | link_verify + pool.dr | pool.dr 由 OPR 批量刷新（暂挂） |
| 漏斗/趋势 | run_log、link_verify | 与 B4/B5 同一聚合 |
| 报告存档 | client_report 邮件 | 同时镜像为门户页面 |

约束：门户 API 对卷上 sqlite **只读 SQL**。投递留在调度器路径
（cron + 人工闸）；门户进程与 cron 进程分离，sqlite WAL 模式允许
两者并存。

## 5. 构建顺序

1. ~~门户骨架上 fly.io~~ **2026-11-18 已交付**（`src/osched/portal.py`、
   `osched portal` CLI）：邮箱注册 + 验证信（复用 report SMTP 机制）、
   scrypt 密码、HttpOnly 会话 cookie、只读视图 A/B v0 看板；Google/
   Facebook 按钮先桩着（回调域名绑定后先接 Google）。
2. 按 §4 契约做视图 A/B v1（DR 分桶、漏斗趋势、报告存档页）。
3. ~~自助建项目 + 归属验证 + 试用额度（§1.5）~~ **2026-11-18 已交付**：
   meta 标签验证（DNS TXT 后续）、每已验证域名且每账号仅一次试用、
   资产经标准 project_asset schema 写入。
4. 第 4 层下游流量——与被服务站点约定 UTM 规范后做。

待定项：OAuth 回调域名（fly 应用需绑真实域名）、Facebook 开发者
应用过审、账期边界定义（自然月 vs 滚动 30 天）。

## 6. 部署拓扑（2026-11-18 定稿）

**前端 Cloudflare Pages，后台 fly.io。**

- `frontend/` 是静态 SPA（index.html + app.js + styles.css，原生 JS，
  无构建步骤）。部署：`wrangler pages deploy frontend --project-name
  osched-portal`。
- SPA 跨域调用 fly 后端 JSON API（`/api/*`），bearer token 放
  `X-Auth-Token` 头（localStorage）。API 返回宽松 CORS
  （`Access-Control-Allow-Origin: *`）——鉴权靠 token 而非 cookie，
  通配 origin 在此安全。
- fly 应用同时保留旧版 HTML 页面作回退；两个 UI 共用同一 API 与
  数据库。
- `frontend/app.js` 的 `API_BASE` 是后端换自定义域名时的唯一触点。
- 超管引导：`PORTAL_ADMIN_EMAILS`（fly env）在启动时播种
  `admin_whitelist`；白名单成员注册/登录即得 role=admin。管理端：
  审批请求、设额度、管白名单（最后一个白名单条目不可移除）。

### 前端部署步骤（Cloudflare）

```bash
wrangler login                                   # 一次，交互式
wrangler pages project create osched-portal --production-branch main
wrangler pages deploy frontend --project-name osched-portal --branch main
```

非交互 shell 需要 `CLOUDFLARE_API_TOKEN`（Pages: Edit 权限）。

### 后端部署步骤（fly.io）

见 docs/deployment.zh.md；门户经 `scripts/start.sh` 跑在同一台机器
（8080 端口，`[http_service]`）。

### 线上端点（2026-11-18）

- 前端：https://osched-portal.pages.dev（Cloudflare Pages，项目
  `osched-portal`，账号 ***REMOVED-CF-ACCT***…）
- 后端 API：https://tokencoup-skills-outreach-scheduler.fly.dev/api/*
  （fly 专用入口 v4 ***REMOVED-FLY-IPV4*** / v6 ***REMOVED-FLY-IPV6***；
  这两个固定 IP 同时是 OPR key 改绑目标）
- 同域名的旧版 HTML 门户保留作回退。
- CORS 端到端已验证：preflight 204，未带 token 的 /api/dashboard
  返回 401 JSON。
