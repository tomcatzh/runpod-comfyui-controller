from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any

from .assets import normalize_asset_manifest
from .asset_metadata import target_for
from .utils import json_dumps, sha256_text


CORE_NODE_TYPES = {
    "BasicGuider",
    "BasicScheduler",
    "CheckpointLoaderSimple",
    "CLIPLoader",
    "CLIPTextEncode",
    "ConditioningZeroOut",
    "DualCLIPLoader",
    "EmptyLatentImage",
    "EmptySD3LatentImage",
    "KSampler",
    "LoadImage",
    "LoraLoader",
    "ModelSamplingSD3",
    "RandomNoise",
    "SamplerCustomAdvanced",
    "SaveImage",
    "TripleCLIPLoader",
    "UNETLoader",
    "VAEDecode",
    "VAEEncode",
    "VAELoader",
}


MODEL_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")
MODEL_FOLDER_BY_KEY = [
    ("seedvr2", "SEEDVR2"),
    ("lora", "loras"),
    ("vae", "vae"),
    ("controlnet", "controlnet"),
    ("control_net", "controlnet"),
    ("upscale", "upscale_models"),
    ("clip_vision", "clip_vision"),
    ("clip", "text_encoders"),
    ("text_encoder", "text_encoders"),
    ("unet", "diffusion_models"),
    ("diffusion", "diffusion_models"),
    ("checkpoint", "checkpoints"),
    ("ckpt", "checkpoints"),
]


def normalize_workflow_json(raw: Any) -> Any:
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        import json

        return json.loads(raw)
    if isinstance(raw, (dict, list)):
        return copy.deepcopy(raw)
    raise ValueError("workflow json must be an object, array, JSON string, or empty")


def workflow_hash(raw: Any) -> str:
    workflow = normalize_workflow_json(raw)
    if workflow is None:
        raise ValueError("workflow json is required")
    return sha256_text(json_dumps(workflow))


def normalize_custom_nodes(raw: Any) -> list[dict[str, Any]]:
    if raw in (None, ""):
        return []
    if isinstance(raw, dict):
        items = raw.get("items") or raw.get("custom_nodes") or []
        if isinstance(items, dict):
            items = [
                {"package": package, **(value if isinstance(value, dict) else {"repo_url": value})}
                for package, value in items.items()
            ]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("custom_nodes must be a list or object")
    normalized = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"custom node {index} must be an object")
        package = str(item.get("package") or item.get("name") or "").strip()
        if not package:
            raise ValueError(f"custom node {index} missing package")
        node_types = item.get("node_types") or item.get("class_types") or []
        if isinstance(node_types, str):
            node_types = [part.strip() for part in node_types.split(",") if part.strip()]
        normalized.append(
            {
                "package": package,
                "repo_url": str(item.get("repo_url") or item.get("url") or "").strip(),
                "ref": str(item.get("locked_ref") or item.get("ref") or item.get("commit") or item.get("tag") or "").strip(),
                "requested_ref": str(item.get("requested_ref") or item.get("tag") or "").strip(),
                "locked_ref": str(item.get("locked_ref") or item.get("commit") or item.get("ref") or "").strip(),
                "install_method": str(item.get("install_method") or "git_clone").strip(),
                "node_types": sorted({str(node_type).strip() for node_type in node_types if str(node_type).strip()}),
                "source": str(item.get("source") or "explicit_template_mapping"),
                "locked_at": str(item.get("locked_at") or "").strip(),
                "registry_node_id": str(item.get("registry_node_id") or "").strip(),
                "registry_version": str(item.get("registry_version") or "").strip(),
                "registry_status": str(item.get("registry_status") or "").strip(),
                "registry_version_status": str(item.get("registry_version_status") or "").strip(),
                "registry_download_url": str(item.get("registry_download_url") or "").strip(),
            }
        )
    return normalized


RegistryResolver = Callable[[str], dict[str, Any] | None]


