# outreach-scheduler

外链外联计划调度中心：导入收割外链数据（WDC 竞品反链产物 + 同类数据）→ 池化分类 → 项目匹配 → 可配置发送计划 → 周期执行 → 每日邮件汇报。

英文版见 `README.md`。

**状态：功能层已交付——import → probe → classify → match（人工闸）→
schedule → execute（HTTP 表单 + CUA 通道 + wave2 SMTP）→ 日报，含 cron
接线。架构见 `DESIGN.zh.md`，运维与业务路线图见 [docs](docs/README.zh.md)。**

## 硬规则

1. **用户数据永不进仓库。** `data/`、`*.env`、`config/secrets*` 全部 gitignore；`data_root` 可指向仓库外——但只允许以本项目命名的文件夹（如 `~/outreach-scheduler-data/`），路径不得涉及其他私人文件夹。GitHub 只有源码。
2. **无外部工具链依赖。** 仅 Python3 标准库 + PyYAML + requests；只读外部产物文件，不 import 任何现有系统。
3. **验证码走 CUA 免费通道。** 本机真实 Chrome 自动完成图像/隐形挑战；付费打码可选、默认关闭。
4. **提交前必跑私有扫描。** `python3 tools/private_scan.py` 必须通过，命中即拒绝提交（邮箱、密码、私有域名、私有文件夹名、`~/` 点目录与绝对家目录路径）。
5. **文档语言规范。** 英文主文档 `.md`；中文翻译 `.zh.md`。

## 目录结构

```
bin/osched.py            CLI：import / probe / project / match / queue /
                         confirm / reject / classify / dr / schedule / plan /
                         execute / cua / verify / report / client-report /
                         account / mail-verify / account-activate /
                         profile / overview / asset / stats
config/global.example.yaml   配置模板——复制为 config/global.yaml
src/osched/
  config.py              YAML 配置 + env 凭据，fail-fast
  models.py              SourceRecord——统一导入模型（DESIGN.zh.md §4b）
  adapters/base.py       SourceAdapter 抽象接口（只读、惰性）
  adapters/wdc.py        wdc_competitor_links 适配器
  db.py                  SQLite 七表：pool/project/match/plan/run_log/
                         probe_cache/project_asset
  pool.py                导入管线（按 domain+url 去重）
  probe.py               HTTP 探测：提交入口、验证码识别、probe_cache
  classify.py            站点/提交类型、wave 与分类规则、DR 富化
  matching.py            打分、项目配置、落地 URL 选择
  scheduler.py           计划排期生成 + due_today 工作清单
  executor.py            计划执行：wave1 HTTP、验证码路由、wave2 SMTP
  cua.py                 CUA 通道：真实 Chrome 处理验证码类提交
  verify.py              实链回访台账（B1）
  report.py              五段式日报（§8）
  client_report.py       客户周期报告（B2）
  accounts.py            目录账号台账 + IMAP 收验证信（B3）
  profile.py             每域名可投递性画像与处置建议（B4）
  ops.py                 多客户运营视图 + SLA 告警（B5）
tools/private_scan.py    私有信息提交前扫描闸
import/                  WDC 收割基线代码（原样复制）+ prepare_discovery_snapshot.py；见 import/README.zh.md
scripts/daily.sh         cron 入口：pipeline / run 两段
docs/                    接单 / 运维 / 业务路线图（见 docs/README.zh.md）
```

## 快速开始

```bash
cp config/global.example.yaml config/global.yaml   # 然后改路径
python3 bin/osched.py import                        # 适配器 -> 池
python3 bin/osched.py stats                         # 池摘要
```

全流程：`docs/operations.zh.md`（cron）· `docs/client-intake.zh.md`
（接单）· `docs/business.zh.md`（下一步做什么）。

## 路线图（DESIGN.zh.md §10）

- [x] Phase 2：探测（入口/表单/验证码类型/联系邮箱识别）+ 分类
- [x] Phase 3：项目匹配 + 人工确认队列
- [x] Phase 4：排期执行器 + wave1 表单直投（HTTP 优先）+ wave2 SMTP
- [x] Phase 5：每日邮件汇报；DR 用 Open PageRank 免费层
- 下一阶段：业务层 B1–B6（商业化落地）——见 docs/business.zh.md
