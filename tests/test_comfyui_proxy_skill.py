from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "skills" / "comfyui-proxy-api" / "comfyui_api.py"
spec = importlib.util.spec_from_file_location("comfyui_api", SCRIPT)
comfyui_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comfyui_api)


class ComfyuiProxySkillTest(unittest.TestCase):
    def test_headers_carry_browser_identity_for_cloudflare(self) -> None:
        headers = comfyui_api.build_headers("https://pod-8188.proxy.runpod.net/", json_body=True)
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertEqual(headers["Origin"], "https://pod-8188.proxy.runpod.net")
        self.assertEqual(headers["Referer"], "https://pod-8188.proxy.runpod.net/")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("Content-Type", comfyui_api.build_headers("https://x"))

    def test_load_graph_rejects_ui_format_with_pointer(self) -> None:
        ui_workflow = {"nodes": [{"id": 1, "type": "KSampler"}], "links": []}
        path = pathlib.Path(self.enterContext(__import__("tempfile").TemporaryDirectory())) / "ui.json"
        path.write_text(json.dumps(ui_workflow), encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            comfyui_api.load_graph(str(path))
        self.assertIn("API format", str(ctx.exception))

    def test_load_graph_accepts_api_format(self) -> None:
        graph = {"1": {"class_type": "KSampler", "inputs": {}}}
        path = pathlib.Path(self.enterContext(__import__("tempfile").TemporaryDirectory())) / "api.json"
        path.write_text(json.dumps(graph), encoding="utf-8")
        self.assertEqual(comfyui_api.load_graph(str(path)), graph)

    def test_history_images_flattens_output_nodes(self) -> None:
        entry = {"outputs": {"10": {"images": [{"filename": "a.png"}]}, "11": {"images": [{"filename": "b.png"}]}}}
        names = [img["filename"] for img in comfyui_api.history_images(entry)]
        self.assertEqual(sorted(names), ["a.png", "b.png"])
