---
name: runpod-controller
description: Use the local RunPod controller API to request ComfyUI sessions, poll resource readiness, extend leases, record task metadata, finalize artifacts, and reclaim resources without exposing RunPod or S3 credentials to the calling agent.
---

# RunPod Controller Skill

## Safety

- Never ask for or handle RunPod/S3 keys directly. The controller owns provider credentials.
- Use dry run and the controller's budget fields before requesting a paid ComfyUI session.
- Always finalize or reclaim sessions when work is complete.
- Use controller-reported `ui_url`, `phase`, `idle_deadline`, `max_total_usd`, and `tunnel_status` instead of guessing runtime state. There is no time-based hard cap; the watchdog reclaims a session when its effective spend reaches `max_total_usd` (with a `cost_cap_warning` at 90%).

## Discovery

Call:

```bash
curl -sS http://localhost:8088/api/v1/capabilities
```

## Cost And Billing

Use `effective_cost_usd` for cost decisions. It prefers imported RunPod billing data and falls back to local runtime estimates.

Trigger a billing sync when reconciliation is needed:

```bash
curl -sS -X POST http://localhost:8088/api/v1/billing/sync \
  -H 'Content-Type: application/json' \
  -d '{"lookback_days":30,"bucket_size":"hour"}'
```

Inspect imported billing buckets:

```bash
curl -sS http://localhost:8088/api/v1/billing/records
```

Pod billing calibrates local Pod rows by `podId`. Network volume billing is imported as account-level storage billing unless RunPod returns a concrete volume id in the record.

## Session Flow

1. Upload or select a ComfyUI workflow. The UI workflow JSON is canonicalized and reused by hash:

```bash
curl -sS -X POST http://localhost:8088/api/v1/comfyui/workflows/upload \
  -H 'Content-Type: application/json' \
  -d '{"filename":"workflow.json","content":"{\"nodes\":[]}"}'
```

2. Inspect the workflow. If `analysis.unresolved_custom_nodes` is non-empty, ask the user for a Registry choice, Git repository mapping, or explicit built-in override. Do not guess custom-node code:

```bash
curl -sS http://localhost:8088/api/v1/comfyui/workflows/<workflow_id>
```

3. Resolve a custom node with a concrete repo lock when instructed:

```bash
curl -sS -X POST http://localhost:8088/api/v1/comfyui/workflows/<workflow_id>/nodes/resolve \
  -H 'Content-Type: application/json' \
  -d '{"decision":"install_git_repo","class_type":"Custom Node Name","package":"custom-node-package","repo_url":"https://github.com/org/repo"}'
```

4. Fill workflow model URLs/sizes through `PUT /api/v1/comfyui/workflows/<workflow_id>` or the Web UI. Use `POST /api/v1/assets/peek` for URL metadata. Unknown sizes block launch.

5. Run a dry run before paid resources:

```bash
curl -sS -X POST http://localhost:8088/api/v1/resource-requests/dry-run \
  -H 'Content-Type: application/json' \
  -d '{"product":"comfyui","workflow_id":"<workflow_id>","min_vram_gb":24,"gpu_vendor":"NVIDIA","max_gpu_usd_per_hr":1.25,"max_total_usd":5.0}'
```

6. Request a ComfyUI session by workflow id:

```bash
curl -sS -X POST http://localhost:8088/api/v1/resource-requests \
  -H 'Content-Type: application/json' \
  -d '{"product":"comfyui","mode":"interactive","workflow_id":"<workflow_id>"}'
```

7. Poll the request until it returns `hydrated_cpu_ready`, `interactive_ready`, `failed`, or `reclaimed`:

```bash
curl -sS http://localhost:8088/api/v1/resource-requests/<request_id>
```

8. Read the session:

```bash
curl -sS http://localhost:8088/api/v1/sessions/<session_id>
```

9. Extend the lease while actively using the session:

```bash
curl -sS -X POST http://localhost:8088/api/v1/sessions/<session_id>/lease \
  -H 'Content-Type: application/json' \
  -d '{"minutes":60}'
```

10. Record task provenance:

```bash
curl -sS -X POST http://localhost:8088/api/v1/sessions/<session_id>/tasks \
  -H 'Content-Type: application/json' \
  -d '{"workflow_ref":"workflows/api-workflow.json","prompt":"short task note"}'
```

11. For interactive ComfyUI work, use the controller `ui_url` after the session reaches `interactive_ready`. If the tunnel is unhealthy, ask the controller to restart it:

```bash
curl -sS -X POST http://localhost:8088/api/v1/sessions/<session_id>/tunnel/restart
```

12. After at least one output artifact is collected and validation passed, mark the workflow verified when appropriate:

```bash
curl -sS -X POST http://localhost:8088/api/v1/sessions/<session_id>/workflow/verify
```

13. Finalize and reclaim:

```bash
curl -sS -X POST http://localhost:8088/api/v1/sessions/<session_id>/finalize \
  -H 'Content-Type: application/json' \
  -d '{"note":"artifacts are ready"}'

curl -sS -X POST http://localhost:8088/api/v1/sessions/<session_id>/reclaim \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Reclaim stops GPU compute first, runs a final S3 output collection, and deletes the network volume only after a successful collection. If collection fails or finds nothing for an interactive session, the session ends in `output_collection_failed_keep_volume` or `output_collection_empty_keep_volume` and the volume is retained (billed) for recovery. Retry with `{"force":true}` after fixing the cause, or discard intentionally:

```bash
curl -sS -X POST http://localhost:8088/api/v1/sessions/<session_id>/reclaim \
  -H 'Content-Type: application/json' \
  -d '{"force":true,"discard_outputs":true}'
```
