# outreach-scheduler — Design Document

Date: 2026-11-18 · Status: design draft, ratified by user across four decision rounds
This document lives in the `outreach-scheduler/` project folder; the system will later run independently of any external toolchain.

Chinese version: see `DESIGN.zh.md`.

---

## 0. Positioning

The four original requirements describe one thing: an **outreach planning & scheduling center**. Recommended as a standalone repo `outreach-scheduler` with four layers:

1. **Import layer**: link library → link pool (one-way import; nothing is written back to sources)
2. **Pool**: single-file SQLite, auto-classified + DR weighting
3. **Planning layer**: project (domain) → auto-match → **one mandatory human-confirmation gate** → configurable send plan
4. **Execution layer**: periodic auto-execution (wave1 form submit + wave2 email send) + daily email report

Relation to existing systems:
- **Import sources = WDC harvested data + other similar backlink datasets** (never written back)
- workbench `targets.jsonl` / prospector exports are **NOT import sources** and are no longer upstream; workbench keeps its own review/ledger role untouched
- The legacy mail system's R-520 convention (human sending) applies to that system only; this system implements automated sending independently, but keeps an escape hatch: failures/suspicious results go to a human queue

Key design consequence: WDC/similar data are **raw competitor backlinks** (external links pointing to competitors), not a ready-made "submittable platform list". The import layer therefore includes a **probe stage**: HTTP-probe each URL to determine "is it submittable" — entries with submit forms / register endpoints / contact emails are upgraded to opportunity entries; the rest stay as ordinary pool entries.

---

## 1. Ratified decisions (2026-11-18, four rounds)

| # | Question | Decision |
|---|----------|----------|
| 1 | Link-library source | **WDC data + other similar harvested backlink datasets**. workbench targets.jsonl + prospector exports are not used |
| 1a | Import-source design | **Abstract interface first + unified output model** (§4b SourceAdapter/SourceRecord); new sources plug in via adapters, zero downstream changes |
| 1b | WDC code reuse | The 4 WDC processing files from the internal harvest repository were copied as-is into `import/` (harvest_competitor_links.py, competitor_backlinks.py, export_wdc_fast.py, dl.sh + README) |
| 1b | Captcha recognition | **Added requirement.** Execution layer must recognize captchas (tiered strategy, §7); unsolvable ones go to the human queue |
| 2 | DR weighting | **Yes.** Enabled via free sources: Keywords Everywhere Open PageRank (free 30k domains/month, 0-10 score); Moz free tier (50 rows/month, needs credit card) is supplementary only, not a dependency |
| 3 | Execution boundary | **Fully automated** (wave1 form submit auto, wave2 email auto-send); no "list-only" mode |
| 4 | Email report | Sent automatically; **recipient defaults to empty = not sent**; enabling happens via config |
| 5 | Human confirmation after matching | **Required.** Auto-match → pending queue → human selection → scheduled plan → executor only runs confirmed items |
| 6 | Doc language | **English-primary `.md` / Chinese `.zh.md`** |
| 7 | Submit paths (round 5) | **Discovered submit paths are recorded and reused**; paths are re-verified and updated promptly when they change (§4c) |
| 8 | Project assets (round 5) | **New project input = a domain.** Site TDK, level-1/2 page URLs, blog articles, outreach email copy are collected per project; missing items are probed; if probing fails, generic templates fill in (§5a) |

---

## 2. Architecture & data flow

```
Import sources                     This repo (standalone)
┌──────────────────┐   ┌──────────────────────────────────────────────┐
│ WDC harvest data │──►│ ① import+probe ──► ② pool (SQLite)           │
│ similar datasets │   │  dedupe → HTTP probe (entry/form/captcha/    │
│ (raw backlinks)  │   │  contact) → classify + DR                    │
└──────────────────┘   └──────────────────────┬───────────────────────┘
                                    ┌─────────▼──────────┐
                                    │ ③ planning          │
   human-confirmation gate ───────► │ project (domain) →  │
   (select from pending queue)      │ auto-match → score  │
                                    │ → pending queue     │
                                    └─────────┬──────────┘
                                    ┌─────────▼──────────┐
                                    │ ④ execution         │
                                    │ cron-launched CLI:  │
                                    │ take today's plan → │
                                    │ auto-execute        │
                                    │ (captcha tiers) →   │
                                    │ write back → SMTP   │
                                    │ report              │
                                    └─────────────────────┘
```

