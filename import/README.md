# import/ — WDC harvest baseline code (copied as-is)

Copied: 2026-11-18 · Source: internal harvest repository (paths live in the
local ledger, never in the repo).
Nature: **copied as-is, unmodified**. When outreach-scheduler is formally
developed, these become the baseline to adapt (read from config, import into
the pool).
Scanned and confirmed: no passwords / keys / emails or other private info.

Chinese version: see `README.zh.md`.

## Files & responsibilities

| File | Role | Original location |
|------|------|-------------------|
| `harvest_competitor_links.py` | Main harvester for the WDC 2012 PLD graph: reads pld-index.gz + pld-arc.gz, outputs "who linked to competitor domains" candidates (competitor-links-*.json) | tools/ |
| `competitor_backlinks.py` | Generic harvester: supports the newer WDC Hyperlink Graph (domain-ranks + domain-edges format, webdatamining.org crawl windows) | tools/ |
| `export_wdc_fast.py` | Fast export: WDC rows exported without HTTP verification (verification is delegated to downstream probe) | tools/ |
| `dl.sh` | Resumable downloader for raw WDC graph data (polite 429 backoff + Content-Length check) | data/wdc/ |

## WDC data chain (three layers)

1. **Raw graph data** (local cache, never in git):
   - `pld-index.gz` (~297MB) and `pld-arc.gz` (~2.8GB)
   - 2012-08 edition: `https://data.dws.informatik.uni-mannheim.de/hyperlinkgraph/2012-08/` (via dl.sh)
   - newer editions: domain-ranks/domain-edges pairs from webdatamining.org (competitor_backlinks.py supports them)
2. **Harvest tools** (the 4 files in this directory)
3. **Products** (= the direct input of outreach-scheduler's import layer):
   - product files are copied into this project's `data/sources/`, names
     neutralized by content hash (`competitor-links-<hash>.json`); the
     original-name mapping lives only in the local gitignored
     `data/sources/index.json`
   - format: `{"meta": {...}, "rows": [{"url": "http://host/", "via_competitors": [...], "inbound": n}]}`

## Adaptation notes for the import layer (to be developed)

- Input = competitor-links-*.json (and future similar harvested JSON/CSV)
- After import comes the probe stage: identify submit endpoints / forms /
  captcha types / contact emails before an entry is upgraded to a submittable
  opportunity
- `via_competitors` (which competitors it linked to) and `inbound` are
  important scoring priors — preserved at pool import
