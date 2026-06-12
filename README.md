English | [简体中文](README.zh-CN.md)

# RunPod ComfyUI Controller

[![CI](https://github.com/tomcatzh/runpod-comfyui-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/tomcatzh/runpod-comfyui-controller/actions/workflows/ci.yml)

A local-first session controller that runs interactive [ComfyUI](https://github.com/comfyanonymous/ComfyUI) on cheap [RunPod](https://www.runpod.io/) GPUs — with **money safety as a design constraint**, not an afterthought.

You upload a ComfyUI workflow JSON; the controller resolves its custom nodes against the Comfy Registry, sizes a network volume from real model metadata, pre-downloads models on cheap CPU pods across several datacenters in parallel, races for the cheapest eligible GPU, configures ComfyUI over SSH, and hands you a ready UI URL. When the session ends, the GPU stops first, every generated image is collected to your local disk through RunPod's S3-compatible API, and the volume is deleted only after collection succeeds.

Zero Python dependencies — standard library only. One SQLite file holds all state.

## Why

Running ComfyUI on rented GPUs usually means either a SaaS subscription or hand-managed pods where a forgotten session bills overnight and outputs die with the pod. This controller enforces:

- **A hard cap on spend, not time.** Every session carries a `Max total $` budget; the watchdog recomputes live spend each tick and force-reclaims at the cap (warning at 90%). An actively used session is never killed by the clock.
- **No silent losses.** Output collection is a gate, not best-effort: ComfyUI is forced to write outputs onto the network volume, a background collector mirrors them locally every few minutes, and shutdown refuses to delete the volume until a final collection succeeds.
- **No orphans.** Provider deletes are fail-closed (`cleanup_failed` is surfaced, never hidden), every candidate resource is scoped to its session, and a startup sweep reclaims anything a crash left behind.
- **Honest accounting.** Costs start as runtime estimates and are calibrated against real RunPod billing records by an in-server worker.

## Requirements

- A RunPod account, an API key, and an S3 API key pair (Settings → S3 API Keys)
- Docker (any recent version; tested with OrbStack/Docker Compose), **or** bare Python ≥ 3.11
- Optional: Hugging Face / Civitai read tokens for gated model downloads

## Quick start (Docker)

```bash
mkdir -p ~/runpod-controller/secrets
cp controller.env.example ~/runpod-controller/secrets/controller.env
# fill in RUNPOD_API_KEY and the S3 key pair, then:
docker compose up --build -d
```

Open <http://localhost:8088>. State persists under `~/runpod-controller` (override the host path with `RUNPOD_CONTROLLER_DATA_DIR`, the host port with `RUNPOD_CONTROLLER_PORT`).

## Quick start (bare Python)

No dependencies to install:

```bash
mkdir -p ~/runpod-controller/secrets
cp controller.env.example ~/runpod-controller/secrets/controller.env
# fill in the keys, then:
python3 -m controller.server
```

Open <http://localhost:8088>. The data directory defaults to `~/runpod-controller` (`CONTROLLER_DATA_DIR` overrides it); bind address and port come from `CONTROLLER_HOST` / `CONTROLLER_PORT`.

## Using it

1. **Create ComfyUI** on the dashboard opens a five-step wizard: upload a workflow JSON → resolve custom nodes (Comfy Registry suggestions, locked to exact git commits) → fill model URLs (sizes fetched automatically; the volume is auto-sized) → optional CPU dependency probe → set budget and launch.
2. The controller fans out across compatible datacenters: one network volume plus one cheap CPU pod each, downloading your models in parallel. Hydrated candidates then race for a GPU **serially** (only one paid GPU at a time); if environment configuration fails on one, the next hydrated candidate takes over.
3. When the session is `interactive_ready`, open the ComfyUI URL and work normally. Outputs are mirrored to `<data dir>/artifacts/sessions/<id>/outputs/` every few minutes.
4. **Shutdown** stops the GPU first, runs a final output collection, and only then deletes the volume. If collection fails or finds nothing for a session that was interactive, the volume is retained for recovery — discarding it requires an explicit `discard_outputs` action.

The UI is bilingual: it follows your browser language (English default, 中文 via `Accept-Language` or `?lang=zh`). All timestamps render in your local timezone; storage stays UTC.

## Configuration

Everything is environment-driven; the important ones:

| Variable | Default | Meaning |
|---|---|---|
| `CONTROLLER_DATA_DIR` | `~/runpod-controller` (`/data` in Docker) | Root for DB, artifacts, logs, secrets |
| `CONTROLLER_SECRET_ENV_FILE` | `<data dir>/secrets/controller.env` | Credentials file loaded at startup |
| `CONTROLLER_HOST` / `CONTROLLER_PORT` | `127.0.0.1` / `8088` | Bind address and port |
| `IDLE_SHUTDOWN_MINUTES` | `20` | Idle reclaim window (lease extensions push it out) |
| `OUTPUT_COLLECTOR_INTERVAL_SECONDS` | `300` | Background output mirror interval |
| `BILLING_WORKER_POLL_INTERVAL_SECONDS` | `600` | Billing calibration poll interval |
| `DEFAULT_VOLUME_SIZE_GB`, `DEFAULT_DATA_CENTER`, … | see `controller/config.py` | Planning defaults |

There is intentionally **no time-based hard cap**: spend is bounded by each session's `Max total $`.

## API

Every UI action is a JSON API call (`GET /api/v1/capabilities` lists ~37 endpoints): upload/analyze/probe workflows, dry-run planning, session lifecycle, runtime model downloads and moves onto a running pod, grow-only volume resize, output collection, and billing sync. `skills/runpod-controller/SKILL.md` documents the flow for LLM agents driving the controller without ever touching provider credentials.

## Development

```bash
python3 -m unittest discover -s tests   # 122 tests, no network, no paid resources
```

Tests run against a fake provider adapter; live behavior (S3 quirks, template differences) is documented in `docs/runpod-controller-v1.md`.

## License

[MIT](LICENSE)
