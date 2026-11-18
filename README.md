# outreach-scheduler

Backlink outreach planning & scheduling system. Import harvested link data
(WDC competitor-link products + similar datasets), build a classified pool,
match projects by domain, generate configurable send plans, run them on a
periodic schedule, and email a daily report.

外链外联计划调度中心：导入收割外链数据 → 池化分类 → 项目匹配 → 可配置发送计划 → 周期执行 → 每日邮件汇报。

**Status: Phase 1 (import pipeline) — see `DESIGN.md` for the full design.**

## Hard rules

1. **User data never enters the repo.** `data/`, `*.env`, `config/secrets*` are
   gitignored; `data_root` may point outside the repo entirely. GitHub carries
   source code only — zero private information. 用户数据与代码隔离：仓库只有源码。
2. **No .workbuddy / MCP dependency.** Python3 stdlib + PyYAML + requests only;
   reads external *product files*, imports nothing from other systems.
   无 .workbuddy 依赖：只读外部产物文件，不 import 任何现有系统。
3. **Captcha solving is CUA-first and free.** Real-Chrome automation solves
   image/stealth challenges; paid solvers are optional and default-off.
   验证码识别走 CUA 免费通道，付费打码默认关闭。
4. **Pre-commit private scan.** `python3 tools/private_scan.py` must pass
   before every commit; any hit blocks the commit. 提交前必跑私有信息扫描。

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
python3 bin/osched.py import                        # adapters -> pool
python3 bin/osched.py stats                         # pool summary
```

## Roadmap (DESIGN.md §10)

- Phase 2: probe (entry/form/captcha-type/contact detection) + classify
- Phase 3: project matching + human confirmation queue
- Phase 4: plan scheduler + wave1 form submit (HTTP-first) + wave2 SMTP
- Phase 5: daily report email; DR via Open PageRank (free tier)
