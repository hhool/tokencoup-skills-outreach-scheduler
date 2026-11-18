# Business layer — commercialization roadmap

**Positioning: the business layer is the commercialization track.** The
functional layer (import → probe → match → execute → report) is the
*machine*; the business layer is what turns the machine into a client
service that can be sold, delivered and renewed. Every item below either
produces client-visible deliverables (B1/B2), extends deliverable service
capacity (B3/B4), enables scaling to more clients (B5), or secures the
supply that the service resells (B6).

Derived from the shipped functional layer. This doc records the ratified
discussion: which business features build on that layer, in what order,
and why. No functional-layer rework is implied.

中文版：business.zh.md。

## Commercialization mapping

| commercialization question | answers |
|---------------------------|---------|
| 客户凭什么付费？ What does the client pay for? | B1 live-link ledger + B2 client report |
| 服务能力边界在哪？ What can we deliver? | B3 account lifecycle + B4 submittability |
| 规模化靠什么？ How does it scale? | B5 multi-client ops view |
| 货源谁保障？ Who secures supply? | B6 competitor refresh |

## Priority order: 1 → 2 → 3 first (proof-of-value closed loop), then 4/5 (efficiency), 6 (supply).

---

## B1. Live-link verification ledger  (priority 1)

**Submission ≠ live link.** After a plan reaches `submitted`/`sent`, re-visit
the directory page after N days and confirm the client's link actually
appears; produce a deliverable "live links list". Misses auto-retry or fall
to the human queue.

- Built on: plan state machine (new `live` state after `submitted`) +
  probe re-visit machinery (probe_cache already has the 30-day audit
  framework) + the CUA channel can reuse the same browser run.
- Value: the only hard evidence a client pays for; without it the daily
  report's "submitted" is a process metric, not a result metric.

## B2. Client report  (priority 2)

A client-facing filtered view of run_log: submissions, success rate,
**live-link list**, items awaiting the client's decision. Emailed to the
client summary mailbox on a weekly/monthly digest.

- Built on: report.py five-section skeleton + filter/render layer +
  report.email (client summary-role mailbox).
- Value: the retention/renewal communication artifact; implements the
  "client report" row in client-intake §3.

## B3. Directory account lifecycle  (priority 3)

Many directories require a registered account before submission. Close the
loop on the registration-role mailbox: fetch verification mails (IMAP),
activate accounts, keep a credential ledger, reuse aged accounts.

- Built on: contact-email asset (registration role) + the SMTP/IMAP
  credential model (`email_code_imap` is already reserved in config).
- Value: unlocks the large "register-first" directory segment; accounts
  become reusable assets.

## B4. Submittability profile & template attribution  (priority 4)

Aggregate execution failures (bot-blocked / captcha / no-form / 4xx) into a
per-domain submittability profile feeding min_score and exclusion lists;
rotate wave2 templates and attribute by approval rate.

- Built on: run_log already records every failure detail — this is an
  aggregation layer only.
- Value: quota is finite (100 addresses/domain/day); spend it where forms
  actually pass.

## B5. Multi-client operations view  (priority 5)

Per-client aggregation table: pending / confirmed / submitted / live /
human-queue, plus a client-level SLA (minimum daily submissions) with
alerts on shortfall.

- Built on: project + plan tables; a pure query layer.
- Value: surfaced by the three simulated clients; the operations base
  before scaling beyond a handful of clients.

## B6. Competitor-driven supply refresh  (priority 6)

Upgrade WDC competitor harvesting from manual import to a scheduled
refresh: new directories flow into the pool → probe → match → confirmation
queue automatically; humans only confirm.

- Built on: import chain + cron `pipeline` segment already reserves a slot.
- Value: keeps backlink supply from going dry.

---

## Ratified mailbox-role context (client-intake §5.4)

The client mailbox carries two roles — registration (contact-email asset)
and periodic summary (report.email) — and one mailbox may serve both.
B2's recipient is the summary role; B3's credentials are the registration
role. Splitting into two mailboxes later is config-only.

## Strategy context (ratified 2026-11-18)

- **Why this exists**: the served sites must pass AdSense review and grow
  on their own traffic; backlinks are the traffic engine behind that goal.
  Every commercial-layer feature traces back to "more real links, proven
  to the client".
- **Operations mode**: backend runs on fly.io (HTTP-side pipeline, cron,
  sqlite volume) — fully agent-free (no assistant-platform dependency).
  CUA (real-Chrome captcha) stays on the desktop for the current sites and
  is acceptable in early commercial stage; later replace with an
  open-source or commercial captcha solution (single touch point: §7.1
  routing). See docs/deployment.md for the duty matrix.