Key points:
- The pool is the single source of truth; imports dedupe by (domain + page URL), with provenance kept in `source`
- WDC/similar data are raw competitor backlinks — **the probe stage decides "submittable or not"**: entries with a submit endpoint/contact are upgraded to opportunities, others stay as ordinary pool entries
- A mandatory human-confirmation gate sits between matching and execution (decision #5)
- The executor only consumes items that are confirmed *and* scheduled; it never touches `pending` state

---

## 3. Data model (SQLite tables, draft)

```
pool        pool entry
  id, source(wdc|similar:<name>), source_id, url, domain,
  anchor_text, target_domain(competitor domain the backlink points to),
  entry_url(discovered submit/register endpoint),
  has_submit_form, form_fields(JSON), captcha_type(none|recaptcha_v2|
    recaptcha_v3|turnstile|hcaptcha|image|email_code|unknown),
  contact_email, probe_state(unprobed|probed|unreachable),
  site_type(directory|bookmark|saas-directory|launchpad|blog|forum|…),
  submit_type(auto|register|free|listing|posting|guest_post|pitch|…),
  wave(1|2|3), category(gaming|parenting|tools|general|…),
  dr(0-10 Open PageRank), language, geo,
  status(active|dead|bot-blocked|rejected),
  first_seen, last_seen, raw(JSON passthrough)

project     outreach project
  id, domain, tags(JSON), status, created_at

match       match result
  id, project_id, pool_id, score, score_detail(JSON),
  state(pending|confirmed|rejected), confirmed_at

plan        schedule & execution record
  id, project_id, match_id, scheduled_date, channel(form|email),
  template_id, state(queued|sent|failed|blocked|skipped),
  captcha_solved(0|1), captcha_cost, executed_at, result_note

run_log     execution log
  id, ts, action, target, result, note

probe_cache  submit-path cache (recorded discoveries, reusable; §4c)
  id, domain, entry_url, form_signature(field-name hash), captcha_type,
  contact_email, success_count, last_probed, last_verified,
  state(valid|changed|gone|blocked),
  history(JSON: [{ts, old_entry, new_entry, reason}])

project_asset  per-project site assets (§5a)
  id, project_id, kind(tdk|page|article|email_copy),
  source(provided|probed|template), content, url(nullable), fetched_at
```

Configuration never lives in the database — all YAML (§6); changing config changes behavior.

---

## 4. Auto-classification pipeline (runs at import)

**Step 1: HTTP probe** — WDC/similar data are raw backlinks and must be probed before submission:
- Fetch the page, identify submit/register endpoints (patterns like submit.php / add-link / register + form DOM signatures)
- Extract form fields, **captcha type** (recaptcha_v2/v3, turnstile, hcaptcha, image, email code, none), contact email
- Entries with no endpoint are marked unprobed and settle in the pool; they do not enter submit matching. Probe results are cached to avoid refetching

**Step 2: three-layer classification**, all automatic:

1. **site_type**: classified by submit_type + DOM/URL signatures into directory / bookmark / SaaS directory / launchpad / blog / forum
2. **wave derivation**: decided by the primary submit_type; an explicit `wave` value always wins over the derived one
3. **category**: keyword rule table (gaming/parenting/tools/…); rules first, manual correction later; rule table lives in config and is extensible

DR enrichment:
- Open PageRank batch queries (deduped by domain, local cache, never queried twice)
- **Known pitfall**: free API keys are bound to a single egress IP; switching networks yields 403 address_error (already handled in claude-seo). Bind the machine's egress IP on the account page; changeable once per week
- DR feeds matching weights (§5) and pool layer statistics

Liveness rule (existing convention): 403/412/429 = Cloudflare anti-bot, **not** a dead link — keep and mark `bot-blocked`, do not prune.

---

## 4b. Import-source abstraction & unified data model (decision: interface first)

The import layer uses the **adapter pattern**: every data source implements one SourceAdapter, all producing the unified SourceRecord; probe/classify/DR only face the unified model and stay decoupled from specific sources.

### Unified output model: SourceRecord

| Field | Type | Meaning |
|-------|------|---------|
| `url` | str, required | harvested page/site URL |
| `domain` | str | derived (url → pay-level domain); if the adapter omits it, the pipeline normalizes |
| `source` | provenance object | `{kind, file, row_no}` — traceable to source file and row |
| `evidence` | evidence object | `{via_competitors: [domains…], inbound: n, corpus: "wdc-2012-08"}` — WDC-graph evidence priors; other sources fill what they have, empty otherwise |
| `hints` | hints object | `{category, language}` nullable — source-provided hints, pipeline inputs only, never conclusions |
| `raw` | JSON | original row passed through, never lost |

### Abstract interface: SourceAdapter

```
kind: str                                 source type id ("wdc" / "similar:<name>")
discover() -> [SourceFile]                enumerate available data files (mtime/size, incremental-friendly)
parse(file) -> Iterator[SourceRecord]     lazy per-record parsing (large files never fully loaded)
validate(rec) -> [Issue]                  record-level validation: URL sane, required fields, obvious junk
```

Invariants (adapter discipline):
1. **Read-only, side-effect-free**: no probing, no DB writes, no file changes
2. Probe/classify/DR all run downstream in the pipeline — **adding a source = one adapter + one YAML block, zero downstream changes**
3. Adapters never import downstream modules (one-way dependency: adapter → unified model)
4. Large files are produced lazily record-by-record (a single WDC product file can be tens of thousands of rows)

### Built-in adapters

| kind | input | status |
|------|-------|--------|
| `wdc_competitor_links` | `competitor-links-<center>.json` (product of import/harvest_competitor_links.py); evidence maps via_competitors→evidence.via_competitors, inbound→evidence.inbound | defined, shipped with baseline code |
| `similar_csv` / `similar_json` | generic mapping adapters for other similar harvested data (column/field mapping lives in YAML) | reserved, mapping pending samples |

Data flow: `SourceAdapter → SourceRecord → probe → classify → DR enrichment → PoolEntry (SQLite pool)`

---

## 4c. Submit-path cache: record, reuse, keep fresh (round 5)

Submit endpoints discovered during probing are **first-class assets**, not
transient probe output — they are persisted in `probe_cache` and reused:

**Record**
- Every discovered entry (URL + form signature + captcha type + contact email) lands in `probe_cache` at probe time
- `form_signature` = hash of the form's field names — the field mapping itself is the reusable piece
- Successful submissions bump `success_count`; the proven chain (entry → fields → captcha handling) is what gets replayed

**Reuse**
- Same domain matched again (new project, new submission round) → take the cached path directly, skip full re-probing (saves time and request quota)
- Same-CMS / same-template sites (identical `form_signature`) → reuse the field mapping, only verify the URL

**Keep fresh (update promptly on change)**
1. Light pre-submit check: HEAD/GET the cached `entry_url`; 404/redirect → re-probe before submitting
2. Submit failure caused by form mismatch (missing/renamed fields) → mark `suspect`, re-probe immediately
3. Low-frequency background audit (default every 30 days) keeps long-tail entries valid
4. Every change appends to `history` (old path → new path → reason → timestamp); never overwritten silently, fully auditable

---

## 5. Matching & scoring

```
score = w1×category match (0/1)
      + w2×language/geo match
      + w3×wave priority (wave1=1, wave2=0.6, wave3=0)
      + w4×DR normalized (dr/10)
      + w5×competitor-evidence prior (via_competitors/inbound from import)
      - exclusions (already submitted / rejected / excluded-domain list → never enqueued)
```

- Weights and threshold live in YAML, overridable per project
- Output enters the project's pending-confirmation queue sorted by score descending; scheduling is generated only after human selection
- Schedule generation: fill future dates from the confirmed pool respecting daily_quota

---

## 5a. Project assets: TDK, pages, articles, email copy (round 5)

Creating a project takes **just a domain**. The system then assembles four
asset kinds, each with a three-level fallback: `provided` (user supplied) →
`probed` (auto-discovered) → `template` (generic template).

| Asset | Used for | provided | probed | fallback |
|-------|----------|----------|--------|----------|
| **TDK** (title / description / keywords) | directory-form title/desc fields, outreach copy | user fills in | fetch homepage meta + og: tags | none — marked `missing`, flagged in the daily report for manual fill (TDK cannot be genericized) |
| **Level-1/2 page URLs** (specific paths under the domain) | landing-URL choice per submission | user lists them | sitemap/RSS/nav discovery → pending-confirmation queue | homepage only |
| **Blog articles** | guest_post / article submission bodies | site's article list | sitemap/RSS harvest | **generic article template** |
| **Outreach email copy** | wave2 email sends | project-specific template | — | **generic outreach email template** |

Rules:
- Templates live in `templates/` with placeholders (`{domain}`, `{title}`, `{description}`, `{url}`, `{page}`); rendering injects the project's own assets — a project with real assets never sees template text
- Probed assets (TDK, pages, articles) are **cached in `project_asset` and reused** — no refetch per submission, re-probe only on staleness
- Every asset row records its `source` (provided/probed/template), so the daily report can show "this project is running on N template-backed assets" as a quality signal
- Level-1/2 page discovery lands in the pending-confirmation queue (same gate as matching) — auto-discovered pages are confirmed by a human before being used as landing URLs

---

## 6. Config samples (YAML, not code)

```yaml
# global.yaml —— global
data_root: ~/outreach-scheduler-data   # user-data root: a folder named after THIS project only (never other private folders)
sources:
  competitor_links_glob: "data/sources/competitor-links-*.json"
  similar: []          # other similar sources, listed one by one (format/fields pending samples)
  exclude: [workbench_jsonl, prospector_store]   # explicitly not used
scheduler:
  run_at: "09:30"
  timezone: Asia/Shanghai
report:
  email: ""            # empty = not sent; enable by configuring
  smtp_env_file: config/secrets/smtp.env   # project-local, gitignored
dr:
  provider: openpagerank
  cache_days: 30
captcha:
  channel: cua         # default free CUA channel (local Chrome)
  solver_api: ""       # optional fallback; default empty = free-only mode
  api_key_env: SOLVER_API_KEY
  monthly_budget_usd: 0    # default 0 = paid solving off
quotas:                  # guardrails (R-522/R-530 conventions)
  per_domain_per_day: 100
  per_project_per_day: 10

# projects/<project-domain>.yaml —— one per project
domain: <project-domain>
tags: [gaming, browser-games, english]
daily_quota: 10
channels: [form, email]
templates: [dir-submit, outreach-mail]
scoring:
  min_score: 0.55
  weights: {category: 0.35, geo: 0.15, wave: 0.20, dr: 0.15, via_competitors: 0.15}
captcha:
  auto_types: [none, recaptcha_v3, turnstile, recaptcha_v2, image, email_code]
exclusions: []
start_date: 2026-11-20
```

---

## 7. Execution layer

- **Scheduling**: system cron launches the CLI (`scheduler run --date today`); no resident process, no external-toolchain dependency — this is the key choice for "independent of the host environment"
- **wave1 form submit**: HTTP form channel first (requests + form rules); sites requiring a real browser are CUA-class — dependent on local Chrome — and are **not automated in phase 1**; they go to the human queue (trade-off: HTTP first, independence over automation)
- **wave2 email auto-send**: SMTP, reusing Gmail app passwords (identity.py env pattern); quota key = domain (R-530 convention)
- **Failure handling**: retries ≤3 (5xx/network only; 4xx never retried); exhausted retries go to the human queue and appear in the daily report

### 7.1 Captcha recognition (added requirement; channel ratified: CUA free first)

Probe already tags every endpoint's captcha_type; execution handles by tier. **Free channels first; paid solver APIs are optional fallback only**:

| Type | Strategy | Cost |
|------|----------|------|
| none | submit directly | 0 |
| recaptcha_v3 / turnstile / invisible hcaptcha | CUA (local real Chrome) loads the page; real fingerprint passes naturally | 0 |
| recaptcha_v2 / hcaptcha image challenge | **CUA channel**: challenge recognized and clicked inside the real browser | 0 |
| image captcha (self-drawn glyphs) | local OCR first (free); on failure fall through to CUA | 0 |
| email_code | IMAP auto-fetch code and backfill (mail_verify.py pattern) | 0 |
| unknown / CUA failure | human queue | 0 |
| (optional fallback) paid solver API | enabled only if CUA throughput proves insufficient; unconfigured by default | ~$1-3/1k |

CUA channel notes:
- Depends on local Chrome + browser automation — the **only local-environment dependency** in the whole system (user-ratified)
- A browser session that solved a captcha can be reused for that site's form submission (same fingerprint → higher pass rate)
- Success rate / duration go to run_log and the daily report; sites failing repeatedly are auto-downgraded to the human queue
- Paid solver config defaults to empty = runs in fully free mode

Guardrails:
1. CUA concurrency = 1 (single machine, single browser); same-day queues run per-site serially to avoid triggering anti-bot by parallel challenges
2. Captcha failures are never retried (wastes quota) — straight to human queue
3. If a paid API is ever enabled: monthly budget cap in config; exceeding it auto-downgrades

### 7.2 Full-automation guardrails (wave2 email risks)

1. Two-tier daily quotas: per domain / per project
2. Warm-up period (new templates start low-volume for N days)
3. Bounced/refused domains auto-enter the exclusion list
4. Everything sent is auditable in run_log

---

## 8. Daily email report (configurable)

Five sections:
1. Today's plan (item counts, by project, by channel)
2. Yesterday's completions / failures detail
3. Pending-confirmation queue (new matches count, top-10 preview)
4. Pool changes (new entries, classification changes, DR updates, dead links)
5. Human-action items (human-queue entries, decisions needed)

Empty recipient = skip sending, run_log only. Send time follows scheduler config.

---

## 9. Independence & data isolation

### 9.1 No external-toolchain dependency
- Python3 stdlib + PyYAML; HTTP via requests (the only third-party, degradable to urllib)
- No external agent toolchain, no MCP, no existing-system code — reads external **product files** only (competitor-links-*.json etc.)
- Credentials read only from env vars / env files, never in the codebase
- **Paths never reference other private folders** (user rule, 2026-11-18): all paths point inside this project folder, or to a folder named after this project (e.g. the outreach-scheduler-data folder in the home directory); no home dot-folder paths and no sibling-repo paths

### 9.2 User-data vs repo-code isolation (hard rule)
- **GitHub carries zero private information**: repo contains source, docs, YAML structure samples (placeholder values) only
- **User data enters via "new-project import"**: the `import` CLI reads external data files (WDC products, harvested JSON/CSV) into the project-private `data/` (SQLite + raw files); `data/`, `config/secrets*`, `*.env` are all gitignored, never uploaded
- The data dir can move out of the repo entirely (set `data_root` to an external folder named after this project, like `~/outreach-scheduler-data/`) — machine moves / reinstalls only need config + data dir
- Pre-commit checklist (automated): private-scan checks staged files for emails/passwords/domain-ledger signatures; any hit blocks the commit
- **Domains never appear in file names or file contents** (user rule, 2026-11-18): product files carry neutral content-hashed names; the name mapping lives only in the gitignored local index; own-site/competitor domains in docs and code are replaced with placeholders
- Raw WDC graph data (~3GB) never enters the repo or git; dl.sh downloads to a local cache on demand

### 9.3 The only local dependency
- The CUA captcha channel depends on local Chrome + browser automation (ratified: CUA free); nothing else does — import/probe/match/plan/email are pure HTTP + SQLite
- CUA is used only for captchas and fingerprint-sensitive submissions; ordinary HTTP forms never launch a browser

### 9.4 Doc language convention
- English-primary `.md`; Chinese translations in `.zh.md` (e.g. `DESIGN.md` / `DESIGN.zh.md`)

---

## 10. Open items / next steps

0. **Round-5 ratification pending (2026-11-18):**
   - TDK probing fails (no meta at all): mark `missing` + daily-report reminder (current design) — or degrade to a domain-derived placeholder title?
   - Generic article template content strategy: original placeholder text vs. site-summary derived text?
   - Level-1/2 page URLs: manual input only, or sitemap candidates + human confirmation (current design)?
   - Submit-path freshness audit interval: 30 days default OK?

1. ~~Exact WDC input~~ **Confirmed (2026-11-18)**:
   - Input products = `competitor-links-<center>.json` (existing 4 product files copied into the project; file names neutralized by content hash; the mapping lives only in the local gitignored index)
   - Processing code copied as-is into `outreach-scheduler/import/` (harvest_competitor_links.py, competitor_backlinks.py, export_wdc_fast.py, dl.sh + README), no private info
   - Raw graph data ~3GB cached locally, never in git
   - "Other similar data" samples still pending (one JSON, one CSV)
2. **Paid solver**: default off (CUA free channel first); whether to abandon paid APIs entirely will be decided after measuring CUA throughput
3. Repo name confirmation: `outreach-scheduler` (alternatives: backlink-outreach-ops / bl-campaign-pilot)
4. git init + GitHub repo (hhool/outreach-scheduler): **before first push, land .gitignore (data/, *.env, config/secrets*) + private-scan hook** (§9.2 hard rule)
5. Phase 1 architecture decision list: directory structure, table DDL, competitor-links→pool field mapping (via_competitors/inbound kept as scoring priors), probe rule list
6. wave2 full-auto email needs a detail pass: templates & warm-up policy
7. First project: use the site with the richest products (findable via the local mapping), validating the full chain import → probe → match → confirm → execute → report
