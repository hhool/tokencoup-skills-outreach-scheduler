# Client intake & operations handbook

Purpose: define what a client must provide, how the system feeds back
operating status, and the internal business process from intake to steady
state. Validated against three simulated client profiles (§3).

Chinese version: see `client-intake.zh.md`.

---

## 1. Client intake checklist

### 1.1 Required (kickoff blocked without these)

| # | Item | Maps to system | Notes |
|---|------|----------------|-------|
| 1 | **Domain** (promotion target) | `project.domain` | one project per domain |
| 2 | **Promotion scope**: whole site now, specific pages later | `project.tags` + level-1/2 URL list (asset `page`) | per-page/product promotion = a later phase adding landing URLs; scope changes are just asset updates, no new project |
| 3 | **Client operational mailbox** | wave2 sender identity + directory-site registrations | email address **plus SMTP/IMAP app-password credentials** (or the client sends manually); credentials go to gitignored env file, never in the repo |
| 4 | **Site TDK** (title / description / keywords) | asset `tdk` | if not provided, system probes homepage meta; if probing fails → flagged `missing` in the daily report for manual fill |
| 5 | **Level-1/2 page URLs** to promote (manual list) | asset `page` | manual input only; absent → submissions land on the homepage |
| 6 | **Brand basics**: brand/maker name, one-line intro, logo URL | form fields, outreach signature | probed from the site if absent |

### 1.2 Optional (defaults exist)

| # | Item | Default when absent |
|---|------|---------------------|
| 7 | Blog article list (guest_post bodies) | sitemap/RSS harvest; still nothing → **site-summary article** generated from the site's own summary |
| 8 | Outreach email copy preferences (tone, signature) | **generic outreach email template** |
| 9 | Exclusion list (site types / competitors not to touch) | empty |
| 10 | Daily quota preference | system default (per-project 10/day) |
| 11 | Report recipients (client-facing) | report only lands in run_log; enable by configuring `report.email` |

---

## 2. Operating status feedback (client-facing loop)

Two report layers, both from the same run_log — nothing is reported that is
not already recorded:

| Layer | Cadence | Content |
|-------|---------|---------|
| **Daily report** (internal, §8 of DESIGN.md) | daily, auto | today's plan / yesterday's results / pending-confirmation queue / pool changes / human-action items |
| **Client report** (filtered daily report) | configurable (daily or weekly digest) | submissions made, successes/failures, **live link list**, assets on template fallback (quality signal), decisions needed from client |

Key notifications that always reach the client regardless of cadence:
- first live backlink for the project
- weekly digest (volume, success rate, new live links)
- anomalies: domain blocks, email bounces, path changes on reused submit endpoints

Recipient empty = no send (run_log only), same fail-open rule as the internal report.

---

## 3. Simulated client profiles (validation cases)

| | Client A — game portal | Client B — kids-bike reviews | Client C — sound-board tools |
|---|---|---|---|
| Domain | games-portal.example | kids-bike-reviews.example | sound-tools.example |
| Goal | traffic; **phase 1 whole site, phase 2 specific game pages** (e.g. Crossy Road, 2048 Couch Lazy) | traffic | traffic |
| Category tags | gaming, browser-games, english | parenting, balance-bike, english | tools, sound, english |
| Phase 2 mechanics | add game landing URLs to the `page` asset list — matching/scoring picks them up as landing-URL candidates; no new project needed | — | — |
| Likely channels | wave1 form submits (game/app directories) + wave2 email | wave1 + wave2 (parenting blogs, guest posts) | wave1 (tool directories) |

What the simulation validates:
- one project per domain, scope evolves via assets (A's two phases)
- category tags drive matching per market (B parenting vs C tools vs A gaming)
- assets fallback chain works per client: A/B/C each may miss different assets

---

## 4. Internal business process (intake → steady state)

```
① intake         client provides checklist §1.1 (+ any §1.2)
                 ↓
② verification   domain reachable? TDK probe? mailbox SMTP/IMAP login test?
                 page URLs return 200? → problems reported before kickoff
                 ↓
③ project setup  create project + assets (provided / probed / template),
                 tags, quota, exclusions
                 ↓
④ matching       pool × project → score → pending-confirmation queue
                 ↓  (human gate: select/confirm)
⑤ scheduling     confirmed items fill future dates by daily_quota
                 ↓
⑥ execution      cron: wave1 HTTP form submit; wave2 SMTP send;
                 captcha tiering; results → run_log
                 ↓
⑦ feedback       daily report (internal) + client report (filtered);
                 anomalies flagged
                 ↓
⑧ review loop    live-rate / failure-rate review → adjust quota,
                 templates, exclusions → back to ④
```

Step ownership: ①② are human (sales/ops), ③④⑤⑥⑦ are system with the
single human gate at ④, ⑧ is human strategy on system data.

---

## 5. Gaps this simulation surfaced → design notes

1. **Multi-client sender identity**: wave2 SMTP credentials are currently one
   global env file. With three clients, per-project sender identity (name +
   email + credentials path in `projects/<domain>.yaml`) is needed if each
   client uses its own mailbox. Default if unspecified: the shared ops mailbox.
2. **Client report recipient**: `report.email` in project YAML (per-project),
   falling back to global config — already fits the config model.
3. **Client A phase-2** needs no schema change: it is just new entries in the
   `page` asset. Matching treats them as higher-priority landing URLs (fresh
   entries get a recency boost in scoring — to be confirmed in Phase 3).
4. **Client mailbox roles** (ratified from real usage): a client mailbox
   carries two distinct business roles —
   - **registration role**: brand identity for directory/form submissions
     (lands in the `contact-email` asset, used by wave1 forms and CUA fills,
     and as wave2 sender identity);
   - **summary role**: recipient of periodic data digests (client report /
     daily report, `report.email`).
   One mailbox may serve both roles at once (current azgames practice:
   one Gmail does registrations *and* receives the digests). The config
   model already supports this — the same address can appear in
   `contact-email` and `report.email`; splitting into two mailboxes later
   is a config-only change, no schema impact. Client mailboxes are user
   data: credentials live in gitignored env files, addresses never in the
   repo.
