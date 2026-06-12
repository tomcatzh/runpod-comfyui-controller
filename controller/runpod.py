from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .gpu_catalog import gpu_type_meets_intent, normalize_gpu_vendor, normalize_min_vram_gb, select_comfyui_gpu_type
from .ssh_keys import normalize_public_key, public_key_for, resolve_private_key_path


REST_BASE = "https://rest.runpod.io/v1"


def _stock_status_available(value: object) -> bool:
    stock = str(value or "").strip().lower()
    return stock not in {
        "",
        "none",
        "null",
        "unavailable",
        "out_of_stock",
        "sold_out",
        "zero",
        "0",
    }


@dataclass(frozen=True)
class ScoutResult:
    ok: bool
    authoritative: bool
    data_center_id: str
    gpu_type_id: str | None
    price_per_hr_usd: float | None
    reason: str
    raw: dict[str, Any]


class RunpodAdapter:
    def scout_gpu(self, intent: dict[str, Any]) -> ScoutResult:
        raise NotImplementedError

    def scout_gpu_matrix(self, intent: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "authoritative": False, "reason": "gpu_matrix_scout_not_available", "candidates": []}

    def create_network_volume(self, *, name: str, size_gb: int, data_center_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def create_cpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, env: dict[str, str]) -> dict[str, Any]:
        raise NotImplementedError

    def create_probe_cpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, env: dict[str, str]) -> dict[str, Any]:
        return self.create_cpu_pod(name=name, volume_provider_id=volume_provider_id, data_center_id=data_center_id, env=env)

    def create_gpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, gpu_type_id: str, env: dict[str, str]) -> dict[str, Any]:
        raise NotImplementedError

    def get_pod(self, provider_pod_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def stop_pod(self, provider_pod_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def delete_pod(self, provider_pod_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def delete_network_volume(self, provider_volume_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def update_network_volume_size(self, provider_volume_id: str, size_gb: int) -> dict[str, Any]:
        raise NotImplementedError

    def workspace_disk_usage(self, provider_pod_id: str) -> dict[str, Any]:
        return {"ok": False, "reason": "workspace_disk_usage_not_available", "provider_pod_id": provider_pod_id}

    def list_comfyui_models(self, provider_pod_id: str, folders: list[str]) -> dict[str, Any]:
        return {"ok": False, "reason": "model_tree_not_available", "provider_pod_id": provider_pod_id, "folders": folders}

    def download_comfyui_model(self, provider_pod_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "reason": "runtime_model_download_not_available", "provider_pod_id": provider_pod_id}

    def move_comfyui_model(self, provider_pod_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "reason": "runtime_model_move_not_available", "provider_pod_id": provider_pod_id}

    def configure_comfyui_environment(
        self,
        *,
        provider_pod_id: str,
        session_id: str,
        assets: list[dict[str, Any]],
        install_plan: dict[str, Any] | None = None,
        validation_plan: dict[str, Any] | None = None,
        custom_nodes: list[dict[str, Any]] | None = None,
        ui_workflow_json: Any = None,
        api_workflow_json: Any = None,
    ) -> dict[str, Any]:
        return {"ok": True, "mode": "adapter_default_noop", "provider_pod_id": provider_pod_id, "session_id": session_id, "assets": len(assets)}

    def billing_pods(self, *, start_time: str, end_time: str, bucket_size: str = "hour") -> list[dict[str, Any]]:
        raise NotImplementedError

    def billing_network_volumes(self, *, start_time: str, end_time: str, bucket_size: str = "hour") -> list[dict[str, Any]]:
        raise NotImplementedError

    def ensure_ssh_public_key_registered(self, public_key: str) -> dict[str, Any]:
        return {"ok": True, "state": "not_supported"}


class RunpodRestAdapter(RunpodAdapter):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.api_key = os.environ.get("RUNPOD_API_KEY", "")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: int = 90) -> tuple[int, Any]:
        if not self.api_key:
            raise RuntimeError("RUNPOD_API_KEY is required for live RunPod mode")
        data = None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{REST_BASE}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload: Any = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"error": raw[:4000]}
            return exc.code, payload
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            # Cleanup paths in the service layer rely on delete/get calls returning
            # a non-ok result; a raised DNS/timeout/connection error there would
            # strand sessions and leak paid resources.
            return 0, {"error": f"network_error: {exc!r}"}

    def _graphql(self, query: str, variables: dict[str, Any] | None = None, timeout: int = 45) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("RUNPOD_API_KEY is required for RunPod GraphQL")
        url = f"https://api.runpod.io/graphql?api_key={urllib.parse.quote(self.api_key)}"
        data = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "runpod-comfyui-controller/1.0"}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}

    def _account_public_keys(self) -> list[str]:
        cached = getattr(self, "_account_keys_cache", None)
        now = time.time()
        if cached and now - cached[0] < 3600:
            return list(cached[1])
        try:
            payload = self._graphql("query { myself { pubKey } }", timeout=15)
            raw = str(((payload.get("data") or {}).get("myself") or {}).get("pubKey") or "")
        except Exception:  # noqa: BLE001 - account keys are a nice-to-have
            return list(cached[1]) if cached else []
        keys = [line.strip() for line in raw.splitlines() if len(line.split()) >= 2]
        self._account_keys_cache = (now, keys)
        return list(keys)

    def _env_with_public_key(self, env: dict[str, str]) -> dict[str, str]:
        # RunPod base images append $PUBLIC_KEY to authorized_keys at boot.
        # Live test 2026-06-12: when the create body sets PUBLIC_KEY, RunPod no
        # longer injects the account-registered keys, so merge those in too --
        # otherwise the controller key alone would lock out manual SSH.
        try:
            controller_key = public_key_for(resolve_private_key_path(self.settings)).strip()
        except OSError:
            controller_key = ""
        lines = [line.strip() for line in str(env.get("PUBLIC_KEY") or "").splitlines() if line.strip()]
        seen = {normalize_public_key(line) for line in lines}
        for key in [*self._account_public_keys(), controller_key]:
            normalized = normalize_public_key(key)
            if normalized and normalized not in seen:
                lines.append(key)
                seen.add(normalized)
        if not lines:
            return env
        return {**env, "PUBLIC_KEY": "\n".join(lines)}

    def ensure_ssh_public_key_registered(self, public_key: str) -> dict[str, Any]:
        normalized = normalize_public_key(public_key)
        if not normalized:
            return {"ok": False, "reason": "no_public_key"}
        try:
            payload = self._graphql("query { myself { id pubKey } }")
        except Exception as exc:  # noqa: BLE001 - best-effort, never blocks startup
            return {"ok": False, "reason": "account_keys_unreadable", "error": repr(exc)[:300]}
        if payload.get("errors"):
            return {"ok": False, "reason": "account_keys_unreadable", "error": str(payload["errors"])[:300]}
        myself = (payload.get("data") or {}).get("myself") or {}
        existing = str(myself.get("pubKey") or "")
        if normalized in {normalize_public_key(line) for line in existing.splitlines()}:
            return {"ok": True, "state": "already_registered"}
        merged = f"{existing.strip()}\n\n{public_key.strip()}".strip()
        try:
            result = self._graphql(
                "mutation($input: UpdateUserSettingsInput) { updateUserSettings(input: $input) { id } }",
                {"input": {"pubKey": merged}},
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "register_failed", "error": repr(exc)[:300]}
        if result.get("errors"):
            return {"ok": False, "reason": "register_failed", "error": str(result["errors"])[:300]}
        return {"ok": True, "state": "registered"}

    def scout_gpu_matrix(self, intent: dict[str, Any]) -> dict[str, Any]:
        data_centers = [str(item) for item in (intent.get("data_centers") or []) if str(item).strip()]
        gpu_rows = [row for row in (intent.get("gpu_rows") or []) if row.get("gpu_type_id")]
        max_rate = float(intent.get("max_gpu_usd_per_hr") or 0)
        if not self.api_key:
            return {"ok": False, "authoritative": False, "reason": "missing_runpod_api_key", "candidates": []}
        if not data_centers or not gpu_rows:
            return {"ok": False, "authoritative": True, "reason": "no_gpu_or_datacenter_candidates", "candidates": []}
        candidates: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for gpu in gpu_rows:
            aliases = []
            alias_to_dc: dict[str, str] = {}
            for index, dc in enumerate(data_centers):
                alias = f"dc{index}"
                alias_to_dc[alias] = dc
                aliases.append(
                    f'{alias}: lowestPrice(input: {{ gpuCount: 1, secureCloud: true, dataCenterId: "{dc}" }}) '
                    "{ stockStatus uninterruptablePrice minimumBidPrice clusterPrice rentedCount totalCount maxUnreservedGpuCount availableGpuCounts }"
                )
            query = (
                "query($gpu_id: String!) { "
                "gpuTypes(input: { id: $gpu_id }) { "
                "id displayName memoryInGb secureCloud communityCloud securePrice "
                "nodeGroupDatacenters { id name location } "
                + " ".join(aliases)
                + " } }"
            )
            try:
                payload = self._graphql(query, {"gpu_id": gpu["gpu_type_id"]})
            except Exception as exc:  # noqa: BLE001
                errors.append({"gpu_type_id": gpu["gpu_type_id"], "error": repr(exc)})
                continue
            if payload.get("errors"):
                errors.append({"gpu_type_id": gpu["gpu_type_id"], "error": payload.get("errors")})
                continue
            gpu_types = (((payload.get("data") or {}).get("gpuTypes")) or [])
            if not gpu_types:
                continue
            item = gpu_types[0]
            listed_dc_ids = {
                str(dc_item.get("id") or dc_item.get("name"))
                for dc_item in (item.get("nodeGroupDatacenters") or [])
                if isinstance(dc_item, dict) and (dc_item.get("id") or dc_item.get("name"))
            }
            for alias, dc in alias_to_dc.items():
                quote = item.get(alias) or {}
                stock = quote.get("stockStatus")
                price = quote.get("uninterruptablePrice")
                price_stock_confirmed = price is not None and _stock_status_available(stock)
                if price_stock_confirmed and max_rate > 0 and float(price) > max_rate:
                    continue
                if not price_stock_confirmed and dc not in listed_dc_ids:
                    continue
                candidates.append(
                    {
                        "data_center_id": dc,
                        "gpu_type_id": item.get("id") or gpu["gpu_type_id"],
                        "display_name": item.get("displayName"),
                        "vendor": gpu.get("vendor"),
                        "vram_gb": item.get("memoryInGb") or gpu.get("vram_gb"),
                        "template": gpu.get("template"),
                        "quoted_cost_usd_per_hr": price if price_stock_confirmed else None,
                        "estimated_cost_usd_per_hr": gpu.get("estimated_price_usd_per_hr"),
                        "quote_source": (
                            "runpod_graphql_datacenter_lowestPrice"
                            if price_stock_confirmed
                            else "runpod_graphql_gpu_type_datacenter_listing"
                        ),
                        "scout_status": "price_stock_confirmed" if price_stock_confirmed else "datacenter_listed_stock_unconfirmed",
                        "stock_status": stock if price_stock_confirmed else "unconfirmed",
                        "available_gpu_counts": quote.get("availableGpuCounts"),
                        "max_unreserved_gpu_count": quote.get("maxUnreservedGpuCount"),
                        "rented_count": quote.get("rentedCount"),
                        "total_count": quote.get("totalCount"),
                        "eligible": bool(price_stock_confirmed),
                        "datacenter_listed": dc in listed_dc_ids if listed_dc_ids else None,
                    }
                )
        if candidates:
            confirmed = [candidate for candidate in candidates if candidate.get("eligible")]
            return {
                "ok": True,
                "authoritative": bool(confirmed),
                "listing_available": True,
                "reason": (
                    "runpod_graphql_datacenter_lowestPrice"
                    if confirmed
                    else "runpod_graphql_gpu_type_datacenter_listing_no_stock_quote"
                ),
                "candidates": candidates,
                "confirmed_candidate_count": len(confirmed),
                "errors": errors,
            }
        runpodctl_result = self._scout_gpu_matrix_runpodctl(data_centers, gpu_rows)
        if runpodctl_result.get("ok"):
            runpodctl_result["errors"] = errors
            return runpodctl_result
        return {
            "ok": False,
            "authoritative": False,
            "reason": "runpod_graphql_no_datacenter_candidates" if not errors else "runpod_graphql_failed",
            "candidates": [],
            "errors": errors,
        }

    def _scout_gpu_matrix_runpodctl(self, data_centers: list[str], gpu_rows: list[dict[str, Any]]) -> dict[str, Any]:
        binary = shutil.which("runpodctl")
        if not binary:
            return {"ok": False, "authoritative": False, "reason": "runpodctl_not_installed", "candidates": []}
        try:
            proc = subprocess.run(
                [binary, "datacenter", "list", "-o", "json"],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "authoritative": False, "reason": "runpodctl_failed", "error": repr(exc), "candidates": []}
        if proc.returncode != 0:
            return {
                "ok": False,
                "authoritative": False,
                "reason": "runpodctl_datacenter_list_failed",
                "stderr_tail": proc.stderr[-1000:],
                "candidates": [],
            }
        try:
            payload = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "authoritative": False,
                "reason": "runpodctl_datacenter_list_non_json",
                "error": str(exc),
                "stdout_tail": proc.stdout[-1000:],
                "candidates": [],
            }
        rows_by_gpu = {str(row.get("gpu_type_id")): row for row in gpu_rows if row.get("gpu_type_id")}
        wanted_data_centers = set(data_centers)
        candidates: list[dict[str, Any]] = []
        for item in payload if isinstance(payload, list) else []:
            dc = str((item or {}).get("id") or (item or {}).get("name") or "")
            if dc not in wanted_data_centers:
                continue
            for gpu_item in (item or {}).get("gpuAvailability") or []:
                if not isinstance(gpu_item, dict):
                    continue
                gpu_type_id = str(gpu_item.get("gpuId") or "")
                gpu = rows_by_gpu.get(gpu_type_id)
                if not gpu:
                    continue
                stock = gpu_item.get("stockStatus")
                stock_confirmed = _stock_status_available(stock)
                candidates.append(
                    {
                        "data_center_id": dc,
                        "gpu_type_id": gpu_type_id,
                        "display_name": gpu_item.get("displayName") or gpu_type_id,
                        "vendor": gpu.get("vendor"),
                        "vram_gb": gpu.get("vram_gb"),
                        "template": gpu.get("template"),
                        "quoted_cost_usd_per_hr": None,
                        "estimated_cost_usd_per_hr": gpu.get("estimated_price_usd_per_hr"),
                        "quote_source": "runpodctl_datacenter_list_gpuAvailability",
                        "scout_status": (
                            "runpodctl_stock_confirmed_price_catalog_estimate"
                            if stock_confirmed
                            else "runpodctl_datacenter_listed_stock_unconfirmed"
                        ),
                        "stock_status": stock if stock_confirmed else "unconfirmed",
                        "eligible": stock_confirmed,
                        "datacenter_listed": True,
                    }
                )
        if not candidates:
            return {
                "ok": False,
                "authoritative": False,
                "reason": "runpodctl_no_matching_datacenter_gpu_rows",
                "candidates": [],
            }
        confirmed = [candidate for candidate in candidates if candidate.get("eligible")]
        return {
            "ok": True,
            "authoritative": False,
            "listing_available": True,
            "reason": "runpodctl_datacenter_list_gpuAvailability_after_graphql_failed",
            "candidates": candidates,
            "confirmed_candidate_count": len(confirmed),
        }

    def scout_gpu(self, intent: dict[str, Any]) -> ScoutResult:
        min_vram_gb = normalize_min_vram_gb(intent.get("min_vram_gb") or self.settings.default_min_vram_gb)
        gpu_vendor = normalize_gpu_vendor(intent.get("gpu_vendor") or self.settings.default_gpu_vendor)
        if gpu_vendor != "NVIDIA":
            return ScoutResult(
                ok=False,
                authoritative=True,
                data_center_id=str(intent.get("data_center_id") or self.settings.default_data_center),
                gpu_type_id=None,
                price_per_hr_usd=None,
                reason=f"unsupported_gpu_vendor:{gpu_vendor}",
                raw={"mode": self.settings.runpod_mode, "min_vram_gb": min_vram_gb, "gpu_vendor": gpu_vendor},
            )
        if self.settings.runpod_mode == "live_cpu_test":
            mock_gpu_type_id = select_comfyui_gpu_type(min_vram_gb=min_vram_gb, gpu_vendor=gpu_vendor)
            if not mock_gpu_type_id:
                return ScoutResult(
                    ok=False,
                    authoritative=True,
                    data_center_id=str(intent.get("data_center_id") or self.settings.default_data_center),
                    gpu_type_id=None,
                    price_per_hr_usd=None,
                    reason=f"no_cpu_test_gpu_meets_min_vram:{min_vram_gb}",
                    raw={"mode": "live_cpu_test", "min_vram_gb": min_vram_gb, "gpu_vendor": gpu_vendor},
                )
            return ScoutResult(
                ok=True,
                authoritative=True,
                data_center_id=str(intent.get("data_center_id") or self.settings.default_data_center),
                gpu_type_id=mock_gpu_type_id,
                price_per_hr_usd=0.0,
                reason="live_cpu_test_uses_mock_gpu_quote",
                raw={"mode": "live_cpu_test", "warning": "GPU quote is mocked; GPU creation remains blocked", "min_vram_gb": min_vram_gb, "gpu_vendor": gpu_vendor},
            )
        if self.settings.live_gpu_type_id and self.settings.live_gpu_price_usd_per_hr > 0:
            ok, reason = gpu_type_meets_intent(self.settings.live_gpu_type_id, min_vram_gb=min_vram_gb, gpu_vendor=gpu_vendor)
            if not ok:
                return ScoutResult(
                    ok=False,
                    authoritative=True,
                    data_center_id=str(intent.get("data_center_id") or self.settings.default_data_center),
                    gpu_type_id=self.settings.live_gpu_type_id,
                    price_per_hr_usd=self.settings.live_gpu_price_usd_per_hr,
                    reason=reason,
                    raw={
                        "mode": "live",
                        "source": "LIVE_GPU_TYPE_ID/LIVE_GPU_PRICE_USD_PER_HR",
                        "min_vram_gb": min_vram_gb,
                        "gpu_vendor": gpu_vendor,
                    },
                )
            return ScoutResult(
                ok=True,
                authoritative=True,
                data_center_id=str(intent.get("data_center_id") or self.settings.default_data_center),
                gpu_type_id=self.settings.live_gpu_type_id,
                price_per_hr_usd=self.settings.live_gpu_price_usd_per_hr,
                reason="operator_configured_live_gpu_quote",
                raw={
                    "mode": "live",
                    "source": "LIVE_GPU_TYPE_ID/LIVE_GPU_PRICE_USD_PER_HR",
                    "fail_closed_without_operator_quote": True,
                    "min_vram_gb": min_vram_gb,
                    "gpu_vendor": gpu_vendor,
                },
            )
        return ScoutResult(
            ok=False,
            authoritative=False,
            data_center_id=str(intent.get("data_center_id") or self.settings.default_data_center),
            gpu_type_id=None,
            price_per_hr_usd=None,
            reason="live_graphql_scout_not_embedded_yet_use_existing_price_scout_before_gpu",
            raw={"mode": "live", "fail_closed": True},
        )

    def create_network_volume(self, *, name: str, size_gb: int, data_center_id: str) -> dict[str, Any]:
        status, payload = self._request(
            "POST",
            "/networkvolumes",
            {"name": name, "size": size_gb, "dataCenterId": data_center_id},
        )
        if status not in {200, 201} or not isinstance(payload, dict) or not payload.get("id"):
            raise RuntimeError(f"network volume create failed: HTTP {status}: {payload}")
        return payload

    def create_cpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, env: dict[str, str]) -> dict[str, Any]:
        body = {
            "name": name,
            "cloudType": "SECURE",
            "computeType": "CPU",
            "cpuFlavorIds": self.settings.cpu_flavor_ids,
            "cpuFlavorPriority": "availability",
            "dataCenterIds": [data_center_id],
            "dataCenterPriority": "custom",
            "imageName": self.settings.cpu_pod_image,
            "containerDiskInGb": 10,
            "networkVolumeId": volume_provider_id,
            "volumeMountPath": "/workspace",
            "vcpuCount": 2,
            "interruptible": False,
            "ports": [],
            "dockerEntrypoint": ["/bin/bash", "-lc"],
            "dockerStartCmd": [self._hydration_shell()],
            "env": env,
        }
        status, payload = self._request("POST", "/pods", body, timeout=120)
        if status not in {200, 201} or not isinstance(payload, dict) or not payload.get("id"):
            raise RuntimeError(self._pod_create_error("CPU Pod create failed", status, payload))
        return payload

    def create_probe_cpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, env: dict[str, str]) -> dict[str, Any]:
        body = {
            "name": name,
            "cloudType": "SECURE",
            "computeType": "CPU",
            "cpuFlavorIds": self.settings.cpu_flavor_ids,
            "cpuFlavorPriority": "availability",
            "dataCenterIds": [data_center_id],
            "dataCenterPriority": "custom",
            "networkVolumeId": volume_provider_id,
            "volumeMountPath": "/workspace",
            "imageName": self.settings.cpu_pod_image,
            "containerDiskInGb": 20,
            "vcpuCount": 2,
            "interruptible": False,
            "env": env,
            "dockerEntrypoint": ["/bin/bash", "-lc"],
            "dockerStartCmd": [self._probe_shell()],
            "ports": [],
        }
        status, payload = self._request("POST", "/pods", body, timeout=120)
        if status not in {200, 201} or not isinstance(payload, dict) or not payload.get("id"):
            raise RuntimeError(self._pod_create_error("CPU probe Pod create failed", status, payload))
        return payload

    def _pod_create_error(self, prefix: str, status: int, payload: Any) -> str:
        error = ""
        if isinstance(payload, dict):
            error = str(payload.get("error") or payload.get("message") or payload)
        else:
            error = str(payload)
        compact = " ".join(error.split())
        if "unmarshal to struct" in compact and "invalid character" in compact:
            compact = "runpod_create_pod_provider_parse_error"
        elif len(compact) > 600:
            compact = compact[:600] + "...(truncated)"
        return f"{prefix}: HTTP {status}: {compact}"

    def _probe_shell(self) -> str:
        return r"""set -eu
root=/workspace/runpod-controller/probes/${PROBE_ID}
mkdir -p "$root"
if ! command -v git >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends git ca-certificates
  rm -rf /var/lib/apt/lists/*
fi
python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess

PROBE_ID = os.environ["PROBE_ID"]
root = pathlib.Path("/workspace/runpod-controller/probes") / PROBE_ID
root.mkdir(parents=True, exist_ok=True)
install_plan = json.loads(os.environ.get("INSTALL_PLAN_JSON") or '{"steps":[]}')
custom_nodes = json.loads(os.environ.get("CUSTOM_NODES_JSON") or "[]")
ui_workflow = os.environ.get("UI_WORKFLOW_JSON") or ""
api_workflow = os.environ.get("API_WORKFLOW_JSON") or ""

def now_iso():
    return dt.datetime.now(dt.UTC).isoformat()

def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def is_commit_ref(ref):
    return bool(re.fullmatch(r"[0-9a-fA-F]{40}", ref or ""))

def clone_locked_repo(repo_url, ref, target):
    if is_commit_ref(ref):
        cmd = ["git", "clone", repo_url, str(target)]
    else:
        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd += ["--branch", ref]
        cmd += [repo_url, str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        return proc
    if is_commit_ref(ref):
        checkout = subprocess.run(["git", "-C", str(target), "checkout", ref], capture_output=True, text=True, timeout=120)
        if checkout.returncode != 0:
            checkout.stdout = (proc.stdout or "") + "\n" + (checkout.stdout or "")
            checkout.stderr = (proc.stderr or "") + "\n" + (checkout.stderr or "")
            return checkout
    return proc

steps = []
ok = True
nodes_root = root / "custom_nodes"
nodes_root.mkdir(parents=True, exist_ok=True)
for step in install_plan.get("steps") or []:
    package = str(step.get("package") or "custom-node")
    repo_url = str(step.get("repo_url") or "")
    ref = str(step.get("ref") or "")
    target = nodes_root / package
    row = {
        "package": package,
        "repo_url": repo_url,
        "ref": ref,
        "target": str(target.relative_to(root)),
        "state": "pending",
        "requirements_present": False,
        "error": None,
        "started_at": now_iso(),
    }
    if not repo_url:
        row["state"] = "failed"
        row["error"] = "repo_url_missing"
        ok = False
        steps.append(row)
        continue
    proc = clone_locked_repo(repo_url, ref, target)
    row["git_returncode"] = proc.returncode
    row["git_stdout_tail"] = proc.stdout[-1000:]
    row["git_stderr_tail"] = proc.stderr[-1000:]
    if proc.returncode != 0:
        row["state"] = "failed"
        row["error"] = "git_clone_failed"
        ok = False
        steps.append(row)
        continue
    req = target / "requirements.txt"
    row["requirements_present"] = req.exists()
    if req.exists():
        row["requirements_preview"] = req.read_text(encoding="utf-8", errors="replace").splitlines()[:50]
    rev = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    row["resolved_commit"] = rev.stdout.strip() if rev.returncode == 0 else ""
    row["state"] = "passed"
    row["completed_at"] = now_iso()
    steps.append(row)

result = {
    "probe_id": PROBE_ID,
    "state": "passed" if ok else "failed",
    "dependency_screening_only": True,
    "custom_nodes": custom_nodes,
    "steps": steps,
    "ui_workflow_bytes": len(ui_workflow.encode("utf-8")),
    "api_workflow_bytes": len(api_workflow.encode("utf-8")),
    "completed_at": now_iso(),
}
write_json(root / "INSTALL_PLAN.json", install_plan)
write_json(root / "PROBE.json", result)
write_json(root / "DONE.json", {"probe_id": PROBE_ID, "state": result["state"], "completed_at": now_iso()})
checks = []
for name in ["INSTALL_PLAN.json", "PROBE.json", "DONE.json"]:
    path = root / name
    checks.append(sha256_file(path) + "  probes/" + PROBE_ID + "/" + name)
(root / "checksums.sha256").write_text("\n".join(checks) + "\n", encoding="utf-8")
if not ok:
    raise SystemExit(2)
PY
sync
runpodctl stop pod "$RUNPOD_POD_ID" || sleep 30
"""

    def _hydration_shell(self) -> str:
        return r"""set -eu
root=/workspace/runpod-controller/hydration/${HYDRATION_ID}
mkdir -p "$root"
python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

HYDRATION_ID = os.environ["HYDRATION_ID"]
SESSION_ID = os.environ.get("SESSION_ID")
root = pathlib.Path("/workspace/runpod-controller/hydration") / HYDRATION_ID
asset_root = pathlib.Path("/workspace/runpod-controller")
root.mkdir(parents=True, exist_ok=True)
assets = json.loads(os.environ.get("ASSETS_JSON") or "[]")
install_plan = json.loads(os.environ.get("INSTALL_PLAN_JSON") or '{"steps":[]}')
validation_plan = json.loads(os.environ.get("VALIDATION_PLAN_JSON") or "{}")
custom_nodes = json.loads(os.environ.get("CUSTOM_NODES_JSON") or "[]")
ui_workflow = os.environ.get("UI_WORKFLOW_JSON") or ""
api_workflow = os.environ.get("API_WORKFLOW_JSON") or ""
progress_path = root / "progress.json"
CHUNK = 8 * 1024 * 1024
MAX_RETRIES = 5


def now_iso():
    return dt.datetime.now(dt.UTC).isoformat()


def write_json_atomic(path, payload):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


progress = {
    "hydration_id": HYDRATION_ID,
    "session_id": SESSION_ID,
    "state": "running",
    "updated_at": now_iso(),
    "assets": [],
}


def flush_progress():
    progress["updated_at"] = now_iso()
    write_json_atomic(progress_path, progress)


def token_for(provider):
    if provider == "huggingface":
        return os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""
    if provider == "civitai":
        return os.environ.get("CIVITAI_TOKEN") or ""
    return ""


def request_headers(provider, start=None):
    headers = {"User-Agent": "runpod-controller/1.0"}
    token = token_for(provider)
    if token and provider != "civitai":
        headers["Authorization"] = "Bearer " + token
    if start and start > 0:
        headers["Range"] = "bytes=%d-" % start
    return headers


def strip_redacted_params(url):
    # Persisted asset URLs replace secret-looking query values with the literal
    # "<redacted>"; sending those upstream breaks the download and blocks the
    # real token injection below.
    parsed = urllib.parse.urlparse(url)
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if value != "%3Credacted%3E" and value != "<redacted>"]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def request_url(url, provider):
    if provider != "civitai":
        return url
    token = token_for(provider)
    if not token:
        return url
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.lower() == "token" for key, _value in query):
        return url
    query.append(("token", token))
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def record_error(row, error):
    row["state"] = "retrying" if row.get("retry", 0) < MAX_RETRIES else "failed"
    row["last_error"] = str(error)[-1000:]
    row["updated_at"] = now_iso()
    flush_progress()


files = []
flush_progress()
for index, asset in enumerate(assets):
    url = strip_redacted_params(asset.get("url") or "")
    target_rel = asset.get("target") or ""
    provider = asset.get("provider") or "generic"
    expected = asset.get("size_bytes")
    expected = int(expected) if expected is not None else None
    if not url or not target_rel:
        continue
    target = asset_root / target_rel
    target_resolved = target.parent.resolve() / target.name
    if not str(target_resolved).startswith(str(asset_root.resolve()) + "/"):
        raise RuntimeError("asset_target_outside_root:" + str(target_rel))
    part = target.with_name(target.name + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "index": index,
        "target": str(target.relative_to(asset_root)),
        "expected_bytes": expected,
        "observed_bytes": target.stat().st_size if target.exists() else part.stat().st_size if part.exists() else 0,
        "state": "pending",
        "retry": 0,
        "last_error": None,
        "started_at": now_iso(),
        "updated_at": now_iso(),
        "speed_bytes_per_sec": 0,
    }
    progress["assets"].append(row)
    flush_progress()
    while True:
        if target.exists() and (expected is None or target.stat().st_size == expected):
            row["state"] = "hashing"
            row["observed_bytes"] = target.stat().st_size
            flush_progress()
            digest = sha256_file(target)
            files.append(
                {
                    "path": str(target.relative_to(asset_root)),
                    "purpose": provider + " " + str(asset.get("kind") or "model"),
                    "sha256": digest,
                    "size_bytes": target.stat().st_size,
                }
            )
            row["sha256"] = digest
            row["state"] = "complete"
            row["updated_at"] = now_iso()
            flush_progress()
            break
        if row["retry"] >= MAX_RETRIES:
            raise RuntimeError("asset_download_failed:" + row["target"] + ":" + str(row.get("last_error")))
        row["state"] = "downloading"
        row["retry"] += 1
        start = part.stat().st_size if part.exists() else 0
        row["observed_bytes"] = start
        row["updated_at"] = now_iso()
        flush_progress()
        try:
            req = urllib.request.Request(request_url(url, provider), headers=request_headers(provider, start))
            with urllib.request.urlopen(req, timeout=1800) as resp:
                status = getattr(resp, "status", 200)
                if start > 0 and status != 206:
                    part.unlink(missing_ok=True)
                    start = 0
                mode = "ab" if start > 0 else "wb"
                started = time.monotonic()
                observed = start
                last_flush = 0.0
                with part.open(mode) as fh:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        observed += len(chunk)
                        elapsed = max(0.001, time.monotonic() - started)
                        row["observed_bytes"] = observed
                        row["speed_bytes_per_sec"] = round((observed - start) / elapsed, 2)
                        if time.monotonic() - last_flush >= 2:
                            last_flush = time.monotonic()
                            flush_progress()
            observed = part.stat().st_size if part.exists() else 0
            row["observed_bytes"] = observed
            if expected is not None and observed != expected:
                raise RuntimeError("size_mismatch:%s:%s" % (observed, expected))
            part.replace(target)
            row["state"] = "downloaded"
            row["updated_at"] = now_iso()
            flush_progress()
        except Exception as exc:
            record_error(row, exc)
            time.sleep(min(60, 2 ** row["retry"]))

hydrated = {
    "hydration_id": HYDRATION_ID,
    "session_id": SESSION_ID,
    "state": "hydrated",
    "portable_assets_only": True,
    "assets": assets,
    "custom_nodes": custom_nodes,
    "created_at": now_iso(),
}
launch_root = asset_root / "launch" / (SESSION_ID or HYDRATION_ID)
launch_root.mkdir(parents=True, exist_ok=True)
write_json_atomic(launch_root / "install_plan.json", install_plan)
write_json_atomic(launch_root / "validation_plan.json", validation_plan)
write_json_atomic(launch_root / "custom_nodes.json", custom_nodes)
if ui_workflow:
    (launch_root / "ui-workflow.json").write_text(ui_workflow + "\n", encoding="utf-8")
if api_workflow:
    (launch_root / "api-workflow.json").write_text(api_workflow + "\n", encoding="utf-8")
write_json_atomic(root / "HYDRATED.json", hydrated)
inventory = {
    "files": [
        {"path": "hydration/" + HYDRATION_ID + "/HYDRATED.json", "purpose": "hydration marker"},
        {"path": "hydration/" + HYDRATION_ID + "/inventory.json", "purpose": "file inventory"},
        {"path": "hydration/" + HYDRATION_ID + "/checksums.sha256", "purpose": "integrity marker"},
        {"path": "hydration/" + HYDRATION_ID + "/DONE.json", "purpose": "completion marker"},
        {"path": "launch/" + (SESSION_ID or HYDRATION_ID) + "/install_plan.json", "purpose": "custom node install plan"},
        {"path": "launch/" + (SESSION_ID or HYDRATION_ID) + "/validation_plan.json", "purpose": "ComfyUI validation plan"},
        {"path": "launch/" + (SESSION_ID or HYDRATION_ID) + "/custom_nodes.json", "purpose": "resolved custom nodes"},
        *files,
    ],
    "estimated_reusable_bytes": sum(int(item.get("size_bytes") or 0) for item in files),
}
write_json_atomic(root / "inventory.json", inventory)
done = {
    "hydration_id": HYDRATION_ID,
    "session_id": SESSION_ID,
    "state": "done",
    "completed_at": now_iso(),
    "file_count": len(files),
    "bytes": inventory["estimated_reusable_bytes"],
}
write_json_atomic(root / "DONE.json", done)
checks = []
for name in ["HYDRATED.json", "inventory.json", "DONE.json"]:
    path = root / name
    checks.append(sha256_file(path) + "  hydration/" + HYDRATION_ID + "/" + name)
for name in ["install_plan.json", "validation_plan.json", "custom_nodes.json", "ui-workflow.json", "api-workflow.json"]:
    path = launch_root / name
    if path.exists():
        checks.append(sha256_file(path) + "  launch/" + (SESSION_ID or HYDRATION_ID) + "/" + name)
for item in files:
    checks.append(str(item["sha256"]) + "  " + item["path"])
(root / "checksums.sha256").write_text("\n".join(checks) + "\n", encoding="utf-8")
progress["state"] = "complete"
progress["completed_at"] = now_iso()
flush_progress()
PY
sync
runpodctl stop pod "$RUNPOD_POD_ID" || sleep 30
"""

    def create_gpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, gpu_type_id: str, env: dict[str, str]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "cloudType": "SECURE",
            "computeType": "GPU",
            "dataCenterIds": [data_center_id],
            "dataCenterPriority": "custom",
            "gpuTypeIds": [gpu_type_id],
            "gpuTypePriority": "availability",
            "gpuCount": 1,
            "containerDiskInGb": 50,
            "networkVolumeId": volume_provider_id,
            "volumeMountPath": "/workspace",
            "interruptible": False,
            "supportPublicIp": True,
            "ports": [f"{self.settings.comfyui_remote_port}/http", f"{self.settings.ssh_remote_port}/tcp"],
            "env": self._env_with_public_key(env),
        }
        if self.settings.gpu_template_id:
            body["templateId"] = self.settings.gpu_template_id
        else:
            body["imageName"] = self.settings.gpu_pod_image
        status, payload = self._request("POST", "/pods", body, timeout=120)
        if status not in {200, 201} or not isinstance(payload, dict) or not payload.get("id"):
            raise RuntimeError(f"GPU Pod create failed: HTTP {status}: {payload}")
        return payload

    def configure_comfyui_environment(
        self,
        *,
        provider_pod_id: str,
        session_id: str,
        assets: list[dict[str, Any]],
        install_plan: dict[str, Any] | None = None,
        validation_plan: dict[str, Any] | None = None,
        custom_nodes: list[dict[str, Any]] | None = None,
        ui_workflow_json: Any = None,
        api_workflow_json: Any = None,
    ) -> dict[str, Any]:
        if provider_pod_id.startswith(("test-", "fake-")):
            return {
                "ok": True,
                "mode": "test_noop",
                "provider_pod_id": provider_pod_id,
                "session_id": session_id,
                "assets": len(assets),
                "install_steps": len((install_plan or {}).get("steps") or []),
                "custom_nodes": len(custom_nodes or []),
            }
        mapping = self._wait_for_ssh_mapping(provider_pod_id)
        if not mapping.get("ok"):
            return mapping
        public_ip = str(mapping["public_ip"])
        ssh_port = mapping["ssh_port"]
        key_path = str(resolve_private_key_path(self.settings))
        if not os.path.exists(key_path):
            return {"ok": False, "reason": "ssh_key_missing", "key_path": key_path, "provider_pod_id": provider_pod_id}
        ready = self._wait_for_ssh_ready(provider_pod_id=provider_pod_id, public_ip=public_ip, ssh_port=ssh_port, key_path=key_path)
        if not ready.get("ok"):
            return ready
        payload = {
            "session_id": session_id,
            "assets": [
                {
                    "target": str(asset.get("target") or ""),
                    "model_folder": str(asset.get("model_folder") or ""),
                    "filename": str(asset.get("filename") or ""),
                }
                for asset in assets
                if isinstance(asset, dict)
            ],
            "comfyui_port": self.settings.comfyui_remote_port,
            "install_plan": install_plan or {"steps": []},
            "validation_plan": validation_plan or {},
            "custom_nodes": custom_nodes or [],
            "ui_workflow": ui_workflow_json,
            "api_workflow": api_workflow_json,
        }
        remote_script = (
            "import io, sys\n"
            f"sys.stdin = io.StringIO({repr(json.dumps(payload))})\n"
            + self._comfyui_environment_script()
        )
        proc = subprocess.run(
            self._ssh_base_command(key_path=key_path, public_ip=public_ip, ssh_port=ssh_port) + ["python3", "-"],
            input=remote_script,
            capture_output=True,
            text=True,
            timeout=max(1800, int(self.settings.hydration_timeout_seconds)),
        )
        if proc.returncode != 0:
            return {
                "ok": False,
                "reason": "ssh_configure_failed",
                "returncode": proc.returncode,
                "stdout_tail": proc.stdout[-2000:],
                "stderr_tail": proc.stderr[-2000:],
                "provider_pod_id": provider_pod_id,
            }
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        try:
            result = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError:
            result = {"ok": False, "reason": "configure_non_json_output", "stdout_tail": proc.stdout[-2000:]}
        result.setdefault("provider_pod_id", provider_pod_id)
        return result

    def _run_ssh_python(self, provider_pod_id: str, payload: dict[str, Any], script: str, *, timeout: int = 900) -> dict[str, Any]:
        mapping = self._wait_for_ssh_mapping(provider_pod_id)
        if not mapping.get("ok"):
            return mapping
        public_ip = str(mapping["public_ip"])
        ssh_port = mapping["ssh_port"]
        key_path = str(resolve_private_key_path(self.settings))
        if not os.path.exists(key_path):
            return {"ok": False, "reason": "ssh_key_missing", "key_path": key_path, "provider_pod_id": provider_pod_id}
        ready = self._wait_for_ssh_ready(provider_pod_id=provider_pod_id, public_ip=public_ip, ssh_port=ssh_port, key_path=key_path)
        if not ready.get("ok"):
            return ready
        remote_script = (
            "import io, sys\n"
            f"sys.stdin = io.StringIO({repr(json.dumps(payload))})\n"
            + script
        )
        proc = subprocess.run(
            self._ssh_base_command(key_path=key_path, public_ip=public_ip, ssh_port=ssh_port) + ["python3", "-"],
            input=remote_script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return {
                "ok": False,
                "reason": "ssh_python_failed",
                "returncode": proc.returncode,
                "stdout_tail": proc.stdout[-2000:],
                "stderr_tail": proc.stderr[-2000:],
                "provider_pod_id": provider_pod_id,
            }
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        try:
            result = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError:
            result = {"ok": False, "reason": "ssh_non_json_output", "stdout_tail": proc.stdout[-2000:]}
        result.setdefault("provider_pod_id", provider_pod_id)
        return result

    def workspace_disk_usage(self, provider_pod_id: str) -> dict[str, Any]:
        if provider_pod_id.startswith(("test-", "fake-")):
            return {"ok": True, "path": "/workspace", "total_bytes": 100 * 1024**3, "used_bytes": 1 * 1024**3, "available_bytes": 99 * 1024**3}
        return self._run_ssh_python(provider_pod_id, {}, self._workspace_disk_usage_script(), timeout=120)

    def list_comfyui_models(self, provider_pod_id: str, folders: list[str]) -> dict[str, Any]:
        if provider_pod_id.startswith(("test-", "fake-")):
            return {"ok": True, "asset_root": "/workspace/runpod-controller/assets/comfyui", "comfyui_root": "/workspace/runpod-slim/ComfyUI", "folders": folders, "files": []}
        return self._run_ssh_python(provider_pod_id, {"folders": folders}, self._model_tree_script(), timeout=180)

    def download_comfyui_model(self, provider_pod_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if provider_pod_id.startswith(("test-", "fake-")):
            return {"ok": True, "state": "downloaded", "target_path": payload.get("target_path"), "size_bytes": payload.get("size_bytes") or 0, "checksum_sha256": "test"}
        return self._run_ssh_python(provider_pod_id, payload, self._runtime_model_download_script(), timeout=max(1800, int(self.settings.hydration_timeout_seconds)))

    def move_comfyui_model(self, provider_pod_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if provider_pod_id.startswith(("test-", "fake-")):
            return {"ok": True, "state": "moved", "source_path": payload.get("source_path"), "target_path": payload.get("target_path")}
        return self._run_ssh_python(provider_pod_id, payload, self._runtime_model_move_script(), timeout=300)

    def _ssh_base_command(self, *, key_path: str, public_ip: str, ssh_port: object) -> list[str]:
        return [
            "ssh",
            "-i",
            key_path,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=4",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/tmp/runpod_controller_known_hosts",
            "-p",
            str(ssh_port),
            f"root@{public_ip}",
        ]

    def _wait_for_ssh_ready(
        self,
        *,
        provider_pod_id: str,
        public_ip: str,
        ssh_port: object,
        key_path: str,
        timeout_seconds: int = 300,
        interval_seconds: int = 5,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(1, timeout_seconds)
        attempts = 0
        last: dict[str, Any] = {}
        command = self._ssh_base_command(key_path=key_path, public_ip=public_ip, ssh_port=ssh_port) + ["true"]
        while True:
            attempts += 1
            proc = subprocess.run(command, capture_output=True, text=True, timeout=30)
            last = {
                "returncode": proc.returncode,
                "stdout_tail": proc.stdout[-500:],
                "stderr_tail": proc.stderr[-1000:],
            }
            if proc.returncode == 0:
                return {
                    "ok": True,
                    "provider_pod_id": provider_pod_id,
                    "public_ip": public_ip,
                    "ssh_port": ssh_port,
                    "ssh_ready_attempts": attempts,
                }
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "reason": "ssh_not_ready",
                    "provider_pod_id": provider_pod_id,
                    "public_ip_present": bool(public_ip),
                    "ssh_port": ssh_port,
                    "attempts": attempts,
                    **last,
                }
            time.sleep(interval_seconds)

    def _wait_for_ssh_mapping(self, provider_pod_id: str, *, timeout_seconds: int = 180, interval_seconds: int = 5) -> dict[str, Any]:
        deadline = time.monotonic() + max(1, timeout_seconds)
        attempts = 0
        last: dict[str, Any] = {}
        poll_error = ""
        while True:
            attempts += 1
            try:
                pod = self.get_pod(provider_pod_id)
                poll_error = ""
            except Exception as exc:  # noqa: BLE001 - transient poll failures must not abort the wait
                pod = {}
                poll_error = repr(exc)
            last = pod if isinstance(pod, dict) else {}
            public_ip = str(last.get("publicIp") or "")
            port_mappings = last.get("portMappings") or {}
            ssh_port = port_mappings.get("22") or port_mappings.get(22)
            if public_ip and ssh_port:
                return {
                    "ok": True,
                    "provider_pod_id": provider_pod_id,
                    "public_ip": public_ip,
                    "ssh_port": ssh_port,
                    "attempts": attempts,
                }
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "reason": "ssh_mapping_missing",
                    "provider_pod_id": provider_pod_id,
                    "attempts": attempts,
                    "public_ip_present": bool(public_ip),
                    "port_mapping_keys": sorted(str(key) for key in port_mappings.keys()) if isinstance(port_mappings, dict) else [],
                    "desired_status": last.get("desiredStatus"),
                    "last_status_change": last.get("lastStatusChange"),
                    "poll_error": poll_error,
                }
            time.sleep(interval_seconds)

    def _workspace_disk_usage_script(self) -> str:
        return textwrap.dedent(
            r'''
            import json
            import shutil
            total, used, free = shutil.disk_usage("/workspace")
            print(json.dumps({"ok": True, "path": "/workspace", "total_bytes": total, "used_bytes": used, "available_bytes": free}))
            '''
        )

    def _model_tree_script(self) -> str:
        return textwrap.dedent(
            r'''
            import json
            import os
            import pathlib
            import time

            payload = json.loads(sys.stdin.read())
            folders = [str(item).strip("/").strip() for item in payload.get("folders") or [] if str(item).strip()]
            asset_root = pathlib.Path("/workspace/runpod-controller/assets/comfyui")
            comfyui_candidates = [
                pathlib.Path("/workspace/madapps/ComfyUI"),
                pathlib.Path("/workspace/runpod-slim/ComfyUI"),
                pathlib.Path("/workspace/ComfyUI"),
                pathlib.Path("/ComfyUI"),
            ]
            comfyui_root = next((root for root in comfyui_candidates if (root / "main.py").exists()), None)
            if comfyui_root is None:
                for path in pathlib.Path("/workspace").glob("**/ComfyUI/main.py"):
                    comfyui_root = path.parent
                    break

            def file_row(path, root, source):
                try:
                    st = path.lstat()
                    is_link = path.is_symlink()
                    target = os.readlink(path) if is_link else ""
                    resolved = str(path.resolve()) if is_link else ""
                    return {
                        "folder": str(path.relative_to(root)).split("/", 1)[0] if path != root else "",
                        "name": path.name,
                        "relative_path": str(path.relative_to(root)),
                        "path": str(path),
                        "size_bytes": st.st_size,
                        "mtime": int(st.st_mtime),
                        "source": source,
                        "is_symlink": is_link,
                        "symlink_target": target,
                        "resolved_path": resolved,
                        "controller_managed": str(path).startswith(str(asset_root)) or resolved.startswith(str(asset_root)),
                    }
                except Exception as exc:
                    return {"path": str(path), "source": source, "error": str(exc)}

            files = []
            for folder in folders:
                base = asset_root / folder
                if base.exists():
                    for path in sorted(base.rglob("*")):
                        if path.is_file() or path.is_symlink():
                            files.append(file_row(path, asset_root, "controller_assets"))
                if comfyui_root:
                    model_base = comfyui_root / "models" / folder
                    if model_base.exists():
                        for path in sorted(model_base.rglob("*")):
                            if path.is_file() or path.is_symlink():
                                files.append(file_row(path, comfyui_root / "models", "comfyui_models"))
            print(json.dumps({
                "ok": True,
                "asset_root": str(asset_root),
                "comfyui_root": str(comfyui_root) if comfyui_root else "",
                "folders": folders,
                "files": files,
                "generated_at": int(time.time()),
            }))
            '''
        )

    def _runtime_model_download_script(self) -> str:
        return textwrap.dedent(
            r'''
            import hashlib
            import json
            import os
            import pathlib
            import shutil
            import time
            import urllib.parse
            import urllib.request

            payload = json.loads(sys.stdin.read())
            url = str(payload["url"])
            target_path = pathlib.Path(str(payload["target_path"]))
            expected = payload.get("size_bytes")
            provider = str(payload.get("provider") or "generic")
            operation_id = str(payload.get("operation_id") or "runtime-download")
            progress_path = pathlib.Path("/workspace/runpod-controller/runtime-ops") / (operation_id + ".json")
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            asset_root = pathlib.Path("/workspace/runpod-controller/assets/comfyui").resolve()
            target_resolved = target_path.resolve().parent / target_path.name
            if not str(target_resolved).startswith(str(asset_root) + "/"):
                raise RuntimeError("target_outside_asset_root")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            part = pathlib.Path(str(target_path) + ".part")

            def write_progress(state, **extra):
                row = {"state": state, "target_path": str(target_path), "updated_at": time.time()}
                row.update(extra)
                progress_path.write_text(json.dumps(row) + "\n", encoding="utf-8")

            def sha256_file(path):
                h = hashlib.sha256()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                return h.hexdigest()

            def find_comfyui_root():
                candidates = [
                    pathlib.Path("/workspace/madapps/ComfyUI"),
                    pathlib.Path("/workspace/runpod-slim/ComfyUI"),
                    pathlib.Path("/workspace/ComfyUI"),
                    pathlib.Path("/ComfyUI"),
                ]
                for root in candidates:
                    if (root / "main.py").exists():
                        return root
                for path in pathlib.Path("/workspace").glob("**/ComfyUI/main.py"):
                    return path.parent
                return None

            def link_into_comfyui():
                root = find_comfyui_root()
                links = []
                if not root:
                    return links
                rel = target_path.relative_to(asset_root)
                first = rel.parts[0]
                dest_dirs = [root / "models" / first]
                if first == "diffusion_models":
                    dest_dirs.append(root / "models" / "unet")
                for base in dest_dirs:
                    dest = base / pathlib.Path(*rel.parts[1:])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists() or dest.is_symlink():
                        if dest.is_symlink() and os.readlink(dest) == str(target_path):
                            links.append(str(dest))
                            continue
                        if dest.is_symlink():
                            dest.unlink()
                        elif dest.is_file() and dest.stat().st_size == target_path.stat().st_size:
                            links.append(str(dest))
                            continue
                        else:
                            links.append("conflict:" + str(dest))
                            continue
                    dest.symlink_to(target_path)
                    links.append(str(dest))
                return links

            if target_path.exists() and expected and target_path.stat().st_size == int(expected):
                digest = sha256_file(target_path)
                links = link_into_comfyui()
                print(json.dumps({"ok": True, "state": "already_present", "target_path": str(target_path), "size_bytes": target_path.stat().st_size, "checksum_sha256": digest, "links": links}))
                raise SystemExit(0)

            parsed = urllib.parse.urlparse(url)
            cleaned_query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if value != "<redacted>"]
            url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(cleaned_query)))
            headers = {"User-Agent": "runpod-controller/1.0"}
            token = ""
            if provider == "huggingface":
                token = str(payload.get("hf_token") or "")
                if token:
                    headers["Authorization"] = "Bearer " + token
            elif provider == "civitai":
                token = str(payload.get("civitai_token") or "")
                if token:
                    parsed = urllib.parse.urlparse(url)
                    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                    if not any(key.lower() == "token" for key, _ in query):
                        query.append(("token", token))
                        url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))

            resume_at = part.stat().st_size if part.exists() else 0
            if resume_at:
                headers["Range"] = f"bytes={resume_at}-"
            write_progress("downloading", observed_bytes=resume_at, expected_bytes=expected or 0)
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resume_at and resp.status != 206:
                    part.unlink(missing_ok=True)
                    resume_at = 0
                mode = "ab" if resume_at else "wb"
                observed = resume_at
                started = time.time()
                with open(part, mode) as out:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        observed += len(chunk)
                        if int(time.time() - started) % 3 == 0:
                            write_progress("downloading", observed_bytes=observed, expected_bytes=expected or 0)
            size = part.stat().st_size
            if expected and size != int(expected):
                write_progress("failed", observed_bytes=size, expected_bytes=expected, error="size_mismatch")
                raise RuntimeError(f"size_mismatch:{size}!={expected}")
            part.replace(target_path)
            digest = sha256_file(target_path)
            links = link_into_comfyui()
            write_progress("downloaded", observed_bytes=size, expected_bytes=expected or size, checksum_sha256=digest)
            print(json.dumps({"ok": True, "state": "downloaded", "target_path": str(target_path), "size_bytes": size, "checksum_sha256": digest, "links": links}))
            '''
        )

    def _runtime_model_move_script(self) -> str:
        return textwrap.dedent(
            r'''
            import json
            import os
            import pathlib

            payload = json.loads(sys.stdin.read())
            source = pathlib.Path(str(payload["source_path"]))
            target = pathlib.Path(str(payload["target_path"]))
            asset_root = pathlib.Path("/workspace/runpod-controller/assets/comfyui").resolve()
            source_resolved = source.resolve().parent / source.name
            target_resolved = target.resolve().parent / target.name
            if not str(source_resolved).startswith(str(asset_root) + "/"):
                raise RuntimeError("source_outside_asset_root")
            if not str(target_resolved).startswith(str(asset_root) + "/"):
                raise RuntimeError("target_outside_asset_root")
            if not source.exists() or not source.is_file():
                raise RuntimeError("source_file_missing")
            if target.exists() and target.resolve() != source.resolve():
                raise RuntimeError("target_exists")

            def find_comfyui_root():
                for root in [pathlib.Path("/workspace/madapps/ComfyUI"), pathlib.Path("/workspace/runpod-slim/ComfyUI"), pathlib.Path("/workspace/ComfyUI"), pathlib.Path("/ComfyUI")]:
                    if (root / "main.py").exists():
                        return root
                for path in pathlib.Path("/workspace").glob("**/ComfyUI/main.py"):
                    return path.parent
                return None

            root = find_comfyui_root()

            def dest_paths(rel):
                first = rel.parts[0]
                bases = [root / "models" / first]
                if first == "diffusion_models":
                    bases.append(root / "models" / "unet")
                return [base / pathlib.Path(*rel.parts[1:]) for base in bases]

            # All conflict checks must run before os.replace: failing afterwards
            # would leave the file moved while the operation reports failure.
            planned = []
            stale_links = []
            if root:
                for dest in dest_paths(target.relative_to(asset_root)):
                    if dest.exists() or dest.is_symlink():
                        if dest.is_symlink():
                            link_target = os.readlink(dest)
                            link_alive = (dest.parent / link_target).exists()
                            if link_alive and link_target not in (str(target), str(source)):
                                raise RuntimeError("comfyui_target_exists:" + str(dest))
                        else:
                            raise RuntimeError("comfyui_target_exists:" + str(dest))
                    planned.append(dest)
                for old in dest_paths(source.relative_to(asset_root)):
                    if old.is_symlink() and os.readlink(old) == str(source):
                        stale_links.append(old)

            target.parent.mkdir(parents=True, exist_ok=True)
            size = source.stat().st_size
            os.replace(source, target)
            links = []
            for dest in planned:
                if dest.is_symlink():
                    if os.readlink(dest) == str(target):
                        links.append(str(dest))
                        continue
                    dest.unlink()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.symlink_to(target)
                links.append(str(dest))
            for old in stale_links:
                if old.is_symlink():
                    old.unlink()
            print(json.dumps({"ok": True, "state": "moved", "source_path": str(source), "target_path": str(target), "size_bytes": size, "links": links}))
            '''
        )

    def _comfyui_environment_script(self) -> str:
        return textwrap.dedent(
            r'''
            import json
            import os
            import pathlib
            import re
            import shutil
            import signal
            import subprocess
            import sys
            import time
            import urllib.error
            import urllib.request

            payload = json.loads(sys.stdin.read())
            assets = payload.get("assets") or []
            install_plan = payload.get("install_plan") or {"steps": []}
            validation_plan = payload.get("validation_plan") or {}
            custom_nodes = payload.get("custom_nodes") or []
            api_workflow = payload.get("api_workflow")
            ui_workflow = payload.get("ui_workflow")
            session_id = payload.get("session_id") or ""
            port = int(payload.get("comfyui_port") or 8188)

            def write_session_workflow(root):
                if not isinstance(ui_workflow, (dict, list)):
                    return {"written": False, "reason": "no_ui_workflow"}
                result = {"written": True}
                try:
                    workflows_dir = root / "user" / "default" / "workflows"
                    workflows_dir.mkdir(parents=True, exist_ok=True)
                    wf_path = workflows_dir / "runpod-controller-session.json"
                    wf_path.write_text(json.dumps(ui_workflow, indent=2), encoding="utf-8")
                    result["workflow_path"] = str(wf_path)
                except Exception as exc:
                    result["workflow_error"] = repr(exc)
                try:
                    ext_dir = root / "custom_nodes" / "runpod-controller-autoload"
                    web_dir = ext_dir / "web"
                    web_dir.mkdir(parents=True, exist_ok=True)
                    (ext_dir / "__init__.py").write_text(
                        'NODE_CLASS_MAPPINGS = {}\nNODE_DISPLAY_NAME_MAPPINGS = {}\nWEB_DIRECTORY = "./web"\n',
                        encoding="utf-8",
                    )
                    js = (
                        'import { app } from "../../scripts/app.js";\n\n'
                        "const WORKFLOW = " + json.dumps(ui_workflow) + ";\n"
                        "const KEY = " + json.dumps("runpod-controller-autoload:" + session_id) + ";\n\n"
                        "app.registerExtension({\n"
                        '  name: "runpod_controller.autoload",\n'
                        "  async setup() {\n"
                        "    try {\n"
                        "      if (localStorage.getItem(KEY)) return;\n"
                        '      localStorage.setItem(KEY, "1");\n'
                        "      await app.loadGraphData(WORKFLOW);\n"
                        "    } catch (err) {\n"
                        '      console.warn("runpod-controller autoload failed", err);\n'
                        "    }\n"
                        "  },\n"
                        "});\n"
                    )
                    (web_dir / "autoload.js").write_text(js, encoding="utf-8")
                    result["autoload_path"] = str(web_dir / "autoload.js")
                except Exception as exc:
                    result["autoload_error"] = repr(exc)
                return result

            def find_comfyui_root():
                candidates = [
                    pathlib.Path("/workspace/madapps/ComfyUI"),
                    pathlib.Path("/workspace/runpod-slim/ComfyUI"),
                    pathlib.Path("/workspace/ComfyUI"),
                    pathlib.Path("/ComfyUI"),
                ]
                for root in candidates:
                    if (root / "main.py").exists():
                        return root
                for root in pathlib.Path("/workspace").glob("**/ComfyUI/main.py"):
                    return root.parent
                return None

            def model_rel(asset):
                target = str(asset.get("target") or "").lstrip("/")
                prefix = "assets/comfyui/"
                if target.startswith(prefix):
                    return target[len(prefix):]
                folder = str(asset.get("model_folder") or "checkpoints").strip("/") or "checkpoints"
                name = pathlib.Path(target).name or str(asset.get("filename") or "asset")
                return f"{folder}/{name}"

            def dest_dirs(root, rel):
                first = rel.split("/", 1)[0]
                dirs = [root / "models" / first]
                if first == "diffusion_models":
                    dirs.append(root / "models" / "unet")
                return dirs

            def local_json(path, timeout=5):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except Exception:
                    return None

            def wait_system_stats(deadline):
                while time.monotonic() < deadline:
                    data = local_json("/system_stats", timeout=5)
                    if data:
                        return data
                    time.sleep(2)
                return None

            def visible(root, rel):
                first = rel.split("/", 1)[0]
                basename = pathlib.Path(rel).name
                lookup = {
                    "diffusion_models": "/object_info/UNETLoader",
                    "unet": "/object_info/UNETLoader",
                    "vae": "/object_info/VAELoader",
                    "text_encoders": "/object_info/CLIPLoader",
                    "clip": "/object_info/CLIPLoader",
                    "checkpoints": "/object_info/CheckpointLoaderSimple",
                    "loras": "/object_info/LoraLoader",
                    "upscale_models": "/object_info/UpscaleModelLoader",
                }.get(first)
                if not lookup:
                    return True
                data = local_json(lookup, timeout=10)
                if not data:
                    return False
                text = json.dumps(data)
                return basename in text or rel.split("/", 1)[-1] in text

            def find_python(root):
                python = None
                for candidate in [root / ".venv-cu128/bin/python", root / ".venv/bin/python"]:
                    if candidate.exists():
                        python = str(candidate)
                        break
                return python or shutil.which("python3") or "python3"

            def ensure_git():
                if shutil.which("git"):
                    return {"ok": True, "source": "existing"}
                apt = shutil.which("apt-get")
                if not apt:
                    return {"ok": False, "reason": "git_missing_and_apt_unavailable"}
                subprocess.run([apt, "update"], check=False, capture_output=True, text=True, timeout=300)
                proc = subprocess.run(
                    [apt, "install", "-y", "--no-install-recommends", "git", "ca-certificates"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=900,
                    env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
                )
                return {
                    "ok": proc.returncode == 0 and bool(shutil.which("git")),
                    "returncode": proc.returncode,
                    "stdout_tail": proc.stdout[-1000:],
                    "stderr_tail": proc.stderr[-1000:],
                }

            def is_commit_ref(ref):
                return bool(re.fullmatch(r"[0-9a-fA-F]{40}", ref or ""))

            def clone_locked_repo(repo_url, ref, target):
                if is_commit_ref(ref):
                    cmd = ["git", "clone", repo_url, str(target)]
                else:
                    cmd = ["git", "clone", "--depth", "1"]
                    if ref:
                        cmd += ["--branch", ref]
                    cmd += [repo_url, str(target)]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
                if proc.returncode != 0:
                    return proc
                if is_commit_ref(ref):
                    checkout = subprocess.run(["git", "-C", str(target), "checkout", ref], capture_output=True, text=True, timeout=120)
                    if checkout.returncode != 0:
                        checkout.stdout = (proc.stdout or "") + "\n" + (checkout.stdout or "")
                        checkout.stderr = (proc.stderr or "") + "\n" + (checkout.stderr or "")
                        return checkout
                return proc

            def restart_comfyui(root, output_dir):
                subprocess.run(["pkill", "-f", f"main.py.*{port}"], check=False)
                time.sleep(2)
                python = find_python(root)
                log_dir = pathlib.Path("/workspace/runpod-controller")
                log_dir.mkdir(parents=True, exist_ok=True)
                log = open(log_dir / "comfyui-controller-restart.log", "ab", buffering=0)
                return subprocess.Popen(
                    [
                        python,
                        "main.py",
                        "--listen",
                        "0.0.0.0",
                        "--port",
                        str(port),
                        "--enable-cors-header",
                        "--output-directory",
                        str(output_dir),
                    ],
                    cwd=str(root),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )

            def restart_log_tail():
                try:
                    return pathlib.Path("/workspace/runpod-controller/comfyui-controller-restart.log").read_text(errors="replace")[-1000:]
                except OSError:
                    return ""

            def install_custom_nodes(root):
                python = find_python(root)
                custom_root = root / "custom_nodes"
                custom_root.mkdir(parents=True, exist_ok=True)
                results = []
                ok = True
                git_ready = ensure_git()
                if not git_ready.get("ok"):
                    return {"ok": False, "python": python, "git": git_ready, "steps": []}
                for step in install_plan.get("steps") or []:
                    package = str(step.get("package") or "custom-node")
                    repo_url = str(step.get("repo_url") or "")
                    ref = str(step.get("ref") or "")
                    target = custom_root / package
                    row = {
                        "package": package,
                        "repo_url": repo_url,
                        "ref": ref,
                        "target": str(target),
                        "state": "pending",
                        "python": python,
                    }
                    try:
                        if not repo_url:
                            raise RuntimeError("repo_url_missing")
                        if not target.exists():
                            proc = clone_locked_repo(repo_url, ref, target)
                            row["git_returncode"] = proc.returncode
                            row["git_stdout_tail"] = proc.stdout[-1500:]
                            row["git_stderr_tail"] = proc.stderr[-1500:]
                            if proc.returncode != 0:
                                raise RuntimeError("git_clone_failed")
                        else:
                            row["already_present"] = True
                        rev = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
                        row["resolved_commit"] = rev.stdout.strip() if rev.returncode == 0 else ""
                        req = target / "requirements.txt"
                        row["requirements_present"] = req.exists()
                        if req.exists() and step.get("run_requirements", True):
                            pip = subprocess.run([python, "-m", "pip", "install", "-r", str(req)], capture_output=True, text=True, timeout=1200)
                            row["pip_returncode"] = pip.returncode
                            row["pip_stdout_tail"] = pip.stdout[-2000:]
                            row["pip_stderr_tail"] = pip.stderr[-2000:]
                            if pip.returncode != 0:
                                raise RuntimeError("pip_requirements_failed")
                        row["state"] = "passed"
                    except Exception as exc:
                        row["state"] = "failed"
                        row["error"] = str(exc)[-1000:]
                        ok = False
                    results.append(row)
                return {"ok": ok, "python": python, "steps": results}

            def custom_nodes_visible():
                expected = validation_plan.get("custom_node_types") or []
                if not expected:
                    return {"ok": True, "expected": [], "visible": [], "missing": [], "backend_missing": [], "frontend_declared": {}}
                data = local_json("/object_info", timeout=20)
                text = json.dumps(data or {})
                visible = [node_type for node_type in expected if str(node_type) in text]
                missing = [node_type for node_type in expected if str(node_type) not in text]
                frontend_declared = {}
                backend_missing = []
                custom_root = pathlib.Path(root) / "custom_nodes"
                for node_type in missing:
                    declared_paths = []
                    needle = str(node_type)
                    for subdir in ["web", "src_web"]:
                        for path in custom_root.glob("*/" + subdir + "/**/*"):
                            if path.suffix.lower() not in {".js", ".ts", ".mjs"}:
                                continue
                            try:
                                if needle in path.read_text(encoding="utf-8", errors="ignore"):
                                    declared_paths.append(str(path.relative_to(custom_root)))
                            except Exception:
                                continue
                    if declared_paths:
                        frontend_declared[node_type] = declared_paths[:5]
                    else:
                        backend_missing.append(node_type)
                return {
                    "ok": not backend_missing,
                    "expected": expected,
                    "visible": visible,
                    "missing": missing,
                    "backend_missing": backend_missing,
                    "frontend_declared": frontend_declared,
                }

            def run_api_smoke():
                if not api_workflow:
                    return {"ok": True, "state": "not_requested"}
                try:
                    body = json.dumps({"prompt": api_workflow, "client_id": "runpod-controller-" + session_id}).encode("utf-8")
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/prompt",
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    prompt_id = data.get("prompt_id")
                    if not prompt_id:
                        return {"ok": False, "state": "submitted_without_prompt_id", "response": data}
                    deadline = time.monotonic() + 600
                    while time.monotonic() < deadline:
                        history = local_json("/history/" + str(prompt_id), timeout=20)
                        if history and str(prompt_id) in history:
                            return {"ok": True, "state": "completed", "prompt_id": prompt_id}
                        time.sleep(5)
                    return {"ok": False, "state": "timeout", "prompt_id": prompt_id}
                except Exception as exc:
                    return {"ok": False, "state": "failed", "error": str(exc)[-1000:]}

            root = find_comfyui_root()
            if not root:
                searched = [
                    "/workspace/madapps/ComfyUI/main.py",
                    "/workspace/runpod-slim/ComfyUI/main.py",
                    "/workspace/ComfyUI/main.py",
                    "/ComfyUI/main.py",
                    "/workspace/**/ComfyUI/main.py",
                ]
                observed = []
                for candidate in [
                    pathlib.Path("/workspace/madapps/ComfyUI"),
                    pathlib.Path("/workspace/runpod-slim/ComfyUI"),
                    pathlib.Path("/workspace/ComfyUI"),
                    pathlib.Path("/ComfyUI"),
                ]:
                    observed.append({
                        "path": str(candidate),
                        "exists": candidate.exists(),
                        "main_py": (candidate / "main.py").exists(),
                        "models": (candidate / "models").exists(),
                    })
                print(json.dumps({"ok": False, "reason": "comfyui_root_not_found", "searched_paths": searched, "observed_paths": observed}))
                raise SystemExit(0)
            runs = pathlib.Path("/workspace/runpod-controller/runs") / session_id
            output_dir = runs / "outputs" / "raw"
            output_dir.mkdir(parents=True, exist_ok=True)
            fallback_output_dirs = [
                str(root / "output"),
                "/workspace/madapps/ComfyUI/output",
                "/workspace/ComfyUI/output",
                "/workspace/runpod-slim/ComfyUI/output",
            ]
            install_result = install_custom_nodes(root)
            linked = []
            missing_sources = []
            for asset in assets:
                rel = model_rel(asset)
                source = pathlib.Path("/workspace/runpod-controller/assets/comfyui") / rel
                if not source.exists():
                    missing_sources.append(str(source))
                    continue
                for base in dest_dirs(root, rel):
                    dest = base / "/".join(rel.split("/")[1:])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists() or dest.is_symlink():
                        if dest.is_symlink() and os.readlink(dest) == str(source):
                            linked.append(str(dest))
                            continue
                        if dest.is_symlink():
                            dest.unlink()
                        elif dest.is_file() and dest.stat().st_size == source.stat().st_size:
                            linked.append(str(dest))
                            continue
                        else:
                            linked.append("conflict:" + str(dest))
                            continue
                    dest.symlink_to(source)
                    linked.append(str(dest))
            runs.mkdir(parents=True, exist_ok=True)
            # Best-effort: saved into the workflow sidebar plus a tiny frontend
            # extension that opens the session workflow on first visit.
            session_workflow = write_session_workflow(root)
            core_rels = [model_rel(asset) for asset in assets]
            visible_before = []
            custom_before = custom_nodes_visible()
            restart_proc = restart_comfyui(root, output_dir)
            restarted = True
            stats = wait_system_stats(time.monotonic() + 240)
            visible_after = [rel for rel in core_rels if visible(root, rel)]
            custom_after = custom_nodes_visible()
            smoke = run_api_smoke()
            output_dir_ok = output_dir.exists() and output_dir.is_dir()
            # A pod-supervised ComfyUI can keep answering health checks after our
            # forced-output instance dies, silently writing outputs off-volume.
            restart_alive = restart_proc.poll() is None
            pod_info = {
                "comfyui_root": str(root),
                "output_dir": str(output_dir),
                "fallback_output_dirs": fallback_output_dirs,
                "run_prefix": f"runpod-controller/runs/{session_id}",
                "linked": linked,
                "custom_node_install": install_result,
                "custom_nodes": custom_nodes,
                "session_workflow": session_workflow,
            }
            (runs / "pod-info.json").write_text(json.dumps(pod_info, indent=2) + "\n", encoding="utf-8")
            missing_visible_models = [rel for rel in core_rels if rel not in visible_after]
            failure_reasons = []
            if not stats:
                failure_reasons.append("system_stats_failed")
            if not install_result.get("ok"):
                failure_reasons.append("custom_node_install_failed")
            if not custom_after.get("ok"):
                failure_reasons.append("custom_node_visibility_failed")
            if not smoke.get("ok"):
                failure_reasons.append("api_smoke_failed")
            if not output_dir_ok:
                failure_reasons.append("output_dir_missing")
            if not restart_alive:
                failure_reasons.append("forced_output_comfyui_died")
            if missing_sources:
                failure_reasons.append("asset_sources_missing")
            if missing_visible_models:
                failure_reasons.append("model_visibility_failed")
            ok = not failure_reasons
            print(json.dumps({
                "ok": ok,
                "reason": "configured" if ok else ",".join(failure_reasons),
                "comfyui_root": str(root),
                "output_dir": str(output_dir),
                "output_dir_ok": output_dir_ok,
                "restart_alive": restart_alive,
                "restart_log_tail": "" if restart_alive else restart_log_tail(),
                "fallback_output_dirs": fallback_output_dirs,
                "run_prefix": f"runpod-controller/runs/{session_id}",
                "comfyui_python": install_result.get("python"),
                "linked_count": len(linked),
                "missing_sources": missing_sources,
                "missing_visible_models": missing_visible_models,
                "custom_node_install": install_result,
                "custom_visible_before": custom_before,
                "custom_visible_after": custom_after,
                "api_smoke": smoke,
                "visible_before": visible_before,
                "visible_after": visible_after,
                "restarted": restarted,
                "session_workflow": session_workflow,
            }))
            '''
        )

    def get_pod(self, provider_pod_id: str) -> dict[str, Any]:
        status, payload = self._request("GET", f"/pods/{urllib.parse.quote(provider_pod_id)}")
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError(f"Pod get failed: HTTP {status}: {payload}")
        return payload

    def stop_pod(self, provider_pod_id: str) -> dict[str, Any]:
        status, payload = self._request("POST", f"/pods/{urllib.parse.quote(provider_pod_id)}/stop", {})
        return {"status_code": status, "response": payload, "ok": status in {200, 201, 202, 204}}

    def delete_pod(self, provider_pod_id: str) -> dict[str, Any]:
        status, payload = self._request("DELETE", f"/pods/{urllib.parse.quote(provider_pod_id)}")
        return {"status_code": status, "response": payload, "ok": status in {200, 202, 204, 404}}

    def delete_network_volume(self, provider_volume_id: str) -> dict[str, Any]:
        status, payload = self._request("DELETE", f"/networkvolumes/{urllib.parse.quote(provider_volume_id)}")
        absent = status == 500 and any(
            phrase in str(payload).lower()
            for phrase in ["nonexistent network volume", "not found"]
        )
        return {"status_code": status, "response": payload, "ok": status in {200, 202, 204, 404} or absent}

    def update_network_volume_size(self, provider_volume_id: str, size_gb: int) -> dict[str, Any]:
        status, payload = self._request(
            "POST",
            f"/networkvolumes/{urllib.parse.quote(provider_volume_id)}/update",
            {"size": int(size_gb)},
            timeout=120,
        )
        return {"status_code": status, "response": payload, "ok": status in {200, 201, 202}}

    def billing_pods(self, *, start_time: str, end_time: str, bucket_size: str = "hour") -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "startTime": start_time,
                "endTime": end_time,
                "bucketSize": bucket_size,
                "grouping": "podId",
            }
        )
        status, payload = self._request("GET", f"/billing/pods?{query}")
        if status != 200 or not isinstance(payload, list):
            raise RuntimeError(f"Pod billing sync failed: HTTP {status}: {payload}")
        return payload

    def billing_network_volumes(self, *, start_time: str, end_time: str, bucket_size: str = "hour") -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "startTime": start_time,
                "endTime": end_time,
                "bucketSize": bucket_size,
            }
        )
        status, payload = self._request("GET", f"/billing/networkvolumes?{query}")
        if status != 200 or not isinstance(payload, list):
            raise RuntimeError(f"Network volume billing sync failed: HTTP {status}: {payload}")
        return payload


def build_adapter(settings: Settings) -> RunpodAdapter:
    return RunpodRestAdapter(settings)
