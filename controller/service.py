from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import math
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from collections.abc import Callable
from typing import Any

from .asset_metadata import MIN_NETWORK_VOLUME_GB, duplicate_url_keys, normalized_url_key, peek_url_metadata, target_for, volume_size_gb_for_assets
from .assets import canonical_asset_url, detect_provider, is_secret_query_key, normalize_asset_manifest, redact_url
from .comfyui_workflow import (
    analyze_comfyui_workflow,
    dependency_fingerprint,
    extract_model_requirements,
    launch_template_fingerprint,
    normalize_custom_nodes,
    normalize_workflow_json,
    rewrite_workflow_model_references,
    workflow_hash,
    workflow_launch_fingerprint,
)
from .comfyui_registry import ComfyRegistryClient
from .config import Settings
from .costing import enrich_pod_cost, enrich_volume_cost, format_runtime, network_volume_rate_usd_per_hr
from .db import Database
from .gpu_catalog import COMFYUI_CANDIDATE_DATA_CENTERS, comfyui_gpu_rows, dryrun_data_centers, normalize_gpu_vendor, normalize_min_vram_gb
from .runpod import RunpodAdapter
from .s3_volume import RunpodS3VolumeClient, has_s3_credentials, summarize_objects
from .utils import json_dumps, json_loads, new_id, parse_iso, read_json, redact_secrets, sha256_text, utc_iso, utc_now, write_json


OUTPUT_COLLECTION_STATES = {
    "environment_configuring",
    "tunnel_ready",
    "interactive_ready",
    "reclaim_pending",
    "collecting_outputs",
}
OUTPUT_FILE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".mp4",
    ".webm",
    ".mov",
    ".avi",
    ".json",
    ".txt",
}
COMFYUI_MODEL_FOLDERS = [
    "checkpoints",
    "diffusion_models",
    "loras",
    "vae",
    "text_encoders",
    "clip",
    "clip_vision",
    "controlnet",
    "upscale_models",
    "ultralytics",
    "embeddings",
    "SEEDVR2",
    "unet",
    "configs",
    "gligen",
    "hypernetworks",
]
TERMINAL_SESSION_STATES = {
    "reclaimed",
    "finalized",
    "failed",
    "cleanup_failed",
    "output_collection_failed_keep_volume",
    "output_collection_empty_keep_volume",
}


