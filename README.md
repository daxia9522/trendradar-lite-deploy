# TrendRadar Lite Deploy

> 源码版本 v26.10.05 ｜ [Releases](https://github.com/daxia9522/trendradar-lite-deploy/releases) ｜ 镜像 `ghcr.io/daxia9522/trendradar-lite-deploy`

聚合 11 平台热榜与 RSS → 关键词筛选 → AI 事件分析 → HTML 日报/周报邮件推送。

基于 [`sansan0/TrendRadar`](https://github.com/sansan0/TrendRadar) 的**非官方**精简部署发行版。

## 怎么选部署方式

| 方式 | 适合 | 你需要有 |
|---|---|---|
| [原生 Linux](#方式一原生-linux) | 小内存 VPS（推荐）；任务跑完即退出，无常驻进程 | Python 3.10+、systemd |
| [GitHub Actions](#方式二github-actions) | 不想养服务器；云端按时执行 | R2/S3 存储桶（数据持久化）+ 外部定时触发 |
| [Docker Compose](#方式三docker-compose) | 已有 Docker/NAS 环境；直接拉取预构建镜像 | Docker Engine + Compose v2 |

原生 Linux 安装使用**纯终端分组菜单**，安装后输入以下命令可随时重新配置：

```bash
trendradar
```

Docker 使用同一套数字分组菜单，安装后输入以下命令重新配置：

```bash
trendradar-docker
```

克隆仓库后可用统一入口选择部署方式：

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

本地部署的密钥保存在环境文件中；GitHub Actions 使用仓库 Secrets。不要把密钥写入 YAML 或提交到 Git。原生 Linux 使用 `~/.config/trendradar-lite/env`，Docker 使用 `runtime/env`；Docker 根目录的 `.env` 仅用于镜像和部署参数。首次安装直接运行安装器，无需复制 `.env.example`。

### AI

开关类变量填 `true/false`，也接受 `1/0`（不区分大小写，首尾空格会忽略）；留空时使用默认配置，其他值会报错。`R2_BACKUP_ENABLED` 留空表示关闭备份；`DOCKER_CONTAINER` 由 Docker 自动设置，无需手动填写。

| 变量 | 必填 | 说明 |
|---|---|---|
| `AI_API_KEY` | **必填**（启用 AI 时） | 中转站或官方 Key |
| `AI_MODEL` | **必填**（启用 AI 时） | LiteLLM `provider/model` 格式，如 `gemini/gemini-3.5-flash`、`openai/实际模型名` |
| `AI_API_BASE` | 中转站必填；官方留空 | 中转根地址**原样使用**，项目不自动追加 `/v1` |
| `AI_FALLBACK_MODELS` | 可选 | **英文逗号**分隔备用模型；首项同时作周报关键词便宜模型 |
| `AI_FALLBACK_API_BASE` | 可选，新列表模式 | **`@` 分隔**各备用地址，与模型按位置对应，保留空位 |
| `AI_FALLBACK_API_KEY` | 可选，新列表模式 | **`@` 分隔**各备用 Key；整列作为一个秘密，仅通过 env / Secrets 输入 |
| `AI_ANALYSIS_ENABLED` | 可选，默认 `false` | 是否启用日报 AI 分析 |
| `AI_TIMEOUT` | 可选，默认 `120` | 日报、周报正文及周报关键词共用的单次请求超时（秒），不是整条模型链的总时限 |

#### 备用列表规则

<details>
<summary>展开查看备用列表规则与配置示例</summary>

只增加地址、Key 两列，不需要 `AI_FALLBACK_1_*` 等编号变量。主模型仍由 `AI_MODEL` / `AI_API_BASE` / `AI_API_KEY` 独立配置，**不占备用列表的一项**。

- **唯一解析规则**：所有备用都按位置绑定；两个鉴权列都空时只是 N 个空槽，不进入另一种模式。任一非空地址/Key 列都必须恰好 N 项。模型严格按英文逗号切分、空模型报错、重复模型保留原槽位。
- **逐项对应**：模型列按英文逗号切分，URL / Key 列按 `@` 切分。`@url` 是 `[空,url]`，`url@` 是 `[url,空]`，`@` 是 `[空,空]`，`url@@url` 保留中间空项。不能过滤开头、结尾或连续分隔符产生的空位。
- **严格数量**：最终备用模型数为 N，每个有内容的地址 / Key 列必须恰好 N 项。环境变量中的模型列表有内容时覆盖 YAML，三列均按这一最终顺序对齐；无备用模型却填新列、连续 / 首尾逗号产生空模型、非法 `provider/model`、列数不足或超出都报错，不能截断、补齐部分列或向左移位。校验不能识别人工把两把合法 Key 顺序写反，填写时仍须逐槽核对。
- **空 URL 槽**：表示该 provider 的**官方默认地址**，不是继承主接口的自定义中转。主备想用同一中转时，在备用槽明确填写相同 URL。`openai/` 空 URL 也是官方默认，不代表任意兼容中转；第三方地址请显式填写。URL 原样使用、不自动追加 `/v1`，必须为合法 HTTP(S)，不能含内嵌 `user:password` 凭据；Gemini/OpenAI 默认地址由客户端显式固定，不受 SDK 的地址环境变量覆盖；其他 provider 需显式填写地址。
- **空 Key 槽**：仅在**同 provider 且同有效主端点**时继承主 Key。两侧均为同 provider 的官方默认地址可继承；两个显式地址作保守规范化比较。默认地址与手填地址不能可靠判定相同时必须显式填 Key。同 provider 但不同 URL 也必须分别配置 Key，并隔离 `headers` / `extra_headers` 等鉴权信息；不能只看模型前缀。不会从另一个备用继承 Key，也不按模型名去重，同名模型可以指向不同端点。
- **保留分隔符**：URL、Key 的**单个值内任何 `@` 都不支持**，本版没有转义语法，也不要擅自编码或改写 Key。模型列只按逗号分隔，`openai/@cf/...` 这类模型名不受影响。
- **错误范围**：列数或空模型会阻止按猜测运行；单个备用缺独立 Key 或 URL 不合法会标记该槽不可用，诊断不显示 Key / URL 原文。关键词始终选择原始备用 1；该槽不可用时沿既有规则降级，不会偷偷换到备用 2。未配备用时关键词使用主模型。

**示例一：两个官方 Gemini 共用主 Key**

以下均为示例模型和占位 Key，使用前按服务商实际支持替换。备用地址、Key 留空，表示使用官方端点并继承主 Key：

```dotenv
AI_MODEL=gemini/gemini-3.5-flash
AI_API_BASE=
AI_API_KEY=synthetic-gemini-key
AI_FALLBACK_MODELS=gemini/gemini-3.5-flash-lite
AI_FALLBACK_API_BASE=
AI_FALLBACK_API_KEY=
```

若再加一个独立中转备用，只需调整三列；前导 `@` 保留第一备用 Gemini 的空槽：

```dotenv
AI_FALLBACK_MODELS=gemini/gemini-3.5-flash-lite,openai/relay-model
AI_FALLBACK_API_BASE=@https://relay.example.invalid/v1
AI_FALLBACK_API_KEY=@synthetic-relay-key
```

**示例二：多个 OpenAI-compatible 接口各用独立地址 / Key**

即使都使用 `openai/`，不同端点仍需分别填写地址和 Key：

```dotenv
AI_MODEL=openai/primary-model
AI_API_BASE=https://primary.example.invalid/v1
AI_API_KEY=synthetic-primary-key
AI_FALLBACK_MODELS=openai/fallback-model-a,openai/fallback-model-b
AI_FALLBACK_API_BASE=https://backup-a.example.invalid/v1@https://backup-b.example.invalid/v1
AI_FALLBACK_API_KEY=synthetic-backup-a-key@synthetic-backup-b-key
```

日报和周报正文共用这条有序模型链；周报补充关键词使用第一备用，未配置备用时使用主模型。

**填写与密钥保存**

- 原生、Docker 菜单及环境文件遵循同一规则；菜单直接回车保留整列，清空第一槽并保留第二槽可填 `@后续值`。Key 输入和预览不回显。
- `AI_FALLBACK_API_KEY` 整列只放 env / Actions 同名 Secret，不写 YAML、不提交 Git；暂无 `AI_FALLBACK_API_KEY_FILE`。
- 主 Key 可用 `AI_API_KEY_FILE`：仅限权限 `0400/0600`、路径无符号链接的普通文件，直接环境变量 Key 优先。Docker 中该文件须在容器内可读；Actions 直接用同名 Secret。
- 旧预构建镜像须先升级到包含此功能的版本，修改环境文件不会更新镜像代码。

</details>

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

安装器会创建并启用 systemd 用户定时器；运行配置保存在 `~/.config/trendradar-lite/env`（权限 `600`），默认时区为 `Asia/Shanghai`。使用 `--no-enable` 可只安装、不启用定时器。设置 `XDG_CONFIG_HOME` 时，配置和 unit 文件会保存到该目录。安装或配置不会发送测试邮件，也不会调用 AI。

重新运行安装器会修正已知旧格式并保留自定义 unit 设置和现有定时计划；仅更新源码不会改变已安装的服务配置。

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
6. R2/S3 晚间备份（可选）
s. 保存并应用
q. 放弃修改并退出
```

这是**数字选择式菜单**，不是依次重填所有字段：进入分组，选择字段，输入新值，返回子菜单即可。

- 回车保留当前值；`:cancel` 取消当前字段编辑；可选字段用 `:clear` 清空。
- 密码和 API Key 输入不回显，菜单和变更预览只显示是否设置，不能查看秘密原文。
- 邮件分组包含发件人、授权码、收件人和 SMTP；AI 分组包含开关、主模型/接口/密钥及备用模型、地址列表、Key 列表。
- 时间分组包含每小时采集分钟、早/午/晚推送、全天汇总、周报星期与时间、时区。
- 高级配置包含 AI 请求超时、热榜数据接口及备用接口；通常保留默认值即可。
- R2/S3 备份分组默认关闭；可设置夜间时间、回看天数及桶/端点/密钥，复用 Actions 的 `S3_*` 参数。仅对本地数据库部署启用，详见[可选夜间备份](#可选夜间-r2s3-备份)。
- 修改先保存在内存，返回分组或主菜单不会写文件。修改项标注 `[待保存]`，`5` 查看旧值到新值的变更；恢复原值后不再列出。
- `s` 校验并确认后备份、保存和应用；`q` 放弃，有改动时要求确认。EOF/Ctrl-C 不保存；首次安装取消后不继续启用任务。

### 保存与生效

| 修改内容 | 生效方式 |
|---|---|
| 邮件、AI、数据接口等 | 下次 oneshot 任务启动读取新 env，不重启当前任务 |
| 采集、推送、周报时间或时区 | 同步环境覆盖与相关 timer，保持原来的启用/禁用状态 |
| R2/S3 备份开关、时间 | 管理独立备份 timer；明确开关变化可启停，普通保存/重装保留人工暂停；不手动启动上传服务 |

仍可使用 `nano ~/.config/trendradar-lite/env` 修改普通环境参数。

### 常用维护

关闭采集、日报和周报定时任务（用安装时的同一用户执行，不加 `sudo`；保留配置与数据）：

```bash
systemctl --user disable --now trendradar-lite.timer trendradar-weekly.timer
```

重新启用并启动定时任务：

```bash
systemctl --user enable --now trendradar-lite.timer trendradar-weekly.timer
```

关闭 timer 不会中断已经运行的任务；日报和周报 timer 默认 `Persistent=true`，恢复时可能立即触发错过的任务。

若已安装并开启 R2/S3 定时备份，需单独关闭：

```bash
systemctl --user disable --now trendradar-r2-backup.timer
```

恢复已安装的备份 timer（配置中仍需保持 `R2_BACKUP_ENABLED=true`）：

```bash
systemctl --user enable --now trendradar-r2-backup.timer
```

其他维护命令（按需选择，不要整段执行）：

```bash
trendradar                              # 打开配置菜单（任意目录）
./deploy/linux/install.sh --configure   # 从仓库目录打开同一菜单，不重装依赖
./deploy/linux/update.sh                # 更新（脏工作区会被拒绝）
./deploy/linux/uninstall.sh             # 卸载本安装的入口与 units，保留数据与 env
./deploy/linux/uninstall.sh --purge-data
loginctl enable-linger "$USER"          # 退出 SSH 后 timer 仍要运行时需要

cd ~/trendradar-lite-deploy
.venv/bin/python -m trendradar --force-run   # ⚠️ 真实链路：AI+邮件（绕过推送窗口与 once 去重）
```

更新器只接受 fast-forward：先确认工作区干净，再显式 `fetch` + `merge --ff-only`；历史分叉、网络失败都会在改动前退出，不 rebase、不 stash，不受 `pull.rebase` 配置影响。

原生 Linux 默认不自动回退到网页。无交互终端时会退出并提示；使用 SSH 请分配终端。网页配置仅通过 `trendradar --web` 或 `./deploy/linux/install.sh --configure --web` 显式开启，默认绑定 `127.0.0.1`；远程访问仍需 SSH 端口转发。

## 方式二：GitHub Actions

无需服务器，数据持久化依赖 R2/S3。触发方式：**仅 `workflow_dispatch`**（可手动或由外部定时调用）。可用云函数定时器或已有主机的 crontab 按计划调用 GitHub API；外部定时能减少对 GitHub 内置定时调度的依赖，但 Actions 仍可能排队。

步骤：

1. Fork 或使用自己有写权限的目标仓库；
2. 在 `Settings → Secrets and variables → Actions` 配置下表 Secrets（AI 变量按[共同配置](#共同配置)一节）；
3. 手动运行 `Get Hot News`、`Weekly AI Report` 各一次完成首验（会真实调用 AI 和发送邮件）；
4. 外部定时器按需要的推送时刻 dispatch。

### AI Secrets（日报 / 周报共用）

两条业务 workflow 均在各自的运行步骤中，从**同名 Actions Secrets** 注入以下变量，沿用既有 Secrets 约定，不改用 Actions Variables：

| Secret | 用途 |
|---|---|
| `AI_MODEL` | 主模型，如 `gemini/gemini-3.5-flash` |
| `AI_API_KEY` | 主接口 Key（Gemini 方案用官方 Key） |
| `AI_API_BASE` | 主接口地址；Gemini 官方留空 |
| `AI_FALLBACK_MODELS` | 英文逗号分隔，按槽位顺序填写；有内容时覆盖 YAML 备用链 |
| `AI_FALLBACK_API_BASE` | `@` 分隔的备用地址列，原样保留开头、结尾与连续空位 |
| `AI_FALLBACK_API_KEY` | `@` 分隔的备用 Key 列，整列保存在这一个 Secret 中 |
| `AI_TIMEOUT` | 日报与周报共用，当前约定 `120`；不设/留空沿用 YAML |

日报另按需配置 `AI_ANALYSIS_ENABLED=true`；周报正文不由这个日报开关控制。未创建的 Secret 在 Actions 中注入为空字符串；两个鉴权列都空时同样按 N 个空项处理，随后逐项校验；`@` 表示两个空槽，不是模式开关。旧 `AI_OPENAI_*` 不再使用，不读取旧 `_FILE`，也不产生新旧冲突。两条 workflow 直接通过步骤 `env` 注入，不拼接 shell 命令、不拆分或过滤 Secret 字符串；列数和鉴权继承遵循上面的共同规则。只在仓库设置页面填写真实 Key，勿写入 YAML、示例、运行命令或日志。

可先执行无网络、无 AI、无邮件的映射与加载验证（已安装项目依赖时）：

```bash
PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True python3 -m unittest discover -s tests -p 'test_actions_ai_env.py' -v
```

此验证清空外部环境、阻断网络 / AI completion / SMTP，只解析 workflow、加载合成配置并检查候选绑定和关键词派生，不运行业务命令。实际 `workflow_dispatch` 以及周报 `--dry-run` **都不是离线 AI 测试**（后者仅禁止发信），真实首验需另行授权。

### 华为云函数调用模板

创建 Python 事件函数，把下面的代码保存为 `index.py`，执行入口设为 **`index.handler`**。在函数中配置一个环境变量 **`GITHUB_TOKEN`**，值为你的 GitHub PAT；细粒度 PAT 选择目标仓库并授予 **Actions: Read and write**。

需要确认的值：

- **`<用户名>`**：替换为你的 GitHub 用户名或组织名。
- **`<仓库名>`**：替换为目标仓库名，例如 `trendradar-lite-deploy`。
- **`workflow` / `ref`**：默认 `crawler.yml` / `main`，通常不用改。

**PAT 只限定访问权限，不会自动选择仓库。代码中的 `owner/repo` 必须与 PAT 授权的仓库对应。** 以下沿用原有函数逻辑；使用前将 `<用户名>`、`<仓库名>` 连同尖括号替换为实际值。

<details>
<summary>展开复制完整 Python 代码</summary>

```python
# -*- coding: utf-8 -*-
"""GitHub workflow_dispatch。入口: index.handler
环境变量必填: GITHUB_TOKEN
默认: <用户名>/<仓库名> / crawler.yml / main
TIMER 附加或测试事件可覆盖, 例:
 {"workflow":"weekly-report.yml"}
"""
import json
import os
import urllib.error
import urllib.request


def _dict(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            o = json.loads(v)
            return o if isinstance(o, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def handler(event, context):
    token = os.environ["GITHUB_TOKEN"]
    ev = _dict(event)
    cfg = _dict(ev.get("user_event")) or ev

    owner = cfg.get("owner") or "<用户名>"
    repo = cfg.get("repo") or "<仓库名>"
    workflow = cfg.get("workflow") or cfg.get("workflow_id") or "crawler.yml"
    ref = cfg.get("ref") or "main"

    url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/actions/workflows/{workflow}/dispatches"
    )
    req = urllib.request.Request(
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            return {"ok": resp.status == 204, "status": resp.status, "workflow": workflow}
    except urllib.error.HTTPError as e:
        return {
            "ok": False,
            "status": e.code,
            "workflow": workflow,
            "error": e.read().decode("utf-8", errors="replace"),
        }
    except Exception as e:
        return {"ok": False, "status": 0, "workflow": workflow, "error": str(e)}
```

</details>

在华为云 TIMER 的“附加信息”或函数测试事件中填写：

- 日报：`{}`（使用代码中的默认配置）。
- 周报：`{"workflow":"weekly-report.yml"}`。
- 不改代码也可通过事件指定目标，填写前替换占位符：`{"owner":"<用户名>","repo":"<仓库名>","workflow":"crawler.yml","ref":"main"}`。

返回 `ok: true, status: 204` 表示 GitHub 已接受触发请求，实际执行结果到仓库 **Actions** 查看。当前外部 dispatch 也会绕过分析/推送时间窗和 once 去重，因此按需要的推送时刻触发，不要照搬原生 Linux 的每小时采集频率。

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

Actions 的日报和周报都由手动或外部 `workflow_dispatch` 触发，不受 `timeline.yaml` 时间窗限制；请按需要发送的时刻触发，避免重复运行造成重复邮件。排队过久的任务会自动跳过（日报 30 分钟、周报 6 小时），手动重跑除外。

## 方式三：Docker Compose

**Docker 使用与原生同款的数字分组菜单，配置保存在宿主机 `runtime/env`；保存配置不会拉取/构建镜像，也不会重启或重建服务容器。**

| 方式 | 配置操作 | 配置文件 |
|---|---|---|
| 原生 Linux | 输入 `trendradar`，分组菜单 | `~/.config/trendradar-lite/env` |
| Docker | 输入 `trendradar-docker`（或 `./deploy/docker/install.sh --configure`），同一套分组菜单；非交互环境可用网页表单 | 项目目录 `runtime/env`（只读挂载进容器） |

分组菜单支持邮件、AI、采集与推送时间、高级配置的字段级修改、待保存预览、秘密输入不回显与取消退出。与原生版的区别：Docker 菜单不调用 systemd（容器内置调度器），保存后由调度器在下一次任务读取新配置；**修改正在执行的时间参数不会立即补跑当前分钟**。

前置：Docker Engine + Compose v2。全新系统用 Docker 官方脚本安装：

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # 非 root 用户执行后重新登录
```

部署：

```bash
git clone https://github.com/daxia9522/trendradar-lite-deploy.git
cd trendradar-lite-deploy
./deploy/docker/install.sh        # 拉取兼容镜像 → 分组菜单配置 → 启动容器
docker compose ps trendradar      # 状态应为 healthy
docker compose logs --tail=50 trendradar
```

默认使用 `ghcr.io/daxia9522/trendradar-lite-deploy:latest`，首次安装会拉取并校验镜像；已有部署通过下方更新命令获取新版。`latest` 代表最近成功发布的镜像，不等于尚未发布的本地源码。

### 镜像更新与源码构建

在仓库目录按需选择：

```bash
./deploy/docker/update.sh            # 默认拉取 latest 并重建容器，保留配置与数据
./deploy/docker/update.sh --build    # 从当前本地源码构建并重建容器，不拉取预构建应用镜像
```

- **旧部署切换 latest**：将根 `.env` 中的 `TREND_RADAR_IMAGE` 改为 `ghcr.io/daxia9522/trendradar-lite-deploy:latest`，或删除该项，再运行更新命令。同名 shell 环境变量优先级更高，也需检查。
- 首次从源码安装用 `./deploy/docker/install.sh --build`。从官方 `latest` 或固定 digest 构建时，自动改用并保存 `trendradar-lite-deploy:local`；自定义标签保留。切回预构建镜像时，按上一条重置镜像选择。
- `latest` 随成功的镜像发布更新：推送版本标签，或在 Actions 的 **Release Container Image** 工作流选择 `main` 手动运行；普通源码推送不会发布镜像。

### 修改配置（无需重建镜像或容器）

任意目录执行（PATH 未含 `~/.local/bin` 时用完整路径）：

```bash
trendradar-docker
```

或在仓库目录运行 `./deploy/docker/install.sh --configure`。配置保存在 `runtime/env`，也可直接编辑，下次任务读取；**保存配置不会拉取/构建镜像，也不会启动或重建常驻容器**。

缺少 `runtime/env` 的旧部署，会在安装或更新时打开菜单，确认后才迁移旧 `.env`。可追加 `--terminal` / `--web` 选择界面；取消不保存配置或启动服务，但不会撤销已完成的拉取/构建。网页请用“取消”结束配置，仅关闭标签页不会退出配置进程。

### 调度与数据

Docker 按 `runtime/env` 中的 `TZ` 调度，默认 `Asia/Shanghai`。常用时间配置及默认值：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `CRAWLER_MINUTE` | `5` | 每小时采集分钟 |
| `MORNING_PUSH_TIME` / `NOON_PUSH_TIME` / `EVENING_PUSH_TIME` | `07:00` / `12:00` / `18:00` | 早间、午间、傍晚推送 |
| `DAILY_SUMMARY_TIME` | `22:00` | 全天汇总 |
| `WEEKLY_WEEKDAY` / `WEEKLY_HOUR` / `WEEKLY_MINUTE` | `6` / `12` / `30` | 周报时间（周日 12:30；周日编号为 6） |

推送时刻是窗口起点，窗口持续到该小时 `:59`；四档推送必须设在不同小时，否则配置会被拒绝。成功的分析／推送在各自窗口内只执行一次；错过的任务不会补跑。修改配置后，调度器按新配置运行。

数据保存在 Docker 持久卷 `trendradar-output`；运行配置在宿主机 `runtime/env`。普通卸载保留数据，`--purge-data` 会删除数据卷和运行配置；需要异地备份时见[夜间 R2/S3 备份](#可选夜间-r2s3-备份)。

查看配置和下一次调度：

```bash
docker compose exec trendradar python deploy/docker/entrypoint.py doctor
docker compose exec trendradar python deploy/docker/entrypoint.py show-schedule
```

### 定时任务启停

在仓库目录执行。停止容器即关闭全部内置定时任务（含已开启的备份），保留配置和数据，但会终止容器内正在执行的任务：

```bash
docker compose stop trendradar
```

恢复已停止的容器及定时调度：

```bash
docker compose start trendradar
```

---

## 可选：夜间 R2/S3 备份

原生与 Docker 均支持“本地 SQLite 每晚备份到 R2/S3”，**默认关闭**；菜单 `6` 设置，密钥不回显，不开启就不需要凭据。

| 参数 | 默认 | 说明 |
|---|---|---|
| `R2_BACKUP_ENABLED` | `false` | 设为 `true` 才自动上传 |
| `R2_BACKUP_TIME` | `23:40` | 按 `TZ`/`TIMEZONE` 每日执行 |
| `R2_BACKUP_LOOKBACK_DAYS` | `2` | 当天+昨天；首次补齐历史可调大 |

开启需 `STORAGE_BACKEND=local` 和已有的四个 `S3_*` 必填项。两端共用 `deploy/r2_backup.py`：原生用独立备份 timer（菜单开关即启停，人工暂停不会被重装恢复），Docker 用容器内调度器。旧镜像尚不支持备份时，按[镜像更新与源码构建](#镜像更新与源码构建)升级后再配置。

备份对象与 Actions 远程存储同一布局（`news/`、`rss/` 按日文件），因此同一桶持续同步后切 Actions 不用搬采集数据库。**同日期整库覆盖，切换前必须：停旧端 → 最后一次同步 → 再启新端。** 此备份不包含发布账本和待补投邮件；仅迁移采集数据库会重新建立发布基线，不能当作完整的投递状态迁移。

```bash
python3 deploy/native_install.py backup-status                    # 原生：开关/计划/timer状态
.venv/bin/python deploy/native_install.py install-backup --enable # 原生：明确安装/启用timer
docker compose exec trendradar python deploy/docker/entrypoint.py backup --dry-run  # Docker：列举待传，不上传
```

## 新增内容与发布状态

- **本次新增热点、RSS 新增更新**统一相对上次已发布报告统计，包含期间采到、后来退榜的内容；每小时采集不会提前消耗新增。主报告的 `current/daily` 范围不变，统一的 `is_new` 继续参与 AI 的新增加分。
- 新闻和 RSS 的输入、覆盖边界在 AI 分析前冻结；发送耗时不会把后续采集误算为已覆盖。首次接入来源时采用最近 24 小时的初始化范围，不把旧定时窗口记录当作成功推送凭据。
- 任一收件人被 SMTP 明确接受、且回执持久化后，本期即视为**已发布**，不等于所有人都已收到。明确暂时失败者在后续有效推送执行中补投原邮件，不重跑 AI、不重发给已接受者；永久拒收或结果未知保留待核对。
- 本地发布状态在数据目录的 `meta/publication-v1.sqlite3`；Actions 在 R2/S3 的 `meta/publication-v1/`。包含私有报告和收件人信息，请随数据妥善保留；远端必须支持条件写入，不会静默退回本地状态。
- 发布清单保持有界；精确去重身份、历史报告及回执存入不可变分片/归档，不会因时间到期删除身份。总存储仍会增长；未解决报告达到容量上限时先停止新生成/发信，不丢弃问题记录。
- 新闻/RSS 分别初始化和推进覆盖边界。某类来源超过 32 天读取范围或读取失败，只标记该类缺口，不拖住健康来源；部分成功采到并已发布的内容仍会消费新增身份，未读范围不冒充已覆盖。

查看状态（不采集、不调用 AI、不发邮件；默认隐藏收件人）：

```bash
.venv/bin/python -m trendradar.publication_cli --backend local status
.venv/bin/python -m trendradar.publication_cli --backend local receipts REPORT_ID --limit 20
# Docker：
docker compose exec trendradar python -m trendradar.publication_cli --backend local status
```

自定义本地数据目录可在子命令前加 `--data-dir 路径`；远端用 `--backend remote`，并通过环境提供现有 `S3_*` 配置。`status` 用 `--offset/--limit` 分页，`receipts` 用返回的 `next_cursor` 配合 `--cursor` 读取后续页；二者默认不展示邮箱，包含已归档历史，且不会隐式执行恢复。

异常恢复入口为 `release-generation` / `resolve-unknown`，用 `--help` 查看参数：必须先核实原执行进程已停止，提供最新状态版本和非敏感证据引用；这些命令只更新状态，不发送邮件。提交中断且未核实的尝试会阻止新的 SMTP 提交，但不停止小时采集；已有持久化 SMTP 回执会在后续正常恢复时重放，不重复发信。**邮箱未到账不等于 SMTP 未接受，不能据此把未知结果改成可重投。**

来源历史确实无法补齐时，可在停止相关采集/生成进程、核实缺口并取得最新 `status` 版本后使用 `readopt-source news|rss --version VERSION --evidence 非敏感工单引用 --confirm --stopped`（同样通过 `python -m trendradar.publication_cli` 调用）。这会明确重新采用最近 24 小时，保留全部已发布身份、原边界审计和报告发布序号；**它是承认缺口，不是补回历史**。日常无需运行此命令。

## 手动运行

按所在环境选择对应命令。⚠️ `--force-run` 会绕过推送窗口与 once 去重，**真实调用 AI 并立即发送邮件**；不会绕过未解决投递的安全检查。验证链路请优先在非推送窗口执行普通命令。

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

**Docker Compose**（常驻容器内调度，手动执行走 exec；entrypoint 自动读取最新 `runtime/env`）：

```bash
docker compose exec trendradar python deploy/docker/entrypoint.py doctor
docker compose exec trendradar python deploy/docker/entrypoint.py force-run
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

## 许可与致谢

基于 [sansan0/TrendRadar](https://github.com/sansan0/TrendRadar) 精简、修改并完成三种部署适配；本仓库非上游官方发行版，新增与修改内容由本仓库维护者负责。遵循 [GPL-3.0](./LICENSE)。

- 上游项目：<https://github.com/sansan0/TrendRadar>
- 修改要点：精简功能面，统一 GitHub Actions / 原生 Linux / Docker Compose 三套部署
