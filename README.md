# runbow007

李宁 TMS 订单提醒工具。程序读取 TMS 导出的 `.xls` / `.xlsx`，校验数据完整性，执行 R1–R4 业务规则，并向飞书群发送汇总消息。

## 当前运行方式

当前推荐主流程是 **人工下载 Excel + 网页上传**：

1. 在李宁 TMS 创建导出任务并下载 Excel；
2. 打开 runbow007 上传页；
3. 选择规则并上传，系统直接把本轮全部命中订单发送到飞书群。

自动登录 TMS 和自动下载能力仍然保留；承载它们的定时任务默认关闭，因为 TMS 后台导出偶尔无法稳定产出文件。

> 定时任务没有删除。仓库仍包含 Windows 计划任务脚本、Linux systemd timer/service 和启停脚本。普通部署会主动禁用两个 Linux timer，但不会删除这些文件或已安装的 unit。

## 功能

- 支持 `.xls` 和 `.xlsx`；
- 校验必要表头和可选的 TMS 页面总条数；同一订单号出现多次时采用文件中的最后一行；
- 执行 R1–R4 订单提醒规则；
- 使用 SQLite 保存运行记录、异常状态和飞书发送记录；
- 新异常立即提醒，未解决的 R3/R4 默认每天 09:00 后最多重复提醒一次；
- 人工上传与自动下载共用解析、规则、发送、存储和文件锁；网页上传不应用历史提醒去重；
- 日志默认保留 30 天；CLI 或自动任务运行后会清理超过保留期的自动下载文件；
- 自动下载任务失败时可向飞书发送告警；
- CLI 默认演练，不加 `--send` 不会发送飞书消息。

处理链路：

```text
TMS Excel -> 文件校验 -> R1-R4 规则 -> 网页：本轮全部命中 -> 飞书汇总
                                      -> 自动任务/CLI：SQLite 去重 -> 飞书汇总
```

## 业务规则

| 规则 | 触发条件 |
| --- | --- |
| R1 WMS 过账时效 | 离厂时间为空，且当前北京时间超过 `WMS过账时间 + 90 分钟`。阈值由 `rules.wms_lead_minutes` 配置。 |
| R2 今日实际到达 | 实际到达日期是今天，但运输状态仍为“运输在途”或“运输在途（已离厂）”。 |
| R3 合同签署异常 | ① 实际到达时间等于签收时间、状态为“已签收”、合同仍“签署中”；或 ② 两个时间相等、合同“已完成”、运输状态仍为在途。 |
| R4 延迟无原因 | “是否延迟”为“是”，但“延迟原因”为空。 |

网页人工上传每次都会重新推送本轮全部命中项，不与上次上传的数据比较。自动任务和普通 CLI 真实发送仍使用 SQLite 去重；R3/R4 一直未解决时，会按 `rules.unresolved_repeat_hour` 设置的时间开始每日提醒，短暂消失又出现的异常还会受 `rules.reopen_grace_hours` 约束。

## 使用人工上传页

打开：

```text
http://<服务器地址>:8080/
```

默认账号仅供首次登录：

```text
账号：admin
口令：admin123456
```

首次使用建议：

1. 立即修改默认账号和口令；
2. 从 TMS 下载中心取得完整的 `.xls` 或 `.xlsx`；
3. 登录上传页，选择文件；
4. 如能看到 TMS 页面总条数，填入页面用于完整性校验；
5. R1、R2、R3、R4 默认全部勾选；
6. 点击“确认并推送”，确认后直接发送；
7. 页面会显示解析行数、各规则命中数和本次推送数。

网页上传固定直接发送飞书，不受 Linux 的 `RUNBOW007_ENABLE_SENDING` 定时任务开关影响。每次上传都会发送当次规则命中的全部订单，即使和此前数据一致也会再次发送；没有任何规则命中时不会发送空消息。

健康检查：

```bash
curl http://127.0.0.1:8080/healthz
```

## Alibaba Cloud Linux 3 / Ubuntu 24.04 部署

