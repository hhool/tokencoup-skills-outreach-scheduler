# Operations — cron wiring & daily rhythm

## Cron entry points

`scripts/daily.sh` is the only cron entry. Two segments:

| segment | what it does | cadence |
|---------|-------------|---------|
| `run`   | `execute` every active project (due plans only) + `report` | daily, at `scheduler.run_at` (default 09:30) |
| `pipeline` | `import -> probe -> classify -> dr -> match -> schedule` | weekly, or after new source data lands |

Install (crontab -e):

```
30 9 * * * /path/to/outreach-scheduler/scripts/daily.sh run >> /tmp/osched.cron.log 2>&1
0 9 * * 1 /path/to/outreach-scheduler/scripts/daily.sh pipeline >> /tmp/osched.cron.log 2>&1
```

Rules:
- Every stage is idempotent; a cron re-run never double-submits (plan rows leave `queued` once executed).
- `dr` is fail-open: an unbound/absent API key skips enrichment, never blocks the pipeline.
- Failures never crash the chain (`|| true` per project); everything lands in `run_log` and shows up in the daily report's human-action section.

## DR (Open PageRank) IP binding

The free key is bound to one IP address. If the API returns
`403 This free API key is set to another IP address`, either update the
allowed IP at keywordseverywhere.com/account/openpagerank, or run the `dr`
stage on the machine whose IP matches the binding. A stable server IP is
preferable for batch enrichment (55k+ domains run against a 30k/month
budget); a laptop on mobile broadband changes IPs and will need rebinding.

## State machine quick reference

| plan.state | meaning |
|-----------|---------|
| `queued` | scheduled, awaiting execution |
| `submitted` / `sent` | wave1 / wave2 done |
| `cua` | needs the CUA browser channel (captcha class) |
| `human` | needs a human (bot-blocked, no email, unknown captcha, 4xx) |
| `failed` | retryable failure exhausted (5xx/network, max 3) |

中文说明见 operations.zh.md。
