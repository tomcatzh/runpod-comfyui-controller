"""Local SSH keypair management for pod access.

The controller needs a private key to SSH into GPU pods for environment
configuration. A fresh environment has none, so the controller generates an
ed25519 keypair on first start under the data directory (which survives Docker
restarts), injects the public key into every pod it creates through the
PUBLIC_KEY env var that RunPod base images append to authorized_keys, and
best-effort registers it on the RunPod account so manual SSH works too.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
from typing import Any

from .config import Settings

# Key created by `runpodctl ssh add-key`; reused when present so existing
# deployments keep working with the key already registered on the account.
LEGACY_RUNPODCTL_KEY = "~/.runpod/ssh/runpodctl-ssh-key"
KEY_COMMENT = "runpod-comfyui-controller"


def managed_key_path(settings: Settings) -> pathlib.Path:
    return settings.secrets_dir / "runpod-ssh-key"


def resolve_private_key_path(settings: Settings) -> pathlib.Path:
    override = os.environ.get("RUNPOD_SSH_KEY_PATH", "").strip()
    if override:
        return pathlib.Path(override).expanduser()
    managed = managed_key_path(settings)
    if managed.exists():
        return managed
    legacy = pathlib.Path(LEGACY_RUNPODCTL_KEY).expanduser()
    if legacy.exists():
        return legacy
    return managed


def public_key_for(private_key_path: pathlib.Path | str) -> str:
    pub = pathlib.Path(f"{private_key_path}.pub")
    try:
        return pub.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def normalize_public_key(text: Any) -> str:
    """Reduce a public key line to "<type> <blob>" so comments don't break equality."""
    parts = str(text or "").split()
    return " ".join(parts[:2]) if len(parts) >= 2 else ""


def ensure_private_key(settings: Settings) -> dict[str, Any]:
    """Generate a keypair if no usable private key exists yet."""
    path = resolve_private_key_path(settings)
    if path.exists():
        return {"ok": True, "key_path": str(path), "public_key": public_key_for(path), "generated": False}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", KEY_COMMENT, "-q", "-f", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "ssh_keygen_failed", "key_path": str(path), "error": repr(exc), "generated": False}
    if proc.returncode != 0 or not path.exists():
        return {
            "ok": False,
            "reason": "ssh_keygen_failed",
            "key_path": str(path),
            "error": (proc.stderr or proc.stdout or "").strip()[:500],
            "generated": False,
        }
    os.chmod(path, 0o600)
    return {"ok": True, "key_path": str(path), "public_key": public_key_for(path), "generated": True}
