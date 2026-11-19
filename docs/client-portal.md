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

### Anti-abuse guards (delivered 2026-11-18)

Implemented in `src/osched/guard.py`; every grant lands in `trial_ledger`
(`email_canon`, `reg_domain`, `ip`) so the same signal cannot mint a
second trial.

| vector | guard |
|--------|-------|
| throwaway mailboxes / aliases | `canonical_email()` — `+tag` stripped, gmail dots stripped, googlemail≡gmail; one trial per canonical mailbox |
| site split into sub-domains | `registrable_domain()` (eTLD+1) — `shop.a.com` collides with `a.com` for both the ledger and the "already claimed" check |
| second account on the same laptop / office | `client_ip()` reads `Fly-Client-IP`/`CF-Connecting-IP`; one trial per IP, and 5 signups per IP per 24h |
| draft hoarding | max 5 unverified `project_request` rows per account |

Refusal is graceful: the project activates with `quota_total=0`,
`quota_type=paid`, and `trial_note` carrying the reason
(`email_reused` / `site_reused` / `site_claimed` / `ip_reused` /
`client_used`). The SPA dashboard prints the reason with a package CTA;
the admin console shows it per project and can set quota manually.
Manual "approve" runs the mailbox/site checks and skips only the network
rule, since the approval IP belongs to the operator.

**Credits are enforced, not just displayed:** `guard.credit_cap` /
`allowance` bound both `scheduler.generate_plan` (won't schedule beyond
remaining credits) and `scheduler.due_today` (won't execute beyond them);
`verify.check_one` mirrors each `live` transition into
`project_request.quota_used`. Projects without a portal request stay
uncapped (our own sites, imports), and so does any request marked
`quota_type='internal'` — the escape hatch for operator-owned sites that
should never be throttled by a customer-facing credit count.

Tests: `PYTHONPATH=src python3 -m pytest tests/test_guard.py -q`
(24 cases: normalisation, verdict matrix, schedule/execute ceilings).

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
timestamp). Filters: period, domain, DR band. Each receipt is a public,
signed page (`osched evidence-url <plan>`) the client can open without
logging in: listing URL, anchor, rel, first-live time, the last 12 checks
with page hashes, and the captured listing screenshot (`osched
evidence-shot <plan> shot.png`).

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

- `frontend/` root is the **LinkAttest marketing landing page**
  (index.html + landing.css, pure CSS, no JS): hero with a live-link
  ledger mockup, stats strip, how-it-works, differentiators, the
  ratified pricing tiers, FAQ. Portal CTAs link into `/app/`.
- The client portal SPA lives at `frontend/app/` (index.html + app.js +
  styles.css, vanilla JS, hash routing, routes `#/login` `#/dashboard`
  `#/reports` `#/new` `#/admin` `#/pricing`). Deployed via
  `wrangler pages deploy frontend --project-name osched-portal`.
- `#/pricing` (public): the ratified per-live-link tiers + service
  guarantee, auth-aware CTA. Dashboard v2 adds a next-steps card
  (draft → verify button, verified → awaiting approval, rejected) and
  quota progress bars with a trial countdown (`project_request.trial_until`,
  set to +14d at trial activation; additive migration).
- The SPA calls the fly backend's JSON API (`/api/*`) cross-origin with
  a bearer token in the `X-Auth-Token` header (localStorage). The API
  answers with permissive CORS (`Access-Control-Allow-Origin: *`) —
  auth is the token, not cookies, so wildcard origin is safe here.
- The fly app keeps serving the legacy HTML pages on the same routes as
  a fallback; both UIs share the same API and database.
- `API_BASE` in `frontend/app/app.js` is the single touch point when the
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

- brand / production domain: **LinkAttest** → `linkattest.com`
  (registered 2026-11-18, Namecheap; CF zone active 22:24, Pages cert
  issued 22:29 — **live**: apex / www / `/app/` all 200)
- frontend: https://linkattest.com (Cloudflare Pages, project
  `<cf-pages-project>` in the operator's Cloudflare account; the earlier
  domain used for this portal was revoked 2026-11-18 — Pages custom domains
  detached, DNS records deleted, no redirect) — linkattest.com is the single
  frontend face
