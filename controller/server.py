from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings, load_settings
from .ssh_keys import ensure_private_key
from .db import Database
from .i18n import detect_locale, set_locale
from .runpod import build_adapter
from .service import ControllerService
from .web import (
    comfyui_new_page,
    dashboard,
    history_page,
    page,
    session_debug_page,
    session_detail,
    session_models_page,
    session_outputs_page,
    workflow_page,
)


class ControllerHandler(BaseHTTPRequestHandler):
    service: ControllerService

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        lang_override = (parse_qs(parsed.query).get("lang") or [""])[0]
        set_locale(detect_locale(self.headers.get("Accept-Language", ""), lang_override))
        try:
            if path == "/":
                self._send_html(dashboard(self.service.cost_report()))
                return
            if path == "/history":
                self._send_html(history_page(self.service.cost_report()))
                return
            if path == "/comfyui/new":
                self._send_html(comfyui_new_page())
                return
            if match := re.fullmatch(r"/sessions/([^/]+)", path):
                session = self.service.get_session(match.group(1))
                if not session:
                    self._send_json({"error": "not_found"}, status=404)
                    return
                self._send_html(session_detail(session))
                return
            if match := re.fullmatch(r"/sessions/([^/]+)/(workflow|models|outputs|debug)", path):
                session = self.service.get_session(match.group(1))
                if not session:
                    self._send_json({"error": "not_found"}, status=404)
                    return
                renderer = {
                    "workflow": workflow_page,
                    "models": session_models_page,
                    "outputs": session_outputs_page,
                    "debug": session_debug_page,
                }[match.group(2)]
                self._send_html(renderer(session))
                return
            if path == "/api/v1/capabilities":
                self._send_json(self.service.capabilities())
                return
            if path == "/api/v1/model-templates":
                query = parse_qs(parsed.query)
                product = (query.get("product") or [None])[0]
                self._send_json({"templates": self.service.list_model_templates(product)})
                return
            if path == "/api/v1/comfyui/templates":
                query = parse_qs(parsed.query)
                product = (query.get("product") or [None])[0]
                self._send_json({"templates": self.service.list_comfyui_launch_templates(product)})
                return
            if path == "/api/v1/comfyui/workflows":
                query = parse_qs(parsed.query)
                product = (query.get("product") or [None])[0]
                self._send_json({"workflows": self.service.list_comfyui_workflows(product)})
                return
            if match := re.fullmatch(r"/api/v1/comfyui/workflows/([^/]+)/export", path):
                try:
                    filename, body = self.service.export_comfyui_workflow_package(match.group(1))
                except KeyError:
                    self._send_json({"error": "not_found"}, status=404)
                    return
                self._send_download(body, filename=filename)
                return
            if match := re.fullmatch(r"/api/v1/comfyui/workflows/([^/]+)", path):
                row = self.service.get_comfyui_workflow(match.group(1))
                self._send_json(row or {"error": "not_found"}, status=200 if row else 404)
                return
            if match := re.fullmatch(r"/api/v1/comfyui/templates/([^/]+)", path):
                row = self.service.get_comfyui_launch_template(match.group(1))
                self._send_json(row or {"error": "not_found"}, status=200 if row else 404)
                return
            if match := re.fullmatch(r"/api/v1/comfyui/probes/([^/]+)", path):
                row = self.service.get_comfyui_probe(match.group(1))
                self._send_json(row or {"error": "not_found"}, status=200 if row else 404)
                return
            if path == "/api/v1/asset-metadata-cache":
                query = parse_qs(parsed.query)
                product = (query.get("product") or [None])[0]
                self._send_json({"cache": self.service.list_asset_metadata_cache(product)})
                return
            if path == "/api/v1/sessions":
                self._send_json({"sessions": self.service.list_sessions()})
                return
            if path == "/api/v1/resources/pods":
                self._send_json({"pods": self.service.list_pods()})
                return
            if path == "/api/v1/resources/volumes":
                self._send_json({"volumes": self.service.list_volumes()})
                return
            if path == "/api/v1/reports/costs":
                self._send_json(self.service.cost_report())
                return
            if path == "/api/v1/billing/records":
                query = parse_qs(parsed.query)
                limit = int((query.get("limit") or ["200"])[0])
                self._send_json({"billing_records": self.service.list_billing_records(limit=limit)})
                return
            if match := re.fullmatch(r"/api/v1/resource-requests/([^/]+)", path):
                row = self.service.get_resource_request(match.group(1))
                self._send_json(row or {"error": "not_found"}, status=200 if row else 404)
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)", path):
                row = self.service.get_session(match.group(1))
                self._send_json(row or {"error": "not_found"}, status=200 if row else 404)
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/outputs", path):
                self._send_json(self.service.list_session_outputs(match.group(1)))
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/models/tree", path):
                self._send_json(self.service.list_session_models_tree(match.group(1)))
                return
            if match := re.fullmatch(r"/api/v1/volumes/([^/]+)/hydration", path):
                row = self.service.get_volume_hydration(match.group(1))
                self._send_json(row or {"error": "not_found"}, status=200 if row else 404)
                return
            self._send_json({"error": "not_found"}, status=404)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": repr(exc)}, status=500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"invalid_request_body: {exc!r}"}, status=400)
            return
        try:
            if path == "/api/v1/assets/peek":
                self._send_json(self.service.peek_asset(payload))
                return
            if path == "/api/v1/model-templates":
                self._send_json(self.service.create_model_template(payload), status=201)
                return
            if path == "/api/v1/comfyui/templates":
                self._send_json(self.service.create_comfyui_launch_template(payload), status=201)
                return
            if path == "/api/v1/comfyui/workflows/analyze":
                self._send_json(self.service.analyze_comfyui_workflow(payload))
                return
            if path == "/api/v1/comfyui/workflows/upload":
                self._send_json(self.service.upload_comfyui_workflow(payload), status=201)
                return
            if path == "/api/v1/comfyui/workflows/import":
                try:
                    self._send_json(self.service.import_comfyui_workflow_package(payload), status=201)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                return
            if match := re.fullmatch(r"/api/v1/comfyui/workflows/([^/]+)/analyze", path):
                self._send_json(self.service.analyze_saved_comfyui_workflow(match.group(1)))
                return
            if match := re.fullmatch(r"/api/v1/comfyui/workflows/([^/]+)/nodes/resolve", path):
                self._send_json(self.service.resolve_comfyui_workflow_node(match.group(1), payload))
                return
            if match := re.fullmatch(r"/api/v1/comfyui/workflows/([^/]+)/probe", path):
                result = self.service.probe_comfyui_workflow(match.group(1), payload)
                self._send_json(result, status=200 if result.get("ok") else 409)
                return
            if match := re.fullmatch(r"/api/v1/comfyui/templates/([^/]+)/probe", path):
                result = self.service.probe_comfyui_launch_template(match.group(1), payload)
                self._send_json(result, status=200 if result.get("ok") else 409)
                return
            if path == "/api/v1/billing/sync":
                self._send_json(self.service.sync_billing(payload), status=202)
                return
            if path == "/api/v1/resource-requests/dry-run":
                self._send_json(self.service.dry_run_resource_request(payload))
                return
            if path == "/api/v1/resource-requests":
                self._send_json(self.service.create_resource_request(payload, process_inline=False), status=202)
                return
            if path == "/api/v1/volumes/hydration-requests":
                self._send_json(self.service.create_hydration_request(payload), status=202)
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/lease", path):
                self._send_json(self.service.extend_lease(match.group(1), payload))
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/tasks", path):
                self._send_json(self.service.create_task(match.group(1), payload), status=201)
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/finalize", path):
                self._send_json(self.service.finalize_session(match.group(1), payload))
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/outputs/collect", path):
                result = self.service.collect_session_outputs(match.group(1), mode=str(payload.get("mode") or "manual"))
                self._send_json(result, status=200 if result.get("ok") else 409)
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/outputs/open-local", path):
                result = self.service.open_session_outputs_locally(match.group(1))
                self._send_json(result, status=200 if result.get("ok") else 409)
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/models/downloads", path):
                result = self.service.start_session_model_download(match.group(1), payload)
                self._send_json(result, status=202 if result.get("ok") else 409)
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/models/move", path):
                result = self.service.move_session_model(match.group(1), payload)
                self._send_json(result, status=200 if result.get("ok") else 409)
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/volume/resize", path):
                result = self.service.resize_session_volume(match.group(1), payload)
                self._send_json(result, status=200 if result.get("ok") else 409)
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/reclaim", path):
                result = self.service.reclaim_session(match.group(1), payload)
                self._send_json(result, status=200 if result.get("ok") else 409)
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/promote-to-gpu", path):
                result = self.service.promote_session_to_gpu(match.group(1))
                self._send_json(result, status=200 if result.get("ok") else 409)
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/tunnel/restart", path):
                self._send_json(self.service.restart_tunnel(match.group(1)))
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/watchdog/tick", path):
                self._send_json(self.service.watchdog_tick(match.group(1), payload))
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/watchdog/pause", path):
                self._send_json(self.service.set_watchdog_paused(match.group(1), True))
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/watchdog/resume", path):
                self._send_json(self.service.set_watchdog_paused(match.group(1), False))
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/workflow/terminate", path):
                self._send_json(self.service.terminate_workflow(match.group(1)))
                return
            if match := re.fullmatch(r"/api/v1/sessions/([^/]+)/workflow/verify", path):
                result = self.service.mark_workflow_verified(match.group(1), payload)
                self._send_json(result, status=200 if result.get("ok") else 409)
                return
            self._send_json({"error": "not_found"}, status=404)
        except KeyError:
            self._send_json({"error": "not_found"}, status=404)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": repr(exc)}, status=500)

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"invalid_request_body: {exc!r}"}, status=400)
            return
        try:
            if match := re.fullmatch(r"/api/v1/model-templates/([^/]+)", path):
                self._send_json(self.service.update_model_template(match.group(1), payload))
                return
            if match := re.fullmatch(r"/api/v1/comfyui/templates/([^/]+)", path):
                self._send_json(self.service.update_comfyui_launch_template(match.group(1), payload))
                return
            if match := re.fullmatch(r"/api/v1/comfyui/workflows/([^/]+)", path):
                self._send_json(self.service.update_comfyui_workflow(match.group(1), payload))
                return
            self._send_json({"error": "not_found"}, status=404)
        except KeyError:
            self._send_json({"error": "not_found"}, status=404)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": repr(exc)}, status=500)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            if match := re.fullmatch(r"/api/v1/model-templates/([^/]+)", path):
                self._send_json(self.service.delete_model_template(match.group(1)))
                return
            if match := re.fullmatch(r"/api/v1/comfyui/templates/([^/]+)", path):
                self._send_json(self.service.delete_comfyui_launch_template(match.group(1)))
                return
            if match := re.fullmatch(r"/api/v1/comfyui/workflows/([^/]+)", path):
                result = self.service.delete_comfyui_workflow(match.group(1))
                self._send_json(result, status=200 if result.get("ok") else 409)
                return
            self._send_json({"error": "not_found"}, status=404)
        except KeyError:
            self._send_json({"error": "not_found"}, status=404)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": repr(exc)}, status=500)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, payload: Any, *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, body: bytes, *, filename: str, content_type: str = "application/zip") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_service(settings: Settings | None = None) -> ControllerService:
    settings = settings or load_settings()
    settings.ensure_dirs()
    db = Database(settings)
    db.initialize()
    return ControllerService(settings, db, build_adapter(settings))


