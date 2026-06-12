# RunPod Controller V1

## Overview

This controller is a local-first ComfyUI session manager. It runs either bare (`python -m controller.server`) or through Docker Compose, and persists all state under one data directory (default `~/runpod-controller`, override with `CONTROLLER_DATA_DIR`; the Compose bind mount defaults to the same path and is overridden with `RUNPOD_CONTROLLER_DATA_DIR`).

The controller defaults to live RunPod operation. The explicit `Dry run` button inspects candidate datacenters and GPU types without creating sessions, Pods, or volumes. `Create ComfyUI` can create paid CPU and GPU resources, guarded by intent, `Max $/hr`, `Min VRAM GB`, `Max total $`, watchdog, and cleanup policy.

## Lifecycle

1. Workflow analysis parses uploaded UI workflow JSON, canonicalizes it by hash, extracts model requirements, surfaces custom-node decisions, and blocks launch when executable custom nodes remain unresolved.
2. Dependency probe checks the workflow dependency fingerprint. A cached passing probe is reused; a new/changed workflow/node/base-template lock gets one cheap CPU probe before paid GPU fan-out.
3. Planning peeks model URLs, computes a network-volume size, and selects candidate datacenters.
4. Each candidate datacenter runs in its own worker: create network volume, start CPU hydration Pod, download portable assets and the resolved launch bundle to `/workspace/runpod-controller/...`, wait for the CPU Pod to exit, verify remote S3-visible hydration markers and asset sizes, then delete the CPU Pod.
5. Hydrated candidates race for a GPU with concurrent Pod-create calls, not a serialized winner lock. The first successfully promoted GPU Pod becomes the winner; loser workers delete their own Pods and network volumes, including loser GPU Pods that were created during the race.
6. Candidate GPUs are selected from the same per-datacenter matrix used by dry run, then raced concurrently after CPU hydration succeeds.
7. The winner records GPU acquisition attempts, configures hydrated assets and locked custom nodes into the detected ComfyUI tree over SSH, verifies local ComfyUI health/model/custom-node visibility, then exposes a UI route and marks the session `interactive_ready`.
8. Watchdog policy tracks a `20m` idle shutdown and `5m` warning window by default, and the server starts a background watchdog loop. The hard cap is on spend, not time: the watchdog reclaims a session when its effective cost reaches the session's `max_total_usd` budget, warning at 90% first. An actively used session is never killed by the clock.
9. Finalization writes local `DONE.json` and checksums.
10. Reclaim deletes Pods, closes tunnels, and deletes the temporary network volume when retention policy allows it.

## Run Locally

```bash
mkdir -p ~/runpod-controller/{config,secrets,db,artifacts,logs,cache,backups}
docker compose up --build
```

Open `http://localhost:8088`.

Store model-download credentials in `~/runpod-controller/secrets/controller.env`:

```text
HF_TOKEN=...
CIVITAI_TOKEN=...
```

The controller also reads this file when running locally with `python -m controller`, not only in Docker Compose. Tokens are injected into CPU hydration Pods only when matching Hugging Face or Civitai assets are present.

The Web UI is split into `Active` and `History`. The `Active` home page shows only currently active sessions and live spend counters; deleted/reclaimed sessions move to `History`, with full pagination and filters deferred for later.

Click `Create ComfyUI` on the `Active` page to open `/comfyui/new`, a full-page ComfyUI-only wizard. Other products should get their own design later rather than sharing this UI prematurely. The preferred path is now a saved ComfyUI workflow: uploaded UI workflow JSON first, then custom-node decisions, model URLs/sizes, cached CPU dependency probe, and launch intent. Legacy model templates and launch-template APIs remain only as compatibility records.

## ComfyUI Workflows

A ComfyUI workflow stores:

- display `name`
- canonical UI workflow JSON
- `workflow_hash`
- extracted model requirements
- extra or replacement assets
- node mappings and locked node repositories
- generated install and validation plans
- RunPod base template/image lock evidence
- dependency and launch fingerprints
- last cached dependency probe result
- live verification evidence after output collection

UI workflow JSON is the canonical dependency-analysis input. API workflow JSON is not collected by the create UI in V1; automated prompt conversion/fetching is deferred.

The analyzer extracts node `class_type`/type usage, ignores notes/groups/corrupt non-executable nodes with warnings, and asks the user to resolve executable custom nodes. Comfy Registry lookup through the official `GET /comfy-nodes/{comfyNodeName}/node` API provides suggestions, but a suggestion does not count as resolved until the user accepts it or supplies a Git repository mapping. A repository URL without a ref means “lock the current default branch commit now,” and future launches use that locked commit.