推荐把项目部署到 `/opt/runbow007`，使用 Docker Compose 运行。Web 使用常驻容器，批处理按需创建临时容器；二者使用同一镜像，并共用数据库、下载目录和日志目录。

### 前置条件

- Alibaba Cloud Linux 3 或 Ubuntu 24.04；
- Docker CE、Buildx 和 Docker Compose 插件；
- 服务器能通过 HTTPS 访问 TMS 和飞书开放平台；
- 安全组仅向可信办公出口 IP 放行上传页端口，默认是 `8080`。

### 仅部署人工上传 Web（推荐）

如果服务器只运行“上传 Excel -> 解析 -> 推送飞书”，使用专用的轻量部署。它不安装 Playwright、Chromium 和系统凭据库，也不会启动自动下载定时任务。

首次安装：

```bash
git clone git@github.com:haoxumgg/runbow007-lite.git /opt/runbow007
cd /opt/runbow007
sudo ./scripts/rebuild-web-alinux3.sh
```

第一次运行会创建 `/opt/runbow007/config.yaml` 和 `/etc/runbow007/secrets.env`，并提示填写配置。人工上传模式只需要以下五项：

```dotenv
RUNBOW007_FEISHU_APP_ID=
RUNBOW007_FEISHU_APP_SECRET=
RUNBOW007_FEISHU_CHAT_ID=
RUNBOW007_WEB_USERNAME=
RUNBOW007_WEB_PASSWORD=
```

填写后再次执行同一条命令：

```bash
cd /opt/runbow007
sudo ./scripts/rebuild-web-alinux3.sh
```

脚本会使用 `Dockerfile.web` 构建最小镜像、替换旧的 `runbow007-web` 容器、启动新服务并检查健康状态。默认监听 `0.0.0.0:18080`；可通过 `RUNBOW007_WEB_PORT` 改端口。由于服务器的 Docker bridge DNS 不可用，该专用 Compose 配置在构建和运行时均复用宿主机网络。

更新版本同样只需：

```bash
cd /opt/runbow007
git pull --ff-only
sudo ./scripts/rebuild-web-alinux3.sh
```

密钥只通过 `/etc/runbow007/secrets.env` 注入，不会复制进镜像。脚本不会删除数据库、上传归档、日志或其他业务容器。公网访问仍需在当前 ECS 的入方向安全组中放行实际监听端口。

### 手工部署

```bash
sudo git clone https://github.com/haoxumgg/runbow007-lite.git /opt/runbow007
cd /opt/runbow007
sudo ./scripts/deploy-alinux3.sh
```

部署脚本会：

- 创建 `config.yaml`、运行目录和密钥文件模板；
- 构建 `runbow007:local` 镜像；
- 安装并启动 `runbow007-web.service`；
- 安装自动下载相关 systemd unit；
- 默认执行 `disable --now`，确保两个自动下载 timer 处于关闭状态。

部署完成后编辑非敏感配置：

```bash
sudo vi /opt/runbow007/config.yaml
```

再把敏感值写入 `/etc/runbow007/secrets.env`：

```dotenv
RUNBOW007_TMS_USERNAME='填写 TMS 账号'
RUNBOW007_TMS_PASSWORD='填写 TMS 密码'
RUNBOW007_FEISHU_APP_ID='填写飞书 App ID'
RUNBOW007_FEISHU_APP_SECRET='填写飞书 App Secret'
RUNBOW007_FEISHU_CHAT_ID='填写飞书群 ID'
RUNBOW007_WEB_USERNAME='填写上传页账号'
RUNBOW007_WEB_PASSWORD='填写上传页口令'
```

```bash
sudo chmod 600 /etc/runbow007/secrets.env
sudo systemctl restart runbow007-web.service
```

上传页本身不提供 HTTPS。直接开放端口时必须限制来源 IP；如经 HTTPS 反向代理访问，再把 `web.secure_cookie` 设为 `true`。

也可以生成口令哈希，填入 `web.password_hash`，并清空 `web.password`：

```bash
cd /opt/runbow007
sudo env RUNBOW007_SECRETS_FILE=/etc/runbow007/secrets.env \
  docker compose run --rm app --config /app/config.yaml web-password
```