def run() -> None:
    settings = load_settings()
    service = build_service(settings)
    _ensure_ssh_access(service, settings)
    _start_orphan_sweep(service)
    _start_watchdog_loop(service, settings)
    _start_output_collector_loop(service, settings)
    _start_billing_loop(service, settings)
    ControllerHandler.service = service
    server = ThreadingHTTPServer((settings.host, settings.port), ControllerHandler)
    print(f"RunPod controller listening on http://{settings.host}:{settings.port}", flush=True)
    server.serve_forever()


def _ensure_ssh_access(service: ControllerService, settings: Settings) -> None:
    """Make sure pod SSH works in a fresh environment: generate a keypair on
    first start and best-effort register the public key on the RunPod account."""
    try:
        info = ensure_private_key(settings)
    except Exception as exc:  # noqa: BLE001 - never block startup
        print(f"[ssh] key check failed: {exc!r}", flush=True)
        return
    if not info.get("ok"):
        print(f"[ssh] could not generate an SSH key ({info.get('error') or info.get('reason')}); "
              f"pod environment configuration will fail until a key exists at {info.get('key_path')}", flush=True)
        return
    if info.get("generated"):
        print(f"[ssh] generated controller SSH key at {info['key_path']}", flush=True)
    public_key = str(info.get("public_key") or "").strip()
    if not public_key:
        print(f"[ssh] no public key next to {info.get('key_path')}; pods may reject SSH", flush=True)
        return
    try:
        result = service.adapter.ensure_ssh_public_key_registered(public_key)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "reason": "register_error", "error": repr(exc)[:300]}
    if result.get("ok"):
        print(f"[ssh] public key {result.get('state')} on the RunPod account (key: {info['key_path']})", flush=True)
    else:
        print("[ssh] could not auto-register the public key on the RunPod account "
              f"({result.get('reason')}: {result.get('error', '')}). Pods created by this controller still "
              "receive it via the PUBLIC_KEY env var; to also SSH in manually, paste this line into "
              "RunPod Settings -> SSH Public Keys:\n" + public_key, flush=True)


