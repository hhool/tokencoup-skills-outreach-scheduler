# outreach-scheduler

Backlink outreach planning & scheduling system. Import harvested link data
(WDC competitor-link products + similar datasets), build a classified pool,
match projects by domain, generate configurable send plans, run them on a
periodic schedule, and email a daily report.

Chinese version: see `README.zh.md`.

**Status: functional layer shipped — import → probe → classify → match
(human gate) → schedule → execute (HTTP form + CUA channel + wave2 SMTP)
→ daily report, with cron wiring. See `DESIGN.md` for architecture and
[docs](docs/README.md) for operations & the business roadmap.**

## Hard rules

1. **User data never enters the repo.** `data/`, `*.env`, `config/secrets*` are
   gitignored; `data_root` may point outside the repo — but only to a folder
   named after this project (e.g. `~/outreach-scheduler-data/`). Paths never
   reference other private folders. GitHub carries source code only.
2. **No external-toolchain dependency.** Python3 stdlib + PyYAML + requests
   only; reads external *product files*, imports nothing from other systems.
3. **Captcha solving is CUA-first and free.** Real-Chrome automation solves
   image/stealth challenges; paid solvers are optional and default-off.
4. **Pre-commit private scan.** `python3 tools/private_scan.py` must pass
   before every commit; any hit blocks the commit (emails, passwords, private
   domains, private folder names, `~/` dot-folder and absolute home paths).
5. **Doc language.** English-primary `.md`; Chinese translations in `.zh.md`.

## Layout

```
bin/osched.py            CLI: import / probe / project / match / queue /
                         confirm / reject / classify / dr / schedule / plan /
                         execute / cua / verify / report / client-report /
                         account / mail-verify / account-activate /
                         profile / overview / asset / stats
config/global.example.yaml   config template — copy to config/global.yaml
src/osched/
  config.py              YAML config + env-file credentials, fail-fast
  models.py              SourceRecord — unified import model (DESIGN.md §4b)
  adapters/base.py       SourceAdapter abstract interface (read-only, lazy)
  adapters/wdc.py        wdc_competitor_links adapter
  db.py                  SQLite schema: pool/project/match/plan/run_log/
                         probe_cache/project_asset
  pool.py                import pipeline (dedupe by domain+url)
  probe.py               HTTP probe: submit endpoints, captcha typing, cache
  classify.py            site/submit type, wave & category rules, DR enrich
  matching.py            scoring, project config, landing-URL selection
  scheduler.py           dated plan generation + due_today worklist
  executor.py            plan execution: wave1 HTTP, captcha routing, wave2 SMTP
  cua.py                 CUA channel: real-Chrome captcha-class submission
  verify.py              live-link re-visit ledger (B1)
  report.py              five-section daily report (§8)
  client_report.py       client-facing periodic report (B2)
  accounts.py            directory account ledger + IMAP verification-mail fetch (B3)
  profile.py             per-domain submittability profile & suggestions (B4)
  ops.py                 multi-client operations view + SLA alerts (B5)
tools/private_scan.py    private-information pre-commit gate
import/                  baseline WDC harvest code (as-is) + prepare_discovery_snapshot.py; see import/README.md
scripts/daily.sh         cron entry: pipeline & run segments
docs/                    client-intake / operations / business roadmap (see docs/README.md)
```

## Quick start

```bash
cp config/global.example.yaml config/global.yaml   # then edit paths
python3 bin/osched.py import                        # adapters -> pool
python3 bin/osched.py stats                         # pool summary
```

Full loop: `docs/operations.md` (cron) · `docs/client-intake.md`
(onboarding) · `docs/business.md` (what to build next).

## Roadmap (DESIGN.md §10)

- [x] Phase 2: probe (entry/form/captcha-type/contact detection) + classify
- [x] Phase 3: project matching + human confirmation queue
- [x] Phase 4: plan scheduler + wave1 form submit (HTTP-first) + wave2 SMTP
- [x] Phase 5: daily report email; DR via Open PageRank (free tier)
- Next: business layer B1–B6 (commercialization) — see docs/business.md
