#!/bin/bash
# Resumable WDC downloader with polite backoff on 429.
# v2: success requires BOTH a 2xx/416 code AND curl exit 0 — a mid-transfer
# disconnect still reports the initial 206 via -w %{http_code}, which made
# v1 declare truncated files "done". Size is also verified against the
# server's Content-Length before accepting.
base=https://data.dws.informatik.uni-mannheim.de/hyperlinkgraph/2012-08
expected() { curl -sI "$base/$1" --max-time 60 | awk 'tolower($1)=="content-length:"{gsub("\r","");print $2}'; }
for f in pld-index.gz pld-arc.gz; do
  want=$(expected "$f")
  while :; do
    code=$(curl -s -C - -o "$f" -w "%{http_code}" "$base/$f")
    rc=$?
    sz=$(stat -f%z "$f" 2>/dev/null || echo 0)
    if [ "$rc" -eq 0 ] && { [ "$code" = "200" ] || [ "$code" = "206" ] || [ "$code" = "416" ]; }; then
      if [ -n "$want" ] && [ "$sz" != "$want" ]; then
        echo "$(date +%H:%M:%S) $f: transfer ended but $sz/$want bytes, reconnecting in 60s"
        sleep 60; continue
      fi
      echo "$f done ($sz bytes, expected $want)"; break
    fi
    if [ "$code" = "429" ] || [ "$code" = "000" ] || [ "$rc" -ne 0 ]; then
      echo "$(date +%H:%M:%S) $f: HTTP $code rc=$rc, size=$sz/$want, backoff 600s"
      sleep 600
      continue
    fi
    echo "$f: unexpected HTTP $code rc=$rc, backoff 120s"; sleep 120
  done
done
echo ALL_DONE
