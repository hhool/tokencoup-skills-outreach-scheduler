# 运维手册 — cron 接线与每日节奏

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
| `cua` | 待 CUA 浏览器通道（验证码类） |
| `human` | 需人工（bot-blocked、无邮箱、未知验证码、4xx） |
| `failed` | 可重试失败耗尽（5xx/网络，最多 3 次） |
