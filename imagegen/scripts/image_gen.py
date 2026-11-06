#!/usr/bin/env python3
"""CLI for image generation and editing with GPT Image models through the OpenAI Image API.

Defaults to gpt-image-2.6-sunburst (IMAGE_GEN_MODEL or --model changes it) and a structured
prompt augmentation workflow.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

from io import BytesIO

DEFAULT_MODEL = "gpt-image-2.6-sunburst"
# Optional environment variable that replaces DEFAULT_MODEL. --model and a batch job's "model" key
# still override it.
MODEL_ENV_VAR = "IMAGE_GEN_MODEL"
# The API picks a size that fits the prompt; --size asks for exact dimensions.
DEFAULT_SIZE = "auto"
DEFAULT_QUALITY = "auto"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_CONCURRENCY = 5
DEFAULT_DOWNSCALE_SUFFIX = "-web"
DEFAULT_OUTPUT_PATH = "output/imagegen/output.png"
GPT_IMAGE_MODEL_PREFIX = "gpt-image-"

# Deprecated GPT Image 1.x models (source: developers.openai.com/api/docs/deprecations).
# They accept only the fixed sizes and base qualities, and they are the only models that accept
# input_fidelity. This list does not grow: newer models use the current rules below.
LEGACY_MODEL_STATUS = {
    "gpt-image-1": "was shut down on 2026-10-23",
    "gpt-image-1-mini": "shuts down on 2026-12-01",
    "gpt-image-1.5": "shuts down on 2026-12-01",
    "gpt-image-1.5-2025-12-16": "shuts down on 2026-12-01",
}
LEGACY_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
LEGACY_INPUT_FIDELITIES = {"gpt-image-1-mini": {"low"}}
DEFAULT_LEGACY_INPUT_FIDELITIES = {"low", "high"}
# Models that accept custom sizes but not the xhigh and max qualities added in GPT Image 2.5.
BASE_QUALITY_MODELS = {"gpt-image-2", "gpt-image-2-2026-04-21"}

BASE_QUALITIES = {"low", "medium", "high", "auto"}
EXTENDED_QUALITIES = BASE_QUALITIES | {"xhigh", "max"}
ALLOWED_BACKGROUNDS = {"transparent", "opaque", "auto", None}
ALLOWED_COMPLEXITIES = {"low", "medium", "high"}
ALLOWED_OUTPUT_FORMATS = {"png", "jpeg", "jpg", "webp", "gif", "svg"}
TRANSPARENT_OUTPUT_FORMATS = {"png", "webp", "svg"}
SVG_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}

# Custom WIDTHxHEIGHT limits for GPT Image 2 and newer (image-generation guide, "Size and quality").
SIZE_MULTIPLE = 16
MAX_ASPECT_RATIO = 3
MAX_EDGE_PIXELS = 3840
MIN_TOTAL_PIXELS = 655_360
MAX_TOTAL_PIXELS = 8_294_400

GIF_FRAME_COUNT_RANGE = (2, 16)
GIF_FRAME_DURATION_MS_RANGE = (20, 2000)
PARTIAL_IMAGES_RANGE = (0, 3)
N_RANGE = (1, 10)
OUTPUT_COMPRESSION_RANGE = (0, 100)
MAX_SAFETY_IDENTIFIER_CHARS = 128
# Longest server error text the CLI prints for a failed API call.
MAX_ERROR_DETAIL_CHARS = 600
# File extension for each output format the API can return.
FORMAT_EXTENSIONS = {"png": ".png", "jpeg": ".jpeg", "webp": ".webp", "gif": ".gif", "svg": ".svg"}
# --alpha-snap raises alpha values from this one up to 254 to 255. Transparent logos from an
# OpenAI-compatible proxy came back with their solid areas at alpha 252-254 (observed 2026-11-06).
ALPHA_SNAP_MIN = 250

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_BATCH_JOBS = 500


def _die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def _dependency_hint(package: str, *, upgrade: bool = False) -> str:
    command = f"uv pip install {'-U ' if upgrade else ''}{package}"
    return (
        "Activate the repo-selected environment first, then install it with "
        f"`{command}`. If this repo uses a local virtualenv, start with "
        "`source .venv/bin/activate`; otherwise use this repo's configured shared fallback "
        "environment. If your project declares dependencies, prefer that project's normal "
        "`uv sync` flow."
    )


def _ensure_api_key(dry_run: bool) -> None:
    if os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is set.", file=sys.stderr)
        return
    if dry_run:
        _warn("OPENAI_API_KEY is not set; dry-run only.")
        return
    _die("OPENAI_API_KEY is not set. Export it before running.")


def _read_prompt(prompt: Optional[str], prompt_file: Optional[str]) -> str:
    if prompt and prompt_file:
        _die("Use --prompt or --prompt-file, not both.")
    if prompt_file:
        path = Path(prompt_file)
        if not path.exists():
            _die(f"Prompt file not found: {path}")
        return path.read_text(encoding="utf-8").strip()
    if prompt:
        return prompt.strip()
    _die("Missing prompt. Use --prompt or --prompt-file.")
    return ""  # unreachable


def _check_image_paths(paths: Iterable[str]) -> List[Path]:
    resolved: List[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            _die(f"Image file not found: {path}")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            _warn(f"Image exceeds 50MB limit: {path}")
        resolved.append(path)
    return resolved


def _normalize_output_format(fmt: Optional[str]) -> str:
    if not fmt:
        return DEFAULT_OUTPUT_FORMAT
    fmt = fmt.lower()
    if fmt not in ALLOWED_OUTPUT_FORMATS:
        _die("output-format must be png, jpeg, jpg, webp, gif, or svg.")
    return "jpeg" if fmt == "jpg" else fmt


def _validate_size(size: str, model: str, output_format: str) -> None:
    if size == "auto":
        return
    if model in LEGACY_MODEL_STATUS:
        if size not in LEGACY_SIZES:
            _die(f"size must be one of 1024x1024, 1536x1024, 1024x1536, or auto for {model}.")
        return
    match = re.fullmatch(r"([0-9]+)x([0-9]+)", size)
    if not match:
        _die(f"size must be auto or WIDTHxHEIGHT (for example 1536x864), got {size!r}.")
    width, height = int(match.group(1)), int(match.group(2))
    if width < 1 or height < 1 or width % SIZE_MULTIPLE or height % SIZE_MULTIPLE:
        _die(f"size width and height must be positive multiples of {SIZE_MULTIPLE}, got {size}.")
    if output_format == "gif":
        # GIF frame sizes have their own per-model budget that the API checks.
        return
    if max(width, height) > MAX_EDGE_PIXELS:
        _die(f"size edges must be at most {MAX_EDGE_PIXELS} pixels, got {size}.")
    if max(width, height) > MAX_ASPECT_RATIO * min(width, height):
        _die(f"size aspect ratio must be between 1:3 and 3:1, got {size}.")
    pixels = width * height
    if not MIN_TOTAL_PIXELS <= pixels <= MAX_TOTAL_PIXELS:
        _die(
            f"size must have between {MIN_TOTAL_PIXELS:,} and {MAX_TOTAL_PIXELS:,} pixels, "
            f"got {size} ({pixels:,} pixels)."
        )


def _validate_quality(quality: str, model: str) -> None:
    if model in LEGACY_MODEL_STATUS or model in BASE_QUALITY_MODELS:
        if quality not in BASE_QUALITIES:
            _die(f"quality must be one of low, medium, high, or auto for {model}, got {quality!r}.")
        return
    if quality not in EXTENDED_QUALITIES:
        _die(f"quality must be one of low, medium, high, xhigh, max, or auto, got {quality!r}.")


def _validate_background(background: Optional[str]) -> None:
    if background not in ALLOWED_BACKGROUNDS:
        _die("background must be one of transparent, opaque, or auto.")


def _validate_input_fidelity(input_fidelity: Optional[str], model: str) -> None:
    if input_fidelity is None:
        return
    if model not in LEGACY_MODEL_STATUS:
        _die(
            f"Omit --input-fidelity for {model}: GPT Image 2 and newer models always "
            "process input images at high fidelity."
        )
    allowed = LEGACY_INPUT_FIDELITIES.get(model, DEFAULT_LEGACY_INPUT_FIDELITIES)
    if input_fidelity not in allowed:
        if allowed == {"low"}:
            _die(f"{model} supports only low input-fidelity, got {input_fidelity!r}.")
        _die(f"input-fidelity must be one of low or high, got {input_fidelity!r}.")


def _default_model() -> str:
    """Return the model used when --model is not given.

    IMAGE_GEN_MODEL holds the user's standing model choice; an empty value counts as unset.
    """
    return os.getenv(MODEL_ENV_VAR, "").strip() or DEFAULT_MODEL


def _validate_model(model: str) -> None:
    if not model.startswith(GPT_IMAGE_MODEL_PREFIX):
        _die(
            "model must be a GPT Image model (for example gpt-image-2.6-sunburst or "
            f"gpt-image-2.6-flare), got {model!r}. Set the model with --model or {MODEL_ENV_VAR}."
        )


# Each deprecated model is reported once per run, also when many batch jobs use it.
_reported_deprecated_models = set()


def _warn_if_deprecated(model: str) -> None:
    status = LEGACY_MODEL_STATUS.get(model)
    if status and model not in _reported_deprecated_models:
        _reported_deprecated_models.add(model)
        _warn(
            f"{model} is deprecated and {status}. "
            "Use gpt-image-2.6-sunburst or gpt-image-2.6-flare instead."
        )


def _validate_transparency(background: Optional[str], output_format: str) -> None:
    if background == "transparent" and output_format not in TRANSPARENT_OUTPUT_FORMATS:
        _die("transparent background requires output-format png, webp, or svg.")


def _validate_int_range(name: str, value: Any, bounds: Tuple[int, int]) -> None:
    low, high = bounds
    # Batch JSONL values can be floats, strings, or booleans (bool is an int subclass).
    # The API takes integers only, and truncating would hide out-of-range values.
    if isinstance(value, bool) or not isinstance(value, int):
        _die(f"{name} must be an integer, got {value!r}.")
    if not low <= value <= high:
        _die(f"{name} must be between {low} and {high}, got {value}.")


def _build_svg_options(svg_model: Optional[str], effort: Optional[str]) -> Optional[Dict[str, Any]]:
    if effort is not None and svg_model is None:
        _die("--svg-reasoning-effort requires --svg-model.")
    if svg_model is None:
        return None
    options: Dict[str, Any] = {"model": svg_model}
    if effort is not None:
        options["reasoning"] = {"effort": effort}
    return options


def _validate_svg_options(svg: Any) -> None:
    if not isinstance(svg, dict) or not isinstance(svg.get("model"), str) or not svg["model"]:
        _die(f"svg options must be an object with a non-empty model, got {svg!r}.")
    effort = (svg.get("reasoning") or {}).get("effort")
    if effort is not None and effort not in SVG_REASONING_EFFORTS:
        _die(
            "svg-reasoning-effort must be one of "
            f"{', '.join(sorted(SVG_REASONING_EFFORTS))}, got {effort!r}."
        )


def _validate_output_format_rules(
    payload: Dict[str, Any],
    *,
    endpoint: str,
    has_mask: bool,
    downscale: bool,
    alpha_snap: bool,
) -> None:
    """Check the option combinations of GIF, SVG, streaming, and local post-processing."""
    output_format = payload["output_format"]
    if alpha_snap and output_format not in {"png", "webp"}:
        _die("--alpha-snap works only with png or webp output.")
    has_frame_options = "frame_count" in payload or "frame_duration_ms" in payload
    if has_frame_options and output_format != "gif":
        _die("--frame-count and --frame-duration-ms require --output-format gif.")
    if "frame_count" in payload:
        _validate_int_range("frame-count", payload["frame_count"], GIF_FRAME_COUNT_RANGE)
    if "frame_duration_ms" in payload:
        _validate_int_range(
            "frame-duration-ms", payload["frame_duration_ms"], GIF_FRAME_DURATION_MS_RANGE
        )

    if "svg" in payload:
        if output_format != "svg":
            _die("--svg-model and --svg-reasoning-effort require --output-format svg.")
        _validate_svg_options(payload["svg"])

    if output_format in {"gif", "svg"}:
        if endpoint == "edit" and output_format == "svg":
            _die("The edit endpoint does not support svg output.")
        if output_format == "svg" and int(payload.get("n", 1)) != 1:
            _die("svg output requires n=1.")
        if payload.get("output_compression") is not None:
            _die(f"{output_format} output does not support --output-compression.")
        if payload.get("stream"):
            _die(f"{output_format} output does not support streaming.")
        if has_mask and output_format == "gif":
            _die("gif output does not support --mask.")
        if downscale:
            _die(f"--downscale-max-dim does not support {output_format} output.")

    if "partial_images" in payload:
        if not payload.get("stream"):
            _die("--partial-images requires --stream.")
        _validate_int_range("partial-images", payload["partial_images"], PARTIAL_IMAGES_RANGE)
    if payload.get("stream") and int(payload.get("n", 1)) != 1:
        _die("--stream supports only --n 1 in this CLI.")


def _validate_request(
    payload: Dict[str, Any],
    *,
    endpoint: str,
    has_mask: bool = False,
    downscale: bool = False,
    alpha_snap: bool = False,
) -> None:
    """Validate a complete request payload whose output_format and size are already resolved."""
    model = str(payload.get("model", DEFAULT_MODEL))
    _validate_model(model)
    _warn_if_deprecated(model)
    _validate_int_range("n", payload.get("n", 1), N_RANGE)
    output_format = payload["output_format"]
    _validate_size(str(payload["size"]), model, output_format)
    _validate_quality(str(payload.get("quality", DEFAULT_QUALITY)), model)
    _validate_background(payload.get("background"))
    _validate_transparency(payload.get("background"), output_format)
    oc = payload.get("output_compression")
    if oc is not None:
        _validate_int_range("output-compression", oc, OUTPUT_COMPRESSION_RANGE)
    complexity = payload.get("complexity")
    if complexity is not None and complexity not in ALLOWED_COMPLEXITIES:
        _die(f"complexity must be one of low, medium, or high, got {complexity!r}.")
    safety_identifier = payload.get("safety_identifier")
    if safety_identifier is not None and len(str(safety_identifier)) > MAX_SAFETY_IDENTIFIER_CHARS:
        _die(f"safety-identifier must be at most {MAX_SAFETY_IDENTIFIER_CHARS} characters.")
    if endpoint == "edit":
        _validate_input_fidelity(payload.get("input_fidelity"), model)
    _validate_output_format_rules(
        payload, endpoint=endpoint, has_mask=has_mask, downscale=downscale, alpha_snap=alpha_snap
    )


def _resolve_output_format_and_size(payload: Dict[str, Any]) -> str:
    output_format = _normalize_output_format(payload.get("output_format"))
    payload["output_format"] = output_format
    payload.setdefault("size", DEFAULT_SIZE)
    return output_format


def _build_output_paths(
    out: str,
    output_format: str,
    count: int,
    out_dir: Optional[str],
) -> List[Path]:
    ext = "." + output_format

    if out_dir:
        out_base = Path(out_dir)
        out_base.mkdir(parents=True, exist_ok=True)
        return [out_base / f"image_{i}{ext}" for i in range(1, count + 1)]

    out_path = Path(out)
    if out_path.exists() and out_path.is_dir():
        out_path.mkdir(parents=True, exist_ok=True)
        return [out_path / f"image_{i}{ext}" for i in range(1, count + 1)]

    if out_path.suffix == "":
        out_path = out_path.with_suffix(ext)
    elif output_format and out_path.suffix.lstrip(".").lower() != output_format:
        _warn(
            f"Output extension {out_path.suffix} does not match output-format {output_format}."
        )

    if count == 1:
        return [out_path]

    return [
        out_path.with_name(f"{out_path.stem}-{i}{out_path.suffix}")
        for i in range(1, count + 1)
    ]


def _augment_prompt(args: argparse.Namespace, prompt: str) -> str:
    fields = _fields_from_args(args)
    return _augment_prompt_fields(args.augment, prompt, fields)


def _augment_prompt_fields(augment: bool, prompt: str, fields: Dict[str, Optional[str]]) -> str:
    if not augment:
        return prompt

    sections: List[str] = []
    if fields.get("use_case"):
        sections.append(f"Use case: {fields['use_case']}")
    sections.append(f"Primary request: {prompt}")
    if fields.get("scene"):
        sections.append(f"Scene/background: {fields['scene']}")
    if fields.get("subject"):
        sections.append(f"Subject: {fields['subject']}")
    if fields.get("style"):
        sections.append(f"Style/medium: {fields['style']}")
    if fields.get("composition"):
        sections.append(f"Composition/framing: {fields['composition']}")
    if fields.get("lighting"):
        sections.append(f"Lighting/mood: {fields['lighting']}")
    if fields.get("palette"):
        sections.append(f"Color palette: {fields['palette']}")
    if fields.get("materials"):
        sections.append(f"Materials/textures: {fields['materials']}")
    if fields.get("text"):
        sections.append(f"Text (verbatim): \"{fields['text']}\"")
    if fields.get("constraints"):
        sections.append(f"Constraints: {fields['constraints']}")
    if fields.get("negative"):
        sections.append(f"Avoid: {fields['negative']}")

    return "\n".join(sections)


def _fields_from_args(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    return {
        "use_case": getattr(args, "use_case", None),
        "scene": getattr(args, "scene", None),
        "subject": getattr(args, "subject", None),
        "style": getattr(args, "style", None),
        "composition": getattr(args, "composition", None),
        "lighting": getattr(args, "lighting", None),
        "palette": getattr(args, "palette", None),
        "materials": getattr(args, "materials", None),
        "text": getattr(args, "text", None),
        "constraints": getattr(args, "constraints", None),
        "negative": getattr(args, "negative", None),
    }


def _print_request(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _detect_image_format(data: bytes) -> Optional[str]:
    """Return the format of image bytes from their signature, or None when it is not recognized."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    head = data[:512].lstrip()
    if head.startswith((b"<?xml", b"<svg")):
        return "svg"
    return None