- backend API: https://tokencoup-skills-outreach-scheduler.fly.dev/api/*
  (fly dedicated ingress: two fixed IPs, one IPv4 + one IPv6 — recorded in
  the operator's private ops notes, not in this repo; they are also the OPR
  key rebinding target)
- legacy HTML portal on the same fly domain stays as a fallback.
- CORS verified end-to-end: preflight 204, unauthenticated /api/dashboard
  returns 401 JSON.

### Multilingual (EN / ZH, shipped 2026-11-18)

Two different mechanisms, deliberately:

| surface | mechanism | URLs |
|---------|-----------|------|
| marketing (landing, privacy) | **separate static pages per language** — crawlable, each with its own canonical + `hreflang` cluster | `/` (en), `/zh/` (zh-Hans), `/privacy`, `/zh/privacy` |
| portal SPA | **client-side dictionary** in `frontend/app/app.js` (`DICT.en` / `DICT.zh` + `t()`), no reload | `/app/#/…` |

Rules:

- Static pages always carry the four-link hreflang set (`en`, `zh-Hans`,
  `x-default` → EN) and a visible switcher in the header (EN page →
  `中文`, ZH page → `EN`). Adding a language = new directory
  (`frontend/<lang>/`) + hreflang entries in every existing page.
- SPA language is persisted under `portal_lang`; default is
  `navigator.language` (zh* → Chinese, else English). The switcher is the
  last item in the portal nav; `toggleLang()` re-renders without reload.
- **Never hard-code UI copy in the SPA render functions** — add a key to
  both `DICT.en` and `DICT.zh` and call `t("key")`. A missing key falls
  back to EN, so a missed translation degrades gracefully rather than
  breaking the page.
- Values returned by the backend (API messages, report text) stay
  English; the SPA localises labels and chrome only.

### Custom-domain binding (linkattest.com — executed 2026-11-18, awaiting NS)

Done on 2026-11-18 (Namecheap registration → CF zone → Pages domain → DNS):