Before paid GPU launch, the controller runs a cached CPU dependency probe when the workflow dependency fingerprint is new or changed. The dependency fingerprint covers the canonical workflow hash, node locks, and intended base RunPod template/image facts. Model URL changes affect the launch fingerprint but do not force a dependency probe unless node or base-template locks change. The probe uses a cheap CPU Pod and a small network volume, stages custom-node repos/install scripts, writes probe markers/checksums, verifies through the S3-compatible network-volume API when credentials are available, then deletes the CPU Pod and volume.

CPU probe is dependency screening, not final CUDA proof. Winner GPU environment configuration still detects the actual ComfyUI root and Python environment, installs locked custom nodes into `ComfyUI/custom_nodes`, runs requirements with the ComfyUI Python, restarts ComfyUI if needed, checks `/system_stats`, `/object_info`, model visibility, expected custom-node visibility, and missing-node absence. A workflow becomes `live_verified` only after a successful live session collects at least one output artifact and validation passes.

Custom RunPod Docker/Pod template generation is intentionally deferred. Successful probes and GPU install facts are recorded as bake candidates, but the controller does not build images, push registries, or create custom RunPod templates yet. Image baking should start only after repeated workflow locks prove stable.

Workflow APIs:

```bash
curl -sS http://localhost:8088/api/v1/comfyui/workflows
curl -sS -X POST http://localhost:8088/api/v1/comfyui/workflows/upload \
  -H 'Content-Type: application/json' \
  -d '{"filename":"workflow.json","content":"{\"nodes\":[]}"}'
curl -sS -X POST http://localhost:8088/api/v1/comfyui/workflows/WORKFLOW_ID/nodes/resolve \
  -H 'Content-Type: application/json' \
  -d '{"decision":"install_git_repo","class_type":"Custom Node Name","package":"custom-node-package","repo_url":"https://github.com/org/repo"}'
curl -sS -X POST http://localhost:8088/api/v1/comfyui/workflows/WORKFLOW_ID/probe
curl -sS http://localhost:8088/api/v1/comfyui/probes/PROBE_ID
```

Resource requests accept `workflow_id`; inline assets/workflow remain allowed for advanced callers, and `launch_template_id` remains a legacy compatibility alias:

```bash
curl -sS -X POST http://localhost:8088/api/v1/resource-requests \
  -H 'Content-Type: application/json' \
  -d '{"product":"comfyui","workflow_id":"WORKFLOW_ID","min_vram_gb":24,"max_gpu_usd_per_hr":1.25}'
```

The wizard extracts model-like requirements from the workflow and keeps the single `URL + ComfyUI folder + Add` row for extra or replacement models. `Add` calls `POST /api/v1/assets/peek`, which checks the local URL metadata cache first, then uses HTTP `HEAD` and a one-byte `Range` fallback to discover filename, size, provider, final URL, and redirect chain. Token values are injected server-side for Hugging Face or Civitai but are never persisted in the cache or API response. Civitai provider detection covers `civitai.com`, `civitai.red`, and `civitai.green`; HTTP 4xx/5xx responses fail the peek instead of being added as bogus model rows.

Supported ComfyUI folder targets are:

```text
checkpoints
diffusion_models
loras
vae
controlnet
clip
clip_vision
embeddings
upscale_models
configs
gligen
hypernetworks
```

Those quick entries are staged under `/workspace/runpod-controller/assets/comfyui/<folder>/...` during CPU hydration. During GPU winner promotion, the controller connects over SSH, detects the actual ComfyUI root, creates symlinks into `ComfyUI/models/<folder>/...` (including a `models/unet` mirror for `diffusion_models`), restarts ComfyUI when needed, and only then moves the session to `interactive_ready`.

Standalone model-list templates are no longer part of the Web UI. The URL metadata cache is persistent and does not expire automatically; use `Refresh metadata` to force a new peek. The same normalized URL cannot be added twice to one ComfyUI launch, even if the requested folder differs; if that rare case appears later, the controller should hydrate once and create a copy or symlink. Volume size is computed as `max(10GB, ceil(total_asset_bytes * 1.20 / 1GiB) + 5GB)`, leaving practical space for ComfyUI outputs, previews, logs, marker files, and temporary runtime writes. Unknown-size assets must be filled manually before a session can be created.

GPU selection is expressed only as intent: `Min VRAM GB` defaults to `24`, `GPU vendor` defaults to `NVIDIA`, and `Max $/hr` is the hourly budget ceiling. AMD GPU types exist on RunPod, but the ComfyUI controller keeps V1 NVIDIA-only to avoid mixing CUDA and ROCm runtime assumptions.

