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
- **跳过 `dropped`**：保留 `working` / `pending` / `done`。
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
