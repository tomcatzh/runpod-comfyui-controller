from __future__ import annotations

from typing import Any


DEFAULT_GPU_VENDOR = "NVIDIA"
DEFAULT_MIN_VRAM_GB = 24

DEFAULT_DRYRUN_DATA_CENTERS = [
    "US-KS-2",
    "US-IL-1",
    "US-WA-1",
    "US-CA-2",
    "US-GA-2",
    "US-MD-1",
    "US-NC-1",
    "US-NC-2",
    "US-MO-1",
    "US-MO-2",
    "US-NE-1",
    "EU-CZ-1",
    "EU-RO-1",
    "EUR-IS-1",
    "EUR-NO-1",
]

S3_NETWORK_VOLUME_DATA_CENTERS = {
    "EU-CZ-1",
    "EU-RO-1",
    "EUR-IS-1",
    "EUR-NO-1",
    "US-CA-2",
    "US-IL-1",
    "US-KS-2",
    "US-MD-1",
    "US-MO-1",
    "US-MO-2",
    "US-NC-1",
    "US-NC-2",
    "US-NE-1",
    "US-WA-1",
}

REST_POD_CREATE_DATA_CENTERS = {
    "AP-JP-1",
    "CA-MTL-3",
    "EU-CZ-1",
    "EU-FR-1",
    "EU-NL-1",
    "EU-RO-1",
    "EUR-IS-1",
    "EUR-IS-3",
    "EUR-NO-1",
    "US-CA-2",
    "US-IL-1",
    "US-KS-2",
    "US-MD-1",
    "US-NC-1",
    "US-TX-3",
    "US-WA-1",
}

COMFYUI_CANDIDATE_DATA_CENTERS = S3_NETWORK_VOLUME_DATA_CENTERS & REST_POD_CREATE_DATA_CENTERS

