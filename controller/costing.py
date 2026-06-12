from __future__ import annotations

import datetime as dt
from typing import Any

from .utils import parse_iso, utc_now


NETWORK_VOLUME_PRICE_USD_PER_GB_MONTH_FIRST_TB = 0.07
NETWORK_VOLUME_PRICE_USD_PER_GB_MONTH_BEYOND_TB = 0.05
NETWORK_VOLUME_FIRST_TB_GB = 1024
HOURS_PER_MONTH_FOR_STORAGE_ESTIMATE = 730.0


TERMINAL_STATES = {"deleted", "stopped", "reclaimed", "failed"}


def provider_mode(provider_id: str | None) -> str:
    if provider_id and provider_id.startswith("fake-"):
        return "fake"
    if provider_id:
        return "live"
    return "unknown"


def is_fake_provider(provider_id: str | None) -> bool:
    return provider_mode(provider_id) == "fake"


def runtime_seconds(created_at: str | None, terminal_at: str | None, *, now: dt.datetime | None = None) -> float:
    started = parse_iso(created_at)
    if not started:
        return 0.0
    ended = parse_iso(terminal_at) or now or utc_now()
    if ended < started:
        return 0.0
    return max(0.0, (ended - started).total_seconds())


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def billed_runtime_seconds(row: dict[str, Any], *, now: dt.datetime | None = None) -> float:
    billed_ms = _int_or_none(row.get("billed_time_ms"))
    if billed_ms is not None and billed_ms >= 0:
        return billed_ms / 1000.0
    return runtime_seconds(row.get("billed_start_at"), row.get("billed_end_at"), now=now)


def format_runtime(seconds: float) -> str:
    total = int(max(0, round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def network_volume_rate_usd_per_hr(size_gb: int | float | None, *, fake: bool = False) -> float:
    if fake:
        return 0.0
    size = max(0.0, float(size_gb or 0))
    first_tier_gb = min(size, NETWORK_VOLUME_FIRST_TB_GB)
    beyond_tier_gb = max(0.0, size - NETWORK_VOLUME_FIRST_TB_GB)
    monthly = (
        first_tier_gb * NETWORK_VOLUME_PRICE_USD_PER_GB_MONTH_FIRST_TB
        + beyond_tier_gb * NETWORK_VOLUME_PRICE_USD_PER_GB_MONTH_BEYOND_TB
    )
    return monthly / HOURS_PER_MONTH_FOR_STORAGE_ESTIMATE


def pod_rate_usd_per_hr(row: dict[str, Any]) -> float:
    # Internally this still maps to RunPod's costPerHr/adjustedCostPerHr response
    # fields. The UI labels it as a rate, not as accumulated cost.
    try:
        return float(row.get("cost_per_hr") or 0)
    except (TypeError, ValueError):
        return 0.0


def enrich_pod_cost(row: dict[str, Any], *, now: dt.datetime | None = None) -> dict[str, Any]:
    enriched = dict(row)
    rate = 0.0 if is_fake_provider(enriched.get("provider_pod_id")) else pod_rate_usd_per_hr(enriched)
    terminal_at = enriched.get("stopped_at") or enriched.get("deleted_at")
    if not terminal_at and str(enriched.get("state")) in TERMINAL_STATES:
        terminal_at = enriched.get("updated_at")
    estimated_seconds = runtime_seconds(enriched.get("created_at"), terminal_at, now=now)
    estimated_cost = rate * estimated_seconds / 3600.0
    actual_cost = _float_or_none(enriched.get("actual_cost_usd"))
    has_actual = actual_cost is not None
    actual_seconds = billed_runtime_seconds(enriched, now=now) if has_actual else 0.0
    effective_seconds = actual_seconds if has_actual and actual_seconds > 0 else estimated_seconds
    effective_start_at = enriched.get("billed_start_at") if has_actual and enriched.get("billed_start_at") else enriched.get("created_at")
    effective_stop_at = enriched.get("billed_end_at") if has_actual and enriched.get("billed_end_at") else terminal_at
    enriched["provider_mode"] = provider_mode(enriched.get("provider_pod_id"))
    enriched["rate_usd_per_hr"] = round(rate, 6)
    enriched["estimated_runtime_seconds"] = round(estimated_seconds, 3)
    enriched["estimated_runtime"] = format_runtime(estimated_seconds)
    enriched["billed_runtime_seconds"] = round(actual_seconds, 3) if has_actual else None
    enriched["runtime_seconds"] = round(effective_seconds, 3)
    enriched["runtime_hours"] = round(effective_seconds / 3600.0, 6)
    enriched["runtime"] = format_runtime(effective_seconds)
    enriched["estimated_cost_usd"] = round(estimated_cost, 6)
    enriched["actual_cost_usd"] = round(actual_cost, 6) if has_actual else None
    enriched["effective_cost_usd"] = round(actual_cost if has_actual else estimated_cost, 6)
    enriched["cost_source"] = "runpod_billing" if has_actual else "estimate"
    enriched["effective_start_at"] = effective_start_at
    enriched["effective_stop_at"] = effective_stop_at
    return enriched


def enrich_volume_cost(row: dict[str, Any], *, now: dt.datetime | None = None) -> dict[str, Any]:
    enriched = dict(row)
    fake = is_fake_provider(enriched.get("provider_volume_id"))
    rate = network_volume_rate_usd_per_hr(enriched.get("size_gb"), fake=fake)
    terminal_at = enriched.get("deleted_at")
    if not terminal_at and str(enriched.get("state")) in TERMINAL_STATES:
        terminal_at = enriched.get("updated_at")
    estimated_seconds = runtime_seconds(enriched.get("created_at"), terminal_at, now=now)
    estimated_cost = rate * estimated_seconds / 3600.0
    actual_cost = _float_or_none(enriched.get("actual_cost_usd"))
    has_actual = actual_cost is not None
    actual_seconds = billed_runtime_seconds(enriched, now=now) if has_actual else 0.0
    effective_seconds = actual_seconds if has_actual and actual_seconds > 0 else estimated_seconds
    effective_start_at = enriched.get("billed_start_at") if has_actual and enriched.get("billed_start_at") else enriched.get("created_at")
    effective_stop_at = enriched.get("billed_end_at") if has_actual and enriched.get("billed_end_at") else terminal_at
    enriched["provider_mode"] = provider_mode(enriched.get("provider_volume_id"))
    enriched["rate_usd_per_hr"] = round(rate, 6)
    enriched["estimated_runtime_seconds"] = round(estimated_seconds, 3)
    enriched["estimated_runtime"] = format_runtime(estimated_seconds)
    enriched["billed_runtime_seconds"] = round(actual_seconds, 3) if has_actual else None
    enriched["runtime_seconds"] = round(effective_seconds, 3)
    enriched["runtime_hours"] = round(effective_seconds / 3600.0, 6)
    enriched["runtime"] = format_runtime(effective_seconds)
    enriched["estimated_cost_usd"] = round(estimated_cost, 6)
    enriched["actual_cost_usd"] = round(actual_cost, 6) if has_actual else None
    enriched["effective_cost_usd"] = round(actual_cost if has_actual else estimated_cost, 6)
    enriched["cost_source"] = "runpod_billing" if has_actual else "estimate"
    enriched["effective_start_at"] = effective_start_at
    enriched["effective_stop_at"] = effective_stop_at
    return enriched


def split_active_recent(rows: list[dict[str, Any]], *, history_limit: int = 50) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active = [row for row in rows if str(row.get("state")) not in TERMINAL_STATES]
    recent = [row for row in rows if str(row.get("state")) in TERMINAL_STATES][:history_limit]
    return active, recent
