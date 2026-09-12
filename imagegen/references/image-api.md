# OpenAI-compatible Image API Notes

This skill targets OpenAI-compatible image endpoints through `scripts/image_gen.py`. The endpoint can be a direct upstream service or a generic proxy.

For generation and edit, the configured endpoint must expose:

```text
POST /v1/images/generations
POST /v1/images/edits
```

The optional `models` command uses `GET /v1/models`. Generation and edit do not call this route first.

## Endpoint configuration

Endpoint/auth defaults and precedence live in `references/troubleshooting.md`.

## Parameters

Common parameters:

| CLI option | API field | Notes |
|---|---|---|
| `--model` | `model` | Default `gpt-image-2.5-flare`. Provider-defined names are accepted. |
| `--prompt` / `--prompt-file` | `prompt` | Required for generate/edit. |
| `--n` | `n` | 1-10. |
| `--size` | `size` | `auto` or positive ASCII `WIDTHxHEIGHT`. The backend enforces size support. |
| `--quality` | `quality` | `low`, `medium`, `high`, `xhigh`, `max`, or `auto`. The backend enforces quality support. |
| `--background` | `background` | `transparent`, `opaque`, or `auto`; transparent requires PNG or WebP. |
| `--output-format` | `output_format` | `png`, `jpeg`, or `webp`; default `png`. |
| `--output-compression` | `output_compression` | 0-100 when supported. |
| `--moderation` | `moderation` | Passed through when set. |

Edit-only parameters:

| CLI option | API field | Notes |
|---|---|---|
| `--image` | multipart `image` | Repeat for multiple input images. |
| `--mask` | multipart `mask` | PNG mask with alpha channel when supported. |
| `--input-fidelity` | `input_fidelity` | `low` or `high` when supported. |

## Model-specific selection guidance

The rules in `SKILL.md` control when to infer settings or ask the user. The limits below describe OpenAI's documented GPT Image 2.5 API, not every compatible provider or alias. The CLI checks size syntax only and leaves provider-specific limits to the endpoint.

For GPT Image 2.5 Flare and Sunburst, `xhigh` and `max` are additional quality settings. Use them to address an unmet requirement within the user's cost and latency limits. Do not assume that the same quality label produces equal results or costs across models. Do not apply these settings to older models or unknown aliases without evidence of support.

When selecting custom dimensions for the documented OpenAI GPT Image 2.5 API, use these limits:

- Make both dimensions multiples of 16 pixels.
- Keep each dimension at or below 3840 pixels.
- Keep the longer dimension at most three times the shorter dimension.
- Keep the total pixel count between 655,360 and 8,294,400.

Use the target layout to choose an aspect ratio, the width relative to the height. Common starting sizes are `1024x1024` for square, `1536x1024` for landscape, and `1024x1536` for portrait. Use a custom canvas such as `1536x864` for a known 16:9 layout. Do not infer a need for 4K from the word "hero" or "final" alone.

OpenAI lists `3840x2160` and `2160x3840` as 4K options. Outputs above 3,686,400 total pixels (`2560x1440`) are experimental. Explain this limit before an applicable request. Do not silently change a requested canvas if the endpoint rejects it or returns different dimensions.

These notes follow the [OpenAI image-prompting guide](https://developers.openai.com/api/docs/guides/image-prompting), checked September 12, 2026. Provider capabilities and pricing can change. Consult the selected provider's current documentation when its limits are unknown. A successful model listing or request does not prove that the backend honors every parameter.

## Response handling

The CLI accepts either response shape:

```json
{"data":[{"b64_json":"..."}]}
```

or:

```json
{"data":[{"url":"https://..."}]}
```

It writes decoded image bytes to the resolved output path and prints the path.

If the endpoint returns `revised_prompt`, the CLI prints it to stderr for traceability.

The CLI reports requested `model` and `size` values separately from backend-reported values. It does not replace a missing backend value with the request. It measures saved PNG, JPEG, and WebP dimensions from image bytes when possible. C2PA metadata alone is not definitive proof of the actual served model.

The `models` command is optional discovery. Generation does not require it, and the client does not apply a model or billing fallback when a request fails.

## Direct curl tests

Direct generation/edit curl tests live in `references/troubleshooting.md`.

## Invariants for safe edits

Edits drift unless the prompt states the containment boundary. Repeat the invariant in every edit prompt:

```text
Change only <target region/object>. Keep <identity/pose/layout/lighting/text/edges> unchanged. No extra objects. No watermark.
```

For project assets, keep the original file untouched and write a sibling output such as `hero-edited.png` or `product-background-v2.png`.
