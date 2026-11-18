# outreach-scheduler

Backlink outreach planning & scheduling system. Import harvested link data
(WDC competitor-link products + similar datasets), build a classified pool,
match projects by domain, generate configurable send plans, run them on a
periodic schedule, and email a daily report.

Chinese version: see `README.zh.md`.

**Status: Phase 1 (import pipeline) — see `DESIGN.md` for the full design.**

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
bin/osched.py            CLI entry (import / stats)
config/global.example.yaml   config template — copy to config/global.yaml
src/osched/
  config.py              YAML config + env-file credentials, fail-fast
  models.py              SourceRecord — unified import model (DESIGN.md §4b)
  adapters/base.py       SourceAdapter abstract interface (read-only, lazy)
  adapters/wdc.py        wdc_competitor_links adapter
  db.py                  SQLite schema: pool/project/match/plan/run_log
  pool.py                import pipeline (dedupe by domain+url)
tools/private_scan.py    private-information pre-commit gate
import/                  baseline WDC harvest code (copied as-is; see import/README.md)
```

## Quick start

```bash
cp config/global.example.yaml config/global.yaml   # then edit paths
cp config/secrets/smtp.env.example config/secrets/smtp.env   # when email report is needed
python3 bin/osched.py import                        # adapters -> pool
python3 bin/osched.py stats                         # pool summary
```

## Roadmap (DESIGN.md §10)

- Phase 2: probe (entry/form/captcha-type/contact detection) + classify
- Phase 3: project matching + human confirmation queue
- Phase 4: plan scheduler + wave1 form submit (HTTP-first) + wave2 SMTP
- Phase 5: daily report email; DR via Open PageRank (free tier)
