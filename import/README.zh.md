# import/ — WDC 收割基线代码（原样复制）

复制时间：2026-11-18 · 来源：内部收割仓库（路径见本地台账，不入库）。
性质：**原样复制，未改动**。outreach-scheduler 正式开发时以此为基线适配（改为读配置、导入 pool）。
已扫描确认：无密码/密钥/邮箱等私有信息。

英文版见 `README.md`。

## 文件清单与职责

| 文件 | 职责 | 原位置 |
|------|------|--------|
| `harvest_competitor_links.py` | WDC 2012 PLD 图收割主工具：读 pld-index.gz + pld-arc.gz，输出「谁链接过竞品域」候选（competitor-links-*.json） | tools/ |
| `competitor_backlinks.py` | 通用版收割：支持新版 WDC Hyperlink Graph（domain-ranks + domain-edges 格式，webdatamining.org 各 crawl window） | tools/ |
| `export_wdc_fast.py` | 快速导出：WDC 行不经 HTTP 校验直接导出候选（校验交给下游探测） | tools/ |
| `dl.sh` | WDC 原始图数据可断点续传下载器（429 礼貌退避 + Content-Length 校验） | data/wdc/ |

## WDC 数据链路（三层）

1. **原始图数据**（本地缓存，不进 git）：
   - `pld-index.gz`（~297MB）与 `pld-arc.gz`（~2.8GB）
   - 2012-08 版：`https://data.dws.informatik.uni-mannheim.de/hyperlinkgraph/2012-08/`（dl.sh 下载）
   - 新版可从 webdatamining.org 取 domain-ranks/domain-edges 对（competitor_backlinks.py 支持）
2. **收割工具**（本目录 4 个文件）
3. **产物**（= outreach-scheduler 导入层的直接输入）：
   - 产物文件已复制进本工程 `data/sources/`，文件名按内容哈希中性化（competitor-links-<hash>.json），
     原始文件名↔中性名的映射表仅存本地 `data/sources/index.json`（gitignored，不入库）
   - 格式：`{"meta": {...}, "rows": [{"url": "http://host/", "via_competitors": [...], "inbound": n}]}`

## 发现层快照源（2026-11-20 新增）

并列的第二类 WDC 源：v0.3 共链/出链图谱产出的**发现层候选**。它不是竞品反链，也不能被描述成竞品反链。

- 产物：`colink_cross_validated.json`（12,133 域名 / 9 行业；字段 `domain`、`industry`、`inlink_support`、`dir_llr`、`dir_prob`、`outlink_*`、`composite`）。
- 准备：`python3 import/prepare_discovery_snapshot.py --input <产物路径> --version 0.3`，校验后写入 `data/sources/wdc-discovery-v0.3.json`（gitignored）。工作台路径不进提交配置。
- 配置块：`sources.wdc_discovery_snapshot`（`glob`、可选 `snapshot`）。
- 适配器：`src/osched/adapters/wdc_discovery.py`，kind 为 `wdc_discovery`。
- 字段映射：`inlink_support` → `evidence.inbound`（同行业种子共链数，不是竞品链接）；`via_competitors` 恒为空；`corpus` = 快照版本；全部图谱字段保留在 `hints["discovery"]` 与 `raw`。
- 打分：`matching.discovery_prior()` 只对发现层行读取图谱 hints，上限 0.75；与竞品证据/人工 fit 用 `max()` 合并（绝不累加），非发现层行打分完全不变，图谱证据也不能单独触发强证据加成。行业→项目标签匹配尚未接入。
- 诚实边界：发现层候选**不等于**可投递平台；入池后仍需 probe/classify 决定是否升级为机会条目。图谱不提供可提交性/存活反馈，不伪造该层。

## 导入层适配要点（待开发）

- 输入 = competitor-links-*.json（以及未来其他同类收割 JSON/CSV）
- 导入后进入探测阶段（probe）：识别提交入口/表单/验证码类型/联系邮箱，才升级为可投递机会条目
- `via_competitors`（哪些竞品被它链过）与 `inbound` 是匹配打分的重要先验，入池时保留