def _node_type_set(raw: Any) -> set[str]:
    if raw in (None, ""):
        return set()
    if isinstance(raw, str):
        return {item.strip() for item in raw.split(",") if item.strip()}
    if isinstance(raw, list):
        return {str(item).strip() for item in raw if str(item).strip()}
    return set()


def analyze_comfyui_workflow(payload: dict[str, Any], *, registry_resolver: RegistryResolver | None = None) -> dict[str, Any]:
    ui_workflow = normalize_workflow_json(payload.get("ui_workflow_json") or payload.get("workflow_json"))
    api_workflow = normalize_workflow_json(payload.get("api_workflow_json"))
    explicit_custom_nodes = normalize_custom_nodes(payload.get("custom_nodes"))
    builtin_overrides = _node_type_set(payload.get("builtin_overrides") or payload.get("assumed_builtin_nodes"))
    ignored_node_types = _node_type_set(payload.get("ignored_node_types"))
    nodes, warnings = extract_workflow_nodes(ui_workflow, api_workflow)
    used_types = sorted({node["class_type"] for node in nodes if node.get("class_type")})
    source_by_type: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        source_by_type.setdefault(node["class_type"], []).append(
            {"source": node.get("source"), "id": node.get("id"), "title": node.get("title")}
        )
    resolved_by_package: dict[str, dict[str, Any]] = {}
    unresolved = []
    suggestions = []
    core_nodes = []
    assumed_builtin_nodes = []
    ignored_nodes = []
    node_resolutions = []

    for node_type in used_types:
        try:
            resolution = resolve_node_type(
                node_type,
                explicit_custom_nodes,
                registry_resolver=registry_resolver,
                builtin_overrides=builtin_overrides,
                ignored_node_types=ignored_node_types,
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"registry_lookup_failed:{node_type}:{type(exc).__name__}")
            resolution = resolve_node_type(
                node_type,
                explicit_custom_nodes,
                registry_resolver=None,
                builtin_overrides=builtin_overrides,
                ignored_node_types=ignored_node_types,
            )
        node_resolutions.append({"class_type": node_type, "sources": source_by_type.get(node_type, []), **resolution})
        if resolution["state"] == "resolved":
            package = resolution["package"]
            existing = resolved_by_package.setdefault(
                package,
                {
                    "package": package,
                    "display_name": resolution.get("display_name") or "",
                    "repo_url": resolution.get("repo_url") or "",
                    "ref": resolution.get("ref") or "",
                    "install_method": resolution.get("install_method") or "git_clone",
                    "node_types": [],
                    "source": resolution.get("source") or "unknown",
                    "registry_node_id": resolution.get("registry_node_id") or "",
                    "registry_version": resolution.get("registry_version") or "",
                    "registry_status": resolution.get("registry_status") or "",
                    "registry_version_status": resolution.get("registry_version_status") or "",
                    "registry_download_url": resolution.get("registry_download_url") or "",
                    "requested_ref": resolution.get("requested_ref") or "",
                    "locked_ref": resolution.get("locked_ref") or resolution.get("ref") or "",
                    "locked_at": resolution.get("locked_at") or "",
                },
            )
            existing["node_types"] = sorted({*existing["node_types"], node_type})
        elif resolution["state"] == "core":
            core_nodes.append(node_type)
        elif resolution["state"] == "assumed_builtin":
            assumed_builtin_nodes.append(node_type)
        elif resolution["state"] == "ignored":
            ignored_nodes.append(node_type)
        elif resolution["state"] == "suggested":
            suggestion = {key: value for key, value in resolution.items() if key not in {"state"}}
            suggestions.append({"class_type": node_type, "sources": source_by_type.get(node_type, []), "suggestion": suggestion})
            unresolved.append(
                {
                    "class_type": node_type,
                    "reason": "custom_node_mapping_needs_acceptance",
                    "sources": source_by_type.get(node_type, []),
                    "suggestion": suggestion,
                }
            )
        elif resolution["state"] == "unresolved":
            unresolved.append(
                {
                    "class_type": node_type,
                    "reason": resolution.get("reason") or "unresolved_custom_node",
                    "sources": source_by_type.get(node_type, []),
                }
            )

    custom_nodes = sorted(resolved_by_package.values(), key=lambda item: item["package"])
    install_plan = build_install_plan(custom_nodes)
    validation_plan = {
        "checks": ["system_stats", "model_visibility", "custom_nodes_visible", "missing_nodes_absent"],
        "api_smoke": bool(api_workflow),
        "custom_node_types": sorted(
            {
                node_type
                for node in custom_nodes
                for node_type in node.get("node_types", [])
            }
        ),
    }
    return {
        "ok": not unresolved,
        "node_count": len(nodes),
        "class_types": used_types,
        "core_node_count": len(core_nodes),
        "core_nodes": core_nodes,
        "assumed_builtin_count": len(assumed_builtin_nodes),
        "assumed_builtin_nodes": assumed_builtin_nodes,
        "ignored_nodes": ignored_nodes,
        "warnings": warnings,
        "unresolved_custom_nodes": unresolved,
        "resolved_custom_nodes": custom_nodes,
        "suggested_custom_nodes": suggestions,
        "node_resolutions": node_resolutions,
        "install_plan": install_plan,
        "validation_plan": validation_plan,
        "ui_workflow_present": ui_workflow is not None,
        "api_workflow_present": api_workflow is not None,
    }


