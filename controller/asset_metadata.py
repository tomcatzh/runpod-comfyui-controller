from __future__ import annotations

import email.message
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .assets import canonical_asset_url, detect_provider, is_secret_query_key, normalize_model_folder, redact_url, safe_asset_filename


REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 8
MIN_NETWORK_VOLUME_GB = 10
DEFAULT_VOLUME_OUTPUT_RESERVE_GB = 5
DEFAULT_VOLUME_SCRATCH_RESERVE_RATIO = 0.20


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def normalized_url_key(url: str) -> str:
    parsed = urllib.parse.urlparse(canonical_asset_url(url))
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe_query = [
        (key, value)
        for key, value in query
        if not is_secret_query_key(key)
    ]
    normalized_query = urllib.parse.urlencode(sorted(safe_query))
    return urllib.parse.urlunparse(
        parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            query=normalized_query,
            fragment="",
        )
    )


def volume_size_gb_for_assets(
    assets: list[dict[str, Any]],
    *,
    reserve_gb: int | None = None,
    minimum_gb: int = MIN_NETWORK_VOLUME_GB,
) -> int:
    total = 0
    for asset in assets:
        size = asset.get("size_bytes")
        if size is None:
            raise ValueError("all assets must have size_bytes before volume sizing")
        total += int(size)
    gib = 1024**3
    output_reserve = DEFAULT_VOLUME_OUTPUT_RESERVE_GB if reserve_gb is None else int(reserve_gb)
    model_and_scratch_gb = math.ceil(total * (1 + DEFAULT_VOLUME_SCRATCH_RESERVE_RATIO) / gib)
    return max(minimum_gb, model_and_scratch_gb + output_reserve)


