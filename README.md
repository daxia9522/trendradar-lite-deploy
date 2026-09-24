# TrendRadar Lite Deploy

> 版本 v26.9 ｜ [Release](https://github.com/daxia9522/trendradar-lite-deploy/releases/tag/v26.9) ｜ 镜像 `ghcr.io/daxia9522/trendradar-lite-deploy`

聚合 11 平台热榜与 RSS → 关键词筛选 → AI 事件分析 → HTML 日报/周报邮件推送。同一份核心代码支持三种部署方式。

基于 [`sansan0/TrendRadar`](https://github.com/sansan0/TrendRadar) 的**非官方**精简部署发行版。

## 怎么选部署方式

| 方式 | 适合 | 你需要有 |
|---|---|---|
| [原生 Linux](#方式一原生-linux) | 小内存 VPS（推荐）；任务跑完即退出，无常驻进程 | Python 3.10+、systemd |
| [GitHub Actions](#方式二github-actions) | 不想养服务器；云端按时执行 | R2/S3 存储桶（数据持久化）+ 外部定时触发 |
| [Docker Compose](#方式三docker-compose) | 已有 Docker/NAS 环境；直接拉取预构建镜像 | Docker Engine + Compose v2 |

原生 Linux 首次安装使用**纯终端分组菜单**，无需浏览器、监听端口或 SSH 端口转发；安装后输入 `trendradar` 可随时重新配置。Docker 保留原有终端/网页向导，GitHub Actions 使用仓库 Secrets。克隆仓库后可用统一入口选择部署方式：

```bash
./install.sh
```

## 共同配置

三种方式共用同一组核心文件：

| 文件 | 作用 |
|---|---|
| `config/config.yaml` | 平台、报告、AI、存储主配置（模型/地址/Key 留空占位，一律由环境变量注入） |
| `config/timeline.yaml` | 每日推送窗口（07/12/18/22 点，北京时间） |
| `config/frequency_words.txt` | 筛选关键词 |
| `config/ai_analysis_prompt.txt` | AI 分析提示词 |

敏感信息通过环境变量注入，勿提交 Git。**原生 Linux 由菜单维护 `~/.config/trendradar-lite/env`，不是仓库根目录的 `.env`**；Docker 使用项目根目录的 `.env`，可以从模板创建：

```bash
cp .env.example .env && chmod 600 .env
```

### AI

| 变量 | 必填 | 说明 |
|---|---|---|
| `AI_API_KEY` | **必填**（启用 AI 时） | 中转站或官方 Key |
| `AI_MODEL` | **必填**（启用 AI 时） | LiteLLM `provider/model` 格式，如 `gemini/gemini-3.5-flash`、`openai/实际模型名` |
| `AI_API_BASE` | 中转站必填；官方留空 | 中转根地址**原样使用**，项目不自动追加 `/v1` |
| `AI_FALLBACK_MODELS` | 可选 | 逗号分隔备用模型；首项同时作周报关键词便宜模型 |
| `AI_ANALYSIS_ENABLED` | 可选，默认 `false` | 是否启用日报 AI 分析 |
| `AI_TIMEOUT` | 可选，默认 `120` | AI 单次请求超时（秒） |

### 邮件

| 变量 | 必填 | 说明 |
|---|---|---|
| `EMAIL_FROM` | **必填** | 发件人 |
| `EMAIL_PASSWORD` | **必填** | SMTP 密码/授权码（163/189/QQ 填授权码；Gmail 填应用专用密码） |
| `EMAIL_TO` | **必填** | 收件人（多个用英文逗号分隔） |
| `EMAIL_SMTP_SERVER` | 可选，如 `smtp.163.com` | 不填则按发件域名自动识别 |
| `EMAIL_SMTP_PORT` | 可选，如 `465` 或 `587` | 与 SERVER 成对配置，都填或都不填 |

> PS：SMTP 自动识别内置 gmail / qq / 163 / vip.163 / 126 / sina / sohu / 189 / aliyun / yandex / outlook（含 hotmail、live）/ icloud 域名，其余回退 `smtp.<发件域名>:587`。常见邮箱只需上面三个必填项，SMTP 两项可不配。

---

## 方式一：原生 Linux

推荐 Debian 12+ / Ubuntu 22.04+。最小化系统先补依赖：

```bash
sudo apt update && sudo apt install -y git python3 python3-venv
```

安装与验证：

```bash
git clone https://github.com/daxia9522/trendradar-lite-deploy.git
cd trendradar-lite-deploy
./deploy/linux/install.sh        # 终端配置 → 建 venv → 装 timer 与命令入口
./deploy/linux/status.sh         # 按需查看状态并执行本地配置体检
```

安装器创建每小时采集、日推和周报的 systemd user timer，环境文件保存于 `~/.config/trendradar-lite/env`（权限 `600`）。设置了 `XDG_CONFIG_HOME` 时，配置和 user units 位于该目录。采集、推送与周报统一使用配置的时区，默认 `Asia/Shanghai`。首次安装默认启用定时器，之后会按计划运行；`--no-enable` 只安装而不启用。菜单和安装器不会额外执行采集/AI/发信测试。

### 一个命令打开菜单

安装器创建用户级入口 `~/.local/bin/trendradar`。在任意目录执行：

```bash
trendradar
```

如果当前 shell 的 PATH 未包含 `~/.local/bin`，使用 `~/.local/bin/trendradar`，或按安装器提示为当前会话设置 PATH；安装器不会修改 shell 启动文件，也不会覆盖其他程序占用的同名入口。

```text
TrendRadar Lite 配置
1. 邮件推送
2. AI 模型与接口
3. 采集与推送时间
4. 高级配置
5. 查看待保存变更
s. 保存并应用
q. 放弃修改并退出
```

这是**数字选择式菜单**，不是依次重填所有字段：进入分组，选择字段，输入新值，返回子菜单即可。

- 回车保留当前值；`:cancel` 取消当前字段编辑；可选字段用 `:clear` 清空。
- 密码和 API Key 输入不回显，菜单和变更预览只显示是否设置，不能查看秘密原文。
- 邮件分组包含发件人、授权码、收件人和 SMTP；AI 分组包含开关、模型、接口、密钥及备用模型。
- 时间分组包含每小时采集分钟、早/午/晚推送、全天汇总、周报星期与时间、时区。
- 高级配置包含 AI 请求超时、热榜数据接口及备用接口；通常保留默认值即可。
- 修改先保存在内存，返回分组或主菜单不会写文件。修改项标注 `[待保存]`，`5` 查看旧值到新值的变更；恢复原值后不再列出。
- `s` 校验并确认后备份、保存和应用；`q` 放弃，有改动时要求确认。EOF/Ctrl-C 不保存；首次安装取消后不继续启用任务。

### 保存与生效

| 修改内容 | 生效方式 |
|---|---|
| 邮件、AI、数据接口等 | 下次 oneshot 任务启动读取新 env，不重启当前任务 |
| 采集、推送、周报时间或时区 | 同步环境覆盖与相关 timer，保持原来的启用/禁用状态 |

打开菜单、查看变更或保存配置**不会手动启动新闻任务或执行 AI/发信测试**；原有定时任务仍会正常运行。菜单保留未展示的 env 参数；写入前在环境文件旁的 `.env.backups/` 创建私有备份（例如 `~/.config/trendradar-lite/.env.backups/`，目录 `700`、文件 `600`），并检测外部编辑，避免覆盖其他修改。

**时间调整有额外安全检查**：`daemon-reload` 本身也可能按上次触发时间补跑。菜单会只读检查两个活动 timer 的状态、旧/新日历与下一次触发点；存在已错过的触发点、进入 120 秒安全窗口，或无法可靠核验时，拒绝应用而不强行重载。可在原定时器下一次正常触发完成后、远离原/新计划时刻重试。旧 `hourly` 或未显式写时区的 timer，在活动状态下也会拒绝修改；需要由操作者先停止相关 timer，再明确规范化，工具不会擅自停止它或清除时间戳。未启用的定时器不会因此启动。此检查不是对 systemd 管理器的锁，不能阻止外部改时钟、挂起或其他管理员并发操作。

旧安装缺少时间变量或存在自定义 timer 时，菜单会检查现有计划并显示警告。时间子菜单的 `n` 将可识别的推定值加入草稿；保存前需确认把原来的推送窗口收敛到指定分钟，并统一 timer 时区。复杂 calendar、自定义 timeline、drop-in、符号链接或不完整安装不会被自动覆盖；只修改 AI/邮件时也不会顺便把时间重置为模板默认值。

仍可使用 `nano ~/.config/trendradar-lite/env` 修改普通环境参数，下次任务读取；**手动改时间变量还需要同步 systemd 定时器，建议使用菜单修改时间**。菜单不是新闻执行入口；手动运行任务仍使用下文的 Python CLI。

### 常用维护

```bash
trendradar                              # 打开配置菜单（任意目录）
./deploy/linux/install.sh --configure   # 从仓库目录打开同一菜单，不重装依赖
./deploy/linux/update.sh                # 更新（脏工作区会被拒绝）
./deploy/linux/uninstall.sh             # 卸载本安装的入口与 units，保留数据与 env
./deploy/linux/uninstall.sh --purge-data
loginctl enable-linger "$USER"          # 退出 SSH 后 timer 仍要运行时需要
```

原生 Linux 默认不自动回退到网页。无交互终端时会退出并提示；使用 SSH 请分配终端。网页配置仅通过 `trendradar --web` 或 `./deploy/linux/install.sh --configure --web` 显式开启，默认绑定 `127.0.0.1`；远程访问仍需 SSH 端口转发。

## 方式二：GitHub Actions

无需服务器，数据持久化依赖 R2/S3。触发方式：**仅 `workflow_dispatch`**（可手动或由外部定时调用）。GitHub 内置 cron 有排队延迟风险，建议用云函数定时器（如腾讯云/阿里云函数计算）或任意 crontab 按时调用 API 触发，保证准点：

```bash
curl -X POST \
  -H "Authorization: Bearer <PAT>" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<owner>/trendradar-lite-deploy/actions/workflows/crawler.yml/dispatches \
  -d '{"ref":"main"}'
```

步骤：

1. Fork 或直接使用本仓库；
2. 在 `Settings → Secrets and variables → Actions` 配置下表 Secrets（AI 变量按[共同配置](#共同配置)一节）；
3. 手动运行 `Get Hot News`、`Weekly AI Report` 各一次完成首验；
4. 外部定时器按需要的时刻 dispatch。

### R2/S3 Secrets（Actions 必填）

| 分组 | Secret | 说明 |
|---|---|---|
| R2/S3 | `S3_BUCKET_NAME` | 桶名 |
| R2/S3 | `S3_ACCESS_KEY_ID` | Access Key ID |
| R2/S3 | `S3_SECRET_ACCESS_KEY` | Secret Access Key |
| R2/S3 | `S3_ENDPOINT_URL` | 如 `https://<account-id>.r2.cloudflarestorage.com` |
| R2/S3 | `S3_REGION` | 可选，默认 `auto` |

<details>
<summary>👉 点击展开：如何获取 R2/S3 凭据（以 Cloudflare R2 为例）</summary>

**⚠️ 前置条件：** 根据 Cloudflare 平台规则，开通 R2 需绑定支付方式。

- **目的**：仅作身份验证（Verify Only），**不产生扣费**。
- **支付**：支持双币信用卡或国区 PayPal。
- **用量**：R2 免费额度（10GB 存储/月）足以覆盖本项目日常运行。

**操作步骤：**

1. **创建存储桶**：登录 [Cloudflare Dashboard](https://dash.cloudflare.com/) → 左侧 `R2 对象存储` → 右上角 `创建存储桶`（如 `trendradar-data`）。
2. **创建 API 令牌**：`概述` → `Account Details` → `Manage`（Manage R2 API Tokens）→ `创建 Account API 令牌`。
   - 权限选择 `管理员读和写`；
   - 建议 `仅适用于指定存储桶` 并选中你的桶；
   - 同时可见 `S3 API` 地址：`https://<account-id>.r2.cloudflarestorage.com`（即 `S3_ENDPOINT_URL`）。
3. **填入 GitHub Secrets**：
   - `S3_BUCKET_NAME` = 桶名
   - `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` = 创建后立即复制的两个密钥（只显示一次）
   - `S3_ENDPOINT_URL` = 上一步 S3 API 地址
   - `S3_REGION` = `auto`（可选）

</details>

Actions 固定 `STORAGE_BACKEND=remote`，每日数据库写入 R2/S3，周报运行前自动从 R2/S3 拉取最近 7 天数据。推送窗口（07/12/18/22 点）的判定始终按 `config/timeline.yaml` 的北京时间进行，与 runner 所在时区无关——外部定时器只需按你希望触发的**北京时刻**（如每小时 :05）dispatch 即可。

**队列自愈**：并发组 `queue: single` + `cancel-in-progress`（新触发自动替换排队中的旧 run）、`timeout-minutes` 执行兜底、内置 guard（排队超 30 分钟/6 小时后才开跑的 run 自动跳过，防错峰乱发信；手动 re-run 始终放行）。

## 方式三：Docker Compose

前置：Docker Engine + Compose v2。全新系统用 Docker 官方脚本安装：

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # 非 root 用户执行后重新登录
```

部署：

```bash
git clone https://github.com/daxia9522/trendradar-lite-deploy.git
cd trendradar-lite-deploy
./deploy/docker/install.sh        # 建 .env → 配置向导 → 拉取预构建镜像 → 启动
docker compose ps trendradar      # 状态应为 healthy
docker compose logs --tail=50 trendradar
```

默认拉取 GHCR 预构建镜像（约 111 MB，双架构），拉取失败自动回退本地构建；生产可在 `.env` 钉死版本：`TREND_RADAR_IMAGE=ghcr.io/daxia9522/trendradar-lite-deploy:v26.9`。镜像更新需显式 `docker compose pull && docker compose up -d`。

容器内置轻量调度器，**所有时刻均为北京时间（容器默认 Asia/Shanghai，可在 `.env` 用 `TZ` 改）**：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CRAWLER_MINUTE` | `5` | 每小时采集的分钟数 |
| `MORNING_PUSH_TIME` | `07:00` | 早间推送 |
| `NOON_PUSH_TIME` | `12:00` | 午间推送 |
| `EVENING_PUSH_TIME` | `18:00` | 傍晚推送 |
| `DAILY_SUMMARY_TIME` | `22:00` | 全天汇总 |
| `WEEKLY_WEEKDAY` / `WEEKLY_HOUR` / `WEEKLY_MINUTE` | `6` / `12` / `30` | 周报（周日 12:30，Python 约定周日=6） |

`output/` 存于 Docker volume；`config/` 宿主只读挂载。仅 `setup` 配置容器可写挂载项目目录，用于原子替换 `.env` 和保存 `.env.backups/` 私有备份；已有 `.env` 的宿主 UID/GID 会保留。常驻 `trendradar` 服务的挂载方式不变。

卸载三选一（后两种**不可恢复**）：

```bash
./deploy/docker/uninstall.sh               # 停容器/网络，留数据、env、镜像
./deploy/docker/uninstall.sh --purge-data  # 另删数据卷与 .env
./deploy/docker/uninstall.sh --purge-all   # 另删本地镜像
```

---

## 手动运行

按所在环境选择对应命令。⚠️ `--force-run` 会绕过推送窗口与 once 去重，**真实调用 AI 并立即发送邮件**；验证链路请优先在非推送窗口执行普通命令。

**原生 Linux / 源码目录**（在仓库目录内，用安装器创建的 venv）：

注意：以下是直接运行 Python 的命令，**不会自动加载 systemd 的环境文件**。需要先通过可信的环境注入方式提供配置；由 timer 启动的 service 则会自动读取 `~/.config/trendradar-lite/env`。不要为加载配置而执行来源不明的 shell 内容。

```bash
cd ~/trendradar-lite-deploy
.venv/bin/python -m trendradar --show-schedule   # 查看当前命中的调度
.venv/bin/python -m trendradar --doctor          # 配置体检
.venv/bin/python -m trendradar                   # 按窗口执行（等同 timer 的一班）
.venv/bin/python -m trendradar --force-run       # 立即执行完整链路（发邮件）
.venv/bin/python weekly_report/weekly_ai_report_email.py
```

**Docker Compose**（常驻容器内调度，手动执行走 exec）：

```bash
docker compose exec trendradar python -m trendradar --doctor
docker compose exec trendradar python -m trendradar --force-run
```

**GitHub Actions**：网页 `Actions → Get Hot News / Weekly AI Report → Run workflow`，等价于 dispatch。

## 输出结构

```text
output/
├── news/YYYY-MM-DD.db        # 当日热榜库
├── rss/YYYY-MM-DD.db         # 当日 RSS 库
├── txt/YYYY-MM-DD/HH-MM.txt  # 采集快照
├── html/YYYY-MM-DD/HH-MM.html
├── html/latest/              # current.html / daily.html
├── weekly-ai-reports/        # 周报归档
└── meta/                     # 调度与执行状态
```

## 开发与验证

仅部署使用可忽略本节；**修改代码后、push 之前**，本地先跑一遍与 CI 相同的自检，避免推上去才看到红叉：

```bash
python -m compileall -q trendradar weekly_report deploy tests   # 语法编译
python -m unittest discover -s tests                            # 全量行为测试
for script in install.sh deploy/linux/*.sh deploy/docker/*.sh; do
  bash -n "$script" || exit 1                                  # 逐个脚本检查语法
done
docker compose config --quiet                                   # compose 配置校验
```

测试全部使用 mock 与临时目录：不抓取真实新闻、不调用真实 AI、不发送真实邮件；原生菜单与安装器测试使用临时 HOME/XDG 和模拟 systemctl，不操作开发者的真实定时器。测试代码不打入镜像、不由安装器部署。

原生终端菜单的设计与验收边界见 [`docs/native-terminal-menu-design.md`](docs/native-terminal-menu-design.md)。

## 许可与致谢

基于 [sansan0/TrendRadar](https://github.com/sansan0/TrendRadar) 精简、修改并完成三种部署适配；本仓库非上游官方发行版，新增与修改内容由本仓库维护者负责。遵循 [GPL-3.0](./LICENSE)。

- 上游项目：<https://github.com/sansan0/TrendRadar>
- 修改要点：精简功能面，统一 GitHub Actions / 原生 Linux / Docker Compose 三套部署