def extract_workflow_nodes(ui_workflow: Any, api_workflow: Any) -> tuple[list[dict[str, Any]], list[str]]:
    nodes: list[dict[str, Any]] = []
    warnings: list[str] = []

    def add_node(source: str, node_id: Any, raw_node: Any) -> None:
        if not isinstance(raw_node, dict):
            warnings.append(f"{source}:node_not_object:{node_id}")
            return
        class_type = raw_node.get("class_type") or raw_node.get("type")
        if not class_type:
            title = str(raw_node.get("title") or raw_node.get("name") or "").strip().lower()
            if title in {"note", "group", "reroute"} or raw_node.get("mode") == "group":
                warnings.append(f"{source}:ignored_non_executable_node:{node_id}")
                return
            warnings.append(f"{source}:missing_class_type:{node_id}")
            return
        nodes.append(
            {
                "source": source,
                "id": str(raw_node.get("id") or node_id or ""),
                "class_type": str(class_type).strip(),
                "title": str(raw_node.get("title") or raw_node.get("name") or ""),
            }
        )

    if isinstance(ui_workflow, dict):
        if isinstance(ui_workflow.get("nodes"), list):
            for index, node in enumerate(ui_workflow.get("nodes") or []):
                add_node("ui", index, node)
        elif ui_workflow:
            for node_id, node in ui_workflow.items():
                add_node("ui", node_id, node)
    elif isinstance(ui_workflow, list):
        for index, node in enumerate(ui_workflow):
            add_node("ui", index, node)

    if isinstance(api_workflow, dict):
        for node_id, node in api_workflow.items():
            add_node("api", node_id, node)
    elif isinstance(api_workflow, list):
        for index, node in enumerate(api_workflow):
            add_node("api", index, node)

    return nodes, warnings


