from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import subprocess
import zipfile
import threading
import time
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from controller.assets import canonical_asset_url, detect_provider, normalize_asset_manifest, redact_url
from controller.asset_metadata import duplicate_url_keys, peek_url_metadata, normalized_url_key, volume_size_gb_for_assets
from controller.comfyui_workflow import (
    analyze_comfyui_workflow,
    extract_model_requirements,
    launch_template_fingerprint,
    rewrite_workflow_model_references,
    workflow_hash,
)
from controller.costing import enrich_pod_cost, enrich_volume_cost, network_volume_rate_usd_per_hr
from controller.config import Settings, load_settings
from controller.comfyui_registry import registry_node_to_custom_node
from controller.db import Database
from controller.i18n import detect_locale, set_locale
from controller.gpu_catalog import comfyui_gpu_rows, normalize_gpu_vendor, normalize_min_vram_gb, select_comfyui_gpu_type
from controller.runpod import RunpodAdapter, RunpodRestAdapter, ScoutResult
from controller.s3_volume import RunpodS3VolumeClient, S3Object
from controller.service import ControllerService
from controller.web import comfyui_new_page, dashboard, history_page, session_detail, session_models_page, session_outputs_page, workflow_page
from controller.utils import new_id, redact_secrets, utc_iso


def test_settings(root: Path) -> Settings:
    return Settings(
        data_dir=root,
        secret_env_file=root / "secrets" / "controller.env",
        host="127.0.0.1",
        port=0,
        runpod_mode="live",
        product="comfyui",
        default_data_center="US-KS-2",
        default_volume_size_gb=10,
        default_min_vram_gb=24,
        default_gpu_vendor="NVIDIA",
        default_max_gpu_usd_per_hr=1.25,
        default_max_total_usd=5.0,
        default_lease_minutes=120,
        default_cpu_usd_per_hr=0.24,
        workflow_background_threads=False,
        hydration_estimate_minutes=15,
        gpu_acquisition_estimate_minutes=15,
        hydration_poll_interval_seconds=0,
        hydration_timeout_seconds=5,
        hydration_ttl_hours=24,
        billing_worker_poll_interval_seconds=600,
        billing_worker_bucket_size="hour",
        billing_cpu_absent_grace_hours=24,
        watchdog_poll_interval_seconds=30,
        output_collector_interval_seconds=300,
        idle_shutdown_minutes=20,
        reclaim_warning_minutes=5,
        tunnel_host="127.0.0.1",
        tunnel_port_start=18180,
        tunnel_port_end=18182,
        tunnel_auto_recover=True,
        comfyui_remote_port=8188,
        ssh_remote_port=22,
        live_gpu_type_id="",
        live_gpu_price_usd_per_hr=0,
        gpu_pod_image="runpod/comfyui:latest",
        gpu_template_id="",
        cpu_pod_image="python:3.11-slim",
        cpu_flavor_ids=["cpu3c"],
        comfyui_registry_lookup=False,
        comfyui_registry_timeout_seconds=0.1,
    )


class TestRunpodAdapter(RunpodAdapter):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.volume_updates: list[tuple[str, int]] = []
        self.downloads: list[dict[str, object]] = []
        self.moves: list[dict[str, object]] = []

    def scout_gpu(self, intent: dict[str, object]) -> ScoutResult:
        min_vram_gb = normalize_min_vram_gb(intent.get("min_vram_gb") or self.settings.default_min_vram_gb)
        gpu_vendor = normalize_gpu_vendor(intent.get("gpu_vendor") or self.settings.default_gpu_vendor)
        if gpu_vendor != "NVIDIA":
            return ScoutResult(False, True, str(intent.get("data_center_id") or self.settings.default_data_center), None, None, f"unsupported_gpu_vendor:{gpu_vendor}", {})
        gpu_type_id = select_comfyui_gpu_type(min_vram_gb=min_vram_gb, gpu_vendor=gpu_vendor)
        return ScoutResult(
            ok=bool(gpu_type_id),
            authoritative=True,
            data_center_id=str(intent.get("data_center_id") or self.settings.default_data_center),
            gpu_type_id=gpu_type_id,
            price_per_hr_usd=0.0,
            reason="test_authoritative_quote" if gpu_type_id else f"no_test_gpu_meets_min_vram:{min_vram_gb}",
            raw={"mode": "test", "min_vram_gb": min_vram_gb, "gpu_vendor": gpu_vendor},
        )

    def scout_gpu_matrix(self, intent: dict[str, object]) -> dict[str, object]:
        data_centers = [str(item) for item in (intent.get("data_centers") or [])]
        gpu_rows = list(intent.get("gpu_rows") or [])
        candidates = []
        for index, dc in enumerate(data_centers):
            selected = gpu_rows[: min(len(gpu_rows), (index % 3) + 1)]
            for gpu in selected:
                candidates.append(
                    {
                        "data_center_id": dc,
                        "gpu_type_id": gpu["gpu_type_id"],
                        "vendor": gpu["vendor"],
                        "vram_gb": gpu["vram_gb"],
                        "template": gpu.get("template"),
                        "quoted_cost_usd_per_hr": gpu.get("estimated_price_usd_per_hr"),
                        "quote_source": "test_datacenter_matrix",
                        "stock_status": "High",
                        "eligible": True,
                    }
                )
        return {"ok": True, "authoritative": True, "reason": "test_datacenter_matrix", "candidates": candidates}

    def create_network_volume(self, *, name: str, size_gb: int, data_center_id: str) -> dict[str, object]:
        return {"id": f"test-nv-{new_id('rp')[3:]}", "name": name, "size": size_gb, "dataCenterId": data_center_id, "createdAt": utc_iso()}

    def create_cpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, env: dict[str, str]) -> dict[str, object]:
        return {
            "id": f"test-cpu-{new_id('rp')[3:]}",
            "name": name,
            "computeType": "CPU",
            "desiredStatus": "RUNNING",
            "networkVolumeId": volume_provider_id,
            "dataCenterId": data_center_id,
            "envKeys": sorted(env),
            "costPerHr": 0.0,
        }

    def create_gpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, gpu_type_id: str, env: dict[str, str]) -> dict[str, object]:
        return {
            "id": f"test-gpu-{new_id('rp')[3:]}",
            "name": name,
            "computeType": "GPU",
            "desiredStatus": "RUNNING",
            "gpuTypeId": gpu_type_id,
            "networkVolumeId": volume_provider_id,
            "dataCenterId": data_center_id,
            "publicIp": "127.0.0.1",
            "portMappings": {"22": self.settings.ssh_remote_port, str(self.settings.comfyui_remote_port): self.settings.comfyui_remote_port},
            "ports": [f"{self.settings.comfyui_remote_port}/http", f"{self.settings.ssh_remote_port}/tcp"],
            "costPerHr": 0.0,
        }

    def get_pod(self, provider_pod_id: str) -> dict[str, object]:
        return {"id": provider_pod_id, "desiredStatus": "EXITED", "lastStatusChange": "Exited by test"}

    def stop_pod(self, provider_pod_id: str) -> dict[str, object]:
        return {"id": provider_pod_id, "desiredStatus": "EXITED", "ok": True, "mode": "test"}

    def delete_pod(self, provider_pod_id: str) -> dict[str, object]:
        return {"id": provider_pod_id, "deleted": True, "ok": True, "mode": "test"}

    def delete_network_volume(self, provider_volume_id: str) -> dict[str, object]:
        return {"id": provider_volume_id, "deleted": True, "ok": True, "mode": "test"}

    def update_network_volume_size(self, provider_volume_id: str, size_gb: int) -> dict[str, object]:
        self.volume_updates.append((provider_volume_id, size_gb))
        return {"id": provider_volume_id, "size": size_gb, "ok": True, "mode": "test"}

    def workspace_disk_usage(self, provider_pod_id: str) -> dict[str, object]:
        return {"ok": True, "path": "/workspace", "total_bytes": 100 * 1024**3, "used_bytes": 1 * 1024**3, "available_bytes": 99 * 1024**3}

    def list_comfyui_models(self, provider_pod_id: str, folders: list[str]) -> dict[str, object]:
        return {
            "ok": True,
            "asset_root": "/workspace/runpod-controller/assets/comfyui",
            "comfyui_root": "/workspace/runpod-slim/ComfyUI",
            "folders": folders,
            "files": [
                {
                    "folder": "loras",
                    "name": "test.safetensors",
                    "relative_path": "loras/test.safetensors",
                    "path": "/workspace/runpod-controller/assets/comfyui/loras/test.safetensors",
                    "size_bytes": 123,
                    "source": "controller_assets",
                    "controller_managed": True,
                }
            ],
        }

    def download_comfyui_model(self, provider_pod_id: str, payload: dict[str, object]) -> dict[str, object]:
        self.downloads.append(payload)
        return {
            "ok": True,
            "state": "downloaded",
            "target_path": payload.get("target_path"),
            "size_bytes": payload.get("size_bytes") or 123,
            "checksum_sha256": "abc123",
        }

    def move_comfyui_model(self, provider_pod_id: str, payload: dict[str, object]) -> dict[str, object]:
        self.moves.append(payload)
        return {"ok": True, "state": "moved", "source_path": payload.get("source_path"), "target_path": payload.get("target_path"), "size_bytes": 123}

    def billing_pods(self, *, start_time: str, end_time: str, bucket_size: str = "hour") -> list[dict[str, object]]:
        return []

    def billing_network_volumes(self, *, start_time: str, end_time: str, bucket_size: str = "hour") -> list[dict[str, object]]:
        return []


class CountingProbeAdapter(TestRunpodAdapter):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.probe_cpu_pods = 0

    def create_probe_cpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, env: dict[str, str]) -> dict[str, object]:
        self.probe_cpu_pods += 1
        payload = super().create_cpu_pod(name=name, volume_provider_id=volume_provider_id, data_center_id=data_center_id, env=env)
        payload["id"] = f"test-probe-{new_id('rp')[3:]}"
        return payload


class FailFirstProbeAdapter(CountingProbeAdapter):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.probe_attempt_data_centers: list[str] = []
        self.deleted_volumes: list[str] = []

    def create_probe_cpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, env: dict[str, str]) -> dict[str, object]:
        self.probe_attempt_data_centers.append(data_center_id)
        if len(self.probe_attempt_data_centers) == 1:
            raise RuntimeError("CPU probe Pod create failed: HTTP 500: runpod_create_pod_provider_parse_error")
        return super().create_probe_cpu_pod(
            name=name,
            volume_provider_id=volume_provider_id,
            data_center_id=data_center_id,
            env=env,
        )

    def delete_network_volume(self, provider_volume_id: str) -> dict[str, object]:
        self.deleted_volumes.append(provider_volume_id)
        return super().delete_network_volume(provider_volume_id)


class DelayedBillingAdapter(TestRunpodAdapter):
    def __init__(
        self,
        settings: Settings,
        *,
        pod_batches: list[list[dict[str, object]]] | None = None,
        volume_batches: list[list[dict[str, object]]] | None = None,
    ):
        super().__init__(settings)
        self.pod_batches = list(pod_batches or [])
        self.volume_batches = list(volume_batches or [])
        self.pod_calls = 0
        self.volume_calls = 0

    def billing_pods(self, *, start_time: str, end_time: str, bucket_size: str = "hour") -> list[dict[str, object]]:
        self.pod_calls += 1
        if self.pod_batches:
            return self.pod_batches.pop(0)
        return []

    def billing_network_volumes(self, *, start_time: str, end_time: str, bucket_size: str = "hour") -> list[dict[str, object]]:
        self.volume_calls += 1
        if self.volume_batches:
            return self.volume_batches.pop(0)
        return []


class FailingVolumeDeleteAdapter(TestRunpodAdapter):
    def delete_network_volume(self, provider_volume_id: str) -> dict[str, object]:
        return {"id": provider_volume_id, "deleted": False, "ok": False, "mode": "test", "error": "delete failed"}


class NoResizeVisibilityAdapter(TestRunpodAdapter):
    def workspace_disk_usage(self, provider_pod_id: str) -> dict[str, object]:
        return {"ok": True, "path": "/workspace", "total_bytes": 10 * 1024**3, "used_bytes": 1 * 1024**3, "available_bytes": 9 * 1024**3}


class RealishVolumeAdapter(TestRunpodAdapter):
    def create_cpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, env: dict[str, str]) -> dict[str, object]:
        payload = super().create_cpu_pod(name=name, volume_provider_id=volume_provider_id, data_center_id=data_center_id, env=env)
        payload["id"] = f"pod-realish-{new_id('rp')[3:]}"
        return payload


class ConcurrentGpuAdapter(TestRunpodAdapter):
    def __init__(self, settings: Settings, *, parties: int):
        super().__init__(settings)
        self.barrier = threading.Barrier(parties)
        self.lock = threading.Lock()
        self.active_gpu_creates = 0
        self.max_active_gpu_creates = 0

    def create_gpu_pod(self, *, name: str, volume_provider_id: str, data_center_id: str, gpu_type_id: str, env: dict[str, str]) -> dict[str, object]:
        with self.lock:
            self.active_gpu_creates += 1
            self.max_active_gpu_creates = max(self.max_active_gpu_creates, self.active_gpu_creates)
        try:
            self.barrier.wait(timeout=5)
            time.sleep(0.02)
            return super().create_gpu_pod(
                name=name,
                volume_provider_id=volume_provider_id,
                data_center_id=data_center_id,
                gpu_type_id=gpu_type_id,
                env=env,
            )
        finally:
            with self.lock:
                self.active_gpu_creates -= 1


class FailingEnvironmentConfigAdapter(TestRunpodAdapter):
    def configure_comfyui_environment(
        self,
        *,
        provider_pod_id: str,
        session_id: str,
        assets: list[dict[str, object]],
        install_plan: dict[str, object] | None = None,
        validation_plan: dict[str, object] | None = None,
        custom_nodes: list[dict[str, object]] | None = None,
        ui_workflow_json: object = None,
        api_workflow_json: object = None,
    ) -> dict[str, object]:
        return {
            "ok": False,
            "reason": "custom_node_visibility_failed",
            "provider_pod_id": provider_pod_id,
            "session_id": session_id,
        }


class FailFirstEnvironmentConfigAdapter(TestRunpodAdapter):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.configure_calls = 0
        self.configure_lock = threading.Lock()

    def configure_comfyui_environment(
        self,
        *,
        provider_pod_id: str,
        session_id: str,
        assets: list[dict[str, object]],
        install_plan: dict[str, object] | None = None,
        validation_plan: dict[str, object] | None = None,
        custom_nodes: list[dict[str, object]] | None = None,
        ui_workflow_json: object = None,
        api_workflow_json: object = None,
    ) -> dict[str, object]:
        with self.configure_lock:
            self.configure_calls += 1
            call = self.configure_calls
        if call == 1:
            return {
                "ok": False,
                "reason": "comfyui_root_not_found",
                "provider_pod_id": provider_pod_id,
                "searched_paths": ["/workspace/madapps/ComfyUI/main.py"],
            }
        return super().configure_comfyui_environment(
            provider_pod_id=provider_pod_id,
            session_id=session_id,
            assets=assets,
            install_plan=install_plan,
            validation_plan=validation_plan,
            custom_nodes=custom_nodes,
            ui_workflow_json=ui_workflow_json,
            api_workflow_json=api_workflow_json,
        )


class FakeS3VolumeClient:
    def __init__(
        self,
        *,
        data_center_id: str,
        volume_id: str,
        objects: list[S3Object] | None = None,
        texts: dict[str, str] | None = None,
        payloads: dict[str, bytes] | None = None,
    ):
        self.data_center_id = data_center_id
        self.volume_id = volume_id
        self.objects = objects or []
        self.texts = texts or {}
        self.payloads = payloads or {}
        self.get_calls: list[str] = []

    def list_objects(self, prefix: str) -> list[S3Object]:
        return [item for item in self.objects if item.key.startswith(prefix)]

    def get_text(self, key: str, *, timeout: int = 180) -> str:
        if key not in self.texts:
            raise RuntimeError(f"s3_get_failed:404:{key}")
        return self.texts[key]

    def get_object(self, key: str, *, timeout: int = 180) -> bytes:
        self.get_calls.append(key)
        if key in self.payloads:
            return self.payloads[key]
        if key in self.texts:
            return self.texts[key].encode("utf-8")
        raise RuntimeError(f"s3_get_failed:404:{key}")


