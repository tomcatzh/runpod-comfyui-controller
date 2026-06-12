#!/usr/bin/env python3
"""Drive a ComfyUI instance behind the RunPod proxy URL.

RunPod's proxy (https://<pod>-<port>.proxy.runpod.net) sits behind Cloudflare,
which rejects non-browser requests: POSTs without a browser-like User-Agent
return 403 (the RunPod GraphQL API blocks the same way with error 1010).
Every request from this script carries a proven header set (User-Agent +
Origin + Referer), so agents and shell scripts can call the ComfyUI API
without rediscovering that failure mode.

Stdlib only. Usage examples live in SKILL.md next to this file.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 runpod-comfyui-skill/1.0"
)


def build_headers(base_url: str, *, json_body: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Origin": base_url.rstrip("/"),
        "Referer": base_url.rstrip("/") + "/",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def request_json(base_url: str, path: str, payload: dict[str, Any] | None = None, *, timeout: int = 30) -> Any:
    url = base_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=build_headers(base_url, json_body=payload is not None))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def request_bytes(base_url: str, path: str, *, timeout: int = 60) -> bytes:
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, headers=build_headers(base_url))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def load_graph(source: str) -> dict[str, Any]:
    text = sys.stdin.read() if source == "-" else pathlib.Path(source).read_text(encoding="utf-8")
    graph = json.loads(text)
    if not isinstance(graph, dict) or not graph:
        raise SystemExit("graph must be a non-empty JSON object in ComfyUI API format (node id -> {class_type, inputs})")
    sample = next(iter(graph.values()))
    if not isinstance(sample, dict) or "class_type" not in sample:
        raise SystemExit("this looks like a UI-format workflow; /prompt needs the API format "
                         "(in ComfyUI: Workflow menu -> Export (API))")
    return graph


def history_images(entry: dict[str, Any]) -> list[dict[str, Any]]:
    images = []
    for node in (entry.get("outputs") or {}).values():
        images.extend(node.get("images") or [])
    return images


def wait_for_prompt(base_url: str, prompt_id: str, *, timeout_seconds: int, poll_seconds: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        hist = request_json(base_url, f"/history/{prompt_id}")
        entry = hist.get(prompt_id)
        if entry:
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                return {"ok": False, "status": "error", "messages": status.get("messages"), "prompt_id": prompt_id}
            if status.get("completed") or entry.get("outputs"):
                return {"ok": True, "status": status.get("status_str") or "success", "prompt_id": prompt_id,
                        "images": history_images(entry)}
        time.sleep(poll_seconds)
    return {"ok": False, "status": "timeout", "prompt_id": prompt_id}


def cmd_prompt(args: argparse.Namespace) -> int:
    graph = load_graph(args.graph)
    result = request_json(args.base_url, "/prompt", {"prompt": graph, "client_id": "comfyui-proxy-api-skill"})
    if result.get("node_errors"):
        print(json.dumps({"ok": False, "node_errors": result["node_errors"]}, indent=2))
        return 1
    prompt_id = result.get("prompt_id")
    if not args.wait:
        print(json.dumps({"ok": True, "prompt_id": prompt_id}))
        return 0
    outcome = wait_for_prompt(args.base_url, prompt_id, timeout_seconds=args.timeout)
    print(json.dumps(outcome, indent=2))
    return 0 if outcome.get("ok") else 1


def cmd_models(args: argparse.Namespace) -> int:
    info = request_json(args.base_url, f"/object_info/{urllib.parse.quote(args.loader)}")
    node = info.get(args.loader) or {}
    options = {}
    for key, value in ((node.get("input") or {}).get("required") or {}).items():
        if isinstance(value, list) and value and isinstance(value[0], list):
            options[key] = value[0]
    print(json.dumps(options, indent=2, ensure_ascii=False))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    print(json.dumps(request_json(args.base_url, args.path), indent=2, ensure_ascii=False))
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    query = urllib.parse.urlencode({"filename": args.filename, "subfolder": args.subfolder, "type": args.type})
    payload = request_bytes(args.base_url, f"/view?{query}")
    out = pathlib.Path(args.output or args.filename)
    out.write_bytes(payload)
    print(json.dumps({"ok": True, "path": str(out), "bytes": len(payload)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url", help="ComfyUI base URL, e.g. https://<pod>-8188.proxy.runpod.net (the session ui_url)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prompt", help="queue an API-format workflow; --wait polls until images exist")
    p.add_argument("graph", help="path to API-format workflow JSON, or '-' for stdin")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=int, default=600, help="seconds to wait with --wait (default 600)")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("models", help="list selectable model files for a loader node type")
    p.add_argument("loader", help="e.g. CheckpointLoaderSimple, UNETLoader, VAELoader, CLIPLoader, LoraLoader")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("get", help="GET any JSON endpoint (/system_stats, /queue, /history/<id>, /object_info)")
    p.add_argument("path")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("download", help="download a generated image via /view")
    p.add_argument("filename")
    p.add_argument("-o", "--output", default=None)
    p.add_argument("--subfolder", default="")
    p.add_argument("--type", default="output")
    p.set_defaults(func=cmd_download)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