| item | value |
|------|-------|
| CF zone id | `<cf-zone-id>` (32 hex chars — in the operator's private notes) |
| assigned NS | `<cf-ns-1>` / `<cf-ns-2>` (the two name servers CF assigned) |
| DNS records | apex `linkattest.com` CNAME → the Pages `*.pages.dev` target (proxied)<br>`www.linkattest.com` CNAME → same (proxied) |
| Pages custom domains | `linkattest.com` + `www.linkattest.com`, status initializing |

**Only remaining manual step**: Namecheap dashboard → Domain List →
linkattest.com → Manage → Nameservers → **Custom DNS**, enter the two NS
above, save. Propagation is usually 0.5–2 h (24 h worst case). Once the
CF zone turns active, Pages issues the certificate automatically.

After NS is live: set fly's `PORTAL_FRONTEND_URL` to
`https://linkattest.com/app/` (`fly secrets set` + deploy), replacing
whatever host it held before. OAuth callback URIs stay on fly.dev —
unaffected.

Original API steps (for re-runs):

```bash
CF_TOKEN=...   # account token with Pages:Edit — kept in the OS keychain
               # under the operator's own service name, never in this repo
CF_ACCT=<cf-account-id>

# 1. attach the domain to the Pages project (CF validates + provisions
#    the certificate automatically once NS/records point at CF)
curl -X POST "https://api.cloudflare.com/client/v4/accounts/$CF_ACCT/pages/projects/<cf-pages-project>/domains" \
  -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" \
  --data '{"name":"linkattest.com"}'

# 2. apex + www CNAME to the pages.dev target
#    (via dashboard or DNS records API; apex uses CNAME flattening)
```

Also update `API_BASE` in `frontend/app.js` only if the backend gets
its own domain; otherwise pages.dev → fly.dev CORS path stays as-is.

### Public contact address (required for the privacy policy)

`frontend/privacy.html` names a contact address for data requests, and the
policy is only honest if that mailbox is read by a human. It must **not** be
a personal mailbox (that address lands in every page's HTML, in git history,
and in scraped copies).

Ratified: `privacy@linkattest.com`, delivered by **Cloudflare Email Routing**
(available on the same zone, free, no extra MX provider).

**Configured via API, not the dashboard** — `tools/setup_email_routing.py`
applies the whole thing (3 MX + SPF + DMARC + the routing rule) and is
idempotent, so re-running it is safe:

```bash
CF_TOKEN=... CF_ZONE_ID=... CF_ZONE_NAME=linkattest.com \
CF_DESTINATION=<your verified mailbox> \
python3 tools/setup_email_routing.py --dry-run
```

Token permissions, verified against the live API rather than assumed:

| endpoint | this token | note |
|---|---|---|
| `zones/<id>/email/routing/rules` | **works** (read + write) | Email Routing Rules Edit |
| `zones/<id>/dns_records` | **works** (read + write) | DNS Edit |
| `zones/<id>/email/routing` (settings/enable) | blocked | zone settings write |
| `zones/<id>/email/routing/destinations` | blocked | Email Routing Addresses Edit |
| `zones/<id>/email/routing/dns` (holds the DKIM record) | blocked | Email Routing Addresses Edit |
| `accounts/<id>/email/routing/addresses` | blocked | Email Routing Addresses Edit |

### State of the live zone (checked 2026-11-20)

| item | state |
|---|---|
| MX ×3 (`route1/2/3.mx.cloudflare.net`) | present |
| SPF TXT (`include:_spf.mx.cloudflare.net ~all`) | present |
| DMARC TXT (`p=none`) | present |
| rule `privacy@linkattest.com` → operator mailbox | present, enabled, literal matcher |
| forward target | **verified** — Cloudflare accepted a rule targeting it |
| external mail to `privacy@linkattest.com` | accepted by Cloudflare's MX |
| DKIM (`cf2024-1._domainkey`) | **not published** |

Two read-only commands re-check all of that without writing anything:

```bash
python3 tools/setup_email_routing.py --check          # audit DNS + rule
python3 tools/setup_email_routing.py --probe-destination  # is the target verified?
```

`--probe-destination` works because Cloudflare refuses to create a rule
whose forward target is not a verified destination (error `2054`) and
accepts the rule when the address is verified. The script creates a
throwaway rule and deletes it again; two probe addresses came back `2054`
while the operator's mailbox came back accepted, which is how the
"destination must be verified" caveat below turned out to be already
satisfied.

Two consequences of the blocked endpoints:

1. **The DKIM record is not published.** CF normally generates
   `cf2024-1._domainkey.<zone>` during dashboard onboarding; the endpoint
   that returns it is blocked. Forwarded mail still arrives — CF signs
   outbound mail and Gmail falls back to SPF — but DKIM verification will
   not pass, which costs a little inbox trust. The DMARC record this
   script writes is therefore `p=none`, so a forwarded email that fails
   DKIM does not also fail a strict DMARC policy and get filtered. To fix
   properly: enable Email Routing once in the dashboard (Compute > Email
   Service > Email Routing > linkattest.com), which publishes the DKIM
   record, then re-run the script with `--publish-dkim` and it writes the
   record itself.
2. **The destination must already be verified.** Destination addresses are
   account-level and this token cannot list or create them. Until the
   mailbox confirms the Cloudflare verification email, CF keeps the rule
   inactive and drops the mail — the API still reports `enabled=true`, so
   do not trust that field alone. `--probe-destination` is the check for
   it.

Also worth knowing: `PATCH` on a rule id is rejected with "Authentication
error" by the same token that may `POST` and `PUT` rules. The script uses
`PUT` for updates and never `PATCH`.

The forwarding destination is the operator's existing mailbox — CF requires
the destination to confirm before it forwards, so the personal address
stays private while `privacy@` stays public.

If the address ever changes, it is a one-line edit in each of
`frontend/privacy.html` and `frontend/zh/privacy.html` — never a policy
rewrite. Because it is a plain string in two static pages, there is no
config indirection worth adding for it.
