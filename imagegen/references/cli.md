# Image Generation CLI for Pi

Use the bundled CLI for all image generation and editing work in this skill.

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" <command> [options]
```

The CLI uses direct HTTP requests to an OpenAI-compatible image endpoint. It does not require the OpenAI Python SDK. The endpoint can be a direct upstream service or a generic proxy.

## Configuration

Endpoint precedence:
1. `--base-url`
2. `PI_IMAGEGEN_BASE_URL`
3. `OPENAI_BASE_URL`
4. `http://localhost:8317/v1`

API key precedence:
1. `--api-key`
2. `PI_IMAGEGEN_API_KEY`
3. `OPENAI_API_KEY`
4. `dummy` for local proxies

Output directory precedence:
1. `--out` / `--out-dir`
2. `PI_IMAGEGEN_OUTPUT_DIR`
3. sibling `generated_images` next to `PI_CODING_AGENT_DIR`
4. `~/.pi/generated_images`

For current endpoint/model/auth/output defaults, read `references/troubleshooting.md`.

## Commands

- `models` — optionally list models exposed by the endpoint.
- `generate` — create one or more images from a prompt.
- `edit` — edit one or more input images.
- `generate-batch` — generate from a JSONL file.

## Health check

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" models
```

Generation does not call this command first. If this command fails, generation can still work when the image routes work.

## Generate

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" generate \
  --prompt "A tiny red cube on a plain white background, no text"
```

Useful options:

```bash
--model gpt-image-2.5-flare
--size 1024x1024          # auto or positive ASCII WIDTHxHEIGHT
--quality auto            # low, medium, high, xhigh, max, auto
--background transparent  # transparent, opaque, auto
--output-format png       # png, jpeg, webp
--n 4                     # variants, 1-10
--out path/to/file.png
--out-dir path/to/dir
--force
--dry-run
```

The script refuses to overwrite existing outputs unless `--force` is set.

The CLI prints the requested model and size for each saved image. It prints backend model and size only when the response reports them. It measures PNG, JPEG, and WebP dimensions from the saved bytes when possible. It prints `unavailable` when a value is missing or cannot be inspected.

## Edit

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" edit \
  --image "input.png" \
  --prompt "Remove the mug from the table; keep the table, lighting, and background unchanged" \
  --constraints "change only the mug area; no new objects; no text; no watermark"
```

Multiple input images are passed with repeated `--image` flags:

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" edit \
  --image "person.png" \
  --image "jacket.png" \
  --prompt "Put the jacket from image 2 on the person in image 1; preserve face, pose, and lighting"
```

Mask example:

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" edit \
  --image "photo.png" \
  --mask "mask.png" \
  --prompt "Fill only the transparent masked region with matching floor texture"
```

## Batch generation

Input file: one JSON object or plain prompt per line. Empty lines and `#` comments are ignored.

```jsonl
{"prompt":"Cavernous hangar interior with a compact shuttle parked near the center","size":"1536x1024","quality":"auto","constraints":"no logos; no watermark"}
{"prompt":"Gray wolf in profile in a snowy forest","size":"1024x1024","out":"wolf.png"}
A tiny red cube on a plain white background, no text
```

Run:

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" generate-batch \
  --input "tmp/imagegen/prompts.jsonl" \
  --out-dir "output/imagegen" \
  --concurrency 3
```

Per-job keys may override shared CLI values: `model`, `n`, `size`, `quality`, `background`, `output_format`, `output_compression`, `moderation`, `out`, and prompt-scaffolding fields such as `use_case`, `asset_type`, `style`, `composition`, `constraints`, and `negative`. The model value can be any non-empty provider-defined name.

## Prompt scaffolding flags

The CLI can turn a compact prompt into a labeled prompt spec:

```bash
--use-case product-mockup
--asset-type "landing page hero"
--scene "warm kitchen counter"
--subject "matte ceramic mug"
--style "clean product photography"
--composition "wide shot with negative space on the left"
--lighting "soft studio window light"
--palette "warm neutrals"
--materials "matte ceramic, subtle steam"
--text "exact text to render"
--constraints "no logos; no watermark"
--negative "extra hands, misspelled words"
```

Use `--no-augment` when the prompt is already exactly what should be sent to the endpoint.

## Downscaled web copy

Optionally create a second, smaller copy:

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" generate \
  --prompt "..." \
  --out "public/images/hero.png" \
  --downscale-max-dim 1600 \
  --downscale-suffix -web
```

Downscaling requires Pillow in the active Python environment. Generation itself does not.

## Endpoint/config troubleshooting

Direct curl tests and endpoint/auth/output failure recovery live in `references/troubleshooting.md`.

## Failure modes

If the endpoint, auth, output path, or response format fails, read `references/troubleshooting.md`.
