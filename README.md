# TrendRadar Lite Deploy

TrendRadar Lite 的精简部署发行版：聚合多平台热榜与 RSS，按关键词筛选新闻，生成 HTML 日报和 AI 周报，并通过邮件推送。

本仓库使用同一份核心代码支持三种部署方式：

- GitHub Actions：无需服务器，使用 R2/S3 保存数据。
- 原生 Linux + systemd：小内存 VPS 首选，任务执行完即退出。
- Docker Compose：适合已有 Docker 或 NAS 环境。

> 本仓库是基于 `sansan0/TrendRadar` 修改的非官方发行版，不是上游项目的官方部署仓库。

原生 Linux 和 Docker Compose 均提供首次安装配置页。克隆仓库后也可以直接运行统一入口，选择部署方式：

```bash
./install.sh
```

配置页仅监听 `127.0.0.1`，不会把邮箱密码或 AI 密钥暴露到公网。远程 VPS 安装时，安装器会自动识别 SSH 用户、服务器 IP 和端口，显示一条可直接复制的端口转发命令。

## 功能

- 11 个热榜来源与自定义 RSS 聚合
- 关键词筛选、当前榜单和全天汇总
- 热榜与 RSS 统一进入 AI 新闻事件分析管线
- HTML 邮件日报和每周 AI 报告
- 本地 SQLite/TXT/HTML 或 R2/S3 持久化
- 统一 CLI、配置、环境变量和 `--doctor` 体检

## 共同配置

三种部署方式共同使用：

- `config/config.yaml`
- `config/timeline.yaml`
- `config/frequency_words.txt`
- `python -m trendradar`
- `python -m trendradar --doctor`
- `python weekly_report/weekly_ai_report_email.py`

敏感信息只通过环境变量注入。复制模板后填写，不要把真实凭据提交到 Git：

```bash
cp .env.example .env
chmod 600 .env
```

主要变量：

| 变量 | 说明 |
|---|---|
| `STORAGE_BACKEND` | Linux/Docker 使用 `local`，Actions 使用 `remote` |
| `AI_ANALYSIS_ENABLED` | 是否启用日报 AI 分析 |
| `AI_MODEL` | LiteLLM 的 `provider/model` 格式 |
| `AI_API_KEY` | AI 服务密钥 |
| `AI_API_BASE` | 可选 OpenAI-compatible API 地址 |
| `AI_FALLBACK_MODELS` | 可选备用模型，逗号分隔 |
| `EMAIL_*` | SMTP 发件与收件配置 |
| `S3_*` | GitHub Actions 使用的 R2/S3 凭据 |

## 方式一：原生 Linux

推荐 Debian 12+ 或 Ubuntu 22.04+，需要 Python 3.10+、Python venv 和 systemd。最小化安装的 Ubuntu/Debian 请先安装系统依赖：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
```

```bash
git clone https://github.com/daxia9522/trendradar-lite-deploy.git
cd trendradar-lite-deploy
./deploy/linux/install.sh
```

安装器会：

1. 创建 `.venv` 并安装依赖。
2. 打开配置页，填写邮件和可选 AI 参数。
3. 在 `~/.config/trendradar-lite/env` 保存权限为 `600` 的环境文件。
4. 安装每小时采集和周日 12:30（Asia/Shanghai）周报的 systemd user timer。
5. 运行 `--doctor`。

安装完成后检查状态：

```bash
./deploy/linux/status.sh
systemctl --user list-timers 'trendradar-*'
```

首次部署后可以立即强制执行一次，用于验证采集、报告生成和邮件发送：

```bash
cd ~/trendradar-lite-deploy
(
  set -a
  source ~/.config/trendradar-lite/env
  set +a
  .venv/bin/python -m trendradar --force-run
)
```

`--force-run` 会绕过当前推送时间窗口和 once 去重；如果已启用 AI，也会产生一次 AI 调用并立即发送邮件。

以后重新打开配置页：

```bash
./deploy/linux/install.sh --configure
```

更新与卸载：

```bash
./deploy/linux/update.sh
./deploy/linux/uninstall.sh
```

默认卸载会保留数据和环境文件。只有明确执行以下命令才会删除它们：

```bash
./deploy/linux/uninstall.sh --purge-data
```

如果退出 SSH 后 user timer 停止，需要管理员启用 linger：

```bash
loginctl enable-linger "$USER"
```

## 方式二：GitHub Actions

1. Fork 本仓库。
2. 在仓库 `Settings > Secrets and variables > Actions` 配置 SMTP、AI 和 R2/S3 Secrets。
3. 手动运行 `Get Hot News` 和 `Weekly AI Report` 完成首次验证。
4. 新增仓库变量 `ACTIONS_DEPLOYMENT_ENABLED=true`，启用定时任务。

内置调度：

- 热榜与日报：每小时第 5 分钟触发。
- AI 周报：周日 04:30 UTC，即北京时间周日 12:30。

未设置 `ACTIONS_DEPLOYMENT_ENABLED=true` 时，cron 触发会安全跳过，避免未配置 Secrets 的新部署持续失败；`workflow_dispatch` 手动触发不受此开关限制。

Actions 方案必须配置：

```text
EMAIL_FROM
EMAIL_PASSWORD
EMAIL_TO
EMAIL_SMTP_SERVER
EMAIL_SMTP_PORT
S3_BUCKET_NAME
S3_ACCESS_KEY_ID
S3_SECRET_ACCESS_KEY
S3_ENDPOINT_URL
S3_REGION
```

AI 分析还需要 `AI_MODEL` 和 `AI_API_KEY`；中转服务可设置 `AI_API_BASE`。

## 方式三：Docker Compose

安装器需要 Docker Engine 和 Docker Compose v2。全新 Ubuntu/Debian 服务器先安装并启动 Docker：

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker

docker --version
docker compose version
docker info >/dev/null && echo "Docker 运行正常"
```

