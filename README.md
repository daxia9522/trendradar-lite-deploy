# TrendRadar Lite Deploy

> 源码版本 **v26.11.15** ｜ [Releases](https://github.com/daxia9522/trendradar-lite-deploy/releases) ｜ 镜像 `ghcr.io/daxia9522/trendradar-lite-deploy`

聚合热榜与 RSS → 六栏筛选 → AI 日报/周报分析 → HTML 与邮件推送。基于 [sansan0/TrendRadar](https://github.com/sansan0/TrendRadar) 的非官方精简发行版。

## 选择部署方式

| 方式 | 适合 | 前置条件 |
|---|---|---|
| 原生 Linux | VPS、本地生产与源码维护 | Python 3.10+、venv、systemd |
| GitHub Actions | 云端执行，由外部定时器触发 | GitHub仓库、R2/S3、邮件及AI凭据 |
| Docker Compose | NAS或已有Docker环境 | Docker Engine、Compose v2 |

克隆仓库后，可运行 `./install.sh` 选择部署方式。下面的配置、安装、触发、维护和日志说明均可直接在本页查阅。

## 配置文件与变量

| 文件 | 作用 |
|---|---|
| `config/config.yaml` | 新闻源、报告、存储、模型连接组与四项AI任务 |
| `config/timeline.yaml` | 日报推送窗口和执行策略 |
| `config/frequency_words.txt` | 关键词筛选及AI筛选失败时的兜底规则 |
| `config/ai_filter/tags.yaml` | 六栏描述及展示上限 |
| `config/ai_filter/prompt.txt` | 标题分类提示词 |
| `config/ai_analysis_prompt.txt` | 日报分析提示词 |
| `config/weekly_ai_prompt.txt` | 周报分析结构与事件三级标题要求 |
| `config/weekly_keyword_prompt.txt` | 周报短标签与原文引用要求 |

原生 Linux 将私有环境变量保存在 `~/.config/trendradar-lite/env`（权限0600）；Docker使用宿主机 `runtime/env`；Actions使用仓库Secrets。首次安装由菜单填写，无需手工复制模板。密钥及私有接口地址不写入YAML、不提交Git。

原生部署设置 `XDG_CONFIG_HOME` 时，env与用户unit保存到该配置根目录下；安装、配置和维护时使用一致的环境。

### AI连接与功能开关

| 变量 | 填写方法 |
|---|---|
| `GEMINI_API_KEY` | Gemini连接组API Key，供组内模型共用 |
| `CF_API_KEY` | CF连接组API Key |
| `CF_API_BASE` | CF完整HTTP(S)接口根地址，保留所需路径，不自动拼接 `/v1` |
| `AI_ANALYSIS_ENABLED` | 日报AI分析开关：`true/false/1/0`，留空沿用YAML；安装模板默认false |
| `AI_FILTER_ENABLED` | AI六栏筛选开关：`true/false/1/0`，留空沿用YAML；安装模板默认false |
| `GEMINI_API_KEY_FILE` / `CF_API_KEY_FILE` | 原生部署可选：私有密钥文件路径，须在对应组改用 `api_key_file_env` 引用 |

密钥文件须为无符号链接路径中的普通文件，权限0400/0600。菜单不会读取密钥文件或调用模型。

### 邮件

| 变量 | 必填 | 填写方法 |
|---|---|---|
| `EMAIL_FROM` | 是 | 发件邮箱 |
| `EMAIL_PASSWORD` | 是 | SMTP密码或邮箱授权码；iCloud使用App专用密码 |
| `EMAIL_TO` | 是 | 收件邮箱，多个地址用英文逗号分隔 |
| `EMAIL_SMTP_SERVER` | 否 | SMTP服务器；与端口成对设置 |
| `EMAIL_SMTP_PORT` | 否 | SMTP端口，例如465或587 |

SMTP服务器和端口都留空时按发件邮箱域名识别；内置支持Gmail、QQ、163/126、Outlook/Hotmail、iCloud等常见服务。无法自动识别时填写邮箱服务商提供的服务器和端口。

### 时间与采集

| 变量 | 默认值 | 用途 |
|---|---|---|
| `TZ` | `Asia/Shanghai` | 部署时区 |
| `TIMEZONE` | 可选 | 程序优先读取的时区覆盖；设置时须与 `TZ` 一致 |
| `CRAWLER_MINUTE` | `5` | 每小时采集分钟，0～59 |
| `MORNING_PUSH_TIME` | `07:00` | 早间推送 |
| `NOON_PUSH_TIME` | `12:00` | 午间推送 |
| `EVENING_PUSH_TIME` | `18:00` | 傍晚推送 |
| `DAILY_SUMMARY_TIME` | `22:00` | 全天汇总 |
| `WEEKLY_WEEKDAY` | `6` | 周报星期：0=周一，6=周日 |
| `WEEKLY_HOUR` / `WEEKLY_MINUTE` | `12` / `30` | 周报时、分 |
| `PLATFORMS_API_URL` | 留空沿用YAML | 热榜主接口 |
| `PLATFORMS_API_FALLBACK_URLS` | 留空沿用YAML | 多个备用接口用英文逗号分隔 |
| `CONFIG_PATH` | `config/config.yaml` | 日报及配置加载的可选配置路径；当前周报固定读取仓库 `config/config.yaml` |

推送时刻是窗口起点，持续到该小时 `:59`；四档推送须位于不同小时。非推送整点只采集入库，不调用筛选/分析模型或生成邮件。实际时间以已安装timer或Docker调度配置为准。

### 存储与R2/S3变量

| 变量 | 填写方法 |
|---|---|
| `STORAGE_BACKEND` | 原生可选 `local`、`remote`或`auto`；Actions固定remote，官方Docker固定local |
| `S3_BUCKET_NAME` | 已创建的存储桶名称 |
| `S3_ENDPOINT_URL` | S3接口根地址；R2通常为 `https://<account-id>.r2.cloudflarestorage.com` |
| `S3_ACCESS_KEY_ID` | 对象存储Access Key ID |
| `S3_SECRET_ACCESS_KEY` | 对象存储Secret Access Key |
| `S3_REGION` | R2用 `auto`；其他S3服务按平台填写 |

- `local`：新闻/RSS日库保存在 `storage.local.data_dir`，默认 `output/`。
- `remote`：新闻/RSS日库直接保存在R2/S3的 `news/`、`rss/`；读取时下载到临时目录，需配置S3变量。
- `auto`：本机选local；Actions远程配置齐全时选remote，否则选local。Actions业务工作流明确使用remote。
- 官方Docker的运行配置锁定local，应用数据保存在持久卷中。
- 同一组日库只保留一个采集写入端；交接前先停原写入端，核对所需历史日库，再启新端。

<details>
<summary>点击展开：如何获取 R2/S3 凭据（以 Cloudflare R2 为例）</summary>

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)，进入 **Storage & databases → R2 → Overview**，按页面提示开通R2订阅并完成付款信息设置。R2有免费月度额度，超出后按用量计费，以开通页面和[官方价格](https://developers.cloudflare.com/r2/pricing/)为准。
2. 点击 **Create bucket** 创建存储桶，例如 `trendradar-data`，记录桶名。
3. 回到R2概览，在 **Account Details → API Tokens → Manage** 创建R2 API token。可按账户权限选择Account或User token。
4. 权限选择 **Object Read & Write（对象读和写）**，并限定到本项目的存储桶；本程序通过S3接口读写对象，不需要创建或删除桶的管理员权限。
5. 创建后立即保存 **Access Key ID** 和 **Secret Access Key**；Secret Access Key只在创建时显示，不能用普通Cloudflare API token字符串替代这两项。
6. 从桶连接信息或账户详情取得 **S3 API endpoint**。通常为 `https://<account-id>.r2.cloudflarestorage.com`；使用特定jurisdiction的桶时，采用控制台给出的对应地址。
7. 在本机配置菜单或GitHub仓库Secrets中填写：
   - `S3_BUCKET_NAME`：桶名。
   - `S3_ACCESS_KEY_ID`：Access Key ID。
   - `S3_SECRET_ACCESS_KEY`：Secret Access Key。
   - `S3_ENDPOINT_URL`：S3 API endpoint。
   - `S3_REGION`：`auto`。

只在受保护的环境配置或Secrets页面填写真实凭据。需要轮换时先创建新凭据、更新配置并确认读写，再撤销旧凭据。官方入口：[R2认证说明](https://developers.cloudflare.com/r2/api/tokens/)。

</details>

## AI模型、筛选与周报

### 模型组与任务

`model_groups` 定义连接及模型ID，`tasks` 决定每项任务使用哪些模型。

```yaml
model_groups:
  gemini_official:
    api_key_env: GEMINI_API_KEY
    models:
      g4_hydrogen: gemini/gemini-4-hydrogen
      g35_lite: gemini/gemini-3.5-flash-lite
      g31_lite: gemini/gemini-3.1-flash-lite
  cf:
    api_base_env: CF_API_BASE
    api_key_env: CF_API_KEY
    models:
      qwen_cf: openai/@cf/qwen/qwen3.8-27b
```

每组地址选择 `api_base`（字面地址）或 `api_base_env`（变量名），二选一；凭据选择 `api_key_env` 或 `api_key_file_env`，二选一。Gemini原生组可省略地址或写 `api_base: ""`；显式地址引用缺失、为空或非法时会报错。同组模型须使用同一协议，模型ID须全局唯一。

配置与源码/镜像版本须匹配，字段按本页的模型组与任务结构填写；无效或冲突配置会明确报错。新增Actions连接组时，在对应工作流的 `env` 中显式注入该组引用的Secret；只在仓库设置页面新增Secret不会自动传入任务。

| 任务 | 当前候选链 | 用途与参数 |
|---|---|---|
| `ai_filter` | `g35_lite → g31_lite` | 六栏标题分类，使用默认思考设置 |
| `daily_analysis` | `g4_hydrogen → g35_lite → qwen_cf` | 日报正文；Gemini low，Qwen关闭thinking |
| `weekly_analysis` | `g4_hydrogen → g35_lite → qwen_cf` | 周报正文，任务参数与日报独立 |
| `weekly_keywords` | `g35_lite` | 周报短标签，单模型、默认思考设置 |

每项任务设置 `timeout`、`retry` 和 `model_chain`；链中每个节点为 `model_id` 与可选 `request_params`。当前请求超时120秒，Gemini正文参数为 `reasoning_effort: low`，Qwen为 `extra_body.chat_template_kwargs.enable_thinking: false`。请求参数只能控制生成内容，不能覆盖连接地址、密钥、TLS或SDK重试。

筛选每候选最多2次，失败后等待5秒，总共最多4次。正文普通错误最多初次加2次，超时后切换下一候选。调整重试次数或模型链前，应核对平台限流。

### 六栏筛选与缓存

栏目为国内、国际、科技、财经、文娱、体育，默认展示上限20、20、15、15、10、10；相关度门槛默认为0.7。栏目和上限在 `config/ai_filter/tags.yaml` 调整。

全部未缓存标题整包分类，不按固定条数拆批；全命中缓存时不调用分类模型。AI筛选独立于关键词规则；关闭AI或所有候选失败时，整轮使用 `frequency_words.txt` 筛选。

归类后按热度、展示去重及限额形成最终名单，同时用于正文分析与HTML。默认主榜最多90条、新增最多40条；独立速览只展示。相关度用于判断栏目匹配，不代替热度排序。

缓存默认位于 `output/ai_filter/cache.sqlite3`，随 `storage.local.data_dir` 调整，不上传R2。缓存用于减少重复分类请求，30天前的记录会自动清理。

### 周报正文与关键词

周报从所选日期的历史日库整理素材，正文与短标签分别调用模型。正文固定六个章节，第二节每个事件名使用 `### 序号. 主题名称`，下方写“发生了什么、为什么重要、后续怎么看”。格式在 `config/weekly_ai_prompt.txt` 维护。

短标签附带正文中的逐字引用，数量按内容确定。标签与引用随周报保存在 `output/weekly-ai-reports/`；页头显示正文实际使用的模型。

## 原生 Linux

推荐Debian/Ubuntu；安装系统依赖后，以运行服务的同一用户操作：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
git clone https://github.com/daxia9522/trendradar-lite-deploy.git
cd trendradar-lite-deploy
./deploy/linux/install.sh
./deploy/linux/status.sh
```

安装器创建 `.venv/`、用户级systemd服务/定时器和 `~/.local/bin/trendradar` 配置入口。只安装而不启用timer时加 `--no-enable`。需要退出SSH后继续运行用户timer时，执行 `loginctl enable-linger "$USER"`（由系统决定是否需要管理员权限）。

### 配置菜单与生效

任意目录运行 `trendradar`；PATH未包含 `~/.local/bin` 时使用完整路径。仓库内也可执行 `./deploy/linux/install.sh --configure`。

```text
1. 邮件推送
2. AI 开关与连接组变量
3. 采集与推送时间
4. 高级配置
5. 查看待保存变更
6. 存储配置
s. 保存并应用
q. 放弃修改并退出
```

进入分组后逐项修改；回车保留原值，`:cancel` 取消该字段编辑，`:clear` 清空可选字段。密码、密钥及CF地址不回显。修改先保存在内存，预览确认后按 `s` 保存；`q`、EOF或Ctrl-C不保存。

| 修改内容 | 生效方式 |
|---|---|
| 邮件、AI、采集接口等 | 下次任务启动读取新env，不重启正在运行的任务 |
| 采集/推送/周报时间、时区 | 同步相关timer，保留原启用/禁用及运行状态 |
| 存储模式与S3参数 | 下次任务读取新env；选择remote时填写对应桶、地址和凭据 |

原生 timer 由安装器和配置菜单统一生成、更新，不提供独立生成命令。

网页配置需显式执行 `trendradar --web`，默认绑定本机 `127.0.0.1`；远程使用SSH端口转发，地址和端口按命令输出填写。普通终端菜单不会自动开放网络配置页面。

### 定时启停、更新与卸载

程序更新使用下列更新脚本；日常采集不请求远程版本清单。

以下命令按需要单独执行，不要整段一起运行：

```bash
systemctl --user disable --now trendradar-lite.timer trendradar-weekly.timer
systemctl --user enable --now trendradar-lite.timer trendradar-weekly.timer
./deploy/linux/update.sh
./deploy/linux/uninstall.sh
```

关闭timer不会中断已运行任务；timer设置 `Persistent=true`，恢复时可能立即补触发错过的任务。

更新器要求已跟踪文件没有未提交修改，并且与上游可快进；更新失败不会自动stash或重写历史。卸载默认保留env及数据；明确要连数据删除时才使用 `./deploy/linux/uninstall.sh --purge-data`。

## GitHub Actions

业务工作流 `Get Hot News`（`crawler.yml`）和 `Weekly AI Report`（`weekly-report.yml`）均由 `workflow_dispatch` 触发，可手动或由外部定时器调用。

1. Fork或使用自己有写权限的仓库，确认工作流文件已在默认分支。
2. 在 **Settings → Secrets and variables → Actions → New repository secret** 填写前文的邮件、AI及R2/S3变量。`CF_API_BASE`也使用同名Secret；本机env不会自动同步。
3. 日报按需设置 `AI_ANALYSIS_ENABLED`、`AI_FILTER_ENABLED`；周报正文不受日报AI开关控制。
4. 在 **Actions → 工作流 → Run workflow** 手动运行一次首验。这会真实调用AI并发送邮件。
5. 外部定时器按需要发送的时刻dispatch。Actions会绕过日报推送时间窗和once去重，不要照搬本机每小时采集频率。

同一组日库不要同时启用本机和Actions采集。排队超过30分钟的日报、超过6小时的周报会跳过，手动重跑除外。CI仅检查依赖、语法、入口及容器构建；镜像发布由版本标签或 `Release Container Image` 手动工作流触发。源码版本号不代表同名镜像已经发布。

### 华为云函数调用模板

创建Python事件函数，代码文件为 `index.py`，执行入口为 **`index.handler`**。函数环境变量设置 **`GITHUB_TOKEN`**：细粒度GitHub PAT选择目标仓库，并授予 **Actions: Read and write**。按需要设置超时，须大于模板中的20秒HTTP超时。

将代码里的 `<用户名>`、`<仓库名>` 替换为实际值。PAT限定的是权限，不会替你选择仓库；`owner/repo`必须属于授权范围。模板默认触发 `crawler.yml` 的 `main` 分支。

<details>
<summary>点击展开：完整华为云函数 Python 代码</summary>

```python
# -*- coding: utf-8 -*-
"""GitHub workflow_dispatch；入口index.handler，环境变量GITHUB_TOKEN。"""
import json
import os
import urllib.error
import urllib.request


def _dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def handler(event, context):
    token = os.environ["GITHUB_TOKEN"]
    payload = _dict(event)
    config = _dict(payload.get("user_event")) or payload
    owner = config.get("owner") or "<用户名>"
    repo = config.get("repo") or "<仓库名>"
    workflow = config.get("workflow") or config.get("workflow_id") or "crawler.yml"
    ref = config.get("ref") or "main"
    url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/actions/workflows/{workflow}/dispatches"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps({"ref": ref}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "huawei-fg-trendradar",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return {"ok": response.status == 204, "status": response.status, "workflow": workflow}
    except urllib.error.HTTPError as error:
        return {"ok": False, "status": error.code, "workflow": workflow}
    except Exception as error:
        return {"ok": False, "status": 0, "error_type": type(error).__name__}
```

</details>

创建TIMER触发器时，在“附加信息”填写：

- 日报：`{}`。
- 周报：`{"workflow":"weekly-report.yml"}`。
- 指定仓库和分支：`{"owner":"用户名","repo":"仓库名","workflow":"crawler.yml","ref":"main"}`。

先通过函数测试事件检查返回，再配置定时触发；每次成功dispatch都会真正启动对应业务。`ok: true, status: 204` 只表示GitHub接受触发请求，运行和邮件结果到Actions日志查看。常见错误：401检查PAT及有效期，403检查仓库权限，404检查仓库/工作流路径，422检查分支与工作流是否支持dispatch。

## Docker Compose

要求Docker Engine和Compose v2。按 [Docker官方安装说明](https://docs.docker.com/engine/install/)安装；非root用户需具备Docker权限，加入docker组意味着较高的主机权限。

克隆仓库后，根据实际来源选择一种安装方式，不要两条一起执行：

```bash
./deploy/docker/install.sh --build      # 从当前源码构建并安装
./deploy/docker/install.sh --pull       # 拉取已发布且与配置匹配的镜像
```

安装器打开分组配置菜单并启动容器。`--no-start`只准备不启动；需要指定界面时加 `--terminal` 或 `--web`。

```bash
trendradar-docker                       # 修改runtime/env
./deploy/docker/install.sh --configure # 仓库内打开同一菜单
docker compose ps trendradar
docker compose logs --tail=50 trendradar
```

Docker的应用变量在宿主机 `runtime/env`，根目录 `.env` 用于部署参数；保存应用配置不拉取/构建镜像、不重建服务容器，调度器在后续任务读取新值。网页配置仅绑定宿主机回环地址，远程使用安装器提示的SSH转发；关闭浏览器标签不等于结束配置进程，应在页面选择取消。

| 根 `.env` 变量 | 用途 |
|---|---|
| `TREND_RADAR_IMAGE` | 镜像标签；默认 `ghcr.io/daxia9522/trendradar-lite-deploy:latest` |
| `TRENDRADAR_UID` / `TRENDRADAR_GID` | 安装器沿用已存身份，首次通常取当前非root用户UID/GID；直接Compose回退1000/1000 |
| `SETUP_PORT` | 配置网页宿主机回环端口，默认8765 |
| `SETUP_SSH_USER` / `SETUP_SSH_HOST` / `SETUP_SSH_PORT` | 可选，用于配置页面显示SSH转发连接信息 |

### 镜像更新、调度与数据

以下更新方式二选一：

```bash
./deploy/docker/update.sh --build       # 当前源码构建并重建容器
./deploy/docker/update.sh --pull        # 拉取所选镜像并重建容器
```

从官方latest或digest执行源码构建时，安装器会将镜像选择保存为本地标签；使用自定义标签时保留该选择。切换预构建版本时在 `.env` 设置所需 `TREND_RADAR_IMAGE`，同时检查同名shell变量是否覆盖它。`latest`指向最近成功发布的镜像，普通源码push不会自动发布镜像。

Docker使用 `TZ` 与前文时间变量调度；持久化数据保存在 `trendradar-output` 卷。调度器不会因修改时间立即补跑当前分钟，错过的业务任务也不会自动补跑。

```bash
docker compose exec trendradar python deploy/docker/entrypoint.py doctor
docker compose exec trendradar python deploy/docker/entrypoint.py show-schedule
docker compose stop trendradar
docker compose start trendradar
```

停止容器会终止其中正在执行的任务，操作前确认空闲。普通卸载 `./deploy/docker/uninstall.sh` 保留数据卷、运行配置和源码；`--purge-data`删除数据卷、配置及本安装入口；`--purge-all`再尝试删除所选本地镜像。清理参数具有破坏性，只在明确不再需要这些数据时使用。

## 本地维护与手动运行

| 目录 | 职责 |
|---|---|
| `trendradar/` | 主程序、AI、采集、存储、日报、共享渲染与邮件 |
| `weekly_report/` | 素材整理、正文分析、短标签及周报入口 |
| `config/` | 可调整的业务配置与提示词 |
| `deploy/` | 安装、配置、更新、卸载、数据维护工具 |
| `.github/` | CI、业务触发及镜像发布 |

只建立源码运行环境而不安装定时器：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m trendradar --help
.venv/bin/python weekly_report/weekly_ai_report_email.py --help
```

直接执行Python不会自动读取systemd的私有env，须先通过可信方式提供环境变量；由timer启动的service会自动读取它。以下命令各自独立，按需要选择：

```bash
.venv/bin/python -m trendradar --show-schedule  # 调度状态
.venv/bin/python -m trendradar --doctor         # 配置与本地存储体检
.venv/bin/python -m trendradar                  # 按当前窗口执行真实业务
.venv/bin/python -m trendradar --force-run      # 绕过推送窗口/once，真实AI与邮件
.venv/bin/python weekly_report/weekly_ai_report_email.py # 真实周报与邮件
```

`--help`只显示帮助；`--doctor`可能写本地体检结果并检查存储。周报 `--dry-run`仍会采集/读取素材并调用AI，只跳过邮件。Docker手动业务入口使用 `docker compose exec trendradar python deploy/docker/entrypoint.py force-run`。

### 输出文件与目录

默认目录示例：

```text
output/
├── news/YYYY-MM-DD.db            # local模式热榜日库
├── rss/YYYY-MM-DD.db             # local模式RSS日库
├── txt/YYYY-MM-DD/HH-MM.txt      # 启用文本存储时的采集快照
├── html/YYYY-MM-DD/HH-MM-SS-ffffff.html # 日报HTML快照
├── weekly-ai-reports/            # 周报HTML和keywords.json
├── ai_filter/cache.sqlite3       # 本机分类缓存
├── publication.json             # 成功发送记录
└── meta/                        # 调度与执行状态
```

remote模式下news/RSS日库在R2/S3，HTML、缓存和发送记录仍保存在执行端。本地日库、筛选缓存和 `publication.json` 随 `storage.local.data_dir` 调整；日报HTML和周报归档仍在仓库 `output/` 下。新增新闻按上次成功发送的采集截止点统计。

### 日志与运行核验

查看定时任务、服务状态和当天日志：

```bash
systemctl --user list-timers 'trendradar*'
systemctl --user status trendradar-lite.service trendradar-weekly.service
journalctl --user -u trendradar-lite.service -u trendradar-weekly.service \
  --since today --no-pager -o short-iso
```

分享日志前隐藏邮箱、密钥和私有地址。

## 许可与致谢

基于 [sansan0/TrendRadar](https://github.com/sansan0/TrendRadar) 修改并适配三种部署方式，非上游官方发行版。遵循 [GPL-3.0](LICENSE)，保留 [NOTICE](NOTICE) 和上游归属。
