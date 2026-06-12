[English](README.md) | 简体中文

# RunPod ComfyUI Controller

[![CI](https://github.com/tomcatzh/runpod-comfyui-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/tomcatzh/runpod-comfyui-controller/actions/workflows/ci.yml)

一个本地优先的会话控制器，在廉价的 [RunPod](https://www.runpod.io/) GPU 上运行交互式 [ComfyUI](https://github.com/comfyanonymous/ComfyUI) —— **把资金安全作为设计约束**，而不是事后补丁。

你上传一个 ComfyUI 工作流 JSON；控制器自动对照 Comfy Registry 解析自定义节点、根据真实模型元数据计算网络卷容量、在多个机房并行用廉价 CPU Pod 预下载模型、抢占最便宜的合格 GPU、通过 SSH 配置好 ComfyUI，然后把就绪的 UI 地址交给你。会话结束时，先停 GPU，再通过 RunPod 兼容 S3 的 API 把每一张生成的图片采集到本地磁盘，**采集成功后才删除网络卷**。

零 Python 依赖——只用标准库。全部状态存在一个 SQLite 文件里。

## 为什么做这个

在租用 GPU 上跑 ComfyUI，通常要么订阅 SaaS，要么手动管理 Pod——忘了关就烧一整晚的钱，输出也随 Pod 一起消失。这个控制器强制保证：

- **硬上限是花费，不是时间。** 每个会话带一个 `Max total $` 预算；看门狗每个周期重算实时花费，到达预算强制回收（90% 时预警）。正在使用中的会话永远不会被时钟杀死。
- **不静默丢失任何东西。** 输出采集是门槛而非尽力而为：ComfyUI 被强制把输出写到网络卷上，后台采集器每几分钟镜像到本地，关闭流程在最终采集成功前拒绝删卷。
- **不留孤儿资源。** 供应商删除调用采用 fail-closed 语义（`cleanup_failed` 会被暴露而非隐藏），每个候选资源都限定在自己的会话范围内，启动时的清扫会回收崩溃遗留的一切。
- **诚实记账。** 成本先按运行时长估算，随后由 server 内置的账单 worker 对照 RunPod 真实账单记录校准。

## 环境要求

- RunPod 账号、API key，以及一对 S3 API key（Settings → S3 API Keys）
- Docker（任意较新版本；已在 OrbStack/Docker Compose 验证），**或** 裸 Python ≥ 3.11
- 可选：Hugging Face / Civitai 只读 token，用于下载需要授权的模型

## 快速开始（Docker）

```bash
mkdir -p ~/runpod-controller/secrets
cp controller.env.example ~/runpod-controller/secrets/controller.env
# 填入 RUNPOD_API_KEY 和 S3 key 对，然后：
docker compose up --build -d
```

打开 <http://localhost:8088>。状态持久化在 `~/runpod-controller`（宿主机路径用 `RUNPOD_CONTROLLER_DATA_DIR` 覆盖，宿主机端口用 `RUNPOD_CONTROLLER_PORT` 覆盖）。

## 快速开始（裸 Python）

无需安装任何依赖：

```bash
mkdir -p ~/runpod-controller/secrets
cp controller.env.example ~/runpod-controller/secrets/controller.env
# 填入密钥，然后：
python3 -m controller.server
```

打开 <http://localhost:8088>。数据目录默认 `~/runpod-controller`（`CONTROLLER_DATA_DIR` 覆盖）；监听地址和端口来自 `CONTROLLER_HOST` / `CONTROLLER_PORT`。

## 使用流程

1. 在 Dashboard 点 **Create ComfyUI**，进入五步向导：上传工作流 JSON → 解析自定义节点（Comfy Registry 建议，锁定到精确 git 提交）→ 填模型链接（自动抓取大小、自动计算卷容量）→ 可选的 CPU 依赖探测 → 设置预算并启动。
2. 控制器在兼容机房间展开：每个机房一个网络卷加一个廉价 CPU Pod，并行下载你的模型。预下载完成的候选**串行**抢 GPU（同一时刻只有一台付费 GPU）；某个候选环境配置失败时，下一个已就绪的候选自动接棒。
3. 会话到达 `interactive_ready` 后，打开 ComfyUI 地址正常使用。输出每几分钟镜像到 `<数据目录>/artifacts/sessions/<id>/outputs/`。
4. **关闭**会先停 GPU，再做最终输出采集，之后才删除卷。如果采集失败、或一个交互过的会话没采到任何输出，卷会被保留以便恢复——丢弃它需要显式的 `discard_outputs` 操作。

界面是双语的：跟随浏览器语言（默认英文，中文通过 `Accept-Language` 或 `?lang=zh`）。所有时间按你的本地时区显示；存储保持 UTC。

## 配置

全部通过环境变量驱动，常用项：

| 变量 | 默认值 | 含义 |
|---|---|---|
| `CONTROLLER_DATA_DIR` | `~/runpod-controller`（Docker 内为 `/data`） | DB、产物、日志、密钥的根目录 |
| `CONTROLLER_SECRET_ENV_FILE` | `<数据目录>/secrets/controller.env` | 启动时加载的凭据文件 |
| `CONTROLLER_HOST` / `CONTROLLER_PORT` | `127.0.0.1` / `8088` | 监听地址与端口 |
| `IDLE_SHUTDOWN_MINUTES` | `20` | 空闲回收窗口（续租可推后） |
| `OUTPUT_COLLECTOR_INTERVAL_SECONDS` | `300` | 后台输出镜像间隔 |
| `BILLING_WORKER_POLL_INTERVAL_SECONDS` | `600` | 账单校准轮询间隔 |
| `DEFAULT_VOLUME_SIZE_GB`、`DEFAULT_DATA_CENTER` 等 | 见 `controller/config.py` | 规划默认值 |

刻意**没有基于时间的硬上限**：花费由每个会话自己的 `Max total $` 约束。

## API

界面上的每个操作都是 JSON API 调用（`GET /api/v1/capabilities` 列出约 37 个端点）：上传/分析/探测工作流、试算规划、会话生命周期、向运行中的 Pod 下载/移动模型、只增不减的卷扩容、输出采集、账单同步。`skills/runpod-controller/SKILL.md` 记录了 LLM agent 驱动控制器的完整流程——agent 全程接触不到供应商凭据。

## 开发

```bash
python3 -m unittest discover -s tests   # 122 个测试，无网络、不创建付费资源
```

测试基于假供应商适配器运行；真实环境行为（S3 实现的怪癖、模板差异）记录在 `docs/runpod-controller-v1.md`。

## 许可证

[MIT](LICENSE)