Use `Dry run` before `Create ComfyUI` to compute the candidate plan. Dry run first limits datacenters to locations that are compatible with both RunPod S3-backed network volumes and REST Pod creation, then tries RunPod GraphQL per-datacenter `lowestPrice` for GPU stock and price. If GraphQL is blocked, the controller falls back to `runpodctl datacenter list -o json` and reads each datacenter's `gpuAvailability`. Rows from `runpodctl` have per-datacenter stock status, while hourly rates are catalog estimates until Pod creation or billing reconciliation provides the final provider price. Dry run does not return a global GPU list, and it omits scanned datacenters that have no matching GPU rows; the primary `Datacenters` count means candidate datacenters shown in the result, not all datacenters scanned.

After create, the browser goes to `/sessions/<id>/workflow`. The create request returns after planning and starting the background workflow; callers should poll the request/session for phase changes. The create form no longer asks for a datacenter; the controller plans location candidates during workflow startup. The workflow page shows candidate progress, CPU hydration progress, GPU attempt counts, winner/loser cleanup status, workflow events, `Open ComfyUI`, `Confirm`, and `Terminate and cleanup`. For automated browser testing, append `?bypass_confirm=1` or set `localStorage.runpodControllerBypassConfirm = "1"` to skip browser confirmation dialogs; JSON API endpoints never require browser confirmation.

Create and dry run now share the same candidate matrix. For each datacenter, create chooses the lowest-rate eligible GPU row for the current `Min VRAM GB`, `GPU vendor`, and `Max $/hr` intent, rather than using legacy hidden GPU selection state. `Max total $` includes the winner GPU lease estimate plus fan-out CPU hydration and temporary network-volume estimates.

Cleanup is fail-closed and session-scoped. The controller only marks a Pod or network volume `deleted` after the provider delete call returns `ok`; otherwise the session/candidate becomes `cleanup_failed` so the UI keeps showing the resource that still needs attention. Candidate cleanup checks `session_id`, `workflow_id`, and candidate-owned pod/volume ids, so reclaiming or terminating one session does not delete another session's running or racing resources. If no candidate wins a GPU, all owned candidate resources for that workflow are cleaned by default.

## Example API

```bash
curl -sS -X POST http://localhost:8088/api/v1/resource-requests \
  -H 'Content-Type: application/json' \
  -d '{"product":"comfyui","mode":"interactive","network_volume_size_gb":10}'
```

Run a no-resource dry run:

```bash
curl -sS -X POST http://localhost:8088/api/v1/resource-requests/dry-run \
  -H 'Content-Type: application/json' \
  -d '{"product":"comfyui","mode":"interactive","min_vram_gb":24,"gpu_vendor":"NVIDIA","max_gpu_usd_per_hr":1.25}'
```

Poll the returned request:

```bash
curl -sS http://localhost:8088/api/v1/resource-requests/REQ_ID
```

Create a workflow with explicit assets. Candidate datacenters are planned by the controller unless an advanced API caller explicitly provides `data_centers` or `candidate_data_centers`:

```bash
curl -sS -X POST http://localhost:8088/api/v1/resource-requests \
  -H 'Content-Type: application/json' \
  -d '{
    "product":"comfyui",
    "mode":"interactive",
    "min_vram_gb":24,
    "gpu_vendor":"NVIDIA",
    "max_gpu_usd_per_hr":1.25,
    "assets":[
      {
        "url":"https://huggingface.co/org/model/resolve/main/model.safetensors",
        "model_folder":"checkpoints",
        "filename":"model.safetensors",
        "size_bytes":123456789
      }
    ]
  }'
```

Useful interactive controls:

```bash
curl -sS -X POST http://localhost:8088/api/v1/assets/peek \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://huggingface.co/org/model/resolve/main/model.safetensors","model_folder":"checkpoints"}'
curl -sS -X POST http://localhost:8088/api/v1/sessions/SESSION_ID/tunnel/restart
curl -sS -X POST http://localhost:8088/api/v1/sessions/SESSION_ID/watchdog/tick \
  -H 'Content-Type: application/json' \
  -d '{"queue_active":false,"output_active":false}'
curl -sS -X POST http://localhost:8088/api/v1/sessions/SESSION_ID/watchdog/pause
curl -sS -X POST http://localhost:8088/api/v1/sessions/SESSION_ID/watchdog/resume
curl -sS -X POST http://localhost:8088/api/v1/sessions/SESSION_ID/reclaim \
  -H 'Content-Type: application/json' \
  -d '{"force":true}'
curl -sS -X POST http://localhost:8088/api/v1/sessions/SESSION_ID/workflow/terminate
```

Session pages are split into five tabs sharing one header: `Overview` (`/sessions/<id>`), `Workflow`, `Models` (runtime model manager plus grow-only volume resize), `Outputs` (collection summary, manual collect, and `Discard outputs + delete volume` for retained volumes via the `discard_outputs` reclaim flag), and `Debug` (raw rows, attempts, tunnels, watchdog, pods, volume, audit). The header shows a `Shutdown: stop GPU + collect outputs` button for active sessions only; it confirms first, stops GPU compute, runs a final S3 output collection, and deletes the network volume only after a successful collection. Terminal sessions hide all mutating controls.