### GitHub Actions 部署

`.github/workflows/deploy.yml` 只支持手动触发，不会在 `push` 时自动部署。先在仓库 `Settings -> Secrets and variables -> Actions` 配置：

Repository variables：

- `DEPLOY_HOST`
- `DEPLOY_USER`
- `DEPLOY_PORT`，可选，默认 `22`
- `RUNBOW007_TMS_USERNAME`
- `RUNBOW007_FEISHU_APP_ID`
- `RUNBOW007_FEISHU_CHAT_ID`
- `RUNBOW007_WEB_USERNAME`，可选

Repository secrets：

- `DEPLOY_SSH_PRIVATE_KEY`
- `DEPLOY_KNOWN_HOSTS`
- `RUNBOW007_TMS_PASSWORD`
- `RUNBOW007_FEISHU_APP_SECRET`
- `RUNBOW007_WEB_PASSWORD`，可选

然后进入 `Actions -> deploy -> Run workflow`。以下三个安全相关输入默认都是 `false`：

- `run_smoke_test`：部署后执行一次真实 TMS 下载演练，不发送飞书；
- `enable_timers`：启用自动下载定时器；
- `enable_sending`：允许定时任务真实发送飞书。

`feishu_test_rule` 和 `feishu_test_orders` 用于人工验收群消息；非零测试会真实发送，请先确认目标群。

## 上传页运维

```bash
# systemd 服务状态
sudo systemctl status runbow007-web.service

# 重启 Web；查看容器状态和日志
sudo systemctl restart runbow007-web.service
sudo /opt/runbow007/scripts/web-alinux3.sh status
sudo /opt/runbow007/scripts/web-alinux3.sh logs 200
```

持久化目录：

```text
/opt/runbow007/data             SQLite 数据库和上传临时目录
/opt/runbow007/downloads        已处理的 Excel（人工上传归档和自动下载）
/opt/runbow007/logs             运行日志
/opt/runbow007/browser-profile  可选的 Playwright 持久化配置
```

## Windows 本地运行

在 PowerShell 中执行：

```powershell
git clone https://github.com/haoxumgg/runbow007.git
cd runbow007
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1
```

安装脚本会创建 `.venv`、安装开发依赖和 Playwright Chromium，并在缺少时复制 `config.example.yaml` 为 `config.yaml`。

编辑 `config.yaml` 后，把密码和 App Secret 保存到 Windows 凭据管理器：

```powershell
.\.venv\Scripts\runbow007.exe --config config.yaml credentials set-tms
.\.venv\Scripts\runbow007.exe --config config.yaml credentials set-feishu
.\.venv\Scripts\runbow007.exe --config config.yaml check-config
```

启动仅本机可访问的上传页：

```powershell
.\.venv\Scripts\runbow007.exe --config config.yaml web --host 127.0.0.1 --port 8080
```

## 命令行处理 Excel

演练，不发送飞书：

```powershell
.\.venv\Scripts\runbow007.exe --config config.yaml process-file "D:\下载\TMS导出.xls" --rules R1,R3,R4 --ui-total 4750
```

核对结果后真实发送：

```powershell
.\.venv\Scripts\runbow007.exe --config config.yaml process-file "D:\下载\TMS导出.xls" --rules R1,R3,R4 --ui-total 4750 --send
```

`--force-send` 会绕过历史发送去重，只应用于人工验收；`--max-send-orders 1` 到 `5` 可限制真实发送涉及的唯一订单数。不要在常规生产处理里使用 `--force-send`。

## 可选：自动下载与定时任务

自动下载功能仍可手工演练：

```powershell
# 默认数据集是 current_month；不加 --send 不会发送飞书
.\.venv\Scripts\runbow007.exe --config config.yaml run --rules R1,R3,R4

# 已配置历史未完结筛选时
.\.venv\Scripts\runbow007.exe --config config.yaml run --dataset open_carryover --rules R1,R3,R4
```

首次校准 TMS 页面时，可暂时设置：

```yaml
tms:
  headless: false
```

### Linux systemd timer

当前保留两个 timer：

