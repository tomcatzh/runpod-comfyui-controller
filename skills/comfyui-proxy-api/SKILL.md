---
name: comfyui-proxy-api
description: Call the ComfyUI API of a running session through its RunPod proxy URL (queue prompts, poll results, list models, download images) with the browser headers Cloudflare requires; use comfyui_api.py instead of raw curl/urllib.
---

# ComfyUI Proxy API Skill

## Why this exists

RunPod proxy URLs (`https://<pod>-8188.proxy.runpod.net`) sit behind Cloudflare. Plain-client requests fail in confusing ways:

- **POST without a browser-like `User-Agent` returns 403** (`/prompt` etc.). GETs may pass with default UAs, so the failure looks intermittent.
- The RunPod GraphQL API blocks UA-less calls the same way (Cloudflare error 1010).

`comfyui_api.py` (stdlib only, same directory) sends a proven header set — browser `User-Agent` + `Origin` + `Referer` — on every request. Use it instead of raw `curl`/`urllib` whenever you talk to a session's ComfyUI.

## Getting the base URL

The base URL is the session's `ui_url` from the controller:

```bash
curl -sS http://localhost:8088/api/v1/sessions/<session_id> | python3 -c 'import json,sys; print(json.load(sys.stdin)["ui_url"])'
```

The session must be `interactive_ready`.

## Commands

List the model files a loader can see (verifies hydration/symlinks):

```bash
python3 skills/comfyui-proxy-api/comfyui_api.py "$UI_URL" models UNETLoader
python3 skills/comfyui-proxy-api/comfyui_api.py "$UI_URL" models CheckpointLoaderSimple
```

Queue a workflow and wait for the images:

```bash
python3 skills/comfyui-proxy-api/comfyui_api.py "$UI_URL" prompt graph.json --wait --timeout 600
```

- `graph.json` must be **API format** (node id → `{class_type, inputs}`), not the UI format the controller stores. In ComfyUI: Workflow → Export (API). The script rejects UI-format input with a pointer.
- `--wait` polls `/history/<prompt_id>` and prints `{ok, status, images}`; without it you get the `prompt_id` back immediately.

Any JSON GET (health, queue, history):

```bash
python3 skills/comfyui-proxy-api/comfyui_api.py "$UI_URL" get /system_stats
python3 skills/comfyui-proxy-api/comfyui_api.py "$UI_URL" get /queue
```

Download a generated image directly (optional — see below):

```bash
python3 skills/comfyui-proxy-api/comfyui_api.py "$UI_URL" download controller-live-verify_00001_.png -o /tmp/out.png
```

## Notes

- Generated images land on the network volume and are mirrored to the local `artifacts/sessions/<id>/outputs/` directory by the controller's collector (periodic + on shutdown). `download` is for quick checks, not for collection — never rely on it as the only copy.
- A queued prompt keeps the GPU busy; the controller's cost cap still applies. Reclaim the session when done (see the `runpod-controller` skill).
- Live-verified 2026-06-12 on session `ses_8cbc6725...`: 403-without-UA reproduced, header set confirmed, two images generated and collected.