def _write_image(raw: bytes, out_path: Path, *, force: bool, expected_format: str) -> Tuple[Path, str]:
    """Write image bytes and return the path written and the format of the bytes.

    Some backends ignore output_format. When the bytes are in another known format than the
    request, the file gets that format's extension, so its name matches its content.
    """
    actual_format = _detect_image_format(raw) or expected_format
    if actual_format != expected_format:
        out_path = out_path.with_suffix(FORMAT_EXTENSIONS[actual_format])
    if out_path.exists() and not force:
        _die(f"Output already exists: {out_path} (use --force to overwrite)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    print(f"Wrote {out_path}")
    if actual_format != expected_format:
        _warn(f"requested {expected_format} output, but the API returned {actual_format} data; wrote {out_path}.")
    return out_path, actual_format


def _derive_downscale_path(path: Path, suffix: str) -> Path:
    if suffix and not suffix.startswith("-") and not suffix.startswith("_"):
        suffix = "-" + suffix
    return path.with_name(f"{path.stem}{suffix}{path.suffix}")


def _import_pillow(feature: str) -> Any:
    try:
        from PIL import Image
    except ImportError:
        _die(f"{feature} requires Pillow. {_dependency_hint('pillow')}")
    return Image


def _alpha_snap_skip_reason(raw: bytes) -> Optional[str]:
    """Return why --alpha-snap cannot change these image bytes, or None when it can."""
    fmt = _detect_image_format(raw)
    if fmt not in {"png", "webp"}:
        return f"the API returned {fmt or 'unrecognized'} data, not png or webp"
    Image = _import_pillow("--alpha-snap")
    with Image.open(BytesIO(raw)) as img:
        if "A" not in img.getbands() and "transparency" not in img.info:
            return "the image has no alpha channel"
    return None


def _snap_alpha(raw: bytes) -> Tuple[bytes, int, int]:
    """Raise alpha ALPHA_SNAP_MIN..254 to 255; return the new bytes, pixels changed, and total pixels.

    The image keeps its format. WebP is saved lossless, so the snap adds no compression loss.
    """
    Image = _import_pillow("--alpha-snap")
    fmt = _detect_image_format(raw)
    with Image.open(BytesIO(raw)) as img:
        rgba = img.convert("RGBA")
    red, green, blue, alpha = rgba.split()
    changed = sum(alpha.histogram()[ALPHA_SNAP_MIN:255])
    alpha = alpha.point(lambda value: 255 if value >= ALPHA_SNAP_MIN else value)
    snapped = Image.merge("RGBA", (red, green, blue, alpha))
    out = BytesIO()
    if fmt == "webp":
        snapped.save(out, format="WEBP", lossless=True)
    else:
        snapped.save(out, format="PNG")
    return out.getvalue(), changed, rgba.width * rgba.height


def _apply_alpha_snap(raw: bytes) -> bytes:
    reason = _alpha_snap_skip_reason(raw)
    if reason:
        _warn(f"--alpha-snap had no effect: {reason}.")
        return raw
    snapped, changed, total = _snap_alpha(raw)
    print(
        f"Alpha snap: set {changed} of {total} pixels with alpha {ALPHA_SNAP_MIN}-254 to fully opaque.",
        file=sys.stderr,
    )
    return snapped


def _downscale_image_bytes(image_bytes: bytes, *, max_dim: int, output_format: str) -> bytes:
    try:
        from PIL import Image
    except Exception:
        _die(f"Downscaling requires Pillow. {_dependency_hint('pillow')}")

    if max_dim < 1:
        _die("--downscale-max-dim must be >= 1")

    with Image.open(BytesIO(image_bytes)) as img:
        img.load()
        w, h = img.size
        scale = min(1.0, float(max_dim) / float(max(w, h)))
        target = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))

        resized = img if target == (w, h) else img.resize(target, Image.Resampling.LANCZOS)

        fmt = output_format.lower()
        if fmt == "jpg":
            fmt = "jpeg"

        if fmt == "jpeg":
            if resized.mode in ("RGBA", "LA") or ("transparency" in getattr(resized, "info", {})):
                bg = Image.new("RGB", resized.size, (255, 255, 255))
                bg.paste(resized.convert("RGBA"), mask=resized.convert("RGBA").split()[-1])
                resized = bg
            else:
                resized = resized.convert("RGB")

        out = BytesIO()
        resized.save(out, format=fmt.upper())
        return out.getvalue()