def resolve_node_type(
    node_type: str,
    explicit_custom_nodes: list[dict[str, Any]],
    *,
    registry_resolver: RegistryResolver | None = None,
    builtin_overrides: set[str] | None = None,
    ignored_node_types: set[str] | None = None,
) -> dict[str, Any]:
    if node_type in (ignored_node_types or set()):
        return {"state": "ignored", "reason": "user_ignored_non_executable"}
    if node_type in (builtin_overrides or set()):
        return {"state": "assumed_builtin", "reason": "user_treat_as_builtin"}
    if is_core_node_type(node_type):
        return {"state": "core", "reason": "comfyui_core_or_builtin"}
    lowered = node_type.lower()
    for custom_node in explicit_custom_nodes:
        explicit_types = {item.lower() for item in custom_node.get("node_types", [])}
        if lowered in explicit_types or (custom_node["package"].lower() in lowered and custom_node.get("repo_url")):
            return {"state": "resolved", **custom_node}
    if registry_resolver:
        registry_node = registry_resolver(node_type)
        if registry_node:
            return {"state": "suggested", **registry_node}
    if looks_like_custom_node(node_type):
        return {"state": "unresolved", "reason": "custom_node_mapping_missing"}
    return {"state": "assumed_builtin", "reason": "unknown_without_custom_marker"}


def is_core_node_type(node_type: str) -> bool:
    if node_type in CORE_NODE_TYPES:
        return True
    return node_type.startswith(("Primitive", "Reroute", "Note", "Preview", "Load"))


def looks_like_custom_node(node_type: str) -> bool:
    lowered = node_type.lower()
    return (
        "(" in node_type
        and ")" in node_type
        or "custom" in lowered
        or "|" in node_type
        or lowered.startswith(("easy ", "impact", "was "))
    )


def build_install_plan(custom_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    steps = []
    for node in custom_nodes:
        steps.append(
            {
                "action": node.get("install_method") or "git_clone",
                "package": node["package"],
                "display_name": node.get("display_name") or "",
                "repo_url": node.get("repo_url") or "",
                "ref": node.get("locked_ref") or node.get("ref") or "",
                "requested_ref": node.get("requested_ref") or "",
                "locked_ref": node.get("locked_ref") or node.get("ref") or "",
                "locked_at": node.get("locked_at") or "",
                "target": f"custom_nodes/{node['package']}",
                "run_requirements": True,
                "node_types": node.get("node_types") or [],
                "source": node.get("source") or "unknown",
                "registry_node_id": node.get("registry_node_id") or "",
                "registry_version": node.get("registry_version") or "",
                "registry_status": node.get("registry_status") or "",
                "registry_version_status": node.get("registry_version_status") or "",
                "registry_download_url": node.get("registry_download_url") or "",
            }
        )
    return {"version": 1, "steps": steps}


def extract_model_requirements(ui_workflow_json: Any, api_workflow_json: Any = None) -> list[dict[str, Any]]:
    ui_workflow = normalize_workflow_json(ui_workflow_json)
    api_workflow = normalize_workflow_json(api_workflow_json)
    raw_nodes = _raw_workflow_nodes(ui_workflow, "ui") + _raw_workflow_nodes(api_workflow, "api")
    rows: dict[str, dict[str, Any]] = {}
    for source, node_id, node in raw_nodes:
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or node.get("type") or "")
        values = _model_like_values_for_node(node, source)
        for key_path, value in values:
            folder = guess_model_folder(class_type, key_path, value)
            filename = _filename_from_model_value(value)
            if not filename:
                continue
            row_key = f"{folder}:{filename}"
            rows.setdefault(
                row_key,
                {
                    "id": f"req_{sha256_text(row_key)[:16]}",
                    "kind": "workflow_model_requirement",
                    "filename": filename,
                    "model_folder": folder,
                    "target": target_for(folder, filename),
                    "url": value if _looks_like_url(value) else "",
                    "size_bytes": None,
                    "size_unknown": True,
                    "provider": "generic",
                    "status": "needs_metadata" if _looks_like_url(value) else "needs_url",
                    "source_node_id": str(node.get("id") or node_id or ""),
                    "source_node_type": class_type,
                    "source_field": key_path,
                    "source": source,
                },
            )
    return sorted(rows.values(), key=lambda item: (item["model_folder"], item["filename"]))


def guess_model_folder(class_type: str, key_path: str, value: str) -> str:
    haystack = f"{class_type} {key_path} {value}".lower()
    for marker, folder in MODEL_FOLDER_BY_KEY:
        if marker in haystack:
            return folder
    return "checkpoints"