如果当前不是 root 用户，安装后还需要把当前用户加入 `docker` 组，然后退出 SSH 并重新登录一次：

```bash
sudo usermod -aG docker "$USER"
```

Docker 验证通过后再部署 TrendRadar：

```bash
git clone https://github.com/daxia9522/trendradar-lite-deploy.git
cd trendradar-lite-deploy
./deploy/docker/install.sh
```

安装器会检查 Docker Compose、创建权限为 `600` 的私密 `.env`，通过一次性 setup 容器打开配置页；保存配置后会自动构建镜像、启动服务并显示容器状态。安装完成后检查：

```bash
docker compose ps trendradar
docker compose logs --tail=50 trendradar
```

`trendradar` 状态应为 `healthy`。以后重新配置：

```bash
./deploy/docker/install.sh --configure
```

卸载 Docker 部署：

```bash
# 仅停止并删除容器和网络，保留数据、密钥和镜像
./deploy/docker/uninstall.sh

# 删除容器、网络、数据卷和私密 .env，保留本地镜像和 Git 仓库
./deploy/docker/uninstall.sh --purge-data

# 在上述基础上同时删除本地 TrendRadar 镜像
./deploy/docker/uninstall.sh --purge-all
```

彻底清空后如不再需要代码仓库：

```bash
cd ..
rm -rf -- trendradar-lite-deploy
```

`--purge-data` 和 `--purge-all` 会永久删除 Docker volume 中的报告、数据库和采集记录，无法恢复。

首次部署后可以立即强制执行一次，验证采集、报告生成和邮件发送：

```bash
docker compose exec trendradar python -m trendradar --force-run
```

`--force-run` 会绕过当前推送时间窗口和 once 去重；如果已启用 AI，也会产生一次 AI 调用并立即发送邮件。

Compose 使用一个轻量前台调度器：

- 每小时在指定分钟采集一次，并在四个自定义日更时间准确运行推送
- 每周日 12:30（`TZ` 指定时区）运行 AI 周报
- `output/` 保存于 Docker volume
- `config/` 从宿主机只读挂载

可选调度变量：

| 变量 | 默认值 |
|---|---:|
| `CRAWLER_MINUTE` | `5`，每小时采集分钟 |
| `MORNING_PUSH_TIME` | `07:00`，早间推送 |
| `NOON_PUSH_TIME` | `12:00`，午间推送 |
| `EVENING_PUSH_TIME` | `18:00`，傍晚推送 |
| `DAILY_SUMMARY_TIME` | `22:00`，全天汇总 |
| `WEEKLY_WEEKDAY` | `6`，Python 约定周日 |
| `WEEKLY_HOUR` | `12` |
| `WEEKLY_MINUTE` | `30` |

## 手动运行

```bash
python -m trendradar --show-schedule
python -m trendradar --doctor
python -m trendradar
python -m trendradar --force-run
python weekly_report/weekly_ai_report_email.py
```

`--force-run` 会忽略当前分析/推送时间窗口和 once 去重，可能调用 AI 并发送邮件。

## 开发验证

```bash
python -m compileall -q trendradar weekly_report deploy tests
python -m unittest discover -s tests -v
bash -n install.sh deploy/linux/*.sh deploy/docker/*.sh
cp .env.example .env
docker compose config --quiet
docker build -t trendradar-lite-deploy:local .
```

测试使用 mock、临时目录和本地配置，不抓取真实新闻、不调用真实 AI、不发送真实邮件。测试不会复制进 Docker 镜像，也不会由 Linux 安装器部署到生产服务。

## 输出结构

```text
output/
├── news/YYYY-MM-DD.db
├── rss/YYYY-MM-DD.db
├── txt/YYYY-MM-DD/HH-MM.txt
├── html/YYYY-MM-DD/HH-MM.html
├── html/latest/
├── weekly-ai-reports/
└── meta/
```

## 致谢与许可

本项目基于 [sansan0/TrendRadar](https://github.com/sansan0/TrendRadar) 进行精简、修改和部署适配。

TrendRadar 原项目及其贡献者拥有原始项目相关版权。本仓库不是原项目的官方发行版，新增与修改内容由本仓库维护者负责。

本项目遵循 [GNU General Public License v3.0](./LICENSE)（GPL-3.0）发布。分发、修改或再发布本项目时，须继续遵守 GPL-3.0 的相关条款。

- 上游项目：[sansan0/TrendRadar](https://github.com/sansan0/TrendRadar)
- 许可证：[GNU General Public License v3.0](./LICENSE)
- 修改说明：精简功能并统一 GitHub Actions、原生 Linux 和 Docker Compose 部署
