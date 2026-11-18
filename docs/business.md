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

## Backlog snapshot (2026-11-18)

| item | status |
|------|--------|
| functional layer (import→report, cron, CUA) | shipped, pushed |
| OPR DR enrichment | pending IP binding decision (VPN exit, on hold) |
| wave2 SMTP live send | ready; awaiting first wave2 plan |
| real-site full run | pending queue has v3/v2 captcha candidates |
