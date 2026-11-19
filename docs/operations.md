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
- **`dropped` targets are skipped and propagate.** `working` / `pending` /
  `done` are kept; a ledger row marked `dropped`/`banned`/`do_not_use` also
  flips every existing pool row for that domain to `status='dropped'`, so the
  scheduler stops delivering to a target the operator already rejected.
  (Without this the pool kept queueing contact pages labelled as listings.)
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
| cua_primary | `delivery: {cua_primary: true}` sends form plans to the browser channel | window-1 evidence: both submittable static targets silently rejected the POST (re-rendered form; JS-populated hidden field + AJAX submit); the captcha gate stays above this switch |
| verdict | after submit, rejection copy on the re-rendered page -> `human` (reason recorded) | a validation error comes back at HTTP 200; calling that `submitted` is how silent rejections became fake ledger rows |
| verdict | HTTP 2xx alone is never `submitted` | POST responses are scanned for success / failure / already-listed copy (scripts stripped) and for a re-rendered submit form; ambiguous 2xx goes to `human`, silent re-render goes to `human`, no signals at all trusts 2xx |

## Register channel (stage 1: signup)

The 138 wave1 `register` rows need accounts before any link can be placed.
The chain has three segments; stage 1 is automated, stages 2-3 reuse the
existing account ledger:

| segment | tooling | status |
|---|---|---|
| 1. signup | `osched register-signup <domain>`: CUA opens the platform (static-first discovery over `/register` `/signup` `/sign-up` `/join`, then CUA walks the same paths), finds the signup form (password + identity fields, login forms rejected), fills identity + generated password, submits | done; captcha or no-form -> `human` |
| 2. verify mail | `osched mail-verify` (IMAP) extracts activation links/codes -> `account.activation_url` | done (existing) |
| 3. activate + post | `osched account-activate` (CUA clicks the link); `osched register-publish` logs in, finds a link-bearing form, posts and writes `plan(state=submitted)` so verify can see it | done |
| 1b. captcha handoff | `osched register-signup <domain> --assist` pauses on a challenge, writes `data_root/captcha_handoff/<domain>.json`, and waits for the operator to solve it in the visible browser and touch the `.go` marker (`osched handoff-status <domain>` to inspect). Signup paths hang off the **site root**, not a deep `entry_url` (alternativeto's `/manage/add-app`); the `.go` marker is the human attestation — a solved turnstile's leftover widget markup must not re-gate the form fill. | done; no automated captcha solving |

Pilot (2026-11-19, 6 confirmed targets): 0 signed — every big platform gates
signup with captcha/anti-bot (alternativeto, sitelike = turnstile; youtube,
tumblr = bot-block/timeout; jasmine, jouwpagina = no reachable form). The
channel needs the human-in-the-loop mix: CUA navigates, a human solves the
captcha. Passwords are per-account generated, stored only in the account
ledger, masked in every output.

## Outreach mail quality gate

`executor.mail_quality_gate` blocks SMTP for plans that would damage the
sending domain: non-prospect targets (`submit_type` not pitch/email/outreach),
placeholder `category=general/other/unknown`, a domain used as `sender_name`,
or unrendered `{{...}}` placeholders. Blocked rows go to `human` with the
reason in `result_note`; no mail is sent. The first review (2026-11-19)
rejected two pitch rows aimed at a news daily and a study-abroad agency.

## Live-link verification (evidence layer)

A submitted plan is re-visited after `days_after` (default 3). `verify.check_one`
resolves the client landing URL from the project domain (durable) — not from
`result_note`, which executor/cua overwrite on submit — and tries several
pages (site home/catalogue first, then the submit entry), because an accepted
link surfaces on the catalogue, not the form we POSTed to. A link found on any
page wins; `missing` is retried to `MAX_ATTEMPTS` then handed to humans.
`db.note_with_landing` folds the original landing into every successor note so
public receipts and the client report keep the delivered URL.

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