def _start_orphan_sweep(service: ControllerService) -> None:
    def sweep() -> None:
        try:
            service.reconcile_orphan_resources()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=sweep, daemon=True, name="controller-orphan-sweep").start()


def _start_watchdog_loop(service: ControllerService, settings: Settings) -> None:
    def loop() -> None:
        while True:
            try:
                sessions = service.list_sessions()
            except Exception:  # noqa: BLE001
                sessions = []
            for session in sessions:
                if str(session.get("state")) in {"reclaimed", "finalized", "failed", "cleanup_failed", "output_collection_failed_keep_volume", "output_collection_empty_keep_volume", "collecting_outputs"}:
                    continue
                # One failing session must not starve lease/hard-cap enforcement
                # for every other paid session.
                try:
                    service.watchdog_tick(session["id"], {"queue_active": False, "output_active": False, "source": "controller_watchdog"})
                except Exception as exc:  # noqa: BLE001
                    try:
                        service.audit("session", session["id"], "watchdog_tick_error", "Watchdog tick failed", {"error": repr(exc)})
                    except Exception:  # noqa: BLE001
                        pass
            time.sleep(max(1, int(settings.watchdog_poll_interval_seconds)))

    threading.Thread(target=loop, daemon=True, name="controller-watchdog").start()


def _start_billing_loop(service: ControllerService, settings: Settings) -> None:
    """Run billing calibration inside the server.

    The standalone `python -m controller.billing_worker` was never part of the
    local screen-based deployment, so every session stayed on estimate-based
    cost forever. The worker loop already sleeps between polls and keeps
    watching when no candidates exist."""

    def loop() -> None:
        while True:
            try:
                service.run_billing_calibration_worker(
                    poll_interval_seconds=settings.billing_worker_poll_interval_seconds,
                    bucket_size=settings.billing_worker_bucket_size,
                    max_polls=None,
                    watch=True,
                )
            except Exception:  # noqa: BLE001 - keep calibrating across transient failures
                time.sleep(max(60, int(settings.billing_worker_poll_interval_seconds)))

    threading.Thread(target=loop, daemon=True, name="controller-billing").start()


def _start_output_collector_loop(service: ControllerService, settings: Settings) -> None:
    def loop() -> None:
        while True:
            try:
                for session in service.output_collection_candidates():
                    try:
                        service.collect_session_outputs(session["id"], mode="periodic")
                    except Exception as exc:  # noqa: BLE001
                        service.audit("session", session["id"], "output_collector_error", "Background output collector failed", {"error": repr(exc)})
            except Exception:  # noqa: BLE001
                pass
            time.sleep(max(1, int(settings.output_collector_interval_seconds)))

    threading.Thread(target=loop, daemon=True, name="controller-output-collector").start()


if __name__ == "__main__":
    run()