GPU_SPECS: dict[str, dict[str, Any]] = {
    "NVIDIA A100 80GB PCIe": {"vendor": "NVIDIA", "vram_gb": 80, "template": "comfyui", "public_price_usd_per_hr": 1.39},
    "NVIDIA A100-SXM4-80GB": {"vendor": "NVIDIA", "vram_gb": 80, "template": "comfyui", "public_price_usd_per_hr": 1.49},
    "NVIDIA A30": {"vendor": "NVIDIA", "vram_gb": 24, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA A40": {"vendor": "NVIDIA", "vram_gb": 48, "template": "comfyui", "public_price_usd_per_hr": 0.44},
    "NVIDIA B200": {"vendor": "NVIDIA", "vram_gb": 180, "template": "comfyui-cuda-13", "public_price_usd_per_hr": 5.89},
    "NVIDIA B300 SXM6 AC": {"vendor": "NVIDIA", "vram_gb": 288, "template": "comfyui-cuda-13", "public_price_usd_per_hr": None},
    "NVIDIA GeForce RTX 3070": {"vendor": "NVIDIA", "vram_gb": 8, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA GeForce RTX 3080": {"vendor": "NVIDIA", "vram_gb": 10, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA GeForce RTX 3080 Ti": {"vendor": "NVIDIA", "vram_gb": 12, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA GeForce RTX 3090": {"vendor": "NVIDIA", "vram_gb": 24, "template": "comfyui", "public_price_usd_per_hr": 0.46},
    "NVIDIA GeForce RTX 3090 Ti": {"vendor": "NVIDIA", "vram_gb": 24, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA GeForce RTX 4070 Ti": {"vendor": "NVIDIA", "vram_gb": 12, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA GeForce RTX 4080": {"vendor": "NVIDIA", "vram_gb": 16, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA GeForce RTX 4080 SUPER": {"vendor": "NVIDIA", "vram_gb": 16, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA GeForce RTX 4090": {"vendor": "NVIDIA", "vram_gb": 24, "template": "comfyui", "public_price_usd_per_hr": 0.69},
    "NVIDIA GeForce RTX 5080": {"vendor": "NVIDIA", "vram_gb": 16, "template": "comfyui-cuda-13", "public_price_usd_per_hr": None},
    "NVIDIA GeForce RTX 5090": {"vendor": "NVIDIA", "vram_gb": 32, "template": "comfyui-cuda-13", "public_price_usd_per_hr": 0.99},
    "NVIDIA H100 80GB HBM3": {"vendor": "NVIDIA", "vram_gb": 80, "template": "comfyui", "public_price_usd_per_hr": 3.29},
    "NVIDIA H100 NVL": {"vendor": "NVIDIA", "vram_gb": 94, "template": "comfyui", "public_price_usd_per_hr": 3.19},
    "NVIDIA H100 PCIe": {"vendor": "NVIDIA", "vram_gb": 80, "template": "comfyui", "public_price_usd_per_hr": 2.89},
    "NVIDIA H200": {"vendor": "NVIDIA", "vram_gb": 141, "template": "comfyui-cuda-13", "public_price_usd_per_hr": 4.39},
    "NVIDIA H200 NVL": {"vendor": "NVIDIA", "vram_gb": 143, "template": "comfyui-cuda-13", "public_price_usd_per_hr": None},
    "NVIDIA L4": {"vendor": "NVIDIA", "vram_gb": 24, "template": "comfyui", "public_price_usd_per_hr": 0.39},
    "NVIDIA L40": {"vendor": "NVIDIA", "vram_gb": 48, "template": "comfyui", "public_price_usd_per_hr": 0.99},
    "NVIDIA L40S": {"vendor": "NVIDIA", "vram_gb": 48, "template": "comfyui", "public_price_usd_per_hr": 0.86},
    "NVIDIA RTX 2000 Ada Generation": {"vendor": "NVIDIA", "vram_gb": 16, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA RTX 4000 Ada Generation": {"vendor": "NVIDIA", "vram_gb": 20, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA RTX 4000 SFF Ada Generation": {"vendor": "NVIDIA", "vram_gb": 20, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA RTX 5000 Ada Generation": {"vendor": "NVIDIA", "vram_gb": 32, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA RTX 6000 Ada Generation": {"vendor": "NVIDIA", "vram_gb": 48, "template": "comfyui", "public_price_usd_per_hr": 0.77},
    "NVIDIA RTX A2000": {"vendor": "NVIDIA", "vram_gb": 6, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA RTX A4000": {"vendor": "NVIDIA", "vram_gb": 16, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA RTX A4500": {"vendor": "NVIDIA", "vram_gb": 20, "template": "comfyui", "public_price_usd_per_hr": None},
    "NVIDIA RTX A5000": {"vendor": "NVIDIA", "vram_gb": 24, "template": "comfyui", "public_price_usd_per_hr": 0.27},
    "NVIDIA RTX A6000": {"vendor": "NVIDIA", "vram_gb": 48, "template": "comfyui", "public_price_usd_per_hr": 0.49},
    "NVIDIA RTX PRO 4500 Blackwell": {"vendor": "NVIDIA", "vram_gb": 32, "template": "comfyui-cuda-13", "public_price_usd_per_hr": None},
    "NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition": {"vendor": "NVIDIA", "vram_gb": 96, "template": "comfyui-cuda-13", "public_price_usd_per_hr": 2.09},
    "NVIDIA RTX PRO 6000 Blackwell Server Edition": {"vendor": "NVIDIA", "vram_gb": 96, "template": "comfyui-cuda-13", "public_price_usd_per_hr": 2.09},
    "NVIDIA RTX PRO 6000 Blackwell Workstation Edition": {"vendor": "NVIDIA", "vram_gb": 96, "template": "comfyui-cuda-13", "public_price_usd_per_hr": 2.09},
    "Tesla V100-PCIE-16GB": {"vendor": "NVIDIA", "vram_gb": 16, "template": "comfyui", "public_price_usd_per_hr": None},
    "Tesla V100-SXM2-16GB": {"vendor": "NVIDIA", "vram_gb": 16, "template": "comfyui", "public_price_usd_per_hr": None},
    "Tesla V100-SXM2-32GB": {"vendor": "NVIDIA", "vram_gb": 32, "template": "comfyui", "public_price_usd_per_hr": None},
    "AMD Instinct MI300X OAM": {"vendor": "AMD", "vram_gb": 192, "template": "rocm", "public_price_usd_per_hr": None},
}


def normalize_gpu_vendor(value: object) -> str:
    vendor = str(value or DEFAULT_GPU_VENDOR).strip().upper()
    if vendor in {"NVIDIA", "NVIDIA_ONLY", "NVIDIA-ONLY"}:
        return "NVIDIA"
    if vendor == "AMD":
        return "AMD"
    return vendor


def normalize_min_vram_gb(value: object) -> int:
    try:
        parsed = int(float(str(value)))
    except (TypeError, ValueError):
        parsed = DEFAULT_MIN_VRAM_GB
    return max(1, parsed)


def gpu_type_meets_intent(gpu_type_id: str | None, *, min_vram_gb: int, gpu_vendor: str) -> tuple[bool, str]:
    if not gpu_type_id:
        return False, "missing_gpu_type_id"
    spec = GPU_SPECS.get(gpu_type_id)
    if not spec:
        return False, "unknown_gpu_type_specs"
    actual_vendor = normalize_gpu_vendor(spec.get("vendor"))
    requested_vendor = normalize_gpu_vendor(gpu_vendor)
    if actual_vendor != requested_vendor:
        return False, f"gpu_vendor_mismatch:{actual_vendor}!={requested_vendor}"
    actual_vram = int(spec.get("vram_gb") or 0)
    if actual_vram < min_vram_gb:
        return False, f"gpu_vram_below_min:{actual_vram}<{min_vram_gb}"
    return True, "gpu_matches_intent"


def select_comfyui_gpu_type(*, min_vram_gb: int, gpu_vendor: str) -> str | None:
    rows = comfyui_gpu_rows(min_vram_gb=min_vram_gb, gpu_vendor=gpu_vendor)
    if not rows:
        return None
    return rows[0]["gpu_type_id"]


def comfyui_gpu_rows(*, min_vram_gb: int, gpu_vendor: str, max_usd_per_hr: float | None = None) -> list[dict[str, Any]]:
    rows = []
    max_rate = float(max_usd_per_hr or 0)
    for gpu_type_id in GPU_SPECS:
        ok, reason = gpu_type_meets_intent(gpu_type_id, min_vram_gb=min_vram_gb, gpu_vendor=gpu_vendor)
        if not ok:
            continue
        spec = GPU_SPECS[gpu_type_id]
        price = spec.get("public_price_usd_per_hr")
        if max_rate > 0 and (price is None or float(price) > max_rate):
            continue
        rows.append(
            {
                "gpu_type_id": gpu_type_id,
                "vendor": spec["vendor"],
                "vram_gb": spec["vram_gb"],
                "template": spec.get("template"),
                "estimated_price_usd_per_hr": price,
                "price_source": "runpod_public_pricing_catalog" if price is not None else "unknown",
                "reason": reason,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            float(row["estimated_price_usd_per_hr"]) if row.get("estimated_price_usd_per_hr") is not None else 1_000_000.0,
            int(row.get("vram_gb") or 0),
            str(row.get("gpu_type_id") or ""),
        ),
    )


def dryrun_data_centers(excluded: set[str] | None = None) -> list[str]:
    excluded = excluded or set()
    return [dc for dc in DEFAULT_DRYRUN_DATA_CENTERS if dc in COMFYUI_CANDIDATE_DATA_CENTERS and dc not in excluded]
