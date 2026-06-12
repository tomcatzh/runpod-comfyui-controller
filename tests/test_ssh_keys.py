import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from controller import ssh_keys
from controller.runpod import RunpodRestAdapter

from test_controller_service import test_settings


class SshKeyManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = test_settings(self.root / "data")
        self.settings.ensure_dirs()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("RUNPOD_SSH_KEY_PATH", None)
        os.environ.pop("RUNPOD_API_KEY", None)
        # Keep the resolver away from any real ~/.runpod key on this machine.
        self.legacy = mock.patch.object(ssh_keys, "LEGACY_RUNPODCTL_KEY", str(self.root / "legacy" / "runpodctl-ssh-key"))
        self.legacy.start()

    def tearDown(self) -> None:
        self.legacy.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_first_start_generates_key_in_data_dir(self) -> None:
        info = ssh_keys.ensure_private_key(self.settings)
        self.assertTrue(info["ok"], info)
        self.assertTrue(info["generated"])
        key_path = Path(info["key_path"])
        self.assertEqual(key_path, ssh_keys.managed_key_path(self.settings))
        self.assertTrue(key_path.exists())
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(info["public_key"].startswith("ssh-ed25519 "))

        again = ssh_keys.ensure_private_key(self.settings)
        self.assertTrue(again["ok"])
        self.assertFalse(again["generated"])
        self.assertEqual(again["public_key"], info["public_key"])

    def test_env_override_wins_over_managed_key(self) -> None:
        ssh_keys.ensure_private_key(self.settings)
        override = self.root / "custom-key"
        os.environ["RUNPOD_SSH_KEY_PATH"] = str(override)
        self.assertEqual(ssh_keys.resolve_private_key_path(self.settings), override)

    def test_legacy_runpodctl_key_is_reused_until_managed_exists(self) -> None:
        legacy = Path(ssh_keys.LEGACY_RUNPODCTL_KEY)
        legacy.parent.mkdir(parents=True)
        legacy.write_text("private", encoding="utf-8")
        self.assertEqual(ssh_keys.resolve_private_key_path(self.settings), legacy)
        info = ssh_keys.ensure_private_key(self.settings)
        self.assertFalse(info["generated"])
        self.assertEqual(info["key_path"], str(legacy))

        managed = ssh_keys.managed_key_path(self.settings)
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.write_text("private", encoding="utf-8")
        self.assertEqual(ssh_keys.resolve_private_key_path(self.settings), managed)

    def test_normalize_public_key_drops_comment(self) -> None:
        self.assertEqual(ssh_keys.normalize_public_key("ssh-ed25519 AAAAC3 host@box\n"), "ssh-ed25519 AAAAC3")
        self.assertEqual(ssh_keys.normalize_public_key("ssh-ed25519 AAAAC3"), "ssh-ed25519 AAAAC3")
        self.assertEqual(ssh_keys.normalize_public_key("garbage"), "")
        self.assertEqual(ssh_keys.normalize_public_key(None), "")

    def test_gpu_pod_env_receives_public_key(self) -> None:
        info = ssh_keys.ensure_private_key(self.settings)
        adapter = RunpodRestAdapter(self.settings)
        env = adapter._env_with_public_key({"HF_TOKEN": "x"})
        self.assertEqual(env["HF_TOKEN"], "x")
        self.assertEqual(env["PUBLIC_KEY"], info["public_key"])

        merged = adapter._env_with_public_key({"PUBLIC_KEY": "ssh-rsa OTHER user@laptop"})
        self.assertEqual(merged["PUBLIC_KEY"], f"ssh-rsa OTHER user@laptop\n{info['public_key']}")

        deduped = adapter._env_with_public_key({"PUBLIC_KEY": info["public_key"] + " renamed-comment"})
        self.assertNotIn("\n", deduped["PUBLIC_KEY"])

    def test_gpu_pod_env_merges_account_registered_keys(self) -> None:
        # Live finding 2026-06-12: a user-supplied PUBLIC_KEY env suppresses
        # RunPod's own injection of account keys, so the adapter must merge
        # them in or manual SSH breaks.
        info = ssh_keys.ensure_private_key(self.settings)
        adapter = RunpodRestAdapter(self.settings)
        calls: list[str] = []

        def fake_graphql(query, variables=None, timeout=45):
            calls.append(query)
            return {"data": {"myself": {"pubKey": "ssh-rsa MANUAL zxf\ninvalid-line"}}}

        adapter._graphql = fake_graphql
        env = adapter._env_with_public_key({})
        self.assertEqual(env["PUBLIC_KEY"], f"ssh-rsa MANUAL zxf\n{info['public_key']}")
        adapter._env_with_public_key({})
        self.assertEqual(len(calls), 1)  # account keys are cached

    def test_gpu_pod_env_survives_account_lookup_failure(self) -> None:
        info = ssh_keys.ensure_private_key(self.settings)
        adapter = RunpodRestAdapter(self.settings)

        def fake_graphql(query, variables=None, timeout=45):
            raise RuntimeError("graphql down")

        adapter._graphql = fake_graphql
        env = adapter._env_with_public_key({})
        self.assertEqual(env["PUBLIC_KEY"], info["public_key"])

    def test_gpu_pod_env_unchanged_without_key(self) -> None:
        adapter = RunpodRestAdapter(self.settings)
        env = adapter._env_with_public_key({"HF_TOKEN": "x"})
        self.assertNotIn("PUBLIC_KEY", env)

    def test_register_skips_when_already_on_account(self) -> None:
        adapter = RunpodRestAdapter(self.settings)
        calls: list[str] = []

        def fake_graphql(query, variables=None, timeout=45):
            calls.append(query)
            return {"data": {"myself": {"id": "u1", "pubKey": "ssh-ed25519 AAAAC3 old-comment"}}}

        adapter._graphql = fake_graphql
        result = adapter.ensure_ssh_public_key_registered("ssh-ed25519 AAAAC3 runpod-comfyui-controller")
        self.assertEqual(result, {"ok": True, "state": "already_registered"})
        self.assertEqual(len(calls), 1)

    def test_register_appends_missing_key(self) -> None:
        adapter = RunpodRestAdapter(self.settings)
        captured: dict = {}

        def fake_graphql(query, variables=None, timeout=45):
            if query.startswith("query"):
                return {"data": {"myself": {"id": "u1", "pubKey": "ssh-rsa OLD laptop"}}}
            captured.update(variables or {})
            return {"data": {"updateUserSettings": {"id": "u1"}}}

        adapter._graphql = fake_graphql
        result = adapter.ensure_ssh_public_key_registered("ssh-ed25519 NEW runpod-comfyui-controller")
        self.assertEqual(result, {"ok": True, "state": "registered"})
        self.assertEqual(captured["input"]["pubKey"], "ssh-rsa OLD laptop\n\nssh-ed25519 NEW runpod-comfyui-controller")

    def test_register_failure_is_reported_not_raised(self) -> None:
        adapter = RunpodRestAdapter(self.settings)

        def fake_graphql(query, variables=None, timeout=45):
            raise RuntimeError("graphql down")

        adapter._graphql = fake_graphql
        result = adapter.ensure_ssh_public_key_registered("ssh-ed25519 NEW c")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "account_keys_unreadable")


if __name__ == "__main__":
    unittest.main()
