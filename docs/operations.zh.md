# 运维手册 — cron 接线与每日节奏

## Pool 桥接 —— workbench 台账 → pool

爬取来的 pool（WDC 类）里大量是**web2.0 平台根域名**：blogspot、
wordpress.com、reddit、tumblr。这些要「开户 + 发文」，不是表单直投，
所以一律归为 `submit_type=unknown`，卡在 wave3 人工队列。而 SEO 工作台
按站点维护的策展台账（`~/seo/<site>/targets.jsonl`）已经逐条回答了这个
问题。

单向桥接：

```
python3 bin/osched.py import-workbench ~/seo/<site>/targets.jsonl [--min-fit 50]
```

导入器（`src/osched/workbench.py`）强制以下规则：

- **单向**：workbench 仍是台账主（status / link_state / 证据都在那边），
  这里只读快照，永不回写；重复导入幂等。
- **按可注册域去重**：已有 pool 行就地升级；`submit_url` 只在
  `entry_url` 为空时写入（具体端点绝不被根 URL 覆盖）。
- **不允许降级**：台账里 `submit_type=unknown` 的行不会覆盖已探测/已策展
  的类型——那只会把计划无缘无故打回 wave3。
- **跳过 `dropped`，且丢弃会传导**：保留 `working` / `pending` / `done`；
  台账标 `dropped` / `banned` / `do_not_use` 的行，会把该域在 pool 里的
  所有行置为 `status='dropped'`——台账已经放弃的目标，调度器不再投递。
  （此前只跳过不传导，导致已判死的联系表单页仍在排队投递。）
- 策展行的 `fit_score` 存进 `hints_json`，匹配时当作证据先验，并允许
  该行绕过 `min_score` 进待确认队列（`scoring.curated_trust: true`，默认开）。
  人工 `confirm` 闸仍在队列与投递之间。

## 执行闸门（到期计划为何落 `human`）

| 闸门 | 规则 | 原因 |
|------|------|------|
| wave3 | `submit_type=unknown` 永不自动执行 | 还没人搞清「怎么投」；盲投到随机页比不投更糟 |
| 邮件 | `channel=email` 默认人工，除非项目声明 `delivery: {email_auto: true}` | R-520：外联邮件人工发送 |
| 表单 | 实时页面必须存在含 url 类字段的表单 | 登录/注册/搜索/订阅框不是提交表单 |
| 验证码 | 浏览器类 → `cua`；未知 → `human` | §7.1 分级；缓存的旧验证码不会把无表单页面送进浏览器 |
| CUA 优先 | `delivery: {cua_primary: true}` 把 form 计划改走浏览器通道 | 窗口 1 实证：两个可静态提交目标全部静默拒绝（表单重渲染；JS 填充隐藏字段 + AJAX 提交）；验证码闸门仍在此开关之上 |
| 判定 | 提交后页面若出现拒绝文案 → `human`（记拒因） | 校验错误同样 200 重渲染，当成 `submitted` 就是假台账的来源 |
| 判定 | HTTP 2xx 本身不构成 `submitted` | POST 响应扫描成功/失败/已存在文案（剥离 script）与表单重渲染；带失败文案或重渲染表单的 2xx → `human`，完全无信号才信任 2xx |

## Register 通道（阶段 1：开户）

138 条 wave1 `register` 行必须先有账号才能放链。链条分三段；阶段 1 已自动化，
二、三段复用既有账号台账：

| 段 | 工具 | 状态 |
|---|---|---|
| 1. 开户 | `osched register-signup <domain>`：CUA 打开平台（先静态发现 `/register` `/signup` `/sign-up` `/join`，被挡再由 CUA 走同批路径），识别注册表单（密码 + 身份字段，登录表单拒绝），填身份字段 + 生成密码，提交 | 完成；验证码/无表单 → `human` |
| 2. 验证邮件 | `osched mail-verify`（IMAP）提取激活链接/验证码 → `account.activation_url` | 完成（既有） |
| 3. 激活 + 发文 | `osched account-activate`（CUA 点击激活链接）；`osched register-publish` 登录、找含 URL 的表单、发文并落 `plan(state=submitted)` 供验活 | 完成 |
| 1b. 验证码交接 | `osched register-signup <domain> --assist` 遇验证码暂停，写 `data_root/captcha_handoff/<domain>.json`，等人工在可见浏览器解决后 touch `.go` 信号（`osched handoff-status <domain>` 查看）。注册路径挂在**站点根域**，不是深层 `entry_url`（alternativeto 的 `/manage/add-app`）；`.go` 信号即人工确认——已过的 turnstile 残留 widget 标记不得再次拦住表单填报 | 完成；不做自动过码 |