def _decode_write_and_downscale(
    images: List[str],
    outputs: List[Path],
    *,
    force: bool,
    downscale_max_dim: Optional[int],
    downscale_suffix: str,
    output_format: str,
    alpha_snap: bool,
) -> None:
    for idx, image_b64 in enumerate(images):
        if idx >= len(outputs):
            break
        raw = base64.b64decode(image_b64)
        if alpha_snap:
            raw = _apply_alpha_snap(raw)
        out_path, actual_format = _write_image(raw, outputs[idx], force=force, expected_format=output_format)

        if downscale_max_dim is None:
            continue

        derived = _derive_downscale_path(out_path, downscale_suffix)
        if derived.exists() and not force:
            _die(f"Output already exists: {derived} (use --force to overwrite)")
        derived.parent.mkdir(parents=True, exist_ok=True)
        resized = _downscale_image_bytes(raw, max_dim=downscale_max_dim, output_format=actual_format)
        derived.write_bytes(resized)
        print(f"Wrote {derived}")


def _consume_image_stream(
    events: Iterable[Any], output_path: Path, *, force: bool, output_format: str
) -> Tuple[Any, int]:
    """Write partial images next to output_path; return the completed event and the partial count.

    Partial images are named <stem>-partial-<index><suffix>. Generation streams emit
    image_generation.* events and edit streams emit image_edit.* events.
    """
    completed: Any = None
    partial_count = 0
    for event in events:
        event_type = str(getattr(event, "type", ""))
        if event_type.endswith(".partial_image"):
            partial_path = output_path.with_name(
                f"{output_path.stem}-partial-{event.partial_image_index}{output_path.suffix}"
            )
            _write_image(
                base64.b64decode(event.b64_json), partial_path, force=force, expected_format=output_format
            )
            partial_count += 1
        elif event_type.endswith(".completed"):
            completed = event
    if completed is None:
        _die("Image stream ended without a completed image event.")
    return completed, partial_count


