"""Behavior tests for scripts/image_gen.py.

Run from the repository root:  python3 -m unittest discover -s tests -v
Set IMAGEGEN_SCRIPT to test another copy of the script, for example an installed one.

The dry-run tests call the CLI as a subprocess and check the request payload it
prints. The execution tests import the script and replace only the OpenAI client
with a fake, so file writing, streaming and file uploads run for real.

Expected values come from the OpenAI Image API docs (2026-11-06):
- developers.openai.com/api/reference/resources/images/methods/generate
- developers.openai.com/api/reference/resources/images/methods/edit
- developers.openai.com/api/docs/guides/image-generation (size limits, GIF, SVG)
- developers.openai.com/api/docs/guides/image-prompting (GPT Image 2.6 parameters)
- developers.openai.com/api/docs/deprecations
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("IMAGEGEN_SCRIPT", ROOT / "scripts" / "image_gen.py"))


# Variables from the developer's shell that change CLI behavior; tests set them explicitly.
ISOLATED_ENV_VARS = ("OPENAI_API_KEY", "IMAGE_GEN_MODEL")


def _cli_env(overrides: dict | None = None) -> dict:
    env = {name: value for name, value in os.environ.items() if name not in ISOLATED_ENV_VARS}
    env.update(overrides or {})
    return env


def run_cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=_cli_env(env),
        timeout=60,
    )


def parse_json_objects(text: str) -> list:
    decoder = json.JSONDecoder()
    objects = []
    idx = 0
    text = text.strip()
    while idx < len(text):
        obj, end = decoder.raw_decode(text, idx)
        objects.append(obj)
        idx = end
        while idx < len(text) and text[idx].isspace():
            idx += 1
    return objects


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.image = self.tmp / "input.png"
        self.image.write_bytes(b"not-a-real-png")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def dry_generate(self, *extra: str, out: str = "out.png") -> dict:
        result = run_cli(
            "generate", "--prompt", "p", "--no-augment", "--dry-run",
            "--out", str(self.tmp / out), *extra,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def dry_edit(self, *extra: str) -> dict:
        result = run_cli(
            "edit", "--image", str(self.image), "--prompt", "p", "--no-augment",
            "--dry-run", "--out", str(self.tmp / "out.png"), *extra,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_rejected(self, command: str, *extra: str, message: str) -> None:
        args = [command, "--prompt", "p", "--dry-run"]
        if command == "edit":
            args += ["--image", str(self.image)]
        args += ["--out", str(self.tmp / "out.png"), *extra]
        result = run_cli(*args)
        self.assertNotEqual(result.returncode, 0, f"expected rejection for {extra}")
        self.assertIn(message, result.stderr)


class DefaultModelTests(CliTestCase):
    def test_default_model_is_gpt_image_2_6_sunburst(self) -> None:
        # User decision 2026-11-06; gpt-image-1.5 shuts down 2026-12-01.
        self.assertEqual(self.dry_generate()["model"], "gpt-image-2.6-sunburst")
        self.assertEqual(self.dry_edit()["model"], "gpt-image-2.6-sunburst")

    def test_deprecated_model_warns(self) -> None:
        result = run_cli(
            "generate", "--prompt", "p", "--dry-run", "--model", "gpt-image-1.5",
            "--out", str(self.tmp / "o.png"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.count("gpt-image-1.5 is deprecated"), 1, result.stderr)
        self.assertIn("2026-12-01", result.stderr)

    def test_non_gpt_image_model_rejected(self) -> None:
        self.assert_rejected("generate", "--model", "dall-e-3", message="GPT Image model")

    def env_generate(self, env: dict, *extra: str) -> subprocess.CompletedProcess:
        return run_cli(
            "generate", "--prompt", "p", "--dry-run", "--out", str(self.tmp / "o.png"), *extra, env=env
        )

    def test_env_var_sets_default_model(self) -> None:
        # IMAGE_GEN_MODEL is the user's standing model choice for generate and edit.
        env = {"IMAGE_GEN_MODEL": "gpt-image-2.6-flare"}
        result = self.env_generate(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["model"], "gpt-image-2.6-flare")
        result = run_cli(
            "edit", "--image", str(self.image), "--prompt", "p", "--dry-run",
            "--out", str(self.tmp / "e.png"), env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["model"], "gpt-image-2.6-flare")

    def test_model_flag_overrides_env_var(self) -> None:
        result = self.env_generate(
            {"IMAGE_GEN_MODEL": "gpt-image-2.6-flare"}, "--model", "gpt-image-2.5-sunburst"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["model"], "gpt-image-2.5-sunburst")

    def test_empty_env_var_uses_built_in_default(self) -> None:
        for value in ("", "   "):
            with self.subTest(value=value):
                result = self.env_generate({"IMAGE_GEN_MODEL": value})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["model"], "gpt-image-2.6-sunburst")

    def test_invalid_env_model_rejected(self) -> None:
        result = self.env_generate({"IMAGE_GEN_MODEL": "dall-e-3"})
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("'dall-e-3'", result.stderr)
        self.assertIn("IMAGE_GEN_MODEL", result.stderr)

    def test_env_var_with_deprecated_model_warns(self) -> None:
        result = self.env_generate({"IMAGE_GEN_MODEL": "gpt-image-1.5"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["model"], "gpt-image-1.5")
        self.assertEqual(result.stderr.count("gpt-image-1.5 is deprecated"), 1, result.stderr)


class SizeTests(CliTestCase):
    def test_custom_sizes_within_limits_accepted(self) -> None:
        # 1536x864: documented example. 3840x2160: documented maximum (8,294,400 px).
        # 1024x640: exactly the 655,360 px minimum. 2400x800: exactly 3:1.
        for size in ("1536x864", "3840x2160", "2160x3840", "1024x640", "2400x800", "auto"):
            with self.subTest(size=size):
                self.assertEqual(self.dry_generate("--size", size)["size"], size)

    def test_custom_sizes_outside_limits_rejected(self) -> None:
        cases = {
            "1030x1030": "multiples of 16",   # not divisible by 16
            "2448x800": "3:1",                # ratio 3.06
            "3856x1024": "3840",              # edge above 3840
            "1008x640": "655,360",            # 645,120 px, below minimum
            "3840x2176": "8,294,400",         # 8,355,840 px, above maximum
            "big": "WIDTHxHEIGHT",
        }
        for size, message in cases.items():
            with self.subTest(size=size):
                self.assert_rejected("generate", "--size", size, message=message)

    def test_legacy_models_keep_fixed_sizes(self) -> None:
        self.assertEqual(
            self.dry_generate("--model", "gpt-image-1.5", "--size", "1536x1024")["size"],
            "1536x1024",
        )
        self.assert_rejected(
            "generate", "--model", "gpt-image-1.5", "--size", "1536x864",
            message="1024x1024, 1536x1024, 1024x1536, or auto",
        )

    def test_default_size_is_auto(self) -> None:
        # Without --size the API picks the size; the proxy route ignored fixed defaults anyway.
        self.assertEqual(self.dry_generate()["size"], "auto")
        self.assertEqual(self.dry_edit()["size"], "auto")
        self.assertEqual(self.dry_generate("--output-format", "gif", out="loop")["size"], "auto")
        self.assertEqual(self.dry_generate("--size", "1024x1024")["size"], "1024x1024")

    def test_edit_validates_size(self) -> None:
        self.assertEqual(self.dry_edit("--size", "1536x864")["size"], "1536x864")
        self.assert_rejected("edit", "--size", "1030x1030", message="multiples of 16")


class QualityTests(CliTestCase):
    def test_extended_quality_accepted_on_2_5_and_2_6(self) -> None:
        for model in ("gpt-image-2.6-sunburst", "gpt-image-2.6-flare", "gpt-image-2.5-flare"):
            for quality in ("xhigh", "max"):
                with self.subTest(model=model, quality=quality):
                    payload = self.dry_generate("--model", model, "--quality", quality)
                    self.assertEqual(payload["quality"], quality)

    def test_extended_quality_rejected_on_older_models(self) -> None:
        for model in ("gpt-image-2", "gpt-image-2-2026-04-21", "gpt-image-1.5"):
            with self.subTest(model=model):
                self.assert_rejected(
                    "generate", "--model", model, "--quality", "xhigh",
                    message="low, medium, high, or auto",
                )

    def test_unknown_quality_rejected(self) -> None:
        self.assert_rejected("generate", "--quality", "ultra", message="quality must be one of")


class InputFidelityTests(CliTestCase):
    def test_rejected_for_current_models(self) -> None:
        for model in ("gpt-image-2.6-sunburst", "gpt-image-2"):
            with self.subTest(model=model):
                self.assert_rejected(
                    "edit", "--model", model, "--input-fidelity", "high",
                    message="Omit --input-fidelity",
                )

    def test_accepted_for_gpt_image_1_5(self) -> None:
        for value in ("low", "high"):
            with self.subTest(value=value):
                payload = self.dry_edit("--model", "gpt-image-1.5", "--input-fidelity", value)
                self.assertEqual(payload["input_fidelity"], value)

    def test_gpt_image_1_mini_supports_only_low(self) -> None:
        payload = self.dry_edit("--model", "gpt-image-1-mini", "--input-fidelity", "low")
        self.assertEqual(payload["input_fidelity"], "low")
        self.assert_rejected(
            "edit", "--model", "gpt-image-1-mini", "--input-fidelity", "high",
            message="supports only low",
        )

    def test_omitted_when_not_set(self) -> None:
        self.assertNotIn("input_fidelity", self.dry_edit())


class ComplexityTests(CliTestCase):
    def test_complexity_passed_through(self) -> None:
        self.assertEqual(self.dry_generate("--complexity", "low")["complexity"], "low")
        self.assertEqual(self.dry_edit("--complexity", "medium")["complexity"], "medium")
        self.assertNotIn("complexity", self.dry_generate())

    def test_invalid_complexity_rejected(self) -> None:
        self.assert_rejected("generate", "--complexity", "max", message="complexity must be one of")


class GifTests(CliTestCase):
    def test_gif_payload_and_auto_size(self) -> None:
        payload = self.dry_generate(
            "--output-format", "gif", "--frame-count", "12", "--frame-duration-ms", "100",
            out="loop",
        )
        self.assertEqual(payload["output_format"], "gif")
        self.assertEqual(payload["frame_count"], 12)
        self.assertEqual(payload["frame_duration_ms"], 100)
        # GIF frames share a per-request pixel budget; the CLI lets the API pick the size.
        self.assertEqual(payload["size"], "auto")
        self.assertTrue(payload["outputs"][0].endswith(".gif"))

    def test_gif_small_custom_frame_size_accepted(self) -> None:
        # The guide's GIF example uses 512x512, below the still-image pixel minimum.
        self.assertEqual(
            self.dry_generate("--output-format", "gif", "--size", "512x512")["size"], "512x512"
        )
        self.assert_rejected(
            "generate", "--output-format", "gif", "--size", "500x500", message="multiples of 16"
        )

    def test_gif_on_edit(self) -> None:
        self.assertEqual(self.dry_edit("--output-format", "gif")["output_format"], "gif")

    def test_gif_limits(self) -> None:
        for flag, value, message in (
            ("--frame-count", "1", "frame-count must be between 2 and 16"),
            ("--frame-count", "17", "frame-count must be between 2 and 16"),
            ("--frame-duration-ms", "19", "frame-duration-ms must be between 20 and 2000"),
            ("--frame-duration-ms", "2001", "frame-duration-ms must be between 20 and 2000"),
        ):
            with self.subTest(flag=flag, value=value):
                self.assert_rejected(
                    "generate", "--output-format", "gif", flag, value, message=message
                )
        for flag, value in (("--frame-count", "2"), ("--frame-count", "16"),
                            ("--frame-duration-ms", "20"), ("--frame-duration-ms", "2000")):
            with self.subTest(boundary=flag, value=value):
                self.dry_generate("--output-format", "gif", flag, value)

    def test_gif_rejects_incompatible_options(self) -> None:
        self.assert_rejected(
            "generate", "--frame-count", "4", message="require --output-format gif"
        )
        self.assert_rejected(
            "generate", "--output-format", "gif", "--output-compression", "50",
            message="output-compression",
        )
        self.assert_rejected(
            "generate", "--output-format", "gif", "--downscale-max-dim", "256",
            message="downscale",
        )
        mask = self.tmp / "mask.png"
        mask.write_bytes(b"m")
        self.assert_rejected(
            "edit", "--output-format", "gif", "--mask", str(mask), message="mask"
        )


class SvgTests(CliTestCase):
    def test_svg_payload(self) -> None:
        payload = self.dry_generate(
            "--output-format", "svg", "--background", "transparent",
            "--svg-model", "author-model", "--svg-reasoning-effort", "high",
            out="logo",
        )
        self.assertEqual(payload["output_format"], "svg")
        self.assertEqual(payload["svg"], {"model": "author-model", "reasoning": {"effort": "high"}})
        self.assertTrue(payload["outputs"][0].endswith(".svg"))

    def test_svg_model_without_effort(self) -> None:
        payload = self.dry_generate("--output-format", "svg", "--svg-model", "author-model")
        self.assertEqual(payload["svg"], {"model": "author-model"})
        self.assertNotIn("svg", self.dry_generate("--output-format", "svg"))

    def test_svg_rules(self) -> None:
        self.assert_rejected("generate", "--output-format", "svg", "--n", "2", message="n=1")
        self.assert_rejected(
            "generate", "--output-format", "svg", "--output-compression", "50",
            message="output-compression",
        )
        self.assert_rejected("edit", "--output-format", "svg", message="edit endpoint does not support svg")
        self.assert_rejected(
            "generate", "--svg-model", "author-model", message="require --output-format svg"
        )
        self.assert_rejected(
            "generate", "--output-format", "svg", "--svg-reasoning-effort", "high",
            message="--svg-reasoning-effort requires --svg-model",
        )
        self.assert_rejected(
            "generate", "--output-format", "svg", "--svg-model", "m",
            "--svg-reasoning-effort", "huge", message="svg-reasoning-effort must be one of",
        )
        self.assert_rejected(
            "generate", "--output-format", "svg", "--downscale-max-dim", "256", message="downscale",
        )

    def test_transparency_formats(self) -> None:
        self.assert_rejected(
            "generate", "--background", "transparent", "--output-format", "jpeg",
            message="png, webp, or svg",
        )


class MarkupTests(CliTestCase):
    def test_markup_in_edit_payload(self) -> None:
        markup = self.tmp / "markup.png"
        markup.write_bytes(b"m")
        self.assertEqual(self.dry_edit("--markup", str(markup))["markup"], str(markup))

    def test_markup_rules(self) -> None:
        markup = self.tmp / "markup.png"
        markup.write_bytes(b"m")
        mask = self.tmp / "mask.png"
        mask.write_bytes(b"m")
        self.assert_rejected(
            "edit", "--markup", str(markup), "--mask", str(mask), message="cannot be combined"
        )
        self.assert_rejected("edit", "--markup", str(self.tmp / "missing.png"), message="not found")


class StreamOptionTests(CliTestCase):
    def test_stream_payload(self) -> None:
        payload = self.dry_generate("--stream", "--partial-images", "2")
        self.assertIs(payload["stream"], True)
        self.assertEqual(payload["partial_images"], 2)
        self.assertNotIn("stream", self.dry_generate())

    def test_stream_rules(self) -> None:
        self.assert_rejected(
            "generate", "--partial-images", "1", message="--partial-images requires --stream"
        )
        self.assert_rejected(
            "generate", "--stream", "--partial-images", "4", message="between 0 and 3"
        )
        self.assert_rejected("generate", "--stream", "--n", "2", message="--stream supports only --n 1")
        for fmt in ("gif", "svg"):
            with self.subTest(fmt=fmt):
                self.assert_rejected(
                    "generate", "--stream", "--output-format", fmt, message="does not support streaming"
                )


class AlphaSnapTests(CliTestCase):
    def test_alpha_snap_is_local_and_needs_png_or_webp(self) -> None:
        payload = self.dry_generate("--background", "transparent", "--alpha-snap")
        self.assertNotIn("alpha_snap", payload)  # a local post-process, not an API parameter
        self.dry_generate("--output-format", "webp", "--alpha-snap")
        for fmt in ("jpeg", "gif", "svg"):
            with self.subTest(fmt=fmt):
                self.assert_rejected(
                    "generate", "--output-format", fmt, "--alpha-snap",
                    message="--alpha-snap works only with png or webp output",
                )


class SafetyIdentifierTests(CliTestCase):
    def test_passed_through_and_length_limited(self) -> None:
        self.assertEqual(self.dry_generate("--safety-identifier", "u1")["safety_identifier"], "u1")
        self.dry_generate("--safety-identifier", "x" * 128)
        self.assert_rejected(
            "generate", "--safety-identifier", "x" * 129, message="at most 128"
        )


class BatchTests(CliTestCase):
    def run_batch(self, *lines: str, env: dict | None = None) -> subprocess.CompletedProcess:
        jobs = self.tmp / "jobs.jsonl"
        jobs.write_text("\n".join(lines), encoding="utf-8")
        return run_cli(
            "generate-batch", "--input", str(jobs), "--out-dir", str(self.tmp / "batch"),
            "--dry-run", "--no-augment", env=env,
        )

    def test_env_model_is_batch_default_and_jobs_override_it(self) -> None:
        result = self.run_batch(
            json.dumps({"prompt": "a"}),
            json.dumps({"prompt": "b", "model": "gpt-image-2.5-flare"}),
            env={"IMAGE_GEN_MODEL": "gpt-image-2.6-flare"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        first, second = parse_json_objects(result.stdout)
        self.assertEqual(first["model"], "gpt-image-2.6-flare")
        self.assertEqual(second["model"], "gpt-image-2.5-flare")

    def test_per_job_new_options(self) -> None:
        result = self.run_batch(
            json.dumps({"prompt": "a", "size": "1536x864", "quality": "max", "complexity": "low"}),
            json.dumps({"prompt": "b", "output_format": "gif", "frame_count": 4}),
            json.dumps({"prompt": "c", "output_format": "svg"}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        first, second, third = parse_json_objects(result.stdout)
        self.assertEqual(first["model"], "gpt-image-2.6-sunburst")
        self.assertEqual((first["size"], first["quality"], first["complexity"]), ("1536x864", "max", "low"))
        self.assertEqual((second["output_format"], second["frame_count"], second["size"]), ("gif", 4, "auto"))
        self.assertTrue(second["outputs"][0].endswith(".gif"))
        # No size given: the CLI default is auto (user decision 2026-11-06; OpenAI's upstream skill does the same).
        self.assertEqual(third["size"], "auto")
        self.assertTrue(third["outputs"][0].endswith(".svg"))

    def test_per_job_gif_options_must_be_integers(self) -> None:
        # API reference: frame_count and frame_duration_ms are integers. JSONL can carry
        # floats, strings, or booleans; truncating them would hide out-of-range values.
        for job, message in (
            ({"frame_count": 16.9}, "frame-count must be an integer"),
            ({"frame_duration_ms": 2000.9}, "frame-duration-ms must be an integer"),
            ({"frame_count": True}, "frame-count must be an integer"),
            ({"frame_count": "4"}, "frame-count must be an integer"),
        ):
            with self.subTest(job=job):
                result = self.run_batch(json.dumps({"prompt": "a", "output_format": "gif", **job}))
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(message, result.stderr)
        result = self.run_batch(
            json.dumps({"prompt": "a", "output_format": "gif", "frame_count": 16, "frame_duration_ms": 2000}),
            json.dumps({"prompt": "b", "output_format": "gif", "frame_count": 2, "frame_duration_ms": 20}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_per_job_counts_must_be_integers(self) -> None:
        # API reference: n and output_compression are integers. Fractions used to be
        # truncated for the check and sent unchanged; non-numeric values crashed.
        for job, message in (
            ({"n": 1.5}, "n must be an integer"),
            ({"n": "two"}, "n must be an integer"),
            ({"n": True}, "n must be an integer"),
            ({"output_format": "jpeg", "output_compression": 50.5}, "output-compression must be an integer"),
            ({"output_format": "jpeg", "output_compression": "high"}, "output-compression must be an integer"),
            ({"n": 11}, "n must be between 1 and 10"),
            ({"output_format": "jpeg", "output_compression": 101}, "output-compression must be between 0 and 100"),
        ):
            with self.subTest(job=job):
                result = self.run_batch(json.dumps({"prompt": "a", **job}))
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        result = self.run_batch(
            json.dumps({"prompt": "a", "n": 10, "output_format": "jpeg", "output_compression": 0}),
            json.dumps({"prompt": "b", "output_format": "webp", "output_compression": 100}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_per_job_deprecated_model_warns_once(self) -> None:
        result = self.run_batch(
            json.dumps({"prompt": "a", "model": "gpt-image-1.5"}),
            json.dumps({"prompt": "b", "model": "gpt-image-1.5"}),
            json.dumps({"prompt": "c"}),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        first, second, third = parse_json_objects(result.stdout)
        self.assertEqual((first["model"], second["model"]), ("gpt-image-1.5", "gpt-image-1.5"))
        self.assertEqual(third["model"], "gpt-image-2.6-sunburst")
        self.assertEqual(result.stderr.count("gpt-image-1.5 is deprecated"), 1, result.stderr)
        self.assertIn("2026-12-01", result.stderr)

    def test_output_folders_are_created_only_when_files_are_written(self) -> None:
        # A rejected batch or a dry run must not leave empty output folders behind.
        result = self.run_batch(json.dumps({"prompt": "ok"}), json.dumps({"prompt": "bad", "n": "two"}))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.tmp / "batch").exists())

        result = self.run_batch(json.dumps({"prompt": "ok"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.tmp / "batch").exists())

        result = run_cli("generate", "--prompt", "p", "--dry-run", "--out-dir", str(self.tmp / "one-off"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.tmp / "one-off").exists())

    def test_per_job_invalid_option_rejected(self) -> None:
        result = self.run_batch(json.dumps({"prompt": "a", "output_format": "svg", "n": 2}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("n=1", result.stderr)
        result = self.run_batch(json.dumps({"prompt": "a", "size": "1030x1030"}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("multiples of 16", result.stderr)


# ---------------------------------------------------------------------------
# Execution tests with a fake OpenAI client.


def _load_module():
    spec = importlib.util.spec_from_file_location("image_gen_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class FakeImages:
    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list = []

    def _respond(self, endpoint: str, kwargs: dict):
        record = dict(kwargs)
        for key in ("image", "mask", "markup"):
            value = record.get(key)
            if isinstance(value, list):
                record[key] = [Path(handle.name).name for handle in value]
            elif value is not None:
                record[key] = Path(value.name).name
        self.calls.append((endpoint, record))
        response = self.responses[endpoint]
        if isinstance(response, BaseException):
            raise response
        return response

    def generate(self, **kwargs):
        return self._respond("generate", kwargs)

    def edit(self, **kwargs):
        return self._respond("edit", kwargs)


class FakeAsyncImages:
    """Async stand-in for client.images in generate-batch; answers by prompt text."""

    def __init__(self, responses_by_prompt: dict) -> None:
        self.responses_by_prompt = responses_by_prompt
        self.calls: list = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        # Yield to the event loop like a network request, so other jobs can run meanwhile.
        await asyncio.sleep(0)
        response = self.responses_by_prompt[kwargs["prompt"]]
        if isinstance(response, BaseException):
            raise response
        return response


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"rest-of-png"
# Alpha values of a 6x1 test image: transparent, partly transparent, and nearly opaque pixels.
ALPHAS = [0, 100, 249, 250, 254, 255]


def _rgba_png(alphas: list, fmt: str = "PNG", **save_args) -> bytes:
    from PIL import Image

    img = Image.new("RGBA", (len(alphas), 1))
    img.putdata([(120, 60, 20, a) for a in alphas])
    buffer = io.BytesIO()
    img.save(buffer, format=fmt, **save_args)
    return buffer.getvalue()


def _alphas(data: bytes) -> list:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return list(img.convert("RGBA").getchannel("A").getdata())
PROXY_URL = "http://localhost:8080/v1/images/generations"


def _fake_openai_module() -> types.ModuleType:
    """Stand-in for the openai package's error classes (openai-python 3.47.0, _exceptions.py).

    APIStatusError takes the HTTP response and the parsed body; APIConnectionError has a
    request but no response. The CLI only uses these attributes.
    """
    module = types.ModuleType("openai")

    class OpenAIError(Exception):
        pass

    class APIError(OpenAIError):
        def __init__(self, message, request, *, body):
            super().__init__(message)
            self.message = message
            self.request = request
            self.body = body

    class APIStatusError(APIError):
        def __init__(self, message, *, response, body):
            super().__init__(message, response.request, body=body)
            self.response = response
            self.status_code = response.status_code
            self.request_id = response.headers.get("x-request-id")

    class BadRequestError(APIStatusError):
        pass

    class APIConnectionError(APIError):
        def __init__(self, *, message="Connection error.", request):
            super().__init__(message, request, body=None)

    for cls in (OpenAIError, APIError, APIStatusError, BadRequestError, APIConnectionError):
        setattr(module, cls.__name__, cls)
    return module


def _status_error(openai_module, status: int, body, message: str, request_id=None):
    request = SimpleNamespace(method="POST", url=PROXY_URL)
    headers = {"x-request-id": request_id} if request_id else {}
    response = SimpleNamespace(status_code=status, request=request, headers=headers)
    return openai_module.BadRequestError(message, response=response, body=body)


def _usage(input_tokens: int, output_tokens: int) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.module = _load_module()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_main(self, responses: dict, *argv: str, async_client: object = None) -> FakeImages:
        """Run the CLI in-process with fake clients; stderr is kept in self.stderr."""
        images = FakeImages(responses)
        self.fake_images = images  # inspectable after main() exits through SystemExit
        client = SimpleNamespace(images=images)
        stderr = io.StringIO()
        try:
            with mock.patch.object(self.module, "_create_client", return_value=client), \
                    mock.patch.object(self.module, "_create_async_client", return_value=async_client), \
                    mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
                    mock.patch.object(sys, "argv", ["image_gen.py", *argv]), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(stderr):
                # patch.dict restores the environment afterwards, including this removal.
                os.environ.pop("IMAGE_GEN_MODEL", None)
                self.assertEqual(self.module.main(), 0)
        finally:
            self.stderr = stderr.getvalue()
        return images

    def test_generate_stream_writes_partials_and_final(self) -> None:
        events = [
            SimpleNamespace(type="image_generation.partial_image", partial_image_index=0, b64_json=_b64(b"p0")),
            SimpleNamespace(type="image_generation.partial_image", partial_image_index=1, b64_json=_b64(b"p1")),
            SimpleNamespace(
                type="image_generation.completed", b64_json=_b64(b"final"), size="1024x1024",
                quality="medium", background="opaque", output_format="png", usage=_usage(1, 2),
            ),
        ]
        out = self.tmp / "river.png"
        images = self.run_main(
            {"generate": iter(events)},
            "generate", "--prompt", "river", "--no-augment", "--stream", "--partial-images", "2",
            "--out", str(out),
        )
        self.assertEqual(out.read_bytes(), b"final")
        self.assertEqual((self.tmp / "river-partial-0.png").read_bytes(), b"p0")
        self.assertEqual((self.tmp / "river-partial-1.png").read_bytes(), b"p1")
        _, call = images.calls[0]
        self.assertIs(call["stream"], True)
        self.assertEqual(call["partial_images"], 2)
        # The completed event reports the settings that ran; auto values resolve here.
        self.assertIn(
            "Result: size=1024x1024; quality=medium; background=opaque; output_format=png; "
            "tokens input=1 output=2 total=3",
            self.stderr,
        )

    def test_stream_without_completed_event_fails(self) -> None:
        events = [SimpleNamespace(type="image_generation.partial_image", partial_image_index=0, b64_json=_b64(b"p0"))]
        with self.assertRaises(SystemExit) as ctx:
            self.run_main(
                {"generate": iter(events)},
                "generate", "--prompt", "x", "--stream", "--out", str(self.tmp / "x.png"),
            )
        self.assertNotEqual(ctx.exception.code, 0)

    def test_edit_stream_and_markup_upload(self) -> None:
        image = self.tmp / "kitchen.png"
        image.write_bytes(b"img")
        markup = self.tmp / "circle.png"
        markup.write_bytes(b"overlay")
        events = [
            SimpleNamespace(type="image_edit.partial_image", partial_image_index=0, b64_json=_b64(b"ep0")),
            SimpleNamespace(
                type="image_edit.completed", b64_json=_b64(b"edited"), size="1024x1024",
                quality="high", background="opaque", output_format="png", usage=_usage(3, 4),
            ),
        ]
        out = self.tmp / "edit.png"
        images = self.run_main(
            {"edit": iter(events)},
            "edit", "--image", str(image), "--markup", str(markup), "--prompt", "recolor",
            "--stream", "--partial-images", "1", "--out", str(out),
        )
        self.assertEqual(out.read_bytes(), b"edited")
        self.assertEqual((self.tmp / "edit-partial-0.png").read_bytes(), b"ep0")
        _, call = images.calls[0]
        self.assertEqual(call["image"], "kitchen.png")
        self.assertEqual(call["markup"], "circle.png")
        self.assertNotIn("mask", call)
        self.assertIn(
            "Result: size=1024x1024; quality=high; background=opaque; output_format=png; "
            "tokens input=3 output=4 total=7",
            self.stderr,
        )

    def test_svg_and_gif_outputs_written_as_returned(self) -> None:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
        response = SimpleNamespace(
            data=[SimpleNamespace(b64_json=_b64(svg))], size="1024x1024", quality="high",
            background="transparent", output_format="svg", usage=_usage(10, 20),
            svg_usage=SimpleNamespace(model="m", input_tokens=5, output_tokens=7, total_tokens=12),
        )
        out = self.tmp / "logo"
        images = self.run_main(
            {"generate": response},
            "generate", "--prompt", "logo", "--output-format", "svg", "--svg-model", "m", "--out", str(out),
        )
        self.assertEqual((self.tmp / "logo.svg").read_bytes(), svg)
        _, call = images.calls[0]
        self.assertEqual(call["svg"], {"model": "m"})
        self.assertIn(
            "Result: size=1024x1024; quality=high; background=transparent; output_format=svg; "
            "tokens input=10 output=20 total=30; svg author m tokens input=5 output=7 total=12",
            self.stderr,
        )

        gif = b"GIF89a-fake"
        response = SimpleNamespace(data=[SimpleNamespace(b64_json=_b64(gif))])
        images = self.run_main(
            {"generate": response},
            "generate", "--prompt", "loop", "--output-format", "gif", "--frame-count", "4",
            "--out", str(self.tmp / "loop"),
        )
        self.assertEqual((self.tmp / "loop.gif").read_bytes(), gif)
        self.assertEqual(images.calls[0][1]["frame_count"], 4)
        # The response fields are optional in the API; a bare response prints no result line.
        self.assertNotIn("Result:", self.stderr)

    def test_batch_writes_outputs_and_reports_metadata(self) -> None:
        jobs = self.tmp / "jobs.jsonl"
        jobs.write_text(
            "\n".join([
                json.dumps({"prompt": "first", "out": "first.png", "size": "1024x1024"}),
                json.dumps({"prompt": "second", "out": "second.png", "quality": "low"}),
            ]),
            encoding="utf-8",
        )
        responses = {
            "first": SimpleNamespace(
                data=[SimpleNamespace(b64_json=_b64(b"one"))], size="1536x1024", quality="high",
                usage=_usage(10, 100),
            ),
            "second": SimpleNamespace(
                data=[SimpleNamespace(b64_json=_b64(b"two"))], size="1024x1024", quality="low",
                usage=_usage(10, 20),
            ),
        }
        async_images = FakeAsyncImages(responses)
        self.run_main(
            {}, "generate-batch", "--input", str(jobs), "--out-dir", str(self.tmp / "batch"),
            "--no-augment", async_client=SimpleNamespace(images=async_images),
        )
        self.assertEqual((self.tmp / "batch" / "first.png").read_bytes(), b"one")
        self.assertEqual((self.tmp / "batch" / "second.png").read_bytes(), b"two")
        self.assertEqual(sorted(call["prompt"] for call in async_images.calls), ["first", "second"])
        self.assertIn("[job 1/2] result: size=1536x1024; quality=high; tokens input=10 output=100 total=110", self.stderr)
        self.assertIn("[job 2/2] result: size=1024x1024; quality=low; tokens input=10 output=20 total=30", self.stderr)
        # Job 1 asked for the default 1024x1024 and got 1536x1024; job 2 got what it asked for.
        self.assertIn(
            "Warning: [job 1/2] result differs from the request: size requested 1024x1024, got 1536x1024.",
            self.stderr,
        )
        self.assertNotIn("[job 2/2] result differs", self.stderr)

    # -- API errors: one clean line, no traceback (source: live runs through an OpenAI-compatible proxy, 2026-11-06).

    def run_with_fake_openai(self, responses: dict, *argv: str, async_client: object = None):
        fake_openai = _fake_openai_module()
        with mock.patch.dict(sys.modules, {"openai": fake_openai}):
            with self.assertRaises(SystemExit) as ctx:
                self.run_main(responses, *argv, async_client=async_client)
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertNotIn("Traceback", self.stderr)
        return fake_openai

    def test_api_status_error_prints_one_error_line(self) -> None:
        fake = _fake_openai_module()
        error = _status_error(
            fake, 400, {"error": {"message": "Model gpt-image-9 is not supported"}},
            "Error code: 400 - Model gpt-image-9 is not supported", request_id="req_123",
        )
        with mock.patch.dict(sys.modules, {"openai": fake}):
            with self.assertRaises(SystemExit):
                self.run_main({"generate": error}, "generate", "--prompt", "x", "--out", str(self.tmp / "x.png"))
        self.assertIn(
            f"Error: Image generation failed with HTTP 400 (POST {PROXY_URL}): Model gpt-image-9 is not supported. "
            "Request id: req_123.",
            self.stderr,
        )

    def test_api_error_shows_proxy_detail_field(self) -> None:
        # A proxy answered a GIF request with {"detail": "..."}; the SDK message omits it.
        fake = _fake_openai_module()
        error = _status_error(fake, 400, {"detail": "GIF output is not enabled for this request."}, "Error code: 400")
        with mock.patch.dict(sys.modules, {"openai": fake}):
            with self.assertRaises(SystemExit):
                self.run_main(
                    {"generate": error}, "generate", "--prompt", "x", "--output-format", "gif",
                    "--out", str(self.tmp / "x"),
                )
        self.assertIn("HTTP 400", self.stderr)
        self.assertIn("GIF output is not enabled for this request.", self.stderr)
        self.assertNotIn("Traceback", self.stderr)

    def test_api_error_without_message_gives_hint(self) -> None:
        for body in (None, "", {}):
            with self.subTest(body=body):
                fake = _fake_openai_module()
                error = _status_error(fake, 400, body, "Error code: 400")
                with mock.patch.dict(sys.modules, {"openai": fake}):
                    with self.assertRaises(SystemExit):
                        self.run_main({"generate": error}, "generate", "--prompt", "x", "--out", str(self.tmp / "x.png"))
                self.assertIn("the server sent no error message", self.stderr)
                self.assertIn("OPENAI_BASE_URL", self.stderr)

    def test_connection_error_names_the_url(self) -> None:
        fake = _fake_openai_module()
        error = fake.APIConnectionError(request=SimpleNamespace(method="POST", url=PROXY_URL))
        with mock.patch.dict(sys.modules, {"openai": fake}):
            with self.assertRaises(SystemExit):
                self.run_main({"generate": error}, "generate", "--prompt", "x", "--out", str(self.tmp / "x.png"))
        self.assertIn(f"Error: Image generation failed: could not reach POST {PROXY_URL} (Connection error.).", self.stderr)
        self.assertIn("OPENAI_BASE_URL", self.stderr)

    def test_edit_api_error_prints_one_error_line(self) -> None:
        image = self.tmp / "in.png"
        image.write_bytes(PNG_BYTES)
        fake = _fake_openai_module()
        error = _status_error(fake, 400, {"error": {"message": "bad edit"}}, "Error code: 400 - bad edit")
        with mock.patch.dict(sys.modules, {"openai": fake}):
            with self.assertRaises(SystemExit):
                self.run_main(
                    {"edit": error}, "edit", "--image", str(image), "--prompt", "x", "--out", str(self.tmp / "e.png"),
                )
        self.assertIn(f"Error: Image edit failed with HTTP 400 (POST {PROXY_URL}): bad edit.", self.stderr)
        self.assertNotIn("Traceback", self.stderr)

    def test_batch_job_api_error_is_one_line_and_other_jobs_finish(self) -> None:
        fake = _fake_openai_module()
        jobs = self.tmp / "jobs.jsonl"
        jobs.write_text(
            "\n".join([json.dumps({"prompt": "bad", "out": "bad.png"}), json.dumps({"prompt": "good", "out": "good.png"})]),
            encoding="utf-8",
        )
        error = _status_error(fake, 400, {"detail": "GIF output is not enabled for this request."}, "Error code: 400")
        good = SimpleNamespace(data=[SimpleNamespace(b64_json=_b64(PNG_BYTES))])
        async_images = FakeAsyncImages({"bad": error, "good": good})
        with mock.patch.dict(sys.modules, {"openai": fake}):
            with self.assertRaises(SystemExit) as ctx:
                self.run_main(
                    {}, "generate-batch", "--input", str(jobs), "--out-dir", str(self.tmp / "batch"),
                    "--no-augment", async_client=SimpleNamespace(images=async_images),
                )
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(
            f"[job 1/2] failed: Image generation failed with HTTP 400 (POST {PROXY_URL}): "
            "GIF output is not enabled for this request.",
            self.stderr,
        )
        self.assertEqual((self.tmp / "batch" / "good.png").read_bytes(), PNG_BYTES)

    def test_error_line_hides_url_credentials(self) -> None:
        # httpx renders user info and query values in URLs; the error line must not.
        secret_url = "http://review-user:review-password@127.0.0.1:8080/v1/images/generations?api_key=review-key"
        request = SimpleNamespace(method="POST", url=secret_url)
        fake = _fake_openai_module()
        status = fake.BadRequestError(
            "Error code: 400", response=SimpleNamespace(status_code=400, request=request, headers={}), body={"detail": "no"}
        )
        connection = fake.APIConnectionError(request=request)
        for error in (status, connection):
            with self.subTest(error=type(error).__name__):
                with mock.patch.dict(sys.modules, {"openai": fake}):
                    with self.assertRaises(SystemExit):
                        self.run_main({"generate": error}, "generate", "--prompt", "x", "--out", str(self.tmp / "x.png"))
                self.assertIn("POST http://127.0.0.1:8080/v1/images/generations", self.stderr)
                for secret in ("review-user", "review-password", "review-key", "api_key"):
                    self.assertNotIn(secret, self.stderr)

    def test_multiline_server_text_stays_on_one_error_line(self) -> None:
        fake = _fake_openai_module()
        error = _status_error(fake, 502, {"detail": "Backend failed\nPlease retry."}, "Error code: 502")
        with mock.patch.dict(sys.modules, {"openai": fake}):
            with self.assertRaises(SystemExit):
                self.run_main({"generate": error}, "generate", "--prompt", "x", "--out", str(self.tmp / "x.png"))
        error_lines = [line for line in self.stderr.splitlines() if line.startswith("Error:")]
        self.assertEqual(len(error_lines), 1, self.stderr)
        self.assertIn("Backend failed Please retry.", error_lines[0])
        self.assertNotIn("\nPlease retry.", self.stderr)

        jobs = self.tmp / "jobs.jsonl"
        jobs.write_text(json.dumps({"prompt": "bad"}), encoding="utf-8")
        async_images = FakeAsyncImages({"bad": error})
        with mock.patch.dict(sys.modules, {"openai": fake}):
            with self.assertRaises(SystemExit):
                self.run_main(
                    {}, "generate-batch", "--input", str(jobs), "--out-dir", str(self.tmp / "batch"),
                    "--no-augment", async_client=SimpleNamespace(images=async_images),
                )
        failed_lines = [line for line in self.stderr.splitlines() if "failed:" in line]
        self.assertEqual(len(failed_lines), 1, self.stderr)
        self.assertIn("Backend failed Please retry.", failed_lines[0])

    def test_batch_fail_fast_stops_without_traceback(self) -> None:
        fake = _fake_openai_module()
        jobs = self.tmp / "jobs.jsonl"
        jobs.write_text(json.dumps({"prompt": "bad"}), encoding="utf-8")
        error = _status_error(fake, 400, {"error": {"message": "bad request"}}, "Error code: 400 - bad request")
        async_images = FakeAsyncImages({"bad": error})
        with mock.patch.dict(sys.modules, {"openai": fake}):
            with self.assertRaises(SystemExit) as ctx:
                self.run_main(
                    {}, "generate-batch", "--input", str(jobs), "--out-dir", str(self.tmp / "batch"),
                    "--no-augment", "--fail-fast", async_client=SimpleNamespace(images=async_images),
                )
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("[job 1/1] failed: Image generation failed with HTTP 400", self.stderr)
        self.assertIn("Error: Batch stopped at the first failed job (--fail-fast).", self.stderr)
        self.assertNotIn("Traceback", self.stderr)

    def test_batch_fail_fast_keeps_non_api_errors(self) -> None:
        jobs = self.tmp / "jobs.jsonl"
        jobs.write_text(json.dumps({"prompt": "bug"}), encoding="utf-8")
        async_images = FakeAsyncImages({"bug": RuntimeError("bug")})
        with mock.patch.dict(sys.modules, {"openai": _fake_openai_module()}):
            with self.assertRaises(RuntimeError):
                self.run_main(
                    {}, "generate-batch", "--input", str(jobs), "--out-dir", str(self.tmp / "batch"),
                    "--no-augment", "--fail-fast", async_client=SimpleNamespace(images=async_images),
                )

    def test_non_api_errors_still_raise(self) -> None:
        # Bugs keep their traceback: only OpenAI SDK errors become an Error line.
        with mock.patch.dict(sys.modules, {"openai": _fake_openai_module()}):
            with self.assertRaises(RuntimeError):
                self.run_main({"generate": RuntimeError("bug")}, "generate", "--prompt", "x", "--out", str(self.tmp / "x.png"))

    # -- Settings the API ignored (source: live runs through an OpenAI-compatible proxy, 2026-11-06).

    def test_warns_when_api_ignores_requested_settings(self) -> None:
        response = SimpleNamespace(
            data=[SimpleNamespace(b64_json=_b64(PNG_BYTES))], size="1672x941", quality="low", background="opaque",
            output_format="png",
        )
        self.run_main(
            {"generate": response}, "generate", "--prompt", "x", "--size", "1024x1024", "--quality", "high",
            "--background", "transparent", "--n", "2", "--out", str(self.tmp / "x.png"),
        )
        self.assertIn(
            "Warning: Result differs from the request: size requested 1024x1024, got 1672x941; "
            "quality requested high, got low; background requested transparent, got opaque; "
            "images requested 2, got 1.",
            self.stderr,
        )
        self.assertEqual((self.tmp / "x-1.png").read_bytes(), PNG_BYTES)

    def test_no_difference_warning_for_auto_or_matching_settings(self) -> None:
        response = SimpleNamespace(
            data=[SimpleNamespace(b64_json=_b64(PNG_BYTES))], size="1536x1024", quality="low", background="opaque",
        )
        for size in ("auto", "1536x1024"):
            with self.subTest(size=size):
                self.run_main(
                    {"generate": response}, "generate", "--prompt", "x", "--size", size, "--force",
                    "--out", str(self.tmp / "x.png"),
                )
                self.assertNotIn("differs from the request", self.stderr)

    def test_out_dir_is_created_when_the_image_is_written(self) -> None:
        response = SimpleNamespace(data=[SimpleNamespace(b64_json=_b64(PNG_BYTES))])
        out_dir = self.tmp / "new" / "folder"
        self.run_main({"generate": response}, "generate", "--prompt", "x", "--out-dir", str(out_dir))
        self.assertEqual((out_dir / "image_1.png").read_bytes(), PNG_BYTES)

    def test_format_mismatch_is_saved_under_the_real_extension(self) -> None:
        response = SimpleNamespace(data=[SimpleNamespace(b64_json=_b64(PNG_BYTES))], output_format="png")
        for fmt, name in (("jpeg", "photo.jpeg"), ("svg", "logo.svg"), ("webp", "pic.webp")):
            with self.subTest(fmt=fmt):
                requested = self.tmp / name
                actual = requested.with_suffix(".png")
                self.run_main({"generate": response}, "generate", "--prompt", "x", "--output-format", fmt, "--out", str(requested))
                self.assertFalse(requested.exists())
                self.assertEqual(actual.read_bytes(), PNG_BYTES)
                self.assertIn(
                    f"Warning: requested {fmt} output, but the API returned png data; wrote {actual}.", self.stderr
                )

    def test_format_warning_when_path_already_has_the_real_extension(self) -> None:
        # Default output path is output.png; a jpeg request answered with PNG must still warn.
        response = SimpleNamespace(data=[SimpleNamespace(b64_json=_b64(PNG_BYTES))])
        out = self.tmp / "already.png"
        self.run_main({"generate": response}, "generate", "--prompt", "x", "--output-format", "jpeg", "--out", str(out))
        self.assertEqual(out.read_bytes(), PNG_BYTES)
        self.assertIn(f"Warning: requested jpeg output, but the API returned png data; wrote {out}.", self.stderr)

    def test_alpha_snap_makes_nearly_opaque_pixels_opaque(self) -> None:
        # Observed 2026-11-06 through an OpenAI-compatible proxy: solid areas of transparent logos at alpha 252-254.
        response = SimpleNamespace(data=[SimpleNamespace(b64_json=_b64(_rgba_png(ALPHAS)))], background="transparent")
        out = self.tmp / "logo.png"
        self.run_main(
            {"generate": response}, "generate", "--prompt", "x", "--background", "transparent", "--alpha-snap",
            "--out", str(out),
        )
        self.assertEqual(_alphas(out.read_bytes()), [0, 100, 249, 255, 255, 255])
        self.assertIn("Alpha snap: set 2 of 6 pixels with alpha 250-254 to fully opaque.", self.stderr)

        plain = self.tmp / "plain.png"
        self.run_main(
            {"generate": response}, "generate", "--prompt", "x", "--background", "transparent", "--out", str(plain),
        )
        self.assertEqual(_alphas(plain.read_bytes()), ALPHAS)

    def test_missing_pillow_stops_before_any_request(self) -> None:
        # Pillow is needed only after download; without it the CLI must stop before paying for a call.
        response = SimpleNamespace(data=[SimpleNamespace(b64_json=_b64(PNG_BYTES))])
        jobs = self.tmp / "jobs.jsonl"
        jobs.write_text("\n".join([json.dumps({"prompt": "a"}), json.dumps({"prompt": "b"})]), encoding="utf-8")
        runs = {
            "two-job batch": (["generate-batch", "--input", str(jobs), "--out-dir", str(self.tmp / "batch"),
                               "--no-augment", "--background", "transparent", "--alpha-snap"], "--alpha-snap"),
            "generate": (["generate", "--prompt", "a", "--alpha-snap", "--out", str(self.tmp / "a.png")], "--alpha-snap"),
            "downscale": (["generate", "--prompt", "a", "--downscale-max-dim", "64", "--out", str(self.tmp / "d.png")],
                          "--downscale-max-dim"),
        }
        for label, (argv, flag) in runs.items():
            with self.subTest(run=label):
                async_images = FakeAsyncImages({"a": response, "b": response})
                with mock.patch.dict(sys.modules, {"PIL": None}):
                    with self.assertRaises(SystemExit) as ctx:
                        self.run_main({"generate": response}, *argv, async_client=SimpleNamespace(images=async_images))
                self.assertNotEqual(ctx.exception.code, 0)
                self.assertIn(f"Error: {flag} requires Pillow.", self.stderr)
                self.assertNotIn("Traceback", self.stderr)
                self.assertNotIn("Task exception", self.stderr)
                self.assertEqual(async_images.calls, [])
                self.assertEqual(self.fake_images.calls, [])

    def test_missing_pillow_does_not_block_dry_run(self) -> None:
        with mock.patch.dict(sys.modules, {"PIL": None}):
            images = self.run_main(
                {}, "generate", "--prompt", "a", "--alpha-snap", "--dry-run", "--out", str(self.tmp / "a.png")
            )
        self.assertEqual(images.calls, [])

    def test_alpha_snap_warns_when_it_has_no_effect(self) -> None:
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (2, 1), (10, 20, 30)).save(buffer, format="PNG")
        rgb = buffer.getvalue()
        response = SimpleNamespace(data=[SimpleNamespace(b64_json=_b64(rgb))])
        out = self.tmp / "opaque.png"
        self.run_main({"generate": response}, "generate", "--prompt", "x", "--alpha-snap", "--out", str(out))
        self.assertEqual(out.read_bytes(), rgb)
        self.assertIn("Warning: --alpha-snap had no effect: the image has no alpha channel.", self.stderr)

    def test_stream_warns_when_no_partial_images_arrive(self) -> None:
        events = [SimpleNamespace(type="image_generation.completed", b64_json=_b64(PNG_BYTES))]
        self.run_main(
            {"generate": iter(events)}, "generate", "--prompt", "x", "--stream", "--partial-images", "2",
            "--size", "auto", "--out", str(self.tmp / "s.png"),
        )
        self.assertIn("Warning: Result differs from the request: partial images requested 2, got 0.", self.stderr)
        self.assertEqual((self.tmp / "s.png").read_bytes(), PNG_BYTES)

    def test_batch_validates_every_job_before_any_request(self) -> None:
        # A valid first job must not start a paid request when a later job is invalid.
        valid = SimpleNamespace(data=[SimpleNamespace(b64_json=_b64(b"ok"))])
        for bad, message in (
            ({"n": "two"}, "n must be an integer"),
            ({"output_format": "jpeg", "output_compression": True}, "output-compression must be an integer"),
        ):
            with self.subTest(bad=bad):
                jobs = self.tmp / "jobs.jsonl"
                jobs.write_text(
                    "\n".join([json.dumps({"prompt": "valid"}), json.dumps({"prompt": "invalid", **bad})]),
                    encoding="utf-8",
                )
                async_images = FakeAsyncImages({"valid": valid})
                with self.assertRaises(SystemExit):
                    self.run_main(
                        {}, "generate-batch", "--input", str(jobs), "--out-dir", str(self.tmp / "batch"),
                        "--no-augment", "--force", async_client=SimpleNamespace(images=async_images),
                    )
                self.assertIn(message, self.stderr)
                self.assertEqual(async_images.calls, [])
                self.assertNotIn("Traceback", self.stderr)


class HelperTests(unittest.TestCase):
    """Table tests for the pure helpers behind the error line and the format check."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def test_detect_image_format_signatures(self) -> None:
        cases = {
            PNG_BYTES: "png",
            b"\xff\xd8\xff\xe0\x00\x10JFIF": "jpeg",
            b"GIF87a\x01\x00": "gif",
            b"GIF89a\x01\x00": "gif",
            b"RIFF\x24\x00\x00\x00WEBPVP8 ": "webp",
            b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>': "svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"/>': "svg",
            b'\n  <svg xmlns="http://www.w3.org/2000/svg"/>': "svg",
            b"RIFF\x24\x00\x00\x00WAVEfmt ": None,
            b"hello": None,
            b"": None,
        }
        for data, expected in cases.items():
            with self.subTest(data=data[:12]):
                self.assertEqual(self.module._detect_image_format(data), expected)

    def test_api_error_detail_body_shapes(self) -> None:
        cases = [
            ({"error": {"message": "openai message"}}, "openai message"),
            ({"error": "string error"}, "string error"),
            ({"message": "top-level message"}, "top-level message"),
            ({"detail": "GIF output is not enabled for this request."}, "GIF output is not enabled for this request."),
            ({"detail": [{"msg": "field required"}]}, '[{"msg": "field required"}]'),
            ({"error": {"message": ""}, "detail": "fallback detail"}, "fallback detail"),
            ("plain text body", "plain text body"),
            ("  line one\n  line two  ", "line one line two"),
            ("", None),
            ("   ", None),
            (None, None),
            ({}, None),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(self.module._api_error_detail(body), expected)

    def test_api_error_detail_is_truncated(self) -> None:
        detail = self.module._api_error_detail("x" * 1000)
        limit = self.module.MAX_ERROR_DETAIL_CHARS
        self.assertTrue(detail.startswith("x" * limit))
        self.assertTrue(detail.endswith("(truncated)"))
        self.assertNotIn("x" * (limit + 1), detail)

    def test_snap_alpha_png_and_webp(self) -> None:
        for fmt, save_args in (("PNG", {}), ("WEBP", {"lossless": True})):
            with self.subTest(fmt=fmt):
                snapped, changed, total = self.module._snap_alpha(_rgba_png(ALPHAS, fmt, **save_args))
                self.assertEqual((changed, total), (2, 6))
                self.assertEqual(self.module._detect_image_format(snapped), fmt.lower())
                self.assertEqual(_alphas(snapped), [0, 100, 249, 255, 255, 255])

    def test_alpha_snap_skip_reasons(self) -> None:
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (2, 1)).save(buffer, format="PNG")
        self.assertEqual(self.module._alpha_snap_skip_reason(buffer.getvalue()), "the image has no alpha channel")
        self.assertEqual(
            self.module._alpha_snap_skip_reason(b"\xff\xd8\xff\xe0rest"), "the API returned jpeg data, not png or webp"
        )
        self.assertIsNone(self.module._alpha_snap_skip_reason(_rgba_png(ALPHAS)))

    def test_safe_url(self) -> None:
        cases = {
            "http://user:pass@127.0.0.1:8080/v1/images/edits?api_key=k#frag": "http://127.0.0.1:8080/v1/images/edits",
            "https://api.openai.com/v1/images/generations": "https://api.openai.com/v1/images/generations",
            "http://[::1]:8080/v1/images/generations": "http://[::1]:8080/v1/images/generations",
            "http://localhost/v1": "http://localhost/v1",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(self.module._safe_url(url), expected)


if __name__ == "__main__":
    unittest.main()