class ControllerServiceTest(unittest.TestCase):
    def make_service(self) -> ControllerService:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        return ControllerService(settings, db, TestRunpodAdapter(settings))

    def make_service_with_adapter(self, adapter_factory) -> ControllerService:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        return ControllerService(settings, db, adapter_factory(settings))

    def s3_output_client(self, session_id: str, payload: bytes = b"PNGDATA") -> FakeS3VolumeClient:
        key = f"runpod-controller/runs/{session_id}/outputs/raw/out.png"
        return FakeS3VolumeClient(
            data_center_id="US-KS-2",
            volume_id="test-output-volume",
            objects=[S3Object(key, len(payload))],
            payloads={key: payload},
        )

    def reclaim_with_output(self, service: ControllerService, session_id: str, payload: bytes = b"PNGDATA") -> tuple[dict[str, object], FakeS3VolumeClient]:
        client = self.s3_output_client(session_id, payload)
        with (
            patch("controller.service.has_s3_credentials", return_value=True),
            patch("controller.service.RunpodS3VolumeClient", return_value=client),
        ):
            result = service.reclaim_session(session_id, {"force": True})
        return result, client

    def tearDown(self) -> None:
        tmp = getattr(self, "tmp", None)
        if tmp:
            tmp.cleanup()
        for key in ["HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "CIVITAI_TOKEN", "CONTROLLER_DATA_DIR", "CONTROLLER_SECRET_ENV_FILE"]:
            os.environ.pop(key, None)

    def test_resource_request_hydrates_cpu_first(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui", "mode": "interactive"})
        self.assertEqual(request["state"], "interactive_ready")
        session = service.get_session(request["session_id"])
        self.assertIsNotNone(session)
        self.assertEqual(session["state"], "interactive_ready")
        self.assertEqual(session["phase"], "interactive_ready")
        self.assertEqual(session["min_vram_gb"], 24)
        self.assertEqual(session["gpu_vendor"], "NVIDIA")
        self.assertEqual(session["workflow"]["state"], "interactive_ready")
        self.assertEqual(session["workflow"]["min_vram_gb"], 24)
        self.assertEqual(session["workflow"]["gpu_vendor"], "NVIDIA")
        self.assertEqual(session["candidate_count"], 10)
        winner_id = session["winner_candidate_id"]
        self.assertTrue(winner_id)
        candidates = {candidate["id"]: candidate for candidate in session["candidates"]}
        self.assertEqual(candidates[winner_id]["state"], "won")
        self.assertTrue(all(candidate["state"] == "deleted" for cid, candidate in candidates.items() if cid != winner_id))
        self.assertTrue(all("data_center_id" in event for event in session["workflow_events"]))
        self.assertTrue(any(event["data_center_id"] == "US-KS-2" for event in session["workflow_events"] if event.get("candidate_id")))
        workflow_html = workflow_page(session).decode("utf-8")
        self.assertIn("data_center_id", workflow_html)
        self.assertIn("US-KS-2", workflow_html)
        volume = service.db.get("network_volumes", session["network_volume_id"])
        self.assertEqual(volume["hydration_state"], "hydrated")
        self.assertTrue((service.settings.artifacts_dir / "hydrations" / session["hydration_id"] / "HYDRATED.json").exists())

    def test_resource_request_can_return_before_background_processing(self) -> None:
        service = self.make_service()
        called = threading.Event()
        seen: list[str] = []

        def fake_process(request_id: str) -> None:
            seen.append(request_id)
            called.set()

        service._process_workflow_resource_request = fake_process  # type: ignore[method-assign]
        request = service.create_resource_request({"product": "comfyui", "mode": "interactive"}, process_inline=False)

        self.assertEqual(request["state"], "accepted")
        self.assertTrue(called.wait(timeout=2))
        self.assertEqual(seen, [request["id"]])

    def test_runtime_model_tree_lists_controller_models(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui", "mode": "interactive"})
        tree = service.list_session_models_tree(request["session_id"])
        self.assertTrue(tree["ok"])
        self.assertEqual(tree["files"][0]["folder"], "loras")
        self.assertEqual(tree["files"][0]["source"], "controller_assets")

    def test_active_session_page_shows_runtime_model_controls(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui", "mode": "interactive"})
        session = service.get_session(request["session_id"])
        models_html = session_models_page(session).decode("utf-8")
        self.assertIn("Add runtime model / LoRA URL", models_html)
        self.assertIn("Add download", models_html)
        self.assertIn("Runtime model operations", models_html)
        self.assertIn("Grow volume", models_html)
        overview_html = session_detail(session).decode("utf-8")
        self.assertIn("Shutdown: stop GPU + collect outputs", overview_html)
        self.assertIn(f'/sessions/{request["session_id"]}/models', overview_html)
        self.assertIn(f'/sessions/{request["session_id"]}/outputs', overview_html)
        self.assertIn("Candidate Plan", overview_html)
        outputs_html = session_outputs_page(session).decode("utf-8")
        self.assertIn("Collect outputs now", outputs_html)
        self.assertIn("Collection runs", outputs_html)

    def test_runtime_model_download_rejects_duplicate_url(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui", "mode": "interactive"})
        with patch(
            "controller.service.peek_url_metadata",
            return_value={
                "provider": "huggingface",
                "url_key": "https://huggingface.co/org/model/resolve/main/lora.safetensors?download=true",
                "original_url_redacted": "https://huggingface.co/org/model/blob/main/lora.safetensors",
                "download_url_redacted": "https://huggingface.co/org/model/resolve/main/lora.safetensors?download=true",
                "filename": "lora.safetensors",
                "size_bytes": 1024,
                "model_folder": "loras",
                "target": "assets/comfyui/loras/lora.safetensors",
            },
        ):
            first = service.start_session_model_download(
                request["session_id"],
                {"url": "https://huggingface.co/org/model/blob/main/lora.safetensors", "model_folder": "loras"},
            )
            second = service.start_session_model_download(
                request["session_id"],
                {"url": "https://huggingface.co/org/model/blob/main/lora.safetensors", "model_folder": "loras"},
            )
        for _ in range(50):
            ops = service.list_session_model_operations(request["session_id"])
            if ops and ops[0]["state"] in {"downloaded", "already_present", "failed"}:
                break
            time.sleep(0.01)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["state"], "duplicate_url")

    def test_runtime_model_move_blocks_non_controller_path(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui", "mode": "interactive"})
        with self.assertRaises(ValueError):
            service.move_session_model(request["session_id"], {"source_path": "/tmp/wrong.safetensors", "target_folder": "loras"})

    def test_runtime_model_move_uses_adapter_and_records_operation(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui", "mode": "interactive"})
        result = service.move_session_model(
            request["session_id"],
            {
                "source_path": "/workspace/runpod-controller/assets/comfyui/checkpoints/test.safetensors",
                "target_folder": "loras",
                "target_filename": "test.safetensors",
            },
        )
        self.assertTrue(result["ok"])
        ops = service.list_session_model_operations(request["session_id"])
        self.assertEqual(ops[0]["operation_type"], "move")
        self.assertEqual(ops[0]["state"], "moved")
        self.assertIn("/loras/test.safetensors", ops[0]["target_path"])

    def test_runtime_volume_resize_only_grows(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui", "mode": "interactive"})
        session = service.get_session(request["session_id"])
        current = int(session["volume"]["size_gb"])
        result = service.resize_session_volume(request["session_id"], {"size_gb": current})
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "not_growing")

    def test_runtime_volume_resize_waits_for_mounted_visibility(self) -> None:
        service = self.make_service_with_adapter(NoResizeVisibilityAdapter)
        request = service.create_resource_request({"product": "comfyui", "mode": "interactive"})
        with patch("controller.service.time.sleep", return_value=None):
            result = service.resize_session_volume(request["session_id"], {"size_gb": 35})
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "waiting_for_resize_visibility")

    def test_remote_hydration_verification_requires_markers_and_asset_sizes(self) -> None:
        service = self.make_service()
        volume = {
            "id": "vol_real",
            "provider_volume_id": "nv-real",
            "data_center_id": "EU-RO-1",
        }
        hydration = {
            "assets_json": json.dumps(
                [
                    {
                        "target": "assets/comfyui/checkpoints/model.safetensors",
                        "size_bytes": 12,
                    }
                ]
            )
        }
        marker_prefix = "runpod-controller/hydration/hyd_real"
        objects = [
            S3Object(f"{marker_prefix}/HYDRATED.json", 20),
            S3Object(f"{marker_prefix}/inventory.json", 20),
            S3Object(f"{marker_prefix}/checksums.sha256", 20),
            S3Object(f"{marker_prefix}/DONE.json", 20),
            S3Object("runpod-controller/assets/comfyui/checkpoints/model.safetensors", 12),
        ]
        texts = {
            f"{marker_prefix}/HYDRATED.json": '{"state":"hydrated"}',
            f"{marker_prefix}/DONE.json": '{"state":"done"}',
            f"{marker_prefix}/inventory.json": "assets/comfyui/checkpoints/model.safetensors",
            f"{marker_prefix}/checksums.sha256": "abc  assets/comfyui/checkpoints/model.safetensors",
        }
        with (
            patch("controller.service.has_s3_credentials", return_value=True),
            patch("controller.service.RunpodS3VolumeClient", return_value=FakeS3VolumeClient(data_center_id="EU-RO-1", volume_id="nv-real", objects=objects, texts=texts)),
        ):
            result = service._verify_remote_hydration_once("hyd_real", volume, hydration)
        self.assertTrue(result["ok"])
        self.assertEqual(result["bytes"], 12)

    def test_cpu_pod_terminal_without_remote_markers_fails_hydration(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        service = ControllerService(settings, db, RealishVolumeAdapter(settings))
        service.db.insert(
            "network_volumes",
            {
                "id": "vol_real",
                "provider_volume_id": "nv-real",
                "name": "real-volume",
                "data_center_id": "EU-RO-1",
                "size_gb": 10,
                "state": "created",
                "hydration_state": "not_started",
                "retention_policy": "delete_after_collection",
                "estimated_cost_usd": 0,
                "last_payload_json": "{}",
                "created_at": utc_iso(),
                "updated_at": utc_iso(),
            },
        )
        row = service._insert_hydration_request(
            {
                "session_id": None,
                "volume_id": "vol_real",
                "assets": [{"url": "https://example.com/model.safetensors", "target": "assets/comfyui/checkpoints/model.safetensors", "size_bytes": 12}],
            }
        )
        with (
            patch.object(service, "_verify_remote_hydration", side_effect=RuntimeError("remote_hydration_verification_failed:missing")),
            self.assertRaisesRegex(RuntimeError, "remote_hydration_verification_failed"),
        ):
            service._process_hydration(row["id"], run_cpu_pod=True)
        hydration = service.db.get("hydration_requests", row["id"])
        self.assertEqual(hydration["state"], "failed")
        self.assertEqual(service.db.get("network_volumes", "vol_real")["state"], "failed")
        pod = service.db.query("SELECT * FROM pods WHERE volume_id = ?", ("vol_real",))[0]
        self.assertEqual(pod["state"], "deleted")

    def test_redaction_scrubs_secret_values_from_provider_payloads(self) -> None:
        os.environ["RUNPOD_API_KEY"] = "rpa_test_secret_value"
        payload = {"message": "Bearer rpa_test_secret_value", "env": {"HF_TOKEN": "hf_secret"}}
        redacted = redact_secrets(payload)
        self.assertNotIn("rpa_test_secret_value", json.dumps(redacted))
        self.assertEqual(redacted["env"]["HF_TOKEN"], "<redacted>")

    def test_gpu_intent_filters_by_min_vram_and_vendor(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui", "min_vram_gb": 48, "gpu_vendor": "NVIDIA"})
        self.assertEqual(request["state"], "interactive_ready")
        session = service.get_session(request["session_id"])
        self.assertEqual(session["min_vram_gb"], 48)
        self.assertEqual(session["gpu_vendor"], "NVIDIA")
        self.assertEqual(session["gpu_acquisition_attempts"][0]["gpu_type_id"], "NVIDIA A40")

    def test_gpu_intent_rejects_non_nvidia_vendor(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui", "gpu_vendor": "AMD"})
        self.assertEqual(request["state"], "failed")
        self.assertEqual(request["error"], "unsupported_gpu_vendor:AMD")
        self.assertIsNone(request.get("session_id"))

    def test_dry_run_returns_candidate_plan_without_creating_resources(self) -> None:
        service = self.make_service()
        result = service.dry_run_resource_request({"product": "comfyui", "min_vram_gb": 48, "gpu_vendor": "NVIDIA"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["creates_resources"])
        self.assertEqual(result["volume_size_gb"], 10)
        self.assertEqual(result["data_center_count"], 10)
        self.assertEqual(result["gpu_type_count"], 5)
        self.assertEqual(result["candidate_count"], 19)
        self.assertEqual(result["confirmed_candidate_count"], 19)
        self.assertTrue(result["datacenter_gpu_scout_authoritative"])
        self.assertNotIn("global_gpu_options", result)
        self.assertEqual(result["candidate_groups"][0]["gpu_types"][0]["gpu_type_id"], "NVIDIA A40")
        self.assertNotEqual(
            len(result["candidate_groups"][0]["gpu_types"]),
            len(result["candidate_groups"][1]["gpu_types"]),
        )
        self.assertEqual(service.cost_report()["summary"]["sessions"], 0)
        self.assertEqual(service.cost_report()["summary"]["volumes"], 0)

    def test_dry_run_omits_datacenters_without_gpu_rows(self) -> None:
        class SparseMatrixAdapter(TestRunpodAdapter):
            def scout_gpu_matrix(self, intent: dict[str, object]) -> dict[str, object]:
                gpu_rows = list(intent.get("gpu_rows") or [])
                selected = gpu_rows[0]
                return {
                    "ok": True,
                    "authoritative": False,
                    "listing_available": True,
                    "reason": "sparse_test_matrix",
                    "candidates": [
                        {
                            "data_center_id": "US-KS-2",
                            "gpu_type_id": selected["gpu_type_id"],
                            "vendor": selected["vendor"],
                            "vram_gb": selected["vram_gb"],
                            "template": selected.get("template"),
                            "estimated_cost_usd_per_hr": selected.get("estimated_price_usd_per_hr"),
                            "quote_source": "runpodctl_datacenter_list_gpuAvailability",
                            "scout_status": "runpodctl_datacenter_listed_stock_unconfirmed",
                            "stock_status": "unconfirmed",
                            "eligible": False,
                        },
                        {
                            "data_center_id": "EU-RO-1",
                            "gpu_type_id": selected["gpu_type_id"],
                            "vendor": selected["vendor"],
                            "vram_gb": selected["vram_gb"],
                            "template": selected.get("template"),
                            "estimated_cost_usd_per_hr": selected.get("estimated_price_usd_per_hr"),
                            "quote_source": "runpodctl_datacenter_list_gpuAvailability",
                            "scout_status": "runpodctl_datacenter_listed_stock_unconfirmed",
                            "stock_status": "unconfirmed",
                            "eligible": False,
                        },
                    ],
                }

        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        service = ControllerService(settings, db, SparseMatrixAdapter(settings))
        result = service.dry_run_resource_request(
            {"product": "comfyui", "min_vram_gb": 48, "gpu_vendor": "NVIDIA", "max_gpu_usd_per_hr": 0.7}
        )
        self.assertEqual(result["scouted_data_center_count"], 10)
        self.assertEqual(result["data_center_count"], 2)
        self.assertEqual(result["candidate_data_center_count"], 2)
        self.assertEqual([group["data_center_id"] for group in result["candidate_groups"]], ["US-KS-2", "EU-RO-1"])
        self.assertNotIn("no_datacenter_gpu_rows", json.dumps(result))

    def test_create_candidate_selection_keeps_listed_unconfirmed_datacenters(self) -> None:
        class SparseMatrixAdapter(TestRunpodAdapter):
            def scout_gpu_matrix(self, intent: dict[str, object]) -> dict[str, object]:
                gpu_rows = list(intent.get("gpu_rows") or [])
                selected = gpu_rows[0]
                return {
                    "ok": True,
                    "authoritative": False,
                    "listing_available": True,
                    "reason": "sparse_test_matrix",
                    "candidates": [
                        {
                            "data_center_id": "US-KS-2",
                            "gpu_type_id": selected["gpu_type_id"],
                            "vendor": selected["vendor"],
                            "vram_gb": selected["vram_gb"],
                            "template": selected.get("template"),
                            "estimated_cost_usd_per_hr": selected.get("estimated_price_usd_per_hr"),
                            "quote_source": "runpodctl_datacenter_list_gpuAvailability",
                            "scout_status": "runpodctl_datacenter_listed_stock_unconfirmed",
                            "stock_status": "unconfirmed",
                            "eligible": False,
                        },
                        {
                            "data_center_id": "EU-RO-1",
                            "gpu_type_id": selected["gpu_type_id"],
                            "vendor": selected["vendor"],
                            "vram_gb": selected["vram_gb"],
                            "template": selected.get("template"),
                            "estimated_cost_usd_per_hr": selected.get("estimated_price_usd_per_hr"),
                            "quote_source": "runpodctl_datacenter_list_gpuAvailability",
                            "scout_status": "runpodctl_datacenter_listed_stock_unconfirmed",
                            "stock_status": "unconfirmed",
                            "eligible": False,
                        },
                    ],
                }

        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        service = ControllerService(settings, db, SparseMatrixAdapter(settings))

        candidates = service._candidate_datacenters(  # noqa: SLF001
            {"product": "comfyui", "min_vram_gb": 48, "gpu_vendor": "NVIDIA", "max_gpu_usd_per_hr": 0.7},
            max_rate=0.7,
            min_vram_gb=48,
            gpu_vendor="NVIDIA",
        )

        self.assertEqual([row["data_center_id"] for row in candidates], ["US-KS-2", "EU-RO-1"])
        self.assertTrue(all(not row["eligible"] for row in candidates))

    def test_dry_run_min_vram_80_includes_large_gpus_when_budget_allows(self) -> None:
        service = self.make_service()
        result = service.dry_run_resource_request(
            {"product": "comfyui", "min_vram_gb": 80, "gpu_vendor": "NVIDIA", "max_gpu_usd_per_hr": 1000}
        )
        self.assertTrue(result["ok"])
        gpu_ids = {row["gpu_type_id"] for row in result["candidates"]}
        self.assertIn("NVIDIA A100 80GB PCIe", gpu_ids)
        self.assertTrue(all(int(row["vram_gb"]) >= 80 for row in result["candidates"]))
        self.assertGreater(result["gpu_type_count"], 5)
        self.assertGreater(result["candidate_count"], 0)

    def test_runpod_rest_matrix_falls_back_to_runpodctl_per_datacenter_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = test_settings(Path(tmp))
            adapter = RunpodRestAdapter(settings)
            adapter.api_key = "test-key"
            gpu_rows = comfyui_gpu_rows(min_vram_gb=80, gpu_vendor="NVIDIA", max_usd_per_hr=1000)

            class Proc:
                returncode = 0
                stderr = ""
                stdout = json.dumps(
                    [
                        {
                            "id": "US-KS-2",
                            "gpuAvailability": [
                                {"gpuId": "NVIDIA H100 NVL", "displayName": "H100 NVL", "stockStatus": "Low"},
                                {"gpuId": "NVIDIA H200", "displayName": "H200 SXM", "stockStatus": ""},
                            ],
                        },
                        {
                            "id": "US-IL-1",
                            "gpuAvailability": [
                                {"gpuId": "NVIDIA GeForce RTX 4090", "displayName": "RTX 4090", "stockStatus": "Low"},
                            ],
                        },
                        {
                            "id": "US-MD-1",
                            "gpuAvailability": [
                                {"gpuId": "NVIDIA A100-SXM4-80GB", "displayName": "A100 SXM", "stockStatus": "Medium"},
                            ],
                        },
                    ]
                )

            with (
                patch.object(adapter, "_graphql", side_effect=RuntimeError("cloudflare 1010")),
                patch("controller.runpod.shutil.which", return_value="/usr/local/bin/runpodctl"),
                patch("controller.runpod.subprocess.run", return_value=Proc()),
            ):
                result = adapter.scout_gpu_matrix(
                    {
                        "data_centers": ["US-KS-2", "US-IL-1", "US-MD-1"],
                        "gpu_rows": gpu_rows,
                        "max_gpu_usd_per_hr": 1000,
                    }
                )
        self.assertTrue(result["ok"])
        self.assertFalse(result["authoritative"])
        self.assertEqual(result["reason"], "runpodctl_datacenter_list_gpuAvailability_after_graphql_failed")
        self.assertEqual(result["confirmed_candidate_count"], 2)
        gpu_by_dc = {(row["data_center_id"], row["gpu_type_id"]): row for row in result["candidates"]}
        self.assertIn(("US-KS-2", "NVIDIA H100 NVL"), gpu_by_dc)
        self.assertIn(("US-KS-2", "NVIDIA H200"), gpu_by_dc)
        self.assertIn(("US-MD-1", "NVIDIA A100-SXM4-80GB"), gpu_by_dc)
        self.assertNotIn(("US-IL-1", "NVIDIA GeForce RTX 4090"), gpu_by_dc)
        self.assertFalse(gpu_by_dc[("US-KS-2", "NVIDIA H200")]["eligible"])

    def test_dry_run_rejects_amd_without_creating_resources(self) -> None:
        service = self.make_service()
        result = service.dry_run_resource_request({"product": "comfyui", "gpu_vendor": "AMD"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unsupported_gpu_vendor:AMD")
        self.assertEqual(service.cost_report()["summary"]["sessions"], 0)

    def test_test_adapter_workflow_creates_gpu_and_tunnel(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session = service.get_session(request["session_id"])
        self.assertEqual(session["phase"], "interactive_ready")
        self.assertEqual(session["gpu_acquisition_attempts"][0]["state"], "created")
        self.assertEqual(session["tunnels"][0]["state"], "proxy_ready")
        self.assertEqual(session["tunnels"][0]["local_url"], "http://127.0.0.1:8188")
        self.assertIn("8188", session["ui_url"])

    def test_comfyui_workflow_analyzer_resolves_explicit_mapping_and_ignores_notes(self) -> None:
        ui_workflow = {
            "nodes": [
                {"id": 1, "type": "CheckpointLoaderSimple", "title": "checkpoint"},
                {"id": 2, "type": "Fancy Custom Node (example)", "title": "custom"},
                {"id": 3, "title": "note"},
            ]
        }
        result = analyze_comfyui_workflow(
            {
                "ui_workflow_json": ui_workflow,
                "custom_nodes": [
                    {
                        "package": "example-custom-node",
                        "repo_url": "https://github.com/example/custom-node",
                        "node_types": ["Fancy Custom Node (example)"],
                    }
                ],
            }
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["node_count"], 2)
        self.assertEqual(result["resolved_custom_nodes"][0]["package"], "example-custom-node")
        self.assertIn("ui:ignored_non_executable_node:2", result["warnings"])
        self.assertEqual(result["install_plan"]["steps"][0]["package"], "example-custom-node")

    def test_comfyui_workflow_analyzer_blocks_unresolved_custom_nodes(self) -> None:
        result = analyze_comfyui_workflow(
            {"ui_workflow_json": {"nodes": [{"id": 1, "type": "Mystery Node (unknown)"}]}}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["unresolved_custom_nodes"][0]["class_type"], "Mystery Node (unknown)")

    def test_comfyui_workflow_analyzer_returns_registry_suggestion_without_accepting_it(self) -> None:
        def resolver(node_type: str) -> dict[str, object] | None:
            if node_type == "LayerStyle: Load Image":
                return {
                    "package": "comfyui-layerstyle",
                    "display_name": "ComfyUI Layer Style",
                    "repo_url": "https://github.com/example/ComfyUI_LayerStyle",
                    "install_method": "git_clone",
                    "node_types": [node_type],
                    "source": "comfy_registry",
                    "registry_node_id": "comfyui-layerstyle",
                    "registry_version": "1.2.3",
                }
            return None

        result = analyze_comfyui_workflow(
            {"ui_workflow_json": {"nodes": [{"id": 1, "type": "LayerStyle: Load Image"}]}},
            registry_resolver=resolver,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["suggested_custom_nodes"][0]["suggestion"]["package"], "comfyui-layerstyle")
        self.assertEqual(result["unresolved_custom_nodes"][0]["reason"], "custom_node_mapping_needs_acceptance")
        self.assertEqual(result["install_plan"]["steps"], [])

    def test_registry_node_payload_maps_to_installable_custom_node(self) -> None:
        result = registry_node_to_custom_node(
            "LayerStyle: Load Image",
            {
                "id": "comfyui-layerstyle",
                "name": "ComfyUI Layer Style",
                "repository": "https://github.com/example/ComfyUI_LayerStyle",
                "status": "NodeStatusActive",
                "latest_version": {"version": "1.2.3", "downloadUrl": "https://example.com/pkg.zip"},
            },
        )
        self.assertEqual(result["package"], "comfyui-layerstyle")
        self.assertEqual(result["repo_url"], "https://github.com/example/ComfyUI_LayerStyle")
        self.assertEqual(result["node_types"], ["LayerStyle: Load Image"])
        self.assertEqual(result["source"], "comfy_registry")

    def test_service_registry_resolver_caches_hits_and_misses(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        object.__setattr__(settings, "comfyui_registry_lookup", True)
        db = Database(settings)
        db.initialize()
        service = ControllerService(settings, db, TestRunpodAdapter(settings))
        calls = []

        class Client:
            def __init__(self, **_kwargs: object):
                pass

            def resolve_comfy_node_name(self, node_type: str) -> dict[str, object] | None:
                calls.append(node_type)
                if node_type == "Known Registry Node":
                    return {
                        "package": "known-registry-node",
                        "repo_url": "https://github.com/example/known",
                        "install_method": "git_clone",
                        "node_types": [node_type],
                        "source": "comfy_registry",
                    }
                return None

        with patch("controller.service.ComfyRegistryClient", Client):
            self.assertIsNotNone(service._resolve_comfyui_registry_node("Known Registry Node"))
            self.assertIsNotNone(service._resolve_comfyui_registry_node("Known Registry Node"))
            self.assertIsNone(service._resolve_comfyui_registry_node("Missing Registry Node"))
            self.assertIsNone(service._resolve_comfyui_registry_node("Missing Registry Node"))
        self.assertEqual(calls, ["Known Registry Node", "Missing Registry Node"])

    def test_launch_template_fingerprint_changes_with_workflow_assets_and_nodes(self) -> None:
        base = {
            "product": "comfyui",
            "ui_workflow_json": {"nodes": [{"id": 1, "type": "CheckpointLoaderSimple"}]},
            "api_workflow_json": None,
            "assets": [{"url": "https://example.com/a.safetensors", "size_bytes": 1}],
            "custom_nodes": [],
            "install_plan": {"version": 1, "steps": []},
            "base_template": {"image": "runpod/comfyui:latest"},
        }
        first = launch_template_fingerprint(**base)
        changed_asset = {**base, "assets": [{"url": "https://example.com/b.safetensors", "size_bytes": 1}]}
        changed_node = {**base, "custom_nodes": [{"package": "example-custom-node", "repo_url": "https://github.com/example/custom-node"}]}
        self.assertNotEqual(first, launch_template_fingerprint(**changed_asset))
        self.assertNotEqual(first, launch_template_fingerprint(**changed_node))

    def test_legacy_launch_template_crud_probe_cache_and_create_flow(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        adapter = CountingProbeAdapter(settings)
        service = ControllerService(settings, db, adapter)
        template = service.create_comfyui_launch_template(
            {
                "product": "comfyui",
                "name": "explicit-custom",
                "ui_workflow_json": {"nodes": [{"id": 1, "type": "Fancy Custom Node (example)"}]},
                "custom_nodes": [
                    {
                        "package": "example-custom-node",
                        "repo_url": "https://github.com/example/custom-node",
                        "node_types": ["Fancy Custom Node (example)"],
                    }
                ],
                "assets": [{"url": "https://example.com/model.safetensors", "model_folder": "checkpoints", "size_bytes": 1024}],
            }
        )
        self.assertEqual(template["analyzer_result"]["resolved_custom_nodes"][0]["package"], "example-custom-node")
        self.assertEqual(len(service.list_comfyui_launch_templates("comfyui")), 1)

        first_probe = service.probe_comfyui_launch_template(template["id"])
        self.assertEqual(first_probe["state"], "passed")
        self.assertFalse(first_probe.get("cached", False))
        self.assertEqual(adapter.probe_cpu_pods, 1)
        second_probe = service.probe_comfyui_launch_template(template["id"])
        self.assertEqual(second_probe["state"], "passed")
        self.assertTrue(second_probe["cached"])
        self.assertEqual(adapter.probe_cpu_pods, 1)

        request = service.create_resource_request({"product": "comfyui", "launch_template_id": template["id"]})
        self.assertEqual(request["state"], "interactive_ready")
        session = service.get_session(request["session_id"])
        self.assertEqual(session["workflow"]["launch_template_id"], template["id"])
        self.assertEqual(session["workflow"]["probe_result"]["state"], "passed")
        self.assertEqual(session["workflow"]["install_plan"]["steps"][0]["package"], "example-custom-node")
        self.assertEqual(session["workflow"]["validation_plan"]["custom_node_types"], ["Fancy Custom Node (example)"])
        html = workflow_page(session).decode("utf-8")
        self.assertIn("Workflow Analysis", html)
        self.assertIn("example-custom-node", html)
        self.assertIn("Dependency Probe", html)

    def test_unresolved_launch_template_blocks_resource_request(self) -> None:
        service = self.make_service()
        template = service.create_comfyui_launch_template(
            {
                "product": "comfyui",
                "name": "missing-node",
                "ui_workflow_json": {"nodes": [{"id": 1, "type": "Mystery Node (missing)"}]},
            }
        )
        self.assertFalse(template["analyzer_result"]["ok"])
        request = service.create_resource_request({"product": "comfyui", "launch_template_id": template["id"]})
        self.assertEqual(request["state"], "failed")
        self.assertEqual(request["error"], "unresolved_custom_nodes")

    def test_workflow_upload_hash_dedup_and_manual_node_lock_create_flow(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        adapter = CountingProbeAdapter(settings)
        service = ControllerService(settings, db, adapter)
        first = service.upload_comfyui_workflow(
            {
                "filename": "flow.json",
                "content": json.dumps({"nodes": [{"id": 1, "type": "Fancy Custom Node (example)"}]}, indent=2),
            }
        )
        second = service.upload_comfyui_workflow(
            {
                "filename": "flow-copy.json",
                "content": '{"nodes":[{"type":"Fancy Custom Node (example)","id":1}]}',
            }
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["workflow_hash"], workflow_hash({"nodes": [{"id": 1, "type": "Fancy Custom Node (example)"}]}))
        self.assertEqual(first["status"], "needs_node_mapping")

        resolved = service.resolve_comfyui_workflow_node(
            first["id"],
            {
                "decision": "install_git_repo",
                "class_type": "Fancy Custom Node (example)",
                "package": "example-custom-node",
                "repo_url": "https://github.com/example/custom-node",
                "locked_ref": "a" * 40,
            },
        )
        self.assertEqual(resolved["status"], "ready_to_probe")
        self.assertEqual(resolved["node_locks"][0]["locked_ref"], "a" * 40)
        probe = service.probe_comfyui_workflow(resolved["id"])
        self.assertEqual(probe["state"], "passed")
        self.assertEqual(adapter.probe_cpu_pods, 1)
        request = service.create_resource_request({"product": "comfyui", "workflow_id": resolved["id"]})
        self.assertEqual(request["state"], "interactive_ready")
        session = service.get_session(request["session_id"])
        self.assertEqual(session["workflow"]["comfyui_workflow_id"], resolved["id"])
        self.assertEqual(session["workflow"]["install_plan"]["steps"][0]["ref"], "a" * 40)

    def test_rewrite_workflow_model_references_updates_widgets_and_duplicate_nodes(self) -> None:
        workflow = {
            "nodes": [
                {"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": ["old-model.safetensors"]},
                {"id": 2, "type": "CheckpointLoaderSimple", "widgets_values": ["old-model.safetensors"]},
                {"id": 3, "type": "LoraLoader", "widgets_values": ["style-lora.safetensors", 0.8]},
            ]
        }
        assets = extract_model_requirements(workflow)
        checkpoint = next(asset for asset in assets if asset["model_folder"] == "checkpoints")
        checkpoint["filename"] = "swapped-model.safetensors"
        checkpoint["target"] = "assets/comfyui/checkpoints/swapped-model.safetensors"
        removed = next(asset for asset in assets if asset["model_folder"] == "loras")
        removed = dict(removed, filename="ignored.safetensors", status="removed")
        rewritten, changes = rewrite_workflow_model_references(workflow, [checkpoint, removed])
        values = [node["widgets_values"][0] for node in rewritten["nodes"]]
        self.assertEqual(values, ["swapped-model.safetensors", "swapped-model.safetensors", "style-lora.safetensors"])
        self.assertEqual(len(changes), 2)
        # The input workflow must stay untouched.
        self.assertEqual(workflow["nodes"][0]["widgets_values"][0], "old-model.safetensors")

    def test_launch_context_rewrites_model_references_for_session(self) -> None:
        service = self.make_service()
        uploaded = service.upload_comfyui_workflow(
            {
                "filename": "flow.json",
                "content": json.dumps({"nodes": [{"id": 1, "type": "CheckpointLoaderSimple", "widgets_values": ["old.safetensors"]}]}),
            }
        )
        self.assertEqual(uploaded["status"], "needs_model_urls")
        asset = dict(uploaded["extracted_assets"][0])
        asset.update(
            {
                "url": "https://example.com/models/new.safetensors",
                "filename": "new.safetensors",
                "target": "assets/comfyui/checkpoints/new.safetensors",
                "size_bytes": 1024,
                "size_unknown": False,
                "status": "ready",
            }
        )
        ready = service.update_comfyui_workflow(uploaded["id"], {"extracted_assets": [asset]})
        self.assertEqual(ready["status"], "ready_to_probe")
        request = service.create_resource_request({"product": "comfyui", "workflow_id": uploaded["id"]})
        self.assertEqual(request["state"], "interactive_ready")
        session = service.get_session(request["session_id"])
        stored = json.dumps(session["workflow"]["ui_workflow"])
        self.assertIn("new.safetensors", stored)
        self.assertNotIn("old.safetensors", stored)
        # The library copy keeps the original JSON; only the launch copy is rewritten.
        library = service.get_comfyui_workflow(uploaded["id"])
        self.assertIn("old.safetensors", json.dumps(library["workflow"]))

    def test_workflow_package_export_import_round_trip(self) -> None:
        service = self.make_service()
        uploaded = service.upload_comfyui_workflow(
            {
                "filename": "flow.json",
                "name": "Shared Flow",
                "content": json.dumps(
                    {
                        "nodes": [
                            {"id": 1, "type": "Fancy Custom Node (example)"},
                            {"id": 2, "type": "CheckpointLoaderSimple", "widgets_values": ["model.safetensors"]},
                        ]
                    }
                ),
            }
        )
        service.resolve_comfyui_workflow_node(
            uploaded["id"],
            {
                "decision": "install_git_repo",
                "class_type": "Fancy Custom Node (example)",
                "package": "example-custom-node",
                "repo_url": "https://github.com/example/custom-node",
                "locked_ref": "a" * 40,
            },
        )
        asset = dict(service.get_comfyui_workflow(uploaded["id"])["extracted_assets"][0])
        asset.update({"url": "https://example.com/model.safetensors?token=secret", "size_bytes": 2048, "size_unknown": False, "status": "ready"})
        service.update_comfyui_workflow(uploaded["id"], {"extracted_assets": [asset]})

        filename, blob = service.export_comfyui_workflow_package(uploaded["id"])
        self.assertTrue(filename.endswith(".comfyui-pack.zip"))
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            packaged_workflow = json.loads(archive.read("workflow.json").decode("utf-8"))
        self.assertEqual(manifest["format"], "comfyui-controller-workflow-package")
        self.assertEqual(manifest["workflow_hash"], workflow_hash(packaged_workflow))
        self.assertNotIn("token=secret", json.dumps(manifest))

        tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(tmp2.cleanup)
        settings2 = test_settings(Path(tmp2.name))
        db2 = Database(settings2)
        db2.initialize()
        service2 = ControllerService(settings2, db2, TestRunpodAdapter(settings2))
        imported = service2.import_comfyui_workflow_package({"package_base64": base64.b64encode(blob).decode("ascii")})
        self.assertTrue(imported["imported"])
        self.assertTrue(imported["package_hash_matched"])
        self.assertEqual(imported["workflow_hash"], manifest["workflow_hash"])
        self.assertEqual(imported["name"], "Shared Flow")
        self.assertEqual(imported["node_locks"][0]["locked_ref"], "a" * 40)
        self.assertEqual(imported["status"], "ready_to_probe")
        self.assertEqual(imported["extracted_assets"][0]["size_bytes"], 2048)

    def test_workflow_package_import_rejects_invalid_packages(self) -> None:
        service = self.make_service()
        with self.assertRaises(ValueError):
            service.import_comfyui_workflow_package({"package_base64": "not-base64!!"})
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("workflow.json", "{}")
        with self.assertRaises(ValueError):
            service.import_comfyui_workflow_package({"package_base64": base64.b64encode(buffer.getvalue()).decode("ascii")})
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("workflow.json", "{}")
            archive.writestr("manifest.json", json.dumps({"format": "something-else", "version": 1}))
        with self.assertRaises(ValueError):
            service.import_comfyui_workflow_package({"package_base64": base64.b64encode(buffer.getvalue()).decode("ascii")})

    def test_environment_script_compiles_and_writes_session_workflow_autoload(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        script = RunpodRestAdapter(settings)._comfyui_environment_script()
        compile(script, "<comfyui-environment-script>", "exec")
        self.assertIn("def write_session_workflow", script)
        self.assertIn("runpod-controller-session.json", script)
        self.assertIn("runpod-controller-autoload", script)
        self.assertIn("loadGraphData", script)
        # The workflow must land on disk before ComfyUI restarts.
        self.assertLess(script.index("session_workflow = write_session_workflow(root)"), script.index("restart_proc = restart_comfyui(root, output_dir)"))

    def test_workflow_dependency_probe_retries_datacenters_and_cleans_failed_volume(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        adapter = FailFirstProbeAdapter(settings)
        service = ControllerService(settings, db, adapter)
        workflow = service.upload_comfyui_workflow(
            {
                "filename": "probe-retry.json",
                "content": json.dumps({"nodes": [{"id": 1, "type": "Fancy Custom Node (example)"}]}),
            }
        )
        resolved = service.resolve_comfyui_workflow_node(
            workflow["id"],
            {
                "decision": "install_git_repo",
                "class_type": "Fancy Custom Node (example)",
                "repo_url": "https://github.com/example/custom-node",
                "locked_ref": "c" * 40,
            },
        )

        probe = service.probe_comfyui_workflow(resolved["id"])

        self.assertEqual(probe["state"], "passed")
        self.assertGreaterEqual(len(adapter.probe_attempt_data_centers), 2)
        self.assertEqual(adapter.probe_attempt_data_centers[0], "US-KS-2")
        result = probe["result"]
        attempts = result["attempts"]
        self.assertEqual(attempts[0]["state"], "failed")
        self.assertEqual(attempts[0]["error"], "CPU probe Pod create failed: HTTP 500: runpod_create_pod_provider_parse_error")
        self.assertEqual(attempts[0]["cleanup_status"], "deleted_owned_probe_resources")
        self.assertEqual(attempts[1]["state"], "cpu_pod_created")
        self.assertGreaterEqual(len(adapter.deleted_volumes), 2)
        failed_volume = service.db.get("network_volumes", attempts[0]["volume_id"])
        self.assertEqual(failed_volume["state"], "deleted")

    def test_runpod_probe_cpu_payload_matches_hydration_cpu_contract(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        adapter = RunpodRestAdapter(settings)
        captured: list[dict[str, object]] = []

        def fake_request(method: str, path: str, body: dict[str, object] | None = None, timeout: int = 90) -> tuple[int, dict[str, object]]:
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/pods")
            self.assertEqual(timeout, 120)
            captured.append(body or {})
            return 201, {"id": "pod-probe", "name": "probe"}

        adapter._request = fake_request  # type: ignore[method-assign]

        payload = adapter.create_probe_cpu_pod(
            name="probe",
            volume_provider_id="nv-1",
            data_center_id="US-KS-2",
            env={"PROBE_ID": "prb_1"},
        )

        self.assertEqual(payload["id"], "pod-probe")
        body = captured[0]
        self.assertEqual(body["computeType"], "CPU")
        self.assertEqual(body["networkVolumeId"], "nv-1")
        self.assertEqual(body["volumeMountPath"], "/workspace")
        self.assertEqual(body["cpuFlavorIds"], ["cpu3c"])
        self.assertEqual(body["cpuFlavorPriority"], "availability")
        self.assertEqual(body["vcpuCount"], 2)
        self.assertEqual(body["interruptible"], False)
        self.assertEqual(body["dockerEntrypoint"], ["/bin/bash", "-lc"])
        self.assertIn("PROBE_ID", str(body["dockerStartCmd"]))

    def test_hydration_shell_uses_civitai_token_query_not_bearer_header(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        shell = RunpodRestAdapter(settings)._hydration_shell()

        self.assertIn('if token and provider != "civitai":', shell)
        self.assertIn('def request_url(url, provider):', shell)
        self.assertIn('query.append(("token", token))', shell)
        self.assertIn("urllib.request.Request(request_url(url, provider), headers=request_headers(provider, start))", shell)

    def test_runpod_volume_delete_treats_provider_nonexistent_as_ok(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        adapter = RunpodRestAdapter(settings)

        def fake_request(method: str, path: str, body: dict[str, object] | None = None, timeout: int = 90) -> tuple[int, dict[str, object]]:
            self.assertEqual(method, "DELETE")
            self.assertEqual(path, "/networkvolumes/nv-absent")
            return 500, {"error": "delete network volume: Tried to delete nonexistent network volume", "status": 500}

        adapter._request = fake_request  # type: ignore[method-assign]

        result = adapter.delete_network_volume("nv-absent")

        self.assertTrue(result["ok"])

    def test_runpod_waits_for_ssh_mapping_before_environment_config(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        adapter = RunpodRestAdapter(settings)
        responses = [
            {"id": "pod-1", "publicIp": "", "portMappings": {}, "desiredStatus": "RUNNING"},
            {"id": "pod-1", "publicIp": "1.2.3.4", "portMappings": {"22": 30022}, "desiredStatus": "RUNNING"},
        ]

        def fake_get_pod(provider_pod_id: str) -> dict[str, object]:
            self.assertEqual(provider_pod_id, "pod-1")
            return responses.pop(0)

        adapter.get_pod = fake_get_pod  # type: ignore[method-assign]

        with patch("controller.runpod.time.sleep") as sleep:
            result = adapter._wait_for_ssh_mapping("pod-1", timeout_seconds=30, interval_seconds=5)  # noqa: SLF001

        self.assertTrue(result["ok"])
        self.assertEqual(result["public_ip"], "1.2.3.4")
        self.assertEqual(result["ssh_port"], 30022)
        sleep.assert_called_once_with(5)

    def test_runpod_environment_config_sends_remote_script_over_stdin(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        adapter = RunpodRestAdapter(settings)
        captured: dict[str, object] = {}
        adapter.get_pod = lambda _pod_id: {"publicIp": "1.2.3.4", "portMappings": {"22": 30022}}  # type: ignore[method-assign]

        def fake_run(args: list[str], **kwargs: object) -> object:
            captured["args"] = args
            captured["input"] = kwargs.get("input")
            captured["timeout"] = kwargs.get("timeout")
            return type("Proc", (), {"returncode": 0, "stdout": '{"ok": true}\n', "stderr": ""})()

        with patch("controller.runpod.os.path.exists", return_value=True):
            with patch("controller.runpod.subprocess.run", side_effect=fake_run):
                result = adapter.configure_comfyui_environment(
                    provider_pod_id="pod-1",
                    session_id="ses-1",
                    assets=[],
                    install_plan={"steps": []},
                    validation_plan={},
                    custom_nodes=[],
                )

        self.assertTrue(result["ok"])
        self.assertEqual(captured["args"][-2:], ["python3", "-"])
        self.assertNotIn("-c", captured["args"])
        self.assertIn("sys.stdin = io.StringIO", str(captured["input"]))
        self.assertIn("/workspace/madapps/ComfyUI", str(captured["input"]))
        self.assertIn("searched_paths", str(captured["input"]))
        self.assertIn("observed_paths", str(captured["input"]))
        self.assertIn("/workspace/madapps/ComfyUI/output", str(captured["input"]))
        self.assertGreaterEqual(int(captured["timeout"]), 1800)

    def test_runpod_environment_config_distinguishes_frontend_custom_nodes(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        script = RunpodRestAdapter(settings)._comfyui_environment_script()  # noqa: SLF001

        self.assertIn("frontend_declared", script)
        self.assertIn("backend_missing", script)
        self.assertIn("custom_node_visibility_failed", script)
        self.assertIn("missing_visible_models", script)

    def test_workflow_node_lock_infers_package_from_repo_url(self) -> None:
        service = self.make_service()
        workflow = service.upload_comfyui_workflow(
            {
                "filename": "repo-url-only.json",
                "content": json.dumps({"nodes": [{"id": 1, "type": "Fancy Custom Node (example)"}]}),
            }
        )
        resolved = service.resolve_comfyui_workflow_node(
            workflow["id"],
            {
                "decision": "install_git_repo",
                "class_type": "Fancy Custom Node (example)",
                "repo_url": "https://github.com/example/custom-node.git",
                "locked_ref": "b" * 40,
            },
        )
        self.assertEqual(resolved["node_locks"][0]["repo_url"], "https://github.com/example/custom-node.git")
        self.assertEqual(resolved["node_locks"][0]["package"], "custom-node")
        self.assertEqual(resolved["node_locks"][0]["locked_ref"], "b" * 40)

    def test_workflow_upload_existing_hash_preserves_saved_configuration(self) -> None:
        service = self.make_service()
        payload = {
            "filename": "stable.json",
            "content": json.dumps({"nodes": [{"id": 1, "type": "UNETLoader", "widgets_values": ["current.safetensors"]}]}),
        }
        first = service.upload_comfyui_workflow(payload)
        service.update_comfyui_workflow(
            first["id"],
            {
                "extracted_assets": [
                    {
                        **first["extracted_assets"][0],
                        "url": "https://huggingface.co/example/current/resolve/main/current.safetensors",
                        "size_bytes": 123,
                        "size_unknown": False,
                        "provider": "huggingface",
                        "status": "ready",
                    }
                ],
                "extra_assets": [{"url": "https://example.com/extra.safetensors", "size_bytes": 1}],
                "node_locks": [{"decision": "treat_builtin", "node_types": ["Stable Node"]}],
            },
        )

        uploaded = service.upload_comfyui_workflow({**payload, "filename": "stable-again.json"})

        self.assertEqual(uploaded["id"], first["id"])
        self.assertEqual(uploaded["original_filename"], "stable-again.json")
        self.assertEqual(uploaded["extracted_assets"][0]["url"], "https://huggingface.co/example/current/resolve/main/current.safetensors?download=true")
        self.assertEqual(uploaded["extra_assets"][0]["url"], "https://example.com/extra.safetensors")
        self.assertEqual(uploaded["extra_assets"][0]["size_bytes"], 1)
        self.assertEqual(uploaded["node_locks"], [{"decision": "treat_builtin", "node_types": ["Stable Node"]}])

    def test_workflow_asset_save_canonicalizes_huggingface_blob_download_url(self) -> None:
        service = self.make_service()
        workflow = service.upload_comfyui_workflow(
            {
                "filename": "hf-blob.json",
                "content": json.dumps({"nodes": [{"id": 1, "type": "CLIPLoader", "widgets_values": ["qwen_3_4b.safetensors"]}]}),
            }
        )
        blob_url = "https://huggingface.co/Comfy-Org/z_image_turbo/blob/main/split_files/text_encoders/qwen_3_4b.safetensors"
        updated = service.update_comfyui_workflow(
            workflow["id"],
            {
                "extracted_assets": [
                    {
                        **workflow["extracted_assets"][0],
                        "url": blob_url,
                        "model_folder": "text_encoders",
                        "size_bytes": 8_044_982_048,
                        "size_unknown": False,
                        "provider": "huggingface",
                        "status": "ready",
                    }
                ]
            },
        )
        self.assertEqual(
            updated["extracted_assets"][0]["url"],
            "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors?download=true",
        )
        self.assertNotIn("/blob/", updated["assets"][0]["url"])

    def test_workflow_delete_clears_state_and_reupload_rebuilds_from_json(self) -> None:
        service = self.make_service()
        payload = {
            "filename": "restored.json",
            "content": json.dumps({"nodes": [{"id": 1, "type": "UNETLoader", "widgets_values": ["current.safetensors"]}]}),
        }
        first = service.upload_comfyui_workflow(payload)
        old_assets = first["extracted_assets"]
        old_assets.append(
            {
                "id": "old_bad_asset",
                "kind": "workflow_model_requirement",
                "filename": "z_image_turbo_bf16.safetensors",
                "model_folder": "diffusion_models",
                "target": "assets/comfyui/diffusion_models/z_image_turbo_bf16.safetensors",
                "url": "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/diffusion_models/z_image_turbo_bf16.safetensors",
                "size_bytes": 1,
                "size_unknown": False,
                "provider": "huggingface",
                "status": "ready",
            }
        )
        service.update_comfyui_workflow(
            first["id"],
            {
                "extracted_assets": old_assets,
                "extra_assets": [{"url": "https://example.com/old-extra.safetensors", "size_bytes": 1}],
                "node_locks": [{"decision": "treat_builtin", "node_types": ["Old Node"]}],
            },
        )
        self.assertEqual(service.delete_comfyui_workflow(first["id"])["ok"], True)
        self.assertEqual(service.list_comfyui_workflows(), [])
        deleted_row = service.db.get("comfyui_workflows", first["id"])
        self.assertEqual(deleted_row["status"], "deleted")
        self.assertEqual(json.loads(deleted_row["extracted_assets_json"]), [])
        self.assertEqual(json.loads(deleted_row["extra_assets_json"]), [])
        self.assertEqual(json.loads(deleted_row["node_locks_json"]), [])
        self.assertEqual(deleted_row["dependency_fingerprint"], "")
        self.assertEqual(deleted_row["launch_fingerprint"], "")

        restored = service.upload_comfyui_workflow({**payload, "filename": "restored-again.json"})
        self.assertEqual(restored["id"], first["id"])
        self.assertNotEqual(restored["status"], "deleted")
        self.assertEqual(restored["original_filename"], "restored-again.json")
        filenames = {asset["filename"] for asset in restored["extracted_assets"]}
        self.assertEqual(filenames, {"current.safetensors"})
        self.assertEqual(restored["extra_assets"], [])
        self.assertEqual(restored["node_locks"], [])
        self.assertEqual([row["id"] for row in service.list_comfyui_workflows()], [first["id"]])

    def test_workflow_extracts_model_requirements_and_blocks_until_sized(self) -> None:
        service = self.make_service()
        workflow = service.upload_comfyui_workflow(
            {
                "filename": "models.json",
                "content": json.dumps(
                    {
                        "nodes": [
                            {
                                "id": 1,
                                "type": "CheckpointLoaderSimple",
                                "widgets_values": ["example.safetensors"],
                            }
                        ]
                    }
                ),
            }
        )
        self.assertEqual(workflow["status"], "needs_model_urls")
        self.assertEqual(workflow["extracted_assets"][0]["filename"], "example.safetensors")
        request = service.create_resource_request({"product": "comfyui", "workflow_id": workflow["id"]})
        self.assertEqual(request["state"], "failed")
        self.assertEqual(request["error"], "workflow_assets_incomplete")
        # The API returns this field name on workflow records; it must work as an
        # input alias instead of silently launching an asset-less paid session.
        alias_request = service.create_resource_request({"product": "comfyui", "comfyui_workflow_id": workflow["id"]})
        self.assertEqual(alias_request["state"], "failed")
        self.assertEqual(alias_request["error"], "workflow_assets_incomplete")
        assets = workflow["extracted_assets"]
        assets[0].update(
            {
                "url": "https://example.com/example.safetensors",
                "size_bytes": 1024,
                "size_unknown": False,
                "status": "ready",
            }
        )
        updated = service.update_comfyui_workflow(workflow["id"], {"extracted_assets": assets})
        self.assertEqual(updated["status"], "ready_to_probe")
        self.assertEqual(updated["ready_assets"][0]["target"], "assets/comfyui/checkpoints/example.safetensors")

    def test_workflow_model_extraction_ignores_ui_properties_model_suggestions(self) -> None:
        workflow = {
            "nodes": [
                {
                    "id": 761,
                    "type": "UNETLoader",
                    "properties": {
                        "models": [
                            {
                                "name": "z_image_turbo_bf16.safetensors",
                                "url": "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/diffusion_models/z_image_turbo_bf16.safetensors",
                                "directory": "diffusion_models",
                            }
                        ]
                    },
                    "widgets_values": ["ComfyUI\\moody-porn-v12.6_00001_.safetensors", "default"],
                }
            ]
        }
        assets = extract_model_requirements(workflow)
        filenames = {asset["filename"] for asset in assets}
        self.assertIn("moody-porn-v12.6_00001_.safetensors", filenames)
        self.assertNotIn("z_image_turbo_bf16.safetensors", filenames)
        self.assertEqual(assets[0]["source_field"], "widgets_values[0]")
        self.assertEqual(assets[0]["model_folder"], "diffusion_models")

    def test_workflow_model_extraction_maps_seedvr2_models_to_seedvr2_folder(self) -> None:
        workflow = {
            "nodes": [
                {"id": 1, "type": "SeedVR2LoadDiTModel", "widgets_values": ["seedvr2_ema_7b-Q4_K_M.gguf"]},
                {"id": 2, "type": "SeedVR2LoadVAEModel", "widgets_values": ["ema_vae_fp16.safetensors"]},
            ]
        }

        assets = extract_model_requirements(workflow)

        self.assertEqual(
            {asset["filename"]: asset["model_folder"] for asset in assets},
            {
                "seedvr2_ema_7b-Q4_K_M.gguf": "SEEDVR2",
                "ema_vae_fp16.safetensors": "SEEDVR2",
            },
        )

    def test_asset_manifest_redacts_hf_and_civitai_tokens(self) -> None:
        assets = normalize_asset_manifest(
            [
                {"url": "https://huggingface.co/org/model/resolve/main/a.safetensors?token=hf_secret", "target": "models/a.safetensors"},
                {"url": "https://civitai.com/api/download/models/123?token=civ_secret"},
            ]
        )
        self.assertEqual(assets[0]["provider"], "huggingface")
        self.assertEqual(assets[1]["provider"], "civitai")
        self.assertIn("<redacted>", assets[0]["url"])
        self.assertIn("<redacted>", assets[1]["url"])
        self.assertNotIn("hf_secret", str(assets))
        self.assertNotIn("civ_secret", str(assets))

    def test_huggingface_blob_url_canonicalizes_to_download_url(self) -> None:
        blob_url = "https://huggingface.co/Comfy-Org/z_image_turbo/blob/main/split_files/text_encoders/qwen_3_4b.safetensors"
        expected = "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors?download=true"
        self.assertEqual(canonical_asset_url(blob_url), expected)
        self.assertEqual(normalized_url_key(blob_url), normalized_url_key(expected))
        assets = normalize_asset_manifest([{"url": blob_url, "model_folder": "text_encoders", "size_bytes": 1}])
        self.assertEqual(assets[0]["url"], expected)

    def test_redact_url_scrubs_signed_download_parameters(self) -> None:
        url = (
            "https://cas-bridge.xethub.hf.co/file?"
            "Expires=123&Policy=policy_secret&Signature=sig_secret&Key-Pair-Id=key_secret"
            "&X-Amz-Credential=cred_secret&X-Amz-Signature=aws_sig_secret"
            "&X-Xet-Cas-Uid=xet_uid_secret"
            "&response-content-disposition=attachment%3Bfilename%3Dmodel.safetensors"
        )
        redacted = redact_url(url)
        self.assertIn("<redacted>", redacted)
        self.assertNotIn("policy_secret", redacted)
        self.assertNotIn("sig_secret", redacted)
        self.assertNotIn("key_secret", redacted)
        self.assertNotIn("cred_secret", redacted)
        self.assertNotIn("aws_sig_secret", redacted)
        self.assertNotIn("xet_uid_secret", redacted)
        self.assertIn("response-content-disposition=attachment;filename=model.safetensors", redacted)

    def test_normalized_url_key_strips_signed_download_parameters(self) -> None:
        url = (
            "https://cas-bridge.xethub.hf.co/file?"
            "Expires=123&Policy=policy_secret&Signature=sig_secret"
            "&X-Amz-Credential=cred_secret&X-Amz-Signature=aws_sig_secret"
            "&X-Xet-Cas-Uid=xet_uid_secret"
            "&response-content-disposition=attachment%3Bfilename%3Dmodel.safetensors"
        )
        key = normalized_url_key(url)
        self.assertNotIn("Expires", key)
        self.assertNotIn("policy_secret", key)
        self.assertNotIn("sig_secret", key)
        self.assertNotIn("cred_secret", key)
        self.assertNotIn("xet_uid_secret", key)
        self.assertIn("response-content-disposition=", key)

    def test_civitai_red_and_green_are_civitai_providers(self) -> None:
        for host in ["civitai.com", "civitai.red", "civitai.green", "www.civitai.red"]:
            self.assertEqual(detect_provider({"url": f"https://{host}/api/download/models/3005223?fileId=2884546"}), "civitai")
        self.assertEqual(detect_provider({"url": "https://notcivitai.com/api/download/models/3005223"}), "generic")

    def test_asset_manifest_maps_model_folder_to_comfyui_asset_target(self) -> None:
        assets = normalize_asset_manifest(
            [
                {
                    "url": "https://huggingface.co/org/model/resolve/main/vae.safetensors",
                    "model_folder": "vae",
                },
                {
                    "url": "https://huggingface.co/org/model/resolve/main/model.bin",
                    "model_folder": "diffusion model",
                    "filename": "flux.safetensors",
                },
            ]
        )
        self.assertEqual(assets[0]["model_folder"], "vae")
        self.assertEqual(assets[0]["target"], "assets/comfyui/vae/vae.safetensors")
        self.assertEqual(assets[1]["model_folder"], "diffusion_models")
        self.assertEqual(assets[1]["target"], "assets/comfyui/diffusion_models/flux.safetensors")

    def test_resource_request_persists_redacted_nested_tokens(self) -> None:
        service = self.make_service()
        request = service.create_resource_request(
            {
                "product": "comfyui",
                "assets": [
                    {"url": "https://huggingface.co/org/model/resolve/main/a.safetensors?token=hf_secret", "size_bytes": 1},
                    {"api_key": "civ_secret"},
                ],
            }
        )
        stored = service.get_resource_request(request["id"])
        self.assertIn("<redacted>", str(stored["requested"]))
        self.assertNotIn("hf_secret", str(stored))
        self.assertNotIn("civ_secret", str(stored))

    def test_secret_env_file_loads_local_tokens_without_git_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_file = root / "secrets" / "controller.env"
            secret_file.parent.mkdir(parents=True)
            secret_file.write_text("HF_TOKEN=hf_local\nCIVITAI_TOKEN=civ_local\n", encoding="utf-8")
            os.environ["CONTROLLER_DATA_DIR"] = str(root)
            settings = load_settings()
            self.assertEqual(settings.secret_env_file, secret_file)
            self.assertEqual(os.environ["HF_TOKEN"], "hf_local")
            self.assertEqual(os.environ["CIVITAI_TOKEN"], "civ_local")

    def test_hydration_env_injects_provider_tokens_only_when_needed(self) -> None:
        service = self.make_service()
        os.environ["HF_TOKEN"] = "hf_secret"
        os.environ["CIVITAI_TOKEN"] = "civ_secret"
        hydration = {
            "session_id": "ses_test",
            "assets_json": '[{"provider":"huggingface"},{"provider":"civitai"}]',
        }
        volume = {"provider_volume_id": "nv_test"}
        env = service._hydration_env("hyd_test", hydration, volume)
        self.assertEqual(env["HF_TOKEN"], "hf_secret")
        self.assertEqual(env["CIVITAI_TOKEN"], "civ_secret")
        self.assertIn("ASSETS_JSON", env)
        env = service._hydration_env("hyd_test", {"assets_json": '[{"provider":"generic"}]'}, volume)
        self.assertNotIn("HF_TOKEN", env)
        self.assertNotIn("CIVITAI_TOKEN", env)

    def test_asset_peek_uses_product_scoped_cache(self) -> None:
        service = self.make_service()
        url = "https://huggingface.co/org/model/resolve/main/model.safetensors?token=secret"
        now = "2026-06-06T00:00:00+00:00"
        service.db.insert(
            "asset_metadata_cache",
            {
                "id": "cache_one",
                "product": "comfyui",
                "url_key": normalized_url_key(url),
                "original_url_redacted": "https://huggingface.co/org/model/resolve/main/model.safetensors?token=<redacted>",
                "final_url_redacted": "https://cdn.example/model.safetensors",
                "provider": "huggingface",
                "model_folder": "checkpoints",
                "filename": "model.safetensors",
                "size_bytes": 123,
                "size_unknown": 0,
                "content_type": "application/octet-stream",
                "etag": "abc",
                "last_modified": None,
                "redirects_json": "[]",
                "target": "assets/comfyui/checkpoints/model.safetensors",
                "observed_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )
        result = service.peek_asset({"url": url, "model_folder": "checkpoints"})
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["size_bytes"], 123)
        self.assertNotIn("secret", str(result))

    def test_asset_peek_refreshes_stale_huggingface_blob_html_cache(self) -> None:
        service = self.make_service()
        url = "https://huggingface.co/Comfy-Org/z_image_turbo/blob/main/split_files/text_encoders/qwen_3_4b.safetensors"
        now = "2026-06-06T00:00:00+00:00"
        service.db.insert(
            "asset_metadata_cache",
            {
                "id": "cache_bad_hf_blob",
                "product": "comfyui",
                "url_key": normalized_url_key(url),
                "original_url_redacted": url,
                "final_url_redacted": url,
                "provider": "huggingface",
                "model_folder": "text_encoders",
                "filename": "qwen_3_4b.safetensors",
                "size_bytes": 1_048_576,
                "size_unknown": 0,
                "content_type": "text/html; charset=utf-8",
                "etag": None,
                "last_modified": None,
                "redirects_json": "[]",
                "target": "assets/comfyui/text_encoders/qwen_3_4b.safetensors",
                "observed_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )
        with patch(
            "controller.service.peek_url_metadata",
            return_value={
                "provider": "huggingface",
                "url_key": normalized_url_key(url),
                "original_url_redacted": url,
                "final_url_redacted": "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors?download=true",
                "filename": "qwen_3_4b.safetensors",
                "size_bytes": 8_589_934_592,
                "size_unknown": False,
                "content_type": "application/octet-stream",
                "etag": "etag",
                "last_modified": None,
                "redirects": [],
                "model_folder": "text_encoders",
                "target": "assets/comfyui/text_encoders/qwen_3_4b.safetensors",
            },
        ) as peek:
            result = service.peek_asset({"url": url, "model_folder": "text_encoders"})

        peek.assert_called_once()
        self.assertFalse(result["cache_hit"])
        self.assertEqual(result["size_bytes"], 8_589_934_592)
        self.assertIn("/resolve/main/", result["final_url_redacted"])
        self.assertNotIn("/blob/main/", result["final_url_redacted"])

    def test_asset_peek_returns_huggingface_download_url_for_ui_save(self) -> None:
        service = self.make_service()
        url = "https://huggingface.co/Comfy-Org/z_image_turbo/blob/main/split_files/text_encoders/qwen_3_4b.safetensors"
        download_url = "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors?download=true"
        with patch(
            "controller.service.peek_url_metadata",
            return_value={
                "provider": "huggingface",
                "url_key": normalized_url_key(url),
                "original_url_redacted": url,
                "download_url_redacted": download_url,
                "final_url_redacted": "https://cas-bridge.xethub.hf.co/file?X-Amz-Signature=<redacted>",
                "filename": "qwen_3_4b.safetensors",
                "size_bytes": 8_044_982_048,
                "size_unknown": False,
                "content_type": "application/octet-stream",
                "etag": None,
                "last_modified": None,
                "redirects": [],
                "model_folder": "text_encoders",
                "target": "assets/comfyui/text_encoders/qwen_3_4b.safetensors",
            },
        ):
            first = service.peek_asset({"url": url, "model_folder": "text_encoders"})
        second = service.peek_asset({"url": url, "model_folder": "text_encoders"})
        self.assertEqual(first["download_url_redacted"], download_url)
        self.assertEqual(second["download_url_redacted"], download_url)
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["size_bytes"], 8_044_982_048)

    def test_asset_peek_refreshes_unredacted_huggingface_signed_cache(self) -> None:
        service = self.make_service()
        url = "https://huggingface.co/Comfy-Org/z_image_turbo/blob/main/split_files/text_encoders/qwen_3_4b.safetensors"
        now = "2026-06-06T00:00:00+00:00"
        service.db.insert(
            "asset_metadata_cache",
            {
                "id": "cache_unredacted_hf_redirect",
                "product": "comfyui",
                "url_key": normalized_url_key(url),
                "original_url_redacted": url,
                "final_url_redacted": "https://cas-bridge.xethub.hf.co/file?X-Xet-Cas-Uid=uid_secret&X-Amz-Signature=sig_secret",
                "provider": "huggingface",
                "model_folder": "text_encoders",
                "filename": "qwen_3_4b.safetensors",
                "size_bytes": 123,
                "size_unknown": 0,
                "content_type": "application/octet-stream",
                "etag": None,
                "last_modified": None,
                "redirects_json": "[]",
                "target": "assets/comfyui/text_encoders/qwen_3_4b.safetensors",
                "observed_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )
        with patch(
            "controller.service.peek_url_metadata",
            return_value={
                "provider": "huggingface",
                "url_key": normalized_url_key(url),
                "original_url_redacted": url,
                "final_url_redacted": "https://cas-bridge.xethub.hf.co/file?X-Xet-Cas-Uid=<redacted>&X-Amz-Signature=<redacted>",
                "filename": "qwen_3_4b.safetensors",
                "size_bytes": 8_044_982_048,
                "size_unknown": False,
                "content_type": "application/octet-stream",
                "etag": None,
                "last_modified": None,
                "redirects": [],
                "model_folder": "text_encoders",
                "target": "assets/comfyui/text_encoders/qwen_3_4b.safetensors",
            },
        ) as peek:
            result = service.peek_asset({"url": url, "model_folder": "text_encoders"})

        peek.assert_called_once()
        self.assertFalse(result["cache_hit"])
        self.assertEqual(result["size_bytes"], 8_044_982_048)
        self.assertNotIn("uid_secret", str(result))
        self.assertNotIn("sig_secret", str(result))

    def test_asset_peek_reuses_redacted_huggingface_signed_cache(self) -> None:
        service = self.make_service()
        url = "https://huggingface.co/Comfy-Org/z_image_turbo/blob/main/split_files/text_encoders/qwen_3_4b.safetensors"
        now = "2026-06-06T00:00:00+00:00"
        service.db.insert(
            "asset_metadata_cache",
            {
                "id": "cache_redacted_hf_redirect",
                "product": "comfyui",
                "url_key": normalized_url_key(url),
                "original_url_redacted": url,
                "final_url_redacted": "https://cas-bridge.xethub.hf.co/file?X-Xet-Cas-Uid=<redacted>&X-Amz-Signature=<redacted>",
                "provider": "huggingface",
                "model_folder": "text_encoders",
                "filename": "qwen_3_4b.safetensors",
                "size_bytes": 8_044_982_048,
                "size_unknown": 0,
                "content_type": "application/octet-stream",
                "etag": None,
                "last_modified": None,
                "redirects_json": "[]",
                "target": "assets/comfyui/text_encoders/qwen_3_4b.safetensors",
                "observed_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )
        with patch("controller.service.peek_url_metadata") as peek:
            result = service.peek_asset({"url": url, "model_folder": "text_encoders"})

        peek.assert_not_called()
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["size_bytes"], 8_044_982_048)

    def test_workflow_launch_refreshes_suspicious_huggingface_small_model_size(self) -> None:
        service = self.make_service()
        workflow = service.upload_comfyui_workflow(
            {"filename": "tiny-cache.json", "content": json.dumps({"nodes": []})}
        )
        url = "https://huggingface.co/dseditor/UpscaleModels/resolve/main/4xNomosWebPhoto_RealPLKSR.pth?download=true"
        workflow = service.update_comfyui_workflow(
            workflow["id"],
            {
                "extracted_assets": [
                    {
                        "id": "req_bad_hf_size",
                        "filename": "4xNomosWebPhoto_RealPLKSR.pth",
                        "model_folder": "upscale_models",
                        "provider": "huggingface",
                        "size_bytes": 83_953,
                        "size_unknown": False,
                        "status": "ready",
                        "target": "assets/comfyui/upscale_models/4xNomosWebPhoto_RealPLKSR.pth",
                        "url": url,
                    }
                ]
            },
        )
        with patch(
            "controller.service.peek_url_metadata",
            return_value={
                "provider": "huggingface",
                "url_key": normalized_url_key(url),
                "original_url_redacted": url,
                "download_url_redacted": url,
                "final_url_redacted": "https://cas-bridge.xethub.hf.co/file?X-Amz-Signature=<redacted>",
                "filename": "4xNomosWebPhoto_RealPLKSR.pth",
                "size_bytes": 29_683_482,
                "size_unknown": False,
                "content_type": "application/octet-stream",
                "etag": None,
                "last_modified": None,
                "redirects": [],
                "model_folder": "upscale_models",
                "target": "assets/comfyui/upscale_models/4xNomosWebPhoto_RealPLKSR.pth",
            },
        ) as peek:
            refreshed = service._refresh_suspicious_workflow_assets(workflow)  # noqa: SLF001

        peek.assert_called_once()
        self.assertEqual(refreshed["ready_assets"][0]["size_bytes"], 29_683_482)

    def test_asset_peek_http_error_does_not_use_error_page_size(self) -> None:
        with patch(
            "controller.asset_metadata._request_follow",
            return_value={
                "status": 403,
                "headers": {"content-length": "1048576", "content-type": "text/html"},
                "final_url": "https://example.com/not-a-model",
                "redirects": [],
            },
        ) as request:
            with self.assertRaisesRegex(RuntimeError, "http_status:403"):
                peek_url_metadata("https://example.com/not-a-model", "checkpoints")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["HEAD"])

    def test_huggingface_peek_injects_provider_auth_without_exposing_tokens(self) -> None:
        calls = []

        def fake_request(method: str, url: str, *, headers: dict[str, str], timeout: int) -> dict[str, object]:
            calls.append({"method": method, "url": url, "headers": dict(headers)})
            return {
                "status": 200,
                "headers": {"content-length": "123", "content-disposition": 'attachment; filename="model.safetensors"'},
                "final_url": url,
                "redirects": [],
            }

        with patch.dict(os.environ, {"HF_TOKEN": "hf_test_token"}, clear=False):
            with patch("controller.asset_metadata._request_follow", side_effect=fake_request):
                huggingface = peek_url_metadata("https://huggingface.co/org/model/resolve/main/model.safetensors", "checkpoints")

        self.assertEqual(calls[0]["method"], "HEAD")
        self.assertNotIn("hf_test_token", str(calls[0]["url"]))
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer hf_test_token")
        self.assertNotIn("hf_test_token", str(huggingface))

    def test_huggingface_blob_url_peek_rewrites_to_download_url(self) -> None:
        calls = []

        def fake_request(method: str, url: str, *, headers: dict[str, str], timeout: int) -> dict[str, object]:
            calls.append({"method": method, "url": url, "headers": dict(headers)})
            return {
                "status": 200,
                "headers": {"content-length": str(8_589_934_592)},
                "final_url": url,
                "redirects": [],
            }

        blob_url = "https://huggingface.co/Comfy-Org/z_image_turbo/blob/main/split_files/text_encoders/qwen_3_4b.safetensors"
        with patch("controller.asset_metadata._request_follow", side_effect=fake_request):
            result = peek_url_metadata(blob_url, "text_encoders")

        self.assertEqual(calls[0]["method"], "HEAD")
        self.assertEqual(
            calls[0]["url"],
            "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors?download=true",
        )
        self.assertEqual(result["url_key"], normalized_url_key(blob_url))
        self.assertEqual(
            result["download_url_redacted"],
            "https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors?download=true",
        )
        self.assertEqual(result["filename"], "qwen_3_4b.safetensors")
        self.assertEqual(result["size_bytes"], 8_589_934_592)
        self.assertIn("/resolve/main/", result["final_url_redacted"])
        self.assertNotIn("/blob/main/", result["final_url_redacted"])

    def test_huggingface_resolve_url_peek_adds_download_true(self) -> None:
        calls = []

        def fake_request(method: str, url: str, *, headers: dict[str, str], timeout: int) -> dict[str, object]:
            calls.append(url)
            return {
                "status": 200,
                "headers": {"content-length": "456"},
                "final_url": url,
                "redirects": [],
            }

        resolve_url = "https://huggingface.co/org/model/resolve/main/model.safetensors"
        with patch("controller.asset_metadata._request_follow", side_effect=fake_request):
            result = peek_url_metadata(resolve_url, "checkpoints")

        self.assertEqual(calls[0], "https://huggingface.co/org/model/resolve/main/model.safetensors?download=true")
        self.assertEqual(result["url_key"], normalized_url_key(resolve_url))
        self.assertEqual(result["size_bytes"], 456)

    def test_civitai_download_url_uses_model_version_metadata_api(self) -> None:
        with patch.dict(os.environ, {"CIVITAI_TOKEN": "civ_test_token"}, clear=False):
            with patch(
                "controller.asset_metadata._request_json",
                return_value={
                    "files": [
                        {
                            "id": 2884421,
                            "name": "moody-porn-v12.6_00001_.safetensors",
                            "sizeKB": 2048,
                            "downloadUrl": "https://civitai.red/api/download/models/3005060?fileId=2884421&token=secret",
                        }
                    ]
                },
            ) as request_json:
                with patch("controller.asset_metadata._request_follow") as request_follow:
                    result = peek_url_metadata("https://civitai.red/api/download/models/3005060?fileId=2884421", "diffusion_models")

        request_follow.assert_not_called()
        self.assertEqual(request_json.call_args.args[0], "https://civitai.com/api/v1/model-versions/3005060")
        self.assertEqual(request_json.call_args.kwargs["headers"]["Authorization"], "Bearer civ_test_token")
        self.assertEqual(result["provider"], "civitai")
        self.assertEqual(result["filename"], "moody-porn-v12.6_00001_.safetensors")
        self.assertEqual(result["size_bytes"], 2_097_152)
        self.assertEqual(result["target"], "assets/comfyui/diffusion_models/moody-porn-v12.6_00001_.safetensors")
        self.assertNotIn("secret", str(result))
        self.assertNotIn("civ_test_token", str(result))

    def test_civitai_direct_download_fallback_uses_token_query_without_bearer_header(self) -> None:
        calls = []

        def fake_request(method: str, url: str, *, headers: dict[str, str], timeout: int) -> dict[str, object]:
            calls.append({"method": method, "url": url, "headers": dict(headers)})
            return {
                "status": 200,
                "headers": {"content-length": "12345", "content-disposition": 'attachment; filename="model.safetensors"'},
                "final_url": url,
                "redirects": [],
            }

        with patch.dict(os.environ, {"CIVITAI_TOKEN": "civ_test_token"}, clear=False):
            with patch("controller.asset_metadata._peek_civitai_download_metadata", return_value=None):
                with patch("controller.asset_metadata._request_follow", side_effect=fake_request):
                    result = peek_url_metadata("https://civitai.red/api/download/models/3005060?fileId=2884421", "diffusion_models")

        self.assertEqual(calls[0]["method"], "HEAD")
        self.assertIn("token=civ_test_token", calls[0]["url"])
        self.assertNotIn("Authorization", calls[0]["headers"])
        self.assertEqual(result["size_bytes"], 12345)
        self.assertNotIn("civ_test_token", str(result))

    def test_volume_size_calculation_requires_known_asset_sizes(self) -> None:
        gib = 1024**3
        self.assertEqual(volume_size_gb_for_assets([{"size_bytes": gib + 1}]), 10)
        self.assertEqual(volume_size_gb_for_assets([{"size_bytes": 11 * gib}]), 19)
        moody_minimal_sizes = [
            6_154_968_736,
            8_044_982_048,
            335_304_388,
        ]
        self.assertEqual(volume_size_gb_for_assets([{"size_bytes": size} for size in moody_minimal_sizes]), 22)
        with self.assertRaises(ValueError):
            volume_size_gb_for_assets([{"size_bytes": None}])

    def test_duplicate_asset_urls_are_detected_even_with_different_targets(self) -> None:
        assets = normalize_asset_manifest(
            [
                {"url": "https://example.com/model.safetensors?token=a", "model_folder": "checkpoints", "size_bytes": 1},
                {"url": "https://example.com/model.safetensors?token=b", "model_folder": "vae", "size_bytes": 1},
            ]
        )
        self.assertEqual(duplicate_url_keys(assets), ["https://example.com/model.safetensors"])

    def test_duplicate_asset_urls_block_create(self) -> None:
        service = self.make_service()
        request = service.create_resource_request(
            {
                "product": "comfyui",
                "assets": [
                    {"url": "https://example.com/model.safetensors", "model_folder": "checkpoints", "size_bytes": 1},
                    {"url": "https://example.com/model.safetensors", "model_folder": "vae", "size_bytes": 1},
                ],
            }
        )
        self.assertEqual(request["state"], "failed")
        stored = service.get_resource_request(request["id"])
        self.assertEqual(stored["error"], "duplicate_asset_url")

    def test_workflow_keeps_gpu_creation_serial_and_cleans_losers(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        adapter = ConcurrentGpuAdapter(settings, parties=1)
        service = ControllerService(settings, db, adapter)
        request = service.create_resource_request(
            {"product": "comfyui", "data_centers": ["US-KS-2", "US-IL-1", "EU-RO-1"]}
        )
        self.assertEqual(request["state"], "interactive_ready")
        self.assertEqual(adapter.max_active_gpu_creates, 1)
        session = service.get_session(request["session_id"])
        self.assertEqual(session["candidate_count"], 3)
        gpu_pods = [pod for pod in service.list_pods() if pod["compute_type"] == "GPU"]
        self.assertEqual(len(gpu_pods), 1)
        self.assertEqual(sum(1 for pod in gpu_pods if pod["state"] == "running"), 1)
        self.assertEqual(session["workflow"]["state"], "interactive_ready")
        self.assertEqual(len({attempt["attempt_number"] for attempt in session["gpu_acquisition_attempts"]}), 1)
        loser_candidates = [
            candidate for candidate in session["workflow"]["candidates"]
            if candidate["id"] != session["winner_candidate_id"]
        ]
        self.assertTrue(all(candidate["state"] == "deleted" for candidate in loser_candidates))

    def test_environment_config_failure_after_cleanup_goes_to_history(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        service = ControllerService(settings, db, FailingEnvironmentConfigAdapter(settings))

        request = service.create_resource_request({"product": "comfyui", "data_centers": ["US-KS-2", "EU-RO-1"]})

        self.assertEqual(request["state"], "failed")
        session = service.get_session(request["session_id"])
        self.assertEqual(session["state"], "failed")
        self.assertEqual(session["phase"], "failed")
        self.assertTrue(all(pod["state"] == "deleted" for pod in session["pods"]))
        candidate_volumes = [
            service.db.get("network_volumes", candidate["volume_id"])
            for candidate in session["workflow"]["candidates"]
            if candidate.get("volume_id")
        ]
        self.assertTrue(all(volume and volume["state"] == "deleted" for volume in candidate_volumes))
        active_html = dashboard(service.cost_report()).decode("utf-8")
        history_html = history_page(service.cost_report()).decode("utf-8")
        self.assertNotIn(request["session_id"], active_html)
        self.assertIn(request["session_id"], history_html)

    def test_failed_winner_configuration_falls_back_to_next_candidate(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        adapter = FailFirstEnvironmentConfigAdapter(settings)
        service = ControllerService(settings, db, adapter)

        request = service.create_resource_request({"product": "comfyui", "data_centers": ["US-KS-2", "EU-RO-1"]})

        self.assertEqual(request["state"], "interactive_ready")
        session = service.get_session(request["session_id"])
        self.assertEqual(session["state"], "interactive_ready")
        self.assertGreaterEqual(adapter.configure_calls, 2)
        candidates = session["workflow"]["candidates"]
        self.assertEqual(sum(1 for candidate in candidates if candidate["state"] == "won"), 1)
        self.assertTrue(any("environment_config_failed" in str(candidate.get("last_error") or "") for candidate in candidates))
        events = [event["event_type"] for event in session["workflow"]["events"]]
        self.assertIn("environment_config_failed", events)
        self.assertIn("winner_candidate_rejected", events)
        self.assertIn("interactive_ready", events)

    def test_reclaim_one_session_does_not_delete_another_session_resources(self) -> None:
        service = self.make_service()
        first = service.create_resource_request({"product": "comfyui", "data_centers": ["US-KS-2", "US-IL-1"]})
        second = service.create_resource_request({"product": "comfyui", "data_centers": ["EU-RO-1", "US-WA-1"]})
        second_before = service.get_session(second["session_id"])
        second_volume_id = second_before["network_volume_id"]
        second_gpu_pod_id = second_before["gpu_pod_id"]
        self.reclaim_with_output(service, first["session_id"])
        second_after = service.get_session(second["session_id"])
        self.assertEqual(second_after["state"], "interactive_ready")
        self.assertEqual(service.db.get("network_volumes", second_volume_id)["state"], "hydrated")
        self.assertEqual(service.db.get("pods", second_gpu_pod_id)["state"], "running")

    def test_reclaim_does_not_mark_volume_deleted_when_provider_delete_fails(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        service = ControllerService(settings, db, FailingVolumeDeleteAdapter(settings))
        request = service.create_resource_request({"product": "comfyui"})
        result, _client = self.reclaim_with_output(service, request["session_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "cleanup_failed")
        session = service.get_session(request["session_id"])
        volume = service.db.get("network_volumes", session["network_volume_id"])
        self.assertEqual(session["state"], "cleanup_failed")
        self.assertNotEqual(volume["state"], "deleted")

    def test_rest_adapter_returns_error_result_on_network_failure(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        adapter = RunpodRestAdapter(settings)
        adapter.api_key = "test-key"
        with patch("controller.runpod.urllib.request.urlopen", side_effect=urllib.error.URLError("dns down")):
            result = adapter.delete_pod("pod-123")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 0)
        self.assertIn("network_error", str(result["response"]))

    def test_s3_list_objects_paginates_past_truncated_pages(self) -> None:
        client = RunpodS3VolumeClient.__new__(RunpodS3VolumeClient)
        client.data_center_id = "US-KS-2"
        client.volume_id = "vol"
        client.access_key = "ak"
        client.secret_key = "sk"
        pages = [
            (
                "<ListBucketResult><IsTruncated>true</IsTruncated><NextContinuationToken>tok1</NextContinuationToken>"
                "<Contents><Key>runs/a0.png</Key><Size>10</Size></Contents>"
                "<Contents><Key>runs/a1.png</Key><Size>11</Size></Contents>"
                "</ListBucketResult>"
            ).encode("utf-8"),
            (
                "<ListBucketResult><IsTruncated>false</IsTruncated>"
                "<Contents><Key>runs/b.png</Key><Size>20</Size></Contents></ListBucketResult>"
            ).encode("utf-8"),
        ]
        queries: list[dict[str, str]] = []

        def fake_request(method: str, *, key: str = "", payload: bytes = b"", query: dict[str, str] | None = None, extra_headers: dict[str, str] | None = None, timeout: int = 120):
            queries.append(dict(query or {}))
            return 200, pages[len(queries) - 1], {}

        client._request = fake_request  # type: ignore[method-assign]
        objects = client.list_objects("runs/")
        self.assertEqual([item.key for item in objects], ["runs/a0.png", "runs/a1.png", "runs/b.png"])
        self.assertEqual(queries[1].get("continuation-token"), "tok1")

    def test_s3_list_objects_terminates_on_runpod_truncation_oscillation(self) -> None:
        # Observed live on EUR-IS-1: RunPod's S3 returns IsTruncated=true with
        # KeyCount < MaxKeys and cycles continuation tokens forever.
        client = RunpodS3VolumeClient.__new__(RunpodS3VolumeClient)
        client.data_center_id = "EUR-IS-1"
        client.volume_id = "vol"
        client.access_key = "ak"
        client.secret_key = "sk"
        page_with_key = (
            "<ListBucketResult><KeyCount>1</KeyCount><MaxKeys>1000</MaxKeys>"
            "<Contents><Key>runs/a.png</Key><Size>10</Size></Contents>"
            "<IsTruncated>true</IsTruncated><NextContinuationToken>tokA</NextContinuationToken></ListBucketResult>"
        ).encode("utf-8")
        empty_page = (
            "<ListBucketResult><KeyCount>0</KeyCount><MaxKeys>1000</MaxKeys>"
            "<IsTruncated>true</IsTruncated><NextContinuationToken>tokB</NextContinuationToken></ListBucketResult>"
        ).encode("utf-8")
        calls = []

        def fake_request(method: str, *, key: str = "", payload: bytes = b"", query: dict[str, str] | None = None, extra_headers: dict[str, str] | None = None, timeout: int = 120):
            calls.append(dict(query or {}))
            return 200, page_with_key if len(calls) % 2 == 1 else empty_page, {}

        client._request = fake_request  # type: ignore[method-assign]
        objects = client.list_objects("runs/")
        self.assertEqual([item.key for item in objects], ["runs/a.png"])
        self.assertLessEqual(len(calls), 3)

    def test_output_collection_lock_released_when_setup_fails(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session_id = request["session_id"]
        original_insert = service.db.insert

        def failing_insert(table: str, values: dict[str, object]) -> None:
            if table == "output_collections":
                raise RuntimeError("database is locked")
            original_insert(table, values)

        with patch.object(service.db, "insert", side_effect=failing_insert):
            with self.assertRaises(RuntimeError):
                service.collect_session_outputs(session_id, mode="manual")
        with patch("controller.service.has_s3_credentials", return_value=False):
            result = service.collect_session_outputs(session_id, mode="manual")
        self.assertNotEqual(result.get("state"), "already_running")

    def test_periodic_collection_skips_terminal_session(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session_id = request["session_id"]
        self.reclaim_with_output(service, session_id)
        result = service.collect_session_outputs(session_id, mode="periodic")
        self.assertEqual(result["state"], "skipped_session_state")
        session = service.get_session(session_id)
        self.assertEqual(session["state"], "reclaimed")
        self.assertNotEqual(session["output_collection_state"], "failed")

    def test_watchdog_tick_does_not_clobber_concurrent_transition(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session_id = request["session_id"]
        stale = dict(service.db.get("sessions", session_id) or {})
        real_get = service.db.get

        def stale_get(table: str, row_id: str):
            if table == "sessions" and row_id == session_id:
                return dict(stale)
            return real_get(table, row_id)

        service.db.update("sessions", session_id, {"state": "collecting_outputs", "phase": "collecting_outputs", "updated_at": "2026-06-12T00:00:00+00:00"})
        with patch.object(service.db, "get", side_effect=stale_get):
            service.watchdog_tick(session_id, {"queue_active": False, "output_active": False})
        after = real_get("sessions", session_id)
        self.assertEqual(after["state"], "collecting_outputs")
        self.assertEqual(after["phase"], "collecting_outputs")

    def test_watchdog_idle_reclaims_expired_session(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session_id = request["session_id"]
        old = "2026-01-01T00:00:00+00:00"
        service.db.update(
            "sessions",
            session_id,
            {"lease_until": old, "idle_shutdown_at": old, "reclaim_warning_at": old, "updated_at": old},
        )
        client = self.s3_output_client(session_id)
        with (
            patch("controller.service.has_s3_credentials", return_value=True),
            patch("controller.service.RunpodS3VolumeClient", return_value=client),
        ):
            tick = service.watchdog_tick(session_id, {"queue_active": False, "output_active": False})
        self.assertEqual(tick["action"], "reclaim")
        session = service.get_session(session_id)
        self.assertEqual(session["state"], "reclaimed")
        self.assertEqual(session["missing_finalization_reason"], "idle_shutdown_reached")

    def test_watchdog_never_reclaims_active_session_by_clock(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session_id = request["session_id"]
        old = "2026-01-01T00:00:00+00:00"
        # Lease and idle deadlines long past, but ComfyUI is actively generating.
        service.db.update(
            "sessions",
            session_id,
            {"lease_until": old, "idle_shutdown_at": old, "reclaim_warning_at": old, "updated_at": old},
        )
        tick = service.watchdog_tick(session_id, {"queue_active": True, "output_active": False})
        self.assertEqual(tick["action"], "none")
        self.assertEqual(service.get_session(session_id)["state"], "interactive_ready")

    def test_watchdog_cost_cap_reclaims_over_budget_session(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session_id = request["session_id"]
        session = service.get_session(session_id)
        # Make the running GPU pod expensive enough to blow the budget.
        service.db.update("pods", session["gpu_pod_id"], {"cost_per_hr": 60.0, "created_at": "2026-01-01T00:00:00+00:00"})
        service.db.update("sessions", session_id, {"max_total_usd": 5.0})
        client = self.s3_output_client(session_id)
        with (
            patch("controller.service.has_s3_credentials", return_value=True),
            patch("controller.service.RunpodS3VolumeClient", return_value=client),
        ):
            tick = service.watchdog_tick(session_id, {"queue_active": True, "output_active": True})
        self.assertEqual(tick["action"], "reclaim")
        self.assertEqual(tick["reason"], "cost_cap_reached")
        after = service.get_session(session_id)
        self.assertEqual(after["state"], "reclaimed")
        self.assertEqual(after["missing_finalization_reason"], "cost_cap_reached")

    def test_watchdog_warns_when_spend_nears_cost_cap(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session_id = request["session_id"]
        session = service.get_session(session_id)
        service.db.update("pods", session["gpu_pod_id"], {"cost_per_hr": 60.0, "created_at": "2026-01-01T00:00:00+00:00"})
        spend = service._session_live_spend(service.db.get("sessions", session_id))
        self.assertGreater(spend, 0)
        service.db.update("sessions", session_id, {"max_total_usd": spend / 0.95})
        tick = service.watchdog_tick(session_id, {"queue_active": True, "output_active": True})
        self.assertEqual(tick["action"], "warn")
        self.assertEqual(tick["reason"], "cost_cap_warning")
        self.assertEqual(service.get_session(session_id)["state"], "reclaim_pending")

    def test_forced_reclaim_during_race_cleans_candidate_resources(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui", "data_centers": ["US-KS-2"]})
        session_id = request["session_id"]
        session = service.get_session(session_id)
        workflow_id = session["workflow"]["id"]
        now = "2026-06-12T00:00:00+00:00"
        service.db.insert(
            "network_volumes",
            {
                "id": "vol_race",
                "provider_volume_id": "prov-vol-race",
                "name": "race",
                "data_center_id": "US-IL-1",
                "size_gb": 10,
                "state": "hydrated",
                "hydration_state": "hydrated",
                "retention_policy": "delete_after_collection",
                "estimated_cost_usd": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        service.db.insert(
            "pods",
            {
                "id": "pod_race",
                "provider_pod_id": "prov-pod-race",
                "session_id": session_id,
                "volume_id": "vol_race",
                "role": "hydration_cpu",
                "compute_type": "CPU",
                "state": "running",
                "data_center_id": "US-IL-1",
                "image": "test",
                "created_at": now,
                "updated_at": now,
            },
        )
        service.db.insert(
            "workflow_candidates",
            {
                "id": "cand_race",
                "workflow_id": workflow_id,
                "session_id": session_id,
                "data_center_id": "US-IL-1",
                "state": "hydrating",
                "volume_id": "vol_race",
                "cpu_pod_id": "pod_race",
                "created_at": now,
                "updated_at": now,
            },
        )
        # Simulate the mid-race shape: no winner yet, session row has no resource pointers.
        service.db.update(
            "sessions",
            session_id,
            {"state": "hydrating_all", "phase": "hydrating_all", "network_volume_id": None, "gpu_pod_id": None, "cpu_pod_id": None, "updated_at": now},
        )
        result = service.reclaim_session(session_id, {"force": True})
        self.assertTrue(result["ok"])
        self.assertEqual(service.db.get("pods", "pod_race")["state"], "deleted")
        self.assertEqual(service.db.get("network_volumes", "vol_race")["state"], "deleted")
        self.assertEqual(service.db.get("workflow_candidates", "cand_race")["state"], "deleted")

    def test_winner_claim_blocked_after_session_reclaimed(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session_id = request["session_id"]
        session = service.get_session(session_id)
        workflow_id = session["workflow"]["id"]
        winner = next(candidate for candidate in session["workflow"]["candidates"] if candidate["state"] == "won")
        self.reclaim_with_output(service, session_id)
        claimed = service._claim_candidate_as_winner(workflow_id, session_id, winner["id"])
        self.assertFalse(claimed)
        self.assertEqual(service.get_session(session_id)["state"], "reclaimed")

    def test_billing_calibration_uses_single_bucket_size(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session = service.get_session(request["session_id"])
        pod = service.db.get("pods", session["gpu_pod_id"])
        provider_id = pod["provider_pod_id"]
        rows = [
            ("2026-06-10T00:00:00Z", "hour", 0.5, "2026-06-10T02:00:00Z"),
            ("2026-06-10T01:00:00Z", "hour", 0.5, "2026-06-10T02:00:00Z"),
            ("2026-06-10T00:00:00Z", "day", 1.1, "2026-06-09T00:00:00Z"),
        ]
        for start, size, amount, observed in rows:
            service._upsert_billing_record(
                source="test",
                provider="runpod",
                resource_type="pod",
                resource_id=pod["id"],
                provider_resource_id=provider_id,
                bucket_start_at=start,
                bucket_end_at=None,
                bucket_size=size,
                amount_usd=amount,
                time_billed_ms=None,
                disk_space_billed_gb=None,
                raw={},
                observed_at=observed,
            )
        service._apply_pod_billing_calibrations({provider_id}, observed_at="2026-06-10T03:00:00Z")
        pod_after = service.db.get("pods", pod["id"])
        self.assertEqual(pod_after["actual_cost_usd"], 1.0)

    def test_orphan_sweep_reclaims_terminal_session_resources(self) -> None:
        service = self.make_service()
        active = service.create_resource_request({"product": "comfyui"})
        stale = service.create_resource_request({"product": "comfyui", "data_centers": ["EU-RO-1"]})
        stale_session = service.get_session(stale["session_id"])
        service.db.update("sessions", stale["session_id"], {"state": "failed", "phase": "failed", "updated_at": "2026-06-12T00:00:00+00:00"})
        results = service.reconcile_orphan_resources()
        self.assertIn(stale_session["gpu_pod_id"], results["pods_deleted"])
        self.assertIn(stale_session["network_volume_id"], results["volumes_deleted"])
        active_session = service.get_session(active["session_id"])
        self.assertEqual(service.db.get("pods", active_session["gpu_pod_id"])["state"], "running")
        self.assertEqual(service.db.get("network_volumes", active_session["network_volume_id"])["state"], "hydrated")

    def test_orphan_sweep_keeps_retained_volume_and_resets_stuck_collection(self) -> None:
        service = self.make_service()
        kept = service.create_resource_request({"product": "comfyui"})
        service.db.update("sessions", kept["session_id"], {"state": "output_collection_failed_keep_volume", "phase": "output_collection_failed_keep_volume", "updated_at": "2026-06-12T00:00:00+00:00"})
        stuck = service.create_resource_request({"product": "comfyui", "data_centers": ["US-IL-1"]})
        service.db.update("sessions", stuck["session_id"], {"state": "collecting_outputs", "phase": "collecting_outputs", "updated_at": "2026-06-12T00:00:00+00:00"})
        results = service.reconcile_orphan_resources()
        kept_session = service.get_session(kept["session_id"])
        self.assertEqual(service.db.get("network_volumes", kept_session["network_volume_id"])["state"], "hydrated")
        self.assertIn(stuck["session_id"], results["sessions_reset"])
        self.assertEqual(service.get_session(stuck["session_id"])["state"], "reclaim_pending")

    def test_default_data_dir_is_home_based_and_overridable(self) -> None:
        env = {
            "CONTROLLER_SECRET_ENV_FILE": "/nonexistent/secrets.env",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CONTROLLER_DATA_DIR", None)
            settings = load_settings()
            self.assertEqual(settings.data_dir, Path.home() / "runpod-controller")
        with patch.dict(os.environ, {**env, "CONTROLLER_DATA_DIR": "/tmp/custom-ctl"}, clear=False):
            settings = load_settings()
            self.assertEqual(str(settings.data_dir), "/tmp/custom-ctl")

    def test_locale_detection_prefers_browser_language(self) -> None:
        self.assertEqual(detect_locale("zh-CN,zh;q=0.9,en;q=0.8"), "zh")
        self.assertEqual(detect_locale("en-US,en;q=0.9,zh;q=0.8"), "en")
        self.assertEqual(detect_locale("ja-JP,fr;q=0.9"), "en")
        self.assertEqual(detect_locale(""), "en")
        self.assertEqual(detect_locale("en-US", override="zh"), "zh")
        self.assertEqual(detect_locale("zh-CN", override="en"), "en")

    def test_chinese_locale_renders_translated_pages(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session = service.get_session(request["session_id"])
        try:
            set_locale("zh")
            dash = dashboard(service.cost_report()).decode("utf-8")
            self.assertIn("活跃会话", dash)
            self.assertIn("创建 ComfyUI", dash)
            overview = session_detail(dict(session)).decode("utf-8")
            self.assertIn("候选计划", overview)
            self.assertIn("关闭：停止 GPU 并采集输出", overview)
            self.assertIn("机房", overview)
            self.assertIn('lang="zh"', overview)
            self.assertIn("window.__T_MAP", overview)
            wizard = comfyui_new_page().decode("utf-8")
            self.assertIn("新建 ComfyUI 会话", wizard)
            self.assertIn("上传工作流", wizard)
        finally:
            set_locale("en")
        english = session_detail(dict(session)).decode("utf-8")
        self.assertIn("Candidate Plan", english)
        self.assertIn('lang="en"', english)

    def test_open_outputs_locally_uses_file_manager(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session_id = request["session_id"]
        missing = service.open_session_outputs_locally(session_id)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error"], "no_local_outputs")
        self.reclaim_with_output(service, session_id)
        calls: list[list[str]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("controller.service.subprocess.run", side_effect=fake_run):
            result = service.open_session_outputs_locally(session_id)
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)
        self.assertIn(f"sessions/{session_id}/outputs", calls[0][-1])
        terminal_html = session_detail(service.get_session(session_id)).decode("utf-8")
        self.assertIn("Open output folder", terminal_html)
        self.assertNotIn("Open ComfyUI", terminal_html)

    def test_reclaim_discard_outputs_deletes_retained_volume(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session_id = request["session_id"]
        with patch("controller.service.has_s3_credentials", return_value=False):
            first = service.reclaim_session(session_id, {"force": True})
            self.assertFalse(first["ok"])
            self.assertEqual(first["state"], "output_collection_failed_keep_volume")
            retry = service.reclaim_session(session_id, {"force": True, "discard_outputs": True})
        self.assertTrue(retry["ok"])
        self.assertEqual(retry["state"], "reclaimed")
        session = service.get_session(session_id)
        self.assertEqual(service.db.get("network_volumes", session["network_volume_id"])["state"], "deleted")

    def test_remote_scripts_strip_redacted_params_and_check_conflicts_first(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        adapter = RunpodRestAdapter(settings)
        hydration = adapter._hydration_shell()
        self.assertIn("strip_redacted_params", hydration)
        self.assertIn("asset_target_outside_root", hydration)
        download = adapter._runtime_model_download_script()
        self.assertIn('"<redacted>"', download)
        self.assertIn("conflict:", download)
        move = adapter._runtime_model_move_script()
        self.assertLess(move.index("comfyui_target_exists"), move.index("os.replace(source, target)"))
        environment = adapter._comfyui_environment_script()
        self.assertIn("conflict:", environment)
        self.assertIn("forced_output_comfyui_died", environment)
        self.assertIn("restart_alive", environment)

    def test_unknown_asset_size_blocks_create(self) -> None:
        service = self.make_service()
        request = service.create_resource_request(
            {"product": "comfyui", "assets": [{"url": "https://example.com/model.safetensors", "model_folder": "checkpoints"}]}
        )
        self.assertEqual(request["state"], "failed")
        stored = service.get_resource_request(request["id"])
        self.assertEqual(stored["error"], "asset_size_unknown")

    def test_failed_asset_peek_is_not_cached(self) -> None:
        service = self.make_service()
        with patch("controller.service.peek_url_metadata", side_effect=RuntimeError("http_status:403")):
            result = service.peek_asset({"url": "https://example.com/not-a-model", "model_folder": "checkpoints"})
        self.assertIn("http_status:403", result["error"])
        self.assertEqual(service.list_asset_metadata_cache("comfyui"), [])

    def test_model_template_crud_is_product_scoped(self) -> None:
        service = self.make_service()
        template = service.create_model_template(
            {"product": "comfyui", "name": "flux", "assets": [{"url": "https://example.com/a.safetensors", "size_bytes": 10}]}
        )
        self.assertEqual(len(service.list_model_templates("comfyui")), 1)
        self.assertEqual(service.list_model_templates("llm-test"), [])
        updated = service.update_model_template(template["id"], {"name": "flux updated"})
        self.assertEqual(updated["name"], "flux updated")
        deleted = service.delete_model_template(template["id"])
        self.assertTrue(deleted["ok"])
        self.assertEqual(service.list_model_templates("comfyui"), [])

    def test_model_template_preserves_filename_and_target(self) -> None:
        service = self.make_service()
        template = service.create_model_template(
            {
                "product": "comfyui",
                "name": "moody",
                "assets": [
                    {
                        "url": "https://huggingface.co/example/model/resolve/main/qwen_3_4b.safetensors",
                        "model_folder": "text_encoders",
                        "filename": "qwen_3_4b.safetensors",
                        "target": "assets/comfyui/text_encoders/qwen_3_4b.safetensors",
                        "size_bytes": 123,
                    },
                    {
                        "url": "https://huggingface.co/example/model/resolve/main/face_yolov8m.pt",
                        "model_folder": "ultralytics",
                        "filename": "face_yolov8m.pt",
                        "target": "assets/comfyui/ultralytics/bbox/face_yolov8m.pt",
                        "size_bytes": None,
                        "size_unknown": True,
                    },
                ],
            }
        )
        assets = template["assets"]
        self.assertEqual(assets[0]["model_folder"], "text_encoders")
        self.assertEqual(assets[0]["filename"], "qwen_3_4b.safetensors")
        self.assertEqual(assets[0]["target"], "assets/comfyui/text_encoders/qwen_3_4b.safetensors")
        self.assertFalse(assets[0]["size_unknown"])
        self.assertEqual(assets[1]["model_folder"], "ultralytics")
        self.assertEqual(assets[1]["filename"], "face_yolov8m.pt")
        self.assertEqual(assets[1]["target"], "assets/comfyui/ultralytics/bbox/face_yolov8m.pt")
        self.assertTrue(assets[1]["size_unknown"])

    def test_finalize_and_reclaim_deletes_session_volume(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session_id = request["session_id"]
        finalized = service.finalize_session(session_id, {"note": "done"})
        self.assertEqual(finalized["state"], "finalized")
        result = service.reclaim_session(session_id, {})
        self.assertTrue(result["ok"])
        session = service.get_session(session_id)
        volume = service.db.get("network_volumes", session["network_volume_id"])
        self.assertEqual(session["state"], "reclaimed")
        self.assertEqual(volume["state"], "deleted")

    def test_reports_include_active_counts(self) -> None:
        service = self.make_service()
        service.create_resource_request({"product": "comfyui"})
        report = service.cost_report()
        self.assertEqual(report["summary"]["sessions"], 1)
        self.assertEqual(report["summary"]["active_sessions"], 1)
        self.assertEqual(report["summary"]["active_volumes"], 1)

    def test_running_pod_cost_uses_now_minus_created_at(self) -> None:
        now = dt.datetime(2026, 6, 6, 2, 0, tzinfo=dt.UTC)
        pod = enrich_pod_cost(
            {
                "provider_pod_id": "pod-real",
                "created_at": "2026-06-06T00:00:00+00:00",
                "stopped_at": None,
                "deleted_at": None,
                "cost_per_hr": 2.0,
            },
            now=now,
        )
        self.assertEqual(pod["runtime_seconds"], 7200)
        self.assertEqual(pod["estimated_cost_usd"], 4.0)

    def test_stopped_pod_cost_uses_terminal_timestamp(self) -> None:
        now = dt.datetime(2026, 6, 6, 4, 0, tzinfo=dt.UTC)
        pod = enrich_pod_cost(
            {
                "provider_pod_id": "pod-real",
                "created_at": "2026-06-06T00:00:00+00:00",
                "stopped_at": "2026-06-06T00:30:00+00:00",
                "deleted_at": "2026-06-06T01:00:00+00:00",
                "cost_per_hr": 2.0,
            },
            now=now,
        )
        self.assertEqual(pod["runtime_seconds"], 1800)
        self.assertEqual(pod["estimated_cost_usd"], 1.0)

    def test_volume_cost_continues_until_deleted(self) -> None:
        now = dt.datetime(2026, 6, 6, 10, 0, tzinfo=dt.UTC)
        volume = enrich_volume_cost(
            {
                "provider_volume_id": "nv-real",
                "created_at": "2026-06-06T00:00:00+00:00",
                "deleted_at": None,
                "size_gb": 100,
            },
            now=now,
        )
        expected = network_volume_rate_usd_per_hr(100) * 10
        self.assertAlmostEqual(volume["estimated_cost_usd"], round(expected, 6))

    def test_legacy_deleted_volume_uses_updated_at_as_terminal_time(self) -> None:
        now = dt.datetime(2026, 6, 6, 10, 0, tzinfo=dt.UTC)
        volume = enrich_volume_cost(
            {
                "provider_volume_id": "nv-real",
                "state": "deleted",
                "created_at": "2026-06-06T00:00:00+00:00",
                "updated_at": "2026-06-06T01:00:00+00:00",
                "deleted_at": None,
                "size_gb": 100,
            },
            now=now,
        )
        expected = network_volume_rate_usd_per_hr(100)
        self.assertEqual(volume["runtime_seconds"], 3600)
        self.assertAlmostEqual(volume["estimated_cost_usd"], round(expected, 6))

    def test_session_cost_equals_pods_plus_volume(self) -> None:
        service = self.make_service()
        created = "2026-06-06T00:00:00+00:00"
        terminal = "2026-06-06T01:00:00+00:00"
        service.db.insert(
            "network_volumes",
            {
                "id": "vol_real",
                "provider_volume_id": "nv-real",
                "name": "real-volume",
                "data_center_id": "US-KS-2",
                "size_gb": 100,
                "state": "deleted",
                "hydration_state": "hydrated",
                "hydration_ttl_until": None,
                "retention_policy": "delete_after_collection",
                "estimated_cost_usd": 0,
                "last_payload_json": "{}",
                "created_at": created,
                "updated_at": terminal,
                "deleted_at": terminal,
            },
        )
        service.db.insert(
            "sessions",
            {
                "id": "ses_real",
                "request_id": None,
                "product": "comfyui",
                "mode": "interactive",
                "state": "reclaimed",
                "data_center_id": "US-KS-2",
                "max_gpu_usd_per_hr": 1.25,
                "max_total_usd": 5,
                "lease_until": None,
                "hard_terminate_at": None,
                "ui_url": None,
                "network_volume_id": "vol_real",
                "hydration_id": None,
                "cpu_pod_id": "pod_real",
                "gpu_pod_id": None,
                "estimated_cost_usd": 0,
                "retention_policy": "delete_after_collection",
                "created_at": created,
                "updated_at": terminal,
            },
        )
        service.db.insert(
            "pods",
            {
                "id": "pod_real",
                "provider_pod_id": "pod-real",
                "session_id": "ses_real",
                "volume_id": "vol_real",
                "role": "hydration_cpu",
                "compute_type": "CPU",
                "state": "deleted",
                "data_center_id": "US-KS-2",
                "image": "python:3.11-slim",
                "cpu_flavor_ids": "cpu3c",
                "gpu_type_id": None,
                "cost_per_hr": 2.0,
                "last_payload_json": "{}",
                "created_at": created,
                "updated_at": terminal,
                "stopped_at": terminal,
                "deleted_at": terminal,
            },
        )
        session = service.get_session("ses_real")
        expected_volume_cost = network_volume_rate_usd_per_hr(100)
        self.assertAlmostEqual(session["estimated_cost_usd"], round(2.0 + expected_volume_cost, 6))
        report = service.cost_report()
        self.assertAlmostEqual(report["summary"]["estimated_cost_usd"], round(2.0 + expected_volume_cost, 6))
        self.assertAlmostEqual(report["summary"]["effective_cost_usd"], round(2.0 + expected_volume_cost, 6))
        self.assertAlmostEqual(report["summary"]["estimated_compute_cost_usd"], 2.0)
        self.assertAlmostEqual(report["summary"]["estimated_storage_cost_usd"], round(expected_volume_cost, 6))

    def test_test_adapter_session_uses_non_fake_provider_ids(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        session = service.get_session(request["session_id"])
        self.assertTrue(session["pods"][0]["provider_pod_id"].startswith("test-"))
        self.assertTrue(session["volume"]["provider_volume_id"].startswith("test-"))
        self.assertNotEqual(session["pods"][0]["provider_mode"], "fake")
        self.assertNotEqual(session["volume"]["provider_mode"], "fake")

    def test_runpod_ssh_ready_wait_retries_connection_closed(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        adapter = RunpodRestAdapter(settings)
        calls: list[list[str]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if len(calls) == 1:
                return subprocess.CompletedProcess(command, 255, "", "Connection closed by host\n")
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch("controller.runpod.subprocess.run", side_effect=fake_run),
            patch("controller.runpod.time.sleep", return_value=None),
        ):
            result = adapter._wait_for_ssh_ready(
                provider_pod_id="pod-live",
                public_ip="127.0.0.1",
                ssh_port=2222,
                key_path="/tmp/test-key",
                timeout_seconds=20,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["ssh_ready_attempts"], 2)
        self.assertIn("BatchMode=yes", calls[0])

    def test_dashboard_links_to_comfyui_workflow_wizard(self) -> None:
        service = self.make_service()
        html = dashboard(service.cost_report()).decode("utf-8")
        self.assertIn("Create ComfyUI", html)
        self.assertNotIn(">Create session<", html)
        self.assertIn('href="/comfyui/new"', html)
        self.assertNotIn('data-open-dialog="new-session-dialog"', html)
        self.assertNotIn('id="new-session-dialog"', html)
        self.assertNotIn('id="new-session-form"', html)
        self.assertNotIn("Asset templates compatibility", html)
        wizard = comfyui_new_page().decode("utf-8")
        self.assertIn("New ComfyUI Session", wizard)
        self.assertIn("Workflow library", wizard)
        self.assertIn("Use selected workflow", wizard)
        self.assertIn("Upload workflow JSON or shared package (.zip)", wizard)
        self.assertIn('document.getElementById("workflow-select").value = ""', wizard)
        self.assertIn('document.getElementById("workflow-name").value = ""', wizard)
        self.assertIn('document.getElementById("workflow-file").value = ""', wizard)
        self.assertIn("Needs decision", wizard)
        self.assertIn("Resolve all with URLs", wizard)
        self.assertIn("Rows without URLs stay here for manual input.", wizard)
        self.assertIn("resolveAllNodesWithUrls", wizard)
        self.assertIn('id="nodes-resolve-all"', wizard)
        self.assertIn('id="nodes-progress"', wizard)
        self.assertIn('id="nodes-progress-bar"', wizard)
        self.assertIn('aria-describedby="nodes-progress-label"', wizard)
        self.assertIn('aria-busy", disabled ? "true" : "false"', wizard)
        self.assertIn("setNodeControlsDisabled", wizard)
        self.assertIn("updateNodeProgress", wizard)
        self.assertIn("button:disabled", wizard)
        self.assertIn("Resolving node locks", wizard)
        self.assertIn("Probe / Lock", wizard)
        self.assertIn('id="asset-url"', wizard)
        self.assertIn('id="asset-folder"', wizard)
        self.assertIn("Models", wizard)
        self.assertIn("Save all model rows", wizard)
        self.assertIn('id="assets-save-all"', wizard)
        self.assertIn("saveAllAssetRows", wizard)
        self.assertIn("Saving row and peeking metadata", wizard)
        self.assertIn("Saving all and peeking metadata", wizard)
        self.assertIn("download_url_redacted", wizard)
        self.assertIn("url: result.download_url_redacted || row.url", wizard)
        self.assertIn("Refresh metadata", wizard)
        self.assertNotIn(">Peek<", wizard)
        self.assertNotIn("data-peek-asset", wizard)
        self.assertIn("asset-url-row", wizard)
        self.assertIn("asset-url-input", wizard)
        self.assertIn("visibleRows", wizard)
        self.assertIn('row.asset.status !== "removed"', wizard)
        self.assertIn("No active rows.", wizard)
        self.assertIn("Model row removed.", wizard)
        self.assertIn("Location and GPU candidates are planned automatically during workflow startup.", wizard)
        self.assertIn('id="new-session-min-vram"', wizard)
        self.assertIn('name="min_vram_gb"', wizard)
        self.assertIn('value="24"', wizard)
        self.assertIn('id="new-session-gpu-vendor"', wizard)
        self.assertIn('name="gpu_vendor"', wizard)
        self.assertIn('<option value="NVIDIA" selected>NVIDIA</option>', wizard)
        self.assertIn('value="vae"', wizard)
        self.assertIn('value="diffusion_models"', wizard)
        self.assertIn('value="controlnet"', wizard)
        self.assertIn("/api/v1/assets/peek", wizard)
        self.assertIn('id="new-session-dryrun"', wizard)
        self.assertIn("Dry run", wizard)
        self.assertIn("/api/v1/resource-requests/dry-run", wizard)
        self.assertIn('id="dryrun-result"', wizard)
        self.assertIn("new-session-volume", wizard)
        self.assertIn('id="volume-estimate"', wizard)
        self.assertIn("asset(s) awaiting metadata", wizard)
        self.assertIn("max(10GB, ceil(total bytes × 1.20) + 5GB)", wizard)
        self.assertIn('class="help"', wizard)
        self.assertIn('id="wizard-banner"', wizard)
        self.assertIn("data-step-nav=", wizard)
        self.assertIn('min="10"', wizard)
        self.assertIn('value="10"', wizard)
        self.assertNotIn('placeholder="bytes"', wizard)
        self.assertIn("Already added. Remove the existing row first.", wizard)
        self.assertIn("Workflow ready. Review budget, dry run, then create.", wizard)
        self.assertIn(".node-resolution-grid", wizard)
        self.assertIn('class="node-resolution-grid"', wizard)
        self.assertIn('T("Git repo URL")', wizard)
        self.assertIn('T("tag / branch / commit optional")', wizard)
        self.assertNotIn("package name optional", wizard)
        self.assertNotIn("Launch templates", wizard)
        self.assertNotIn("Asset templates compatibility", wizard)
        self.assertNotIn('id="new-session-dc"', html)
        self.assertNotIn('id="new-session-gpu"', html)
        self.assertIn("/api/v1/resource-requests", wizard)
        self.assertNotIn("Recent History", html)
        self.assertNotIn("Active Volumes", html)
        self.assertNotIn("Active Pods</h2>", html)

    def test_tunnel_restart_allocates_next_port(self) -> None:
        service = self.make_service()
        request = service.create_resource_request({"product": "comfyui"})
        tunnel = service.restart_tunnel(request["session_id"])
        self.assertEqual(tunnel["local_port"], 18181)
        self.assertEqual(tunnel["state"], "proxy_ready")

    def test_pod_actual_cost_overrides_estimate_when_calibrated(self) -> None:
        pod = enrich_pod_cost(
            {
                "provider_pod_id": "pod-real",
                "created_at": "2026-06-06T00:00:00+00:00",
                "stopped_at": "2026-06-06T02:00:00+00:00",
                "deleted_at": None,
                "cost_per_hr": 10.0,
                "actual_cost_usd": 1.25,
                "billed_start_at": "2026-06-06T00:00:00+00:00",
                "billed_end_at": "2026-06-06T01:00:00+00:00",
                "billed_time_ms": 900000,
            }
        )
        self.assertEqual(pod["estimated_cost_usd"], 20.0)
        self.assertEqual(pod["actual_cost_usd"], 1.25)
        self.assertEqual(pod["effective_cost_usd"], 1.25)
        self.assertEqual(pod["cost_source"], "runpod_billing")
        self.assertEqual(pod["runtime_seconds"], 900)

    def test_billing_sync_calibrates_pod_cost_and_billed_times(self) -> None:
        service = self.make_service()
        created = "2026-06-06T00:00:00+00:00"
        terminal = "2026-06-06T02:00:00+00:00"
        self._insert_real_session(service, created=created, terminal=terminal)
        result = service.sync_billing(
            {
                "start_time": created,
                "end_time": terminal,
                "bucket_size": "hour",
                "pod_records": [
                    {
                        "podId": "pod-real",
                        "amount": 3.21,
                        "diskSpaceBilledGB": 200,
                        "time": "2026-06-06T00:00:00Z",
                        "timeBilledMs": 1800000,
                    }
                ],
                "network_volume_records": [],
            }
        )
        self.assertTrue(result["ok"])
        pod = service.list_pods()[0]
        self.assertEqual(pod["actual_cost_usd"], 3.21)
        self.assertEqual(pod["effective_cost_usd"], 3.21)
        self.assertEqual(pod["runtime_seconds"], 1800)
        self.assertEqual(pod["effective_start_at"], "2026-06-06T00:00:00+00:00")
        self.assertEqual(pod["effective_stop_at"], "2026-06-06T01:00:00+00:00")
        session = service.get_session("ses_real")
        self.assertEqual(session["actual_cost_usd"], 3.21)
        self.assertEqual(session["cost_source"], "runpod_billing")
        records = service.list_billing_records()
        self.assertEqual(records[0]["disk_space_billed_gb"], 200)

    def test_network_volume_account_billing_calibrates_storage_summary(self) -> None:
        service = self.make_service()
        service.create_resource_request({"product": "comfyui"})
        result = service.sync_billing(
            {
                "start_time": "2026-06-06T00:00:00+00:00",
                "end_time": "2026-06-06T01:00:00+00:00",
                "bucket_size": "hour",
                "pod_records": [],
                "network_volume_records": [
                    {
                        "amount": 0.42,
                        "diskSpaceBilledGB": 10,
                        "startDate": "2026-06-06T00:00:00Z",
                    }
                ],
            }
        )
        summary = result["summary"]
        self.assertEqual(summary["effective_storage_cost_usd"], 0.42)
        self.assertEqual(summary["storage_cost_source"], "runpod_billing_account")
        self.assertEqual(summary["runpod_network_volume_billing_records"], 1)
        records = service.list_billing_records()
        self.assertEqual(records[0]["resource_type"], "network_volume_account")
        self.assertEqual(records[0]["disk_space_billed_gb"], 10)

    def test_billing_calibration_candidates_include_terminal_live_resources(self) -> None:
        service = self.make_service()
        self._insert_real_session(
            service,
            created="2026-06-06T00:00:00+00:00",
            terminal="2026-06-06T01:00:00+00:00",
        )
        candidates = service.billing_calibration_candidates()
        self.assertEqual(candidates["pod_count"], 1)
        self.assertEqual(candidates["volume_count"], 1)
        self.assertEqual(candidates["pods"][0]["provider_pod_id"], "pod-real")
        self.assertEqual(candidates["volumes"][0]["provider_volume_id"], "nv-real")

    def test_billing_worker_exits_immediately_when_no_candidates(self) -> None:
        service = self.make_service()
        result = service.run_billing_calibration_worker(max_polls=1, poll_interval_seconds=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_reason"], "no_uncalibrated_resources")
        self.assertEqual(result["polls"], 0)
        self.assertEqual(result["attempts"], [])

    def test_billing_worker_watch_mode_waits_when_no_candidates(self) -> None:
        service = self.make_service()
        sleeps: list[float] = []
        result = service.run_billing_calibration_worker(
            max_polls=1,
            poll_interval_seconds=0,
            sleep_fn=lambda seconds: sleeps.append(seconds),
            watch=True,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_reason"], "max_polls_reached")
        self.assertEqual(result["polls"], 1)
        self.assertEqual(sleeps, [0])
        self.assertTrue(result["attempts"][0]["idle"])

    def test_billing_worker_polls_until_delayed_pod_billing_arrives(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        adapter = DelayedBillingAdapter(
            settings,
            pod_batches=[
                [],
                [
                    {
                        "podId": "pod-real",
                        "amount": 2.5,
                        "time": "2026-06-06T00:00:00Z",
                        "timeBilledMs": 3600000,
                    }
                ],
            ],
            volume_batches=[[], []],
        )
        service = ControllerService(settings, db, adapter)
        self._insert_real_session(
            service,
            created="2026-06-06T00:00:00+00:00",
            terminal="2026-06-06T01:00:00+00:00",
            provider_volume_id="fake-nv-test",
        )
        service.db.update(
            "pods",
            "pod_real",
            {
                "role": "comfyui_gpu",
                "compute_type": "GPU",
                "gpu_type_id": "NVIDIA GeForce RTX 4090",
            },
        )
        result = service.run_billing_calibration_worker(max_polls=3, poll_interval_seconds=0, sleep_fn=lambda _seconds: None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_reason"], "calibrated")
        self.assertEqual(result["polls"], 2)
        self.assertEqual(adapter.pod_calls, 2)
        pod = service.db.get("pods", "pod_real")
        self.assertEqual(pod["actual_cost_usd"], 2.5)

    def test_billing_worker_marks_volume_account_billing_as_calibrated_source(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = test_settings(Path(self.tmp.name))
        db = Database(settings)
        db.initialize()
        adapter = DelayedBillingAdapter(
            settings,
            pod_batches=[[]],
            volume_batches=[
                [
                    {
                        "amount": 0.5,
                        "diskSpaceBilledGb": 10,
                        "time": "2026-06-06T00:00:00Z",
                    }
                ]
            ],
        )
        service = ControllerService(settings, db, adapter)
        self._insert_real_session(
            service,
            created="2026-06-06T00:00:00+00:00",
            terminal="2026-06-06T01:00:00+00:00",
            provider_pod_id="fake-pod-test",
            provider_volume_id="nv-real",
        )
        result = service.run_billing_calibration_worker(max_polls=1, poll_interval_seconds=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_reason"], "calibrated")
        volume = service.db.get("network_volumes", "vol_real")
        self.assertIsNone(volume["actual_cost_usd"])
        self.assertEqual(volume["billing_source"], "runpod_billing_account")
        self.assertEqual(service.billing_calibration_candidates()["volume_count"], 0)
        self.assertEqual(service.cost_report()["summary"]["storage_cost_source"], "runpod_billing_account")

    def test_cpu_pod_empty_billing_before_grace_does_not_mark_absent(self) -> None:
        service = self.make_service()
        terminal = dt.datetime.now(dt.UTC)
        created = terminal - dt.timedelta(minutes=30)
        self._insert_real_session(
            service,
            created=created.isoformat(),
            terminal=terminal.isoformat(),
            provider_volume_id="fake-nv-test",
        )
        service.sync_billing(
            {
                "start_time": (created - dt.timedelta(hours=1)).isoformat(),
                "end_time": (terminal + dt.timedelta(hours=1)).isoformat(),
                "bucket_size": "hour",
                "pod_records": [],
                "network_volume_records": [],
            }
        )
        pod = service.db.get("pods", "pod_real")
        self.assertIsNone(pod["billing_source"])
        self.assertEqual(service.billing_calibration_candidates()["pod_count"], 1)

    def test_cpu_pod_empty_billing_marks_absent_after_grace(self) -> None:
        service = self.make_service()
        created = "2026-06-06T00:00:00+00:00"
        terminal = "2026-06-06T01:00:00+00:00"
        self._insert_real_session(service, created=created, terminal=terminal, provider_volume_id="fake-nv-test")
        payload = {
            "start_time": "2026-06-05T23:00:00+00:00",
            "end_time": "2026-06-06T02:00:00+00:00",
            "bucket_size": "hour",
            "pod_records": [],
            "network_volume_records": [],
        }
        service.sync_billing(payload)
        pod = service.db.get("pods", "pod_real")
        self.assertIsNone(pod["actual_cost_usd"])
        self.assertEqual(pod["billing_source"], "runpod_billing_absent")
        self.assertEqual(service.billing_calibration_candidates()["pod_count"], 0)
        self.assertEqual(service.list_pods()[0]["effective_cost_usd"], 10.0)

    def test_gpu_pod_empty_billing_is_not_marked_absent(self) -> None:
        service = self.make_service()
        created = "2026-06-06T00:00:00+00:00"
        terminal = "2026-06-06T01:00:00+00:00"
        self._insert_real_session(service, created=created, terminal=terminal, provider_volume_id="fake-nv-test")
        service.db.update(
            "pods",
            "pod_real",
            {
                "role": "comfyui_gpu",
                "compute_type": "GPU",
                "gpu_type_id": "NVIDIA GeForce RTX 4090",
            },
        )
        payload = {
            "start_time": "2026-06-05T23:00:00+00:00",
            "end_time": "2026-06-06T02:00:00+00:00",
            "bucket_size": "hour",
            "pod_records": [],
            "network_volume_records": [],
        }
        service.sync_billing(payload)
        pod = service.db.get("pods", "pod_real")
        self.assertIsNone(pod["billing_source"])
        self.assertEqual(service.billing_calibration_candidates()["pod_count"], 1)

    def test_late_cpu_billing_overrides_absent_marker(self) -> None:
        service = self.make_service()
        created = "2026-06-06T00:00:00+00:00"
        terminal = "2026-06-06T01:00:00+00:00"
        self._insert_real_session(service, created=created, terminal=terminal, provider_volume_id="fake-nv-test")
        empty_payload = {
            "start_time": "2026-06-05T23:00:00+00:00",
            "end_time": "2026-06-06T02:00:00+00:00",
            "bucket_size": "hour",
            "pod_records": [],
            "network_volume_records": [],
        }
        service.sync_billing(empty_payload)
        service.sync_billing(
            {
                "start_time": "2026-06-05T23:00:00+00:00",
                "end_time": "2026-06-06T02:00:00+00:00",
                "bucket_size": "hour",
                "pod_records": [
                    {
                        "podId": "pod-real",
                        "amount": 1.23,
                        "time": "2026-06-06T00:00:00Z",
                        "timeBilledMs": 3600000,
                    }
                ],
                "network_volume_records": [],
            }
        )
        pod = service.db.get("pods", "pod_real")
        self.assertEqual(pod["actual_cost_usd"], 1.23)
        self.assertEqual(pod["billing_source"], "runpod_billing")

    def test_billing_calibration_runs_inside_the_server(self) -> None:
        # The standalone billing-worker compose service is superseded by the
        # in-server calibration loop; a stray companion would double-poll.
        repo_root = Path(__file__).resolve().parent.parent
        compose_path = repo_root / "docker-compose.yml"
        if compose_path.exists():
            compose = compose_path.read_text(encoding="utf-8")
            self.assertNotIn("billing-worker:", compose)
        server_source = (repo_root / "controller" / "server.py").read_text(encoding="utf-8")
        self.assertIn("_start_billing_loop(service, settings)", server_source)
        self.assertIn("run_billing_calibration_worker", server_source)

    def _insert_real_session(
        self,
        service: ControllerService,
        *,
        created: str,
        terminal: str,
        provider_pod_id: str = "pod-real",
        provider_volume_id: str = "nv-real",
    ) -> None:
        service.db.insert(
            "network_volumes",
            {
                "id": "vol_real",
                "provider_volume_id": provider_volume_id,
                "name": "test-volume",
                "data_center_id": "US-KS-2",
                "size_gb": 10,
                "state": "deleted",
                "hydration_state": "hydrated",
                "hydration_ttl_until": None,
                "retention_policy": "delete_after_collection",
                "estimated_cost_usd": 0,
                "last_payload_json": "{}",
                "created_at": created,
                "updated_at": terminal,
                "deleted_at": terminal,
            },
        )
        service.db.insert(
            "sessions",
            {
                "id": "ses_real",
                "request_id": None,
                "product": "comfyui",
                "mode": "interactive",
                "state": "reclaimed",
                "data_center_id": "US-KS-2",
                "max_gpu_usd_per_hr": 1.25,
                "max_total_usd": 5,
                "lease_until": None,
                "hard_terminate_at": None,
                "ui_url": None,
                "network_volume_id": "vol_real",
                "hydration_id": None,
                "cpu_pod_id": "pod_real",
                "gpu_pod_id": None,
                "estimated_cost_usd": 0,
                "retention_policy": "delete_after_collection",
                "created_at": created,
                "updated_at": terminal,
            },
        )
        service.db.insert(
            "pods",
            {
                "id": "pod_real",
                "provider_pod_id": provider_pod_id,
                "session_id": "ses_real",
                "volume_id": "vol_real",
                "role": "hydration_cpu",
                "compute_type": "CPU",
                "state": "deleted",
                "data_center_id": "US-KS-2",
                "image": "python:3.11-slim",
                "cpu_flavor_ids": "cpu3c",
                "gpu_type_id": None,
                "cost_per_hr": 10.0,
                "last_payload_json": "{}",
                "created_at": created,
                "updated_at": terminal,
                "stopped_at": terminal,
                "deleted_at": terminal,
            },
        )


if __name__ == "__main__":
    unittest.main()