试点（2026-11-19，6 个 confirmed 目标）：0 开户成功——大平台开户全部有验证码/反爬
（alternativeto、sitelike = turnstile；youtube、tumblr = 反爬/超时；jasmine、
jouwpagina = 无可达表单）。通道需要人机混合：CUA 导航、人工过验证码。密码按账号
生成，仅存账号台账，所有输出路径均掩码。

## 外联邮件质量闸门

`executor.mail_quality_gate` 在 SMTP 前拦下会烧发件域名的计划：目标非 pitch
对象（`submit_type` 不是 pitch/email/outreach）、`category=general/other/unknown`
占位、`sender_name` 是域名、或未渲染的 `{{...}}`。拦截行转 `human`，原因写进
`result_note`，不发任何邮件。首次审查（2026-11-19）拦下两条发往新闻日报与
留学中介的 pitch。

## 实链验活（证据层）

已提交计划在 `days_after`（默认 3 天）后复访。`verify.check_one` 从项目域名
解析客户落地 URL（持久来源）——不从 `result_note` 读，因为 executor/cua
提交时会覆盖它；并尝试多个页面（先站点首页/目录页，再提交入口），因为
被收录的链接出现在目录页而非我们 POST 的表单页。任一页面命中即 live；
`missing` 重试至 `MAX_ATTEMPTS` 后转人工。`db.note_with_landing` 把原 landing
折进每次后续 note，保证公开收据和客户报告保留交付 URL。

## 实链告警（证据层）

每次验活同时对检查序列做边沿检测，命中即发运维信箱
（`cfg.report.email`）——不直发客户；客户通过周报与证据页感知掉链。

| 告警 | 触发 | 去重 |
|------|------|------|
| `link-drop` | 曾 live 的链接复检变 missing/unreachable | 只在边沿触发——连续未命中保持安静 |
| `rel-change` | live 链接的 rel 属性在两次检查间变化 | 每次变化 |
| `live-check-exhausted` | 首次 live 后的 missing 达到 `MAX_ATTEMPTS` → 进人工 | 交接时一次 |

Fail-open：发信失败记 `run_log action='alert' result='failed'`，绝不中断
验活循环。可用时附证据页 URL。live 计划每 `RECHECK_AFTER_DAYS`（14）
天复检一轮。

## Cron 入口

`scripts/daily.sh` 是唯一 cron 入口，两个段：

| 段 | 内容 | 频率 |
|---------|-------------|---------|
| `run` | 对所有 active 项目执行到期计划（`execute`）+ 日报（`report`） | 每日，`scheduler.run_at`（默认 09:30） |
| `pipeline` | `import -> probe -> classify -> dr -> match -> schedule` 供给链 | 每周一次，或新源数据到位后手动跑 |

安装（crontab -e）：

```
30 9 * * * /path/to/outreach-scheduler/scripts/daily.sh run >> /tmp/osched.cron.log 2>&1
0 9 * * 1 /path/to/outreach-scheduler/scripts/daily.sh pipeline >> /tmp/osched.cron.log 2>&1
```

规则：
- 每个阶段幂等；cron 重跑不会重复投递（计划行一旦执行就离开 `queued`）。
- `dr` fail-open：key 未配置/未绑定时跳过富化，不阻塞流水线。
- 单项目失败不断链（每项目 `|| true`）；全部落 `run_log`，出现在日报「人工待办」段。

## DR（Open PageRank）IP 绑定

免费 key 绑定单一 IP。若 API 返回
`403 This free API key is set to another IP address`，二选一：
在 keywordseverywhere.com/account/openpagerank 改绑允许 IP，或在与绑定 IP
一致的机器上跑 `dr` 阶段。批量富化（55k+ 域名对 30k/月额度）建议放固定
服务器 IP；家宽/移动网络 IP 漂移，会反复需要改绑。

## 计划状态机速查

| plan.state | 含义 |
|-----------|---------|
| `queued` | 已排期待执行 |
| `submitted` / `sent` | wave1 / wave2 完成 |
| `live` | 回访确认链接已上线（B1 实链台账） |
| `cua` | 待 CUA 浏览器通道（验证码类） |
| `human` | 需人工（bot-blocked、无邮箱、未知验证码、4xx） |
| `failed` | 可重试失败耗尽（5xx/网络，最多 3 次） |
