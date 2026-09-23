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

首次安装都带本地配置向导（仅监听 `127.0.0.1`，凭据不出公网；远程 VPS 自动给出端口转发命令）。克隆仓库后可用统一入口选择方式：

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

敏感信息只走环境变量，模板复制后填写，勿提交 Git：

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
./deploy/linux/install.sh        # 建 venv → 配置向导 → 装 timer → --doctor
./deploy/linux/status.sh         # 安装后状态检查
```

安装器创建每小时采集 + 日推窗口 + 周报的 systemd user timer（按配置页时间生成，北京时间），环境文件保存于 `~/.config/trendradar-lite/env`（权限 600）。

常用维护：

```bash
./deploy/linux/install.sh --configure   # 重新打开配置页
./deploy/linux/update.sh                # 更新（脏工作区会被拒绝）
./deploy/linux/uninstall.sh             # 卸载（默认保留数据与 env）
./deploy/linux/uninstall.sh --purge-data
loginctl enable-linger "$USER"          # 退出 SSH 后 timer 仍要运行时需要
```

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

`output/` 存于 Docker volume；`config/` 宿主只读挂载。

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
bash -n install.sh deploy/linux/*.sh deploy/docker/*.sh         # 脚本语法检查
docker compose config --quiet                                   # compose 配置校验
```

测试全部使用 mock 与临时目录：不抓取真实新闻、不调用真实 AI、不发送真实邮件；测试代码不打入镜像、不由安装器部署。

## 许可与致谢

基于 [sansan0/TrendRadar](https://github.com/sansan0/TrendRadar) 精简、修改并完成三种部署适配；本仓库非上游官方发行版，新增与修改内容由本仓库维护者负责。遵循 [GPL-3.0](./LICENSE)。

- 上游项目：<https://github.com/sansan0/TrendRadar>
- 修改要点：精简功能面，统一 GitHub Actions / 原生 Linux / Docker Compose 三套部署
