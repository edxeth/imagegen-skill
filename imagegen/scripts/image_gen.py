#!/usr/bin/env python3
"""Pi image generation CLI for OpenAI-compatible image endpoints.

The CLI intentionally talks to the HTTP API directly instead of depending on the
OpenAI Python SDK. That keeps the skill portable inside Pi installations and
works with direct upstream endpoints and generic proxies.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import mimetypes
import os
from pathlib import Path
import random
import re
import struct
import string
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib import error, request
from urllib.parse import urljoin, urlparse

from io import BytesIO

DEFAULT_MODEL = "gpt-image-2.5-flare"
DEFAULT_BASE_URL = "http://localhost:8317/v1"
DEFAULT_SIZE = "1024x1024"
DEFAULT_QUALITY = "auto"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_CONCURRENCY = 3
DEFAULT_DOWNSCALE_SUFFIX = "-web"

ALLOWED_QUALITIES = {"low", "medium", "high", "xhigh", "max", "auto"}
ALLOWED_BACKGROUNDS = {"transparent", "opaque", "auto", None}
ALLOWED_INPUT_FIDELITIES = {"low", "high", None}

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_BATCH_JOBS = 500
REQUEST_TIMEOUT_SECONDS = 300
TROUBLESHOOTING_PATH = Path(__file__).resolve().parents[1] / "references" / "troubleshooting.md"
MAX_DIAGNOSTIC_CHARS = 300
DIAGNOSTIC_SENSITIVE_KEYS = {
    "apikey",
    "authorization",
    "b64json",
    "base64",
    "image",
    "images",
    "password",
    "secret",
    "token",
}
DIAGNOSTIC_TOKEN_PATTERNS = (
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)\b(?:bearer|token|secret|api[_ -]?key)\s*[:=]\s*[^\s,;]+"),
)


class ImageGenError(RuntimeError):
    pass


def _die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def _troubleshooting_hint() -> str:
    return f" For endpoint/auth/output configuration, read {TROUBLESHOOTING_PATH}."


def _env(name: str) -> Optional[str]:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def _expand_path(path: str) -> Path:
    return Path(path).expanduser()


def _default_output_dir() -> Path:
    """Return the Pi-scoped default output directory.

    Precedence:
    1. PI_IMAGEGEN_OUTPUT_DIR: explicit override for this skill.
    2. PI_CODING_AGENT_DIR: Pi config dir override; use its parent as the Pi root.
    3. ~/.pi/generated_images: Pi's conventional user-owned location.
    """

    explicit = _env("PI_IMAGEGEN_OUTPUT_DIR")
    if explicit:
        return _expand_path(explicit)

    pi_config_dir = _env("PI_CODING_AGENT_DIR")
    if pi_config_dir:
        return _expand_path(pi_config_dir).resolve().parent / "generated_images"

    return Path.home() / ".pi" / "generated_images"


def _default_base_url() -> str:
    return _env("PI_IMAGEGEN_BASE_URL") or _env("OPENAI_BASE_URL") or DEFAULT_BASE_URL


def _default_model() -> str:
    return _env("PI_IMAGEGEN_MODEL") or DEFAULT_MODEL


def _default_api_key(base_url: str) -> str:
    key = _env("PI_IMAGEGEN_API_KEY") or _env("OPENAI_API_KEY")
    if key:
        return key
    parsed = urlparse(base_url)
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return "dummy"
    _warn(
        "No PI_IMAGEGEN_API_KEY or OPENAI_API_KEY is set; using Authorization: Bearer dummy."
    )
    return "dummy"


def _normalize_base_url(value: str) -> str:
    value = value.strip()
    if not value:
        _die("base URL cannot be empty")
    return value.rstrip("/") + "/"


def _endpoint(base_url: str, path: str) -> str:
    return urljoin(_normalize_base_url(base_url), path.lstrip("/"))


def _read_prompt(prompt: Optional[str], prompt_file: Optional[str]) -> str:
    if prompt and prompt_file:
        _die("Use --prompt or --prompt-file, not both.")
    if prompt_file:
        path = Path(prompt_file).expanduser()
        if not path.exists():
            _die(f"Prompt file not found: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            _die(f"Prompt file is empty: {path}")
        return text
    if prompt:
        text = prompt.strip()
        if text:
            return text
    _die("Missing prompt. Use --prompt or --prompt-file.")
    return ""  # unreachable


def _check_image_paths(paths: Iterable[str]) -> List[Path]:
    resolved: List[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            _die(f"Image file not found: {path}")
        if not path.is_file():
            _die(f"Image path is not a file: {path}")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            _warn(f"Image exceeds 50MB API limit: {path}")
        resolved.append(path)
    return resolved


def _normalize_output_format(fmt: Optional[str]) -> str:
    if not fmt:
        return DEFAULT_OUTPUT_FORMAT
    fmt = fmt.lower().strip()
    if fmt not in {"png", "jpeg", "jpg", "webp"}:
        _die("output-format must be png, jpeg, jpg, or webp.")
    return "jpeg" if fmt == "jpg" else fmt


def _validate_size(size: str) -> None:
    if size == "auto":
        return
    if not isinstance(size, str) or not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", size):
        _die(
            "size must be auto or WIDTHxHEIGHT with positive ASCII integer dimensions "
            "(for example 1024x1024)."
        )


def _validate_quality(quality: str) -> None:
    if quality not in ALLOWED_QUALITIES:
        _die("quality must be one of low, medium, high, xhigh, max, or auto.")


def _validate_background(background: Optional[str]) -> None:
    if background not in ALLOWED_BACKGROUNDS:
        _die("background must be one of transparent, opaque, or auto.")


def _validate_input_fidelity(input_fidelity: Optional[str]) -> None:
    if input_fidelity not in ALLOWED_INPUT_FIDELITIES:
        _die("input-fidelity must be one of low or high.")


def _validate_model(model: str) -> None:
    if not model or not model.strip():
        _die("model cannot be empty.")


def _validate_transparency(background: Optional[str], output_format: str) -> None:
    if background == "transparent" and output_format not in {"png", "webp"}:
        _die("transparent background requires output-format png or webp.")


def _validate_generate_payload(payload: Dict[str, Any]) -> None:
    _validate_model(str(payload.get("model", DEFAULT_MODEL)))
    n = int(payload.get("n", 1))
    if n < 1 or n > 10:
        _die("n must be between 1 and 10")
    size = payload.get("size", DEFAULT_SIZE)
    quality = payload.get("quality", DEFAULT_QUALITY)
    _validate_size(size if isinstance(size, str) else "")
    _validate_quality(quality if isinstance(quality, str) else "")
    _validate_background(payload.get("background"))
    oc = payload.get("output_compression")
    if oc is not None and not (0 <= int(oc) <= 100):
        _die("output_compression must be between 0 and 100")


def _slugify(value: str, max_len: int = 60) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:max_len].strip("-") if value else "image"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    _die(f"Could not find a free output filename near: {path}")
    return path


def _build_output_paths(
    *,
    prompt: str,
    output_format: str,
    count: int,
    out: Optional[str],
    out_dir: Optional[str],
) -> List[Path]:
    ext = "." + output_format

    if out_dir:
        out_base = Path(out_dir).expanduser()
        out_base.mkdir(parents=True, exist_ok=True)
        return [out_base / f"image_{i}{ext}" for i in range(1, count + 1)]

    if out:
        out_path = Path(out).expanduser()
        if out_path.exists() and out_path.is_dir():
            out_path.mkdir(parents=True, exist_ok=True)
            return [out_path / f"image_{i}{ext}" for i in range(1, count + 1)]
        if out_path.suffix == "":
            out_path = out_path.with_suffix(ext)
        elif out_path.suffix.lstrip(".").lower() != output_format:
            _warn(
                f"Output extension {out_path.suffix} does not match output-format {output_format}."
            )
        if count == 1:
            return [out_path]
        return [
            out_path.with_name(f"{out_path.stem}-{i}{out_path.suffix}")
            for i in range(1, count + 1)
        ]

    default_dir = _default_output_dir()
    default_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_timestamp()}-{_slugify(prompt[:100])}"
    if count == 1:
        return [_unique_path(default_dir / f"{stem}{ext}")]
    return [_unique_path(default_dir / f"{stem}-{i}{ext}") for i in range(1, count + 1)]


def _derive_downscale_path(path: Path, suffix: str) -> Path:
    if suffix and not suffix.startswith("-") and not suffix.startswith("_"):
        suffix = "-" + suffix
    return path.with_name(f"{path.stem}{suffix}{path.suffix}")


def _valid_dimensions(width: int, height: int) -> Optional[Tuple[int, int]]:
    if width < 1 or height < 1:
        return None
    return width, height


def _png_dimensions(image_bytes: bytes) -> Optional[Tuple[int, int]]:
    if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n") or len(image_bytes) < 24:
        return None
    if image_bytes[12:16] != b"IHDR":
        return None
    return _valid_dimensions(*struct.unpack(">II", image_bytes[16:24]))


def _jpeg_dimensions(image_bytes: bytes) -> Optional[Tuple[int, int]]:
    if not image_bytes.startswith(b"\xff\xd8"):
        return None

    start_of_frame_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    offset = 2
    while offset + 1 < len(image_bytes):
        if image_bytes[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(image_bytes) and image_bytes[offset] == 0xFF:
            offset += 1
        if offset >= len(image_bytes):
            return None
        marker = image_bytes[offset]
        offset += 1
        if marker == 0xDA:
            return None
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(image_bytes):
            return None
        segment_length = struct.unpack(">H", image_bytes[offset : offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(image_bytes):
            return None
        if marker in start_of_frame_markers and segment_length >= 7:
            height, width = struct.unpack(">HH", image_bytes[offset + 3 : offset + 7])
            return _valid_dimensions(width, height)
        offset += segment_length
    return None


def _webp_dimensions(image_bytes: bytes) -> Optional[Tuple[int, int]]:
    if not (image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP"):
        return None
    if len(image_bytes) < 20:
        return None
    chunk_type = image_bytes[12:16]
    if chunk_type == b"VP8X" and len(image_bytes) >= 30:
        width = 1 + int.from_bytes(image_bytes[24:27], "little")
        height = 1 + int.from_bytes(image_bytes[27:30], "little")
        return _valid_dimensions(width, height)
    if chunk_type == b"VP8L" and len(image_bytes) >= 25:
        if image_bytes[20] != 0x2F:
            return None
        width = 1 + (image_bytes[21] | ((image_bytes[22] & 0x3F) << 8))
        height = 1 + ((image_bytes[22] >> 6) | (image_bytes[23] << 2) | ((image_bytes[24] & 0x0F) << 10))
        return _valid_dimensions(width, height)
    if chunk_type == b"VP8 " and len(image_bytes) >= 30:
        if image_bytes[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(image_bytes[26:28], "little") & 0x3FFF
        height = int.from_bytes(image_bytes[28:30], "little") & 0x3FFF
        return _valid_dimensions(width, height)
    return None


def _image_dimensions(image_bytes: bytes) -> Optional[Tuple[int, int]]:
    for reader in (_png_dimensions, _jpeg_dimensions, _webp_dimensions):
        dimensions = reader(image_bytes)
        if dimensions is not None:
            return dimensions
    return None


def _downscale_image_bytes(image_bytes: bytes, *, max_dim: int, output_format: str) -> bytes:
    try:
        from PIL import Image
    except Exception:
        _die("Downscaling requires Pillow. Install it in the active Python environment.")

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


def _write_images(
    image_bytes: Sequence[bytes],
    outputs: Sequence[Path],
    *,
    force: bool,
    downscale_max_dim: Optional[int],
    downscale_suffix: str,
    output_format: str,
) -> List[Dict[str, Any]]:
    saved: List[Dict[str, Any]] = []
    for idx, raw in enumerate(image_bytes):
        if idx >= len(outputs):
            break
        out_path = outputs[idx]
        if out_path.exists() and not force:
            _die(f"Output already exists: {out_path} (use --force to overwrite)")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(raw)
        saved.append(
            {
                "index": idx + 1,
                "label": str(idx + 1),
                "path": out_path,
                "dimensions": _image_dimensions(raw),
            }
        )

        if downscale_max_dim is None:
            continue
        derived = _derive_downscale_path(out_path, downscale_suffix)
        if derived.exists() and not force:
            _die(f"Output already exists: {derived} (use --force to overwrite)")
        resized = _downscale_image_bytes(raw, max_dim=downscale_max_dim, output_format=output_format)
        derived.write_bytes(resized)
        saved.append(
            {
                "index": idx + 1,
                "label": f"{idx + 1} downscaled",
                "path": derived,
                "dimensions": _image_dimensions(resized),
            }
        )
    return saved


def _ensure_output_paths_available(
    outputs: Sequence[Path],
    *,
    force: bool,
    downscale_max_dim: Optional[int],
    downscale_suffix: str,
) -> None:
    if force:
        return
    candidates: List[Path] = list(outputs)
    if downscale_max_dim is not None:
        candidates.extend(_derive_downscale_path(path, downscale_suffix) for path in outputs)
    for path in candidates:
        if path.exists():
            _die(f"Output already exists: {path} (use --force to overwrite)")


def _print_saved_paths(saved: Sequence[Dict[str, Any]]) -> None:
    for item in saved:
        print(f"Wrote {item['path']}")


def _response_data_item(response: Dict[str, Any], index: int) -> Dict[str, Any]:
    data = response.get("data")
    if isinstance(data, list) and index < len(data) and isinstance(data[index], dict):
        return data[index]
    return {}


def _response_metadata(response: Dict[str, Any], index: int, key: str) -> str:
    item = _response_data_item(response, index)
    value = item.get(key)
    if value is None or value == "":
        value = response.get(key)
    if isinstance(value, (str, int, float, bool)) and str(value):
        return str(value)
    return "unavailable"


def _compact_response_for_reporting(response: Dict[str, Any]) -> Dict[str, Any]:
    compact: Dict[str, Any] = {}
    for key in ("model", "size"):
        value = response.get(key)
        if isinstance(value, (str, int, float, bool)):
            compact[key] = value

    data = response.get("data")
    if isinstance(data, list):
        compact_data: List[Dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            compact_item: Dict[str, Any] = {}
            for key in ("model", "size", "revised_prompt"):
                value = item.get(key)
                if isinstance(value, (str, int, float, bool)):
                    compact_item[key] = value
            compact_data.append(compact_item)
        compact["data"] = compact_data
    return compact


def _format_dimensions(dimensions: Optional[Tuple[int, int]]) -> str:
    if dimensions is None:
        return "unavailable"
    return f"{dimensions[0]}x{dimensions[1]}"


def _print_image_reports(
    response: Dict[str, Any],
    saved: Sequence[Dict[str, Any]],
    *,
    requested_model: str,
    requested_size: str,
    prefix: str = "",
) -> None:
    for item in saved:
        index = int(item["index"]) - 1
        print(
            f"{prefix}Image {item['label']}: path={item['path']} "
            f"requested_model={requested_model} "
            f"backend_model={_response_metadata(response, index, 'model')} "
            f"requested_size={requested_size} "
            f"backend_size={_response_metadata(response, index, 'size')} "
            f"measured_size={_format_dimensions(item['dimensions'])}"
        )


def _fields_from_args(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    return {
        "use_case": getattr(args, "use_case", None),
        "asset_type": getattr(args, "asset_type", None),
        "input_images": getattr(args, "input_images", None),
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


def _augment_prompt(args: argparse.Namespace, prompt: str) -> str:
    return _augment_prompt_fields(args.augment, prompt, _fields_from_args(args))


def _augment_prompt_fields(augment: bool, prompt: str, fields: Dict[str, Optional[str]]) -> str:
    if not augment:
        return prompt

    sections: List[str] = []
    if fields.get("use_case"):
        sections.append(f"Use case: {fields['use_case']}")
    if fields.get("asset_type"):
        sections.append(f"Asset type: {fields['asset_type']}")
    sections.append(f"Primary request: {prompt}")
    if fields.get("input_images"):
        sections.append(f"Input images: {fields['input_images']}")
    if fields.get("scene"):
        sections.append(f"Scene/backdrop: {fields['scene']}")
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


def _json_request(
    *,
    base_url: str,
    api_key: str,
    path: str,
    payload: Dict[str, Any],
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    url = _endpoint(base_url, path)
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    return _send_request(req, timeout=timeout)


def _get_json(*, base_url: str, api_key: str, path: str, timeout: int = 60) -> Dict[str, Any]:
    url = _endpoint(base_url, path)
    req = request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    return _send_request(req, timeout=timeout)


def _diagnostic_preview(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern in DIAGNOSTIC_TOKEN_PATTERNS:
        text = pattern.sub("[redacted]", text)
    if len(text) > MAX_DIAGNOSTIC_CHARS:
        return text[:MAX_DIAGNOSTIC_CHARS] + "..."
    return text or "empty response"


def _diagnostic_value(value: Any, depth: int = 0) -> Any:
    if depth > 2:
        return "[omitted]"
    if isinstance(value, dict):
        safe: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized_key = re.sub(r"[^a-z0-9]", "", key_text.lower())
            safe[key_text] = (
                "[omitted]"
                if normalized_key in DIAGNOSTIC_SENSITIVE_KEYS
                else _diagnostic_value(item, depth + 1)
            )
        return safe
    if isinstance(value, list):
        return [_diagnostic_value(item, depth + 1) for item in value[:10]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _format_error_detail(detail: Any) -> str:
    if isinstance(detail, dict):
        message = detail.get("message")
        if message:
            detail = message
        else:
            detail = json.dumps(_diagnostic_value(detail), sort_keys=True, separators=(",", ":"))
    elif not isinstance(detail, (str, int, float, bool)):
        detail = json.dumps(_diagnostic_value(detail), sort_keys=True, separators=(",", ":"))
    return _diagnostic_preview(str(detail))


def _safe_error_detail(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _diagnostic_preview(raw.decode("utf-8", errors="replace"))
    if isinstance(payload, dict):
        detail = payload.get("error") or payload.get("message")
        if detail is not None:
            return _format_error_detail(detail)
        keys = ", ".join(sorted(str(key) for key in payload.keys()))
        return f"response fields: {keys or 'none'}"
    return _format_error_detail(payload)


def _send_request(req: request.Request, *, timeout: int) -> Dict[str, Any]:
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except error.HTTPError as exc:
        raw = exc.read()
        detail = _safe_error_detail(raw)
        raise ImageGenError(
            f"HTTP {exc.code} from image endpoint: {detail}.{_troubleshooting_hint()}"
        ) from exc
    except error.URLError as exc:
        raise ImageGenError(
            f"Could not reach image endpoint: {exc}.{_troubleshooting_hint()}"
        ) from exc

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = _diagnostic_preview(raw.decode("utf-8", errors="replace"))
        raise ImageGenError(
            f"Image endpoint returned a non-JSON response: {preview}.{_troubleshooting_hint()}"
        ) from exc


def _multipart_request(
    *,
    base_url: str,
    api_key: str,
    path: str,
    fields: Dict[str, Any],
    files: Sequence[Tuple[str, Path]],
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    boundary = "----pi-imagegen-" + "".join(random.choice(string.ascii_letters) for _ in range(24))
    body = _multipart_body(boundary, fields, files)
    req = request.Request(
        _endpoint(base_url, path),
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    return _send_request(req, timeout=timeout)


def _multipart_body(boundary: str, fields: Dict[str, Any], files: Sequence[Tuple[str, Path]]) -> bytes:
    chunks: List[bytes] = []

    def add(line: str) -> None:
        chunks.append(line.encode("utf-8"))

    for name, value in fields.items():
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            add(f"--{boundary}\r\n")
            add(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
            add(str(item))
            add("\r\n")

    for field_name, path in files:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        add(f"--{boundary}\r\n")
        add(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{path.name}"\r\n'
        )
        add(f"Content-Type: {mime}\r\n\r\n")
        chunks.append(path.read_bytes())
        add("\r\n")

    add(f"--{boundary}--\r\n")
    return b"".join(chunks)


def _response_to_image_bytes(response: Dict[str, Any]) -> List[bytes]:
    if "error" in response:
        raise ImageGenError(f"Image endpoint error: {_format_error_detail(response['error'])}")
    data = response.get("data")
    if not isinstance(data, list) or not data:
        keys = ", ".join(sorted(str(key) for key in response.keys()))
        raise ImageGenError(f"Image endpoint returned no image data. Response fields: {keys or 'none'}.")

    images: List[bytes] = []
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ImageGenError(f"Unexpected image data item {idx}.")
        if item.get("b64_json"):
            try:
                images.append(base64.b64decode(item["b64_json"]))
            except Exception as exc:
                raise ImageGenError(f"Invalid b64_json for image {idx}") from exc
            continue
        if item.get("url"):
            images.append(_fetch_image_url(str(item["url"])))
            continue
        raise ImageGenError(f"Image {idx} has neither b64_json nor url.")
    return images


def _fetch_image_url(url: str) -> bytes:
    req = request.Request(url, headers={"Accept": "image/*,*/*"})
    try:
        with request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except Exception as exc:
        parsed = urlparse(url)
        hostname = parsed.hostname or "<host>"
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            hostname = f"{hostname}:{port}"
        safe_url = (
            f"{parsed.scheme}://{hostname}{parsed.path}"
            if parsed.scheme
            else "<image URL>"
        )
        detail = f"HTTP {exc.code}" if isinstance(exc, error.HTTPError) else type(exc).__name__
        raise ImageGenError(f"Could not download image URL {safe_url}: {detail}") from exc


def _print_request(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _common_payload(args: argparse.Namespace, prompt: str) -> Dict[str, Any]:
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
    }
    return {k: v for k, v in payload.items() if v is not None}


def _run_with_error_boundary(func, args: argparse.Namespace) -> None:
    try:
        func(args)
    except ImageGenError as exc:
        _die(str(exc))


def _generate(args: argparse.Namespace) -> None:
    raw_prompt = _read_prompt(args.prompt, args.prompt_file)
    prompt = _augment_prompt(args, raw_prompt)
    output_format = _normalize_output_format(args.output_format)
    _validate_transparency(args.background, output_format)

    payload = _common_payload(args, prompt)
    payload["output_format"] = output_format
    _validate_generate_payload(payload)

    output_paths = _build_output_paths(
        prompt=raw_prompt,
        output_format=output_format,
        count=args.n,
        out=args.out,
        out_dir=args.out_dir,
    )
    downscaled = None
    if args.downscale_max_dim is not None:
        downscaled = [str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths]

    if args.dry_run:
        _print_request(
            {
                "endpoint": _endpoint(args.base_url, "/images/generations"),
                "outputs": [str(p) for p in output_paths],
                "outputs_downscaled": downscaled,
                **payload,
            }
        )
        return

    _ensure_output_paths_available(
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
    )
    print("Calling image generation endpoint. This can take a couple of minutes.", file=sys.stderr)
    started = time.time()
    response = _json_request(
        base_url=args.base_url,
        api_key=args.api_key,
        path="/images/generations",
        payload=payload,
        timeout=args.timeout,
    )
    elapsed = time.time() - started
    print(f"Generation completed in {elapsed:.1f}s.", file=sys.stderr)

    images = _response_to_image_bytes(response)
    saved = _write_images(
        images,
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        output_format=output_format,
    )
    _print_saved_paths(saved)
    _print_image_reports(
        response,
        saved,
        requested_model=str(payload["model"]),
        requested_size=str(payload["size"]),
    )
    _print_revised_prompts(response)


def _edit(args: argparse.Namespace) -> None:
    raw_prompt = _read_prompt(args.prompt, args.prompt_file)
    prompt = _augment_prompt(args, raw_prompt)
    image_paths = _check_image_paths(args.image)
    mask_path = Path(args.mask).expanduser() if args.mask else None
    if mask_path:
        if not mask_path.exists():
            _die(f"Mask file not found: {mask_path}")
        if mask_path.suffix.lower() != ".png":
            _warn(f"Mask should be a PNG with an alpha channel: {mask_path}")
        if mask_path.stat().st_size > MAX_IMAGE_BYTES:
            _warn(f"Mask exceeds 50MB API limit: {mask_path}")

    output_format = _normalize_output_format(args.output_format)
    _validate_transparency(args.background, output_format)
    _validate_input_fidelity(args.input_fidelity)

    payload = _common_payload(args, prompt)
    payload["output_format"] = output_format
    if args.input_fidelity is not None:
        payload["input_fidelity"] = args.input_fidelity
    _validate_generate_payload(payload)

    output_paths = _build_output_paths(
        prompt=raw_prompt,
        output_format=output_format,
        count=args.n,
        out=args.out,
        out_dir=args.out_dir,
    )
    downscaled = None
    if args.downscale_max_dim is not None:
        downscaled = [str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths]

    if args.dry_run:
        preview = dict(payload)
        preview["image"] = [str(p) for p in image_paths]
        if mask_path:
            preview["mask"] = str(mask_path)
        _print_request(
            {
                "endpoint": _endpoint(args.base_url, "/images/edits"),
                "outputs": [str(p) for p in output_paths],
                "outputs_downscaled": downscaled,
                **preview,
            }
        )
        return

    _ensure_output_paths_available(
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
    )
    print(f"Calling image edit endpoint with {len(image_paths)} image(s).", file=sys.stderr)
    files: List[Tuple[str, Path]] = [("image", p) for p in image_paths]
    if mask_path:
        files.append(("mask", mask_path))
    started = time.time()
    response = _multipart_request(
        base_url=args.base_url,
        api_key=args.api_key,
        path="/images/edits",
        fields=payload,
        files=files,
        timeout=args.timeout,
    )
    elapsed = time.time() - started
    print(f"Edit completed in {elapsed:.1f}s.", file=sys.stderr)

    images = _response_to_image_bytes(response)
    saved = _write_images(
        images,
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        output_format=output_format,
    )
    _print_saved_paths(saved)
    _print_image_reports(
        response,
        saved,
        requested_model=str(payload["model"]),
        requested_size=str(payload["size"]),
    )
    _print_revised_prompts(response)


def _print_revised_prompts(response: Dict[str, Any], prefix: str = "") -> None:
    data = response.get("data")
    if not isinstance(data, list):
        return
    for idx, item in enumerate(data, start=1):
        if isinstance(item, dict) and item.get("revised_prompt"):
            print(f"{prefix}Revised prompt {idx}: {item['revised_prompt']}", file=sys.stderr)


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
    p = Path(path).expanduser()
    if not p.exists():
        _die(f"Input file not found: {p}")
    jobs: List[Dict[str, Any]] = []
    for line_no, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item: Any = json.loads(line) if line.startswith("{") else line
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
        base = Path(explicit_out).expanduser()
        if base.suffix == "":
            base = base.with_suffix(ext)
        elif base.suffix.lstrip(".").lower() != output_format:
            _warn(
                f"Job {idx}: output extension {base.suffix} does not match output-format {output_format}."
            )
        base = out_dir / base.name
    else:
        base = out_dir / f"{idx:03d}-{_slugify(prompt[:80])}{ext}"
    if n == 1:
        return [base]
    return [base.with_name(f"{base.stem}-{i}{base.suffix}") for i in range(1, n + 1)]


def _run_generate_batch(args: argparse.Namespace) -> None:
    jobs = _read_jobs_jsonl(args.input)
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else _default_output_dir() / f"batch-{_timestamp()}"

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
    }

    prepared: List[Tuple[int, Dict[str, Any], List[Path], str]] = []
    for i, job in enumerate(jobs, start=1):
        prompt = str(job["prompt"]).strip()
        fields = _merge_non_null(base_fields, job.get("fields", {}))
        fields = _merge_non_null(fields, {k: job.get(k) for k in base_fields.keys()})
        augmented = _augment_prompt_fields(args.augment, prompt, fields)

        payload = dict(base_payload)
        payload["prompt"] = augmented
        payload = _merge_non_null(payload, {k: job.get(k) for k in base_payload.keys()})
        payload = {k: v for k, v in payload.items() if v is not None}

        output_format = _normalize_output_format(payload.get("output_format"))
        payload["output_format"] = output_format
        _validate_generate_payload(payload)
        _validate_transparency(payload.get("background"), output_format)
        n = int(payload.get("n", 1))
        outputs = _job_output_paths(
            out_dir=out_dir,
            output_format=output_format,
            idx=i,
            prompt=prompt,
            n=n,
            explicit_out=job.get("out"),
        )
        prepared.append((i, payload, outputs, output_format))

    if args.dry_run:
        for i, payload, outputs, output_format in prepared:
            downscaled = None
            if args.downscale_max_dim is not None:
                downscaled = [str(_derive_downscale_path(p, args.downscale_suffix)) for p in outputs]
            _print_request(
                {
                    "endpoint": _endpoint(args.base_url, "/images/generations"),
                    "job": i,
                    "outputs": [str(p) for p in outputs],
                    "outputs_downscaled": downscaled,
                    **payload,
                }
            )
        return

    for _, _, outputs, _ in prepared:
        _ensure_output_paths_available(
            outputs,
            force=args.force,
            downscale_max_dim=args.downscale_max_dim,
            downscale_suffix=args.downscale_suffix,
        )

    any_failed = False

    def run_job(
        item: Tuple[int, Dict[str, Any], List[Path], str]
    ) -> Tuple[int, Optional[str], float, Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        i, payload, outputs, output_format = item
        started = time.time()
        try:
            response = _json_request(
                base_url=args.base_url,
                api_key=args.api_key,
                path="/images/generations",
                payload=payload,
                timeout=args.timeout,
            )
            elapsed = time.time() - started
            images = _response_to_image_bytes(response)
            saved = _write_images(
                images,
                outputs,
                force=args.force,
                downscale_max_dim=args.downscale_max_dim,
                downscale_suffix=args.downscale_suffix,
                output_format=output_format,
            )
            response = _compact_response_for_reporting(response)
            return i, None, elapsed, response, saved
        except Exception as exc:
            if args.fail_fast:
                raise
            return i, str(exc), time.time() - started, None, []

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(run_job, item) for item in prepared]
        for fut in futures:
            i, err, elapsed, response, saved = fut.result()
            job_label = f"[job {i}/{len(prepared)}]"
            if err:
                print(f"{job_label} failed: {err}", file=sys.stderr)
                any_failed = True
                continue
            print(f"{job_label} completed in {elapsed:.1f}s", file=sys.stderr)
            _print_saved_paths(saved)
            _print_image_reports(
                response or {},
                saved,
                requested_model=str(prepared[i - 1][1]["model"]),
                requested_size=str(prepared[i - 1][1]["size"]),
                prefix=f"{job_label} ",
            )
            _print_revised_prompts(response or {}, prefix=f"{job_label} ")

    if any_failed:
        raise SystemExit(1)


def _models(args: argparse.Namespace) -> None:
    response = _get_json(base_url=args.base_url, api_key=args.api_key, path="/models", timeout=60)
    if args.raw:
        print(json.dumps(response, indent=2, sort_keys=True))
        return
    data = response.get("data", [])
    if not isinstance(data, list):
        _print_request(response)
        return
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            print(item["id"])


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url",
        default=_default_base_url(),
        help="OpenAI-compatible base URL. Env: PI_IMAGEGEN_BASE_URL, then OPENAI_BASE_URL. Default: http://localhost:8317/v1",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Bearer token. Env: PI_IMAGEGEN_API_KEY, then OPENAI_API_KEY. Defaults to dummy for local proxies.",
    )
    parser.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT_SECONDS)


def _add_shared_args(parser: argparse.ArgumentParser) -> None:
    _add_connection_args(parser)
    parser.add_argument("--model", default=_default_model())
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--size", default=DEFAULT_SIZE)
    parser.add_argument("--quality", default=DEFAULT_QUALITY)
    parser.add_argument("--background")
    parser.add_argument("--output-format")
    parser.add_argument("--output-compression", type=int)
    parser.add_argument("--moderation")
    parser.add_argument("--out", help="Output file path. Defaults to PI_IMAGEGEN_OUTPUT_DIR or ~/.pi/generated_images.")
    parser.add_argument("--out-dir", help="Output directory; writes image_1.<ext>, image_2.<ext>, ...")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--augment", dest="augment", action="store_true")
    parser.add_argument("--no-augment", dest="augment", action="store_false")
    parser.set_defaults(augment=True)

    parser.add_argument("--use-case")
    parser.add_argument("--asset-type")
    parser.add_argument("--input-images")
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

    parser.add_argument("--downscale-max-dim", type=int)
    parser.add_argument("--downscale-suffix", default=DEFAULT_DOWNSCALE_SUFFIX)


def _prepare_args(args: argparse.Namespace) -> argparse.Namespace:
    args.base_url = _normalize_base_url(args.base_url)
    args.api_key = args.api_key or _default_api_key(args.base_url)
    if getattr(args, "n", 1) < 1 or getattr(args, "n", 1) > 10:
        _die("--n must be between 1 and 10")
    if getattr(args, "concurrency", 1) < 1 or getattr(args, "concurrency", 1) > 25:
        _die("--concurrency must be between 1 and 25")
    if args.timeout < 1:
        _die("--timeout must be >= 1")
    if getattr(args, "output_compression", None) is not None and not (0 <= args.output_compression <= 100):
        _die("--output-compression must be between 0 and 100")
    if getattr(args, "downscale_max_dim", None) is not None and args.downscale_max_dim < 1:
        _die("--downscale-max-dim must be >= 1")
    if hasattr(args, "size"):
        _validate_size(args.size)
    if hasattr(args, "quality"):
        _validate_quality(args.quality)
    if hasattr(args, "background"):
        _validate_background(args.background)
    if hasattr(args, "model"):
        _validate_model(args.model)
    return args


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate or edit images via an OpenAI-compatible image endpoint for Pi."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen_parser = subparsers.add_parser("generate", help="Create a new image")
    _add_shared_args(gen_parser)
    gen_parser.set_defaults(func=_generate)

    batch_parser = subparsers.add_parser(
        "generate-batch", help="Generate multiple prompts from a JSONL file"
    )
    _add_shared_args(batch_parser)
    batch_parser.add_argument("--input", required=True, help="Path to JSONL file, one job per line")
    batch_parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    batch_parser.add_argument("--fail-fast", action="store_true")
    batch_parser.set_defaults(func=_run_generate_batch)

    edit_parser = subparsers.add_parser("edit", help="Edit an existing image")
    _add_shared_args(edit_parser)
    edit_parser.add_argument("--image", action="append", required=True)
    edit_parser.add_argument("--mask")
    edit_parser.add_argument("--input-fidelity")
    edit_parser.set_defaults(func=_edit)

    models_parser = subparsers.add_parser("models", help="List models exposed by the endpoint")
    _add_connection_args(models_parser)
    models_parser.add_argument("--raw", action="store_true")
    models_parser.set_defaults(func=_models)

    args = _prepare_args(parser.parse_args())
    _run_with_error_boundary(args.func, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