| Timer | 时间 | 规则 |
| --- | --- | --- |
| `runbow007-hourly.timer` | 每小时第 5 分钟，Asia/Shanghai | R1/R3/R4 |
| `runbow007-arrival.timer` | 每天 13:30，Asia/Shanghai | R2 |

查看、启用或关闭：

```bash
sudo /opt/runbow007/scripts/timers-alinux3.sh status
sudo /opt/runbow007/scripts/timers-alinux3.sh on
sudo /opt/runbow007/scripts/timers-alinux3.sh off
```

`off` 只会停止并禁用 timer，不会删除 systemd unit。定时任务真实发送还要求 `/etc/runbow007/runtime.env` 中存在：

```dotenv
RUNBOW007_ENABLE_SENDING=true
```

强制不发送的单次下载演练和日志：

```bash
sudo env RUNBOW007_RUNTIME_FILE=/dev/null \
  RUNBOW007_SECRETS_FILE=/etc/runbow007/secrets.env \
  /opt/runbow007/scripts/run-alinux3.sh hourly
sudo tail -n 200 /opt/runbow007/logs/runbow007.log
```

直接执行脚本不会产生对应的 systemd journal 记录。`sudo systemctl start runbow007-hourly.service` 才会写入该 service 的 journal，但它会遵循 `runtime.env`，在发送开关为 `true` 时真实发送。

### Windows 计划任务

```powershell
# 注册两个计划任务；不带 -EnableSending 时只演练
.\scripts\register-scheduled-tasks.ps1

# 注册并允许真实发送
.\scripts\register-scheduled-tasks.ps1 -EnableSending

# 只禁用现有任务，不会删除任务
.\scripts\register-scheduled-tasks.ps1 -Disable
```

计划任务分别是 `Runbow007-Hourly` 和 `Runbow007-Arrival`。

## 配置说明

配置文件是 `config.yaml`，可以从 `config.example.yaml` 复制。主要分区：

| 分区 | 用途 |
| --- | --- |
| `runtime` | 时区、SQLite、日志、下载目录、文件锁和保留天数 |
| `tms` | TMS 地址、账号、超时、保存筛选和页面选择器 |
| `feishu` | App ID、群 ID、可选的 @ 用户和请求超时 |
| `web` | 上传页监听地址、登录凭据、会话、上传上限和默认规则 |
| `rules` | 启用规则、R1 阈值、重复提醒和单次行数上限 |

`rules.max_row_count` 默认为 `20000`：单个导出文件在 20000 行以内（含）不会因超过
行数上限被拦截；设为 `0` 可关闭绝对上限。系统不再根据历史运行行数设置相对下限。
旧版的 `rules.max_row_ratio` 已废弃；旧配置未补上新字段时也会采用 20000 行默认值。
旧配置中残留的 `rules.min_row_ratio` 会被忽略，不再产生相对行数限制。

密码和 App Secret 不应写入仓库、YAML、日志或示例文件：

- Windows：使用系统凭据管理器；
- Linux：使用权限为 `600` 的 `/etc/runbow007/secrets.env`；
- GitHub Actions：使用 Repository secrets。

## 数据与并发安全

- 上传页和自动任务共用 `data/runbow007.db`；网页发送会写入历史记录，但网页本身不使用历史记录抑制本次提醒；
- 共用 `data/runbow007.lock`，同一时间只允许一个处理任务；
- 原始 Excel、SQLite、日志和浏览器配置目录应保持在 `.gitignore` 中；
- 会话 Cookie 使用 `HttpOnly` 和 `SameSite=Strict`，表单带 CSRF 校验；
- 登录失败会触发临时限速；
- 每次运行记录文件 SHA-256、行数、规则命中数、发送数和失败原因。

下载文件的过期清理在 CLI 任务结束时执行。只运行 Web 上传页时，上传临时文件会在每次请求结束后删除，但已有的 `downloads` 历史文件不会仅因 Web 常驻而定期清理。

## 开发检查

要求 Python 3.11 及以上：

```bash
python -m pip install -e ".[dev]"
python -m playwright install chromium
python -m ruff check .
python -m pytest
```

## License

MIT
