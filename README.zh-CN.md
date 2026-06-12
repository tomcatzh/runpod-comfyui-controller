[English](README.md) | 简体中文

# [RunPod](https://runpod.io?ref=ix73cnib) ComfyUI Controller

[![CI](https://github.com/tomcatzh/runpod-comfyui-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/tomcatzh/runpod-comfyui-controller/actions/workflows/ci.yml)

一个本地优先的会话控制器，在廉价的 [RunPod](https://runpod.io?ref=ix73cnib) GPU 上运行交互式 [ComfyUI](https://github.com/comfyanonymous/ComfyUI) —— **把资金安全作为设计约束**，而不是事后补丁。

[RunPod](https://runpod.io?ref=ix73cnib) 是一个按秒计费的 GPU 云：RTX 4090 这类消费级显卡每小时租金远低于一美元，网络卷可以跨 Pod 持久化数据，还提供兼容 S3 的 API 把文件取回本地。这让它非常适合突发式的 ComfyUI 出图会话——前提是有个靠谱的东西负责关机和收图。这个控制器就是那个东西。

你上传一个 ComfyUI 工作流 JSON；控制器自动对照 Comfy Registry 解析自定义节点、根据真实模型元数据计算网络卷容量、在多个机房并行用廉价 CPU Pod 预下载模型、抢占最便宜的合格 GPU、通过 SSH 配置好 ComfyUI，然后把就绪的 UI 地址交给你。会话结束时，先停 GPU，再通过 [RunPod](https://runpod.io?ref=ix73cnib) 兼容 S3 的 API 把每一张生成的图片采集到本地磁盘，**采集成功后才删除网络卷**。

零 Python 依赖——只用标准库。全部状态存在一个 SQLite 文件里。

## 为什么做这个

在租用 GPU 上跑 ComfyUI，通常要么订阅 SaaS，要么手动管理 Pod——忘了关就烧一整晚的钱，输出也随 Pod 一起消失。这个控制器强制保证：

- **硬上限是花费，不是时间。** 每个会话带一个 `Max total $` 预算；看门狗每个周期重算实时花费，到达预算强制回收（90% 时预警）。正在使用中的会话永远不会被时钟杀死。
- **不静默丢失任何东西。** 输出采集是门槛而非尽力而为：ComfyUI 被强制把输出写到网络卷上，后台采集器每几分钟镜像到本地，关闭流程在最终采集成功前拒绝删卷。
- **不留孤儿资源。** 供应商删除调用采用 fail-closed 语义（`cleanup_failed` 会被暴露而非隐藏），每个候选资源都限定在自己的会话范围内，启动时的清扫会回收崩溃遗留的一切。
- **诚实记账。** 成本先按运行时长估算，随后由 server 内置的账单 worker 对照 [RunPod](https://runpod.io?ref=ix73cnib) 真实账单记录校准。

## 环境要求

- [RunPod](https://runpod.io?ref=ix73cnib) 账号、API key，以及一对 S3 API key（Settings → S3 API Keys）
- Docker（任意较新版本；已在 OrbStack/Docker Compose 验证），**或** 裸 Python ≥ 3.11
- 可选：Hugging Face / Civitai 只读 token，用于下载需要授权的模型

## 注册与获取 key

1. **注册 [RunPod](https://runpod.io?ref=ix73cnib) 账号**并充值。按秒计费、预付制——充 $10 就能用很久（一次完整的 2 小时 RTX 4090 会话、含 29 GB 模型预下载，约 $1.40）。
2. **API key**：在控制台打开 **Settings → API Keys → Create API Key**，授予读**和**写权限。控制器用它创建/删除 Pod 和网络卷、读取账单记录。
3. **S3 API key**：打开 **Settings → S3 API Keys** 创建一对。把 access key ID 和 secret 都记下来——它们用于认证兼容 S3 的网络卷 API，负责下载校验和输出采集。
4. 可选：只读的 [Hugging Face](https://huggingface.co/settings/tokens) 和/或 [Civitai](https://civitai.com/user/account) token，只有工作流要拉取需授权的模型时才需要。

## 配置 key

所有凭据放在一个 env 文件里，启动时加载一次（下载 token 只注入到临时 Pod 载荷中——agent 和界面都接触不到它们）：

```bash
mkdir -p ~/runpod-controller/secrets
cp controller.env.example ~/runpod-controller/secrets/controller.env
chmod 600 ~/runpod-controller/secrets/controller.env
```

编辑该文件，填入上一步拿到的值：

```ini
RUNPOD_API_KEY=...              # Settings → API Keys
RUNPODS3_ACCESS_KEY_ID=...      # Settings → S3 API Keys
RUNPODS3_SECRET_ACCESS_KEY=...
HF_TOKEN=...                    # 可选
CIVITAI_TOKEN=...               # 可选
```

如果控制器已在运行，改完要重启——该文件只在启动时读取。

**SSH key——通常无需任何操作。** 控制器需要一把 SSH key 来配置 Pod，按以下优先级选取：

1. **用你自己的**：把 `RUNPOD_SSH_KEY_PATH` 指向你的私钥（旁边需有同名 `.pub`）。
2. **已有的 `runpodctl` key**：`~/.runpod/ssh/runpodctl-ssh-key` 存在就自动复用——RunPod 老用户继续用账号上已注册的那把 key，零变化。
3. **自动生成**：以上都没有时，首次启动在 `<数据目录>/secrets/runpod-ssh-key` 生成一对 ed25519 密钥，并尽力把公钥注册到你的 RunPod 账号（注册失败时启动日志会打印公钥；只有你还想手动 SSH 进 Pod 才需要把它粘贴到 **Settings → SSH Public Keys**）。

无论哪种方式，控制器都会把 key 注入它创建的每个 Pod，并**合并你账号里已注册的全部公钥**——Pod 显式设置 `PUBLIC_KEY` 后 RunPod 就不再注入账号公钥，不做合并的话你个人的 key 会被锁在控制器创建的 Pod 门外。

## 快速开始（Docker）

```bash
docker compose up --build -d
```

打开 <http://localhost:8088>。状态持久化在 `~/runpod-controller`（宿主机路径用 `RUNPOD_CONTROLLER_DATA_DIR` 覆盖，宿主机端口用 `RUNPOD_CONTROLLER_PORT` 覆盖）。

## 快速开始（裸 Python）

无需安装任何依赖：

```bash
python3 -m controller.server
```

打开 <http://localhost:8088>。数据目录默认 `~/runpod-controller`（`CONTROLLER_DATA_DIR` 覆盖）；监听地址和端口来自 `CONTROLLER_HOST` / `CONTROLLER_PORT`。

## 使用流程

1. 在 Dashboard 点 **Create ComfyUI**，进入五步向导：上传工作流 JSON → 解析自定义节点（Comfy Registry 建议，锁定到精确 git 提交）→ 填模型链接（自动抓取大小、自动计算卷容量）→ 可选的 CPU 依赖探测 → 设置预算并启动。
   配置好的工作流可以**导出为分享包**（zip 内含工作流 JSON、锁定的节点和模型链接，token 已剥离）；在另一台控制器上导入该 zip，一次上传即可恢复全部配置。
2. 控制器在兼容机房间展开：每个机房一个网络卷加一个廉价 CPU Pod，并行下载你的模型。预下载完成的候选**串行**抢 GPU（同一时刻只有一台付费 GPU）；某个候选环境配置失败时，下一个已就绪的候选自动接棒。
3. 会话到达 `interactive_ready` 后，打开 ComfyUI 地址——**画布上已经载入你的工作流**，且模型加载节点的引用已被改写为卷上实际存在的文件（在库里换模型链接后无需再到 ComfyUI 里手改文件名）。输出每几分钟镜像到 `<数据目录>/artifacts/sessions/<id>/outputs/`。
4. **关闭**会先停 GPU，再做最终输出采集，之后才删除卷。如果采集失败、或一个交互过的会话没采到任何输出，卷会被保留以便恢复——丢弃它需要显式的 `discard_outputs` 操作。

界面是双语的：跟随浏览器语言（默认英文，中文通过 `Accept-Language` 或 `?lang=zh`）。所有时间按你的本地时区显示；存储保持 UTC。

## 配置

全部通过环境变量驱动，常用项：

| 变量 | 默认值 | 含义 |
|---|---|---|
| `CONTROLLER_DATA_DIR` | `~/runpod-controller`（Docker 内为 `/data`） | DB、产物、日志、密钥的根目录 |
| `CONTROLLER_SECRET_ENV_FILE` | `<数据目录>/secrets/controller.env` | 启动时加载的凭据文件 |
| `RUNPOD_SSH_KEY_PATH` | 自动生成于 `<数据目录>/secrets/runpod-ssh-key` | SSH 登录 Pod 用的私钥 |
| `CONTROLLER_HOST` / `CONTROLLER_PORT` | `127.0.0.1` / `8088` | 监听地址与端口 |
| `IDLE_SHUTDOWN_MINUTES` | `20` | 空闲回收窗口（续租可推后） |
| `OUTPUT_COLLECTOR_INTERVAL_SECONDS` | `300` | 后台输出镜像间隔 |
| `BILLING_WORKER_POLL_INTERVAL_SECONDS` | `600` | 账单校准轮询间隔 |
| `DEFAULT_VOLUME_SIZE_GB`、`DEFAULT_DATA_CENTER` 等 | 见 `controller/config.py` | 规划默认值 |

刻意**没有基于时间的硬上限**：花费由每个会话自己的 `Max total $` 约束。

## API

界面上的每个操作都是 JSON API 调用（`GET /api/v1/capabilities` 列出约 37 个端点）：上传/分析/探测工作流、试算规划、会话生命周期、向运行中的 Pod 下载/移动模型、只增不减的卷扩容、输出采集、账单同步。`skills/runpod-controller/SKILL.md` 记录了 LLM agent 驱动控制器的完整流程——agent 全程接触不到供应商凭据；`skills/comfyui-proxy-api/` 则封装了运行中会话的 ComfyUI API（提交任务、轮询结果、列出模型），自动带上 RunPod 的 Cloudflare 代理对 POST 要求的浏览器请求头。

## 开发

```bash
python3 -m unittest discover -s tests   # 142 个测试，无网络、不创建付费资源
```

测试基于假供应商适配器运行；真实环境行为（S3 实现的怪癖、模板差异）记录在 `docs/runpod-controller-v1.md`。

## 许可证

[MIT](LICENSE)
