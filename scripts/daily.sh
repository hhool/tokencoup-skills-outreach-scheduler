#!/usr/bin/env bash
# Daily pipeline entry point for system cron (DESIGN.md §7).
#
# Two segments:
#   pipeline  — supply side: import -> probe -> classify -> match -> schedule
#               (run less often, e.g. weekly or after new source data lands)
#   run       — demand side: execute due plans + daily report (cron daily)
#
# Cron example (09:30 daily per config scheduler.run_at):
#   30 9 * * * /path/to/outreach-scheduler/scripts/daily.sh run >> /tmp/osched.cron.log 2>&1
#   0 9 * * 1 /path/to/outreach-scheduler/scripts/daily.sh pipeline >> /tmp/osched.cron.log 2>&1
#   0 8 1 * * /path/to/outreach-scheduler/scripts/daily.sh pipeline >> /tmp/osched.cron.log 2>&1
#     (first-of-month extra pass = B6 supply refresh; pipeline is idempotent,
#      new competitor data auto-flows to the confirmation queue)
#
# 中文：cron 入口——pipeline=供给段（低频），run=执行段（每日 execute+report）。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
PY=python3

all_projects() {
  ls data_root/projects/*.yaml 2>/dev/null | while read -r f; do
    basename "$f" .yaml
  done
}

projects() {
  # Projects come from data_root; data_root may point outside the repo.
  cfg_root=$($PY - <<'EOF'
import sys
sys.path.insert(0, "src")
from osched.config import load_config, data_root
print(data_root(load_config()) / "projects")
EOF
)
  ls "$cfg_root"/*.yaml 2>/dev/null | while read -r f; do
    basename "$f" .yaml
  done
}

segment="${1:-run}"

case "$segment" in
  pipeline)
    echo "== pipeline start $(date -u +%FT%TZ) =="
    $PY bin/osched.py import
    $PY bin/osched.py probe
    $PY bin/osched.py classify
    $PY bin/osched.py dr --limit 100 || true   # fail-open when key unbound
    for p in $(projects); do
      $PY bin/osched.py match "$p"
      $PY bin/osched.py schedule "$p"
    done
    $PY bin/osched.py overview   # B5: ops snapshot after supply refresh
    echo "== pipeline done =="
    ;;
  run)
    echo "== daily run start $(date -u +%FT%TZ) =="
    $PY bin/osched.py verify || true   # B1: re-visit old submissions for live links
    for p in $(projects); do
      $PY bin/osched.py mail-verify "$p" || true   # B3: fetch verification mails
      $PY bin/osched.py execute "$p" || true
      # CUA channel needs local real Chrome — desktop machines only.
      # On a server (fly.io) this is skipped; state='cua' plans wait for a
      # local pass (see docs/deployment.md).
      if [ -d "/Applications/Google Chrome.app" ] \
         || command -v google-chrome >/dev/null 2>&1 \
         || command -v google-chrome-stable >/dev/null 2>&1; then
        $PY bin/osched.py cua "$p" --headless || true
        $PY bin/osched.py account-activate "$p" --headless || true   # B3
      fi
    done
    $PY bin/osched.py report
    $PY bin/osched.py overview   # B5: per-client ops view + SLA alerts
    echo "== daily run done =="
    ;;
  *)
    echo "usage: daily.sh [pipeline|run]" >&2
    exit 2
    ;;
esac
