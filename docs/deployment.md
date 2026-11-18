# Deployment — fly.io background operation (no agent, no desktop needed)

The scheduler is a plain CLI + cron system. It runs wherever a Python 3.12
and a volume exist. This page covers the fly.io server deployment; the
desktop keeps exactly one duty (see the matrix below).

## What runs where

| duty | fly.io server | local desktop |
|------|---------------|---------------|
| import → probe → classify → dr → match → schedule | ✅ (pipeline cron) | optional |
| execute (wave1 HTTP form) | ✅ | optional |
| verify (B1 live-link re-visit) | ✅ | optional |
| mail-verify (B3 IMAP) | ✅ | optional |
| report + client-report (B2 email) | ✅ | optional |
| overview / profile / queue CLI | ✅ | optional |
| **cua (browser captcha, real Chrome)** | ❌ skipped automatically | ✅ required |
| **account-activate (B3 CUA click)** | ❌ skipped automatically | ✅ required |

The CUA guard in `scripts/daily.sh` detects Chrome and silently skips on
servers; state='cua' plans stay queued and are processed by a local pass
(`osched cua <project> --headless`) on any machine that has Chrome. The
data volume can be rsynced back for a local CUA pass, or run the local
daily.sh against the same volume over a mounted share.

**CUA-on-desktop is the ratified near-term approach** (2026-11-18): it
serves the current sites today and is acceptable for early-stage
commercial operation. The long-term plan is to replace it with an
open-source or commercial captcha/submission solution once volume
justifies it — the executor's captcha-tier routing (§7.1) is the only
touch point, so swapping the channel is config-plus-one-module work.

## Deploy steps

```bash
cd outreach-scheduler
cp fly.toml.example fly.toml          # set your app name + region
fly volumes create osched_data --size 1 --region hkg
fly deploy

# config (never committed: contains client mailbox addresses)
fly ssh sftp shell -C "mkdir -p /data/config"
cat config/global.yaml | fly ssh sftp shell -C "cat > /data/config/global.yaml"

# secrets — os.environ wins over env files (require_env_key order)
fly secrets set \
  SMTP_HOST=smtp.gmail.com SMTP_PORT=465 \
  SMTP_USER='...' SMTP_APP_PASSWORD='...' \
  IMAP_HOST=imap.gmail.com IMAP_PORT=993 \
  OPENPAGERANK_API_KEY='...'        # optional; note IP binding below

# verify
fly ssh console -C "ls /data && scripts/daily.sh run"
```

Notes:
- **OPR IP binding**: the Open PageRank free key is bound to one IP. A fly
  region egress IP is stable per app, so rebind once to the fly IP and the
  monthly batch enrichment finally works unattended (currently on hold).
- **Gmail from a datacenter IP**: first sends may hit Google's "suspicious
  sign-in" heuristics; the app-password flow usually passes, otherwise
  send report from a local run until trusted.
- Timezone: set `TZ` in fly.toml so cron hours mean what you expect.

## Agent-free operation (ratified 2026-11-18)

The whole system is CLI + cron + sqlite. No WorkBuddy dependency anywhere:
no MCP, no .workbuddy paths, no agent session state. Anything that can run
`python3 bin/osched.py …` on a cron schedule is a complete deployment.

## AI layer hook points (later phase, ratified direction)

The backend will add AI for client work (requirement/case matching,
planning, capability matching). The stable contracts an AI layer plugs
into are already in place:

| AI capability | existing contract it plugs into |
|---------------|--------------------------------|
| client intake parsing (domain, TDK, assets from free text) | `project` YAML + `asset` CLI + §5a assets |
| plan drafting / outreach copy | `templates` chain (project → data_root → generic), wave2 result_note carries `template` |
| site selection judgement | `match` scoring + pending queue (`confirm`/`reject` CLI is the human gate) |
| ops narration (client-facing summaries) | `run_log` + `client_report.py` section builders |
| capability queries ("what can you do for X") | `overview`/`profile`/`queue` JSON-able outputs |

Design rule: AI is a caller of the CLI/DB contracts, never a bypass of the
human gates (confirm, captcha escalate, R-520 manual send).
