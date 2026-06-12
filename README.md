English | [简体中文](README.zh-CN.md)

# [RunPod](https://runpod.io?ref=ix73cnib) ComfyUI Controller

[![CI](https://github.com/tomcatzh/runpod-comfyui-controller/actions/workflows/ci.yml/badge.svg)](https://github.com/tomcatzh/runpod-comfyui-controller/actions/workflows/ci.yml)

A local-first session controller that runs interactive [ComfyUI](https://github.com/comfyanonymous/ComfyUI) on cheap [RunPod](https://runpod.io?ref=ix73cnib) GPUs — with **money safety as a design constraint**, not an afterthought.

[RunPod](https://runpod.io?ref=ix73cnib) is a GPU cloud with per-second billing: consumer cards like the RTX 4090 rent for well under a dollar an hour, network volumes persist data across pods, and an S3-compatible API gets your files out. That makes it a great fit for bursty ComfyUI sessions — as long as something trustworthy shuts everything down and collects the outputs. That something is this controller.

You upload a ComfyUI workflow JSON; the controller resolves its custom nodes against the Comfy Registry, sizes a network volume from real model metadata, pre-downloads models on cheap CPU pods across several datacenters in parallel, races for the cheapest eligible GPU, configures ComfyUI over SSH, and hands you a ready UI URL. When the session ends, the GPU stops first, every generated image is collected to your local disk through [RunPod](https://runpod.io?ref=ix73cnib)'s S3-compatible API, and the volume is deleted only after collection succeeds.

Zero Python dependencies — standard library only. One SQLite file holds all state.

## Why

Running ComfyUI on rented GPUs usually means either a SaaS subscription or hand-managed pods where a forgotten session bills overnight and outputs die with the pod. This controller enforces:

- **A hard cap on spend, not time.** Every session carries a `Max total $` budget; the watchdog recomputes live spend each tick and force-reclaims at the cap (warning at 90%). An actively used session is never killed by the clock.
- **No silent losses.** Output collection is a gate, not best-effort: ComfyUI is forced to write outputs onto the network volume, a background collector mirrors them locally every few minutes, and shutdown refuses to delete the volume until a final collection succeeds.
- **No orphans.** Provider deletes are fail-closed (`cleanup_failed` is surfaced, never hidden), every candidate resource is scoped to its session, and a startup sweep reclaims anything a crash left behind.
- **Honest accounting.** Costs start as runtime estimates and are calibrated against real [RunPod](https://runpod.io?ref=ix73cnib) billing records by an in-server worker.

## Requirements

- A [RunPod](https://runpod.io?ref=ix73cnib) account, an API key, and an S3 API key pair (Settings → S3 API Keys)
- Docker (any recent version; tested with OrbStack/Docker Compose), **or** bare Python ≥ 3.11
- Optional: Hugging Face / Civitai read tokens for gated model downloads

## Sign up and get your keys

1. **Create a [RunPod](https://runpod.io?ref=ix73cnib) account** and add some credit. Billing is prepaid and per-second — $10 goes a long way (a full 2-hour RTX 4090 session with 29 GB of model pre-downloads costs about $1.40).
2. **API key**: in the console, open **Settings → API Keys → Create API Key** and grant read **and** write permission. The controller uses it to create and delete pods and network volumes and to read billing records.
3. **S3 API key**: open **Settings → S3 API Keys** and create one. Save both the access key ID and the secret — they authenticate the S3-compatible network-volume API used for download verification and output collection.
4. Optional: read-only [Hugging Face](https://huggingface.co/settings/tokens) and/or [Civitai](https://civitai.com/user/account) tokens, only needed if your workflows pull gated models.

## Configure the keys

All credentials live in one env file, loaded once at startup (download tokens are injected only into transient pod payloads — agents and the UI never see them):

```bash
mkdir -p ~/runpod-controller/secrets
cp controller.env.example ~/runpod-controller/secrets/controller.env
chmod 600 ~/runpod-controller/secrets/controller.env
```

Edit the file and fill in the values from the previous step:

```ini
RUNPOD_API_KEY=...              # Settings → API Keys
RUNPODS3_ACCESS_KEY_ID=...      # Settings → S3 API Keys
RUNPODS3_SECRET_ACCESS_KEY=...
HF_TOKEN=...                    # optional
CIVITAI_TOKEN=...               # optional
```

If the controller is already running, restart it after editing — the file is read at startup only.

**SSH key — usually nothing to do.** The controller needs an SSH key to configure pods, and picks one in this order:

1. **Bring your own**: set `RUNPOD_SSH_KEY_PATH` to your private key (the matching `.pub` must sit next to it).
2. **Existing `runpodctl` key**: `~/.runpod/ssh/runpodctl-ssh-key` is reused automatically if present — long-time RunPod users keep working with the key already registered on their account.
3. **Auto-generate**: otherwise, first start creates an ed25519 keypair at `<data dir>/secrets/runpod-ssh-key` and best-effort registers the public key on your RunPod account (if registration fails, the startup log prints the key; paste it into **Settings → SSH Public Keys** only if you also want manual SSH).

Either way, the controller injects the key into every pod it creates and **merges in the public keys already registered on your account** — RunPod skips its own account-key injection when a pod sets `PUBLIC_KEY` explicitly, so without the merge your personal key would be locked out of controller-created pods.

## Quick start (Docker)

```bash
docker compose up --build -d
```

Open <http://localhost:8088>. State persists under `~/runpod-controller` (override the host path with `RUNPOD_CONTROLLER_DATA_DIR`, the host port with `RUNPOD_CONTROLLER_PORT`).

## Quick start (bare Python)

No dependencies to install:

```bash
python3 -m controller.server
```

Open <http://localhost:8088>. The data directory defaults to `~/runpod-controller` (`CONTROLLER_DATA_DIR` overrides it); bind address and port come from `CONTROLLER_HOST` / `CONTROLLER_PORT`.

## Using it

1. **Create ComfyUI** on the dashboard opens a five-step wizard: upload a workflow JSON → resolve custom nodes (Comfy Registry suggestions, locked to exact git commits) → fill model URLs (sizes fetched automatically; the volume is auto-sized) → optional CPU dependency probe → set budget and launch.
   A prepared workflow can be **exported as a shareable package** (a zip with the workflow JSON plus locked nodes and model URLs, tokens stripped); importing the zip on another controller restores everything in one upload.
2. The controller fans out across compatible datacenters: one network volume plus one cheap CPU pod each, downloading your models in parallel. Hydrated candidates then race for a GPU **serially** (only one paid GPU at a time); if environment configuration fails on one, the next hydrated candidate takes over.
3. When the session is `interactive_ready`, open the ComfyUI URL — **your workflow is already loaded on the canvas**, with model loader references rewritten to the files actually placed on the volume (so swapping a model URL in the library needs no manual edits in ComfyUI). Outputs are mirrored to `<data dir>/artifacts/sessions/<id>/outputs/` every few minutes.
4. **Shutdown** stops the GPU first, runs a final output collection, and only then touches the volume. The default shutdown keeps the volume **warm** (see below); "Shutdown + delete volume" removes it immediately. If collection fails or finds nothing for a session that was interactive, the volume is retained for recovery — discarding it requires an explicit `discard_outputs` action.

### Warm volumes

A network volume that already holds your downloaded models is the expensive part of startup — not in dollars, but in time. So by default the controller **keeps the hydrated volume after shutdown** instead of deleting it:

- **Next launch is fast.** When a new session needs the same models, the warm volume is reused automatically: the whole multi-datacenter pre-download phase is skipped and the session is ready in ~3–6 minutes instead of 7–12. The wizard marks reusable workflows with ♨ in the library dropdown and in the dry-run result.
- **It costs cents, bounded.** Storage is $0.07/GB/month (≈$0.09/day for 40 GB). An idle warm volume auto-deletes after `WARM_VOLUME_IDLE_TTL_HOURS` (default 72 h); every reuse renews the clock. Strictly speaking re-downloading is cheaper (~$0.03 in CPU time) — a warm volume buys back your waiting minutes, not money.
- **Nothing hides.** Warm volumes are listed on the dashboard with datacenter, size, expiry, and accumulated cost, each with a *Delete now* button. Forced reclaims (cost cap, idle timeout) keep the volume warm by default, since an interrupted session usually relaunches soon.
- **Staleness is safe.** If a warm volume somehow no longer matches (models changed, files missing), environment configuration detects it and the launch fails over to a freshly hydrated candidate.

The UI is bilingual: it follows your browser language (English default, 中文 via `Accept-Language` or `?lang=zh`). All timestamps render in your local timezone; storage stays UTC.

## Configuration

Everything is environment-driven; the important ones:

| Variable | Default | Meaning |
|---|---|---|
| `CONTROLLER_DATA_DIR` | `~/runpod-controller` (`/data` in Docker) | Root for DB, artifacts, logs, secrets |
| `CONTROLLER_SECRET_ENV_FILE` | `<data dir>/secrets/controller.env` | Credentials file loaded at startup |
| `RUNPOD_SSH_KEY_PATH` | auto-generated `<data dir>/secrets/runpod-ssh-key` | Private key used to SSH into pods |
| `CONTROLLER_HOST` / `CONTROLLER_PORT` | `127.0.0.1` / `8088` | Bind address and port |
| `IDLE_SHUTDOWN_MINUTES` | `20` | Idle reclaim window (lease extensions push it out) |
| `OUTPUT_COLLECTOR_INTERVAL_SECONDS` | `300` | Background output mirror interval |
| `WARM_VOLUME_IDLE_TTL_HOURS` | `72` | Idle TTL before a kept-warm volume is auto-deleted |
| `BILLING_WORKER_POLL_INTERVAL_SECONDS` | `600` | Billing calibration poll interval |
| `DEFAULT_VOLUME_SIZE_GB`, `DEFAULT_DATA_CENTER`, … | see `controller/config.py` | Planning defaults |

There is intentionally **no time-based hard cap**: spend is bounded by each session's `Max total $`.

## API

Every UI action is a JSON API call (`GET /api/v1/capabilities` lists ~37 endpoints): upload/analyze/probe workflows, dry-run planning, session lifecycle, runtime model downloads and moves onto a running pod, grow-only volume resize, output collection, and billing sync. `skills/runpod-controller/SKILL.md` documents the flow for LLM agents driving the controller without ever touching provider credentials.

## Development

```bash
python3 -m unittest discover -s tests   # full suite: no network, no paid resources
```

Tests run against a fake provider adapter; live behavior (S3 quirks, template differences) is documented in `docs/runpod-controller-v1.md`.

## License

[MIT](LICENSE)