def dependency_fingerprint(*, product: str, workflow_hash_value: str, node_locks: list[dict[str, Any]], base_template: dict[str, Any]) -> str:
    return sha256_text(json_dumps({"product": product, "workflow_hash": workflow_hash_value, "node_locks": node_locks, "base_template": base_template}))


def workflow_launch_fingerprint(
    *,
    dependency_fingerprint_value: str,
    assets: list[dict[str, Any]],
    launch_settings: dict[str, Any],
) -> str:
    return sha256_text(
        json_dumps(
            {
                "dependency_fingerprint": dependency_fingerprint_value,
                "assets": normalize_asset_manifest(assets),
                "launch_settings": launch_settings,
            }
        )
    )


def launch_template_fingerprint(
    *,
    product: str,
    ui_workflow_json: Any,
    api_workflow_json: Any,
    assets: list[dict[str, Any]],
    custom_nodes: list[dict[str, Any]],
    install_plan: dict[str, Any],
    base_template: dict[str, Any],
) -> str:
    payload = {
        "product": product,
        "ui_workflow_json": ui_workflow_json,
        "api_workflow_json": api_workflow_json,
        "assets": normalize_asset_manifest(assets),
        "custom_nodes": normalize_custom_nodes(custom_nodes),
        "install_plan": install_plan,
        "base_template": base_template,
    }
    return sha256_text(json_dumps(payload))


def _raw_workflow_nodes(workflow: Any, source: str) -> list[tuple[str, Any, Any]]:
    if workflow is None:
        return []
    if isinstance(workflow, dict):
        if isinstance(workflow.get("nodes"), list):
            return [(source, index, node) for index, node in enumerate(workflow.get("nodes") or [])]
        return [(source, node_id, node) for node_id, node in workflow.items()]
    if isinstance(workflow, list):
        return [(source, index, node) for index, node in enumerate(workflow)]
    return []


def _model_like_values(raw: Any, prefix: str = "") -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            key_path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, str) and _looks_like_model_value(key_path, value):
                values.append((key_path, value.strip()))
            elif isinstance(value, (dict, list)):
                values.extend(_model_like_values(value, key_path))
    elif isinstance(raw, list):
        for index, value in enumerate(raw):
            key_path = f"{prefix}[{index}]"
            if isinstance(value, str) and _looks_like_model_value(key_path, value):
                values.append((key_path, value.strip()))
            elif isinstance(value, (dict, list)):
                values.extend(_model_like_values(value, key_path))
    return values


def _model_like_values_for_node(node: dict[str, Any], source: str) -> list[tuple[str, str]]:
    if source == "ui":
        values: list[tuple[str, str]] = []
        if "widgets_values" in node:
            values.extend(_model_like_values(node.get("widgets_values"), "widgets_values"))
        inputs = node.get("inputs")
        if isinstance(inputs, dict):
            values.extend(_model_like_values(inputs, "inputs"))
        return values
    return _model_like_values(node)


def _looks_like_model_value(key_path: str, value: str) -> bool:
    text = value.strip()
    lowered = text.lower()
    if _looks_like_url(text):
        return any(ext in lowered for ext in MODEL_EXTENSIONS)
    if any(lowered.endswith(ext) for ext in MODEL_EXTENSIONS):
        return True
    key = key_path.lower()
    return any(marker in key for marker, _folder in MODEL_FOLDER_BY_KEY) and bool(re.search(r"\.(safetensors|ckpt|pt|pth|bin|gguf|onnx)(\?|$)", lowered))


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _filename_from_model_value(value: str) -> str:
    text = value.strip()
    if _looks_like_url(text):
        from urllib.parse import unquote, urlparse

        path = urlparse(text).path
        return unquote(path.rsplit("/", 1)[-1])
    return text.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
