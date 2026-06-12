from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from .gpu_catalog import DEFAULT_GPU_VENDOR, DEFAULT_MIN_VRAM_GB
from .utils import parse_bool


@dataclass(frozen=True)
class Settings:
    data_dir: pathlib.Path
    secret_env_file: pathlib.Path
    host: str
    port: int
    runpod_mode: str
    product: str
    default_data_center: str
    default_volume_size_gb: int
    default_min_vram_gb: int
    default_gpu_vendor: str
    default_max_gpu_usd_per_hr: float
    default_max_total_usd: float
    default_lease_minutes: int
    default_cpu_usd_per_hr: float
    workflow_background_threads: bool
    hydration_estimate_minutes: int
    gpu_acquisition_estimate_minutes: int
    hydration_poll_interval_seconds: int
    hydration_timeout_seconds: int
    hydration_ttl_hours: int
    billing_worker_poll_interval_seconds: int
    billing_worker_bucket_size: str
    billing_cpu_absent_grace_hours: int
    watchdog_poll_interval_seconds: int
    output_collector_interval_seconds: int
    idle_shutdown_minutes: int
    reclaim_warning_minutes: int
    tunnel_host: str
    tunnel_port_start: int
    tunnel_port_end: int
    tunnel_auto_recover: bool
    comfyui_remote_port: int
    ssh_remote_port: int
    live_gpu_type_id: str
    live_gpu_price_usd_per_hr: float
    gpu_pod_image: str
    gpu_template_id: str
    cpu_pod_image: str
    cpu_flavor_ids: list[str]
    comfyui_registry_lookup: bool
    comfyui_registry_timeout_seconds: float

    @property
    def db_path(self) -> pathlib.Path:
        return self.data_dir / "db" / "controller.sqlite"

    @property
    def artifacts_dir(self) -> pathlib.Path:
        return self.data_dir / "artifacts"

    @property
    def logs_dir(self) -> pathlib.Path:
        return self.data_dir / "logs"

    @property
    def config_dir(self) -> pathlib.Path:
        return self.data_dir / "config"

    @property
    def secrets_dir(self) -> pathlib.Path:
        return self.data_dir / "secrets"

    @property
    def cache_dir(self) -> pathlib.Path:
        return self.data_dir / "cache"

    def ensure_dirs(self) -> None:
        for path in [
            self.data_dir,
            self.db_path.parent,
            self.artifacts_dir,
            self.logs_dir,
            self.config_dir,
            self.secrets_dir,
            self.cache_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _load_secret_env_file(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value


def load_settings() -> Settings:
    # The Docker image sets CONTROLLER_DATA_DIR=/data explicitly; the bare
    # default targets the operator's home so `python -m controller.server`
    # works out of the box without root-owned paths.
    data_dir = pathlib.Path(os.environ.get("CONTROLLER_DATA_DIR", "~/runpod-controller")).expanduser()
    secret_env_file = pathlib.Path(
        os.environ.get("CONTROLLER_SECRET_ENV_FILE", str(data_dir / "secrets" / "controller.env"))
    ).expanduser()
    _load_secret_env_file(secret_env_file)
    return Settings(
        data_dir=data_dir,
        secret_env_file=secret_env_file,
        host=os.environ.get("CONTROLLER_HOST", "0.0.0.0"),
        port=int(os.environ.get("CONTROLLER_PORT", "8088")),
        runpod_mode=os.environ.get("RUNPOD_MODE", "live").strip().lower(),
        product=os.environ.get("CONTROLLER_PRODUCT", "comfyui"),
        default_data_center=os.environ.get("DEFAULT_DATA_CENTER", "US-KS-2"),
        default_volume_size_gb=int(os.environ.get("DEFAULT_VOLUME_SIZE_GB", "10")),
        default_min_vram_gb=int(os.environ.get("DEFAULT_MIN_VRAM_GB", str(DEFAULT_MIN_VRAM_GB))),
        default_gpu_vendor=os.environ.get("DEFAULT_GPU_VENDOR", DEFAULT_GPU_VENDOR).strip().upper(),
        default_max_gpu_usd_per_hr=float(os.environ.get("DEFAULT_MAX_GPU_USD_PER_HR", "1.25")),
        default_max_total_usd=float(os.environ.get("DEFAULT_MAX_TOTAL_USD", "5.0")),
        default_lease_minutes=int(os.environ.get("DEFAULT_LEASE_MINUTES", "120")),
        default_cpu_usd_per_hr=float(os.environ.get("DEFAULT_CPU_USD_PER_HR", "0.24")),
        workflow_background_threads=parse_bool(os.environ.get("WORKFLOW_BACKGROUND_THREADS"), default=True),
        hydration_estimate_minutes=int(os.environ.get("HYDRATION_ESTIMATE_MINUTES", "15")),
        gpu_acquisition_estimate_minutes=int(os.environ.get("GPU_ACQUISITION_ESTIMATE_MINUTES", "15")),
        hydration_poll_interval_seconds=int(os.environ.get("HYDRATION_POLL_INTERVAL_SECONDS", "10")),
        hydration_timeout_seconds=int(os.environ.get("HYDRATION_TIMEOUT_SECONDS", "7200")),
        hydration_ttl_hours=int(os.environ.get("HYDRATION_TTL_HOURS", "24")),
        billing_worker_poll_interval_seconds=int(os.environ.get("BILLING_WORKER_POLL_INTERVAL_SECONDS", "600")),
        billing_worker_bucket_size=os.environ.get("BILLING_WORKER_BUCKET_SIZE", "hour"),
        billing_cpu_absent_grace_hours=int(os.environ.get("BILLING_CPU_ABSENT_GRACE_HOURS", "24")),
        watchdog_poll_interval_seconds=int(os.environ.get("WATCHDOG_POLL_INTERVAL_SECONDS", "30")),
        output_collector_interval_seconds=int(os.environ.get("OUTPUT_COLLECTOR_INTERVAL_SECONDS", "300")),
        idle_shutdown_minutes=int(os.environ.get("IDLE_SHUTDOWN_MINUTES", "20")),
        reclaim_warning_minutes=int(os.environ.get("RECLAIM_WARNING_MINUTES", "5")),
        tunnel_host=os.environ.get("TUNNEL_HOST", "127.0.0.1"),
        tunnel_port_start=int(os.environ.get("TUNNEL_PORT_START", "18180")),
        tunnel_port_end=int(os.environ.get("TUNNEL_PORT_END", "18220")),
        tunnel_auto_recover=parse_bool(os.environ.get("TUNNEL_AUTO_RECOVER"), default=True),
        comfyui_remote_port=int(os.environ.get("COMFYUI_REMOTE_PORT", "8188")),
        ssh_remote_port=int(os.environ.get("SSH_REMOTE_PORT", "22")),
        live_gpu_type_id=os.environ.get("LIVE_GPU_TYPE_ID", "").strip(),
        live_gpu_price_usd_per_hr=float(os.environ.get("LIVE_GPU_PRICE_USD_PER_HR", "0") or "0"),
        gpu_pod_image=os.environ.get("GPU_POD_IMAGE", "runpod/comfyui:latest"),
        gpu_template_id=os.environ.get("GPU_TEMPLATE_ID", "").strip(),
        cpu_pod_image=os.environ.get("CPU_POD_IMAGE", "python:3.11-slim"),
        cpu_flavor_ids=_csv(os.environ.get("CPU_FLAVOR_IDS", "cpu3c,cpu3g,cpu3m,cpu5c,cpu5g,cpu5m")),
        comfyui_registry_lookup=parse_bool(os.environ.get("COMFYUI_REGISTRY_LOOKUP"), default=True),
        comfyui_registry_timeout_seconds=float(os.environ.get("COMFYUI_REGISTRY_TIMEOUT_SECONDS", "5")),
    )