class ControllerService:
    def __init__(self, settings: Settings, db: Database, adapter: RunpodAdapter):
        self.settings = settings
        self.db = db
        self.adapter = adapter
        self._gpu_attempt_lock = threading.Lock()
        self._tunnel_lock = threading.Lock()
        self._output_collection_locks: dict[str, threading.Lock] = {}
        self._output_collection_locks_lock = threading.Lock()

    def audit(self, subject_type: str, subject_id: str, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.db.insert(
            "audit_events",
            {
                "id": new_id("aud"),
                "subject_type": subject_type,
                "subject_id": subject_id,
                "event_type": event_type,
                "message": message,
                "details_json": json_dumps(details or {}),
                "created_at": utc_iso(),
            },
        )

    def create_resource_request(self, payload: dict[str, Any], *, process_inline: bool = True) -> dict[str, Any]:
        now = utc_iso()
        request_id = new_id("req")
        product = str(payload.get("product") or self.settings.product)
        mode = str(payload.get("mode") or "interactive")
        row = {
            "id": request_id,
            "product": product,
            "mode": mode,
            "state": "accepted",
            "poll_after_seconds": 5,
            "requested_json": json_dumps(self._redact(payload)),
            "result_json": None,
            "error": None,
            "session_id": None,
            "created_at": now,
            "updated_at": now,
        }
        self.db.insert("resource_requests", row)
        self.audit("resource_request", request_id, "accepted", "Resource request accepted", {"payload": self._redact(payload)})
        if process_inline:
            self._process_workflow_resource_request(request_id)
        else:
            self._start_resource_request_thread(request_id)
        return self.get_resource_request(request_id) or row

    def _start_resource_request_thread(self, request_id: str) -> None:
        def run() -> None:
            try:
                self._process_workflow_resource_request(request_id)
            except Exception as exc:  # noqa: BLE001
                self.db.update(
                    "resource_requests",
                    request_id,
                    {
                        "state": "failed",
                        "error": repr(exc),
                        "updated_at": utc_iso(),
                    },
                )
                self.audit("resource_request", request_id, "failed", "Resource request background processing failed", {"error": repr(exc)})

        threading.Thread(target=run, daemon=True, name=f"resource-request-{request_id}").start()

    def dry_run_resource_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        product = str(payload.get("product") or self.settings.product)
        workflow = None
        if payload.get("workflow_id"):
            workflow = self.get_comfyui_workflow(str(payload["workflow_id"]))
            if not workflow:
                return {"ok": False, "dry_run": True, "creates_resources": False, "error": "unknown_workflow", "warnings": [], "candidates": [], "candidate_groups": []}
        launch_template = None
        if payload.get("launch_template_id") and not workflow:
            launch_template = self.get_comfyui_launch_template(str(payload["launch_template_id"]))
        assets = normalize_asset_manifest((workflow or launch_template or {}).get("ready_assets") or (launch_template or {}).get("assets") or payload.get("assets") or [])
        duplicate_keys = duplicate_url_keys(assets)
        missing_sizes = [asset for asset in assets if asset.get("size_bytes") is None]
        if assets and not missing_sizes:
            size_gb = max(MIN_NETWORK_VOLUME_GB, int(payload.get("network_volume_size_gb") or volume_size_gb_for_assets(assets)))
            volume_status = "computed"
        else:
            size_gb = max(MIN_NETWORK_VOLUME_GB, int(payload.get("network_volume_size_gb") or self.settings.default_volume_size_gb))
            volume_status = "asset_size_unknown" if missing_sizes else "defaulted"
        max_rate = float(payload.get("max_gpu_usd_per_hr") or self.settings.default_max_gpu_usd_per_hr)
        max_total = float(payload.get("max_total_usd") or self.settings.default_max_total_usd)
        lease_minutes = int(payload.get("lease_minutes") or self.settings.default_lease_minutes)
        min_vram_gb = normalize_min_vram_gb(payload.get("min_vram_gb") or self.settings.default_min_vram_gb)
        gpu_vendor = normalize_gpu_vendor(payload.get("gpu_vendor") or self.settings.default_gpu_vendor)
        warnings = ["dry_run_only_no_resources_created"]
        if duplicate_keys:
            warnings.append("duplicate_asset_url")
        if gpu_vendor != "NVIDIA":
            return {
                "ok": False,
                "dry_run": True,
                "creates_resources": False,
                "error": f"unsupported_gpu_vendor:{gpu_vendor}",
                "warnings": warnings,
                "candidates": [],
                "candidate_groups": [],
            }
        gpu_rows = comfyui_gpu_rows(min_vram_gb=min_vram_gb, gpu_vendor=gpu_vendor, max_usd_per_hr=max_rate)
        data_centers = self._candidate_seed_datacenters(payload)
        matrix = self.adapter.scout_gpu_matrix(
            {
                "product": product,
                "data_centers": data_centers,
                "gpu_rows": gpu_rows,
                "min_vram_gb": min_vram_gb,
                "gpu_vendor": gpu_vendor,
                "max_gpu_usd_per_hr": max_rate,
            }
        )
        matrix_ok = bool(matrix.get("ok"))
        matrix_authoritative = bool(matrix_ok and matrix.get("authoritative"))
        raw_candidates = list(matrix.get("candidates") or []) if matrix_ok else []
        candidates = [
            candidate
            for candidate in raw_candidates
            if candidate.get("gpu_type_id") and str(candidate.get("scout_status") or "") != "no_datacenter_gpu_rows"
        ]
        if not matrix_ok:
            warnings.append(f"datacenter_gpu_scout_unavailable:{matrix.get('reason') or 'unknown'}")
        confirmed_candidate_count = sum(1 for candidate in candidates if candidate.get("eligible"))
        listed_candidate_count = len(candidates) - confirmed_candidate_count
        groups = []
        for dc in data_centers:
            group_candidates = [candidate for candidate in candidates if candidate["data_center_id"] == dc]
            confirmed_in_group = any(candidate.get("eligible") for candidate in group_candidates)
            runpodctl_in_group = any(
                str(candidate.get("quote_source") or "").startswith("runpodctl") for candidate in group_candidates
            )
            if not group_candidates:
                continue
            groups.append(
                {
                    "data_center_id": dc,
                    "scope": (
                        "runpodctl_datacenter_list_gpuAvailability"
                        if runpodctl_in_group
                        else
                        "runpod_graphql_datacenter_lowestPrice"
                        if confirmed_in_group
                        else "runpod_graphql_gpu_type_datacenter_listing"
                    ),
                    "gpu_types": [
                        {
                            "gpu_type_id": candidate["gpu_type_id"],
                            "quoted_cost_usd_per_hr": candidate.get("quoted_cost_usd_per_hr"),
                            "estimated_cost_usd_per_hr": candidate.get("estimated_cost_usd_per_hr"),
                            "quote_source": candidate.get("quote_source"),
                            "scout_status": candidate.get("scout_status"),
                            "stock_status": candidate.get("stock_status"),
                            "eligible": bool(candidate.get("eligible")),
                        }
                        for candidate in group_candidates
                    ],
                }
            )
        return {
            "ok": True,
            "dry_run": True,
            "creates_resources": False,
            "product": product,
            "mode": str(payload.get("mode") or "interactive"),
            "volume_size_gb": size_gb,
            "volume_status": volume_status,
            "asset_count": len(assets),
            "unknown_asset_count": len(missing_sizes),
            "min_vram_gb": min_vram_gb,
            "gpu_vendor": gpu_vendor,
            "max_gpu_usd_per_hr": max_rate,
            "max_total_usd": max_total,
            "lease_minutes": lease_minutes,
            "data_center_count": len(groups),
            "candidate_data_center_count": len(groups),
            "scouted_data_center_count": len(data_centers),
            "gpu_type_count": len(gpu_rows),
            "candidate_count": len(candidates),
            "confirmed_candidate_count": confirmed_candidate_count,
            "listed_candidate_count": listed_candidate_count,
            "datacenter_gpu_scout_authoritative": matrix_authoritative,
            "datacenter_gpu_listing_available": bool(matrix_ok and matrix.get("listing_available")),
            "datacenter_gpu_scout_reason": matrix.get("reason"),
            "candidate_groups": groups,
            "candidates": candidates,
            "warnings": warnings,
            "workflow": {
                "id": workflow.get("id"),
                "name": workflow.get("name"),
                "workflow_hash": workflow.get("workflow_hash"),
                "status": workflow.get("status"),
                "verification_state": workflow.get("verification_state"),
            } if workflow else None,
            "launch_template": {
                "id": launch_template.get("id"),
                "name": launch_template.get("name"),
                "analyzer_ok": bool((launch_template.get("analyzer_result") or {}).get("ok")),
                "last_probe_id": launch_template.get("last_probe_id"),
            } if launch_template else None,
        }

    def peek_asset(self, payload: dict[str, Any]) -> dict[str, Any]:
        product = str(payload.get("product") or self.settings.product)
        url = str(payload.get("url") or "").strip()
        model_folder = str(payload.get("model_folder") or payload.get("folder") or "checkpoints")
        if not url:
            raise ValueError("url is required")
        url_key = normalized_url_key(url)
        if not payload.get("force_refresh"):
            cached = self.db.query(
                "SELECT * FROM asset_metadata_cache WHERE product = ? AND url_key = ? AND model_folder = ?",
                (product, url_key, model_folder),
            )
            if cached and not self._asset_cache_needs_refresh(url, cached[0]):
                return self._asset_cache_row(cached[0], cache_hit=True)
        try:
            metadata = peek_url_metadata(url, model_folder)
        except Exception as exc:  # noqa: BLE001
            provider = detect_provider({"url": url})
            return {
                "id": None,
                "product": product,
                "provider": provider,
                "original_url_redacted": redact_url(url),
                "download_url_redacted": redact_url(canonical_asset_url(url)),
                "final_url_redacted": redact_url(url),
                "filename": url.rsplit("/", 1)[-1] or "asset",
                "size_bytes": payload.get("manual_size_bytes"),
                "size_unknown": payload.get("manual_size_bytes") is None,
                "content_type": None,
                "redirects": [],
                "model_folder": model_folder,
                "target": target_for(model_folder, url.rsplit("/", 1)[-1] or "asset"),
                "observed_at": utc_iso(),
                "cache_hit": False,
                "error": repr(exc),
            }
        row_id = f"amc_{sha256_text(json_dumps({'product': product, 'url_key': url_key, 'folder': model_folder}))[:20]}"
        now = utc_iso()
        self.db.execute(
            """
            INSERT OR REPLACE INTO asset_metadata_cache (
              id, product, url_key, original_url_redacted, final_url_redacted,
              provider, model_folder, filename, size_bytes, size_unknown,
              content_type, etag, last_modified, redirects_json, target,
              observed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM asset_metadata_cache WHERE id = ?), ?), ?)
            """,
            (
                row_id,
                product,
                url_key,
                metadata["original_url_redacted"],
                metadata.get("final_url_redacted"),
                metadata.get("provider") or "generic",
                metadata.get("model_folder") or model_folder,
                metadata.get("filename") or "asset",
                metadata.get("size_bytes"),
                1 if metadata.get("size_unknown") else 0,
                metadata.get("content_type"),
                metadata.get("etag"),
                metadata.get("last_modified"),
                json_dumps(metadata.get("redirects") or []),
                metadata.get("target") or target_for(model_folder, metadata.get("filename") or "asset"),
                now,
                row_id,
                now,
                now,
            ),
        )
        result = self._asset_cache_row(self.db.get("asset_metadata_cache", row_id) or {}, cache_hit=False)
        return result

    def list_asset_metadata_cache(self, product: str | None = None) -> list[dict[str, Any]]:
        product = product or self.settings.product
        rows = self.db.query("SELECT * FROM asset_metadata_cache WHERE product = ? ORDER BY updated_at DESC", (product,))
        return [self._asset_cache_row(row, cache_hit=True) for row in rows]

    def _asset_cache_row(self, row: dict[str, Any], *, cache_hit: bool) -> dict[str, Any]:
        return {
            "id": row.get("id"),
            "product": row.get("product"),
            "provider": row.get("provider"),
            "filename": row.get("filename"),
            "size_bytes": row.get("size_bytes"),
            "size_unknown": bool(row.get("size_unknown")),
            "content_type": row.get("content_type"),
            "final_url_redacted": row.get("final_url_redacted"),
            "original_url_redacted": row.get("original_url_redacted"),
            "download_url_redacted": redact_url(canonical_asset_url(str(row.get("original_url_redacted") or ""))),
            "redirects": json_loads(row.get("redirects_json"), []),
            "cache_hit": cache_hit,
            "model_folder": row.get("model_folder"),
            "target": row.get("target"),
            "observed_at": row.get("observed_at"),
        }

    def _asset_cache_needs_refresh(self, url: str, row: dict[str, Any]) -> bool:
        provider = str(row.get("provider") or detect_provider({"url": url}))
        if provider != "huggingface":
            return False
        if "/blob/" not in url:
            return False
        final_url = str(row.get("final_url_redacted") or "")
        content_type = str(row.get("content_type") or "").lower()
        query = urllib.parse.parse_qsl(urllib.parse.urlparse(final_url).query, keep_blank_values=True)
        has_unredacted_signed_params = any(is_secret_query_key(key) and value != "<redacted>" for key, value in query)
        return "/blob/" in final_url or content_type.startswith("text/html") or has_unredacted_signed_params

    def analyze_comfyui_workflow(self, payload: dict[str, Any]) -> dict[str, Any]:
        return analyze_comfyui_workflow(payload, registry_resolver=self._comfyui_registry_resolver())

    def _comfyui_registry_resolver(self) -> Callable[[str], dict[str, Any] | None] | None:
        if not self.settings.comfyui_registry_lookup:
            return None
        return self._resolve_comfyui_registry_node

    def _comfyui_registry_cache_path(self) -> pathlib.Path:
        return self.settings.cache_dir / "comfyui-registry-node-cache.json"

    def _read_comfyui_registry_cache(self) -> dict[str, Any]:
        return read_json(self._comfyui_registry_cache_path(), {}) or {}

    def _write_comfyui_registry_cache(self, cache: dict[str, Any]) -> None:
        write_json(self._comfyui_registry_cache_path(), cache)

    def _resolve_comfyui_registry_node(self, node_type: str) -> dict[str, Any] | None:
        key = str(node_type or "").strip()
        if not key:
            return None
        cache = self._read_comfyui_registry_cache()
        cached = cache.get(key)
        if isinstance(cached, dict):
            if cached.get("state") == "miss":
                return None
            return cached
        client = ComfyRegistryClient(timeout_seconds=self.settings.comfyui_registry_timeout_seconds)
        result = client.resolve_comfy_node_name(key)
        cache[key] = result if result else {"state": "miss", "observed_at": utc_iso()}
        self._write_comfyui_registry_cache(cache)
        return result

    def list_comfyui_workflows(self, product: str | None = None) -> list[dict[str, Any]]:
        product = product or self.settings.product
        self._migrate_legacy_launch_templates(product)
        rows = self.db.query(
            "SELECT * FROM comfyui_workflows WHERE product = ? AND status != 'deleted' ORDER BY updated_at DESC, name",
            (product,),
        )
        return [self._workflow_public(row) for row in rows]

    def get_comfyui_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        self._migrate_legacy_launch_templates(self.settings.product)
        row = self.db.get("comfyui_workflows", workflow_id)
        if not row or row.get("status") == "deleted":
            return None
        return self._workflow_public(row)

    def upload_comfyui_workflow(self, payload: dict[str, Any]) -> dict[str, Any]:
        product = str(payload.get("product") or self.settings.product)
        workflow = normalize_workflow_json(payload.get("content") or payload.get("workflow_json") or payload.get("ui_workflow_json"))
        if workflow is None:
            raise ValueError("workflow JSON content is required")
        digest = workflow_hash(workflow)
        existing = self.db.query(
            "SELECT * FROM comfyui_workflows WHERE product = ? AND workflow_hash = ? LIMIT 1",
            (product, digest),
        )
        if existing:
            row = existing[0]
            if row.get("status") == "deleted":
                values = self._build_workflow_values(
                    {
                        "product": product,
                        "name": payload.get("name") or payload.get("filename") or f"workflow-{digest[:10]}",
                        "workflow": workflow,
                        "original_filename": payload.get("filename"),
                    },
                    workflow_id=row["id"],
                    created_at=row["created_at"],
                )
                values.pop("id")
                values.pop("created_at")
                self.db.update("comfyui_workflows", row["id"], values)
                self.audit("comfyui_workflow", row["id"], "restored", "ComfyUI workflow restored from matching upload", {"workflow_hash": digest})
                return self.get_comfyui_workflow(row["id"]) or values
            updates = {"updated_at": utc_iso()}
            if payload.get("name"):
                updates["name"] = str(payload["name"]).strip()
            if payload.get("filename"):
                updates["original_filename"] = str(payload["filename"]).strip()
            self.db.update("comfyui_workflows", row["id"], updates)
            return self.get_comfyui_workflow(row["id"]) or row
        workflow_id = new_id("cw")
        now = utc_iso()
        values = self._build_workflow_values(
            {
                "product": product,
                "name": payload.get("name") or payload.get("filename") or f"workflow-{digest[:10]}",
                "workflow": workflow,
                "original_filename": payload.get("filename"),
            },
            workflow_id=workflow_id,
            created_at=now,
        )
        self.db.insert("comfyui_workflows", values)
        self.audit("comfyui_workflow", workflow_id, "uploaded", "ComfyUI workflow uploaded", {"workflow_hash": digest})
        return self.get_comfyui_workflow(workflow_id) or values

    def update_comfyui_workflow(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self.db.get("comfyui_workflows", workflow_id)
        if not existing or existing.get("status") == "deleted":
            raise KeyError(workflow_id)
        merged = self._workflow_public(existing)
        if "name" in payload:
            merged["name"] = str(payload.get("name") or merged.get("name") or "").strip()
        if "extracted_assets" in payload:
            merged["extracted_assets"] = payload.get("extracted_assets") or []
        if "extra_assets" in payload or "assets" in payload:
            merged["extra_assets"] = payload.get("extra_assets") if "extra_assets" in payload else payload.get("assets")
        if "node_locks" in payload:
            merged["node_locks"] = payload.get("node_locks") or []
        if "node_mappings" in payload:
            merged["node_mappings"] = payload.get("node_mappings") or {}
        values = self._build_workflow_values(merged, workflow_id=workflow_id, created_at=existing["created_at"])
        values.pop("id")
        values.pop("created_at")
        self.db.update("comfyui_workflows", workflow_id, values)
        self.audit("comfyui_workflow", workflow_id, "updated", "ComfyUI workflow updated", {"status": values["status"]})
        return self.get_comfyui_workflow(workflow_id) or values

    def delete_comfyui_workflow(self, workflow_id: str) -> dict[str, Any]:
        if not self.db.get("comfyui_workflows", workflow_id):
            raise KeyError(workflow_id)
        active = self.db.query(
            """
            SELECT sw.id
            FROM session_workflows sw
            JOIN sessions s ON s.id = sw.session_id
            WHERE sw.comfyui_workflow_id = ?
              AND s.state NOT IN ('deleted','reclaimed','finalized','failed','cleanup_failed','terminated')
            LIMIT 1
            """,
            (workflow_id,),
        )
        if active:
            return {"ok": False, "id": workflow_id, "error": "workflow_in_use_by_active_session"}
        self.db.update("comfyui_workflows", workflow_id, self._deleted_workflow_values())
        return {"ok": True, "id": workflow_id}

    WORKFLOW_PACKAGE_FORMAT = "comfyui-controller-workflow-package"
    WORKFLOW_PACKAGE_VERSION = 1
    WORKFLOW_PACKAGE_MEMBER_LIMIT = 64 * 1024 * 1024

    def export_comfyui_workflow_package(self, workflow_id: str) -> tuple[str, bytes]:
        """Bundle a workflow plus its resolved metadata into a shareable zip.

        The package carries everything another controller needs to launch the
        same session: the workflow JSON, locked custom nodes, and model URLs
        with sizes. Asset URLs are stored redacted, so no tokens can leak.
        """
        workflow = self.get_comfyui_workflow(workflow_id)
        if not workflow:
            raise KeyError(workflow_id)
        manifest = {
            "format": self.WORKFLOW_PACKAGE_FORMAT,
            "version": self.WORKFLOW_PACKAGE_VERSION,
            "name": workflow.get("name"),
            "workflow_hash": workflow.get("workflow_hash"),
            "original_filename": workflow.get("original_filename"),
            "exported_at": utc_iso(),
            "node_locks": workflow.get("node_locks") or [],
            "node_mappings": workflow.get("node_mappings") or {},
            "extracted_assets": [self._shareable_asset(asset) for asset in workflow.get("extracted_assets") or []],
            "extra_assets": [self._shareable_asset(asset) for asset in workflow.get("extra_assets") or []],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("workflow.json", json_dumps(workflow.get("workflow")))
            archive.writestr("manifest.json", json_dumps(manifest))
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(workflow.get("name") or "workflow")).strip("-") or "workflow"
        self.audit("comfyui_workflow", workflow_id, "exported", "ComfyUI workflow package exported", {"name": workflow.get("name")})
        return f"{slug[:64]}.comfyui-pack.zip", buffer.getvalue()

    def import_comfyui_workflow_package(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("package_base64") or payload.get("content_base64")
        if not raw:
            raise ValueError("package_base64 is required")
        try:
            data = base64.b64decode(str(raw), validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid_package:base64_decode_failed:{type(exc).__name__}") from exc
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                if "workflow.json" not in names or "manifest.json" not in names:
                    raise ValueError("invalid_package:missing_workflow_or_manifest")
                for member in ("workflow.json", "manifest.json"):
                    if archive.getinfo(member).file_size > self.WORKFLOW_PACKAGE_MEMBER_LIMIT:
                        raise ValueError("invalid_package:member_too_large")
                workflow_json = json_loads(archive.read("workflow.json").decode("utf-8"), None)
                manifest = json_loads(archive.read("manifest.json").decode("utf-8"), None)
        except zipfile.BadZipFile as exc:
            raise ValueError("invalid_package:not_a_zip") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != self.WORKFLOW_PACKAGE_FORMAT:
            raise ValueError("invalid_package:unknown_format")
        try:
            version = int(manifest.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version < 1 or version > self.WORKFLOW_PACKAGE_VERSION:
            raise ValueError(f"invalid_package:unsupported_version:{manifest.get('version')}")
        if workflow_json is None:
            raise ValueError("invalid_package:workflow_json_invalid")
        hash_matched = workflow_hash(workflow_json) == str(manifest.get("workflow_hash") or "")
        uploaded = self.upload_comfyui_workflow(
            {
                "content": workflow_json,
                "name": payload.get("name") or manifest.get("name"),
                "filename": manifest.get("original_filename") or payload.get("filename"),
            }
        )
        updates: dict[str, Any] = {}
        if manifest.get("node_locks"):
            updates["node_locks"] = manifest.get("node_locks")
        if manifest.get("node_mappings"):
            updates["node_mappings"] = manifest.get("node_mappings")
        # Asset positions reference the packaged workflow; apply them only when
        # the manifest still matches the JSON it shipped with.
        if hash_matched:
            if manifest.get("extracted_assets") is not None:
                updates["extracted_assets"] = manifest.get("extracted_assets")
            if manifest.get("extra_assets"):
                updates["extra_assets"] = manifest.get("extra_assets")
        result = self.update_comfyui_workflow(uploaded["id"], updates) if updates else uploaded
        self.audit(
            "comfyui_workflow",
            uploaded["id"],
            "imported",
            "ComfyUI workflow package imported",
            {"name": result.get("name"), "hash_matched": hash_matched},
        )
        result = dict(result)
        result["imported"] = True
        result["package_hash_matched"] = hash_matched
        return result

    def _shareable_asset(self, asset: dict[str, Any]) -> dict[str, Any]:
        row = {key: value for key, value in asset.items() if key not in {"cache_hit"}}
        if row.get("url"):
            row["url"] = redact_url(canonical_asset_url(str(row["url"])))
        return row

    def analyze_saved_comfyui_workflow(self, workflow_id: str) -> dict[str, Any]:
        workflow = self.get_comfyui_workflow(workflow_id)
        if not workflow:
            raise KeyError(workflow_id)
        updated = self.update_comfyui_workflow(workflow_id, {})
        return updated.get("analysis") or {}

    def resolve_comfyui_workflow_node(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        workflow = self.get_comfyui_workflow(workflow_id)
        if not workflow:
            raise KeyError(workflow_id)
        node_type = str(payload.get("class_type") or payload.get("node_type") or "").strip()
        node_types = payload.get("node_types") or ([node_type] if node_type else [])
        node_types = [str(item).strip() for item in node_types if str(item).strip()]
        if not node_types:
            raise ValueError("node_type is required")
        decision = str(payload.get("decision") or "install_git_repo").strip()
        locks = list(workflow.get("node_locks") or [])
        locks = [lock for lock in locks if not set(lock.get("node_types") or []) & set(node_types)]
        now = utc_iso()
        if decision == "use_registry":
            registry_node = self._resolve_comfyui_registry_node(node_types[0])
            if not registry_node:
                raise ValueError("registry_result_not_found")
            lock = dict(registry_node)
            lock["decision"] = "use_registry"
            lock["requested_ref"] = str(payload.get("requested_ref") or lock.get("registry_version") or "").strip()
            if lock.get("repo_url"):
                lock["locked_ref"] = str(payload.get("locked_ref") or self._resolve_git_ref(lock["repo_url"], payload.get("requested_ref") or "")).strip()
                lock["ref"] = lock["locked_ref"]
            lock["node_types"] = sorted(set(node_types) | set(lock.get("node_types") or []))
            lock["locked_at"] = now
            locks.append(lock)
        elif decision == "install_git_repo":
            repo_url = str(payload.get("repo_url") or "").strip()
            if not repo_url:
                raise ValueError("repo_url is required")
            package = str(payload.get("package") or self._package_from_repo_url(repo_url)).strip()
            requested_ref = str(payload.get("requested_ref") or payload.get("ref") or "").strip()
            locked_ref = str(payload.get("locked_ref") or self._resolve_git_ref(repo_url, requested_ref)).strip()
            locks.append(
                {
                    "decision": "install_git_repo",
                    "package": package,
                    "repo_url": repo_url,
                    "requested_ref": requested_ref,
                    "locked_ref": locked_ref,
                    "ref": locked_ref,
                    "install_method": "git_clone",
                    "node_types": node_types,
                    "source": "manual_repo_mapping",
                    "locked_at": now,
                }
            )
        elif decision == "treat_builtin":
            locks.append({"decision": "treat_builtin", "node_types": node_types, "source": "user_override", "locked_at": now})
        elif decision == "ignore_non_executable":
            locks.append({"decision": "ignore_non_executable", "node_types": node_types, "source": "user_override", "locked_at": now})
        else:
            raise ValueError(f"unsupported_node_decision:{decision}")
        return self.update_comfyui_workflow(workflow_id, {"node_locks": locks})

    def probe_comfyui_workflow(self, workflow_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        row = self.db.get("comfyui_workflows", workflow_id)
        if not row or row.get("status") == "deleted":
            raise KeyError(workflow_id)
        public = self._workflow_public(row)
        analysis = public.get("analysis") or {}
        if not analysis.get("ok"):
            return {"ok": False, "state": "failed", "error": "unresolved_custom_nodes", "analysis": analysis}
        fingerprint = str(row["dependency_fingerprint"])
        if not payload.get("force_refresh"):
            cached = self._latest_passed_probe(fingerprint)
            if cached:
                result = self._probe_public(cached) or {}
                result["ok"] = True
                result["cached"] = True
                return result
        return self._run_dependency_probe_for_workflow(row, public)

    def _build_workflow_values(self, payload: dict[str, Any], *, workflow_id: str, created_at: str) -> dict[str, Any]:
        product = str(payload.get("product") or self.settings.product)
        workflow = normalize_workflow_json(payload.get("workflow") or payload.get("canonical_workflow_json") or payload.get("ui_workflow") or payload.get("ui_workflow_json"))
        if workflow is None:
            raise ValueError("workflow JSON is required")
        digest = workflow_hash(workflow)
        node_locks = payload.get("node_locks") or []
        installable_locks = self._installable_node_locks(node_locks)
        builtin_overrides = self._node_types_by_decision(node_locks, "treat_builtin")
        ignored_node_types = self._node_types_by_decision(node_locks, "ignore_non_executable")
        analysis = analyze_comfyui_workflow(
            {
                "ui_workflow_json": workflow,
                "custom_nodes": installable_locks,
                "builtin_overrides": builtin_overrides,
                "ignored_node_types": ignored_node_types,
            },
            registry_resolver=self._comfyui_registry_resolver(),
        )
        extracted_assets = payload.get("extracted_assets")
        if extracted_assets is None:
            extracted_assets = extract_model_requirements(workflow)
        extracted_assets = self._canonicalize_workflow_asset_rows(extracted_assets)
        extra_assets = normalize_asset_manifest(payload.get("extra_assets") if payload.get("extra_assets") is not None else payload.get("assets") or [])
        extra_assets = self._canonicalize_workflow_asset_rows(extra_assets)
        base_lock = self._base_template_lock_payload()
        dep_fp = dependency_fingerprint(product=product, workflow_hash_value=digest, node_locks=node_locks, base_template=base_lock)
        launch_assets = self._workflow_launch_assets_from_parts(extracted_assets, extra_assets)
        launch_fp = workflow_launch_fingerprint(
            dependency_fingerprint_value=dep_fp,
            assets=launch_assets,
            launch_settings={"model_asset_count": len(launch_assets)},
        )
        status = self._workflow_status(analysis, extracted_assets, extra_assets)
        now = utc_iso()
        return {
            "id": workflow_id,
            "product": product,
            "name": str(payload.get("name") or f"workflow-{digest[:10]}").strip(),
            "workflow_hash": digest,
            "canonical_workflow_json": json_dumps(workflow),
            "original_filename": str(payload.get("original_filename") or payload.get("filename") or "").strip() or None,
            "analysis_json": json_dumps(analysis),
            "extracted_assets_json": json_dumps(extracted_assets or []),
            "extra_assets_json": json_dumps(extra_assets),
            "node_mappings_json": json_dumps(payload.get("node_mappings") or {}),
            "node_locks_json": json_dumps(node_locks),
            "install_plan_json": json_dumps(analysis.get("install_plan") or {"version": 1, "steps": []}),
            "validation_plan_json": json_dumps(analysis.get("validation_plan") or {}),
            "base_template_lock_json": json_dumps(base_lock),
            "dependency_fingerprint": dep_fp,
            "launch_fingerprint": launch_fp,
            "status": status,
            "verification_state": str(payload.get("verification_state") or "unverified"),
            "last_probe_id": payload.get("last_probe_id"),
            "last_live_verified_session_id": payload.get("last_live_verified_session_id"),
            "last_verified_output_path": payload.get("last_verified_output_path"),
            "verified_at": payload.get("verified_at"),
            "created_at": created_at,
            "updated_at": now,
        }

    def _workflow_status(self, analysis: dict[str, Any], extracted_assets: list[dict[str, Any]], extra_assets: list[dict[str, Any]]) -> str:
        if not analysis.get("ok"):
            return "needs_node_mapping"
        assets = self._workflow_launch_assets_from_parts(extracted_assets, extra_assets, include_incomplete=True)
        if any(not asset.get("url") for asset in assets):
            return "needs_model_urls"
        if any(asset.get("size_bytes") is None for asset in assets):
            return "needs_model_metadata"
        return "ready_to_probe"

    def _workflow_public(self, row: dict[str, Any]) -> dict[str, Any]:
        public = self._expand_json_fields(
            row,
            [
                "canonical_workflow_json",
                "analysis_json",
                "extracted_assets_json",
                "extra_assets_json",
                "node_mappings_json",
                "node_locks_json",
                "install_plan_json",
                "validation_plan_json",
                "base_template_lock_json",
            ],
        )
        public["workflow"] = public.pop("canonical_workflow", {})
        public["analysis"] = public.pop("analysis", {})
        public["extracted_assets"] = self._canonicalize_workflow_asset_rows(public.pop("extracted_assets", []))
        public["extra_assets"] = self._canonicalize_workflow_asset_rows(public.pop("extra_assets", []))
        public["node_mappings"] = public.pop("node_mappings", {})
        public["node_locks"] = public.pop("node_locks", [])
        public["install_plan"] = public.pop("install_plan", {"version": 1, "steps": []})
        public["validation_plan"] = public.pop("validation_plan", {})
        public["base_template_lock"] = public.pop("base_template_lock", {})
        public["assets"] = self._workflow_launch_assets(public, include_incomplete=True)
        public["ready_assets"] = self._workflow_launch_assets(public, include_incomplete=False)
        public["hash_prefix"] = str(public.get("workflow_hash") or "")[:12]
        if public.get("last_probe_id"):
            public["last_probe_result"] = self.get_comfyui_probe(str(public["last_probe_id"]))
        else:
            public["last_probe_result"] = None
        return public

    def _deleted_workflow_values(self) -> dict[str, Any]:
        return {
            "analysis_json": json_dumps({}),
            "extracted_assets_json": json_dumps([]),
            "extra_assets_json": json_dumps([]),
            "node_mappings_json": json_dumps({}),
            "node_locks_json": json_dumps([]),
            "install_plan_json": json_dumps({"version": 1, "steps": []}),
            "validation_plan_json": json_dumps({}),
            "base_template_lock_json": json_dumps({}),
            "dependency_fingerprint": "",
            "launch_fingerprint": "",
            "status": "deleted",
            "verification_state": "unverified",
            "last_probe_id": None,
            "last_live_verified_session_id": None,
            "last_verified_output_path": None,
            "verified_at": None,
            "updated_at": utc_iso(),
        }

    def _workflow_launch_assets(self, workflow: dict[str, Any], *, include_incomplete: bool) -> list[dict[str, Any]]:
        return self._workflow_launch_assets_from_parts(
            workflow.get("extracted_assets") or [],
            workflow.get("extra_assets") or [],
            include_incomplete=include_incomplete,
        )

    def _workflow_launch_assets_from_parts(
        self,
        extracted_assets: list[dict[str, Any]],
        extra_assets: list[dict[str, Any]],
        *,
        include_incomplete: bool = False,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for asset in [*(extracted_assets or []), *(extra_assets or [])]:
            if asset.get("status") in {"removed", "replaced"}:
                continue
            if not include_incomplete and (not asset.get("url") or asset.get("size_bytes") is None):
                continue
            rows.append(asset)
        return rows

    def _workflow_asset_needs_metadata_refresh(self, asset: dict[str, Any]) -> bool:
        url = str(asset.get("url") or "")
        if not url:
            return False
        provider = str(asset.get("provider") or detect_provider({"url": url}))
        if provider != "huggingface":
            return False
        filename = str(asset.get("filename") or url.rsplit("/", 1)[-1] or "").split("?", 1)[0].lower()
        model_exts = (".safetensors", ".gguf", ".pth", ".pt", ".bin", ".ckpt")
        if "/blob/" in url:
            return True
        if not filename.endswith(model_exts):
            return False
        size = asset.get("size_bytes")
        try:
            size_int = int(size) if size is not None else None
        except (TypeError, ValueError):
            return True
        return size_int is not None and size_int < 128 * 1024

    def _refresh_suspicious_workflow_assets(self, workflow: dict[str, Any]) -> dict[str, Any]:
        extracted_assets = [dict(asset) for asset in (workflow.get("extracted_assets") or [])]
        extra_assets = [dict(asset) for asset in (workflow.get("extra_assets") or [])]
        changed = False
        refresh_errors: list[str] = []
        for asset in [*extracted_assets, *extra_assets]:
            if asset.get("status") in {"removed", "replaced"}:
                continue
            if not self._workflow_asset_needs_metadata_refresh(asset):
                continue
            url = str(asset.get("url") or "")
            model_folder = str(asset.get("model_folder") or "checkpoints")
            metadata = self.peek_asset({"url": url, "model_folder": model_folder, "force_refresh": True})
            if metadata.get("error") or metadata.get("size_bytes") is None:
                refresh_errors.append(f"{asset.get('filename') or url}:{metadata.get('error') or 'size_unknown'}")
                continue
            asset["url"] = metadata.get("download_url_redacted") or redact_url(canonical_asset_url(url))
            asset["provider"] = metadata.get("provider") or "huggingface"
            asset["filename"] = metadata.get("filename") or asset.get("filename")
            asset["size_bytes"] = metadata.get("size_bytes")
            asset["size_unknown"] = bool(metadata.get("size_unknown"))
            asset["model_folder"] = metadata.get("model_folder") or model_folder
            asset["target"] = metadata.get("target") or target_for(model_folder, asset.get("filename") or "asset")
            asset["status"] = "ready"
            asset["cache_hit"] = bool(metadata.get("cache_hit"))
            changed = True
        if refresh_errors:
            self.audit(
                "comfyui_workflow",
                workflow["id"],
                "asset_metadata_refresh_failed",
                "Suspicious Hugging Face asset metadata refresh failed",
                {"errors": refresh_errors},
            )
        if not changed:
            return workflow
        return self.update_comfyui_workflow(workflow["id"], {"extracted_assets": extracted_assets, "extra_assets": extra_assets})

    def _canonicalize_workflow_asset_rows(self, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for asset in assets or []:
            if not isinstance(asset, dict):
                continue
            row = dict(asset)
            if row.get("url"):
                canonical_url = canonical_asset_url(str(row.get("url") or ""))
                row["url"] = redact_url(canonical_url)
                row["provider"] = row.get("provider") or detect_provider({"url": canonical_url})
            rows.append(row)
        return rows

    def _installable_node_locks(self, node_locks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            lock
            for lock in (node_locks or [])
            if lock.get("decision") in {"install_git_repo", "use_registry"} or lock.get("repo_url")
        ]

    def _node_types_by_decision(self, node_locks: list[dict[str, Any]], decision: str) -> list[str]:
        return sorted(
            {
                str(node_type)
                for lock in (node_locks or [])
                if lock.get("decision") == decision
                for node_type in (lock.get("node_types") or [])
            }
        )

    def _base_template_lock_payload(self) -> dict[str, Any]:
        payload = self._base_template_fingerprint_payload()
        payload["image_digest"] = "digest_unknown"
        payload["locked_at"] = utc_iso()
        return payload

    def _resolve_git_ref(self, repo_url: str, requested_ref: str | None = None) -> str:
        requested = str(requested_ref or "").strip()
        patterns = ["HEAD"] if not requested else [requested, f"refs/heads/{requested}", f"refs/tags/{requested}"]
        result = subprocess.run(
            ["git", "ls-remote", repo_url, *patterns],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError("git_ref_resolution_failed:" + (result.stderr.strip() or str(result.returncode)))
        for line in result.stdout.splitlines():
            sha = line.split(None, 1)[0].strip()
            if len(sha) >= 40:
                return sha
        raise RuntimeError("git_ref_resolution_failed:no_matching_ref")

    def _package_from_repo_url(self, repo_url: str) -> str:
        package = repo_url.rstrip("/").rsplit("/", 1)[-1]
        if package.endswith(".git"):
            package = package[:-4]
        return package or "custom-node"

    def _migrate_legacy_launch_templates(self, product: str) -> None:
        rows = self.db.query("SELECT * FROM comfyui_launch_templates WHERE product = ?", (product,))
        for row in rows:
            workflow_json = json_loads(row.get("ui_workflow_json"), None)
            if workflow_json is None:
                continue
            digest = workflow_hash(workflow_json)
            exists = self.db.query("SELECT id FROM comfyui_workflows WHERE product = ? AND workflow_hash = ? LIMIT 1", (product, digest))
            if exists:
                continue
            workflow_id = "cw_" + sha256_text(f"legacy:{row['id']}")[:20]
            try:
                values = self._build_workflow_values(
                    {
                        "product": product,
                        "name": row.get("name") or f"workflow-{digest[:10]}",
                        "workflow": workflow_json,
                        "extra_assets": json_loads(row.get("assets_json"), []),
                        "node_locks": [
                            {**lock, "decision": "install_git_repo", "locked_ref": lock.get("ref") or lock.get("locked_ref") or ""}
                            for lock in json_loads(row.get("custom_nodes_json"), [])
                        ],
                        "last_probe_id": row.get("last_probe_id"),
                    },
                    workflow_id=workflow_id,
                    created_at=row.get("created_at") or utc_iso(),
                )
                self.db.insert("comfyui_workflows", values)
            except Exception:  # noqa: BLE001
                continue

    def list_comfyui_launch_templates(self, product: str | None = None) -> list[dict[str, Any]]:
        product = product or self.settings.product
        rows = self.db.query("SELECT * FROM comfyui_launch_templates WHERE product = ? ORDER BY name", (product,))
        return [self._launch_template_public(row) for row in rows]

    def get_comfyui_launch_template(self, template_id: str) -> dict[str, Any] | None:
        row = self.db.get("comfyui_launch_templates", template_id)
        return self._launch_template_public(row) if row else None

    def create_comfyui_launch_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        template_id = new_id("clt")
        now = utc_iso()
        values = self._build_launch_template_values(payload, template_id=template_id, created_at=now)
        self.db.insert("comfyui_launch_templates", values)
        self.audit("comfyui_launch_template", template_id, "created", "ComfyUI launch template created", {"name": values["name"], "fingerprint": values["fingerprint"]})
        return self.get_comfyui_launch_template(template_id) or values

    def update_comfyui_launch_template(self, template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self.db.get("comfyui_launch_templates", template_id)
        if not existing:
            raise KeyError(template_id)
        merged = {
            "product": existing["product"],
            "name": existing["name"],
            "ui_workflow_json": json_loads(existing.get("ui_workflow_json"), {}),
            "api_workflow_json": json_loads(existing.get("api_workflow_json"), None),
            "assets": json_loads(existing.get("assets_json"), []),
            "custom_nodes": json_loads(existing.get("custom_nodes_json"), []),
        }
        merged.update(payload)
        values = self._build_launch_template_values(merged, template_id=template_id, created_at=existing["created_at"])
        values.pop("id")
        values.pop("created_at")
        self.db.update("comfyui_launch_templates", template_id, values)
        self.audit("comfyui_launch_template", template_id, "updated", "ComfyUI launch template updated", {"fingerprint": values["fingerprint"]})
        return self.get_comfyui_launch_template(template_id) or values

    def delete_comfyui_launch_template(self, template_id: str) -> dict[str, Any]:
        if not self.db.get("comfyui_launch_templates", template_id):
            raise KeyError(template_id)
        self.db.execute("DELETE FROM comfyui_launch_templates WHERE id = ?", (template_id,))
        return {"ok": True, "id": template_id}

    def probe_comfyui_launch_template(self, template_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        template = self.db.get("comfyui_launch_templates", template_id)
        if not template:
            raise KeyError(template_id)
        public_template = self._launch_template_public(template)
        analysis = public_template.get("analyzer_result") or {}
        if not analysis.get("ok"):
            return {"ok": False, "state": "failed", "error": "unresolved_custom_nodes", "analysis": analysis}
        fingerprint = str(template["fingerprint"])
        if not payload.get("force_refresh"):
            cached = self._latest_passed_probe(fingerprint)
            if cached:
                result = self._probe_public(cached) or {}
                result["ok"] = True
                result["cached"] = True
                return result
        return self._run_dependency_probe(template, public_template)

    def get_comfyui_probe(self, probe_id: str) -> dict[str, Any] | None:
        row = self.db.get("comfyui_dependency_probes", probe_id)
        return self._probe_public(row) if row else None

    def _build_launch_template_values(self, payload: dict[str, Any], *, template_id: str, created_at: str) -> dict[str, Any]:
        product = str(payload.get("product") or self.settings.product)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("template name is required")
        ui_workflow = normalize_workflow_json(payload.get("ui_workflow_json") or payload.get("workflow_json"))
        if ui_workflow is None:
            raise ValueError("ui_workflow_json is required")
        api_workflow = normalize_workflow_json(payload.get("api_workflow_json"))
        assets = normalize_asset_manifest(payload.get("assets") or [])
        custom_nodes = normalize_custom_nodes(payload.get("custom_nodes") or [])
        analysis = analyze_comfyui_workflow(
            {
                "ui_workflow_json": ui_workflow,
                "api_workflow_json": api_workflow,
                "custom_nodes": custom_nodes,
            },
            registry_resolver=self._comfyui_registry_resolver(),
        )
        install_plan = analysis["install_plan"]
        validation_plan = analysis["validation_plan"]
        fingerprint = launch_template_fingerprint(
            product=product,
            ui_workflow_json=ui_workflow,
            api_workflow_json=api_workflow,
            assets=assets,
            custom_nodes=custom_nodes,
            install_plan=install_plan,
            base_template=self._base_template_fingerprint_payload(),
        )
        now = utc_iso()
        return {
            "id": template_id,
            "product": product,
            "name": name,
            "ui_workflow_json": json_dumps(ui_workflow),
            "api_workflow_json": json_dumps(api_workflow) if api_workflow is not None else None,
            "assets_json": json_dumps(assets),
            "custom_nodes_json": json_dumps(analysis["resolved_custom_nodes"]),
            "analyzer_result_json": json_dumps(analysis),
            "install_plan_json": json_dumps(install_plan),
            "validation_plan_json": json_dumps(validation_plan),
            "fingerprint": fingerprint,
            "last_probe_id": payload.get("last_probe_id"),
            "last_probe_result_json": json_dumps(payload.get("last_probe_result")) if payload.get("last_probe_result") else None,
            "bake_candidate_json": json_dumps(payload.get("bake_candidate")) if payload.get("bake_candidate") else None,
            "created_at": created_at,
            "updated_at": now,
        }

    def _base_template_fingerprint_payload(self) -> dict[str, Any]:
        return {
            "gpu_template_id": self.settings.gpu_template_id,
            "gpu_pod_image": self.settings.gpu_pod_image,
            "cpu_pod_image": self.settings.cpu_pod_image,
            "comfyui_remote_port": self.settings.comfyui_remote_port,
        }

    def _latest_passed_probe(self, fingerprint: str) -> dict[str, Any] | None:
        rows = self.db.query(
            "SELECT * FROM comfyui_dependency_probes WHERE fingerprint = ? AND state = 'passed' ORDER BY completed_at DESC, updated_at DESC LIMIT 1",
            (fingerprint,),
        )
        return rows[0] if rows else None

    def _dependency_probe_data_centers(self) -> list[str]:
        centers = dryrun_data_centers()
        if not centers:
            centers = list(COMFYUI_CANDIDATE_DATA_CENTERS)
        default = str(self.settings.default_data_center or "")
        ordered: list[str] = []
        if default and default in centers:
            ordered.append(default)
        for center in centers:
            if center not in ordered:
                ordered.append(center)
        return ordered

    def _probe_env(self, probe_id: str, public_template: dict[str, Any]) -> dict[str, str]:
        return {
            "PROBE_ID": probe_id,
            "INSTALL_PLAN_JSON": json_dumps(public_template.get("install_plan") or {"steps": []}),
            "CUSTOM_NODES_JSON": json_dumps(public_template.get("custom_nodes") or []),
            "UI_WORKFLOW_BYTES": str(len(json_dumps(public_template.get("ui_workflow") or {}).encode("utf-8"))),
            "API_WORKFLOW_BYTES": str(len(json_dumps(public_template.get("api_workflow") or {}).encode("utf-8"))),
        }

    def _compact_probe_error(self, exc: Exception | str) -> str:
        text = str(exc)
        compact = " ".join(text.split())
        if "unmarshal to struct" in compact and "invalid character" in compact:
            compact = "runpod_create_pod_provider_parse_error"
        elif len(compact) > 500:
            compact = compact[:500] + "...(truncated)"
        return compact

    def _create_dependency_probe_resources(self, probe_id: str, public_template: dict[str, Any]) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        env = self._probe_env(probe_id, public_template)
        for data_center_id in self._dependency_probe_data_centers():
            attempt: dict[str, Any] = {
                "data_center_id": data_center_id,
                "state": "creating_volume",
                "started_at": utc_iso(),
            }
            volume_id: str | None = None
            pod_id: str | None = None
            provider_pod_id: str | None = None
            try:
                volume_payload = self.adapter.create_network_volume(
                    name=f"probe-{probe_id}",
                    size_gb=MIN_NETWORK_VOLUME_GB,
                    data_center_id=data_center_id,
                )
                volume_id = self._record_volume(
                    volume_payload,
                    f"probe-{probe_id}",
                    MIN_NETWORK_VOLUME_GB,
                    data_center_id,
                    {"retention_policy": "delete_after_probe"},
                )
                attempt.update(
                    {
                        "state": "creating_cpu_pod",
                        "volume_id": volume_id,
                        "provider_volume_id": volume_payload.get("id"),
                    }
                )
                self.db.update("comfyui_dependency_probes", probe_id, {"volume_id": volume_id, "updated_at": utc_iso()})
                cpu_payload = self.adapter.create_probe_cpu_pod(
                    name=f"probe-{probe_id}",
                    volume_provider_id=str(volume_payload.get("id")),
                    data_center_id=data_center_id,
                    env=env,
                )
                provider_pod_id = str(cpu_payload.get("id") or "")
                pod_id = self._record_pod(
                    provider_payload=cpu_payload,
                    session_id=None,
                    volume_id=volume_id,
                    role="dependency_probe_cpu",
                    compute_type="CPU",
                    data_center_id=data_center_id,
                    image=self.settings.cpu_pod_image,
                )
                attempt.update(
                    {
                        "state": "cpu_pod_created",
                        "cpu_pod_id": pod_id,
                        "provider_pod_id": provider_pod_id,
                        "updated_at": utc_iso(),
                    }
                )
                attempts.append(attempt)
                self.db.update(
                    "comfyui_dependency_probes",
                    probe_id,
                    {"cpu_pod_id": pod_id, "result_json": json_dumps({"attempts": attempts}), "updated_at": utc_iso()},
                )
                return {"data_center_id": data_center_id, "volume_id": volume_id, "pod_id": pod_id, "attempts": attempts}
            except Exception as exc:  # noqa: BLE001
                error = self._compact_probe_error(exc)
                cleanup_errors: list[str] = []
                if pod_id:
                    pod = self.db.get("pods", pod_id)
                    if pod and pod.get("state") != "deleted":
                        cleanup_error = self._delete_pod_record(pod)
                        if cleanup_error:
                            cleanup_errors.append(cleanup_error)
                elif provider_pod_id:
                    try:
                        result = redact_secrets(self.adapter.delete_pod(provider_pod_id))
                        if not result.get("ok", True):
                            cleanup_errors.append(f"probe_provider_pod_delete_failed:{provider_pod_id}:{result}")
                    except Exception as cleanup_exc:  # noqa: BLE001
                        cleanup_errors.append(self._compact_probe_error(cleanup_exc))
                if volume_id:
                    volume = self.db.get("network_volumes", volume_id)
                    if volume and volume.get("state") != "deleted":
                        cleanup_error = self._delete_volume_record(volume)
                        if cleanup_error:
                            cleanup_errors.append(cleanup_error)
                attempt.update(
                    {
                        "state": "failed",
                        "error": error,
                        "cleanup_status": "cleanup_failed" if cleanup_errors else "deleted_owned_probe_resources",
                        "cleanup_errors": cleanup_errors,
                        "updated_at": utc_iso(),
                    }
                )
                attempts.append(attempt)
                self.db.update(
                    "comfyui_dependency_probes",
                    probe_id,
                    {"result_json": json_dumps({"attempts": attempts}), "updated_at": utc_iso()},
                )
        summary = "; ".join(f"{item['data_center_id']}={item.get('error', item.get('state'))}" for item in attempts)
        raise RuntimeError(f"CPU probe Pod create failed in {len(attempts)} datacenter(s): {summary}")

    def _run_dependency_probe(self, template: dict[str, Any], public_template: dict[str, Any]) -> dict[str, Any]:
        probe_id = new_id("prb")
        now = utc_iso()
        self.db.insert(
            "comfyui_dependency_probes",
            {
                "id": probe_id,
                "template_id": template["id"],
                "product": template["product"],
                "fingerprint": template["fingerprint"],
                "state": "running",
                "volume_id": None,
                "cpu_pod_id": None,
                "result_json": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            },
        )
        volume_id = None
        pod_id = None
        try:
            probe_resources = self._create_dependency_probe_resources(probe_id, public_template)
            volume_id = probe_resources["volume_id"]
            pod_id = probe_resources["pod_id"]
            self._wait_for_cpu_hydration_pod(pod_id)
            result = self._verify_remote_probe_once(probe_id, self.db.get("network_volumes", volume_id) or {}, public_template)
            result["attempts"] = probe_resources.get("attempts") or []
            state = "passed" if result.get("ok") else "failed"
            completed_at = utc_iso()
            self.db.update("comfyui_dependency_probes", probe_id, {"state": state, "result_json": json_dumps(result), "updated_at": completed_at, "completed_at": completed_at})
            self.db.update("comfyui_launch_templates", template["id"], {"last_probe_id": probe_id, "last_probe_result_json": json_dumps(result), "updated_at": completed_at})
            self._record_bake_candidate(template["id"], public_template, result)
            probe = self.get_comfyui_probe(probe_id) or {}
            probe["ok"] = state == "passed"
            probe["cached"] = False
            return probe
        except Exception as exc:  # noqa: BLE001
            error = self._compact_probe_error(exc)
            completed_at = utc_iso()
            row = self.db.get("comfyui_dependency_probes", probe_id) or {}
            result = json_loads(row.get("result_json"), {}) or {}
            result.update({"ok": False, "error": error})
            self.db.update("comfyui_dependency_probes", probe_id, {"state": "failed", "error": error, "result_json": json_dumps(result), "updated_at": completed_at, "completed_at": completed_at})
            self.db.update("comfyui_launch_templates", template["id"], {"last_probe_id": probe_id, "last_probe_result_json": json_dumps(result), "updated_at": completed_at})
            probe = self.get_comfyui_probe(probe_id) or {}
            probe["ok"] = False
            probe["cached"] = False
            probe["error"] = error
            return probe
        finally:
            cleanup_errors = []
            if pod_id:
                pod = self.db.get("pods", pod_id)
                if pod and pod.get("state") != "deleted":
                    err = self._delete_pod_record(pod)
                    if err:
                        cleanup_errors.append(err)
            if volume_id:
                volume = self.db.get("network_volumes", volume_id)
                if volume and volume.get("state") != "deleted":
                    err = self._delete_volume_record(volume)
                    if err:
                        cleanup_errors.append(err)
            if cleanup_errors:
                row = self.db.get("comfyui_dependency_probes", probe_id) or {}
                result = json_loads(row.get("result_json"), {}) or {}
                result["cleanup_errors"] = cleanup_errors
                self.db.update("comfyui_dependency_probes", probe_id, {"result_json": json_dumps(result), "updated_at": utc_iso()})

    def _run_dependency_probe_for_workflow(self, workflow: dict[str, Any], public_workflow: dict[str, Any]) -> dict[str, Any]:
        probe_id = new_id("prb")
        now = utc_iso()
        public_template = {
            "id": workflow["id"],
            "product": workflow["product"],
            "fingerprint": workflow["dependency_fingerprint"],
            "ui_workflow": public_workflow.get("workflow") or {},
            "api_workflow": {},
            "custom_nodes": self._installable_node_locks(public_workflow.get("node_locks") or []),
            "install_plan": public_workflow.get("install_plan") or {"steps": []},
        }
        self.db.insert(
            "comfyui_dependency_probes",
            {
                "id": probe_id,
                "template_id": None,
                "workflow_id": workflow["id"],
                "product": workflow["product"],
                "fingerprint": workflow["dependency_fingerprint"],
                "state": "running",
                "volume_id": None,
                "cpu_pod_id": None,
                "result_json": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            },
        )
        volume_id = None
        pod_id = None
        try:
            probe_resources = self._create_dependency_probe_resources(probe_id, public_template)
            volume_id = probe_resources["volume_id"]
            pod_id = probe_resources["pod_id"]
            self._wait_for_cpu_hydration_pod(pod_id)
            result = self._verify_remote_probe_once(probe_id, self.db.get("network_volumes", volume_id) or {}, public_template)
            result["attempts"] = probe_resources.get("attempts") or []
            state = "passed" if result.get("ok") else "failed"
            completed_at = utc_iso()
            self.db.update("comfyui_dependency_probes", probe_id, {"state": state, "result_json": json_dumps(result), "updated_at": completed_at, "completed_at": completed_at})
            self.db.update("comfyui_workflows", workflow["id"], {"last_probe_id": probe_id, "status": "dependency_probed" if state == "passed" else workflow.get("status"), "updated_at": completed_at})
            probe = self.get_comfyui_probe(probe_id) or {}
            probe["ok"] = state == "passed"
            probe["cached"] = False
            return probe
        except Exception as exc:  # noqa: BLE001
            error = self._compact_probe_error(exc)
            completed_at = utc_iso()
            row = self.db.get("comfyui_dependency_probes", probe_id) or {}
            result = json_loads(row.get("result_json"), {}) or {}
            result.update({"ok": False, "error": error})
            self.db.update("comfyui_dependency_probes", probe_id, {"state": "failed", "error": error, "result_json": json_dumps(result), "updated_at": completed_at, "completed_at": completed_at})
            self.db.update("comfyui_workflows", workflow["id"], {"last_probe_id": probe_id, "updated_at": completed_at})
            probe = self.get_comfyui_probe(probe_id) or {}
            probe["ok"] = False
            probe["cached"] = False
            probe["error"] = error
            return probe
        finally:
            cleanup_errors = []
            if pod_id:
                pod = self.db.get("pods", pod_id)
                if pod and pod.get("state") != "deleted":
                    err = self._delete_pod_record(pod)
                    if err:
                        cleanup_errors.append(err)
            if volume_id:
                volume = self.db.get("network_volumes", volume_id)
                if volume and volume.get("state") != "deleted":
                    err = self._delete_volume_record(volume)
                    if err:
                        cleanup_errors.append(err)
            if cleanup_errors:
                row = self.db.get("comfyui_dependency_probes", probe_id) or {}
                result = json_loads(row.get("result_json"), {}) or {}
                result["cleanup_errors"] = cleanup_errors
                self.db.update("comfyui_dependency_probes", probe_id, {"result_json": json_dumps(result), "updated_at": utc_iso()})

    def _verify_remote_probe_once(self, probe_id: str, volume: dict[str, Any], public_template: dict[str, Any]) -> dict[str, Any]:
        provider_volume_id = str(volume.get("provider_volume_id") or "")
        if provider_volume_id.startswith("test-"):
            return {
                "ok": True,
                "source": "test_adapter_skip",
                "state": "passed",
                "dependency_screening_only": True,
                "install_steps": len((public_template.get("install_plan") or {}).get("steps") or []),
            }
        if not has_s3_credentials():
            raise RuntimeError("dependency_probe_verification_failed:missing_runpod_s3_credentials")
        client = RunpodS3VolumeClient(data_center_id=str(volume["data_center_id"]), volume_id=provider_volume_id)
        prefix = f"runpod-controller/probes/{probe_id}/"
        objects = client.list_objects(prefix)
        keys = {item.key for item in objects}
        required = {prefix + "PROBE.json", prefix + "INSTALL_PLAN.json", prefix + "DONE.json", prefix + "checksums.sha256"}
        missing = sorted(required - keys)
        if missing:
            raise RuntimeError("dependency_probe_verification_failed:missing:" + ",".join(missing))
        return {
            "ok": True,
            "source": "runpod_s3",
            "state": "passed",
            "dependency_screening_only": True,
            "objects": summarize_objects([item for item in objects if item.key in required]),
        }

    def _record_bake_candidate(self, template_id: str, public_template: dict[str, Any], probe_result: dict[str, Any]) -> None:
        if not probe_result.get("ok"):
            return
        bake_candidate = {
            "state": "candidate_recorded",
            "deferred": True,
            "reason": "custom_image_baking_deferred_until_repeated_successes",
            "fingerprint": public_template.get("fingerprint"),
            "custom_nodes": public_template.get("custom_nodes") or [],
            "install_plan": public_template.get("install_plan") or {},
            "recorded_at": utc_iso(),
        }
        self.db.update("comfyui_launch_templates", template_id, {"bake_candidate_json": json_dumps(bake_candidate), "updated_at": utc_iso()})

    def _launch_template_public(self, row: dict[str, Any]) -> dict[str, Any]:
        public = self._expand_json_fields(
            row,
            [
                "ui_workflow_json",
                "api_workflow_json",
                "assets_json",
                "custom_nodes_json",
                "analyzer_result_json",
                "install_plan_json",
                "validation_plan_json",
                "last_probe_result_json",
                "bake_candidate_json",
            ],
        )
        public["ui_workflow"] = public.pop("ui_workflow", {})
        public["api_workflow"] = public.pop("api_workflow", None)
        public["assets"] = public.pop("assets", [])
        public["custom_nodes"] = public.pop("custom_nodes", [])
        public["analyzer_result"] = public.pop("analyzer_result", {})
        public["install_plan"] = public.pop("install_plan", {"steps": []})
        public["validation_plan"] = public.pop("validation_plan", {})
        public["last_probe_result"] = public.pop("last_probe_result", None)
        public["bake_candidate"] = public.pop("bake_candidate", None)
        return public

    def _probe_public(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        return self._expand_json_fields(row, ["result_json"])

    def _process_workflow_resource_request(self, request_id: str) -> None:
        request = self.db.get("resource_requests", request_id)
        if not request:
            return
        payload = json_loads(request["requested_json"], {})
        self.db.update("resource_requests", request_id, {"state": "workflow_analyzing", "updated_at": utc_iso()})
        product = str(payload.get("product") or self.settings.product)
        try:
            launch_context = self._resolve_launch_context(payload, request_id=request_id)
        except ValueError as exc:
            self.db.update("resource_requests", request_id, {"state": "failed", "error": str(exc), "updated_at": utc_iso()})
            return
        self.db.update("resource_requests", request_id, {"state": "planning", "updated_at": utc_iso()})
        assets = launch_context["assets"]
        duplicate_keys = duplicate_url_keys(assets)
        if duplicate_keys:
            reason = "duplicate_asset_url"
            self.db.update("resource_requests", request_id, {"state": "failed", "error": reason, "updated_at": utc_iso()})
            return
        if assets:
            missing = [asset for asset in assets if asset.get("size_bytes") is None]
            if missing:
                reason = "asset_size_unknown"
                self.db.update("resource_requests", request_id, {"state": "failed", "error": reason, "updated_at": utc_iso()})
                return
            size_gb = max(MIN_NETWORK_VOLUME_GB, int(payload.get("network_volume_size_gb") or volume_size_gb_for_assets(assets)))
        else:
            size_gb = max(MIN_NETWORK_VOLUME_GB, int(payload.get("network_volume_size_gb") or self.settings.default_volume_size_gb))
        max_rate = float(payload.get("max_gpu_usd_per_hr") or self.settings.default_max_gpu_usd_per_hr)
        lease_minutes = int(payload.get("lease_minutes") or self.settings.default_lease_minutes)
        max_total = float(payload.get("max_total_usd") or self.settings.default_max_total_usd)
        min_vram_gb = normalize_min_vram_gb(payload.get("min_vram_gb") or self.settings.default_min_vram_gb)
        gpu_vendor = normalize_gpu_vendor(payload.get("gpu_vendor") or self.settings.default_gpu_vendor)
        if gpu_vendor != "NVIDIA":
            reason = f"unsupported_gpu_vendor:{gpu_vendor}"
            self.db.update("resource_requests", request_id, {"state": "failed", "error": reason, "updated_at": utc_iso()})
            return
        if max_total > 0 and max_rate > 0 and (max_rate * lease_minutes / 60.0) > max_total:
            reason = "estimated_gpu_runtime_exceeds_max_total"
            self.db.update("resource_requests", request_id, {"state": "failed", "error": reason, "updated_at": utc_iso()})
            return
        candidates = self._candidate_datacenters(payload, max_rate=max_rate, min_vram_gb=min_vram_gb, gpu_vendor=gpu_vendor)
        if not candidates:
            reason = "no_candidate_datacenters"
            self.db.update("resource_requests", request_id, {"state": "failed", "error": reason, "updated_at": utc_iso()})
            return
        budget = self._workflow_budget_estimate(candidates=candidates, size_gb=size_gb, max_rate=max_rate, lease_minutes=lease_minutes)
        if max_total > 0 and budget["estimated_total_usd"] > max_total:
            reason = "estimated_workflow_exceeds_max_total"
            self.db.update(
                "resource_requests",
                request_id,
                {
                    "state": "failed",
                    "error": reason,
                    "result_json": json_dumps({"budget": budget, "max_total_usd": max_total}),
                    "updated_at": utc_iso(),
                },
            )
            return

        session_id = new_id("ses")
        now_dt = utc_now()
        lease_until = (now_dt + dt.timedelta(minutes=lease_minutes)).isoformat()
        # No time-based hard cap: spend is bounded by max_total_usd in watchdog_tick.
        hard_terminate_at = None
        idle_shutdown_at = (now_dt + dt.timedelta(minutes=self.settings.idle_shutdown_minutes)).isoformat()
        reclaim_warning_at = (parse_iso(idle_shutdown_at) - dt.timedelta(minutes=self.settings.reclaim_warning_minutes)).isoformat()
        now = utc_iso()
        session = {
            "id": session_id,
            "request_id": request_id,
            "product": product,
            "mode": str(payload.get("mode") or "interactive"),
            "state": "dependency_probe_passed" if launch_context.get("probe_result") else "hydrating_all",
            "phase": "dependency_probe_passed" if launch_context.get("probe_result") else "hydrating_all",
            "data_center_id": candidates[0]["data_center_id"],
            "min_vram_gb": min_vram_gb,
            "gpu_vendor": gpu_vendor,
            "max_gpu_usd_per_hr": max_rate,
            "max_total_usd": max_total,
            "lease_until": lease_until,
            "hard_terminate_at": hard_terminate_at,
            "idle_shutdown_at": idle_shutdown_at,
            "reclaim_warning_at": reclaim_warning_at,
            "watchdog_paused": 0,
            "watchdog_last_checked_at": None,
            "watchdog_last_reason": None,
            "missing_finalization_reason": None,
            "ui_url": None,
            "network_volume_id": None,
            "hydration_id": None,
            "cpu_pod_id": None,
            "gpu_pod_id": None,
            "estimated_cost_usd": 0,
            "actual_cost_usd": None,
            "actual_cost_observed_at": None,
            "billed_start_at": None,
            "billed_end_at": None,
            "retention_policy": str(payload.get("retention_policy") or "delete_after_collection"),
            "created_at": now,
            "updated_at": now,
        }
        self.db.insert("sessions", session)
        workflow_id = new_id("wf")
        excluded = payload.get("excluded_data_centers") or []
        self.db.insert(
            "session_workflows",
            {
                "id": workflow_id,
                "session_id": session_id,
                "state": "hydrating_all",
                "winner_candidate_id": None,
                "launch_template_id": launch_context.get("launch_template_id"),
                "comfyui_workflow_id": launch_context.get("comfyui_workflow_id"),
                "dependency_fingerprint": launch_context.get("dependency_fingerprint"),
                "launch_fingerprint": launch_context.get("launch_fingerprint"),
                "selected_data_centers_json": json_dumps([item["data_center_id"] for item in candidates]),
                "excluded_data_centers_json": json_dumps(excluded),
                "assets_json": json_dumps(assets),
                "ui_workflow_json": json_dumps(launch_context.get("ui_workflow")) if launch_context.get("ui_workflow") is not None else None,
                "api_workflow_json": json_dumps(launch_context.get("api_workflow")) if launch_context.get("api_workflow") is not None else None,
                "analyzer_result_json": json_dumps(launch_context.get("analyzer_result") or {}),
                "probe_id": launch_context.get("probe_id"),
                "probe_result_json": json_dumps(launch_context.get("probe_result") or {}),
                "custom_nodes_json": json_dumps(launch_context.get("custom_nodes") or []),
                "install_plan_json": json_dumps(launch_context.get("install_plan") or {"steps": []}),
                "validation_plan_json": json_dumps(launch_context.get("validation_plan") or {}),
                "volume_size_gb": size_gb,
                "min_vram_gb": min_vram_gb,
                "gpu_vendor": gpu_vendor,
                "max_gpu_usd_per_hr": max_rate,
                "max_total_usd": max_total,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            },
        )
        self._workflow_event(workflow_id, session_id, None, "candidates_selected", f"Selected {len(candidates)} datacenter candidates", {"candidates": candidates, "budget": budget})
        if launch_context.get("analyzer_result"):
            self._workflow_event(workflow_id, session_id, None, "workflow_analyzed", "Workflow dependencies analyzed", launch_context["analyzer_result"])
        if launch_context.get("probe_result"):
            self._workflow_event(workflow_id, session_id, None, "dependency_probe_passed", "Cached or live CPU dependency probe passed", launch_context["probe_result"])
        candidate_ids = [self._create_candidate(workflow_id, session_id, candidate) for candidate in candidates]
        warm_volume = self._claim_warm_volume(assets, size_gb, [str(c.get("data_center_id")) for c in candidates])
        if warm_volume:
            for candidate_id in candidate_ids:
                row = self.db.get("workflow_candidates", candidate_id)
                if row and str(row.get("data_center_id")) == str(warm_volume.get("data_center_id")):
                    self._update_candidate(candidate_id, {"volume_id": warm_volume["id"]})
                    self._workflow_event(
                        workflow_id,
                        session_id,
                        candidate_id,
                        "warm_volume_reused",
                        f"{warm_volume.get('data_center_id')} reuses a warm hydrated volume; CPU hydration skipped",
                        {"volume_id": warm_volume["id"], "size_gb": warm_volume.get("size_gb")},
                    )
                    break
        self.db.update(
            "resource_requests",
            request_id,
            {
                "state": "running_workflow",
                "session_id": session_id,
                "poll_after_seconds": 5,
                "result_json": json_dumps({"session_id": session_id, "workflow_id": workflow_id}),
                "updated_at": utc_iso(),
            },
        )
        self._start_workflow_thread(request_id, workflow_id, session_id, candidate_ids, assets, size_gb, launch_context)

    def _resolve_launch_context(self, payload: dict[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
        context = {
            "launch_template_id": None,
            "comfyui_workflow_id": None,
            "dependency_fingerprint": None,
            "launch_fingerprint": None,
            "ui_workflow": None,
            "api_workflow": None,
            "analyzer_result": {},
            "probe_id": None,
            "probe_result": None,
            "custom_nodes": [],
            "install_plan": {"version": 1, "steps": []},
            "validation_plan": {},
            "assets": normalize_asset_manifest(payload.get("assets") or []),
        }
        # Accept the field name the API returns for workflow records as an alias;
        # silently ignoring it launched paid sessions without any assets.
        workflow_id = str(payload.get("workflow_id") or payload.get("comfyui_workflow_id") or "").strip()
        if workflow_id:
            workflow = self.get_comfyui_workflow(workflow_id)
            if not workflow:
                raise ValueError(f"unknown_workflow:{workflow_id}")
            workflow = self._refresh_suspicious_workflow_assets(workflow)
            analysis = workflow.get("analysis") or {}
            if not analysis.get("ok"):
                raise ValueError("unresolved_custom_nodes")
            assets = normalize_asset_manifest(workflow.get("ready_assets") or [])
            incomplete = self._workflow_launch_assets(workflow, include_incomplete=True)
            if len(assets) != len(incomplete):
                raise ValueError("workflow_assets_incomplete")
            if request_id:
                self.db.update("resource_requests", request_id, {"state": "dependency_probe_pending", "updated_at": utc_iso()})
            probe = self.probe_comfyui_workflow(workflow_id)
            if not probe.get("ok"):
                raise ValueError("dependency_probe_failed:" + str(probe.get("error") or "unknown"))
            if request_id:
                self.db.update("resource_requests", request_id, {"state": "dependency_probe_passed", "result_json": json_dumps({"probe": probe}), "updated_at": utc_iso()})
            ui_workflow, reference_rewrites = rewrite_workflow_model_references(
                workflow.get("workflow"),
                self._workflow_launch_assets(workflow, include_incomplete=True),
            )
            if reference_rewrites:
                self.audit(
                    "comfyui_workflow",
                    workflow_id,
                    "model_references_rewritten",
                    "Workflow model references rewritten to match resolved assets",
                    {"changes": reference_rewrites},
                )
            return {
                "launch_template_id": None,
                "comfyui_workflow_id": workflow_id,
                "dependency_fingerprint": workflow.get("dependency_fingerprint"),
                "launch_fingerprint": workflow.get("launch_fingerprint"),
                "ui_workflow": ui_workflow,
                "api_workflow": None,
                "analyzer_result": analysis,
                "probe_id": probe.get("id") or (probe.get("probe") or {}).get("id"),
                "probe_result": probe,
                "custom_nodes": self._installable_node_locks(workflow.get("node_locks") or []),
                "install_plan": workflow.get("install_plan") or {"version": 1, "steps": []},
                "validation_plan": workflow.get("validation_plan") or {},
                "assets": assets,
            }
        template_id = str(payload.get("launch_template_id") or "").strip()
        if template_id:
            template = self.db.get("comfyui_launch_templates", template_id)
            if not template:
                raise ValueError(f"unknown_launch_template:{template_id}")
            public_template = self._launch_template_public(template)
            analysis = public_template.get("analyzer_result") or {}
            if not analysis.get("ok"):
                raise ValueError("unresolved_custom_nodes")
            if request_id:
                self.db.update("resource_requests", request_id, {"state": "dependency_probe_pending", "updated_at": utc_iso()})
            probe = self.probe_comfyui_launch_template(template_id)
            if not probe.get("ok"):
                raise ValueError("dependency_probe_failed:" + str(probe.get("error") or (probe.get("probe") or {}).get("error") or "unknown"))
            if request_id:
                self.db.update("resource_requests", request_id, {"state": "dependency_probe_passed", "result_json": json_dumps({"probe": probe}), "updated_at": utc_iso()})
            return {
                "launch_template_id": template_id,
                "ui_workflow": public_template.get("ui_workflow"),
                "api_workflow": public_template.get("api_workflow"),
                "analyzer_result": analysis,
                "probe_id": probe.get("id") or (probe.get("probe") or {}).get("id"),
                "probe_result": probe,
                "custom_nodes": public_template.get("custom_nodes") or [],
                "install_plan": public_template.get("install_plan") or {"version": 1, "steps": []},
                "validation_plan": public_template.get("validation_plan") or {},
                "assets": normalize_asset_manifest(public_template.get("assets") or []),
            }
        if payload.get("ui_workflow_json") or payload.get("workflow_json") or payload.get("api_workflow_json"):
            analysis = analyze_comfyui_workflow(payload, registry_resolver=self._comfyui_registry_resolver())
            if not analysis.get("ok"):
                raise ValueError("unresolved_custom_nodes")
            context.update(
                {
                    "ui_workflow": normalize_workflow_json(payload.get("ui_workflow_json") or payload.get("workflow_json")),
                    "api_workflow": normalize_workflow_json(payload.get("api_workflow_json")),
                    "analyzer_result": analysis,
                    "custom_nodes": analysis.get("resolved_custom_nodes") or [],
                    "install_plan": analysis.get("install_plan") or {"version": 1, "steps": []},
                    "validation_plan": analysis.get("validation_plan") or {},
                }
            )
        return context

    def _candidate_seed_datacenters(self, payload: dict[str, Any]) -> list[str]:
        raw = payload.get("data_centers") or payload.get("candidate_data_centers")
        if isinstance(raw, str):
            data_centers = [item.strip() for item in raw.split(",") if item.strip()]
        elif isinstance(raw, list) and raw:
            data_centers = [str(item) for item in raw if str(item).strip()]
        elif payload.get("data_center_id"):
            data_centers = [str(payload["data_center_id"])]
        else:
            data_centers = dryrun_data_centers()
        excluded = {str(item) for item in (payload.get("excluded_data_centers") or [])}
        unique = []
        seen = set()
        for dc in data_centers:
            if dc in excluded or dc in seen or dc not in COMFYUI_CANDIDATE_DATA_CENTERS:
                continue
            seen.add(dc)
            unique.append(dc)
        return unique

    def _candidate_datacenters(self, payload: dict[str, Any], *, max_rate: float, min_vram_gb: int | None = None, gpu_vendor: str | None = None) -> list[dict[str, Any]]:
        min_vram_gb = normalize_min_vram_gb(min_vram_gb or payload.get("min_vram_gb") or self.settings.default_min_vram_gb)
        gpu_vendor = normalize_gpu_vendor(gpu_vendor or payload.get("gpu_vendor") or self.settings.default_gpu_vendor)
        data_centers = self._candidate_seed_datacenters(payload)
        gpu_rows = comfyui_gpu_rows(min_vram_gb=min_vram_gb, gpu_vendor=gpu_vendor, max_usd_per_hr=max_rate)
        matrix = self.adapter.scout_gpu_matrix(
            {
                "product": str(payload.get("product") or self.settings.product),
                "data_centers": data_centers,
                "gpu_rows": gpu_rows,
                "min_vram_gb": min_vram_gb,
                "gpu_vendor": gpu_vendor,
                "max_gpu_usd_per_hr": max_rate,
            }
        )
        if matrix.get("ok"):
            by_dc: dict[str, list[dict[str, Any]]] = {}
            for candidate in matrix.get("candidates") or []:
                rate = candidate.get("quoted_cost_usd_per_hr")
                if rate is None:
                    rate = candidate.get("estimated_cost_usd_per_hr")
                if max_rate > 0 and rate is not None and float(rate) > max_rate:
                    continue
                dc = str(candidate.get("data_center_id") or "")
                if not dc:
                    continue
                by_dc.setdefault(dc, []).append(candidate)
            selected = []
            for dc in data_centers:
                options = by_dc.get(dc) or []
                if not options:
                    continue
                best = sorted(
                    options,
                    key=lambda item: (
                        0 if item.get("eligible") else 1,
                        float(item.get("quoted_cost_usd_per_hr") if item.get("quoted_cost_usd_per_hr") is not None else item.get("estimated_cost_usd_per_hr") or 1_000_000.0),
                        int(item.get("vram_gb") or 0),
                        str(item.get("gpu_type_id") or ""),
                    ),
                )[0]
                selected.append(
                    {
                        "data_center_id": dc,
                        "gpu_type_id": best.get("gpu_type_id"),
                        "quoted_cost_usd_per_hr": best.get("quoted_cost_usd_per_hr"),
                        "estimated_cost_usd_per_hr": best.get("estimated_cost_usd_per_hr"),
                        "quote_source": best.get("quote_source") or matrix.get("reason"),
                        "stock_status": best.get("stock_status"),
                        "eligible": bool(best.get("eligible")),
                        "scout_status": best.get("scout_status"),
                        "min_vram_gb": min_vram_gb,
                        "gpu_vendor": gpu_vendor,
                    }
                )
            return selected

        result = []
        for dc in data_centers:
            scout = self.adapter.scout_gpu(
                {
                    "data_center_id": dc,
                    "min_vram_gb": min_vram_gb,
                    "gpu_vendor": gpu_vendor,
                }
            )
            if scout.ok and scout.authoritative and scout.gpu_type_id and (scout.price_per_hr_usd is None or float(scout.price_per_hr_usd) <= max_rate):
                result.append(
                    {
                        "data_center_id": dc,
                        "gpu_type_id": scout.gpu_type_id,
                        "quoted_cost_usd_per_hr": scout.price_per_hr_usd,
                        "estimated_cost_usd_per_hr": scout.price_per_hr_usd,
                        "quote_source": scout.reason,
                        "min_vram_gb": min_vram_gb,
                        "gpu_vendor": gpu_vendor,
                    }
                )
        return result

    def _workflow_budget_estimate(self, *, candidates: list[dict[str, Any]], size_gb: int, max_rate: float, lease_minutes: int) -> dict[str, Any]:
        candidate_count = len(candidates)
        cpu_hours = max(0, self.settings.hydration_estimate_minutes) / 60.0
        temp_volume_hours = max(0, self.settings.hydration_estimate_minutes + self.settings.gpu_acquisition_estimate_minutes) / 60.0
        cpu_cost = candidate_count * self.settings.default_cpu_usd_per_hr * cpu_hours
        volume_cost = candidate_count * network_volume_rate_usd_per_hr(size_gb) * temp_volume_hours
        gpu_cost = max_rate * max(0, lease_minutes) / 60.0
        return {
            "candidate_count": candidate_count,
            "volume_size_gb": size_gb,
            "cpu_rate_usd_per_hr": self.settings.default_cpu_usd_per_hr,
            "cpu_hydration_minutes": self.settings.hydration_estimate_minutes,
            "volume_retention_minutes": self.settings.hydration_estimate_minutes + self.settings.gpu_acquisition_estimate_minutes,
            "winner_gpu_rate_usd_per_hr": max_rate,
            "winner_gpu_lease_minutes": lease_minutes,
            "estimated_cpu_usd": round(cpu_cost, 6),
            "estimated_temp_volume_usd": round(volume_cost, 6),
            "estimated_winner_gpu_usd": round(gpu_cost, 6),
            "estimated_total_usd": round(cpu_cost + volume_cost + gpu_cost, 6),
        }

    def _start_workflow_thread(
        self,
        request_id: str,
        workflow_id: str,
        session_id: str,
        candidate_ids: list[str],
        assets: list[dict[str, Any]],
        size_gb: int,
        launch_context: dict[str, Any] | None = None,
    ) -> None:
        def run() -> None:
            try:
                winner_id = self._run_workflow_candidates(workflow_id, session_id, candidate_ids, assets, size_gb, launch_context or {})
                if winner_id:
                    session = self.db.get("sessions", session_id) or {}
                    self.db.update("resource_requests", request_id, {"state": str(session.get("state") or "interactive_ready"), "updated_at": utc_iso()})
                else:
                    workflow = self.db.get("session_workflows", workflow_id) or {}
                    self.db.update(
                        "resource_requests",
                        request_id,
                        {
                            "state": "failed",
                            "error": str(workflow.get("state") or "workflow_failed_no_winner"),
                            "updated_at": utc_iso(),
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                self.db.update("resource_requests", request_id, {"state": "failed", "error": repr(exc), "updated_at": utc_iso()})
                self.db.update("session_workflows", workflow_id, {"state": "failed", "updated_at": utc_iso()})
                self.db.update("sessions", session_id, {"state": "failed", "phase": "failed", "updated_at": utc_iso()})
                self._workflow_event(workflow_id, session_id, None, "workflow_failed", repr(exc), {})

        if self.settings.workflow_background_threads:
            threading.Thread(target=run, daemon=True, name=f"workflow-{session_id}").start()
        else:
            run()

    def _create_candidate(self, workflow_id: str, session_id: str, candidate: dict[str, Any]) -> str:
        candidate_id = new_id("cand")
        now = utc_iso()
        self.db.insert(
            "workflow_candidates",
            {
                "id": candidate_id,
                "workflow_id": workflow_id,
                "session_id": session_id,
                "data_center_id": candidate["data_center_id"],
                "state": "queued",
                "volume_id": None,
                "cpu_pod_id": None,
                "gpu_pod_id": None,
                "hydration_id": None,
                "gpu_type_id": candidate.get("gpu_type_id"),
                "quoted_cost_usd_per_hr": candidate.get("quoted_cost_usd_per_hr"),
                "download_done_bytes": 0,
                "download_total_bytes": 0,
                "attempt_count": 0,
                "last_error": None,
                "cleanup_status": None,
                "created_at": now,
                "updated_at": now,
            },
        )
        return candidate_id

    def _run_workflow_candidates(
        self,
        workflow_id: str,
        session_id: str,
        candidate_ids: list[str],
        assets: list[dict[str, Any]],
        size_gb: int,
        launch_context: dict[str, Any] | None = None,
    ) -> str | None:
        launch_context = launch_context or {}
        winner: dict[str, Any] = {"id": None, "failed_ids": []}
        winner_lock = threading.Lock()
        winner_event = threading.Event()
        configure_lock = threading.Lock()
        total_bytes = sum(int(asset.get("size_bytes") or 0) for asset in assets)

        def run_candidate(candidate_id: str) -> None:
            candidate = self.db.get("workflow_candidates", candidate_id)
            if not candidate:
                return
            try:
                warm_volume_id = candidate.get("volume_id")
                self._update_candidate(candidate_id, {"state": "volume_creating", "download_total_bytes": total_bytes})
                dc = candidate["data_center_id"]
                if warm_volume_id:
                    volume_id = warm_volume_id
                else:
                    volume_payload = self.adapter.create_network_volume(name=f"rpc-{session_id}-{dc.lower()}", size_gb=size_gb, data_center_id=dc)
                    volume_id = self._record_volume(volume_payload, f"rpc-{session_id}-{dc.lower()}", size_gb, dc, {"retention_policy": "delete_after_collection"})
                self._update_candidate(candidate_id, {"volume_id": volume_id, "state": "cpu_pod_starting"})
                if winner_event.is_set():
                    self._cleanup_loser(candidate_id, session_id=session_id, workflow_id=workflow_id)
                    return
                hydration = self._insert_hydration_request(
                    {
                        "session_id": session_id,
                        "volume_id": volume_id,
                        "assets": assets,
                        "launch_template_id": launch_context.get("launch_template_id"),
                        "install_plan": launch_context.get("install_plan"),
                        "validation_plan": launch_context.get("validation_plan"),
                        "ui_workflow_json": launch_context.get("ui_workflow"),
                        "api_workflow_json": launch_context.get("api_workflow"),
                        "custom_nodes": launch_context.get("custom_nodes"),
                    }
                )
                self._update_candidate(candidate_id, {"hydration_id": hydration["id"], "state": "cpu_pod_starting"})
                if winner_event.is_set():
                    self._cleanup_loser(candidate_id, session_id=session_id, workflow_id=workflow_id)
                    return

                if warm_volume_id:
                    hydration = self._complete_warm_hydration(hydration["id"], volume_id) or hydration
                else:
                    def on_cpu_pod_created(pod_id: str) -> None:
                        self._update_candidate(candidate_id, {"cpu_pod_id": pod_id, "state": "cpu_hydrating"})
                        self._workflow_event(workflow_id, session_id, candidate_id, "cpu_pod_created", f"{dc} CPU hydration Pod created", {"pod_id": pod_id})

                    hydration = self._process_hydration(hydration["id"], run_cpu_pod=True, on_cpu_pod_created=on_cpu_pod_created) or hydration
                self._update_candidate(
                    candidate_id,
                    {
                        "hydration_id": hydration["id"],
                        "cpu_pod_id": hydration.get("cpu_pod_id"),
                        "download_done_bytes": total_bytes,
                        "state": "hydrated",
                    },
                )
                self._workflow_event(
                    workflow_id,
                    session_id,
                    candidate_id,
                    "hydrated",
                    f"{dc} warm volume verified; hydration skipped" if warm_volume_id else f"{dc} CPU hydration completed",
                    {"volume_id": volume_id, "warm_reuse": bool(warm_volume_id)},
                )
                if winner_event.is_set():
                    self._cleanup_loser(candidate_id, session_id=session_id, workflow_id=workflow_id)
                    return
                if not configure_lock.acquire(blocking=False):
                    self._update_candidate(candidate_id, {"state": "backup_hydrated"})
                    self._workflow_event(workflow_id, session_id, candidate_id, "backup_hydrated", "Candidate is hydrated and waiting without a GPU Pod", {})
                    while not winner_event.is_set():
                        if configure_lock.acquire(timeout=5):
                            break
                    else:
                        self._cleanup_loser(candidate_id, session_id=session_id, workflow_id=workflow_id)
                        return
                try:
                    if winner_event.is_set():
                        self._cleanup_loser(candidate_id, session_id=session_id, workflow_id=workflow_id)
                        return
                    if not self._attempt_candidate_gpu(workflow_id, session_id, candidate_id):
                        return
                    if winner_event.is_set():
                        self._workflow_event(workflow_id, session_id, candidate_id, "gpu_race_lost", "Candidate created a GPU Pod but another candidate became the configured winner first", {})
                        self._cleanup_loser(candidate_id, session_id=session_id, workflow_id=workflow_id)
                        return
                    if not self._claim_candidate_as_winner(workflow_id, session_id, candidate_id):
                        self._cleanup_loser(candidate_id, session_id=session_id, workflow_id=workflow_id)
                        return
                    if self._configure_winner_candidate(workflow_id, session_id, candidate_id):
                        with winner_lock:
                            winner["id"] = candidate_id
                            winner_event.set()
                        return
                    winner["failed_ids"].append(candidate_id)
                    self._workflow_event(workflow_id, session_id, candidate_id, "winner_candidate_rejected", "Candidate created a GPU Pod but failed environment configuration; waiting for another hydrated candidate", {})
                    self._cleanup_loser(candidate_id, session_id=session_id, workflow_id=workflow_id)
                    if not winner_event.is_set():
                        self.db.update(
                            "sessions",
                            session_id,
                            {
                                "state": "gpu_acquiring",
                                "phase": "gpu_acquiring",
                                "watchdog_last_reason": "winner_candidate_configuration_failed_waiting_for_backup",
                                "updated_at": utc_iso(),
                            },
                        )
                    return
                finally:
                    configure_lock.release()
            except Exception as exc:  # noqa: BLE001
                self._update_candidate(candidate_id, {"state": "failed", "last_error": repr(exc)})
                self._workflow_event(workflow_id, session_id, candidate_id, "candidate_failed", repr(exc), {})
                cleanup = self._cleanup_loser(candidate_id, session_id=session_id, workflow_id=workflow_id)
                if not cleanup.get("ok"):
                    self._workflow_event(workflow_id, session_id, candidate_id, "candidate_cleanup_failed", "; ".join(cleanup.get("errors") or []), {})

        threads = [threading.Thread(target=run_candidate, args=(candidate_id,), daemon=True) for candidate_id in candidate_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winner_id = winner["id"]
        if winner_id:
            for candidate_id in candidate_ids:
                if candidate_id != winner_id:
                    candidate = self.db.get("workflow_candidates", candidate_id)
                    if candidate and candidate.get("state") != "deleted":
                        self._cleanup_loser(candidate_id, session_id=session_id, workflow_id=workflow_id)
            session = self.db.get("sessions", session_id) or {}
            ready_state = str(session.get("state") or "interactive_ready")
            self.db.update("session_workflows", workflow_id, {"state": ready_state, "winner_candidate_id": winner_id, "completed_at": utc_iso(), "updated_at": utc_iso()})
            self._workflow_event(workflow_id, session_id, winner_id, ready_state, "Winner GPU Pod has an open UI route", {})
        else:
            cleanup_errors = []
            for candidate_id in candidate_ids:
                result = self._cleanup_loser(candidate_id, session_id=session_id, workflow_id=workflow_id)
                cleanup_errors.extend(result.get("errors") or [])
            terminal_state = "cleanup_failed" if cleanup_errors else "failed"
            self.db.update("session_workflows", workflow_id, {"state": terminal_state, "updated_at": utc_iso()})
            self._guarded_session_update(session_id, {"state": terminal_state, "phase": terminal_state})
            message = "All GPU winner candidates failed configuration" if winner.get("failed_ids") else "No candidate acquired a GPU"
            failed_id = (winner.get("failed_ids") or [None])[-1]
            self._workflow_event(workflow_id, session_id, failed_id, "workflow_failed", message, {"cleanup_errors": cleanup_errors, "failed_winner_candidate_ids": winner.get("failed_ids") or []})
        return winner_id

    def _attempt_candidate_gpu(self, workflow_id: str, session_id: str, candidate_id: str) -> bool:
        candidate = self.db.get("workflow_candidates", candidate_id)
        session = self.db.get("sessions", session_id)
        if not candidate or not session:
            return False
        if candidate.get("session_id") != session_id or candidate.get("workflow_id") != workflow_id:
            self._update_candidate(candidate_id, {"state": "failed", "last_error": "candidate_scope_mismatch"})
            return False
        if str(session.get("state") or "") in TERMINAL_SESSION_STATES | {"collecting_outputs"}:
            self._update_candidate(candidate_id, {"state": "failed", "last_error": "session_terminal_before_gpu_attempt"})
            return False
        self._update_candidate(candidate_id, {"state": "gpu_acquiring", "attempt_count": int(candidate.get("attempt_count") or 0) + 1})
        self._mark_session_gpu_acquiring(session_id)
        volume = self.db.get("network_volumes", candidate["volume_id"])
        gpu_type_id = candidate.get("gpu_type_id")
        if not volume:
            self._update_candidate(candidate_id, {"state": "failed", "last_error": "missing_network_volume"})
            return False
        if not gpu_type_id:
            self._update_candidate(candidate_id, {"state": "failed", "last_error": "missing_gpu_type_id"})
            return False
        attempt_id = self._record_gpu_attempt(
            session_id=session_id,
            state="creating",
            data_center_id=candidate["data_center_id"],
            gpu_type_id=candidate.get("gpu_type_id"),
            quoted_cost_usd_per_hr=candidate.get("quoted_cost_usd_per_hr"),
            quote_source="workflow_candidate",
            raw={"candidate_id": candidate_id, "volume_id": volume["id"]},
        )
        try:
            gpu_payload = self.adapter.create_gpu_pod(
                name=f"comfyui-{session_id}-{candidate['data_center_id'].lower()}",
                volume_provider_id=volume["provider_volume_id"],
                data_center_id=candidate["data_center_id"],
                gpu_type_id=gpu_type_id,
                env={"SESSION_ID": session_id, "VOLUME_ID": volume["provider_volume_id"], "CANDIDATE_ID": candidate_id},
            )
        except Exception as exc:  # noqa: BLE001
            error = repr(exc)
            self._update_candidate(candidate_id, {"state": "gpu_failed", "last_error": error})
            self._update_gpu_attempt(attempt_id, {"state": "failed", "error": error, "updated_at": utc_iso()})
            self._workflow_event(workflow_id, session_id, candidate_id, "gpu_create_failed", error, {})
            return False
        gpu_pod_id = self._record_pod(
            provider_payload=gpu_payload,
            session_id=session_id,
            volume_id=volume["id"],
            role="comfyui_gpu",
            compute_type="GPU",
            data_center_id=candidate["data_center_id"],
            image="runpod-comfyui-template",
            gpu_type_id=candidate.get("gpu_type_id"),
        )
        self._update_gpu_attempt(
            attempt_id,
            {
                "state": "created",
                "provider_pod_id": gpu_payload.get("id"),
                "raw_json": json_dumps(redact_secrets(gpu_payload)),
                "updated_at": utc_iso(),
            },
        )
        self._update_candidate(candidate_id, {"state": "gpu_created", "gpu_pod_id": gpu_pod_id})
        self._workflow_event(workflow_id, session_id, candidate_id, "gpu_created", "Candidate created a GPU Pod and is waiting for winner selection", {"gpu_pod_id": gpu_pod_id})
        return True

    def _claim_candidate_as_winner(self, workflow_id: str, session_id: str, candidate_id: str) -> bool:
        candidate = self.db.get("workflow_candidates", candidate_id)
        session = self.db.get("sessions", session_id)
        if not candidate or not session:
            return False
        if candidate.get("session_id") != session_id or candidate.get("workflow_id") != workflow_id:
            self._update_candidate(candidate_id, {"state": "failed", "last_error": "candidate_scope_mismatch"})
            return False
        volume = self.db.get("network_volumes", candidate.get("volume_id"))
        gpu_pod_id = candidate.get("gpu_pod_id")
        if not volume or not gpu_pod_id:
            self._update_candidate(candidate_id, {"state": "failed", "last_error": "winner_missing_gpu_or_volume"})
            return False
        self._update_candidate(candidate_id, {"state": "environment_configuring"})
        claimed = self._guarded_session_update(
            session_id,
            {
                "state": "environment_configuring",
                "phase": "environment_configuring",
                "network_volume_id": volume["id"],
                "hydration_id": candidate.get("hydration_id"),
                "gpu_pod_id": gpu_pod_id,
                "data_center_id": candidate["data_center_id"],
            },
        )
        if not claimed:
            self._update_candidate(candidate_id, {"state": "failed", "last_error": "session_terminal_before_winner_claim"})
            return False
        self._workflow_event(
            workflow_id,
            session_id,
            candidate_id,
            "winner_configuring",
            "Candidate created a GPU Pod and is being configured; it becomes the winner only after validation succeeds",
            {"gpu_pod_id": gpu_pod_id, "volume_id": volume["id"]},
        )
        return True

    def _configure_winner_candidate(self, workflow_id: str, session_id: str, candidate_id: str) -> bool:
        candidate = self.db.get("workflow_candidates", candidate_id)
        if not candidate or candidate.get("session_id") != session_id or candidate.get("workflow_id") != workflow_id:
            return False
        gpu_pod_id = candidate.get("gpu_pod_id")
        if not gpu_pod_id:
            self._update_candidate(candidate_id, {"state": "environment_failed", "last_error": "winner_missing_gpu_pod"})
            return False
        pod = self.db.get("pods", gpu_pod_id)
        workflow = self.db.get("session_workflows", workflow_id) or {}
        assets = json_loads(workflow.get("assets_json"), [])
        install_plan = json_loads(workflow.get("install_plan_json"), {"version": 1, "steps": []})
        validation_plan = json_loads(workflow.get("validation_plan_json"), {})
        custom_nodes = json_loads(workflow.get("custom_nodes_json"), [])
        ui_workflow = json_loads(workflow.get("ui_workflow_json"), None)
        api_workflow = json_loads(workflow.get("api_workflow_json"), None)
        config_result = self.adapter.configure_comfyui_environment(
            provider_pod_id=str((pod or {}).get("provider_pod_id") or ""),
            session_id=session_id,
            assets=assets,
            install_plan=install_plan,
            validation_plan=validation_plan,
            custom_nodes=custom_nodes,
            ui_workflow_json=ui_workflow,
            api_workflow_json=api_workflow,
        )
        self._workflow_event(workflow_id, session_id, candidate_id, "environment_configured" if config_result.get("ok") else "environment_config_failed", str(config_result.get("reason") or config_result), config_result)
        if not config_result.get("ok"):
            error = "environment_config_failed:" + str(config_result.get("reason") or "unknown")
            self._update_candidate(candidate_id, {"state": "environment_failed", "last_error": error})
            self.db.update("sessions", session_id, {"watchdog_last_reason": error, "updated_at": utc_iso()})
            return False
        tunnel = self.ensure_tunnel(session_id, {"pod_id": gpu_pod_id})
        ready = self._guarded_session_update(session_id, {"state": "interactive_ready", "phase": "interactive_ready", "ui_url": tunnel["local_url"]})
        if not ready:
            self._update_candidate(candidate_id, {"state": "environment_failed", "last_error": "session_terminal_before_interactive_ready"})
            return False
        self._update_candidate(candidate_id, {"state": "won", "cleanup_status": "winner_retained"})
        return True

    def _guarded_session_update(self, session_id: str, values: dict[str, Any]) -> bool:
        """Update a session only while it is not terminal.

        Candidate race threads outlive a forced reclaim; an unconditional write
        would resurrect a reclaimed session (and its billing) from one of them.
        Returns False when the session reached a terminal state concurrently."""
        blocked = tuple(sorted(TERMINAL_SESSION_STATES | {"collecting_outputs"}))
        assignments = ", ".join(f"{column} = ?" for column in values)
        placeholders = ", ".join("?" for _ in blocked)
        sql = f"UPDATE sessions SET {assignments}, updated_at = ? WHERE id = ? AND state NOT IN ({placeholders})"
        return self.db.execute_with_rowcount(sql, [*values.values(), utc_iso(), session_id, *blocked]) > 0

    def _mark_session_gpu_acquiring(self, session_id: str) -> None:
        now = utc_iso()
        self.db.execute(
        """
            UPDATE sessions
               SET state = ?, phase = ?, updated_at = ?
             WHERE id = ?
               AND state IN ('dependency_probe_passed', 'hydrating_all', 'hydrated', 'hydrated_cpu_ready', 'gpu_acquiring')
            """,
            ("gpu_acquiring", "gpu_acquiring", now, session_id),
        )

    def _cleanup_loser(self, candidate_id: str, *, session_id: str | None = None, workflow_id: str | None = None) -> dict[str, Any]:
        candidate = self.db.get("workflow_candidates", candidate_id)
        if not candidate:
            return {"ok": True, "errors": []}
        errors = []
        if session_id and candidate.get("session_id") != session_id:
            return {"ok": False, "errors": [f"candidate_session_mismatch:{candidate_id}"]}
        if workflow_id and candidate.get("workflow_id") != workflow_id:
            return {"ok": False, "errors": [f"candidate_workflow_mismatch:{candidate_id}"]}
        for pod_key in ["gpu_pod_id", "cpu_pod_id"]:
            if candidate.get(pod_key):
                pod = self.db.get("pods", candidate[pod_key])
                if pod and pod["state"] != "deleted":
                    if pod.get("session_id") != candidate.get("session_id"):
                        errors.append(f"pod_session_mismatch:{pod['id']}")
                        continue
                    if pod.get("volume_id") and candidate.get("volume_id") and pod.get("volume_id") != candidate.get("volume_id"):
                        errors.append(f"pod_volume_mismatch:{pod['id']}")
                        continue
                    error = self._delete_pod_record(pod)
                    if error:
                        errors.append(error)
        if candidate.get("volume_id"):
            volume = self.db.get("network_volumes", candidate["volume_id"])
            if volume and volume["state"] != "deleted":
                error = self._delete_volume_record(volume)
                if error:
                    errors.append(error)
        if errors:
            self._update_candidate(candidate_id, {"state": "cleanup_failed", "cleanup_status": "; ".join(errors), "last_error": errors[-1]})
            return {"ok": False, "errors": errors}
        self._update_candidate(candidate_id, {"state": "deleted", "cleanup_status": "deleted"})
        return {"ok": True, "errors": []}

    def _delete_pod_record(self, pod: dict[str, Any]) -> str | None:
        result = redact_secrets(self.adapter.delete_pod(pod["provider_pod_id"]))
        if not result.get("ok"):
            return f"pod_delete_failed:{pod['id']}:{result}"
        terminal_at = utc_iso()
        self.db.update("pods", pod["id"], {"state": "deleted", "deleted_at": terminal_at, "updated_at": terminal_at})
        return None

    def _delete_volume_record(self, volume: dict[str, Any]) -> str | None:
        result = redact_secrets(self.adapter.delete_network_volume(volume["provider_volume_id"]))
        if not result.get("ok"):
            return f"volume_delete_failed:{volume['id']}:{result}"
        terminal_at = utc_iso()
        self.db.update("network_volumes", volume["id"], {"state": "deleted", "deleted_at": terminal_at, "updated_at": terminal_at})
        return None

    def _update_candidate(self, candidate_id: str, values: dict[str, Any]) -> None:
        values["updated_at"] = utc_iso()
        self.db.update("workflow_candidates", candidate_id, values)

    def _workflow_event(self, workflow_id: str, session_id: str, candidate_id: str | None, event_type: str, message: str, details: dict[str, Any]) -> None:
        self.db.insert(
            "workflow_events",
            {
                "id": new_id("wev"),
                "workflow_id": workflow_id,
                "session_id": session_id,
                "candidate_id": candidate_id,
                "event_type": event_type,
                "message": message,
                "details_json": json_dumps(details),
                "created_at": utc_iso(),
            },
        )

    def list_model_templates(self, product: str | None = None) -> list[dict[str, Any]]:
        product = product or self.settings.product
        rows = self.db.query("SELECT * FROM model_templates WHERE product = ? ORDER BY name", (product,))
        return [self._expand_json_fields(row, ["assets_json"]) for row in rows]

    def create_model_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        template_id = new_id("tpl")
        now = utc_iso()
        product = str(payload.get("product") or self.settings.product)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("template name is required")
        assets = normalize_asset_manifest(payload.get("assets") or [])
        self.db.insert(
            "model_templates",
            {
                "id": template_id,
                "product": product,
                "name": name,
                "assets_json": json_dumps(assets),
                "created_at": now,
                "updated_at": now,
            },
        )
        return self._expand_json_fields(self.db.get("model_templates", template_id), ["assets_json"])

    def update_model_template(self, template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        template = self.db.get("model_templates", template_id)
        if not template:
            raise KeyError(template_id)
        values: dict[str, Any] = {"updated_at": utc_iso()}
        if payload.get("name"):
            values["name"] = str(payload["name"]).strip()
        if "assets" in payload:
            values["assets_json"] = json_dumps(normalize_asset_manifest(payload.get("assets") or []))
        self.db.update("model_templates", template_id, values)
        return self._expand_json_fields(self.db.get("model_templates", template_id), ["assets_json"])

    def delete_model_template(self, template_id: str) -> dict[str, Any]:
        if not self.db.get("model_templates", template_id):
            raise KeyError(template_id)
        self.db.execute("DELETE FROM model_templates WHERE id = ?", (template_id,))
        return {"ok": True, "id": template_id}

    def terminate_workflow(self, session_id: str) -> dict[str, Any]:
        workflow = self._latest_workflow(session_id)
        if not workflow:
            return self.reclaim_session(session_id, {"force": True, "keep_volume": True})
        errors = []
        for candidate in self.db.query("SELECT * FROM workflow_candidates WHERE workflow_id = ?", (workflow["id"],)):
            if candidate.get("state") not in {"won", "deleted"}:
                result = self._cleanup_loser(candidate["id"], session_id=session_id, workflow_id=workflow["id"])
                errors.extend(result.get("errors") or [])
        result = self.reclaim_session(session_id, {"force": True, "keep_volume": True})
        workflow_state = "terminated" if result.get("ok") else str(result.get("state") or "cleanup_failed")
        self.db.update("session_workflows", workflow["id"], {"state": workflow_state, "updated_at": utc_iso(), "completed_at": utc_iso() if result.get("ok") else None})
        self._workflow_event(workflow["id"], session_id, None, "terminated", "Workflow termination requested; GPU stopped before output collection", {"reclaim": result})
        if errors or not result.get("ok", True):
            return {"ok": False, "workflow_id": workflow["id"], "cleanup_errors": errors, "reclaim": result}
        return {"ok": True, "workflow_id": workflow["id"], "reclaim": result}

    def mark_workflow_verified(self, session_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        session = self.get_session(session_id)
        if not session:
            raise KeyError(session_id)
        workflow = session.get("workflow") or {}
        workflow_id = workflow.get("comfyui_workflow_id")
        if not workflow_id:
            return {"ok": False, "error": "session_has_no_comfyui_workflow"}
        artifacts = [artifact for artifact in (session.get("artifacts") or []) if artifact.get("kind") == "comfyui_output"]
        output = artifacts[-1] if artifacts else None
        if not output:
            return {"ok": False, "error": "no_collected_output_artifact"}
        verification_id = new_id("ver")
        now = utc_iso()
        self.db.insert(
            "comfyui_live_verifications",
            {
                "id": verification_id,
                "workflow_id": workflow_id,
                "session_id": session_id,
                "launch_fingerprint": workflow.get("launch_fingerprint") or "",
                "output_artifact_path": (output or {}).get("local_path") or payload.get("output_artifact_path"),
                "output_checksum_sha256": (output or {}).get("checksum_sha256") or payload.get("output_checksum_sha256"),
                "object_info_result_json": json_dumps(payload.get("object_info_result") or {}),
                "model_visibility_result_json": json_dumps(payload.get("model_visibility_result") or {}),
                "node_visibility_result_json": json_dumps(payload.get("node_visibility_result") or {}),
                "base_template_lock_json": json_dumps(((workflow.get("comfyui_workflow") or {}).get("base_template_lock") or {})),
                "cost_snapshot_json": json_dumps({"effective_cost_usd": session.get("effective_cost_usd"), "cost_source": session.get("cost_source")}),
                "created_at": now,
            },
        )
        self.db.update(
            "comfyui_workflows",
            str(workflow_id),
            {
                "verification_state": "live_verified",
                "last_live_verified_session_id": session_id,
                "last_verified_output_path": (output or {}).get("local_path") or payload.get("output_artifact_path"),
                "verified_at": now,
                "updated_at": now,
            },
        )
        self._workflow_event(workflow["id"], session_id, None, "workflow_verified", "Workflow marked live verified", {"verification_id": verification_id})
        return {"ok": True, "verification_id": verification_id, "workflow_id": workflow_id}

    def _latest_workflow(self, session_id: str) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM session_workflows WHERE session_id = ? ORDER BY created_at DESC LIMIT 1", (session_id,))
        return rows[0] if rows else None

    def _process_resource_request(self, request_id: str) -> None:
        request = self.db.get("resource_requests", request_id)
        if not request:
            return
        payload = json_loads(request["requested_json"], {})
        self.db.update("resource_requests", request_id, {"state": "preflighting", "updated_at": utc_iso()})
        scout = self.adapter.scout_gpu(payload)
        if not (scout.ok and scout.authoritative):
            self.db.update(
                "resource_requests",
                request_id,
                {
                    "state": "failed",
                    "error": scout.reason,
                    "result_json": json_dumps({"scout": scout.raw, "fail_closed": True}),
                    "updated_at": utc_iso(),
                },
            )
            self.audit("resource_request", request_id, "failed_closed", "Preflight had no authoritative quote", {"reason": scout.reason})
            return

        session_id = new_id("ses")
        lease_minutes = int(payload.get("lease_minutes") or self.settings.default_lease_minutes)
        lease_until = (utc_now() + dt.timedelta(minutes=lease_minutes)).isoformat()
        hard_terminate_at = None
        idle_shutdown_at = (utc_now() + dt.timedelta(minutes=self.settings.idle_shutdown_minutes)).isoformat()
        reclaim_warning_at = (parse_iso(idle_shutdown_at) - dt.timedelta(minutes=self.settings.reclaim_warning_minutes)).isoformat()
        size_gb = int(payload.get("network_volume_size_gb") or self.settings.default_volume_size_gb)
        volume_name = f"rpc-{session_id}-{scout.data_center_id.lower()}"
        volume_payload = self.adapter.create_network_volume(
            name=volume_name,
            size_gb=size_gb,
            data_center_id=scout.data_center_id,
        )
        volume_id = self._record_volume(volume_payload, volume_name, size_gb, scout.data_center_id, payload)
        hydration = self.create_hydration_request(
            {
                "session_id": session_id,
                "volume_id": volume_id,
                "assets": payload.get("assets") or {},
                "retention_policy": payload.get("retention_policy") or "delete_after_collection",
                "run_cpu_pod": True,
            }
        )
        session = {
            "id": session_id,
            "request_id": request_id,
            "product": str(payload.get("product") or self.settings.product),
            "mode": str(payload.get("mode") or "interactive"),
            "state": "hydrated_cpu_ready",
            "phase": "hydrated",
            "data_center_id": scout.data_center_id,
            "min_vram_gb": normalize_min_vram_gb(payload.get("min_vram_gb") or self.settings.default_min_vram_gb),
            "gpu_vendor": normalize_gpu_vendor(payload.get("gpu_vendor") or self.settings.default_gpu_vendor),
            "max_gpu_usd_per_hr": float(payload.get("max_gpu_usd_per_hr") or self.settings.default_max_gpu_usd_per_hr),
            "max_total_usd": float(payload.get("max_total_usd") or self.settings.default_max_total_usd),
            "lease_until": lease_until,
            "hard_terminate_at": hard_terminate_at,
            "idle_shutdown_at": idle_shutdown_at,
            "reclaim_warning_at": reclaim_warning_at,
            "watchdog_paused": 0,
            "watchdog_last_checked_at": None,
            "watchdog_last_reason": None,
            "missing_finalization_reason": None,
            "ui_url": None,
            "network_volume_id": volume_id,
            "hydration_id": hydration["id"],
            "cpu_pod_id": hydration.get("cpu_pod_id"),
            "gpu_pod_id": None,
            "estimated_cost_usd": 0,
            "actual_cost_usd": None,
            "actual_cost_observed_at": None,
            "billed_start_at": None,
            "billed_end_at": None,
            "retention_policy": str(payload.get("retention_policy") or "delete_after_collection"),
            "created_at": utc_iso(),
            "updated_at": utc_iso(),
        }
        self.db.insert("sessions", session)
        self.db.update(
            "hydration_requests",
            hydration["id"],
            {"session_id": session_id, "updated_at": utc_iso()},
        )
        self.db.update(
            "resource_requests",
            request_id,
            {
                "state": "hydrated_cpu_ready",
                "session_id": session_id,
                "poll_after_seconds": 30,
                "result_json": json_dumps({"session_id": session_id, "volume_id": volume_id, "hydration_id": hydration["id"]}),
                "updated_at": utc_iso(),
            },
        )
        self.audit("session", session_id, "hydrated_cpu_ready", "CPU hydration completed; GPU promotion is a separate action", {"volume_id": volume_id})

    def _record_volume(self, payload: dict[str, Any], name: str, size_gb: int, data_center_id: str, intent: dict[str, Any]) -> str:
        volume_id = new_id("vol")
        now = utc_iso()
        self.db.insert(
            "network_volumes",
            {
                "id": volume_id,
                "provider_volume_id": str(payload.get("id")),
                "name": str(payload.get("name") or name),
                "data_center_id": str(payload.get("dataCenterId") or data_center_id),
                "size_gb": int(payload.get("size") or size_gb),
                "state": "created",
                "hydration_state": "not_started",
                "hydration_ttl_until": None,
                "retention_policy": str(intent.get("retention_policy") or "delete_after_collection"),
                "estimated_cost_usd": 0,
                "actual_cost_usd": None,
                "actual_cost_observed_at": None,
                "billed_start_at": None,
                "billed_end_at": None,
                "billed_time_ms": None,
                "billing_source": None,
                "last_payload_json": json_dumps(redact_secrets(payload)),
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
            },
        )
        self.audit("network_volume", volume_id, "created", "Network volume created", {"provider_volume_id": payload.get("id")})
        return volume_id

    def create_hydration_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = self._insert_hydration_request(payload)
        processed = self._process_hydration(row["id"], run_cpu_pod=bool(payload.get("run_cpu_pod", True)))
        return processed or self.get_hydration(row["id"]) or row

    def _insert_hydration_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_iso()
        hydration_id = new_id("hyd")
        volume_id = str(payload["volume_id"])
        volume = self.db.get("network_volumes", volume_id)
        if not volume:
            raise ValueError(f"unknown volume_id: {volume_id}")
        ttl_until = (utc_now() + dt.timedelta(hours=self.settings.hydration_ttl_hours)).isoformat()
        row = {
            "id": hydration_id,
            "session_id": payload.get("session_id"),
            "volume_id": volume_id,
            "state": "accepted",
            "assets_json": json_dumps(normalize_asset_manifest(payload.get("assets") or {})),
            "launch_template_id": payload.get("launch_template_id"),
            "install_plan_json": json_dumps(payload.get("install_plan") or {"version": 1, "steps": []}),
            "validation_plan_json": json_dumps(payload.get("validation_plan") or {}),
            "ui_workflow_json": json_dumps(payload.get("ui_workflow_json")) if payload.get("ui_workflow_json") is not None else None,
            "api_workflow_json": json_dumps(payload.get("api_workflow_json")) if payload.get("api_workflow_json") is not None else None,
            "custom_nodes_json": json_dumps(payload.get("custom_nodes") or []),
            "cpu_pod_id": None,
            "artifact_root": None,
            "ttl_until": ttl_until,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        self.db.insert("hydration_requests", row)
        self.audit("hydration", hydration_id, "accepted", "Hydration request accepted", {"volume_id": volume_id})
        return row

    def _process_hydration(
        self,
        hydration_id: str,
        *,
        run_cpu_pod: bool,
        on_cpu_pod_created: Callable[[str], None] | None = None,
    ) -> dict[str, Any] | None:
        hydration = self.db.get("hydration_requests", hydration_id)
        if not hydration:
            return None
        volume = self.db.get("network_volumes", hydration["volume_id"])
        if not volume:
            return None
        self.db.update("hydration_requests", hydration_id, {"state": "hydrating_cpu", "updated_at": utc_iso()})
        self.db.update("network_volumes", volume["id"], {"hydration_state": "hydrating_cpu", "updated_at": utc_iso()})
        pod_id = None
        try:
            if run_cpu_pod:
                cpu_payload = self.adapter.create_cpu_pod(
                    name=f"hydrate-{hydration_id}",
                    volume_provider_id=volume["provider_volume_id"],
                    data_center_id=volume["data_center_id"],
                    env=self._hydration_env(hydration_id, hydration, volume),
                )
                pod_id = self._record_pod(
                    provider_payload=cpu_payload,
                    session_id=hydration.get("session_id"),
                    volume_id=volume["id"],
                    role="hydration_cpu",
                    compute_type="CPU",
                    data_center_id=volume["data_center_id"],
                    image=self.settings.cpu_pod_image,
                )
                self.db.update("hydration_requests", hydration_id, {"cpu_pod_id": pod_id, "updated_at": utc_iso()})
                if on_cpu_pod_created:
                    on_cpu_pod_created(pod_id)
                self._wait_for_cpu_hydration_pod(pod_id)
            verification = self._verify_remote_hydration(hydration_id, volume, hydration)
            artifact_root = self._write_hydration_artifact_markers(hydration_id, volume, hydration)
            if pod_id:
                pod = self.db.get("pods", pod_id)
                if pod:
                    error = self._delete_pod_record(pod)
                    if error:
                        raise RuntimeError(error)
            self.db.update(
                "hydration_requests",
                hydration_id,
                {
                    "state": "hydrated",
                    "artifact_root": str(artifact_root),
                    "updated_at": utc_iso(),
                    "completed_at": utc_iso(),
                },
            )
            self.db.update(
                "network_volumes",
                volume["id"],
                {
                    "hydration_state": "hydrated",
                    "hydration_ttl_until": hydration["ttl_until"],
                    "state": "hydrated",
                    "updated_at": utc_iso(),
                },
            )
            self.audit(
                "hydration",
                hydration_id,
                "hydrated",
                "CPU hydration completed after remote marker verification",
                {"artifact_root": str(artifact_root), "verification": verification},
            )
            return self.get_hydration(hydration_id)
        except Exception as exc:  # noqa: BLE001
            error = repr(exc)
            cleanup_error = None
            if pod_id:
                pod = self.db.get("pods", pod_id)
                if pod and pod.get("state") != "deleted":
                    try:
                        cleanup_error = self._delete_pod_record(pod)
                    except Exception as cleanup_exc:  # noqa: BLE001
                        cleanup_error = repr(cleanup_exc)
            if cleanup_error:
                error = f"{error}; cleanup_error={cleanup_error}"
            self.db.update("hydration_requests", hydration_id, {"state": "failed", "error": error, "updated_at": utc_iso(), "completed_at": utc_iso()})
            self.db.update("network_volumes", volume["id"], {"hydration_state": "failed", "state": "failed", "updated_at": utc_iso()})
            self.audit("hydration", hydration_id, "failed", "CPU hydration failed", {"error": error})
            raise

    def _wait_for_cpu_hydration_pod(self, pod_id: str) -> None:
        pod = self.db.get("pods", pod_id)
        if not pod:
            raise ValueError(f"unknown pod_id: {pod_id}")
        deadline = time.monotonic() + max(1, int(self.settings.hydration_timeout_seconds))
        consecutive_poll_failures = 0
        while True:
            try:
                payload = self.adapter.get_pod(pod["provider_pod_id"])
                consecutive_poll_failures = 0
            except Exception:  # noqa: BLE001 - a transient poll failure must not abort a paid hydration
                payload = None
                consecutive_poll_failures += 1
                if consecutive_poll_failures > 10:
                    raise
            if payload is not None:
                self.db.update("pods", pod_id, {"last_payload_json": json_dumps(redact_secrets(payload)), "updated_at": utc_iso()})
                if self._provider_pod_terminal(payload):
                    terminal_at = utc_iso()
                    self.db.update("pods", pod_id, {"state": "stopped", "stopped_at": terminal_at, "updated_at": terminal_at})
                    return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"hydration_cpu_pod_timeout:{pod['provider_pod_id']}")
            time.sleep(max(0, int(self.settings.hydration_poll_interval_seconds)))

    def _verify_remote_hydration(self, hydration_id: str, volume: dict[str, Any], hydration: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + 120
        last_error: RuntimeError | None = None
        while True:
            try:
                return self._verify_remote_hydration_once(hydration_id, volume, hydration)
            except RuntimeError as exc:
                last_error = exc
                if "missing_runpod_s3_credentials" in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(5)
        if last_error:
            raise last_error
        raise RuntimeError("remote_hydration_verification_failed:unknown")

    def _verify_remote_hydration_once(self, hydration_id: str, volume: dict[str, Any], hydration: dict[str, Any]) -> dict[str, Any]:
        provider_volume_id = str(volume.get("provider_volume_id") or "")
        if provider_volume_id.startswith("test-"):
            return {"ok": True, "source": "test_adapter_skip", "objects": []}
        if not has_s3_credentials():
            raise RuntimeError("remote_hydration_verification_failed:missing_runpod_s3_credentials")
        client = RunpodS3VolumeClient(data_center_id=str(volume["data_center_id"]), volume_id=provider_volume_id)
        prefix = "runpod-controller/"
        objects = client.list_objects(prefix)
        sizes = {item.key: item.size for item in objects}
        marker_keys = [
            f"runpod-controller/hydration/{hydration_id}/HYDRATED.json",
            f"runpod-controller/hydration/{hydration_id}/inventory.json",
            f"runpod-controller/hydration/{hydration_id}/checksums.sha256",
            f"runpod-controller/hydration/{hydration_id}/DONE.json",
        ]
        missing = [key for key in marker_keys if key not in sizes]
        mismatches: list[str] = []
        assets = json_loads(hydration.get("assets_json"), [])
        asset_keys: list[str] = []
        for asset in assets:
            target = str(asset.get("target") or "").lstrip("/")
            if not target:
                continue
            key = f"runpod-controller/{target}"
            asset_keys.append(key)
            actual = sizes.get(key)
            if actual is None:
                missing.append(key)
                continue
            expected = asset.get("size_bytes")
            if expected is not None and int(expected) != int(actual):
                mismatches.append(f"{key}:{actual}!={expected}")
        if missing or mismatches:
            raise RuntimeError(
                "remote_hydration_verification_failed:"
                + json_dumps({"missing": missing, "mismatches": mismatches})
            )
        inventory_text = client.get_text(marker_keys[1])
        checksums_text = client.get_text(marker_keys[2])
        hydrated = json_loads(client.get_text(marker_keys[0]), {})
        done = json_loads(client.get_text(marker_keys[3]), {})
        content_missing = []
        for key in asset_keys:
            rel = key.removeprefix("runpod-controller/")
            if rel not in inventory_text:
                content_missing.append(f"inventory:{rel}")
            if rel not in checksums_text:
                content_missing.append(f"checksums:{rel}")
        if str(hydrated.get("state")) != "hydrated":
            content_missing.append("HYDRATED.json:state")
        if str(done.get("state")) != "done":
            content_missing.append("DONE.json:state")
        if content_missing:
            raise RuntimeError("remote_hydration_verification_failed:" + json_dumps({"content_missing": content_missing}))
        return {
            "ok": True,
            "source": "runpod_s3",
            "data_center_id": volume["data_center_id"],
            "provider_volume_id": provider_volume_id,
            "asset_count": len(asset_keys),
            "bytes": sum(sizes[key] for key in asset_keys),
            "markers": marker_keys,
            "objects": summarize_objects([item for item in objects if item.key in [*marker_keys, *asset_keys]]),
        }

    def _provider_pod_terminal(self, payload: dict[str, Any]) -> bool:
        values = [
            payload.get("desiredStatus"),
            payload.get("status"),
            payload.get("state"),
            payload.get("lastStatusChange"),
        ]
        text = " ".join(str(value or "").lower() for value in values)
        return any(marker in text for marker in ["exited", "exit", "stopped", "terminated", "deleted", "failed", "error"])

    def _hydration_env(self, hydration_id: str, hydration: dict[str, Any], volume: dict[str, Any]) -> dict[str, str]:
        assets = json_loads(hydration.get("assets_json"), [])
        providers = {str(asset.get("provider") or "") for asset in assets if isinstance(asset, dict)}
        env = {
            "HYDRATION_ID": hydration_id,
            "SESSION_ID": str(hydration.get("session_id") or ""),
            "VOLUME_ID": volume["provider_volume_id"],
            "ASSETS_JSON": json_dumps(assets),
            "INSTALL_PLAN_JSON": hydration.get("install_plan_json") or json_dumps({"version": 1, "steps": []}),
            "VALIDATION_PLAN_JSON": hydration.get("validation_plan_json") or json_dumps({}),
            "UI_WORKFLOW_BYTES": str(len(str(hydration.get("ui_workflow_json") or "").encode("utf-8"))),
            "API_WORKFLOW_BYTES": str(len(str(hydration.get("api_workflow_json") or "").encode("utf-8"))),
            "CUSTOM_NODES_JSON": hydration.get("custom_nodes_json") or json_dumps([]),
        }
        hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        civitai_token = os.environ.get("CIVITAI_TOKEN")
        if "huggingface" in providers and hf_token:
            env["HF_TOKEN"] = hf_token
        if "civitai" in providers and civitai_token:
            env["CIVITAI_TOKEN"] = civitai_token
        return env

    def _record_pod(
        self,
        *,
        provider_payload: dict[str, Any],
        session_id: str | None,
        volume_id: str | None,
        role: str,
        compute_type: str,
        data_center_id: str,
        image: str | None,
        gpu_type_id: str | None = None,
    ) -> str:
        pod_id = new_id("pod")
        now = utc_iso()
        self.db.insert(
            "pods",
            {
                "id": pod_id,
                "provider_pod_id": str(provider_payload.get("id")),
                "session_id": session_id,
                "volume_id": volume_id,
                "role": role,
                "compute_type": compute_type,
                "state": "running",
                "data_center_id": data_center_id,
                "image": image,
                "cpu_flavor_ids": ",".join(self.settings.cpu_flavor_ids) if compute_type == "CPU" else None,
                "gpu_type_id": gpu_type_id or provider_payload.get("gpuTypeId"),
                "cost_per_hr": provider_payload.get("adjustedCostPerHr", provider_payload.get("costPerHr")),
                "actual_cost_usd": None,
                "actual_cost_observed_at": None,
                "billed_start_at": None,
                "billed_end_at": None,
                "billed_time_ms": None,
                "billing_source": None,
                "last_payload_json": json_dumps(redact_secrets(provider_payload)),
                "created_at": now,
                "updated_at": now,
                "stopped_at": None,
                "deleted_at": None,
            },
        )
        self.audit("pod", pod_id, "created", f"{compute_type} Pod created", {"provider_pod_id": provider_payload.get("id"), "role": role})
        return pod_id

    def _write_hydration_artifact_markers(self, hydration_id: str, volume: dict[str, Any], hydration: dict[str, Any]) -> pathlib.Path:
        root = self.settings.artifacts_dir / "hydrations" / hydration_id
        assets = json_loads(hydration.get("assets_json"), {})
        hydrated = {
            "hydration_id": hydration_id,
            "volume_id": volume["id"],
            "provider_volume_id": volume["provider_volume_id"],
            "data_center_id": volume["data_center_id"],
            "portable_assets_only": True,
            "state": "hydrated",
            "assets": assets,
            "created_at": utc_iso(),
        }
        write_json(root / "HYDRATED.json", hydrated)
        inventory = {
            "files": [
                {"path": "HYDRATED.json", "purpose": "hydration marker"},
                {"path": "inventory.json", "purpose": "file inventory"},
                {"path": "checksums.sha256", "purpose": "integrity marker"},
                *[
                    {
                        "path": str(asset.get("target")),
                        "purpose": f"{asset.get('provider')} {asset.get('kind')}",
                        "sha256": asset.get("sha256"),
                        "size_bytes": asset.get("size_bytes"),
                    }
                    for asset in assets
                ],
            ],
            "estimated_reusable_bytes": sum(int(asset.get("size_bytes") or 0) for asset in assets),
        }
        write_json(root / "inventory.json", inventory)
        checksums = []
        for file_name in ["HYDRATED.json", "inventory.json"]:
            file_path = root / file_name
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
            checksums.append(f"{digest}  {file_name}")
        (root / "checksums.sha256").write_text("\n".join(checksums) + "\n", encoding="utf-8")
        self._record_artifact(hydration.get("session_id"), None, "hydration_marker", root / "HYDRATED.json")
        self._record_artifact(hydration.get("session_id"), None, "hydration_inventory", root / "inventory.json")
        return root

    def _record_artifact(self, session_id: str | None, task_id: str | None, kind: str, path: pathlib.Path, remote_uri: str | None = None) -> None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        size = path.stat().st_size if path.exists() else None
        self.db.insert(
            "artifacts",
            {
                "id": new_id("art"),
                "session_id": session_id,
                "task_id": task_id,
                "kind": kind,
                "local_path": str(path),
                "remote_uri": remote_uri,
                "checksum_sha256": digest,
                "size_bytes": size,
                "mime_type": "application/json" if path.suffix == ".json" else "text/plain",
                "created_at": utc_iso(),
            },
        )

    def _output_collection_lock(self, session_id: str) -> threading.Lock:
        with self._output_collection_locks_lock:
            lock = self._output_collection_locks.get(session_id)
            if not lock:
                lock = threading.Lock()
                self._output_collection_locks[session_id] = lock
            return lock

    def _output_prefixes(self, session_id: str) -> list[str]:
        prefixes = [
            f"runpod-controller/runs/{session_id}/outputs/raw/",
            f"runpod-controller/runs/{session_id}/outputs/",
            "madapps/ComfyUI/output/",
            "ComfyUI/output/",
            "runpod-slim/ComfyUI/output/",
        ]
        seen = set()
        ordered = []
        for prefix in prefixes:
            if prefix not in seen:
                ordered.append(prefix)
                seen.add(prefix)
        return ordered

    def open_session_outputs_locally(self, session_id: str) -> dict[str, Any]:
        """Open the session's collected-outputs folder in the local file manager.

        The controller runs on the operator's own machine, so this is the most
        direct way to reach the images after a session ends."""
        if not self.db.get("sessions", session_id):
            raise KeyError(session_id)
        path = self.settings.artifacts_dir / "sessions" / session_id / "outputs"
        if not path.is_dir():
            return {"ok": False, "error": "no_local_outputs", "path": str(path)}
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            proc = subprocess.run([opener, str(path)], capture_output=True, text=True, timeout=15)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": repr(exc), "path": str(path)}
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout or "")[-300:], "path": str(path)}
        self.audit("session", session_id, "outputs_opened_locally", "Opened local outputs folder", {"path": str(path)})
        return {"ok": True, "path": str(path)}

    def _output_local_path(self, session_id: str, key: str) -> pathlib.Path:
        primary = f"runpod-controller/runs/{session_id}/outputs/"
        if key.startswith(primary):
            rel = key[len(primary):]
        else:
            rel = "fallback/" + key
        safe_parts = [part for part in pathlib.PurePosixPath(rel).parts if part not in {"", ".", ".."}]
        return self.settings.artifacts_dir / "sessions" / session_id / "outputs" / pathlib.Path(*safe_parts)

    def _is_output_object(self, key: str, size: int) -> bool:
        if not key or key.endswith("/") or size <= 0:
            return False
        suffix = pathlib.PurePosixPath(key).suffix.lower()
        if suffix in {".part", ".tmp"}:
            return False
        return suffix in OUTPUT_FILE_SUFFIXES

    def _upsert_output_artifact(self, session_id: str, local_path: pathlib.Path, remote_uri: str, payload: bytes) -> tuple[str, int]:
        digest = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        existing = self.db.query(
            "SELECT * FROM artifacts WHERE session_id = ? AND kind = 'comfyui_output' AND remote_uri = ? ORDER BY created_at DESC LIMIT 1",
            (session_id, remote_uri),
        )
        if existing and existing[0].get("checksum_sha256") == digest and int(existing[0].get("size_bytes") or 0) == size:
            return "skipped", size
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(payload)
        mime = "application/json" if local_path.suffix.lower() == ".json" else "text/plain" if local_path.suffix.lower() == ".txt" else "application/octet-stream"
        now = utc_iso()
        if existing:
            self.db.update(
                "artifacts",
                existing[0]["id"],
                {
                    "local_path": str(local_path),
                    "checksum_sha256": digest,
                    "size_bytes": size,
                    "mime_type": mime,
                    "created_at": now,
                },
            )
            return "updated", size
        self.db.insert(
            "artifacts",
            {
                "id": new_id("art"),
                "session_id": session_id,
                "task_id": None,
                "kind": "comfyui_output",
                "local_path": str(local_path),
                "remote_uri": remote_uri,
                "checksum_sha256": digest,
                "size_bytes": size,
                "mime_type": mime,
                "created_at": now,
            },
        )
        return "downloaded", size

    def _existing_output_artifact(self, session_id: str, remote_uri: str) -> dict[str, Any] | None:
        rows = self.db.query(
            "SELECT * FROM artifacts WHERE session_id = ? AND kind = 'comfyui_output' AND remote_uri = ? ORDER BY created_at DESC LIMIT 1",
            (session_id, remote_uri),
        )
        return rows[0] if rows else None

    def _collected_output_count(self, session_id: str) -> int:
        rows = self.db.query("SELECT COUNT(*) AS count FROM artifacts WHERE session_id = ? AND kind = 'comfyui_output'", (session_id,))
        return int(rows[0]["count"] or 0) if rows else 0

    def _collected_output_bytes(self, session_id: str) -> int:
        rows = self.db.query("SELECT COALESCE(SUM(size_bytes), 0) AS bytes FROM artifacts WHERE session_id = ? AND kind = 'comfyui_output'", (session_id,))
        return int(rows[0]["bytes"] or 0) if rows else 0

    def list_session_outputs(self, session_id: str) -> dict[str, Any]:
        if not self.db.get("sessions", session_id):
            raise KeyError(session_id)
        collections = self.db.query("SELECT * FROM output_collections WHERE session_id = ? ORDER BY started_at DESC LIMIT 50", (session_id,))
        artifacts = self.db.query("SELECT * FROM artifacts WHERE session_id = ? AND kind = 'comfyui_output' ORDER BY created_at", (session_id,))
        return {
            "session_id": session_id,
            "collections": collections,
            "artifacts": artifacts,
            "file_count": len(artifacts),
            "bytes": sum(int(row.get("size_bytes") or 0) for row in artifacts),
        }

    def list_session_model_operations(self, session_id: str) -> list[dict[str, Any]]:
        if not self.db.get("sessions", session_id):
            raise KeyError(session_id)
        return [
            self._expand_json_fields(row, ["progress_json"])
            for row in self.db.query("SELECT * FROM session_model_operations WHERE session_id = ? ORDER BY created_at DESC LIMIT 100", (session_id,))
        ]

    def list_session_models_tree(self, session_id: str) -> dict[str, Any]:
        session, pod, _volume = self._require_runtime_model_session(session_id, require_active=False)
        result = self.adapter.list_comfyui_models(str(pod["provider_pod_id"]), COMFYUI_MODEL_FOLDERS)
        result["session_id"] = session["id"]
        result["operations"] = self.list_session_model_operations(session_id)
        return result

    def start_session_model_download(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session, _pod, _volume = self._require_runtime_model_session(session_id)
        url = canonical_asset_url(str(payload.get("url") or "").strip())
        if not url:
            raise ValueError("url is required")
        model_folder = target_for(str(payload.get("model_folder") or "checkpoints"), "placeholder").split("/")[2]
        url_key = normalized_url_key(url)
        duplicates = self.db.query(
            """
            SELECT * FROM session_model_operations
             WHERE session_id = ?
               AND operation_type = 'download'
               AND source_url_key = ?
               AND state NOT IN ('failed', 'cancelled')
             LIMIT 1
            """,
            (session_id, url_key),
        )
        if duplicates:
            return {"ok": False, "state": "duplicate_url", "session_id": session_id, "existing_operation_id": duplicates[0]["id"]}
        meta = peek_url_metadata(url, model_folder, timeout=30)
        filename = self._safe_runtime_filename(payload.get("filename") or meta.get("filename") or "asset")
        target = target_for(model_folder, filename)
        operation_id = new_id("mdl")
        now = utc_iso()
        row = {
            "id": operation_id,
            "session_id": session_id,
            "operation_type": "download",
            "state": "queued",
            "source_url_key": url_key,
            "source_url_redacted": redact_url(url),
            "provider": str(meta.get("provider") or detect_provider({"url": url})),
            "source_path": None,
            "target_path": f"/workspace/runpod-controller/{target}",
            "model_folder": model_folder,
            "filename": filename,
            "size_bytes": meta.get("size_bytes"),
            "checksum_sha256": None,
            "progress_json": json_dumps({"state": "queued", "target": target}),
            "error": None,
            "started_at": None,
            "finished_at": None,
            "created_at": now,
            "updated_at": now,
        }
        self.db.insert("session_model_operations", row)
        self.audit("session", session_id, "model_download_queued", "Runtime model download queued", {"operation_id": operation_id, "model_folder": model_folder, "filename": filename, "url": redact_url(url)})
        threading.Thread(target=self._run_session_model_download, args=(operation_id,), daemon=True, name=f"model-download-{operation_id}").start()
        return {"ok": True, "state": "queued", "session_id": session_id, "operation_id": operation_id, "metadata": self._redact(meta)}

    def _run_session_model_download(self, operation_id: str) -> None:
        op = self.db.get("session_model_operations", operation_id)
        if not op:
            return
        session_id = str(op["session_id"])
        started = utc_iso()
        self.db.update("session_model_operations", operation_id, {"state": "checking_space", "started_at": started, "updated_at": started, "progress_json": json_dumps({"state": "checking_space"})})
        try:
            session, pod, volume = self._require_runtime_model_session(session_id)
            expected = int(op.get("size_bytes") or 0)
            if expected > 0:
                disk = self.adapter.workspace_disk_usage(str(pod["provider_pod_id"]))
                if not disk.get("ok"):
                    raise RuntimeError("workspace_disk_usage_failed:" + str(disk.get("reason") or disk))
                available = int(disk.get("available_bytes") or 0)
                reserve = 5 * 1024**3
                if available < expected + reserve:
                    current_size = int(volume.get("size_gb") or 0)
                    grow_by = math.ceil((expected + reserve - available) / 1024**3)
                    resize = self.resize_session_volume(session_id, {"size_gb": current_size + max(1, grow_by), "reason": "runtime_model_download", "operation_id": operation_id})
                    if not resize.get("ok"):
                        state = str(resize.get("state") or "resize_failed")
                        self.db.update(
                            "session_model_operations",
                            operation_id,
                            {
                                "state": state,
                                "error": str(resize.get("error") or resize.get("reason") or resize),
                                "progress_json": json_dumps({"state": state, "resize": resize}),
                                "updated_at": utc_iso(),
                            },
                        )
                        return
            self.db.update("session_model_operations", operation_id, {"state": "downloading", "updated_at": utc_iso(), "progress_json": json_dumps({"state": "downloading"})})
            provider = str(op.get("provider") or "generic")
            download_payload = {
                "operation_id": operation_id,
                "url": str(op.get("source_url_redacted") or ""),
                "provider": provider,
                "target_path": str(op.get("target_path") or ""),
                "size_bytes": op.get("size_bytes"),
                "hf_token": os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or "",
                "civitai_token": os.environ.get("CIVITAI_TOKEN") or "",
            }
            result = self.adapter.download_comfyui_model(str(pod["provider_pod_id"]), download_payload)
            finished = utc_iso()
            if not result.get("ok"):
                raise RuntimeError(str(result.get("reason") or result))
            state = str(result.get("state") or "downloaded")
            self.db.update(
                "session_model_operations",
                operation_id,
                {
                    "state": state,
                    "checksum_sha256": result.get("checksum_sha256"),
                    "size_bytes": result.get("size_bytes") or op.get("size_bytes"),
                    "progress_json": json_dumps(self._redact(result)),
                    "finished_at": finished,
                    "updated_at": finished,
                },
            )
            self.audit("session", session_id, "model_download_finished", "Runtime model download finished", {"operation_id": operation_id, "state": state, "target_path": op.get("target_path")})
        except Exception as exc:  # noqa: BLE001
            error = repr(exc)
            finished = utc_iso()
            self.db.update(
                "session_model_operations",
                operation_id,
                {"state": "failed", "error": error, "progress_json": json_dumps({"state": "failed", "error": error}), "finished_at": finished, "updated_at": finished},
            )
            self.audit("session", session_id, "model_download_failed", "Runtime model download failed", {"operation_id": operation_id, "error": error})

    def resize_session_volume(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session, pod, volume = self._require_runtime_model_session(session_id)
        requested_size = int(payload.get("size_gb") or 0)
        current_size = int(volume.get("size_gb") or 0)
        if requested_size <= current_size:
            return {"ok": False, "state": "not_growing", "session_id": session_id, "current_size_gb": current_size, "requested_size_gb": requested_size}
        operation_id = str(payload.get("operation_id") or new_id("mdl"))
        created_op = False
        if not self.db.get("session_model_operations", operation_id):
            now = utc_iso()
            self.db.insert(
                "session_model_operations",
                {
                    "id": operation_id,
                    "session_id": session_id,
                    "operation_type": "resize",
                    "state": "resizing",
                    "source_url_key": None,
                    "source_url_redacted": None,
                    "provider": "runpod",
                    "source_path": None,
                    "target_path": None,
                    "model_folder": None,
                    "filename": None,
                    "size_bytes": None,
                    "checksum_sha256": None,
                    "progress_json": json_dumps({"state": "resizing", "requested_size_gb": requested_size}),
                    "error": None,
                    "started_at": now,
                    "finished_at": None,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            created_op = True
        result = redact_secrets(self.adapter.update_network_volume_size(str(volume["provider_volume_id"]), requested_size))
        if not result.get("ok"):
            error = str(result)
            self.db.update("session_model_operations", operation_id, {"state": "failed", "error": error, "progress_json": json_dumps({"state": "failed", "result": result}), "updated_at": utc_iso(), "finished_at": utc_iso()})
            return {"ok": False, "state": "failed", "session_id": session_id, "operation_id": operation_id, "error": error}
        now = utc_iso()
        self.db.update("network_volumes", volume["id"], {"size_gb": requested_size, "last_payload_json": json_dumps(result), "updated_at": now})
        self.audit("session", session_id, "volume_resize_requested", "Network volume resize requested", {"operation_id": operation_id, "from_gb": current_size, "to_gb": requested_size, "created_operation": created_op})
        verify = self._verify_workspace_resize(str(pod["provider_pod_id"]), requested_size)
        finished = utc_iso()
        if not verify.get("ok"):
            state = "waiting_for_resize_visibility"
            self.db.update(
                "session_model_operations",
                operation_id,
                {"state": state, "progress_json": json_dumps({"state": state, "verify": verify}), "error": str(verify.get("reason") or verify), "updated_at": finished},
            )
            return {"ok": False, "state": state, "session_id": session_id, "operation_id": operation_id, "verify": verify}
        self.db.update(
            "session_model_operations",
            operation_id,
            {"state": "resized", "progress_json": json_dumps({"state": "resized", "verify": verify}), "finished_at": finished, "updated_at": finished},
        )
        return {"ok": True, "state": "resized", "session_id": session_id, "operation_id": operation_id, "size_gb": requested_size, "verify": verify}

    def move_session_model(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session, pod, _volume = self._require_runtime_model_session(session_id)
        source_path = self._normalize_runtime_asset_path(payload.get("source_path"))
        target_folder = target_for(str(payload.get("target_folder") or payload.get("model_folder") or "checkpoints"), "placeholder").split("/")[2]
        filename = self._safe_runtime_filename(payload.get("target_filename") or pathlib.PurePosixPath(source_path).name)
        target_path = f"/workspace/runpod-controller/assets/comfyui/{target_folder}/{filename}"
        operation_id = new_id("mdl")
        now = utc_iso()
        self.db.insert(
            "session_model_operations",
            {
                "id": operation_id,
                "session_id": session_id,
                "operation_type": "move",
                "state": "moving",
                "source_url_key": None,
                "source_url_redacted": None,
                "provider": "ssh",
                "source_path": source_path,
                "target_path": target_path,
                "model_folder": target_folder,
                "filename": filename,
                "size_bytes": None,
                "checksum_sha256": None,
                "progress_json": json_dumps({"state": "moving", "source_path": source_path, "target_path": target_path}),
                "error": None,
                "started_at": now,
                "finished_at": None,
                "created_at": now,
                "updated_at": now,
            },
        )
        try:
            result = self.adapter.move_comfyui_model(str(pod["provider_pod_id"]), {"source_path": source_path, "target_path": target_path})
            finished = utc_iso()
            if not result.get("ok"):
                raise RuntimeError(str(result.get("reason") or result))
            self.db.update(
                "session_model_operations",
                operation_id,
                {"state": "moved", "size_bytes": result.get("size_bytes"), "progress_json": json_dumps(self._redact(result)), "finished_at": finished, "updated_at": finished},
            )
            self.audit("session", session_id, "model_moved", "Runtime model file moved", {"operation_id": operation_id, "source_path": source_path, "target_path": target_path})
            return {"ok": True, "state": "moved", "session_id": session_id, "operation_id": operation_id, "result": self._redact(result)}
        except Exception as exc:  # noqa: BLE001
            error = repr(exc)
            finished = utc_iso()
            self.db.update("session_model_operations", operation_id, {"state": "failed", "error": error, "progress_json": json_dumps({"state": "failed", "error": error}), "finished_at": finished, "updated_at": finished})
            self.audit("session", session_id, "model_move_failed", "Runtime model move failed", {"operation_id": operation_id, "error": error})
            return {"ok": False, "state": "failed", "session_id": session_id, "operation_id": operation_id, "error": error}

    def _verify_workspace_resize(self, provider_pod_id: str, requested_size_gb: int) -> dict[str, Any]:
        threshold = int(requested_size_gb * 1024**3 * 0.80)
        last: dict[str, Any] = {}
        for _attempt in range(12):
            last = self.adapter.workspace_disk_usage(provider_pod_id)
            if last.get("ok") and int(last.get("total_bytes") or 0) >= threshold:
                return {"ok": True, **last}
            time.sleep(5)
        return {"ok": False, "reason": "workspace_df_did_not_reflect_resize", "requested_size_gb": requested_size_gb, "last": last}

    def _require_runtime_model_session(self, session_id: str, *, require_active: bool = True) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        session = self.db.get("sessions", session_id)
        if not session:
            raise KeyError(session_id)
        if session.get("product") != "comfyui":
            raise ValueError("session_not_comfyui")
        if require_active and session.get("state") in TERMINAL_SESSION_STATES:
            raise ValueError("session_not_active")
        if not session.get("gpu_pod_id"):
            raise ValueError("session_has_no_gpu_pod")
        if not session.get("network_volume_id"):
            raise ValueError("session_has_no_network_volume")
        pod = self.db.get("pods", session["gpu_pod_id"])
        volume = self.db.get("network_volumes", session["network_volume_id"])
        if not pod or pod.get("session_id") != session_id:
            raise ValueError("gpu_pod_not_owned_by_session")
        if not volume or volume.get("state") == "deleted":
            raise ValueError("network_volume_not_available")
        if require_active and pod.get("state") in {"deleted", "stopped"}:
            raise ValueError("gpu_pod_not_running")
        return session, pod, volume

    def _safe_runtime_filename(self, value: Any) -> str:
        filename = pathlib.PurePosixPath(str(value or "").strip()).name
        if not filename or filename in {".", ".."} or "/" in filename:
            raise ValueError("invalid_filename")
        return filename

    def _normalize_runtime_asset_path(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("source_path is required")
        if raw.startswith("assets/comfyui/"):
            raw = "/workspace/runpod-controller/" + raw
        root = pathlib.PurePosixPath("/workspace/runpod-controller/assets/comfyui")
        path = pathlib.PurePosixPath(raw)
        if not path.is_absolute():
            raise ValueError("source_path_must_be_absolute_or_assets_relative")
        if ".." in path.parts:
            raise ValueError("source_path_cannot_contain_parent")
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("source_path_not_controller_managed") from exc
        return str(path)

    def output_collection_candidates(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT *
              FROM sessions
             WHERE product = 'comfyui'
               AND network_volume_id IS NOT NULL
               AND state IN ('environment_configuring', 'tunnel_ready', 'interactive_ready', 'reclaim_pending')
               AND COALESCE(output_collection_state, '') != 'running'
            ORDER BY updated_at
            """
        )
        return rows

    def collect_session_outputs(self, session_id: str, mode: str = "manual") -> dict[str, Any]:
        session = self.db.get("sessions", session_id)
        if not session:
            raise KeyError(session_id)
        mode = mode or "manual"
        lock = self._output_collection_lock(session_id)
        acquired = lock.acquire(blocking=mode == "final")
        if not acquired:
            return {"ok": True, "state": "already_running", "session_id": session_id}
        # Everything past the acquire must run under try/finally: a leaked lock would
        # block every later final collection and wedge the watchdog thread forever.
        try:
            return self._collect_session_outputs_locked(session_id, mode)
        finally:
            lock.release()

    def _collect_session_outputs_locked(self, session_id: str, mode: str) -> dict[str, Any]:
        session = self.db.get("sessions", session_id)
        if not session:
            raise KeyError(session_id)
        if mode == "periodic" and str(session.get("state") or "") not in OUTPUT_COLLECTION_STATES:
            # A periodic run can race a reclaim that finished after the candidate
            # snapshot was taken; do not stamp collection state onto a terminal row.
            return {"ok": True, "state": "skipped_session_state", "session_id": session_id, "session_state": session.get("state")}
        collection_id = new_id("out")
        started = utc_iso()
        volume = self.db.get("network_volumes", session["network_volume_id"]) if session.get("network_volume_id") else None
        self.db.insert(
            "output_collections",
            {
                "id": collection_id,
                "session_id": session_id,
                "volume_id": (volume or {}).get("id"),
                "mode": mode,
                "state": "running",
                "file_count": 0,
                "byte_count": 0,
                "downloaded_count": 0,
                "skipped_count": 0,
                "error": None,
                "volume_delete_allowed": 0,
                "started_at": started,
                "finished_at": None,
                "created_at": started,
                "updated_at": started,
            },
        )
        self.db.update(
            "sessions",
            session_id,
            {
                "output_collection_state": "running",
                "output_collection_last_checked_at": started,
                "output_collection_last_error": None,
                "updated_at": started,
            },
        )
        try:
            if not volume:
                raise RuntimeError("missing_network_volume")
            if volume.get("state") == "deleted":
                raise RuntimeError("network_volume_deleted")
            if not has_s3_credentials():
                raise RuntimeError("missing_runpod_s3_credentials")
            provider_volume_id = str(volume.get("provider_volume_id") or "")
            client = RunpodS3VolumeClient(data_center_id=str(volume["data_center_id"]), volume_id=provider_volume_id)
            objects_by_key: dict[str, Any] = {}
            prefix_errors = []
            for prefix in self._output_prefixes(session_id):
                try:
                    for obj in client.list_objects(prefix):
                        if self._is_output_object(obj.key, obj.size):
                            objects_by_key[obj.key] = obj
                except Exception as exc:  # noqa: BLE001
                    prefix_errors.append(f"{prefix}:{repr(exc)}")
            if prefix_errors and not objects_by_key:
                raise RuntimeError("s3_output_list_failed:" + "; ".join(prefix_errors))
            downloaded = 0
            skipped = 0
            total_bytes = 0
            for key, obj in sorted(objects_by_key.items()):
                remote_uri = f"s3://{provider_volume_id}/{key}"
                existing = self._existing_output_artifact(session_id, remote_uri)
                existing_local = pathlib.Path(str(existing.get("local_path") or "")) if existing else None
                if (
                    existing
                    and existing_local
                    and existing_local.exists()
                    and int(existing.get("size_bytes") or 0) == int(obj.size or 0)
                    and int(existing_local.stat().st_size) == int(obj.size or 0)
                ):
                    total_bytes += int(existing.get("size_bytes") or 0)
                    skipped += 1
                    continue
                payload = client.get_object(key)
                action, size = self._upsert_output_artifact(session_id, self._output_local_path(session_id, key), remote_uri, payload)
                total_bytes += size
                if action == "skipped":
                    skipped += 1
                else:
                    downloaded += 1
            finished = utc_iso()
            file_count = len(objects_by_key)
            state = "succeeded"
            self.db.update(
                "output_collections",
                collection_id,
                {
                    "state": state,
                    "file_count": file_count,
                    "byte_count": total_bytes,
                    "downloaded_count": downloaded,
                    "skipped_count": skipped,
                    "error": "; ".join(prefix_errors) if prefix_errors else None,
                    "volume_delete_allowed": 1 if self._collected_output_count(session_id) > 0 else 0,
                    "finished_at": finished,
                    "updated_at": finished,
                },
            )
            self.db.update(
                "sessions",
                session_id,
                {
                    "output_collection_state": state,
                    "output_collection_last_checked_at": finished,
                    "output_collection_last_error": "; ".join(prefix_errors) if prefix_errors else None,
                    "output_collection_file_count": self._collected_output_count(session_id),
                    "output_collection_bytes": self._collected_output_bytes(session_id),
                    "output_collection_retained_volume": 0,
                    "updated_at": finished,
                },
            )
            self.audit("session", session_id, "outputs_collected", "ComfyUI outputs collected from network volume", {"mode": mode, "file_count": file_count, "downloaded_count": downloaded, "skipped_count": skipped})
            workflow = self._latest_workflow(session_id)
            if workflow:
                self._workflow_event(workflow["id"], session_id, None, "outputs_collected", f"Collected {file_count} output file(s)", {"mode": mode, "downloaded_count": downloaded, "skipped_count": skipped})
            return {"ok": True, "state": state, "session_id": session_id, "collection_id": collection_id, "file_count": file_count, "downloaded_count": downloaded, "skipped_count": skipped, "byte_count": total_bytes}
        except Exception as exc:  # noqa: BLE001
            error = repr(exc)
            finished = utc_iso()
            self.db.update(
                "output_collections",
                collection_id,
                {"state": "failed", "error": error, "finished_at": finished, "updated_at": finished},
            )
            self.db.update(
                "sessions",
                session_id,
                {
                    "output_collection_state": "failed",
                    "output_collection_last_checked_at": finished,
                    "output_collection_last_error": error,
                    "output_collection_retained_volume": 1,
                    "updated_at": finished,
                },
            )
            self.audit("session", session_id, "output_collection_failed", "ComfyUI output collection failed; volume retained", {"mode": mode, "error": error})
            workflow = self._latest_workflow(session_id)
            if workflow:
                self._workflow_event(workflow["id"], session_id, None, "output_collection_failed", error, {"mode": mode})
            return {"ok": False, "state": "failed", "session_id": session_id, "collection_id": collection_id, "error": error}

    def promote_session_to_gpu(self, session_id: str) -> dict[str, Any]:
        session = self.db.get("sessions", session_id)
        if not session:
            raise KeyError(session_id)
        volume = self.db.get("network_volumes", session["network_volume_id"])
        if not volume:
            raise ValueError("session volume is missing")
        self.db.update("sessions", session_id, {"state": "gpu_acquiring", "phase": "gpu_acquiring", "updated_at": utc_iso()})
        scout = self.adapter.scout_gpu(
            {
                "data_center_id": session["data_center_id"],
                "min_vram_gb": session.get("min_vram_gb") or self.settings.default_min_vram_gb,
                "gpu_vendor": session.get("gpu_vendor") or self.settings.default_gpu_vendor,
            }
        )
        attempt_id = self._record_gpu_attempt(
            session_id=session_id,
            state="scouted" if scout.ok else "failed",
            data_center_id=scout.data_center_id,
            gpu_type_id=scout.gpu_type_id,
            quoted_cost_usd_per_hr=scout.price_per_hr_usd,
            quote_source=scout.reason,
            raw=scout.raw,
        )
        if not (scout.ok and scout.authoritative and scout.gpu_type_id):
            self.audit("session", session_id, "gpu_preflight_failed", "GPU preflight failed before promotion", {"reason": scout.reason})
            self.db.update("sessions", session_id, {"state": "hydrated_cpu_ready", "phase": "hydrated", "updated_at": utc_iso()})
            self._update_gpu_attempt(attempt_id, {"state": "failed", "error": scout.reason, "updated_at": utc_iso()})
            return {"ok": False, "state": session["state"], "reason": scout.reason}
        max_rate = float(session.get("max_gpu_usd_per_hr") or self.settings.default_max_gpu_usd_per_hr)
        if scout.price_per_hr_usd is not None and float(scout.price_per_hr_usd) > max_rate:
            reason = f"quoted_price_exceeds_budget:{scout.price_per_hr_usd}>{max_rate}"
            self.audit("session", session_id, "gpu_budget_failed", "GPU quote exceeded hourly budget", {"reason": reason})
            self.db.update("sessions", session_id, {"state": "hydrated_cpu_ready", "phase": "hydrated", "updated_at": utc_iso()})
            self._update_gpu_attempt(attempt_id, {"state": "failed", "error": reason, "updated_at": utc_iso()})
            return {"ok": False, "state": "hydrated_cpu_ready", "reason": reason}
        self.db.update("sessions", session_id, {"state": "gpu_booting", "phase": "gpu_booting", "updated_at": utc_iso()})
        gpu_payload = self.adapter.create_gpu_pod(
            name=f"comfyui-{session_id}",
            volume_provider_id=volume["provider_volume_id"],
            data_center_id=session["data_center_id"],
            gpu_type_id=scout.gpu_type_id,
            env={"SESSION_ID": session_id, "VOLUME_ID": volume["provider_volume_id"]},
        )
        gpu_pod_id = self._record_pod(
            provider_payload=gpu_payload,
            session_id=session_id,
            volume_id=volume["id"],
            role="comfyui_gpu",
            compute_type="GPU",
            data_center_id=session["data_center_id"],
            image="runpod-comfyui-template",
            gpu_type_id=scout.gpu_type_id,
        )
        self._update_gpu_attempt(
            attempt_id,
            {
                "state": "created",
                "provider_pod_id": gpu_payload.get("id"),
                "raw_json": json_dumps(redact_secrets(gpu_payload)),
                "updated_at": utc_iso(),
            },
        )
        hydration = self.db.get("hydration_requests", session.get("hydration_id")) or {}
        config_result = self.adapter.configure_comfyui_environment(
            provider_pod_id=str(gpu_payload.get("id") or ""),
            session_id=session_id,
            assets=json_loads(hydration.get("assets_json"), []),
            install_plan=json_loads(hydration.get("install_plan_json"), {"version": 1, "steps": []}),
            validation_plan=json_loads(hydration.get("validation_plan_json"), {}),
            custom_nodes=json_loads(hydration.get("custom_nodes_json"), []),
            ui_workflow_json=json_loads(hydration.get("ui_workflow_json"), None),
            api_workflow_json=json_loads(hydration.get("api_workflow_json"), None),
        )
        if not config_result.get("ok"):
            reason = "environment_config_failed:" + str(config_result.get("reason") or "unknown")
            self.db.update("sessions", session_id, {"state": "environment_failed", "phase": "environment_failed", "watchdog_last_reason": reason, "updated_at": utc_iso()})
            self.audit("session", session_id, "environment_config_failed", "ComfyUI model path configuration failed", config_result)
            return {"ok": False, "state": "environment_failed", "session_id": session_id, "gpu_pod_id": gpu_pod_id, "reason": reason, "details": config_result}
        tunnel = self.ensure_tunnel(session_id, {"pod_id": gpu_pod_id})
        ui_url = tunnel["local_url"]
        self.db.update(
            "sessions",
            session_id,
            {"state": "interactive_ready", "phase": "interactive_ready", "gpu_pod_id": gpu_pod_id, "ui_url": ui_url, "updated_at": utc_iso()},
        )
        self.audit("session", session_id, "interactive_ready", "GPU Pod started, ComfyUI paths configured, and UI route is allocated", {"gpu_pod_id": gpu_pod_id, "ui_url": ui_url, "environment": config_result})
        return {"ok": True, "state": "interactive_ready", "session_id": session_id, "gpu_pod_id": gpu_pod_id, "ui_url": ui_url, "tunnel": tunnel}

    def _record_gpu_attempt(
        self,
        *,
        session_id: str,
        state: str,
        data_center_id: str,
        gpu_type_id: str | None,
        quoted_cost_usd_per_hr: float | None,
        quote_source: str,
        raw: dict[str, Any],
        error: str | None = None,
        backoff_seconds: int = 0,
    ) -> str:
        with self._gpu_attempt_lock:
            rows = self.db.query("SELECT COALESCE(MAX(attempt_number), 0) AS n FROM gpu_acquisition_attempts WHERE session_id = ?", (session_id,))
            attempt_number = int(rows[0].get("n") or 0) + 1
            attempt_id = new_id("gat")
            now = utc_iso()
            self.db.insert(
                "gpu_acquisition_attempts",
                {
                    "id": attempt_id,
                    "session_id": session_id,
                    "attempt_number": attempt_number,
                    "state": state,
                    "data_center_id": data_center_id,
                    "gpu_type_id": gpu_type_id,
                    "quoted_cost_usd_per_hr": quoted_cost_usd_per_hr,
                    "quote_source": quote_source,
                    "provider_pod_id": None,
                    "error": error,
                    "backoff_seconds": backoff_seconds,
                    "raw_json": json_dumps(redact_secrets(raw)),
                    "created_at": now,
                    "updated_at": now,
                },
            )
        self.audit("session", session_id, "gpu_acquisition_attempt", "GPU acquisition attempt recorded", {"attempt_number": attempt_number, "state": state})
        return attempt_id

    def _update_gpu_attempt(self, attempt_id: str, values: dict[str, Any]) -> None:
        self.db.update("gpu_acquisition_attempts", attempt_id, values)

    def ensure_tunnel(self, session_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self.db.get("sessions", session_id)
        if not session:
            raise KeyError(session_id)
        payload = payload or {}
        existing = self.db.query("SELECT * FROM tunnels WHERE session_id = ? ORDER BY created_at DESC LIMIT 1", (session_id,))
        if existing and not payload.get("force_restart"):
            return self._serialize_tunnel(existing[0])
        pod_id = payload.get("pod_id") or session.get("gpu_pod_id")
        pod = self.db.get("pods", pod_id) if pod_id else None
        proxy_url = self._runpod_proxy_url(pod)
        state = "proxy_ready" if proxy_url else "pending"
        now = utc_iso()
        tunnel_id = new_id("tun")
        with self._tunnel_lock:
            local_port = self._allocate_local_port()
            self.db.insert(
                "tunnels",
                {
                    "id": tunnel_id,
                    "session_id": session_id,
                    "pod_id": pod["id"] if pod else None,
                    "provider_pod_id": pod["provider_pod_id"] if pod else None,
                    "protocol": "ssh",
                    "remote_host": "127.0.0.1",
                    "remote_port": self.settings.comfyui_remote_port,
                    "local_host": self.settings.tunnel_host,
                    "local_port": local_port,
                    "state": state,
                    "pid": None,
                    "restart_count": 0,
                    "auto_recover": 1 if self.settings.tunnel_auto_recover else 0,
                    "health_url": proxy_url or f"http://{self.settings.tunnel_host}:{local_port}",
                    "last_health_check_at": now,
                    "last_error": None if proxy_url else "ssh_tunnel_not_started",
                    "created_at": now,
                    "updated_at": now,
                },
            )
        self.db.update("sessions", session_id, {"state": "tunnel_ready", "phase": "tunnel_ready", "ui_url": proxy_url or f"http://{self.settings.tunnel_host}:{local_port}", "updated_at": now})
        self.audit("session", session_id, "tunnel_ready", "ComfyUI UI route allocated", {"local_port": local_port, "mode": "runpod_proxy" if proxy_url else "ssh_pending"})
        return self._serialize_tunnel(self.db.get("tunnels", tunnel_id))

    def _runpod_proxy_url(self, pod: dict[str, Any] | None) -> str | None:
        if not pod or not pod.get("provider_pod_id"):
            return None
        provider_id = str(pod["provider_pod_id"])
        if provider_id.startswith(("test-", "fake-")):
            return f"http://{self.settings.tunnel_host}:{self.settings.comfyui_remote_port}"
        return f"https://{provider_id}-{self.settings.comfyui_remote_port}.proxy.runpod.net"

    def restart_tunnel(self, session_id: str) -> dict[str, Any]:
        existing = self.db.query("SELECT * FROM tunnels WHERE session_id = ? ORDER BY created_at DESC LIMIT 1", (session_id,))
        if existing:
            self.db.update("tunnels", existing[0]["id"], {"state": "restarting", "updated_at": utc_iso()})
        return self.ensure_tunnel(session_id, {"force_restart": True})

    def _allocate_local_port(self) -> int:
        used = {
            int(row["local_port"])
            for row in self.db.query("SELECT local_port FROM tunnels WHERE state NOT IN ('closed', 'failed')")
            if row.get("local_port") is not None
        }
        for port in range(self.settings.tunnel_port_start, self.settings.tunnel_port_end + 1):
            if port not in used:
                return port
        raise RuntimeError("no tunnel ports available")

    def _serialize_tunnel(self, tunnel: dict[str, Any] | None) -> dict[str, Any]:
        if not tunnel:
            return {}
        row = dict(tunnel)
        if row.get("state") == "proxy_ready" and row.get("health_url"):
            row["local_url"] = row["health_url"]
        else:
            row["local_url"] = f"http://{row['local_host']}:{row['local_port']}"
        return row

    def extend_lease(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self.db.get("sessions", session_id)
        if not session:
            raise KeyError(session_id)
        minutes = int(payload.get("minutes") or 30)
        lease_until = (utc_now() + dt.timedelta(minutes=minutes)).isoformat()
        idle_shutdown_at = (utc_now() + dt.timedelta(minutes=max(minutes, self.settings.idle_shutdown_minutes))).isoformat()
        warning_dt = parse_iso(idle_shutdown_at) - dt.timedelta(minutes=self.settings.reclaim_warning_minutes)
        self.db.update(
            "sessions",
            session_id,
            {
                "lease_until": lease_until,
                "idle_shutdown_at": idle_shutdown_at,
                "reclaim_warning_at": warning_dt.isoformat(),
                "watchdog_last_reason": "lease_extended",
                "updated_at": utc_iso(),
            },
        )
        self.audit("session", session_id, "lease_updated", "Session lease updated", {"lease_until": lease_until, "minutes": minutes})
        return self.get_session(session_id) or {}

    def create_task(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.db.get("sessions", session_id):
            raise KeyError(session_id)
        task_id = new_id("tsk")
        now = utc_iso()
        self.db.insert(
            "tasks",
            {
                "id": task_id,
                "session_id": session_id,
                "state": str(payload.get("state") or "recorded"),
                "workflow_ref": payload.get("workflow_ref"),
                "prompt": payload.get("prompt"),
                "metadata_json": json_dumps(payload.get("metadata") or {}),
                "external_id": payload.get("external_id"),
                "created_at": now,
                "updated_at": now,
            },
        )
        self.audit("session", session_id, "task_recorded", "Task metadata recorded", {"task_id": task_id})
        return self.get_task(task_id) or {}

    def finalize_session(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self.db.get("sessions", session_id)
        if not session:
            raise KeyError(session_id)
        root = self.settings.artifacts_dir / "sessions" / session_id
        write_json(root / "DONE.json", {"session_id": session_id, "state": "done", "finalized_at": utc_iso(), "note": payload.get("note")})
        digest = hashlib.sha256((root / "DONE.json").read_bytes()).hexdigest()
        (root / "checksums.sha256").write_text(f"{digest}  DONE.json\n", encoding="utf-8")
        self._record_artifact(session_id, None, "done_marker", root / "DONE.json")
        self.db.update("sessions", session_id, {"state": "finalized", "phase": "finalized", "updated_at": utc_iso()})
        self.audit("session", session_id, "finalized", "Session finalized and DONE marker written", {"root": str(root)})
        return self.get_session(session_id) or {}

    def reconcile_orphan_resources(self) -> dict[str, Any]:
        """One-shot startup sweep for resources no running code path can reclaim.

        After a controller restart all in-memory workflow/race threads are gone.
        Pods and volumes whose owning session is terminal (or absent, e.g.
        dependency probes) would otherwise bill forever. Resources of active
        sessions are left untouched: an interactive session survives a restart.
        Sessions stranded mid final collection are handed back to the watchdog."""
        results: dict[str, Any] = {"pods_deleted": [], "volumes_deleted": [], "sessions_reset": [], "errors": []}
        keep_volume_states = {"output_collection_failed_keep_volume", "output_collection_empty_keep_volume"}
        orphan_session_states = {"reclaimed", "finalized", "failed", "cleanup_failed"}
        sessions = {row["id"]: row for row in self.db.query("SELECT * FROM sessions")}
        for session in sessions.values():
            if str(session.get("state") or "") == "collecting_outputs":
                self.db.update(
                    "sessions",
                    session["id"],
                    {
                        "state": "reclaim_pending",
                        "phase": "reclaim_pending",
                        "output_collection_state": "interrupted",
                        "watchdog_last_reason": "output_collection_interrupted_by_restart",
                        "updated_at": utc_iso(),
                    },
                )
                session["state"] = "reclaim_pending"
                results["sessions_reset"].append(session["id"])
            elif str(session.get("output_collection_state") or "") == "running":
                # No collection thread survives a restart; a stale 'running' marker
                # would exclude the session from the periodic collector forever.
                self.db.update(
                    "sessions",
                    session["id"],
                    {"output_collection_state": "interrupted", "updated_at": utc_iso()},
                )
                results["sessions_reset"].append(session["id"])

        def orphaned(session_id: Any) -> bool:
            if not session_id:
                return True
            session = sessions.get(str(session_id))
            if not session:
                return True
            return str(session.get("state") or "") in orphan_session_states

        for pod in self.db.query("SELECT * FROM pods WHERE state != 'deleted'"):
            if not orphaned(pod.get("session_id")):
                continue
            error = self._delete_pod_record(pod)
            if error:
                results["errors"].append(error)
            else:
                results["pods_deleted"].append(pod["id"])

        volume_owner: dict[str, Any] = {}
        for candidate in self.db.query("SELECT * FROM workflow_candidates WHERE volume_id IS NOT NULL"):
            volume_owner[str(candidate["volume_id"])] = candidate.get("session_id")
        for session in sessions.values():
            if session.get("network_volume_id"):
                volume_owner[str(session["network_volume_id"])] = session["id"]
        for volume in self.db.query("SELECT * FROM network_volumes WHERE state != 'deleted'"):
            if str(volume.get("retention_policy") or "") == "warm":
                # Warm volumes outlive their session by design; the sweep is
                # also their TTL enforcer. Nothing can write to a detached
                # volume after the final collection succeeded, so expiry
                # deletion needs no re-collection.
                expires = parse_iso(volume.get("warm_expires_at"))
                if expires and utc_now() < expires:
                    continue
                error = self._delete_volume_record(volume)
                if error:
                    results["errors"].append(error)
                else:
                    results["volumes_deleted"].append(volume["id"])
                    self.audit(
                        "network_volume",
                        volume["id"],
                        "warm_volume_expired",
                        "Warm volume idle TTL expired; volume deleted",
                        {"warm_expires_at": volume.get("warm_expires_at"), "warm_session_id": volume.get("warm_session_id")},
                    )
                continue
            owner_id = volume_owner.get(str(volume["id"]))
            owner = sessions.get(str(owner_id)) if owner_id else None
            if owner and str(owner.get("state") or "") in keep_volume_states:
                continue
            if not orphaned(owner_id):
                continue
            error = self._delete_volume_record(volume)
            if error:
                results["errors"].append(error)
            else:
                results["volumes_deleted"].append(volume["id"])
        if any(results.values()):
            self.audit("controller", "startup", "orphan_sweep", "Startup orphan reconciliation", results)
        return results

    def _launch_assets_key(self, assets: list[dict[str, Any]]) -> str:
        return sha256_text(json_dumps(normalize_asset_manifest(assets or [])))

    def _session_launch_assets(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT assets_json FROM session_workflows WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        return json_loads(rows[0].get("assets_json"), []) if rows else []

    def _mark_volume_warm(self, volume: dict[str, Any], session_id: str) -> None:
        expires = (utc_now() + dt.timedelta(hours=self.settings.warm_volume_idle_ttl_hours)).isoformat()
        self.db.update(
            "network_volumes",
            volume["id"],
            {
                "retention_policy": "warm",
                "warm_expires_at": expires,
                "warm_assets_key": self._launch_assets_key(self._session_launch_assets(session_id)),
                "warm_session_id": session_id,
                "updated_at": utc_iso(),
            },
        )
        self.audit(
            "network_volume",
            volume["id"],
            "volume_kept_warm",
            "Hydrated network volume kept warm for reuse",
            {"session_id": session_id, "warm_expires_at": expires, "size_gb": volume.get("size_gb"), "data_center_id": volume.get("data_center_id")},
        )

    def _claim_warm_volume(self, assets: list[dict[str, Any]], size_gb: int, data_centers: list[str]) -> dict[str, Any] | None:
        if not assets:
            return None
        key = self._launch_assets_key(assets)
        rows = self.db.query(
            "SELECT * FROM network_volumes WHERE retention_policy = 'warm' AND state != 'deleted' AND warm_assets_key = ? ORDER BY updated_at DESC",
            (key,),
        )
        now = utc_now()
        for volume in rows:
            expires = parse_iso(volume.get("warm_expires_at"))
            if not expires or now >= expires:
                continue
            if str(volume.get("data_center_id")) not in data_centers:
                continue
            if int(volume.get("size_gb") or 0) < int(size_gb or 0):
                continue
            self.db.update(
                "network_volumes",
                volume["id"],
                {"retention_policy": "delete_after_collection", "warm_expires_at": None, "warm_session_id": None, "updated_at": utc_iso()},
            )
            return self.db.get("network_volumes", volume["id"])
        return None

    def _complete_warm_hydration(self, hydration_id: str, volume_id: str) -> dict[str, Any] | None:
        now = utc_iso()
        ttl_until = (utc_now() + dt.timedelta(hours=self.settings.hydration_ttl_hours)).isoformat()
        self.db.update(
            "hydration_requests",
            hydration_id,
            {"state": "hydrated", "ttl_until": ttl_until, "completed_at": now, "updated_at": now},
        )
        self.db.update(
            "network_volumes",
            volume_id,
            {"hydration_state": "hydrated", "hydration_ttl_until": ttl_until, "state": "hydrated", "updated_at": now},
        )
        self.audit("hydration", hydration_id, "hydrated", "Warm volume reused; CPU hydration skipped", {"volume_id": volume_id})
        return self.db.get("hydration_requests", hydration_id)

    def delete_warm_volume(self, volume_id: str) -> dict[str, Any]:
        volume = self.db.get("network_volumes", volume_id)
        if not volume:
            raise KeyError(volume_id)
        if volume.get("state") == "deleted":
            return {"ok": True, "id": volume_id, "state": "deleted"}
        if str(volume.get("retention_policy") or "") != "warm":
            return {"ok": False, "id": volume_id, "error": "volume_not_warm"}
        error = self._delete_volume_record(volume)
        if error:
            return {"ok": False, "id": volume_id, "error": error}
        self.audit("network_volume", volume_id, "warm_volume_deleted", "Warm volume deleted manually", {"warm_session_id": volume.get("warm_session_id")})
        return {"ok": True, "id": volume_id, "state": "deleted"}

    def reclaim_session(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self.db.get("sessions", session_id)
        if not session:
            raise KeyError(session_id)
        try:
            if session.get("product") == "comfyui" and session.get("network_volume_id") and session.get("retention_policy") == "delete_after_collection":
                return self._safe_reclaim_comfyui_session(session_id, payload)
            return self._legacy_reclaim_session(session_id, payload)
        except Exception as exc:  # noqa: BLE001
            # A reclaim must never strand a session in a transient state such as
            # collecting_outputs: surface it as cleanup_failed so the operator
            # (and a later forced retry) can recover it.
            error = repr(exc)
            self.db.update("sessions", session_id, {"state": "cleanup_failed", "phase": "cleanup_failed", "watchdog_last_reason": error, "updated_at": utc_iso()})
            self._refresh_session_estimated_rollup(session_id)
            self.audit("session", session_id, "cleanup_failed", "Session reclaim raised unexpectedly", {"error": error})
            return {"ok": False, "state": "cleanup_failed", "session_id": session_id, "errors": [error]}

    def _outputs_required_for_reclaim(self, session: dict[str, Any]) -> bool:
        interactive_states = {"interactive_ready", "tunnel_ready", "reclaim_pending"}
        retained_states = {"output_collection_failed_keep_volume", "output_collection_empty_keep_volume"}
        if str(session.get("state") or "") in interactive_states | retained_states:
            return True
        if str(session.get("phase") or "") in interactive_states | retained_states:
            return True
        return self._collected_output_count(str(session["id"])) > 0

    def _cleanup_session_candidates(self, session_id: str) -> list[str]:
        """Delete resources owned by non-winner candidates of this session's workflows.

        While the candidate race is running, the session row does not reference the
        candidates' volumes and pods, so a reclaim that only used the session row
        would leak every in-flight candidate resource."""
        errors: list[str] = []
        for workflow in self.db.query("SELECT * FROM session_workflows WHERE session_id = ?", (session_id,)):
            for candidate in self.db.query("SELECT * FROM workflow_candidates WHERE workflow_id = ?", (workflow["id"],)):
                if candidate.get("state") in {"won", "deleted"}:
                    continue
                result = self._cleanup_loser(candidate["id"], session_id=session_id, workflow_id=workflow["id"])
                errors.extend(result.get("errors") or [])
        return errors

    def _safe_reclaim_comfyui_session(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self.db.get("sessions", session_id)
        if not session:
            raise KeyError(session_id)
        force = bool(payload.get("force"))
        if session["state"] not in {"finalized", "hydrated_cpu_ready"} and not force:
            return {"ok": False, "state": session["state"], "reason": "session_not_finalized_pass_force_to_reclaim"}
        errors = []
        now = utc_iso()
        for tunnel in self.db.query("SELECT * FROM tunnels WHERE session_id = ? AND state NOT IN ('closed', 'failed')", (session_id,)):
            self.db.update("tunnels", tunnel["id"], {"state": "closed", "updated_at": now})
        errors.extend(self._cleanup_session_candidates(session_id))
        gpu_pod_id = session.get("gpu_pod_id")
        if gpu_pod_id:
            pod = self.db.get("pods", gpu_pod_id)
            if pod and pod["state"] != "deleted":
                error = self._delete_pod_record(pod)
                if error:
                    errors.append(error)
        if errors:
            self.db.update("sessions", session_id, {"state": "cleanup_failed", "phase": "cleanup_failed", "watchdog_last_reason": "; ".join(errors), "updated_at": utc_iso()})
            self._refresh_session_estimated_rollup(session_id)
            self.audit("session", session_id, "cleanup_failed", "GPU Pod reclaim failed before output collection", {"force": force, "errors": errors})
            return {"ok": False, "state": "cleanup_failed", "session_id": session_id, "errors": errors}

        outputs_required = self._outputs_required_for_reclaim(session) and not bool(payload.get("discard_outputs"))
        self.db.update("sessions", session_id, {"state": "collecting_outputs", "phase": "collecting_outputs", "output_collection_state": "running", "updated_at": utc_iso()})
        collection = self.collect_session_outputs(session_id, mode="final")
        session_after_collection = self.db.get("sessions", session_id) or session
        volume = self.db.get("network_volumes", session_after_collection["network_volume_id"]) if session_after_collection.get("network_volume_id") else None
        output_count = self._collected_output_count(session_id)
        final_output_collection_state = "succeeded"
        if not collection.get("ok"):
            if not outputs_required:
                final_output_collection_state = "failed_no_output_required"
                self.audit(
                    "session",
                    session_id,
                    "output_collection_failed_delete_allowed",
                    "Output collection failed before the session became interactive; deleting volume after retaining DB logs",
                    {"collection": collection, "previous_state": session.get("state"), "previous_phase": session.get("phase")},
                )
            else:
                state = "output_collection_failed_keep_volume"
                self.db.update(
                    "sessions",
                    session_id,
                    {
                        "state": state,
                        "phase": state,
                        "output_collection_state": "failed",
                        "output_collection_retained_volume": 1,
                        "watchdog_last_reason": str(collection.get("error") or "output_collection_failed"),
                        "updated_at": utc_iso(),
                    },
                )
                self._refresh_session_estimated_rollup(session_id)
                return {"ok": False, "state": state, "session_id": session_id, "collection": collection, "volume_retained": True}
        if output_count <= 0 and outputs_required:
            state = "output_collection_empty_keep_volume"
            self.db.update(
                "sessions",
                session_id,
                {
                    "state": state,
                    "phase": state,
                    "output_collection_state": "empty",
                    "output_collection_retained_volume": 1,
                    "watchdog_last_reason": "no_collected_output_artifact",
                    "updated_at": utc_iso(),
                },
            )
            self._refresh_session_estimated_rollup(session_id)
            self.audit("session", session_id, "output_collection_empty_keep_volume", "No ComfyUI output artifacts found; network volume retained", {"collection": collection})
            return {"ok": False, "state": state, "session_id": session_id, "collection": collection, "volume_retained": True}
        if output_count <= 0:
            if final_output_collection_state == "succeeded":
                final_output_collection_state = "empty_no_output_required"
            self.audit(
                "session",
                session_id,
                "output_collection_empty_delete_allowed",
                "No output artifacts found before the session became interactive; network volume deletion is allowed",
                {"collection": collection, "previous_state": session.get("state"), "previous_phase": session.get("phase")},
            )

        cpu_pod_id = session_after_collection.get("cpu_pod_id")
        if cpu_pod_id:
            pod = self.db.get("pods", cpu_pod_id)
            if pod and pod["state"] != "deleted":
                error = self._delete_pod_record(pod)
                if error:
                    errors.append(error)
        # discard_outputs means "get rid of this volume's data"; keeping it warm
        # would contradict that, so keep only applies to the normal path.
        keep_volume = bool(payload.get("keep_volume")) and not bool(payload.get("discard_outputs"))
        volume_kept_warm = False
        if volume and volume["state"] != "deleted":
            if keep_volume:
                self._mark_volume_warm(volume, session_id)
                volume_kept_warm = True
            else:
                error = self._delete_volume_record(volume)
                if error:
                    errors.append(error)
        if errors:
            self.db.update("sessions", session_id, {"state": "cleanup_failed", "phase": "cleanup_failed", "watchdog_last_reason": "; ".join(errors), "updated_at": utc_iso()})
            self._refresh_session_estimated_rollup(session_id)
            self.audit("session", session_id, "cleanup_failed", "Session reclaim did not delete all provider resources after output collection", {"force": force, "errors": errors, "collection": collection})
            return {"ok": False, "state": "cleanup_failed", "session_id": session_id, "errors": errors, "collection": collection}
        self.db.update(
            "sessions",
            session_id,
            {
                "state": "reclaimed",
                "phase": "reclaimed",
                "output_collection_state": final_output_collection_state,
                "output_collection_retained_volume": 0,
                "updated_at": utc_iso(),
            },
        )
        self._refresh_session_estimated_rollup(session_id)
        self.audit("session", session_id, "reclaimed", "Session resources reclaimed after output collection", {"force": force, "collection": collection, "volume_kept_warm": volume_kept_warm})
        return {"ok": True, "state": "reclaimed", "session_id": session_id, "collection": collection, "volume_kept_warm": volume_kept_warm}

    def _legacy_reclaim_session(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self.db.get("sessions", session_id)
        if not session:
            raise KeyError(session_id)
        force = bool(payload.get("force"))
        if session["state"] not in {"finalized", "hydrated_cpu_ready"} and not force:
            return {"ok": False, "state": session["state"], "reason": "session_not_finalized_pass_force_to_reclaim"}
        errors = []
        errors.extend(self._cleanup_session_candidates(session_id))
        for pod_col in ["gpu_pod_id", "cpu_pod_id"]:
            pod_id = session.get(pod_col)
            if pod_id:
                pod = self.db.get("pods", pod_id)
                if pod and pod["state"] != "deleted":
                    error = self._delete_pod_record(pod)
                    if error:
                        errors.append(error)
        volume = self.db.get("network_volumes", session["network_volume_id"]) if session.get("network_volume_id") else None
        if volume and session["retention_policy"] == "delete_after_collection":
            error = self._delete_volume_record(volume)
            if error:
                errors.append(error)
        if errors:
            self.db.update("sessions", session_id, {"state": "cleanup_failed", "phase": "cleanup_failed", "watchdog_last_reason": "; ".join(errors), "updated_at": utc_iso()})
            self._refresh_session_estimated_rollup(session_id)
            self.audit("session", session_id, "cleanup_failed", "Session reclaim did not delete all provider resources", {"force": force, "errors": errors})
            return {"ok": False, "state": "cleanup_failed", "session_id": session_id, "errors": errors}
        for tunnel in self.db.query("SELECT * FROM tunnels WHERE session_id = ? AND state NOT IN ('closed', 'failed')", (session_id,)):
            self.db.update("tunnels", tunnel["id"], {"state": "closed", "updated_at": utc_iso()})
        self.db.update("sessions", session_id, {"state": "reclaimed", "phase": "reclaimed", "updated_at": utc_iso()})
        self._refresh_session_estimated_rollup(session_id)
        self.audit("session", session_id, "reclaimed", "Session resources reclaimed", {"force": force})
        return {"ok": True, "state": "reclaimed", "session_id": session_id}

    def set_watchdog_paused(self, session_id: str, paused: bool) -> dict[str, Any]:
        if not self.db.get("sessions", session_id):
            raise KeyError(session_id)
        self.db.update(
            "sessions",
            session_id,
            {
                "watchdog_paused": 1 if paused else 0,
                "watchdog_last_reason": "paused" if paused else "resumed",
                "updated_at": utc_iso(),
            },
        )
        self.audit("session", session_id, "watchdog_paused" if paused else "watchdog_resumed", "Watchdog pause state changed", {"paused": paused})
        return self.get_session(session_id) or {}

    def watchdog_tick(self, session_id: str, observed: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self.db.get("sessions", session_id)
        if not session:
            raise KeyError(session_id)
        observed = observed or {}
        now = utc_now()
        reason = "healthy"
        action = "none"
        if int(session.get("watchdog_paused") or 0):
            reason = "paused"
        elif session["state"] in {"reclaimed", "finalized", "failed", "cleanup_failed", "output_collection_failed_keep_volume", "output_collection_empty_keep_volume", "collecting_outputs"}:
            reason = "terminal"
        else:
            idle_at = parse_iso(session.get("idle_shutdown_at"))
            warning_at = parse_iso(session.get("reclaim_warning_at"))
            queue_active = bool(observed.get("queue_active"))
            output_active = bool(observed.get("output_active"))
            lease_active = parse_iso(session.get("lease_until")) and parse_iso(session.get("lease_until")) > now
            idle_clear = not queue_active and not output_active and not lease_active
            # The hard cap is on money, not time: an actively used session is never
            # killed by the clock, only by reaching its own max_total_usd budget.
            max_total = float(session.get("max_total_usd") or 0)
            spend = self._session_live_spend(session) if max_total > 0 else 0.0
            if max_total > 0 and spend >= max_total:
                reason = "cost_cap_reached"
                action = "reclaim"
            elif max_total > 0 and spend >= 0.9 * max_total and session["state"] not in {"reclaim_pending"}:
                reason = "cost_cap_warning"
                action = "warn"
            elif idle_at and now >= idle_at and idle_clear:
                reason = "idle_shutdown_reached"
                action = "reclaim"
            elif warning_at and now >= warning_at and idle_clear and session["state"] not in {"reclaim_pending"}:
                reason = "idle_warning_window"
                action = "warn"
        # Never write the state/phase we read back: a tick races concurrent
        # transitions (reclaim, winner configuration) and would revert them.
        self.db.update(
            "sessions",
            session_id,
            {
                "watchdog_last_checked_at": now.isoformat(),
                "watchdog_last_reason": reason,
                "updated_at": utc_iso(),
            },
        )
        if action == "warn":
            self.db.execute(
                "UPDATE sessions SET state = ?, phase = ?, updated_at = ? WHERE id = ? AND state = ?",
                ("reclaim_pending", "reclaim_pending", utc_iso(), session_id, session["state"]),
            )
        self.db.insert(
            "watchdog_events",
            {
                "id": new_id("wdg"),
                "session_id": session_id,
                "event_type": action,
                "reason": reason,
                "observed_json": json_dumps(observed),
                "created_at": utc_iso(),
            },
        )
        if action == "reclaim":
            if session["state"] != "finalized":
                self.db.update(
                    "sessions",
                    session_id,
                    {
                        "missing_finalization_reason": reason,
                        "updated_at": utc_iso(),
                    },
                )
            # Forced reclaims (cost cap, idle, lease) keep the hydrated volume
            # warm by default: the user will likely relaunch soon.
            reclaim = self.reclaim_session(session_id, {"force": True, "keep_volume": True})
            return {"ok": True, "action": action, "reason": reason, "reclaim": reclaim}
        return {"ok": True, "action": action, "reason": reason, "session": self.get_session(session_id)}

    def sync_billing(self, payload: dict[str, Any]) -> dict[str, Any]:
        bucket_size = str(payload.get("bucket_size") or payload.get("bucketSize") or "hour")
        start_time, end_time = self._billing_window(payload)
        observed_at = utc_iso()
        pod_records = payload.get("pod_records")
        if pod_records is None:
            pod_records = self.adapter.billing_pods(start_time=start_time, end_time=end_time, bucket_size=bucket_size)
        network_volume_records = payload.get("network_volume_records")
        if network_volume_records is None:
            network_volume_records = self.adapter.billing_network_volumes(start_time=start_time, end_time=end_time, bucket_size=bucket_size)
        if not isinstance(pod_records, list):
            raise ValueError("pod_records must be a list when supplied")
        if not isinstance(network_volume_records, list):
            raise ValueError("network_volume_records must be a list when supplied")

        pod_count = self._ingest_pod_billing_records(pod_records, bucket_size=bucket_size, observed_at=observed_at)
        volume_count = self._ingest_network_volume_billing_records(
            network_volume_records,
            bucket_size=bucket_size,
            observed_at=observed_at,
        )
        if volume_count:
            self._mark_account_level_volume_billing(start_time=start_time, end_time=end_time, observed_at=observed_at)
        cpu_absent_count = self._record_cpu_absent_billing_attempts(
            start_time=start_time,
            end_time=end_time,
            observed_at=observed_at,
            observed_provider_ids=self._pod_provider_ids_from_billing_records(pod_records),
        )
        self.audit(
            "billing",
            "runpod",
            "synced",
            "RunPod billing records synced",
            {
                "start_time": start_time,
                "end_time": end_time,
                "bucket_size": bucket_size,
                "pod_records": pod_count,
                "network_volume_records": volume_count,
                "cpu_absent_marked": cpu_absent_count,
            },
        )
        return {
            "ok": True,
            "start_time": start_time,
            "end_time": end_time,
            "bucket_size": bucket_size,
            "pod_records": pod_count,
            "network_volume_records": volume_count,
            "cpu_absent_marked": cpu_absent_count,
            "summary": self.cost_report()["summary"],
        }

    def billing_calibration_candidates(self) -> dict[str, Any]:
        pods = self.db.query(
            """
            SELECT * FROM pods
            WHERE actual_cost_usd IS NULL
              AND (billing_source IS NULL OR billing_source = '')
              AND provider_pod_id NOT LIKE 'fake-%'
              AND (
                stopped_at IS NOT NULL
                OR deleted_at IS NOT NULL
                OR state IN ('stopped', 'deleted', 'failed')
              )
            ORDER BY COALESCE(deleted_at, stopped_at, updated_at, created_at)
            """
        )
        volumes = self.db.query(
            """
            SELECT * FROM network_volumes
            WHERE actual_cost_usd IS NULL
              AND (billing_source IS NULL OR billing_source = '')
              AND provider_volume_id NOT LIKE 'fake-%'
              AND (
                deleted_at IS NOT NULL
                OR state IN ('deleted', 'failed')
              )
            ORDER BY COALESCE(deleted_at, updated_at, created_at)
            """
        )
        return {
            "pods": pods,
            "volumes": volumes,
            "pod_count": len(pods),
            "volume_count": len(volumes),
            "total": len(pods) + len(volumes),
        }

    def run_billing_calibration_worker(
        self,
        *,
        poll_interval_seconds: int | None = None,
        bucket_size: str | None = None,
        max_polls: int | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        watch: bool = False,
    ) -> dict[str, Any]:
        interval = self.settings.billing_worker_poll_interval_seconds if poll_interval_seconds is None else int(poll_interval_seconds)
        bucket = bucket_size or self.settings.billing_worker_bucket_size
        sleep = sleep_fn or self._sleep
        attempts: list[dict[str, Any]] = []
        poll_index = 0
        while True:
            del attempts[:-50]  # watch mode runs indefinitely inside the server
            candidates = self.billing_calibration_candidates()
            if max_polls is not None and poll_index >= max_polls:
                return {
                    "ok": False,
                    "exit_reason": "max_polls_reached",
                    "polls": poll_index,
                    "attempts": attempts,
                    "remaining": candidates,
                }
            if candidates["total"] == 0:
                if watch:
                    attempts.append(
                        {
                            "poll": poll_index + 1,
                            "idle": True,
                            "before": {"pods": 0, "volumes": 0},
                            "after": {"pods": 0, "volumes": 0},
                        }
                    )
                    poll_index += 1
                    sleep(max(0, interval))
                    continue
                return {
                    "ok": True,
                    "exit_reason": "no_uncalibrated_resources",
                    "polls": poll_index,
                    "attempts": attempts,
                    "remaining": candidates,
                }
            start_time, end_time = self._billing_window_for_candidates(candidates)
            sync_result = self.sync_billing({"start_time": start_time, "end_time": end_time, "bucket_size": bucket})
            after = self.billing_calibration_candidates()
            attempts.append(
                {
                    "poll": poll_index + 1,
                    "start_time": start_time,
                    "end_time": end_time,
                    "bucket_size": bucket,
                    "before": {
                        "pods": candidates["pod_count"],
                        "volumes": candidates["volume_count"],
                    },
                    "after": {
                        "pods": after["pod_count"],
                        "volumes": after["volume_count"],
                    },
                    "pod_records": sync_result["pod_records"],
                    "network_volume_records": sync_result["network_volume_records"],
                    "cpu_absent_marked": sync_result["cpu_absent_marked"],
                }
            )
            poll_index += 1
            if after["total"] == 0 and not watch:
                return {
                    "ok": True,
                    "exit_reason": "calibrated",
                    "polls": poll_index,
                    "attempts": attempts,
                    "remaining": after,
                }
            sleep(max(0, interval))

    def _billing_window_for_candidates(self, candidates: dict[str, Any]) -> tuple[str, str]:
        starts: list[dt.datetime] = []
        ends: list[dt.datetime] = []
        for row in [*candidates.get("pods", []), *candidates.get("volumes", [])]:
            start = parse_iso(row.get("created_at"))
            end = parse_iso(row.get("deleted_at") or row.get("stopped_at") or row.get("updated_at"))
            if start:
                starts.append(start - dt.timedelta(hours=1))
            if end:
                ends.append(end + dt.timedelta(hours=1))
        if not starts:
            starts.append(utc_now() - dt.timedelta(days=30))
        if not ends:
            ends.append(utc_now())
        return min(starts).isoformat(), max(ends).isoformat()

    def _sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)

    def list_billing_records(self, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        rows = self.db.query(
            "SELECT * FROM billing_records ORDER BY bucket_start_at DESC, observed_at DESC LIMIT ?",
            (safe_limit,),
        )
        return [self._expand_json_fields(row, ["raw_json"]) for row in rows]

    def _billing_window(self, payload: dict[str, Any]) -> tuple[str, str]:
        end_time = payload.get("end_time") or payload.get("endTime") or utc_iso()
        start_time = payload.get("start_time") or payload.get("startTime")
        if start_time:
            return str(start_time), str(end_time)
        if payload.get("lookback_days"):
            start = utc_now() - dt.timedelta(days=int(payload["lookback_days"]))
            return start.isoformat(), str(end_time)
        starts = []
        for table in ["pods", "network_volumes"]:
            row = self.db.query(f"SELECT MIN(created_at) AS start_at FROM {table}")[0]
            if row.get("start_at"):
                starts.append(row["start_at"])
        parsed = [parse_iso(value) for value in starts]
        parsed = [value for value in parsed if value is not None]
        if parsed:
            return min(parsed).isoformat(), str(end_time)
        return (utc_now() - dt.timedelta(days=30)).isoformat(), str(end_time)

    def _ingest_pod_billing_records(self, records: list[dict[str, Any]], *, bucket_size: str, observed_at: str) -> int:
        local_pods = {
            row["provider_pod_id"]: row["id"]
            for row in self.db.query("SELECT id, provider_pod_id FROM pods")
            if row.get("provider_pod_id")
        }
        providers: set[str] = set()
        count = 0
        for record in records:
            provider_id = str(record.get("podId") or "")
            bucket_start_at = self._normalize_billing_time(record.get("time"))
            if not provider_id or not bucket_start_at:
                continue
            providers.add(provider_id)
            self._upsert_billing_record(
                source="runpod_billing",
                provider="runpod",
                resource_type="pod",
                resource_id=local_pods.get(provider_id),
                provider_resource_id=provider_id,
                bucket_start_at=bucket_start_at,
                bucket_end_at=self._bucket_end_at(bucket_start_at, bucket_size),
                bucket_size=bucket_size,
                amount_usd=self._as_float(record.get("amount")) or 0.0,
                time_billed_ms=self._as_int(record.get("timeBilledMs")),
                disk_space_billed_gb=self._billing_disk_gb(record),
                raw=record,
                observed_at=observed_at,
            )
            count += 1
        self._apply_pod_billing_calibrations(providers, observed_at=observed_at)
        return count

    def _ingest_network_volume_billing_records(self, records: list[dict[str, Any]], *, bucket_size: str, observed_at: str) -> int:
        local_volumes = {
            row["provider_volume_id"]: row["id"]
            for row in self.db.query("SELECT id, provider_volume_id FROM network_volumes")
            if row.get("provider_volume_id")
        }
        providers: set[str] = set()
        count = 0
        for record in records:
            provider_id = self._network_volume_provider_id(record)
            bucket_start_at = self._normalize_billing_time(record.get("time") or record.get("startDate"))
            if not bucket_start_at:
                continue
            resource_type = "network_volume" if provider_id else "network_volume_account"
            provider_resource_id = provider_id or "runpod-account-networkvolumes"
            if provider_id:
                providers.add(provider_id)
            self._upsert_billing_record(
                source="runpod_billing",
                provider="runpod",
                resource_type=resource_type,
                resource_id=local_volumes.get(provider_id) if provider_id else None,
                provider_resource_id=provider_resource_id,
                bucket_start_at=bucket_start_at,
                bucket_end_at=self._bucket_end_at(bucket_start_at, bucket_size),
                bucket_size=bucket_size,
                amount_usd=self._network_volume_amount(record),
                time_billed_ms=self._as_int(record.get("timeBilledMs")),
                disk_space_billed_gb=self._network_volume_disk_gb(record),
                raw=record,
                observed_at=observed_at,
            )
            count += 1
        self._apply_network_volume_billing_calibrations(providers, observed_at=observed_at)
        return count

    def _mark_account_level_volume_billing(self, *, start_time: str, end_time: str, observed_at: str) -> None:
        records = self.db.query(
            """
            SELECT COUNT(*) AS record_count
            FROM billing_records
            WHERE resource_type = 'network_volume_account'
              AND bucket_start_at >= ?
              AND bucket_start_at <= ?
            """,
            (start_time, end_time),
        )
        if not records or int(records[0].get("record_count") or 0) == 0:
            return
        volumes = self.db.query(
            """
            SELECT * FROM network_volumes
            WHERE actual_cost_usd IS NULL
              AND (billing_source IS NULL OR billing_source = '')
              AND provider_volume_id NOT LIKE 'fake-%'
              AND (
                deleted_at IS NOT NULL
                OR state IN ('deleted', 'failed')
              )
              AND created_at <= ?
              AND COALESCE(deleted_at, updated_at, created_at) >= ?
            """,
            (end_time, start_time),
        )
        session_ids: set[str] = set()
        for volume in volumes:
            self.db.update(
                "network_volumes",
                volume["id"],
                {
                    "actual_cost_observed_at": observed_at,
                    "billed_start_at": start_time,
                    "billed_end_at": end_time,
                    "billing_source": "runpod_billing_account",
                    "updated_at": utc_iso(),
                },
            )
            sessions = self.db.query("SELECT id FROM sessions WHERE network_volume_id = ?", (volume["id"],))
            session_ids.update(row["id"] for row in sessions)
        self._refresh_session_billing_rollups(session_ids, observed_at=observed_at)

    def _latest_bucket_size_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only records of one bucket size before summing.

        The dedup key includes bucket_size, so syncing the same period with two
        bucket sizes stores both sets of rows; summing across them would roughly
        double the calibrated cost. Prefer the most recently observed sync."""
        if not records:
            return records
        latest = max(records, key=lambda row: str(row.get("observed_at") or ""))
        bucket_size = str(latest.get("bucket_size") or "")
        return [row for row in records if str(row.get("bucket_size") or "") == bucket_size]

    def _upsert_billing_record(
        self,
        *,
        source: str,
        provider: str,
        resource_type: str,
        resource_id: str | None,
        provider_resource_id: str | None,
        bucket_start_at: str,
        bucket_end_at: str | None,
        bucket_size: str,
        amount_usd: float,
        time_billed_ms: int | None,
        disk_space_billed_gb: float | None,
        raw: dict[str, Any],
        observed_at: str,
    ) -> None:
        record_key = sha256_text(
            json_dumps(
                {
                    "source": source,
                    "provider": provider,
                    "resource_type": resource_type,
                    "provider_resource_id": provider_resource_id or "",
                    "bucket_start_at": bucket_start_at,
                    "bucket_size": bucket_size,
                }
            )
        )
        record_id = f"bil_{record_key[:20]}"
        self.db.execute(
            """
            INSERT OR REPLACE INTO billing_records (
              id, record_key, source, provider, resource_type, resource_id,
              provider_resource_id, bucket_start_at, bucket_end_at, bucket_size,
              amount_usd, time_billed_ms, disk_space_billed_gb, raw_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                record_key,
                source,
                provider,
                resource_type,
                resource_id,
                provider_resource_id,
                bucket_start_at,
                bucket_end_at,
                bucket_size,
                round(float(amount_usd or 0), 6),
                time_billed_ms,
                disk_space_billed_gb,
                json_dumps(redact_secrets(raw)),
                observed_at,
            ),
        )

    def _apply_pod_billing_calibrations(self, provider_ids: set[str], *, observed_at: str) -> None:
        session_ids: set[str] = set()
        for provider_id in provider_ids:
            pods = self.db.query("SELECT * FROM pods WHERE provider_pod_id = ?", (provider_id,))
            if not pods:
                continue
            records = self.db.query(
                "SELECT * FROM billing_records WHERE resource_type = 'pod' AND provider_resource_id = ? ORDER BY bucket_start_at",
                (provider_id,),
            )
            records = self._latest_bucket_size_records(records)
            if not records:
                continue
            amount = round(sum(float(row.get("amount_usd") or 0) for row in records), 6)
            billed_ms_values = [int(row["time_billed_ms"]) for row in records if row.get("time_billed_ms") is not None]
            billed_time_ms = sum(billed_ms_values) if billed_ms_values else None
            billed_start_at = min(row["bucket_start_at"] for row in records if row.get("bucket_start_at"))
            billed_end_values = [row["bucket_end_at"] for row in records if row.get("bucket_end_at")]
            billed_end_at = max(billed_end_values) if billed_end_values else None
            for pod in pods:
                self.db.update(
                    "pods",
                    pod["id"],
                    {
                        "actual_cost_usd": amount,
                        "actual_cost_observed_at": observed_at,
                        "billed_start_at": billed_start_at,
                        "billed_end_at": billed_end_at,
                        "billed_time_ms": billed_time_ms,
                        "billing_source": "runpod_billing",
                        "updated_at": utc_iso(),
                    },
                )
                if pod.get("session_id"):
                    session_ids.add(pod["session_id"])
        self._refresh_session_billing_rollups(session_ids, observed_at=observed_at)

    def _apply_network_volume_billing_calibrations(self, provider_ids: set[str], *, observed_at: str) -> None:
        session_ids: set[str] = set()
        for provider_id in provider_ids:
            volumes = self.db.query("SELECT * FROM network_volumes WHERE provider_volume_id = ?", (provider_id,))
            if not volumes:
                continue
            records = self.db.query(
                "SELECT * FROM billing_records WHERE resource_type = 'network_volume' AND provider_resource_id = ? ORDER BY bucket_start_at",
                (provider_id,),
            )
            records = self._latest_bucket_size_records(records)
            if not records:
                continue
            amount = round(sum(float(row.get("amount_usd") or 0) for row in records), 6)
            billed_ms_values = [int(row["time_billed_ms"]) for row in records if row.get("time_billed_ms") is not None]
            billed_time_ms = sum(billed_ms_values) if billed_ms_values else None
            billed_start_at = min(row["bucket_start_at"] for row in records if row.get("bucket_start_at"))
            billed_end_values = [row["bucket_end_at"] for row in records if row.get("bucket_end_at")]
            billed_end_at = max(billed_end_values) if billed_end_values else None
            for volume in volumes:
                self.db.update(
                    "network_volumes",
                    volume["id"],
                    {
                        "actual_cost_usd": amount,
                        "actual_cost_observed_at": observed_at,
                        "billed_start_at": billed_start_at,
                        "billed_end_at": billed_end_at,
                        "billed_time_ms": billed_time_ms,
                        "billing_source": "runpod_billing",
                        "updated_at": utc_iso(),
                    },
                )
                sessions = self.db.query("SELECT id FROM sessions WHERE network_volume_id = ?", (volume["id"],))
                session_ids.update(row["id"] for row in sessions)
        self._refresh_session_billing_rollups(session_ids, observed_at=observed_at)

    def _refresh_session_billing_rollups(self, session_ids: set[str], *, observed_at: str) -> None:
        for session_id in session_ids:
            pods = [enrich_pod_cost(row) for row in self.db.query("SELECT * FROM pods WHERE session_id = ?", (session_id,))]
            session = self.db.get("sessions", session_id)
            if not session:
                continue
            volume = self.db.get("network_volumes", session["network_volume_id"]) if session.get("network_volume_id") else None
            enriched_volume = enrich_volume_cost(volume) if volume else None
            actual_values = [
                float(row["actual_cost_usd"])
                for row in [*pods, *([enriched_volume] if enriched_volume else [])]
                if row and row.get("actual_cost_usd") is not None
            ]
            if not actual_values:
                continue
            starts = [
                row.get("billed_start_at")
                for row in [*pods, *([enriched_volume] if enriched_volume else [])]
                if row and row.get("billed_start_at")
            ]
            ends = [
                row.get("billed_end_at")
                for row in [*pods, *([enriched_volume] if enriched_volume else [])]
                if row and row.get("billed_end_at")
            ]
            self.db.update(
                "sessions",
                session_id,
                {
                    "actual_cost_usd": round(sum(actual_values), 6),
                    "actual_cost_observed_at": observed_at,
                    "billed_start_at": min(starts) if starts else None,
                    "billed_end_at": max(ends) if ends else None,
                    "updated_at": utc_iso(),
                },
            )

    def _normalize_billing_time(self, value: Any) -> str | None:
        parsed = parse_iso(str(value)) if value is not None else None
        return parsed.isoformat() if parsed else None

    def _pod_provider_ids_from_billing_records(self, records: list[dict[str, Any]]) -> set[str]:
        return {str(record.get("podId") or "") for record in records if record.get("podId")}

    def _record_cpu_absent_billing_attempts(
        self,
        *,
        start_time: str,
        end_time: str,
        observed_at: str,
        observed_provider_ids: set[str],
    ) -> int:
        window_start = parse_iso(start_time)
        window_end = parse_iso(end_time)
        observed = parse_iso(observed_at) or utc_now()
        if not window_start or not window_end:
            return 0
        grace_hours = max(0, int(self.settings.billing_cpu_absent_grace_hours))
        grace = dt.timedelta(hours=grace_hours)
        candidates = self.db.query(
            """
            SELECT * FROM pods
            WHERE compute_type = 'CPU'
              AND actual_cost_usd IS NULL
              AND (billing_source IS NULL OR billing_source = '')
              AND provider_pod_id NOT LIKE 'fake-%'
              AND (
                stopped_at IS NOT NULL
                OR deleted_at IS NOT NULL
                OR state IN ('stopped', 'deleted', 'failed')
              )
            ORDER BY COALESCE(deleted_at, stopped_at, updated_at, created_at)
            """
        )
        marked = 0
        for pod in candidates:
            provider_id = str(pod.get("provider_pod_id") or "")
            if not provider_id or provider_id in observed_provider_ids:
                continue
            created_at = parse_iso(pod.get("created_at"))
            terminal_at = parse_iso(pod.get("deleted_at") or pod.get("stopped_at") or pod.get("updated_at"))
            if not created_at or not terminal_at:
                continue
            if observed < terminal_at + grace:
                continue
            if window_start > created_at or window_end < terminal_at:
                continue
            self.db.update(
                "pods",
                pod["id"],
                {
                    "billing_source": "runpod_billing_absent",
                    "updated_at": utc_iso(),
                },
            )
            marked += 1
        return marked

    def _bucket_end_at(self, bucket_start_at: str, bucket_size: str) -> str | None:
        start = parse_iso(bucket_start_at)
        if not start:
            return None
        if bucket_size == "hour":
            delta = dt.timedelta(hours=1)
        elif bucket_size == "day":
            delta = dt.timedelta(days=1)
        elif bucket_size == "week":
            delta = dt.timedelta(weeks=1)
        elif bucket_size == "month":
            delta = dt.timedelta(days=31)
        elif bucket_size == "year":
            delta = dt.timedelta(days=366)
        else:
            delta = dt.timedelta(hours=1)
        return (start + delta).isoformat()

    def _network_volume_provider_id(self, record: dict[str, Any]) -> str | None:
        for key in ["networkVolumeId", "networkVolumeID", "volumeId", "network_volume_id"]:
            if record.get(key):
                return str(record[key])
        return None

    def _network_volume_amount(self, record: dict[str, Any]) -> float:
        return self._as_float(record.get("amount")) or 0.0

    def _network_volume_disk_gb(self, record: dict[str, Any]) -> float | None:
        disk = self._billing_disk_gb(record)
        high_perf_disk = self._as_float(record.get("highPerformanceStorageDiskSpaceBilledGb"))
        if high_perf_disk is None:
            high_perf_disk = self._as_float(record.get("highPerformanceStorageDiskSpaceBilledGB"))
        if disk is None:
            return high_perf_disk
        if high_perf_disk is None:
            return disk
        return disk + high_perf_disk

    def _billing_disk_gb(self, record: dict[str, Any]) -> float | None:
        disk = self._as_float(record.get("diskSpaceBilledGb"))
        if disk is not None:
            return disk
        return self._as_float(record.get("diskSpaceBilledGB"))

    def _as_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _as_int(self, value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def get_resource_request(self, request_id: str) -> dict[str, Any] | None:
        row = self.db.get("resource_requests", request_id)
        return self._expand_json_fields(row, ["requested_json", "result_json", "failure_json"]) if row else None

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.db.get("sessions", session_id)
        if not row:
            return None
        workflow = self._latest_workflow(session_id)
        if workflow:
            workflow = self._expand_json_fields(
                workflow,
                [
                    "selected_data_centers_json",
                    "excluded_data_centers_json",
                    "assets_json",
                    "ui_workflow_json",
                    "api_workflow_json",
                    "analyzer_result_json",
                    "probe_result_json",
                    "custom_nodes_json",
                    "install_plan_json",
                    "validation_plan_json",
                ],
            )
            if workflow.get("launch_template_id"):
                workflow["launch_template"] = self.get_comfyui_launch_template(str(workflow["launch_template_id"]))
            if workflow.get("comfyui_workflow_id"):
                workflow["comfyui_workflow"] = self.get_comfyui_workflow(str(workflow["comfyui_workflow_id"]))
            candidates = self.db.query("SELECT * FROM workflow_candidates WHERE workflow_id = ? ORDER BY created_at", (workflow["id"],))
            candidate_datacenters = {candidate["id"]: candidate.get("data_center_id") for candidate in candidates}
            events = []
            for event in self.db.query("SELECT * FROM workflow_events WHERE workflow_id = ? ORDER BY created_at", (workflow["id"],)):
                public_event = self._expand_json_fields(event, ["details_json"])
                public_event["data_center_id"] = candidate_datacenters.get(public_event.get("candidate_id")) or ""
                events.append(public_event)
            workflow["candidates"] = candidates
            workflow["events"] = events
            row["workflow"] = workflow
            row["candidates"] = candidates
            row["workflow_events"] = events
            row["candidate_count"] = len(candidates)
            row["winner_candidate_id"] = workflow.get("winner_candidate_id")
        else:
            row["workflow"] = None
            row["candidates"] = []
            row["workflow_events"] = []
            row["candidate_count"] = 0
            row["winner_candidate_id"] = None
        row["tasks"] = self.db.query("SELECT * FROM tasks WHERE session_id = ? ORDER BY created_at", (session_id,))
        row["artifacts"] = self.db.query("SELECT * FROM artifacts WHERE session_id = ? ORDER BY created_at", (session_id,))
        row["output_artifacts"] = self.db.query("SELECT * FROM artifacts WHERE session_id = ? AND kind = 'comfyui_output' ORDER BY created_at", (session_id,))
        row["output_collections"] = self.db.query("SELECT * FROM output_collections WHERE session_id = ? ORDER BY started_at DESC LIMIT 50", (session_id,))
        row["output_summary"] = {
            "file_count": len(row["output_artifacts"]),
            "bytes": sum(int(artifact.get("size_bytes") or 0) for artifact in row["output_artifacts"]),
            "state": row.get("output_collection_state"),
            "last_checked_at": row.get("output_collection_last_checked_at"),
            "last_error": row.get("output_collection_last_error"),
            "retained_volume": bool(row.get("output_collection_retained_volume")),
        }
        row["model_operations"] = self.list_session_model_operations(session_id)
        row["audit_events"] = self.db.query("SELECT * FROM audit_events WHERE subject_type = 'session' AND subject_id = ? ORDER BY created_at", (session_id,))
        row["gpu_acquisition_attempts"] = [
            self._expand_json_fields(attempt, ["raw_json"])
            for attempt in self.db.query("SELECT * FROM gpu_acquisition_attempts WHERE session_id = ? ORDER BY attempt_number", (session_id,))
        ]
        row["tunnels"] = [self._serialize_tunnel(tunnel) for tunnel in self.db.query("SELECT * FROM tunnels WHERE session_id = ? ORDER BY created_at DESC", (session_id,))]
        row["watchdog_events"] = [
            self._expand_json_fields(event, ["observed_json"])
            for event in self.db.query("SELECT * FROM watchdog_events WHERE session_id = ? ORDER BY created_at DESC LIMIT 50", (session_id,))
        ]
        pods = [
            self._public_resource_row(enrich_pod_cost(pod))
            for pod in self.db.query("SELECT * FROM pods WHERE session_id = ? ORDER BY created_at", (session_id,))
        ]
        volume = self.db.get("network_volumes", row["network_volume_id"]) if row.get("network_volume_id") else None
        enriched_volume = self._public_resource_row(enrich_volume_cost(volume)) if volume else None
        row["pods"] = pods
        row["volume"] = enriched_volume
        row.update(self._session_cost_rollup(row, pods, enriched_volume))
        aggregate_seconds = round(sum(float(pod.get("runtime_seconds") or 0) for pod in pods), 3)
        elapsed_seconds = self._session_elapsed_seconds(row, [*pods, enriched_volume])
        row["elapsed_seconds"] = elapsed_seconds
        row["elapsed"] = self._format_runtime(elapsed_seconds)
        row["aggregate_pod_runtime_seconds"] = aggregate_seconds
        row["aggregate_pod_runtime"] = self._format_runtime(aggregate_seconds)
        row["runtime_seconds"] = elapsed_seconds
        row["runtime"] = row["elapsed"]
        row.update(self._session_runtime_status(row))
        return row

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.get("tasks", task_id)
        return self._expand_json_fields(row, ["metadata_json"]) if row else None

    def get_hydration(self, hydration_id: str) -> dict[str, Any] | None:
        row = self.db.get("hydration_requests", hydration_id)
        return self._expand_json_fields(row, ["assets_json", "install_plan_json", "validation_plan_json", "ui_workflow_json", "api_workflow_json", "custom_nodes_json"]) if row else None

    def get_volume_hydration(self, volume_id: str) -> dict[str, Any] | None:
        volume = self.db.get("network_volumes", volume_id)
        if volume is None:
            matches = self.db.query("SELECT * FROM network_volumes WHERE provider_volume_id = ?", (volume_id,))
            volume = matches[0] if matches else None
        if not volume:
            return None
        hydrations = self.db.query("SELECT * FROM hydration_requests WHERE volume_id = ? ORDER BY created_at DESC", (volume["id"],))
        return {"volume": volume, "hydrations": [self._expand_json_fields(row, ["assets_json", "install_plan_json", "validation_plan_json", "ui_workflow_json", "api_workflow_json", "custom_nodes_json"]) for row in hydrations]}

    def list_sessions(self) -> list[dict[str, Any]]:
        now = utc_now()
        pods = [enrich_pod_cost(row, now=now) for row in self.db.query("SELECT * FROM pods ORDER BY created_at DESC")]
        volumes = {row["id"]: enrich_volume_cost(row, now=now) for row in self.db.query("SELECT * FROM network_volumes ORDER BY created_at DESC")}
        pods_by_session: dict[str, list[dict[str, Any]]] = {}
        for pod in pods:
            if pod.get("session_id"):
                pods_by_session.setdefault(pod["session_id"], []).append(pod)
        sessions = []
        for session in self.db.query("SELECT * FROM sessions ORDER BY created_at DESC"):
            session_pods = pods_by_session.get(session["id"], [])
            volume = volumes.get(session.get("network_volume_id"))
            enriched = dict(session)
            enriched.update(self._session_cost_rollup(enriched, session_pods, volume))
            aggregate_seconds = round(sum(float(pod.get("runtime_seconds") or 0) for pod in session_pods), 3)
            elapsed_seconds = self._session_elapsed_seconds(enriched, [*session_pods, volume])
            enriched["elapsed_seconds"] = elapsed_seconds
            enriched["elapsed"] = self._format_runtime(elapsed_seconds)
            enriched["aggregate_pod_runtime_seconds"] = aggregate_seconds
            enriched["aggregate_pod_runtime"] = self._format_runtime(aggregate_seconds)
            enriched["runtime_seconds"] = elapsed_seconds
            enriched["runtime"] = enriched["elapsed"]
            attempts = self.db.query("SELECT COUNT(*) AS n FROM gpu_acquisition_attempts WHERE session_id = ?", (session["id"],))
            candidates = self.db.query("SELECT COUNT(*) AS n FROM workflow_candidates WHERE session_id = ?", (session["id"],))
            tunnel = self.db.query("SELECT * FROM tunnels WHERE session_id = ? ORDER BY created_at DESC LIMIT 1", (session["id"],))
            enriched["gpu_attempts"] = int(attempts[0].get("n") or 0) if attempts else 0
            enriched["candidate_count"] = int(candidates[0].get("n") or 0) if candidates else 0
            enriched["tunnel_status"] = tunnel[0]["state"] if tunnel else ""
            enriched["local_ui_url"] = self._serialize_tunnel(tunnel[0])["local_url"] if tunnel else enriched.get("ui_url")
            enriched.update(self._session_runtime_status(enriched))
            sessions.append(enriched)
        return sessions

    def list_pods(self) -> list[dict[str, Any]]:
        now = utc_now()
        return [self._public_resource_row(enrich_pod_cost(row, now=now)) for row in self.db.query("SELECT * FROM pods ORDER BY created_at DESC")]

    def list_volumes(self) -> list[dict[str, Any]]:
        now = utc_now()
        return [self._public_resource_row(enrich_volume_cost(row, now=now)) for row in self.db.query("SELECT * FROM network_volumes ORDER BY created_at DESC")]

    def cost_report(self) -> dict[str, Any]:
        sessions = self.list_sessions()
        volumes = self.list_volumes()
        pods = self.list_pods()
        compute_cost = round(sum(float(item.get("estimated_cost_usd") or 0) for item in pods), 6)
        storage_cost = round(sum(float(item.get("estimated_cost_usd") or 0) for item in volumes), 6)
        effective_compute_cost = round(sum(float(item.get("effective_cost_usd") or 0) for item in pods), 6)
        storage_billing = self._network_volume_account_billing_summary()
        matched_storage_actual = round(
            sum(float(item.get("actual_cost_usd") or 0) for item in volumes if item.get("actual_cost_usd") is not None),
            6,
        )
        if matched_storage_actual:
            effective_storage_cost = matched_storage_actual
            storage_cost_source = "runpod_billing"
        elif storage_billing["record_count"]:
            effective_storage_cost = storage_billing["amount_usd"]
            storage_cost_source = "runpod_billing_account"
        else:
            effective_storage_cost = storage_cost
            storage_cost_source = "estimate"
        effective_total = round(effective_compute_cost + effective_storage_cost, 6)
        return {
            "generated_at": utc_iso(),
            "summary": {
                "sessions": len(sessions),
                "active_sessions": sum(1 for item in sessions if item["state"] not in {"reclaimed", "failed"}),
                "pods": len(pods),
                "active_pods": sum(1 for item in pods if item["state"] not in {"deleted", "stopped"}),
                "volumes": len(volumes),
                "active_volumes": sum(1 for item in volumes if item["state"] != "deleted"),
                "estimated_cost_usd": round(compute_cost + storage_cost, 6),
                "estimated_compute_cost_usd": compute_cost,
                "estimated_storage_cost_usd": storage_cost,
                "effective_cost_usd": effective_total,
                "effective_compute_cost_usd": effective_compute_cost,
                "effective_storage_cost_usd": round(effective_storage_cost, 6),
                "compute_cost_source": self._summary_cost_source(pods),
                "storage_cost_source": storage_cost_source,
                "actual_compute_cost_usd": round(
                    sum(float(item.get("actual_cost_usd") or 0) for item in pods if item.get("actual_cost_usd") is not None),
                    6,
                ),
                "actual_storage_cost_usd": matched_storage_actual,
                "runpod_network_volume_billing_usd": storage_billing["amount_usd"],
                "runpod_network_volume_billing_records": storage_billing["record_count"],
            },
            "sessions": sessions,
            "pods": pods,
            "volumes": volumes,
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "product": "comfyui",
            "version": 1,
            "gpu_default": "live creation uses dry-run planning, budget checks, and watchdog cleanup",
            "default_flow": [
                "POST /api/v1/resource-requests",
                "poll GET /api/v1/resource-requests/{id}",
                "GET /api/v1/sessions/{id}",
                "POST /api/v1/sessions/{id}/lease when still using it",
                "POST /api/v1/sessions/{id}/finalize when artifacts are ready",
                "POST /api/v1/sessions/{id}/reclaim to stop GPU, collect outputs, then delete volume",
            ],
            "endpoints": {
                "peek_asset": "POST /api/v1/assets/peek",
                "new_comfyui_page": "GET /comfyui/new",
                "list_comfyui_workflows": "GET /api/v1/comfyui/workflows?product=comfyui",
                "upload_comfyui_workflow": "POST /api/v1/comfyui/workflows/upload",
                "get_comfyui_workflow": "GET /api/v1/comfyui/workflows/{id}",
                "update_comfyui_workflow": "PUT /api/v1/comfyui/workflows/{id}",
                "delete_comfyui_workflow": "DELETE /api/v1/comfyui/workflows/{id}",
                "analyze_comfyui_workflow": "POST /api/v1/comfyui/workflows/analyze",
                "analyze_saved_comfyui_workflow": "POST /api/v1/comfyui/workflows/{id}/analyze",
                "resolve_comfyui_workflow_node": "POST /api/v1/comfyui/workflows/{id}/nodes/resolve",
                "probe_comfyui_workflow": "POST /api/v1/comfyui/workflows/{id}/probe",
                "get_comfyui_probe": "GET /api/v1/comfyui/probes/{id}",
                "dry_run_request": "POST /api/v1/resource-requests/dry-run",
                "create_request": "POST /api/v1/resource-requests",
                "get_request": "GET /api/v1/resource-requests/{id}",
                "get_session": "GET /api/v1/sessions/{id}",
                "collect_outputs": "POST /api/v1/sessions/{id}/outputs/collect",
                "list_outputs": "GET /api/v1/sessions/{id}/outputs",
                "open_outputs_locally": "POST /api/v1/sessions/{id}/outputs/open-local",
                "list_models_tree": "GET /api/v1/sessions/{id}/models/tree",
                "download_runtime_model": "POST /api/v1/sessions/{id}/models/downloads",
                "move_runtime_model": "POST /api/v1/sessions/{id}/models/move",
                "resize_session_volume": "POST /api/v1/sessions/{id}/volume/resize",
                "workflow_page": "GET /sessions/{id}/workflow",
                "terminate_workflow": "POST /api/v1/sessions/{id}/workflow/terminate",
                "mark_workflow_verified": "POST /api/v1/sessions/{id}/workflow/verify",
                "extend_lease": "POST /api/v1/sessions/{id}/lease",
                "record_task": "POST /api/v1/sessions/{id}/tasks",
                "finalize": "POST /api/v1/sessions/{id}/finalize",
                "reclaim": "POST /api/v1/sessions/{id}/reclaim",
                "hydrate_volume": "POST /api/v1/volumes/hydration-requests",
                "get_hydration": "GET /api/v1/volumes/{id}/hydration",
                "promote_to_gpu": "POST /api/v1/sessions/{id}/promote-to-gpu",
                "sync_billing": "POST /api/v1/billing/sync",
                "list_billing_records": "GET /api/v1/billing/records",
                "legacy_list_model_templates": "GET /api/v1/model-templates?product=comfyui",
                "legacy_list_launch_templates": "GET /api/v1/comfyui/templates",
            },
            "workers": {
                "billing_calibration": "python -m controller.billing_worker --watch",
                "billing_calibration_once": "python -m controller.billing_worker --once",
                "default_poll_interval_seconds": self.settings.billing_worker_poll_interval_seconds,
                "billing_cpu_absent_grace_hours": self.settings.billing_cpu_absent_grace_hours,
                "watchdog_poll_interval_seconds": self.settings.watchdog_poll_interval_seconds,
                "output_collector_interval_seconds": self.settings.output_collector_interval_seconds,
            },
            "secrets": {
                "secret_env_file": str(self.settings.secret_env_file),
                "hf_token_configured": bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")),
                "civitai_token_configured": bool(os.environ.get("CIVITAI_TOKEN")),
            },
            "interactive_controls": {
                "promote_to_gpu": "POST /api/v1/sessions/{id}/promote-to-gpu",
                "restart_tunnel": "POST /api/v1/sessions/{id}/tunnel/restart",
                "collect_outputs": "POST /api/v1/sessions/{id}/outputs/collect",
                "list_models_tree": "GET /api/v1/sessions/{id}/models/tree",
                "download_runtime_model": "POST /api/v1/sessions/{id}/models/downloads",
                "move_runtime_model": "POST /api/v1/sessions/{id}/models/move",
                "resize_session_volume": "POST /api/v1/sessions/{id}/volume/resize",
                "watchdog_tick": "POST /api/v1/sessions/{id}/watchdog/tick",
                "pause_watchdog": "POST /api/v1/sessions/{id}/watchdog/pause",
                "resume_watchdog": "POST /api/v1/sessions/{id}/watchdog/resume",
            },
            "ui_test_controls": {
                "bypass_confirm_query": "append ?bypass_confirm=1 to controller pages",
                "bypass_confirm_local_storage": "set localStorage.runpodControllerBypassConfirm = '1'",
                "api_note": "JSON API endpoints never require browser confirmation",
            },
        }

    def _expand_json_fields(self, row: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        expanded = dict(row)
        for field in fields:
            if field in expanded:
                expanded[field.removesuffix("_json")] = json_loads(expanded.pop(field), {})
        return expanded

    def _public_resource_row(self, row: dict[str, Any]) -> dict[str, Any]:
        public = dict(row)
        public.pop("last_payload_json", None)
        return public

    def _redact(self, payload: dict[str, Any]) -> dict[str, Any]:
        def scrub(value: Any, key: str = "") -> Any:
            if any(token in key.upper() for token in ["KEY", "SECRET", "TOKEN", "AUTHORIZATION"]):
                return "<redacted>"
            if isinstance(value, dict):
                return {str(k): scrub(v, str(k)) for k, v in value.items()}
            if isinstance(value, list):
                return [scrub(item, key) for item in value]
            if isinstance(value, str):
                if "://" in value and any(marker in value.lower() for marker in ["token=", "key=", "secret=", "api_key="]):
                    return redact_url(value)
            return value

        return redact_secrets(scrub(payload))

    def _session_estimated_cost(self, session: dict[str, Any], pods: list[dict[str, Any]], volume: dict[str, Any] | None) -> float:
        pod_cost = sum(float(pod.get("estimated_cost_usd") or 0) for pod in pods)
        volume_cost = float((volume or {}).get("estimated_cost_usd") or 0)
        return round(pod_cost + volume_cost, 6)

    def _session_live_spend(self, session: dict[str, Any]) -> float:
        """Current effective spend of one session, recomputed from its resource rows.

        Used by the watchdog cost cap: session-row rollups are only refreshed on
        lifecycle events, but a running GPU accrues cost every tick."""
        now = utc_now()
        pods = [enrich_pod_cost(row, now=now) for row in self.db.query("SELECT * FROM pods WHERE session_id = ?", (session["id"],))]
        volume = None
        if session.get("network_volume_id"):
            volume_row = self.db.get("network_volumes", session["network_volume_id"])
            if volume_row:
                volume = enrich_volume_cost(volume_row, now=now)
        rollup = self._session_cost_rollup(dict(session), pods, volume)
        return float(rollup.get("effective_cost_usd") or 0)

    def _session_cost_rollup(self, session: dict[str, Any], pods: list[dict[str, Any]], volume: dict[str, Any] | None) -> dict[str, Any]:
        resource_rows = [*pods, *([volume] if volume else [])]
        estimated = self._session_estimated_cost(session, pods, volume)
        effective = round(sum(float(row.get("effective_cost_usd") or 0) for row in resource_rows if row), 6)
        actual_values = [
            float(row["actual_cost_usd"])
            for row in resource_rows
            if row and row.get("actual_cost_usd") is not None
        ]
        starts = [row.get("billed_start_at") for row in resource_rows if row and row.get("actual_cost_usd") is not None and row.get("billed_start_at")]
        ends = [row.get("billed_end_at") for row in resource_rows if row and row.get("actual_cost_usd") is not None and row.get("billed_end_at")]
        has_actual = bool(actual_values)
        return {
            "estimated_cost_usd": estimated,
            "actual_cost_usd": round(sum(actual_values), 6) if has_actual else None,
            "effective_cost_usd": effective,
            "cost_source": "runpod_billing" if has_actual else "estimate",
            "effective_start_at": min(starts) if starts else session.get("created_at"),
            "effective_stop_at": max(ends) if ends else None,
        }

    def _refresh_session_estimated_rollup(self, session_id: str) -> None:
        session = self.db.get("sessions", session_id)
        if not session:
            return
        pods = [enrich_pod_cost(row) for row in self.db.query("SELECT * FROM pods WHERE session_id = ?", (session_id,))]
        volume = self.db.get("network_volumes", session["network_volume_id"]) if session.get("network_volume_id") else None
        enriched_volume = enrich_volume_cost(volume) if volume else None
        rollup = self._session_cost_rollup(session, pods, enriched_volume)
        updates = {
            "estimated_cost_usd": rollup["estimated_cost_usd"],
            "updated_at": utc_iso(),
        }
        if rollup.get("actual_cost_usd") is not None:
            updates["actual_cost_usd"] = rollup["actual_cost_usd"]
            updates["actual_cost_observed_at"] = utc_iso()
        self.db.update("sessions", session_id, updates)

    def _network_volume_account_billing_summary(self) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT COUNT(*) AS record_count, COALESCE(SUM(amount_usd), 0) AS amount_usd "
            "FROM billing_records WHERE resource_type = 'network_volume_account'"
        )
        row = rows[0] if rows else {}
        return {
            "record_count": int(row.get("record_count") or 0),
            "amount_usd": round(float(row.get("amount_usd") or 0), 6),
        }

    def _summary_cost_source(self, rows: list[dict[str, Any]]) -> str:
        if rows and all(row.get("actual_cost_usd") is not None for row in rows):
            return "runpod_billing"
        if any(row.get("actual_cost_usd") is not None for row in rows):
            return "mixed"
        return "estimate"

    def _format_runtime(self, seconds: float) -> str:
        return format_runtime(seconds)

    def _session_elapsed_seconds(self, session: dict[str, Any], resources: list[dict[str, Any] | None] | None = None) -> float:
        state = str(session.get("state") or "")
        ended = None
        if state in {"reclaimed", "failed", "cleanup_failed"}:
            # Billing calibration keeps touching updated_at long after the session
            # ended, which inflated elapsed time; prefer real resource end stamps.
            ends = []
            for row in resources or []:
                if not row:
                    continue
                for key in ("deleted_at", "stopped_at"):
                    parsed = parse_iso(row.get(key))
                    if parsed:
                        ends.append(parsed)
            ended = max(ends) if ends else parse_iso(session.get("updated_at"))
        started = parse_iso(session.get("created_at"))
        ended = ended or utc_now()
        if not started or ended < started:
            return 0.0
        return round((ended - started).total_seconds(), 3)

    def _session_runtime_status(self, session: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        idle_at = parse_iso(session.get("idle_shutdown_at"))
        warning_at = parse_iso(session.get("reclaim_warning_at"))
        return {
            "idle_deadline": session.get("idle_shutdown_at"),
            "reclaim_warning_at": session.get("reclaim_warning_at"),
            "idle_seconds_remaining": round((idle_at - now).total_seconds(), 3) if idle_at else None,
            "in_reclaim_warning": bool(warning_at and now >= warning_at),
        }