- **AI layer (later phase)**: the backend will add AI for client
  requirement/case matching, planning, and capability matching. AI plugs
  into the existing CLI/DB contracts as a caller, never bypassing human
  gates. Hook-point table in docs/deployment.md.

## Backlog snapshot (2026-11-18)

| item | status |
|------|--------|
| functional layer (import→report, cron, CUA) | shipped, pushed |
| **B1 live-link ledger** (`verify` CLI, `link_verify` table, plan state `live`, in daily.sh run) | **shipped** |
| **B2 client report** (`client-report` CLI, `client_report.py`, per-project `report.email`) | **shipped** — real send verified |
| **B3 account lifecycle (phases 1-2: ledger + mail-verify + CUA activation click)** | **shipped** — real inbox verified, pending -> active end-to-end; phase 3 candidates: registration automation, aged-account reuse |
| **B4 submittability profile** (`profile` CLI, `profile.py`) | **shipped** — failure-mix classification, per-domain recommendation (preferred/keep/bot-blocked/exclude); template attribution fills as wave2 data accumulates |
| **B5 multi-client operations view** (`overview` CLI, `ops.py`, in daily.sh run+pipeline) | **shipped** — per-client stage counts, quota usage, idle/human-queue SLA alerts |
| **B6 competitor supply refresh** (monthly `pipeline` cron pass) | **shipped** — idempotent pipeline auto-flows new competitor data to the confirmation queue; humans only confirm |
| OPR DR enrichment | pending IP binding decision (VPN exit, on hold) |
| wave2 SMTP live send | ready; awaiting first wave2 plan |
| real-site full run | pending queue has v3/v2 captcha candidates |

---

## Competitor landscape & market verdict (ratified 2026-11-18)

Scope: 2C/2B × overseas/domestic. Findings from a structured sweep of the
existing link-building tool market, distilled into judgments that constrain
pricing, packaging and the captcha-replacement plan.

### Market matrix

| quadrant | incumbent examples | what they sell | verdict for us |
|----------|-------------------|----------------|----------------|
| overseas 2C tools | open-source scrapers/submitters, browser-automation frameworks | free self-serve tooling | do NOT compete — free/open-source saturates the low end at $0 |
| overseas 2B SaaS | Respona, Pitchbox-style outreach platforms | per-link pricing roughly $100–500/link, campaign management | benchmark only; our differentiation is the **live-link closed loop** (submission → re-visit → verified live), which none of them productizes |
| overseas 2B done-for-you | link-building agencies | per-link deliverables, manual QA | direct comparables for pricing our service tier |
| domestic 2C | none in this category | — | no directory-link category exists |
| domestic 2B | 友链 exchange ecosystems (platforms + manual trades) | friend-link placement, not directory submission | different category; not a competitor, a supply channel option |

### Judgments

1. **No 2C tool-selling.** The open-source tier already prices at zero; a
   paid 2C tool has no wedge. Tooling stays internal.
2. **Sell 2B service, priced against Resona-class SaaS.** The market anchor
   is ~$100–500 per delivered link. Our edge is evidence: the live-link
   ledger (B1) plus the client report (B2) prove the link exists — no
   competitor in the sweep closes that loop automatically.
3. **CUA replacement candidate: open-source real-Chrome engines.** The
   ratified desktop-only captcha phase has a named successor candidate —
   an open-source, MCP-exposed real-Chrome automation engine (the class of
   engine behind commercial "AI browser operator" products). Interface is
   compatible with the §7.1 routing touch point; swap is config-level.
   Re-evaluate when commercial volume justifies it.
4. **The pool is a durable asset.** ~55k directory pool + probe_cache
   verdict history compounds with every run; it is the moat a new entrant
   cannot copy by installing a tool.
5. **Domestic market: no directory category.** Domestic demand is served
   by the 友链 ecosystem instead. Treat domestic as a possible supply or
   resale channel, not a product market.

### Pricing (ratified 2026-11-18)

Model: **pay per delivered live link, no subscription.** Packaged as a
monthly credit allowance — `quota_total` = package size, only `live`
verdicts consume credits (portal View A implements this).

Anchor: Respona-class SaaS at $100–500/link; done-for-you agencies at
per-link rates with manual QA. Our position: **cheaper per live link than
the SaaS tier, harder proof than the agency tier** (the live-link ledger
plus client report is the invoice receipt — only verified-live links are
billed).

Tiers (recommended defaults, numbers adjustable):

| tier | monthly volume | price per live link | notes |
|------|----------------|--------------------:|-------|
| Trial | 3 links | free | portal self-serve, 14-day window, once per mailbox + site + network (see §Trial guards) |
| Starter | 1–9 links | $80 | below the $100–500 anchor floor |
| Growth | 10–49 links | $65 | main commercial tier |
| Scale | 50+ links | $50 | volume floor; covers pool-marginal quality |