def _report_result_metadata(result: Any, label: str) -> None:
    """Print the settings the API reports for a finished request, plus token usage.

    These response fields are optional in the API, so only the fields present are printed.
    They show what auto size and quality resolved to, and what the request cost in tokens.
    """
    parts = [
        f"{name}={getattr(result, name)}"
        for name in ("size", "quality", "background", "output_format")
        if getattr(result, name, None) is not None
    ]
    usage = getattr(result, "usage", None)
    if usage is not None:
        parts.append(
            f"tokens input={usage.input_tokens} output={usage.output_tokens} total={usage.total_tokens}"
        )
    svg_usage = getattr(result, "svg_usage", None)
    if svg_usage is not None:
        parts.append(
            f"svg author {svg_usage.model} tokens input={svg_usage.input_tokens} "
            f"output={svg_usage.output_tokens} total={svg_usage.total_tokens}"
        )
    if parts:
        print(f"{label}: {'; '.join(parts)}", file=sys.stderr)


def _collect_result(
    result: Any,
    payload: Dict[str, Any],
    first_output: Path,
    *,
    force: bool,
    label: str,
) -> List[str]:
    """Return the final images as base64; print the reported settings and any ignored ones."""
    if payload.get("stream"):
        completed, partial_count = _consume_image_stream(
            result, first_output, force=force, output_format=payload["output_format"]
        )
        _report_result_metadata(completed, label)
        _warn_on_ignored_settings(payload, completed, image_count=1, partial_count=partial_count, label=label)
        return [completed.b64_json]
    images = [item.b64_json for item in result.data]
    _report_result_metadata(result, label)
    _warn_on_ignored_settings(payload, result, image_count=len(images), partial_count=None, label=label)
    return images


