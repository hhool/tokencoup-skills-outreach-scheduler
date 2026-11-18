# import/ — WDC 收割代码（自 backlink-prospector 原样复制）

复制时间：2026-11-18 · 来源：`/Users/hou/Desktop/skill-seo-geo/backlink-prospector`
性质：**原样复制，未改动**。outreach-scheduler 正式开发时以此为基线适配（改为读配置、导入 pool）。
已扫描确认：无密码/密钥/邮箱等私有信息。

## 文件清单与职责

| 文件 | 职责 | 原路径 |
|------|------|--------|
| `harvest_competitor_links.py` | WDC 2012 PLD 图收割主工具：读 pld-index.gz + pld-arc.gz，输出「谁链接过竞品域」候选（competitor-links-*.json） | tools/ |
| `competitor_backlinks.py` | 通用版收割：支持新版 WDC Hyperlink Graph（domain-ranks + domain-edges 格式，webdatamining.org 各 crawl window） | tools/ |
| `export_wdc_fast.py` | 快速导出：WDC 行不经 HTTP 校验直接导出候选（校验交给下游 scan/探测） | tools/ |
| `dl.sh` | WDC 原始图数据可断点续传下载器（429 礼貌退避 + Content-Length 校验） | data/wdc/ |

## WDC 数据链路（三层）

1. **原始图数据**（本地缓存，不进 git）：
   - `backlink-prospector/data/wdc/pld-index.gz`（~297MB）
   - `backlink-prospector/data/wdc/pld-arc.gz`（~2.8GB）
   - 2012-08 版：`https://data.dws.informatik.uni-mannheim.de/hyperlinkgraph/2012-08/`（dl.sh 下载）
   - 新版可从 webdatamining.org 取 domain-ranks/domain-edges 对（competitor_backlinks.py 支持）
2. **收割工具**（本目录 4 个文件）
3. **产物**（= outreach-scheduler 导入层的直接输入）：
   - `backlink-prospector/data/competitor-links-<center>.json`
   - 现有产物：***REMOVED-PRIVATE-DOMAIN*** / balancebiketoddler.com / soundbuttons.cc / soundbuttons.click
   - 格式：`{"meta": {...}, "rows": [{"url": "http://host/", "via_competitors": [...], "inbound": n}]}`

## 导入层适配要点（待开发）

- 输入 = competitor-links-*.json（以及未来其他同类收割 JSON/CSV）
- 导入后进入探测阶段（probe）：识别提交入口/表单/验证码类型/联系邮箱，才升级为可投递机会条目
- `via_competitors`（哪些竞品被它链过）与 `inbound` 是匹配打分的重要先验，入池时保留
