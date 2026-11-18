# outreach-scheduler

外链外联计划调度中心：导入收割外链数据（WDC 竞品反链产物 + 同类数据）→ 池化分类 → 项目匹配 → 可配置发送计划 → 周期执行 → 每日邮件汇报。

英文版见 `README.md`。

**状态：Phase 1（导入链路）已完成 —— 完整设计见 `DESIGN.zh.md`。**

## 硬规则

1. **用户数据永不进仓库。** `data/`、`*.env`、`config/secrets*` 全部 gitignore；`data_root` 可指向仓库外——但只允许以本项目命名的文件夹（如 `~/outreach-scheduler-data/`），路径不得涉及其他私人文件夹。GitHub 只有源码。
2. **无外部工具链依赖。** 仅 Python3 标准库 + PyYAML + requests；只读外部产物文件，不 import 任何现有系统。
3. **验证码走 CUA 免费通道。** 本机真实 Chrome 自动完成图像/隐形挑战；付费打码可选、默认关闭。
4. **提交前必跑私有扫描。** `python3 tools/private_scan.py` 必须通过，命中即拒绝提交（邮箱、密码、私有域名、私有文件夹名、`~/` 点目录与绝对家目录路径）。
5. **文档语言规范。** 英文主文档 `.md`；中文翻译 `.zh.md`。

## 目录结构

```
bin/osched.py            CLI 入口（import / stats）
config/global.example.yaml   配置模板——复制为 config/global.yaml
src/osched/
  config.py              YAML 配置 + env 凭据，fail-fast
  models.py              SourceRecord——统一导入模型（DESIGN.zh.md §4b）
  adapters/base.py       SourceAdapter 抽象接口（只读、惰性）
  adapters/wdc.py        wdc_competitor_links 适配器
  db.py                  SQLite 五表：pool/project/match/plan/run_log
  pool.py                导入管线（按 domain+url 去重）
tools/private_scan.py    私有信息提交前扫描闸
import/                  WDC 收割基线代码（原样复制；见 import/README.zh.md）
```

## 快速开始

```bash
cp config/global.example.yaml config/global.yaml   # 然后改路径
cp config/secrets/smtp.env.example config/secrets/smtp.env   # 需要邮件汇报时
python3 bin/osched.py import                        # 适配器 -> 池
python3 bin/osched.py stats                         # 池摘要
```

## 路线图（DESIGN.zh.md §10）

- Phase 2：探测（入口/表单/验证码类型/联系邮箱识别）+ 分类
- Phase 3：项目匹配 + 人工确认队列
- Phase 4：排期执行器 + wave1 表单直投（HTTP 优先）+ wave2 SMTP
- Phase 5：每日邮件汇报；DR 用 Open PageRank 免费层
