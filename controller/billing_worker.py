from __future__ import annotations

import argparse
import json
import os
from typing import Any

from .config import load_settings
from .server import build_service


def _max_polls_from_env() -> int | None:
    value = os.environ.get("BILLING_WORKER_MAX_POLLS")
    if value is None or value.strip() == "":
        return None
    parsed = int(value)
    return None if parsed <= 0 else parsed


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Poll RunPod billing until stopped local resources are calibrated.")
    parser.add_argument("--once", action="store_true", help="Run one calibration attempt when candidates exist, then exit.")
    parser.add_argument("--watch", action="store_true", help="Keep polling forever, including when no candidates are currently present.")
    parser.add_argument("--max-polls", type=int, default=_max_polls_from_env(), help="Maximum polling attempts; omit or use 0 for unlimited.")
    parser.add_argument("--poll-interval-seconds", type=int, default=settings.billing_worker_poll_interval_seconds)
    parser.add_argument("--bucket-size", default=settings.billing_worker_bucket_size)
    args = parser.parse_args(argv)

    max_polls = 1 if args.once else args.max_polls
    if max_polls is not None and max_polls <= 0:
        max_polls = None

    service = build_service(settings)
    result: dict[str, Any] = service.run_billing_calibration_worker(
        poll_interval_seconds=args.poll_interval_seconds,
        bucket_size=args.bucket_size,
        max_polls=max_polls,
        watch=args.watch,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