def _warn_on_ignored_settings(
    payload: Dict[str, Any],
    reported: Any,
    *,
    image_count: int,
    partial_count: Optional[int],
    label: str,
) -> None:
    """Warn when the result differs from the request, for example because a proxy ignored a setting.

    Size and quality are compared only when the request named a value other than auto. Partial
    images are flagged only when none arrived: the API may send the final image before all
    requested partial images.
    """
    differences = []
    requested_size = str(payload.get("size", "auto"))
    reported_size = getattr(reported, "size", None)
    if requested_size != "auto" and reported_size and reported_size != requested_size:
        differences.append(f"size requested {requested_size}, got {reported_size}")
    requested_quality = payload.get("quality", DEFAULT_QUALITY)
    reported_quality = getattr(reported, "quality", None)
    if requested_quality != "auto" and reported_quality and reported_quality != requested_quality:
        differences.append(f"quality requested {requested_quality}, got {reported_quality}")
    requested_background = payload.get("background")
    reported_background = getattr(reported, "background", None)
    if (
        requested_background in {"transparent", "opaque"}
        and reported_background
        and reported_background != requested_background
    ):
        differences.append(f"background requested {requested_background}, got {reported_background}")
    requested_n = payload.get("n", 1)
    if image_count < requested_n:
        differences.append(f"images requested {requested_n}, got {image_count}")
    requested_partials = payload.get("partial_images", 0)
    if partial_count == 0 and requested_partials > 0:
        differences.append(f"partial images requested {requested_partials}, got 0")
    if differences:
        _warn(f"{label} differs from the request: {'; '.join(differences)}.")


def _api_error_detail(body: Any) -> Optional[str]:
    """Return the server's error text from an OpenAI SDK error body, or None when it has none.

    OpenAI sends {"error": {"message": ...}}. Proxies also send {"error": "..."},
    {"message": ...}, {"detail": ...} (one proxy did for disabled GIF output), or plain text.
    """
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        candidates = (error, body.get("message"), body.get("detail"))
        text = next((c if isinstance(c, str) else json.dumps(c) for c in candidates if c), "")
    elif isinstance(body, str):
        text = body
    else:
        text = ""
    text = _one_line(text)
    if len(text) > MAX_ERROR_DETAIL_CHARS:
        text = text[:MAX_ERROR_DETAIL_CHARS] + " (truncated)"
    return text or None


def _one_line(text: str) -> str:
    """Collapse all whitespace, line breaks included, so a diagnostic stays on one line."""
    return " ".join(str(text).split())


def _safe_url(url: Any) -> str:
    """Return the scheme, host, port, and path of a request URL.

    User info and the query string are dropped because they can hold credentials
    (for example a password in OPENAI_BASE_URL); httpx keeps both when it renders a URL.
    """
    parts = urlsplit(str(url))
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}{parts.path}"


def _describe_api_error(operation: str, exc: BaseException) -> Optional[str]:
    """Return one line describing an OpenAI SDK error, or None for any other exception."""
    try:
        import openai
    except ImportError:
        return None
    request = getattr(exc, "request", None)
    target = f"{request.method} {_safe_url(request.url)}" if request is not None else "the API"
    if isinstance(exc, openai.APIStatusError):
        detail = _api_error_detail(exc.body)
        if detail is None:
            detail = (
                "the server sent no error message. If OPENAI_BASE_URL points at a proxy, it may not "
                "support an option in this request; retry without that option or check the proxy's logs"
            )
        message = f"{operation} failed with HTTP {exc.status_code} ({target}): {detail.rstrip('.')}."
        if exc.request_id:
            message += f" Request id: {exc.request_id}."
        return message
    if isinstance(exc, openai.APIConnectionError):
        return (
            f"{operation} failed: could not reach {target} ({_one_line(exc.message)}). "
            "Check OPENAI_BASE_URL and that the server is running."
        )
    if isinstance(exc, openai.APIError):
        return f"{operation} failed: {_one_line(exc.message)}"
    return None


def _create_client():
    try:
        from openai import OpenAI
    except ImportError:
        _die(f"openai SDK not installed in the active environment. {_dependency_hint('openai')}")
    return OpenAI()


def _create_async_client():
    try:
        from openai import AsyncOpenAI
    except ImportError:
        try:
            import openai as _openai  # noqa: F401
        except ImportError:
            _die(
                f"openai SDK not installed in the active environment. {_dependency_hint('openai')}"
            )
        _die(
            "AsyncOpenAI not available in this openai SDK version. "
            f"{_dependency_hint('openai', upgrade=True)}"
        )
    return AsyncOpenAI()


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:60] if value else "job"


def _normalize_job(job: Any, idx: int) -> Dict[str, Any]:
    if isinstance(job, str):
        prompt = job.strip()
        if not prompt:
            _die(f"Empty prompt at job {idx}")
        return {"prompt": prompt}
    if isinstance(job, dict):
        if "prompt" not in job or not str(job["prompt"]).strip():
            _die(f"Missing prompt for job {idx}")
        return job
    _die(f"Invalid job at index {idx}: expected string or object.")
    return {}  # unreachable