Service guarantee: a link that never reaches `live` after auto-retry +
human fallback is **not billed**. Non-live states (`missing`,
`unreachable`, `bot-blocked`) are work-in-progress, never billed.

### Trial guards (ratified 2026-11-18)

The free trial is 3 live links, and it is the only thing on the price
sheet that can be taken without paying. Three signals are therefore
required to be *fresh* before a project receives it, and each grant is
written to `trial_ledger`:

| signal | normalisation | rule |
|--------|---------------|------|
| mailbox | lowercase, `+tag` stripped, gmail dots stripped, googlemail≡gmail | one trial per mailbox, ever |
| site | registrable domain (eTLD+1), `www.` dropped, IDNA | one trial per site; sub-domains collapse into the parent |
| network | client IP (`Fly-Client-IP` / `CF-Connecting-IP`) | one trial per IP across all accounts |

Refusal is never a dead end: the project still activates — it just starts
at 0 credits, `project_request.trial_note` records the reason, and the
admin console shows it. Manual approval keeps the mailbox/site checks and
skips only the network rule (the operator's IP is not the client's);
an operator can then set quota by hand after review.

Account-creation is throttled too: 5 registrations per IP per 24h
(admin-whitelisted addresses exempt), and one account may hold at most
5 unverified draft projects.

**Enforcement, not just bookkeeping.** `quota_total` is a hard ceiling:
the scheduler refuses to schedule more plans than remaining credits
(`generate_plan`) and the executor refuses to run more than the credits
left (`due_today`). A delivered link consumes exactly one credit
(`quota_used` mirrors `plan.state='live'`). Projects with no portal
request — our own sites, imports — are uncapped, and so is any request
marked `quota_type='internal'`.

Consequence for the business model: "3 free links" cannot be farmed by
opening new accounts, aliasing the mailbox, or splitting a site into
sub-domains, and no amount of couponing turns into unbounded free work.

### Brand (ratified 2026-11-18)

**LinkAttest** — production domain `linkattest.com` (confirmed
unregistered via whois; purchase pending, an external user action).
The name encodes the differentiation directly: *every link attested
live* — the live-link ledger is the attestation, and the attestation is
what the invoice cites. Internal repo/codename (`outreach-scheduler`,
`osched`) stays unchanged; the brand is the external face only.

Positioning formula in brand terms: **cheaper per live link than
Respona-class SaaS, harder attestation than done-for-you agencies.**

### Payments (ratified 2026-11-18)

**Constraint: no Hong Kong entity, no overseas entity.** Stripe direct
onboarding requires a HK/US/UK etc. registered entity, so the
self-serve-Stripe route is **ruled out** until an entity exists.
Competitor evidence: overseas 2B sellers (agency / done-for-you class)
collect by invoice + bank transfer + Wise/Payoneer — no Stripe needed at
our deal sizes; 2C self-serve (alternativeto/producthunt-style listings)
uses Stripe because those buyers swipe a card at $20–50.

The billing model (per delivered live link, no subscription, invoice-cited
by the live-link ledger) fits manual B2B collection naturally — the
ledger IS the invoice attachment.

Matrix by phase and market:

| phase | overseas clients | domestic clients |
|-------|------------------|------------------|
| now (manual) | invoice + bank transfer / Wise / Payoneer; quote in USD | 对公转账 + 增值税发票；quote in CNY (rate set at quote time) |
| self-serve upgrade | **MoR: Paddle or Lemon Squeezy** — they become the merchant of record, handle tax/compliance, accept cards, pay out to a CN entity by wire/Payoneer. No HK/US entity needed on our side. | unchanged: 对公转账 stays (domestic 2B buyers expect it) |
| only if an overseas entity appears later | may add Stripe Invoicing alongside MoR (not before) | unchanged |

Decision rules:

1. Package request (portal) → admin marks done → **offline payment**
   (transfer/Wise/对公) → admin sets paid credits. This shipped loop is
   the payment flow for the manual phase; no gateway integration needed.
2. Do NOT integrate a payment gateway for the manual phase — the request
   loop is already the contract; gateway adds PCI/subscription surface we
   don't sell (no subscriptions).
3. When self-serve checkout is justified by volume, pick ONE MoR
   (Paddle preferred: stronger B2B invoicing + checkout localization;
   Lemon Squeezy simpler but thinner invoice support). Productize only
   the Starter tier; Growth/Scale stay quote-based (volume negotiation
   is part of the price).
4. Domestic and overseas never mix ledgers: separate quotes, separate
   currencies, separate invoices.
