from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, unquote, urlparse, urlunparse


SECRET_MARKERS = ("token", "key", "secret", "authorization", "api_key", "signature", "credential", "policy")
SIGNED_URL_QUERY_KEYS = {
    "expires",
    "key-pair-id",
    "policy",
    "signature",
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-expires",
    "x-amz-security-token",
    "x-amz-signature",
    "x-amz-signedheaders",
    "x-xet-cas-uid",
}
COMFYUI_MODEL_FOLDERS = {
    "checkpoints",
    "loras",
    "vae",
    "controlnet",
    "diffusion_models",
    "text_encoders",
    "unet",
    "clip",
    "clip_vision",
    "embeddings",
    "upscale_models",
    "ultralytics",
    "SEEDVR2",
    "configs",
    "gligen",
    "hypernetworks",
}


def normalize_asset_manifest(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = raw.get("items") or raw.get("assets") or []
        if isinstance(items, dict):
            items = [
                {"name": name, **(value if isinstance(value, dict) else {"value": value})}
                for name, value in items.items()
            ]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("assets must be a list or object")
    normalized = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"asset {index} must be an object")
        raw_url = str(item.get("url") or "")
        url = canonical_asset_url(raw_url)
        provider = str(item.get("provider") or detect_provider({"url": url})).lower()
        model_folder = normalize_model_folder(item.get("model_folder") or item.get("folder") or item.get("model_type"))
        filename = str(item.get("filename") or "").strip()
        target = str(item.get("target") or item.get("path") or default_target(provider, url, index, model_folder=model_folder, filename=filename))
        if not filename:
            filename = target.rsplit("/", 1)[-1]
        normalized.append(
            {
                "id": str(item.get("id") or asset_id(provider, url, target, index)),
                "provider": provider,
                "kind": str(item.get("kind") or "model"),
                "model_folder": model_folder,
                "filename": filename,
                "name": str(item.get("name") or target.rsplit("/", 1)[-1]),
                "url": redact_url(url),
                "target": target,
                "size_bytes": int(item["size_bytes"]) if item.get("size_bytes") is not None else None,
                "size_unknown": bool(item.get("size_unknown")) if item.get("size_bytes") is None else False,
                "sha256": item.get("sha256"),
                "requires_secret": bool(item.get("requires_secret") or provider in {"civitai", "huggingface"}),
            }
        )
    return normalized


def detect_provider(item: dict[str, Any]) -> str:
    url = str(item.get("url") or "")
    host = urlparse(url).netloc.lower()
    if host in {"civitai.com", "civitai.red", "civitai.green"} or host.endswith((".civitai.com", ".civitai.red", ".civitai.green")):
        return "civitai"
    if host in {"huggingface.co", "hf-mirror.com"} or host.endswith((".huggingface.co", ".hf-mirror.com")):
        return "huggingface"
    return "generic"


def canonical_asset_url(url: str) -> str:
    text = str(url or "").strip()
    provider = detect_provider({"url": text})
    if provider == "huggingface":
        return _canonical_huggingface_download_url(text)
    return text


def _canonical_huggingface_download_url(url: str) -> str:
    parsed = urlparse(url)
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    if "blob" in segments:
        blob_index = segments.index("blob")
        if blob_index >= 2 and len(segments) > blob_index + 2:
            segments[blob_index] = "resolve"
            parsed = parsed._replace(path="/" + "/".join(quote(segment, safe="") for segment in segments))
    elif "resolve" not in segments:
        return url
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key.lower() != "download"]
    query.append(("download", "true"))
    return urlunparse(parsed._replace(query=urlencode(query)))


def normalize_model_folder(value: Any) -> str:
    folder = str(value or "").strip().strip("/")
    if folder == "diffusion model":
        folder = "diffusion_models"
    if folder == "upscaler":
        folder = "upscale_models"
    if folder in COMFYUI_MODEL_FOLDERS:
        return folder
    # Folder names become path segments under assets/comfyui/: never allow
    # separators or dot-only segments through.
    if not folder or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]*", folder) or set(folder) <= {"."}:
        return "checkpoints"
    return folder


def safe_asset_filename(value: Any) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name in {"", ".", ".."}:
        return "asset"
    return name


def default_target(provider: str, url: str, index: int, *, model_folder: str = "checkpoints", filename: Any = None) -> str:
    parsed = urlparse(url)
    filename_text = str(filename or "").strip()
    filename_text = filename_text or parsed.path.rsplit("/", 1)[-1] or f"asset-{index + 1}"
    if model_folder:
        return f"assets/comfyui/{model_folder}/{filename_text}"
    if provider == "civitai":
        return f"assets/civitai/{filename_text}"
    if provider == "huggingface":
        return f"assets/huggingface/{filename_text}"
    return f"assets/generic/{filename_text}"


def asset_id(provider: str, url: str, target: str, index: int) -> str:
    digest = hashlib.sha256(f"{provider}:{url}:{target}:{index}".encode("utf-8")).hexdigest()
    return f"asset_{digest[:16]}"


def redact_url(url: str) -> str:
    if not url:
        return url
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    changed = False
    redacted_query = []
    for key, values in query.items():
        if is_secret_query_key(key):
            values = ["<redacted>" for _ in values]
            changed = True
        for value in values:
            redacted_query.append((key, value))
    netloc = parsed.netloc
    if parsed.username or parsed.password:
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"<redacted>@{hostname}{port}"
        changed = True
    if not changed:
        return url
    query_text = "&".join(f"{key}={value}" for key, value in redacted_query)
    return urlunparse(parsed._replace(netloc=netloc, query=query_text))


def is_secret_query_key(key: str) -> bool:
    lower = key.lower()
    return lower in SIGNED_URL_QUERY_KEYS or any(marker in lower for marker in SECRET_MARKERS)
