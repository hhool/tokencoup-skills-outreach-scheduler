# Operations — cron wiring & daily rhythm

## Pool bridge — workbench ledger -> pool

A harvested pool (WDC-style crawls) is full of **web-2.0 property roots**:
blogspot, wordpress.com, reddit, tumblr. Those need an account and a post,
not a form POST, so they classify as `submit_type=unknown` and stall in the
wave-3 human queue. The curated ledger the SEO workbench keeps per site
(`~/seo/<site>/targets.jsonl`) already answers that question per target.

Bridge it one way:

```
python3 bin/osched.py import-workbench ~/seo/<site>/targets.jsonl [--min-fit 50]
```

Rules enforced by the importer (`src/osched/workbench.py`):

- **One way.** The workbench stays the ledger of record (status, link_state,
  evidence). We read a snapshot; nothing writes back. Re-import is idempotent.
- **Dedupe by pay-level domain.** An existing pool row for the domain is
  upgraded in place; its `submit_url` becomes `entry_url` only when the row
  has none (a specific endpoint is never replaced by a root URL).
- **No downgrades.** A ledger row with `submit_type=unknown` never overwrites
  a probed/curated type — that would push the plan back to wave 3 for nothing.
- **`dropped` targets are skipped.** `working` / `pending` / `done` are kept.
- Curated rows carry `fit_score` in `hints_json`; matching treats it as the
  evidence prior and lets the row into the pending queue without clearing
  `min_score` (`scoring.curated_trust: true`, default). The human `confirm`
  gate still stands between the queue and delivery.

## Execution gates (why a due plan goes to `human`)

| gate | rule | why |
|------|------|-----|
| wave-3 | `submit_type=unknown` never auto-executes | nobody knows *how* to submit; a blind POST to a random page is worse than no attempt |
| email | `channel=email` is manual unless `delivery: {email_auto: true}` | R-520: outreach mail is human-sent |
| form | live page must expose a form with a URL-ish field | login / signup / search / newsletter boxes are not submission forms |
| captcha | browser-class captcha -> `cua`; unknown -> `human` | §7.1 tiers; a stale cached captcha never routes a formless page to a browser |

## Live-link alerts (evidence layer)

Every verify pass also runs edge detection over the check series and
emails the ops mailbox (`cfg.report.email`) — never the client directly;
clients see drops via the weekly report and the receipt page.

| alert | fires when | dedupe |
|-------|-----------|--------|
| `link-drop` | a previously-live link rechecks as missing/unreachable | edge only — consecutive missings are quiet |
| `rel-change` | the rel attribute of a live link changed between checks | per change |
| `live-check-exhausted` | missings after the first live verdict hit `MAX_ATTEMPTS` -> human | once, at handoff |

Fail-open: a mail failure logs `run_log action='alert' result='failed'`
and never breaks the verify loop. Receipt URL is attached when
available. Live plans recheck every `RECHECK_AFTER_DAYS` (14) days.

## Cron entry points

`scripts/daily.sh` is the only cron entry. Two segments:

| segment | what it does | cadence |
|---------|-------------|---------|
| `run`   | `execute` every active project (due plans only) + `report` | daily, at `scheduler.run_at` (default 09:30) |
| `pipeline` | `import -> probe -> classify -> dr -> match -> schedule` | weekly, or after new source data lands |
| `client-report` (CLI, not a daily.sh segment) | per-project client-facing report (B2): live-link list, activity, decisions | weekly (`--days 7`) or monthly (`--days 30`) |

Weekly client report via cron:

```
0 10 * * 1 /path/to/outreach-scheduler/bin/osched.py client-report <project-domain> --days 7 >> /tmp/osched.cron.log 2>&1
```

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
| `live` | verified live link on the directory page (B1 ledger) |
| `cua` | needs the CUA browser channel (captcha class) |
| `human` | needs a human (bot-blocked, no email, unknown captcha, 4xx) |
| `failed` | retryable failure exhausted (5xx/network, max 3) |

中文说明见 operations.zh.md。
