from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


COMFY_REGISTRY_BASE_URL = "https://api.comfy.org"


class ComfyRegistryClient:
    def __init__(self, *, base_url: str = COMFY_REGISTRY_BASE_URL, timeout_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def resolve_comfy_node_name(self, comfy_node_name: str) -> dict[str, Any] | None:
        name = str(comfy_node_name or "").strip()
        if not name:
            return None
        quoted = urllib.parse.quote(name, safe="")
        url = f"{self.base_url}/comfy-nodes/{quoted}/node"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise RuntimeError(f"comfy_registry_http_error:{exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"comfy_registry_url_error:{exc.reason}") from exc
        return registry_node_to_custom_node(name, payload)


def registry_node_to_custom_node(comfy_node_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    latest = payload.get("latest_version") if isinstance(payload.get("latest_version"), dict) else {}
    repository = str(payload.get("repository") or payload.get("source_code_repo") or "").strip()
    return {
        "package": str(payload.get("id") or payload.get("name") or "").strip(),
        "display_name": str(payload.get("name") or "").strip(),
        "repo_url": repository,
        "ref": "",
        "install_method": "git_clone" if repository else "comfy_registry",
        "node_types": [comfy_node_name],
        "source": "comfy_registry",
        "registry_node_id": str(payload.get("id") or "").strip(),
        "registry_version": str(latest.get("version") or "").strip(),
        "registry_status": str(payload.get("status") or "").strip(),
        "registry_version_status": str(latest.get("status") or "").strip(),
        "registry_download_url": str(latest.get("downloadUrl") or "").strip(),
    }
