from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import uuid
from typing import Any


UTC = dt.UTC


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def utc_iso() -> str:
    return utc_now().isoformat()


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: pathlib.Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json_loads(path.read_text(encoding="utf-8"), default)


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


def sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SECRET_KEY_MARKERS = ("KEY", "SECRET", "TOKEN", "AUTHORIZATION", "PASSWORD", "API_KEY")


def configured_secret_values() -> list[str]:
    values: list[str] = []
    for key, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if any(marker in key.upper() for marker in SECRET_KEY_MARKERS):
            values.append(value)
    values.sort(key=len, reverse=True)
    return values


def redact_secrets(value: Any, *, extra_values: list[str] | None = None) -> Any:
    secret_values = [item for item in [*(extra_values or []), *configured_secret_values()] if item]

    def scrub(item: Any, key: str = "") -> Any:
        if any(marker in key.upper() for marker in SECRET_KEY_MARKERS):
            return "<redacted>"
        if isinstance(item, dict):
            return {str(k): scrub(v, str(k)) for k, v in item.items()}
        if isinstance(item, list):
            return [scrub(child, key) for child in item]
        if isinstance(item, tuple):
            return tuple(scrub(child, key) for child in item)
        if isinstance(item, str):
            text = item
            for secret in secret_values:
                text = text.replace(secret, "<redacted>")
            return text
        return item

    return scrub(value)