def _read_jobs_jsonl(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        _die(f"Input file not found: {p}")
    jobs: List[Dict[str, Any]] = []
    for line_no, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item: Any
            if line.startswith("{"):
                item = json.loads(line)
            else:
                item = line
            jobs.append(_normalize_job(item, idx=line_no))
        except json.JSONDecodeError as exc:
            _die(f"Invalid JSON on line {line_no}: {exc}")
    if not jobs:
        _die("No jobs found in input file.")
    if len(jobs) > MAX_BATCH_JOBS:
        _die(f"Too many jobs ({len(jobs)}). Max is {MAX_BATCH_JOBS}.")
    return jobs


def _merge_non_null(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(dst)
    for k, v in src.items():
        if v is not None:
            merged[k] = v
    return merged


def _job_output_paths(
    *,
    out_dir: Path,
    output_format: str,
    idx: int,
    prompt: str,
    n: int,
    explicit_out: Optional[str],
) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = "." + output_format

    if explicit_out:
        base = Path(explicit_out)
        if base.suffix == "":
            base = base.with_suffix(ext)
        elif base.suffix.lstrip(".").lower() != output_format:
            _warn(
                f"Job {idx}: output extension {base.suffix} does not match output-format {output_format}."
            )
        base = out_dir / base.name
    else:
        slug = _slugify(prompt[:80])
        base = out_dir / f"{idx:03d}-{slug}{ext}"

    if n == 1:
        return [base]
    return [
        base.with_name(f"{base.stem}-{i}{base.suffix}")
        for i in range(1, n + 1)
    ]


def _extract_retry_after_seconds(exc: Exception) -> Optional[float]:
    # Best-effort: openai SDK errors vary by version. Prefer a conservative fallback.
    for attr in ("retry_after", "retry_after_seconds"):
        val = getattr(exc, attr, None)
        if isinstance(val, (int, float)) and val >= 0:
            return float(val)
    msg = str(exc)
    m = re.search(r"retry[- ]after[:= ]+([0-9]+(?:\\.[0-9]+)?)", msg, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    if "ratelimit" in name or "rate_limit" in name:
        return True
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "too many requests" in msg


def _is_transient_error(exc: Exception) -> bool:
    if _is_rate_limit_error(exc):
        return True
    name = exc.__class__.__name__.lower()
    if "timeout" in name or "timedout" in name or "tempor" in name:
        return True
    msg = str(exc).lower()
    return "timeout" in msg or "timed out" in msg or "connection reset" in msg


async def _generate_one_with_retries(
    client: Any,
    payload: Dict[str, Any],
    *,
    attempts: int,
    job_label: str,
) -> Any:
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return await client.images.generate(**payload)
        except Exception as exc:
            last_exc = exc
            if not _is_transient_error(exc):
                raise
            if attempt == attempts:
                raise
            sleep_s = _extract_retry_after_seconds(exc)
            if sleep_s is None:
                sleep_s = min(60.0, 2.0**attempt)
            print(
                f"{job_label} attempt {attempt}/{attempts} failed ({exc.__class__.__name__}); retrying in {sleep_s:.1f}s",
                file=sys.stderr,
            )
            await asyncio.sleep(sleep_s)
    raise last_exc or RuntimeError("unknown error")


class _BatchJob(NamedTuple):
    """One generate-batch job, built and validated before any request starts."""

    index: int
    payload: Dict[str, Any]
    outputs: List[Path]
    output_format: str


def _prepare_batch_jobs(args: argparse.Namespace) -> List[_BatchJob]:
    """Build and validate the request of every job in the batch.

    Validation stops at the first invalid job, before any client exists, so a batch
    with an invalid job makes no API call at all.
    """
    jobs = _read_jobs_jsonl(args.input)
    out_dir = Path(args.out_dir)

    base_fields = _fields_from_args(args)
    base_payload = {
        "model": args.model,
        "n": args.n,
        "size": args.size,
        "quality": args.quality,
        "background": args.background,
        "output_format": args.output_format,
        "output_compression": args.output_compression,
        "moderation": args.moderation,
        "complexity": args.complexity,
        "frame_count": args.frame_count,
        "frame_duration_ms": args.frame_duration_ms,
        "safety_identifier": args.safety_identifier,
        "svg": _build_svg_options(args.svg_model, args.svg_reasoning_effort),
    }
    downscale = args.downscale_max_dim is not None

    prepared: List[_BatchJob] = []
    for i, job in enumerate(jobs, start=1):
        prompt = str(job["prompt"]).strip()
        fields = _merge_non_null(base_fields, job.get("fields", {}))
        # Allow flat job keys as well (use_case, scene, etc.)
        fields = _merge_non_null(fields, {k: job.get(k) for k in base_fields.keys()})
        augmented = _augment_prompt_fields(args.augment, prompt, fields)

        payload = dict(base_payload)
        payload["prompt"] = augmented
        payload = _merge_non_null(payload, {k: job.get(k) for k in base_payload.keys()})
        payload = {k: v for k, v in payload.items() if v is not None}

        output_format = _resolve_output_format_and_size(payload)
        _validate_request(payload, endpoint="generate", downscale=downscale, alpha_snap=args.alpha_snap)
        outputs = _job_output_paths(
            out_dir=out_dir,
            output_format=output_format,
            idx=i,
            prompt=prompt,
            n=payload.get("n", 1),
            explicit_out=job.get("out"),
        )
        prepared.append(_BatchJob(i, payload, outputs, output_format))
    return prepared


def _print_batch_requests(prepared: List[_BatchJob], args: argparse.Namespace) -> None:
    for job in prepared:
        downscaled = None
        if args.downscale_max_dim is not None:
            downscaled = [
                str(_derive_downscale_path(p, args.downscale_suffix)) for p in job.outputs
            ]
        _print_request(
            {
                "endpoint": "/v1/images/generations",
                "job": job.index,
                "outputs": [str(p) for p in job.outputs],
                "outputs_downscaled": downscaled,
                **job.payload,
            }
        )


async def _run_generate_batch(args: argparse.Namespace, prepared: List[_BatchJob]) -> int:
    client = _create_async_client()
    sem = asyncio.Semaphore(args.concurrency)

    any_failed = False

    async def run_job(job: _BatchJob) -> Tuple[int, Optional[str]]:
        nonlocal any_failed
        job_label = f"[job {job.index}/{len(prepared)}]"
        try:
            async with sem:
                print(f"{job_label} starting", file=sys.stderr)
                started = time.time()
                result = await _generate_one_with_retries(
                    client,
                    job.payload,
                    attempts=args.max_attempts,
                    job_label=job_label,
                )
                elapsed = time.time() - started
                print(f"{job_label} completed in {elapsed:.1f}s", file=sys.stderr)
            images = _collect_result(
                result, job.payload, job.outputs[0], force=args.force, label=f"{job_label} result"
            )
            _decode_write_and_downscale(
                images,
                job.outputs,
                force=args.force,
                downscale_max_dim=args.downscale_max_dim,
                downscale_suffix=args.downscale_suffix,
                output_format=job.output_format,
                alpha_snap=args.alpha_snap,
            )
            return job.index, None
        except Exception as exc:
            any_failed = True
            reason = _describe_api_error("Image generation", exc) or str(exc)
            print(f"{job_label} failed: {reason}", file=sys.stderr)
            if args.fail_fast:
                raise
            return job.index, str(exc)

    tasks = [asyncio.create_task(run_job(job)) for job in prepared]

    try:
        await asyncio.gather(*tasks)
    except Exception:
        for t in tasks:
            if not t.done():
                t.cancel()
        raise

    return 1 if any_failed else 0


def _generate_batch(args: argparse.Namespace) -> None:
    prepared = _prepare_batch_jobs(args)
    if args.dry_run:
        _print_batch_requests(prepared, args)
        return
    try:
        exit_code = asyncio.run(_run_generate_batch(args, prepared))
    except Exception as exc:
        # --fail-fast re-raises the first job's error after its "failed:" line was printed.
        if _describe_api_error("Image generation", exc) is None:
            raise
        _die("Batch stopped at the first failed job (--fail-fast).")
    if exit_code:
        raise SystemExit(exit_code)


def _generate(args: argparse.Namespace) -> None:
    prompt = _read_prompt(args.prompt, args.prompt_file)
    prompt = _augment_prompt(args, prompt)

    payload = {
        "model": args.model,
        "prompt": prompt,
        "n": args.n,
        "size": args.size,
        "quality": args.quality,
        "background": args.background,
        "output_format": args.output_format,
        "output_compression": args.output_compression,
        "moderation": args.moderation,
        "complexity": args.complexity,
        "frame_count": args.frame_count,
        "frame_duration_ms": args.frame_duration_ms,
        "safety_identifier": args.safety_identifier,
        "svg": _build_svg_options(args.svg_model, args.svg_reasoning_effort),
        "stream": True if args.stream else None,
        "partial_images": args.partial_images,
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    output_format = _resolve_output_format_and_size(payload)
    _validate_request(
        payload,
        endpoint="generate",
        downscale=args.downscale_max_dim is not None,
        alpha_snap=args.alpha_snap,
    )
    output_paths = _build_output_paths(args.out, output_format, args.n, args.out_dir)
    downscaled = None
    if args.downscale_max_dim is not None:
        downscaled = [str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths]

    if args.dry_run:
        _print_request(
            {
                "endpoint": "/v1/images/generations",
                "outputs": [str(p) for p in output_paths],
                "outputs_downscaled": downscaled,
                **payload,
            }
        )
        return

    print(
        "Calling Image API (generation). This can take up to a couple of minutes.",
        file=sys.stderr,
    )
    started = time.time()
    client = _create_client()
    try:
        result = client.images.generate(**payload)
        images = _collect_result(result, payload, output_paths[0], force=args.force, label="Result")
    except Exception as exc:
        message = _describe_api_error("Image generation", exc)
        if message is None:
            raise
        _die(message)
    elapsed = time.time() - started
    print(f"Generation completed in {elapsed:.1f}s.", file=sys.stderr)

    _decode_write_and_downscale(
        images,
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        output_format=output_format,
        alpha_snap=args.alpha_snap,
    )


def _edit(args: argparse.Namespace) -> None:
    prompt = _read_prompt(args.prompt, args.prompt_file)
    prompt = _augment_prompt(args, prompt)

    image_paths = _check_image_paths(args.image)
    mask_path = Path(args.mask) if args.mask else None
    if mask_path:
        if not mask_path.exists():
            _die(f"Mask file not found: {mask_path}")
        if mask_path.suffix.lower() != ".png":
            _warn(f"Mask should be a PNG with an alpha channel: {mask_path}")
        if mask_path.stat().st_size > MAX_IMAGE_BYTES:
            _warn(f"Mask exceeds 50MB limit: {mask_path}")
    markup_path = Path(args.markup) if args.markup else None
    if markup_path:
        if mask_path:
            _die("--markup cannot be combined with --mask.")
        if not markup_path.exists():
            _die(f"Markup file not found: {markup_path}")
        if markup_path.suffix.lower() != ".png":
            _warn(f"Markup should be a transparent PNG the size of the first image: {markup_path}")
        if markup_path.stat().st_size > MAX_IMAGE_BYTES:
            _warn(f"Markup exceeds 50MB limit: {markup_path}")

    payload = {
        "model": args.model,
        "prompt": prompt,
        "n": args.n,
        "size": args.size,
        "quality": args.quality,
        "background": args.background,
        "output_format": args.output_format,
        "output_compression": args.output_compression,
        "input_fidelity": args.input_fidelity,
        "moderation": args.moderation,
        "complexity": args.complexity,
        "frame_count": args.frame_count,
        "frame_duration_ms": args.frame_duration_ms,
        "safety_identifier": args.safety_identifier,
        "stream": True if args.stream else None,
        "partial_images": args.partial_images,
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    output_format = _resolve_output_format_and_size(payload)
    _validate_request(
        payload,
        endpoint="edit",
        has_mask=mask_path is not None,
        downscale=args.downscale_max_dim is not None,
        alpha_snap=args.alpha_snap,
    )
    output_paths = _build_output_paths(args.out, output_format, args.n, args.out_dir)
    downscaled = None
    if args.downscale_max_dim is not None:
        downscaled = [str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths]

    if args.dry_run:
        payload_preview = dict(payload)
        payload_preview["image"] = [str(p) for p in image_paths]
        if mask_path:
            payload_preview["mask"] = str(mask_path)
        if markup_path:
            payload_preview["markup"] = str(markup_path)
        _print_request(
            {
                "endpoint": "/v1/images/edits",
                "outputs": [str(p) for p in output_paths],
                "outputs_downscaled": downscaled,
                **payload_preview,
            }
        )
        return

    print(
        f"Calling Image API (edit) with {len(image_paths)} image(s).",
        file=sys.stderr,
    )
    started = time.time()
    client = _create_client()

    with _open_files(image_paths) as image_files, _open_optional_file(
        mask_path
    ) as mask_file, _open_optional_file(markup_path) as markup_file:
        request = dict(payload)
        request["image"] = image_files if len(image_files) > 1 else image_files[0]
        if mask_file is not None:
            request["mask"] = mask_file
        if markup_file is not None:
            request["markup"] = markup_file
        try:
            result = client.images.edit(**request)
            images = _collect_result(result, payload, output_paths[0], force=args.force, label="Result")
        except Exception as exc:
            message = _describe_api_error("Image edit", exc)
            if message is None:
                raise
            _die(message)

    elapsed = time.time() - started
    print(f"Edit completed in {elapsed:.1f}s.", file=sys.stderr)
    _decode_write_and_downscale(
        images,
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        output_format=output_format,
        alpha_snap=args.alpha_snap,
    )


def _open_files(paths: List[Path]):
    return _FileBundle(paths)


def _open_optional_file(path: Optional[Path]):
    if path is None:
        return _NullContext()
    return _SingleFile(path)


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


class _SingleFile:
    def __init__(self, path: Path):
        self._path = path
        self._handle = None

    def __enter__(self):
        self._handle = self._path.open("rb")
        return self._handle

    def __exit__(self, exc_type, exc, tb):
        if self._handle:
            try:
                self._handle.close()
            except Exception:
                pass
        return False


class _FileBundle:
    def __init__(self, paths: List[Path]):
        self._paths = paths
        self._handles: List[object] = []

    def __enter__(self):
        self._handles = [p.open("rb") for p in self._paths]
        return self._handles

    def __exit__(self, exc_type, exc, tb):
        for handle in self._handles:
            try:
                handle.close()
            except Exception:
                pass
        return False


def _add_shared_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default=_default_model(),
        help=f"GPT Image model (default: ${MODEL_ENV_VAR} if set, else {DEFAULT_MODEL})",
    )
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument(
        "--size",
        help=f"auto or WIDTHxHEIGHT (default {DEFAULT_SIZE})",
    )
    parser.add_argument("--quality", default=DEFAULT_QUALITY)
    parser.add_argument("--background")
    parser.add_argument("--output-format")
    parser.add_argument("--output-compression", type=int)
    parser.add_argument("--moderation")
    parser.add_argument("--complexity", help="low|medium|high: planning effort, separate from quality")
    parser.add_argument("--frame-count", type=int, help="GIF output only: 2-16 frames")
    parser.add_argument("--frame-duration-ms", type=int, help="GIF output only: 20-2000 ms per frame")
    parser.add_argument("--safety-identifier", help="Stable hashed end-user id, at most 128 characters")
    parser.add_argument("--out", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--out-dir")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--augment", dest="augment", action="store_true")
    parser.add_argument("--no-augment", dest="augment", action="store_false")
    parser.set_defaults(augment=True)

    # Prompt augmentation hints
    parser.add_argument("--use-case")
    parser.add_argument("--scene")
    parser.add_argument("--subject")
    parser.add_argument("--style")
    parser.add_argument("--composition")
    parser.add_argument("--lighting")
    parser.add_argument("--palette")
    parser.add_argument("--materials")
    parser.add_argument("--text")
    parser.add_argument("--constraints")
    parser.add_argument("--negative")

    # Post-processing (optional): generate an additional downscaled copy for fast web loading.
    parser.add_argument("--downscale-max-dim", type=int)
    parser.add_argument("--downscale-suffix", default=DEFAULT_DOWNSCALE_SUFFIX)
    parser.add_argument(
        "--alpha-snap",
        action="store_true",
        help=f"PNG/WebP only: after download, make pixels with alpha {ALPHA_SNAP_MIN}-254 fully opaque",
    )


def _add_svg_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--svg-model", help="SVG output only: model that authors the SVG")
    parser.add_argument(
        "--svg-reasoning-effort", help="SVG output only, needs --svg-model: reasoning effort of the SVG author"
    )


def _add_stream_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--stream", action="store_true", help="Stream the result (needs --n 1)")
    parser.add_argument("--partial-images", type=int, help="With --stream: 0-3 partial images to save")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate or edit images with GPT Image models through the OpenAI Image API"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen_parser = subparsers.add_parser("generate", help="Create a new image")
    _add_shared_args(gen_parser)
    _add_svg_args(gen_parser)
    _add_stream_args(gen_parser)
    gen_parser.set_defaults(func=_generate)

    batch_parser = subparsers.add_parser(
        "generate-batch",
        help="Generate multiple prompts concurrently (JSONL input)",
    )
    _add_shared_args(batch_parser)
    batch_parser.add_argument("--input", required=True, help="Path to JSONL file (one job per line)")
    batch_parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    batch_parser.add_argument("--max-attempts", type=int, default=3)
    batch_parser.add_argument("--fail-fast", action="store_true")
    _add_svg_args(batch_parser)
    batch_parser.set_defaults(func=_generate_batch)

    edit_parser = subparsers.add_parser("edit", help="Edit an existing image")
    _add_shared_args(edit_parser)
    edit_parser.add_argument("--image", action="append", required=True)
    edit_parser.add_argument("--mask")
    edit_parser.add_argument("--markup", help="Transparent PNG overlay on the first image; not with --mask")
    edit_parser.add_argument("--input-fidelity", help="Deprecated GPT Image 1.x models only")
    _add_stream_args(edit_parser)
    edit_parser.set_defaults(func=_edit)

    args = parser.parse_args()
    if args.n < 1 or args.n > 10:
        _die("--n must be between 1 and 10")
    if getattr(args, "concurrency", 1) < 1 or getattr(args, "concurrency", 1) > 25:
        _die("--concurrency must be between 1 and 25")
    if getattr(args, "max_attempts", 3) < 1 or getattr(args, "max_attempts", 3) > 10:
        _die("--max-attempts must be between 1 and 10")
    if args.output_compression is not None and not (0 <= args.output_compression <= 100):
        _die("--output-compression must be between 0 and 100")
    if args.command == "generate-batch" and not args.out_dir:
        _die("generate-batch requires --out-dir")
    if getattr(args, "downscale_max_dim", None) is not None and args.downscale_max_dim < 1:
        _die("--downscale-max-dim must be >= 1")

    _validate_model(args.model)
    _ensure_api_key(args.dry_run)
    if not args.dry_run:
        # Pillow is needed only after download. Check it before any request, so no paid call is
        # wasted and no batch job fails inside the event loop.
        if args.alpha_snap:
            _import_pillow("--alpha-snap")
        if args.downscale_max_dim is not None:
            _import_pillow("--downscale-max-dim")

    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