## ComfyUI Paths And Output Collection

Do not assume a model downloaded during CPU hydration is automatically visible to ComfyUI. RunPod mounts the network volume at `/workspace`, but ComfyUI normally reads model files from its own `ComfyUI/models/<type>` folders or paths configured through `extra_model_paths.yaml`.

The intended live path is:

1. CPU hydration downloads portable assets under `/workspace/runpod-controller/assets/comfyui/<type>/...`.
2. After GPU Pod SSH is reachable, the controller detects the actual ComfyUI root and output directory.
3. The controller writes `pod-info.json`, creates symlinks into the detected `ComfyUI/models/<type>` folders, restarts ComfyUI only when model visibility is still missing, and gates `interactive_ready` on local `/system_stats` plus model-list visibility.
4. The controller restarts ComfyUI with `--output-directory /workspace/runpod-controller/runs/<session_id>/outputs/raw` so all `SaveImage` output lands on the network volume, and refuses `interactive_ready` if that directory cannot be created.
5. A background collector copies S3-visible outputs into the local artifacts directory every few minutes; on shutdown the controller stops GPU compute first, runs a final S3 collection (paginated), and deletes the network volume only after the collection succeeds.

RunPod S3 collection needs S3 API credentials, separate from the normal RunPod API key:

```text
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

When running in Docker, Compose publishes a localhost-only tunnel range for the future SSH forwarder:

```text
127.0.0.1:18180-18220:18180-18220
```

The controller image installs `runpodctl` for both amd64 and arm64 Docker builds so the dry-run datacenter fallback works inside the container, not only on the host.

## Billing Calibration

The dashboard keeps two cost views:

- `estimated_cost_usd`: local runtime multiplied by the known hourly rate.
- `effective_cost_usd`: the value queries should use. It equals RunPod billing data when imported, otherwise it falls back to `estimated_cost_usd`.

Sync billing records:

```bash
curl -sS -X POST http://localhost:8088/api/v1/billing/sync \
  -H 'Content-Type: application/json' \
  -d '{"lookback_days":30,"bucket_size":"hour"}'
```

Inspect imported records:

```bash
curl -sS http://localhost:8088/api/v1/billing/records
```

Pod billing is matched by RunPod `podId`, and updates `actual_cost_usd`, `billed_start_at`, `billed_end_at`, and `billed_time_ms` on local Pod rows. RunPod network volume billing is currently imported as account-level storage billing unless the billing record includes a concrete volume id. The importer accepts both `time` and `startDate` bucket fields, and both `diskSpaceBilledGb` and `diskSpaceBilledGB` disk fields.

Run the calibration worker locally in watch mode:

```bash
CONTROLLER_DATA_DIR=~/runpod-controller RUNPOD_MODE=live_cpu_test \
  python -m controller.billing_worker --watch
```

The worker scans SQLite for terminal real resources that are not calibrated yet, syncs billing, and sleeps for `BILLING_WORKER_POLL_INTERVAL_SECONDS` seconds between polls. The default interval is `600` seconds. In `--watch` mode it keeps running even when no candidates currently exist, so it can catch later sessions. Without `--watch`, it exits when no uncalibrated resources remain. For a single attempt:

```bash
python -m controller.billing_worker --once
```

With Docker Compose, `billing-worker` is a default companion service running `python -m controller.billing_worker --watch` with `restart: unless-stopped`.

CPU-only Pods can be marked with `billing_source='runpod_billing_absent'` when RunPod billing does not return a per-Pod record in a complete billing window after `BILLING_CPU_ABSENT_GRACE_HOURS` hours. The default grace period is `24` hours. This does not set `actual_cost_usd=0`; those Pods remain estimate-based so spend is not underreported. GPU Pods continue waiting for provider billing.

## Live CPU Test Mode

Set `RUNPOD_MODE=live_cpu_test` and provide `RUNPOD_API_KEY` in `~/runpod-controller/secrets/controller.env` to create real CPU hydration Pods while keeping the run scoped to CPU validation.

For live GPU smoke, use `Create ComfyUI` with a small model/template and tight `Max $/hr`, `Min VRAM GB`, and `Max total $`. The controller uses the same per-datacenter scout as dry run before creating a paid GPU Pod.

## Live Verification

The full path has been verified repeatedly with paid live runs: multi-datacenter candidate fan-out, CPU hydration, GPU acquisition with cold-standby failover, real ComfyUI image generation through the `/prompt` API, S3 output collection into local artifacts, full reclaim, and provider-side `404` confirmation for every created Pod and network volume. Typical cost of one short verification session is well under one US dollar.
