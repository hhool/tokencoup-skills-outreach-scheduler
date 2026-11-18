#!/bin/sh
# fly single-VM entrypoint: cron (supercronic, background) + portal HTTP
# server (foreground, PID kept for fly health checks). One volume, one
# machine — see docs/deployment.md duty matrix.
supercronic /crontab >> /tmp/osched.cron.log 2>&1 &
exec python3 bin/osched.py portal --host 0.0.0.0 --port 8080