def duplicate_url_keys(assets: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for asset in assets:
        url = str(asset.get("url") or "")
        if not url:
            continue
        key = normalized_url_key(url)
        if key in seen and key not in duplicates:
            duplicates.append(key)
        seen.add(key)
    return duplicates


def target_for(model_folder: str, filename: str) -> str:
    return f"assets/comfyui/{normalize_model_folder(model_folder)}/{safe_asset_filename(filename)}"


def peek_url_metadata(url: str, model_folder: str, *, timeout: int = 20) -> dict[str, Any]:
    provider = detect_provider({"url": url})
    headers = _auth_headers(provider)
    if provider == "civitai":
        civitai_metadata = _peek_civitai_download_metadata(url, model_folder, headers=headers, timeout=timeout)
        if civitai_metadata:
            return civitai_metadata
    download_url = canonical_asset_url(url)
    request_url = _auth_url(provider, download_url)
    download_headers = {} if provider == "civitai" else headers
    head = _request_follow("HEAD", request_url, headers=download_headers, timeout=timeout)
    _raise_on_http_error(head)
    chosen = head
    size = _size_from_headers(head["headers"])
    if size is None or size <= 0:
        range_headers = {**download_headers, "Range": "bytes=0-0"}
        ranged = _request_follow("GET", head["final_url"], headers=range_headers, timeout=timeout)
        _raise_on_http_error(ranged)
        ranged_size = _size_from_headers(ranged["headers"])
        if ranged_size is not None and ranged_size > 0:
            size = ranged_size
        chosen = ranged
    filename = _filename_from_headers(chosen["headers"]) or _filename_from_url(chosen["final_url"])
    if not filename:
        filename = "asset"
    return {
        "provider": provider,
        "url_key": normalized_url_key(url),
        "original_url_redacted": redact_url(url),
        "download_url_redacted": redact_url(download_url),
        "final_url_redacted": redact_url(chosen["final_url"]),
        "filename": filename,
        "size_bytes": size,
        "size_unknown": size is None,
        "content_type": chosen["headers"].get("content-type"),
        "etag": chosen["headers"].get("etag"),
        "last_modified": chosen["headers"].get("last-modified"),
        "redirects": chosen["redirects"],
        "model_folder": normalize_model_folder(model_folder),
        "target": target_for(model_folder, filename),
    }


def _peek_civitai_download_metadata(
    url: str,
    model_folder: str,
    *,
    headers: dict[str, str],
    timeout: int,
) -> dict[str, Any] | None:
    parsed = urllib.parse.urlparse(url)
    match = re.search(r"/api/download/models/(\d+)", parsed.path)
    if not match:
        return None
    version_id = match.group(1)
    file_id = urllib.parse.parse_qs(parsed.query).get("fileId", [""])[0]
    api_url = f"https://civitai.com/api/v1/model-versions/{version_id}"
    data = _request_json(api_url, headers=headers, timeout=timeout)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not files:
        raise RuntimeError("civitai_metadata_files_missing")
    selected = None
    if file_id:
        for item in files:
            if isinstance(item, dict) and str(item.get("id") or "") == str(file_id):
                selected = item
                break
    if selected is None:
        for item in files:
            if isinstance(item, dict) and item.get("primary"):
                selected = item
                break
    if selected is None:
        selected = next((item for item in files if isinstance(item, dict)), None)
    if not isinstance(selected, dict):
        raise RuntimeError("civitai_metadata_file_missing")
    filename = str(selected.get("name") or "").strip() or _filename_from_url(str(selected.get("downloadUrl") or "")) or "asset"
    size_kb = selected.get("sizeKB", selected.get("sizeKb"))
    size_bytes = None
    if size_kb is not None:
        try:
            size_bytes = int(round(float(size_kb) * 1024))
        except (TypeError, ValueError):
            size_bytes = None
    final_url = str(selected.get("downloadUrl") or url)
    return {
        "provider": "civitai",
        "url_key": normalized_url_key(url),
        "original_url_redacted": redact_url(url),
        "download_url_redacted": redact_url(url),
        "final_url_redacted": redact_url(final_url),
        "filename": filename,
        "size_bytes": size_bytes,
        "size_unknown": size_bytes is None,
        "content_type": None,
        "etag": None,
        "last_modified": None,
        "redirects": [],
        "model_folder": normalize_model_folder(model_folder),
        "target": target_for(model_folder, filename),
    }


def _raise_on_http_error(response: dict[str, Any]) -> None:
    status = int(response.get("status") or 0)
    if status >= 400:
        raise RuntimeError(f"http_status:{status}")


def _auth_headers(provider: str) -> dict[str, str]:
    token = ""
    if provider == "huggingface":
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""
    elif provider == "civitai":
        token = os.environ.get("CIVITAI_TOKEN") or ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _auth_url(provider: str, url: str) -> str:
    if provider != "civitai":
        return url
    token = os.environ.get("CIVITAI_TOKEN") or ""
    if not token:
        return url
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.lower() == "token" for key, _value in query):
        return url
    query.append(("token", token))
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def _request_json(url: str, *, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={**headers, "User-Agent": "runpod-controller/1.0"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"civitai_api_status:{exc.code}") from exc
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("civitai_api_invalid_json") from exc
    return data if isinstance(data, dict) else {}


def _request_follow(method: str, url: str, *, headers: dict[str, str], timeout: int) -> dict[str, Any]:
    opener = urllib.request.build_opener(NoRedirectHandler)
    current = url
    redirects: list[dict[str, Any]] = []
    last_headers: dict[str, str] = {}
    status = 0
    for _index in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(current, headers={**headers, "User-Agent": "runpod-controller/1.0"}, method=method)
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = resp.status
                last_headers = _lower_headers(resp.headers)
                if method == "GET":
                    resp.read(1)
                return {"status": status, "headers": last_headers, "final_url": current, "redirects": redirects}
        except urllib.error.HTTPError as exc:
            status = exc.code
            last_headers = _lower_headers(exc.headers)
            if status not in REDIRECT_STATUSES:
                return {"status": status, "headers": last_headers, "final_url": current, "redirects": redirects}
            location = exc.headers.get("Location")
            if not location:
                return {"status": status, "headers": last_headers, "final_url": current, "redirects": redirects}
            next_url = urllib.parse.urljoin(current, location)
            redirects.append({"status": status, "from": redact_url(current), "to": redact_url(next_url)})
            current = next_url
    raise RuntimeError(f"too_many_redirects:{MAX_REDIRECTS}")


def _lower_headers(headers: email.message.Message) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items()}


def _size_from_headers(headers: dict[str, str]) -> int | None:
    content_range = headers.get("content-range")
    if content_range:
        match = re.search(r"/(\d+)\s*$", content_range)
        if match:
            return int(match.group(1))
    content_length = headers.get("content-length")
    if content_length and content_length.isdigit():
        return int(content_length)
    return None


def _filename_from_headers(headers: dict[str, str]) -> str | None:
    disposition = headers.get("content-disposition") or ""
    if not disposition:
        return None
    for part in disposition.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.lower() == "filename*":
            value = value.strip().strip('"')
            if "'" in value:
                value = value.rsplit("'", 1)[-1]
            name = urllib.parse.unquote(value).rsplit("/", 1)[-1].strip()
            if name:
                return name
    message = email.message.Message()
    message["Content-Disposition"] = disposition
    filename = message.get_filename()
    if filename:
        return filename.rsplit("/", 1)[-1].strip()
    return None


def _filename_from_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    name = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
    return name or None
