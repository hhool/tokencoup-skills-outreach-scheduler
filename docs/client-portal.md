# Client portal — auth & dashboard design

**Scope**: the client-facing web surface for the commercialization track.
Clients register/login with email (incl. Google / Facebook OAuth), then
read two things and nothing else: **quota consumption** (what their
package bought, what it has consumed) and **promotion effect** (what the
service produced). The portal is read-only over the same database the
scheduler writes; it never bypasses a human gate and never exposes
internal machinery (pool, templates, sender credentials).

中文版：client-portal.zh.md。

## 1. Identity & access model

| item | decision |
|------|----------|
| registration | email + verification mail (reuses B3 IMAP machinery) |
| OAuth | Google and Facebook sign-in buttons; OAuth identity binds to the same client record on first login |
| one identity | one client record per contract; multiple viewers per client are a later phase |
| mapping | client record ↔ one or more `project`s (matches client-intake §2) |
| roles | `client` (read-only own data); `ops` (internal, existing CLI access) |
| hosting | extends the fly.io app (HTTP side); portal API reads sqlite read-only |

Anti-goal: the portal is **not** a self-service submission tool. Clients
never trigger deliveries; they read ledgers and approve queue items that
the human gate already confirmed.

## 1.5 Self-service project creation + trial credits (ratified 2026-11-18)

Registered users may create their own promotion project and receive
trial credits — this is the acquisition funnel for the 2B service.

### Flow

1. **Create project** — wizard collects: target domain, site title,
   site summary, contact email (the same §5a asset schema the scheduler
   already ingests), market/tags. One project per verified domain.
2. **Verify ownership** — DNS TXT record or HTML meta tag on the domain
   (same convention as search-console verification). No verification →
   no project activation, no trial. This is the anti-abuse anchor:
   one trial per **verified domain**, not per email (otherwise throwaway
   mailboxes farm trials).
3. **Trial grant** — on activation the project receives trial credits
   (default: 3 live links, one-time, `quota_type=trial`). Consumption
   uses the same View A ledger; a trial link that never reaches live is
   not billed and does not consume trial quota.
4. **Conversion path** — when trial credits are consumed or exhausted,
   View A switches to a "choose a package" state; purchased packages
   become `quota_type=paid` credits. Historical trial ledger stays
   visible as the evidence the prospect evaluates.

### Write-path discipline

- The portal's **only** direct writes are: client identity records,
  project creation requests, ownership-verification markers, package
  purchase records. All delivery-side tables (pool, match, plan,
  link_verify) remain scheduler-owned.
- A created project enters the normal pipeline: probe → match →
  **human confirm** → schedule → execute. Self-service changes nothing
  about who pulls the trigger on submissions.
- Trial projects are tagged so B5 ops view and R-522 rhythm treat them
  under the same domain-level quotas as paying clients (no free tier
  that starves paid delivery capacity).

### Trial parameters (defaults, tunable per config)

| parameter | default | rationale |
|-----------|---------|-----------|
| trial credits | 3 live links | one directory-spread sample, enough to show ledger + effect views |
| trial window | 14 days | forces the pacing conversation early |
| trial per domain | 1 | ownership-verified domain is the identity |
| trial per OAuth identity | 1 | blocks same-person multi-domain farming |

## 2. View A — quota consumption (额度消耗)

Two layers: **package credits** (commercial) and **delivery capacity**
(operational transparency).

### 2.1 Package credits (commercial layer)

Per the ratified pricing verdict (business.md §Pricing), 2B service is
sold per delivered live link, packaged as a monthly credit allowance.

- `quota_total` = package size (e.g. 30 live links / month)
- `quota_used` = links confirmed `live` **in the current billing period**
- consumption rule: only `live` consumes credits. `submitted`/`sent` are
  work-in-progress, never billed; a link that never reaches live after
  auto-retry + human fallback is **not** billed (service guarantee).
- data source: `link_verify` (verdict=live) joined to `plan`/`match` →
  project → client.

### 2.2 Delivery capacity (operational layer)

Why the client sees pacing: quotas are per-domain and shared by platform
rules, so submissions are deliberately staggered over days (R-522/R-530).

- per-domain address budget: 100 / domain / 24h — rendered as a capacity
  gauge per target domain, "used today" vs "budget".
- client daily rhythm: scheduled vs delivered counts over the rolling
  window; SLA row (project `sla.min_daily`) with on/off-track badge —
  reuses B5 `ops.py` alerts, filtered to the viewer's projects.
- wave composition: which items are waiting (wave1 one-shot vs wave2
  multi-touch) so delays are legible as pacing, not stall.

### 2.3 Quota view wireframe

```
┌ Quota ────────────────────────────────────────────┐
│ Credits        ▓▓▓▓▓▓▓▓░░░░░░░  8 / 30 live      │
│ In flight      submitted 12 · pending confirm 3  │
│ Not billed     retries 4 · human queue 2         │
│ Pace           0.8 links/day (need 1.0)  ⚠ behind│
│ Forecast       package exhausts ~ Nov 30         │
└───────────────────────────────────────────────────┘
┌ Delivery capacity ────────────────────────────────┐
│ example-dir.com   ▓▓▓░░  31/100 addresses today   │
│ daily rhythm      [bars over last 14 days]        │
└───────────────────────────────────────────────────┘
```

