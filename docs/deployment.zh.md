# 部署 — fly.io 后台运行（无智能体、无桌面依赖）

本系统是纯 CLI + cron + sqlite，任何有 Python 3.12 和持久卷的机器都能跑。
本文覆盖 fly.io 服务器部署；桌面机只保留一项职责（见矩阵）。

## 职责矩阵

| 职责 | fly.io 服务器 | 本地桌面 |
|------|---------------|----------|
| import → probe → classify → dr → match → schedule | ✅（pipeline cron） | 可选 |
| execute（wave1 HTTP 表单） | ✅ | 可选 |
| verify（B1 实链回访） | ✅ | 可选 |
| mail-verify（B3 IMAP 收信） | ✅ | 可选 |
| report + client-report（B2 邮件） | ✅ | 可选 |
| overview / profile / queue 命令 | ✅ | 可选 |
| **cua（浏览器验证码，真实 Chrome）** | ❌ 自动跳过 | ✅ 必须 |
| **account-activate（B3 CUA 点击）** | ❌ 自动跳过 | ✅ 必须 |

`scripts/daily.sh` 里的 CUA 守卫会探测 Chrome，服务器上静默跳过；
state='cua' 计划留在队列，由有 Chrome 的本地机跑
`osched cua <project> --headless` 处理。数据卷可 rsync 回本地做 CUA 轮，
或本地 daily.sh 挂载同一卷运行。

**CUA 留本地是已拍板的近期方案**（2026-11-18）：先服务现有站点，商业
初期可接受；后期视量级替换为开源或商用验证码/提交方案——执行器只有
§7.1 验证码分级路由这一个接触点，换通道 = 改配置 + 一个模块。

## 部署步骤

```bash
cd outreach-scheduler
cp fly.toml.example fly.toml          # 改 app 名与 region
fly volumes create osched_data --size 1 --region hkg
fly deploy

# 配置（不入仓库：含客户邮箱）。fly.toml 里 OSCHED_CONFIG 指向卷上副本。
fly ssh sftp shell -C "mkdir -p /data/config"
fly ssh sftp put config/global.yaml /data/config/global.yaml --app <app>

# 数据快照（pool.sqlite3、projects/、sources/）——55k 池压包约 5MB；
# 本地跑完要同步结果时重传：
#   tar czf /tmp/osched-pool.tgz -C data pool.sqlite3
#   fly ssh sftp put /tmp/osched-pool.tgz /tmp/osched-pool.tgz --app <app>
#   fly ssh console -C "sh -c 'cd /data && tar xzf /tmp/osched-pool.tgz && rm /tmp/osched-pool.tgz'"
# （注意：`fly ssh sftp shell -C "put …"` 会静默不动——用 `sftp put`；
#  `ssh console -C` 里复合命令必须包一层 `sh -c '…'`。）

# 密钥——os.environ 优先于 env 文件（require_env_key 顺序）
# 键集合与 config/secrets/smtp.env.example 相同（SMTP 主机/端口/用户、
# Gmail 应用密码键、IMAP 主机/端口、可选 OPR key），用 `fly secrets`
# 设置；Gmail 应用密码的值来自 Google 账号的应用密码页，绝不来自仓库
# 任何文件。

# 验证
fly ssh console -C "ls /data && scripts/daily.sh run"
```

注意：
- **单机入口**：`scripts/start.sh` 后台起 supercronic、前台跑客户端
  门户 HTTP 服务（`[http_service] internal_port = 8080`）。一台机器
  挂一个卷——fly 卷是单挂载，cron 与门户必须共用同一 VM。
- **OPR IP 绑定**：免费 key 绑单 IP。fly region 出口 IP 对同一 app 是
  稳定的，改绑一次到 fly IP 后，月度批量富化即可无人值守（当前暂挂）。
- **数据中心 IP 发 Gmail**：首次发送可能触发 Google 可疑登录拦截；
  应用密码通常可过，否则先由本地跑 report 直到信任建立。
- 时区：fly.toml 里设 `TZ`，cron 时刻按预期含义执行。

## 脱离智能体运行（2026-11-18 拍板）

全系统 = CLI + cron + sqlite，任何环节都无智能体平台依赖：无 MCP、
无平台路径、无会话状态。凡能按 cron 跑
`python3 bin/osched.py …` 的环境即是完整部署。

## AI 层挂接点（后期，方向已拍板）

后台将加入 AI 用于客户业务（需求/案例对接、策划、能力对接）。AI 层
插入的稳定契约已就位：

| AI 能力 | 挂接的现有契约 |
|---------|----------------|
| 客户接单解析（自由文本 → 域名/TDK/资产） | project YAML + `asset` CLI + §5a 资产 |
| 计划起草 / 外联文案 | templates 链（项目 → data_root → 通用），wave2 result_note 带 template |
| 站点选择判断 | match 打分 + pending 队列（`confirm`/`reject` 是人工闸） |
| 运营叙述（客户侧摘要） | run_log + client_report.py 分节构造器 |
| 能力查询（"能为 X 做什么"） | overview / profile / queue 的 JSON 可读输出 |

设计规则：AI 只作为 CLI/DB 契约的调用方，绝不绕过人工闸
（confirm、验证码 escalate、R-520 人工发送）。
