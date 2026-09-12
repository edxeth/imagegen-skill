import base64
import binascii
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
import zlib


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("IMAGEGEN_SCRIPT", ROOT / "imagegen" / "scripts" / "image_gen.py"))
SCRIPT_SPEC = importlib.util.spec_from_file_location("image_gen_test_target", SCRIPT)
if SCRIPT_SPEC is None or SCRIPT_SPEC.loader is None:
    raise RuntimeError(f"Could not load image generator module from {SCRIPT}")
SCRIPT_MODULE = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(SCRIPT_MODULE)


def png_bytes(width: int, height: int, color: Tuple[int, int, int, int] = (40, 80, 120, 255)) -> bytes:
    row = bytes((0,)) + bytes(color) * width
    raw = row * height

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def image_dimensions(raw: bytes) -> Tuple[int, int]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", raw[16:24])
    raise AssertionError("test fixture is not a PNG")


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def jpeg_header_bytes(width: int, height: int) -> bytes:
    sof0_data = b"\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00"
    return b"\xff\xd8\xff\xc0" + struct.pack(">H", len(sof0_data) + 2) + sof0_data + b"\xff\xd9"


def webp_vp8x_header_bytes(width: int, height: int) -> bytes:
    payload = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk


class ServerState:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.get_paths: List[str] = []
        self.generation_response: Optional[Callable[[Dict[str, Any]], Any]] = None
        self.edit_response: Optional[Callable[[bytes], Any]] = None
        self.assets: Dict[str, bytes] = {}
        self.lock = threading.Lock()


