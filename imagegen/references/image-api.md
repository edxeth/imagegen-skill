# Image API quick reference

This file is the API parameter reference for the CLI (`scripts/image_gen.py`). Read it when `references/cli.md` does not answer a parameter question.

These parameters describe the Image API and the bundled CLI.

Source: OpenAI Image API reference, image-generation guide, image-prompting guide, and deprecations page on developers.openai.com, checked 2026-11-06.

## Models
- `gpt-image-2.6-sunburst` (CLI default unless `IMAGE_GEN_MODEL` is set): detailed visual work and precise edits.
- `gpt-image-2.6-flare`: fast iteration; use it when the user asks for speed.
- `gpt-image-2.5-sunburst`, `gpt-image-2.5-flare`, `gpt-image-2`: still available.
- Deprecated: `gpt-image-1.5` and `gpt-image-1-mini` shut down on 2026-12-01. `gpt-image-1` was shut down on 2026-10-23. The CLI prints a warning when you select one of them.

## Endpoints
- Generate: `POST /v1/images/generations` (`client.images.generate(...)`)
- Edit: `POST /v1/images/edits` (`client.images.edit(...)`)

## Core parameters
- `prompt`: text prompt, at most 32,000 characters
- `model`: image model
- `n`: number of images (1-10)
- `size`: `auto` or `WIDTHxHEIGHT`; rules and popular sizes are in `references/cli.md` ("Model, quality, and complexity").
- `quality`: `low`, `medium`, `high`, `xhigh`, `max`, or `auto`. `xhigh` and `max` need GPT Image 2.5 or 2.6. Quality controls rendered detail such as texture and small lettering.
- `complexity`: `low`, `medium`, or `high`. It controls how much the model plans before it renders, separately from `quality`. GPT Image 2.6 uses `high` when it is omitted. It has no extra fee, but higher values can add latency. To compare complexity levels, keep `quality` fixed.
- `background`: output transparency behavior (`transparent`, `opaque`, or `auto`) for generated output; this is not the same thing as the prompt's visual scene/backdrop. Transparent output needs `png`, `webp`, or `svg`.
- `output_format`: `png` (default), `jpeg`, `webp`, `gif`, or `svg` (generate only). `jpeg` is faster than `png`.
- `output_compression`: 0-100 (jpeg/webp only)
- `moderation`: `auto` (default) or `low`
- `safety_identifier`: stable identifier for the end user, at most 128 characters. Hash a username or email address; do not send it in plain text.
- `stream` and `partial_images` (0-3): stream partial images while the final image renders. Each partial image adds 100 output tokens.

## GIF output (`output_format=gif`)
- Works on generate and edit. Use edit to animate from still reference images.
- `frame_count`: 2-16 (default 9). `frame_duration_ms`: 20-2000 per frame, rounded to 10 ms (default 200).
- `size` is the size of each frame: `auto` or `WIDTHxHEIGHT` with multiples of 16. Frame count, frame size, and `n` share one request pixel budget; if a request is too large, reduce one of them or use `auto`.
- Not supported with streaming, partial images, masks, or `output_compression`.
- Needs access to the GIF preview for the selected model.
- In the prompt, give the subject a starting pose to return to, and say whether the camera moves.

## SVG output (`output_format=svg`)
- Generate only, with `n=1`. The edit endpoint does not support SVG output or SVG input.
- The API renders a reference image (controlled by `quality` and `complexity`) and then an author model writes the SVG. The result is static vector content without scripts, external resources, or embedded raster images.
- Optional `svg` object: `{"model": "<author model>", "reasoning": {"effort": "none|minimal|low|medium|high|xhigh|max"}}`. Send it only when your project has SVG model selection; otherwise omit it.
- Not supported with streaming, partial images, or `output_compression`.
- Cost: the image model's rate for the reference image plus the author model's rate for its tokens.

## Edit-specific parameters
- `image`: one or more input images. For GPT Image models, you can provide up to 16 images.
- `mask`: optional mask image (PNG with alpha, same size and format as the first image)
- `markup`: optional transparent PNG overlay with the same pixel size as the first image, for example a circle drawn around the object to change. It applies only to the first image. Do not combine it with `mask`. Supported on GPT Image 2.5 and 2.6. Uses normal edit rates.
- `input_fidelity`: only for the deprecated 1.x models (`gpt-image-1.5`: `low` or `high`; `gpt-image-1-mini`: `low` only). Omit it for GPT Image 2 and newer: they always process input images at high fidelity, which can raise input-token cost.

## Output
- `data[]` list with `b64_json` per image (for SVG, base64 of UTF-8 XML)
- Streams emit `image_generation.partial_image` / `image_generation.completed` events (`image_edit.*` for edits).
- Responses and completed events also report `size`, `quality`, `background`, `output_format`, and token `usage` (plus `svg_usage` for the SVG author model). These response fields are optional. The CLI prints the ones present on a `Result:` line.
- The bundled `scripts/image_gen.py` CLI decodes `b64_json` and writes output files for you.

## Limits and notes
- Input images, masks, and markup must be under 50MB.
- Use the edits endpoint when the user requests changes to an existing image.
- Masking and markup are prompt-guided; exact shapes are not guaranteed. If part of an image must stay pixel-identical, composite the approved edit into the original.
- Large sizes and high quality increase latency and cost. Complex prompts can take up to 2 minutes.
- `complexity`, `markup`, GIF, and SVG need `openai` Python SDK 3.46.0 or newer. Upgrade the SDK if a call fails with an unexpected keyword argument.
- If a request fails because a specific option is unsupported by the selected GPT Image model, retry manually without that option.