## 3. View B — promotion effect (推广效果)

Four tiers, ordered by evidence strength. Tier 1–3 ship with portal v1;
Tier 4 is a later phase (needs UTM/GSC integration).

### Tier 1 — live-link ledger (hard evidence)

Every live link as a row: target URL, anchor, placement domain, **DR**,
confirmed date, verify verdict history. This is the invoice-grade
evidence: each row is a consumed credit with a receipt (link_verify
timestamp). Filters: period, domain, DR band.

### Tier 2 — placement quality

- DR distribution of live placements (buckets: <30 / 30–60 / 60+)
- source class mix (directory / blog / listing / answer) — from pool
  classification
- captcha tier of source (none / v2 / v3) as a durability proxy: sources
  that guard submissions tend to guard deletion too.

### Tier 3 — funnel trend

Weekly series: pending → confirmed → submitted/sent → live, plus
**live rate** (live ÷ submitted) and **approval rate** (approved ÷
attempted). Data source: run_log + link_verify aggregation — same
aggregation as B4 profile, projected to the client as trend charts.

### Tier 4 — downstream traffic (later phase)

Referral visits from live placements via UTM-tagged links and GSC data
for the served site. Out of portal v1: it requires touching the served
sites' analytics, a separate integration.

## 4. Data contract (portal reads, scheduler owns)

| portal element | source table(s) | notes |
|----------------|-----------------|-------|
| credits used | link_verify ⋈ plan ⋈ match | verdict='live', current period |
| in-flight | plan (state submitted/sent), match (pending) | read-only |
| capacity gauge | run_log per domain per day | mirrors R-530 quota keys |
| live ledger rows | link_verify + pool.dr | pool.dr refreshed by OPR batch (on hold) |
| funnel/trend | run_log, link_verify | same aggregation as B4/B5 |
| report archive | client_report emails | also mirrored as portal page |

Constraint: portal API is **read-only SQL** against the volume sqlite.
Deliveries stay in the scheduler path (cron + human gates); the portal
process and the cron process are separate, sqlite WAL mode allows both.

## 5. Build order

1. ~~portal skeleton on fly.io~~ **shipped 2026-11-18** (`src/osched/portal.py`,
   `osched portal` CLI): email registration + verification mail (reuses
   the report SMTP machinery), scrypt passwords, HttpOnly session
   cookies, read-only View A/B v0 dashboard; Google/Facebook buttons
   stubbed (Google first when the callback domain is bound).
2. ~~View A/B v1~~ **shipped 2026-11-18**: monthly quota window,
   capacity gauge, DR buckets, source mix, weekly funnel, report
   archive page (`/reports`, reuses B2 machinery without mail).
3. ~~Self-service project creation + ownership verification + trial
   credits (§1.5)~~ **shipped 2026-11-18**: meta-tag verification
   (DNS TXT later), one trial per verified domain AND per account,
   assets written via the standard project_asset schema.
4. Tier 4 downstream traffic — after UTM convention agreed with served
   sites.

Open items: OAuth callback domain (needs a real domain on the fly app),
Facebook developer app review, billing period boundary definition
(calendar month vs rolling 30d).

## 6. Deployment topology (ratified 2026-11-18)

**Frontend on Cloudflare Pages, backend on fly.io.**

- `frontend/` is a static SPA (index.html + app.js + styles.css, vanilla
  JS, no build step). Deployed via `wrangler pages deploy frontend
  --project-name osched-portal`.
- The SPA calls the fly backend's JSON API (`/api/*`) cross-origin with
  a bearer token in the `X-Auth-Token` header (localStorage). The API
  answers with permissive CORS (`Access-Control-Allow-Origin: *`) —
  auth is the token, not cookies, so wildcard origin is safe here.
- The fly app keeps serving the legacy HTML pages on the same routes as
  a fallback; both UIs share the same API and database.
- `API_BASE` in `frontend/app.js` is the single touch point when the
  backend moves to a custom domain.
- Super-admin bootstrap: `PORTAL_ADMIN_EMAILS` (fly env) seeds the
  `admin_whitelist` at boot; whitelist members get role=admin at
  registration/login. Admin UI: approve requests, set quotas, manage
  the whitelist (last-admin removal is guarded).

### Deploy steps — frontend (Cloudflare)

```bash
wrangler login                                   # once, interactive
wrangler pages project create osched-portal --production-branch main
wrangler pages deploy frontend --project-name osched-portal --branch main
```

Non-interactive shells need `CLOUDFLARE_API_TOKEN` (Pages: Edit).

### Deploy steps — backend (fly.io)

Unchanged from docs/deployment.md, plus: the portal runs inside the
same machine via `scripts/start.sh` (port 8080, `[http_service]`).

### Live endpoints (2026-11-18)

- frontend: https://osched-portal.pages.dev (Cloudflare Pages,
  project `osched-portal`, account ***REMOVED-CF-ACCT***…)
- backend API: https://tokencoup-skills-outreach-scheduler.fly.dev/api/*
  (fly dedicated ingress v4 ***REMOVED-FLY-IPV4*** / v6 ***REMOVED-FLY-IPV6***;
  these fixed IPs are also the OPR key rebinding target)
- legacy HTML portal on the same fly domain stays as a fallback.
- CORS verified end-to-end: preflight 204, unauthenticated /api/dashboard
  returns 401 JSON.