class _ImageHandler(BaseHTTPRequestHandler):
    server_version = "ImageGenTestServer/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def state(self) -> ServerState:
        return self.server.state  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        self._send_raw(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _send_raw(self, status: int, raw: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        with self.state.lock:
            self.state.get_paths.append(self.path)
        if self.path in self.state.assets:
            raw = self.state.assets[self.path]
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.endswith("/models"):
            self._send_json(200, {"data": [{"id": "custom/alias"}]})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload: Optional[Dict[str, Any]] = None
        if self.headers.get("Content-Type", "").startswith("application/json"):
            payload = json.loads(body.decode("utf-8"))

        kind = "generate" if self.path.endswith("/images/generations") else "edit"
        with self.state.lock:
            self.state.requests.append(
                {
                    "kind": kind,
                    "path": self.path,
                    "body": body,
                    "payload": payload,
                    "authorization": self.headers.get("Authorization"),
                }
            )

        if kind == "generate":
            handler = self.state.generation_response
            result = handler(payload or {}) if handler else {"data": [{"b64_json": b64(png_bytes(2, 2))}]}
        else:
            handler = self.state.edit_response
            result = handler(body) if handler else {"data": [{"b64_json": b64(png_bytes(2, 2))}]}

        status = 200
        content_type = "application/json"
        if isinstance(result, tuple):
            if len(result) == 3:
                status, result, content_type = result
            else:
                status, result = result
        if isinstance(result, bytes):
            self._send_raw(status, result, content_type)
        else:
            self._send_json(status, result)


class LocalImageServer:
    def __init__(self) -> None:
        self.state = ServerState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ImageHandler)
        self.httpd.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}/v1"

    def asset_url(self, name: str) -> str:
        path = f"/asset/{name}"
        self.state.assets[path] = png_bytes(3, 2)
        host, port = self.httpd.server_address
        return f"http://{host}:{port}{path}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class ImageGenIntegrationTests(unittest.TestCase):
    def run_cli(self, *args: str, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
        child_env = os.environ.copy()
        for name in (
            "PI_IMAGEGEN_BASE_URL",
            "OPENAI_BASE_URL",
            "PI_IMAGEGEN_API_KEY",
            "OPENAI_API_KEY",
            "PI_IMAGEGEN_MODEL",
            "PI_IMAGEGEN_OUTPUT_DIR",
            "PI_CODING_AGENT_DIR",
        ):
            child_env.pop(name, None)
        if env:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=ROOT,
            env=child_env,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def test_default_model_cli_env_and_custom_alias_overrides(self) -> None:
        server = LocalImageServer()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                def response(payload: Dict[str, Any]) -> Dict[str, Any]:
                    return {"data": [{"b64_json": b64(png_bytes(2, 2))}]}

                server.state.generation_response = response
                default = self.run_cli(
                    "generate", "--base-url", server.base_url, "--prompt", "default",
                    "--no-augment", "--out", str(Path(tmp) / "default.png"),
                )
                self.assertEqual(default.returncode, 0, default.stderr)
                self.assertEqual(server.state.requests[-1]["payload"]["model"], "gpt-image-2.5-flare")
                self.assertEqual(server.state.get_paths, [])

                env_result = self.run_cli(
                    "generate", "--base-url", server.base_url, "--prompt", "env",
                    "--no-augment", "--out", str(Path(tmp) / "env.png"),
                    env={"PI_IMAGEGEN_MODEL": "env-image-model"},
                )
                self.assertEqual(env_result.returncode, 0, env_result.stderr)
                self.assertEqual(server.state.requests[-1]["payload"]["model"], "env-image-model")

                alias = "vendor/custom-image-alias:v7"
                cli_result = self.run_cli(
                    "generate", "--base-url", server.base_url, "--model", alias,
                    "--prompt", "cli", "--no-augment", "--out", str(Path(tmp) / "cli.png"),
                )
                self.assertEqual(cli_result.returncode, 0, cli_result.stderr)
                self.assertEqual(server.state.requests[-1]["payload"]["model"], alias)
        finally:
            server.close()

    def test_endpoint_and_auth_precedence(self) -> None:
        env_server = LocalImageServer()
        cli_server = LocalImageServer()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = self.run_cli(
                    "generate", "--prompt", "env endpoint", "--no-augment",
                    "--out", str(Path(tmp) / "env.png"),
                    env={
                        "PI_IMAGEGEN_BASE_URL": env_server.base_url,
                        "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
                        "PI_IMAGEGEN_API_KEY": "pi-token",
                        "OPENAI_API_KEY": "openai-token",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(env_server.state.requests), 1)
                self.assertEqual(env_server.state.requests[0]["authorization"], "Bearer pi-token")
                self.assertEqual(len(cli_server.state.requests), 0)

                result = self.run_cli(
                    "generate", "--base-url", cli_server.base_url, "--api-key", "cli-token",
                    "--prompt", "cli endpoint", "--no-augment",
                    "--out", str(Path(tmp) / "cli.png"),
                    env={
                        "PI_IMAGEGEN_BASE_URL": env_server.base_url,
                        "PI_IMAGEGEN_API_KEY": "pi-token",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(cli_server.state.requests), 1)
                self.assertEqual(cli_server.state.requests[0]["authorization"], "Bearer cli-token")

                result = self.run_cli(
                    "generate", "--prompt", "OPENAI fallback", "--no-augment",
                    "--out", str(Path(tmp) / "openai.png"),
                    env={
                        "OPENAI_BASE_URL": env_server.base_url,
                        "OPENAI_API_KEY": "openai-token",
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(env_server.state.requests), 2)
                self.assertEqual(env_server.state.requests[-1]["authorization"], "Bearer openai-token")
        finally:
            env_server.close()
            cli_server.close()

    def test_custom_sizes_and_new_quality_values_are_forwarded(self) -> None:
        server = LocalImageServer()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                for index, size, quality in (
                    ("custom", "2048x3072", "xhigh"),
                    ("max", "1536x1024", "max"),
                    ("auto", "auto", "high"),
                ):
                    result = self.run_cli(
                        "generate", "--base-url", server.base_url, "--prompt", index,
                        "--no-augment", "--size", size, "--quality", quality,
                        "--out", str(Path(tmp) / f"{index}.png"),
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = server.state.requests[-1]["payload"]
                    self.assertEqual(payload["size"], size)
                    self.assertEqual(payload["quality"], quality)
        finally:
            server.close()

    def test_malformed_size_is_rejected_before_request(self) -> None:
        server = LocalImageServer()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                for index, invalid_size in enumerate(("0x1024", "1024x0", "1024", "١x٢")):
                    result = self.run_cli(
                        "generate", "--base-url", server.base_url, "--prompt", "bad",
                        "--no-augment", "--size", invalid_size,
                        "--out", str(Path(tmp) / f"bad-{index}.png"),
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("positive ASCII integer", result.stderr)
                self.assertEqual(server.state.requests, [])
        finally:
            server.close()

    def test_generation_reports_backend_and_measured_metadata(self) -> None:
        server = LocalImageServer()
        raw = png_bytes(3, 2)
        encoded = b64(raw)
        server.state.generation_response = lambda payload: {
            "model": "backend-model",
            "size": "1536x1024",
            "data": [{"b64_json": encoded, "revised_prompt": "backend revised"}],
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = self.run_cli(
                    "generate", "--base-url", server.base_url,
                    "--model", "requested-alias", "--size", "999x777",
                    "--prompt", "metadata", "--no-augment",
                    "--out", str(Path(tmp) / "metadata.png"),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("requested_model=requested-alias", result.stdout)
                self.assertIn("backend_model=backend-model", result.stdout)
                self.assertIn("requested_size=999x777", result.stdout)
                self.assertIn("backend_size=1536x1024", result.stdout)
                self.assertIn("measured_size=3x2", result.stdout)
                self.assertIn("Revised prompt 1: backend revised", result.stderr)
                self.assertNotIn(encoded, result.stdout + result.stderr)
        finally:
            server.close()

    def test_reporting_snapshot_drops_image_payloads(self) -> None:
        encoded = b64(png_bytes(2, 2))
        response = {
            "model": "backend-model",
            "size": "1024x1024",
            "data": [
                {
                    "model": "item-model",
                    "size": "2x2",
                    "revised_prompt": "keep this",
                    "b64_json": encoded,
                    "url": "https://example.invalid/signed-image?token=secret",
                }
            ],
        }
        compact = SCRIPT_MODULE._compact_response_for_reporting(response)
        self.assertEqual(compact["model"], "backend-model")
        self.assertEqual(compact["data"][0]["model"], "item-model")
        self.assertEqual(compact["data"][0]["size"], "2x2")
        self.assertEqual(compact["data"][0]["revised_prompt"], "keep this")
        self.assertNotIn("b64_json", json.dumps(compact))
        self.assertNotIn("https://example.invalid", json.dumps(compact))

    def test_header_dimension_fixtures_report_jpeg_and_webp_sizes(self) -> None:
        self.assertEqual(SCRIPT_MODULE._image_dimensions(jpeg_header_bytes(257, 131)), (257, 131))
        self.assertEqual(SCRIPT_MODULE._image_dimensions(webp_vp8x_header_bytes(257, 131)), (257, 131))

    def test_missing_and_conflicting_metadata_are_reported_honestly(self) -> None:
        server = LocalImageServer()
        server.state.generation_response = lambda payload: {
            "model": "top-model",
            "size": "top-size",
            "data": [
                {"model": "item-one", "size": "item-size-one", "b64_json": b64(png_bytes(2, 3))},
                {"model": "item-two", "size": "item-size-two", "b64_json": b64(png_bytes(4, 1))},
            ],
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = self.run_cli(
                    "generate", "--base-url", server.base_url, "--prompt", "conflict",
                    "--no-augment", "--n", "2", "--out-dir", tmp,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("backend_model=item-one", result.stdout)
                self.assertIn("backend_size=item-size-one", result.stdout)
                self.assertIn("measured_size=2x3", result.stdout)
                self.assertIn("backend_model=item-two", result.stdout)
                self.assertIn("backend_size=item-size-two", result.stdout)
                self.assertIn("measured_size=4x1", result.stdout)

                server.state.generation_response = lambda payload: {
                    "c2pa": {"version": "2.0", "model": "not-a-model-field"},
                    "data": [{"b64_json": b64(b"not an image")}]
                }
                missing = self.run_cli(
                    "generate", "--base-url", server.base_url, "--model", "requested-only",
                    "--size", "8x9", "--prompt", "missing", "--no-augment",
                    "--out", str(Path(tmp) / "missing.png"),
                )
                self.assertEqual(missing.returncode, 0, missing.stderr)
                self.assertIn("backend_model=unavailable", missing.stdout)
                self.assertIn("backend_size=unavailable", missing.stdout)
                self.assertIn("measured_size=unavailable", missing.stdout)
                self.assertNotIn("not-a-model-field", missing.stdout)
        finally:
            server.close()

    def test_url_response_and_revised_prompt(self) -> None:
        server = LocalImageServer()
        try:
            url = server.asset_url("result.png")
            server.state.generation_response = lambda payload: {
                "data": [{"url": url, "revised_prompt": "url revised"}]
            }
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "url.png"
                result = self.run_cli(
                    "generate", "--base-url", server.base_url, "--prompt", "url",
                    "--no-augment", "--out", str(output),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(image_dimensions(output.read_bytes()), (3, 2))
                self.assertIn("measured_size=3x2", result.stdout)
                self.assertIn("Revised prompt 1: url revised", result.stderr)
        finally:
            server.close()

    def test_edit_uses_multipart_and_forwards_controls(self) -> None:
        server = LocalImageServer()
        response_image = png_bytes(6, 4)
        server.state.edit_response = lambda body: {
            "data": [{"b64_json": b64(response_image), "revised_prompt": "edit revised"}]
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / "input.png"
                mask = Path(tmp) / "mask.png"
                image.write_bytes(png_bytes(2, 2))
                mask.write_bytes(png_bytes(2, 2, (0, 0, 0, 0)))
                output = Path(tmp) / "edited.png"
                result = self.run_cli(
                    "edit", "--base-url", server.base_url, "--api-key", "edit-token",
                    "--model", "custom/edit-alias", "--size", "1200x800",
                    "--quality", "max", "--input-fidelity", "high",
                    "--image", str(image), "--mask", str(mask),
                    "--prompt", "edit", "--no-augment", "--out", str(output),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                request_record = server.state.requests[-1]
                body = request_record["body"]
                self.assertEqual(request_record["kind"], "edit")
                self.assertEqual(request_record["authorization"], "Bearer edit-token")
                for value in (b"custom/edit-alias", b"1200x800", b"max", b"high", image.read_bytes(), mask.read_bytes()):
                    self.assertIn(value, body)
                self.assertEqual(image_dimensions(output.read_bytes()), (6, 4))
                self.assertIn("Revised prompt 1: edit revised", result.stderr)
        finally:
            server.close()

    def test_batch_keeps_job_association_and_reports_in_job_order(self) -> None:
        server = LocalImageServer()

        def response(payload: Dict[str, Any]) -> Dict[str, Any]:
            if payload["prompt"] == "first":
                time.sleep(0.2)
                return {
                    "model": "backend-first",
                    "size": "backend-first-size",
                    "data": [{"b64_json": b64(png_bytes(2, 3))}],
                }
            return {
                "model": "backend-second",
                "size": "backend-second-size",
                "data": [{"b64_json": b64(png_bytes(4, 1))}],
            }

        server.state.generation_response = response
        try:
            with tempfile.TemporaryDirectory() as tmp:
                jobs = Path(tmp) / "jobs.jsonl"
                jobs.write_text(
                    json.dumps({"prompt": "first", "model": "job-first", "size": "11x12", "quality": "xhigh"})
                    + "\n"
                    + json.dumps({"prompt": "second", "model": "job-second", "size": "13x14", "quality": "max"})
                    + "\n",
                    encoding="utf-8",
                )
                result = self.run_cli(
                    "generate-batch", "--base-url", server.base_url, "--input", str(jobs),
                    "--out-dir", tmp, "--concurrency", "2", "--no-augment",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                stdout = result.stdout
                self.assertIn("[job 1/2] Image 1:", stdout)
                self.assertIn("[job 2/2] Image 1:", stdout)
                self.assertLess(stdout.index("[job 1/2] Image 1:"), stdout.index("[job 2/2] Image 1:"))
                self.assertIn("requested_model=job-first", stdout)
                self.assertIn("backend_model=backend-first", stdout)
                self.assertIn("measured_size=2x3", stdout)
                self.assertIn("requested_model=job-second", stdout)
                self.assertIn("backend_model=backend-second", stdout)
                self.assertIn("measured_size=4x1", stdout)
                self.assertEqual(image_dimensions((Path(tmp) / "001-first.png").read_bytes()), (2, 3))
                self.assertEqual(image_dimensions((Path(tmp) / "002-second.png").read_bytes()), (4, 1))
                payloads = [item["payload"] for item in server.state.requests]
                self.assertEqual({item["model"] for item in payloads}, {"job-first", "job-second"})
                self.assertEqual({item["size"] for item in payloads}, {"11x12", "13x14"})
                self.assertEqual({item["quality"] for item in payloads}, {"xhigh", "max"})
        finally:
            server.close()

    def test_backend_error_does_not_retry_or_fallback(self) -> None:
        server = LocalImageServer()
        server.state.generation_response = lambda payload: (
            500, {"error": {"message": "backend rejected request"}}
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "error.png"
                result = self.run_cli(
                    "generate", "--base-url", server.base_url, "--model", "requested-model",
                    "--prompt", "error", "--no-augment", "--out", str(output),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(server.state.requests), 1)
                self.assertFalse(output.exists())
                self.assertNotIn("requested-model", result.stderr)
                self.assertNotIn("b64_json", result.stderr)
        finally:
            server.close()

    def test_backend_diagnostics_keep_bounded_useful_details(self) -> None:
        server = LocalImageServer()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                server.state.generation_response = lambda payload: (
                    502,
                    b"<html><body>upstream model gpt-image-2.5-flare not found at router</body></html>",
                    "text/html",
                )
                html_error = self.run_cli(
                    "generate", "--base-url", server.base_url, "--prompt", "html", "--no-augment",
                    "--out", str(Path(tmp) / "html.png"),
                )
                self.assertNotEqual(html_error.returncode, 0)
                self.assertIn("upstream model gpt-image-2.5-flare not found at router", html_error.stderr)
                self.assertLess(len(html_error.stderr), 1000)

                server.state.generation_response = lambda payload: (
                    200,
                    b"<html>login page with session=abc</html>",
                    "text/html",
                )
                login_error = self.run_cli(
                    "generate", "--base-url", server.base_url, "--prompt", "login", "--no-augment",
                    "--out", str(Path(tmp) / "login.png"),
                )
                self.assertNotEqual(login_error.returncode, 0)
                self.assertIn("login page with session=abc", login_error.stderr)

                server.state.generation_response = lambda payload: (
                    200,
                    {"error": {"code": "E_NO_MODEL", "param": "model"}},
                )
                code_error = self.run_cli(
                    "generate", "--base-url", server.base_url, "--prompt", "code", "--no-augment",
                    "--out", str(Path(tmp) / "code.png"),
                )
                self.assertNotEqual(code_error.returncode, 0)
                self.assertIn("E_NO_MODEL", code_error.stderr)
                self.assertIn("model", code_error.stderr)
                self.assertNotIn("unknown error", code_error.stderr)

                encoded = b64(png_bytes(2, 2))
                server.state.generation_response = lambda payload: (
                    200,
                    {"error": {"code": "E_IMAGE", "b64_json": encoded}},
                )
                payload_error = self.run_cli(
                    "generate", "--base-url", server.base_url, "--prompt", "payload", "--no-augment",
                    "--out", str(Path(tmp) / "payload.png"),
                )
                self.assertNotEqual(payload_error.returncode, 0)
                self.assertNotIn(encoded, payload_error.stderr)

                server.state.generation_response = lambda payload: (
                    401,
                    {"error": {"message": "invalid token sk-live-SECRET123 for route", "type": "auth"}},
                )
                secret_error = self.run_cli(
                    "generate", "--base-url", server.base_url, "--prompt", "secret", "--no-augment",
                    "--out", str(Path(tmp) / "secret.png"),
                )
                self.assertNotEqual(secret_error.returncode, 0)
                self.assertIn("invalid token", secret_error.stderr)
                self.assertNotIn("sk-live-SECRET123", secret_error.stderr)
        finally:
            server.close()

    def test_downscaled_output_reports_measured_dimensions(self) -> None:
        server = LocalImageServer()
        raw = png_bytes(4, 2)
        server.state.generation_response = lambda payload: {"data": [{"b64_json": b64(raw)}]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "large.png"
                result = self.run_cli(
                    "generate", "--base-url", server.base_url, "--prompt", "downscale",
                    "--no-augment", "--out", str(output), "--downscale-max-dim", "2",
                )
                if "Downscaling requires Pillow" in result.stderr:
                    self.assertNotEqual(result.returncode, 0)
                    return
                self.assertEqual(result.returncode, 0, result.stderr)
                derived = Path(tmp) / "large-web.png"
                self.assertEqual(image_dimensions(output.read_bytes()), (4, 2))
                self.assertEqual(image_dimensions(derived.read_bytes()), (2, 1))
                self.assertIn("path=" + str(derived), result.stdout)
                self.assertIn("measured_size=2x1", result.stdout)
        finally:
            server.close()

    def test_existing_output_is_protected_before_request(self) -> None:
        server = LocalImageServer()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "protected.png"
                original = b"do not replace"
                output.write_bytes(original)
                result = self.run_cli(
                    "generate", "--base-url", server.base_url, "--prompt", "protected",
                    "--no-augment", "--out", str(output),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output.read_bytes(), original)
                self.assertEqual(server.state.requests, [])
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
