# CLI reference (`scripts/image_gen.py`)

Subcommands: `generate` (new image from a prompt), `edit` (one or more existing images), `generate-batch` (many jobs from a JSONL file). Live calls need network access and `OPENAI_API_KEY`; `--dry-run` prints the API payload and output paths without either and without the `openai` package.

Point `IMAGE_GEN` at this skill's own `scripts/image_gen.py` (in the directory that holds the skill's `SKILL.md`); other copies of an imagegen skill can be older, with different flags:

```
export IMAGE_GEN="$HOME/.agents/skills/imagegen/scripts/image_gen.py"  # adjust to this skill's directory
```

## Quick start

Generate:

```bash
python "$IMAGE_GEN" generate \
  --prompt "A cozy alpine cabin at dawn" \
  --size 1024x1024 \
  --out output/imagegen/alpine-cabin.png
```

Edit:

```bash
python "$IMAGE_GEN" edit \
  --image input.png \
  --prompt "Replace only the background with a warm sunset" \
  --out output/imagegen/sunset-edit.png
```

## Model, quality, and complexity
- Model: `$IMAGE_GEN_MODEL` when set and not empty, else `gpt-image-2.6-sunburst` (detailed work and precise edits). `IMAGE_GEN_MODEL` is the user's standing choice, so omit `--model` unless the user asks for a different model in the request. `--model gpt-image-2.6-flare` when the user asks for speed, such as quick drafts or fast iteration. Any other model only when the user names it; `gpt-image-1.5` and `gpt-image-1-mini` are deprecated (shut down 2026-12-01) and print a warning.
- `--quality` (`low|medium|high|xhigh|max|auto`, default `auto`) sets rendered detail such as texture and small lettering: `low` for drafts and thumbnails; `medium`, `high`, or `auto` for final assets; `xhigh` or `max` (GPT Image 2.5 and 2.6) when dense text or fine texture still needs more.
- `--complexity` (`low|medium|high`, default `high`) sets how much the model plans before rendering, with no separate fee. Lower suits focused edits such as a background or a sign; higher helps dense text, complex layouts, and object relationships but can add latency and unwanted changes. Change quality or complexity one at a time and compare.
- `--input-fidelity` is edit-only and only for the deprecated 1.x models; the CLI rejects it for GPT Image 2 and newer, which always use high input fidelity.
- Size: default `auto` (the API picks a size that fits the prompt). Square is fastest: `1024x1024` for quick drafts. Popular: `1536x1024` landscape, `1024x1536` portrait, `2048x2048`, `2048x1152`, `3840x2160` 4K landscape, `2160x3840` 4K portrait. Custom `WIDTHxHEIGHT`: both edges multiples of 16, aspect ratio 1:3 to 3:1, no edge above 3840, 655,360 to 8,294,400 total pixels; above `2560x1440` is experimental. Deprecated 1.x models accept only `1024x1024`, `1536x1024`, `1024x1536`, or `auto`.
- Other defaults: output format `png`; one-off output path `output/imagegen/output.png`; background unset unless `--background` is passed.
- `--mask` and `--markup` are edit-only. `--markup` is a transparent PNG overlay the size of the first image (for example a circle around the object to change); it cannot be combined with `--mask`.

Markup-guided edit:

```bash
python "$IMAGE_GEN" edit \
  --image kitchen.png \
  --markup circle-overlay.png \
  --prompt "Recolor the vase inside the red circle to cobalt blue. Keep everything else unchanged. Remove the red circle." \
  --out output/imagegen/vase-edit.png
```

Mask notes:
- For multi-image edits, pass repeated `--image` flags. Their order is meaningful, so describe each image by index and role in the prompt.
- The CLI accepts a single `--mask`.
- Image and mask must be the same size and format and each under 50MB.
- Masks must include an alpha channel.
- If multiple input images are provided, the mask applies to the first image.
- Masking is prompt-guided; edges are not pixel-exact.

## GIF, SVG, and streaming
These features need `openai` Python SDK 3.46.0 or newer. GIF output and SVG author-model selection may need preview access on the API project.

Looping GIF (`generate`, `edit`, or per-job in `generate-batch`):

```bash
python "$IMAGE_GEN" generate \
  --prompt "A monkey lifts a glowing lantern, blinks, then lowers it back to its starting pose. Camera still. Loop smoothly." \
  --output-format gif --size 512x512 --frame-count 9 --frame-duration-ms 200 \
  --out output/imagegen/lantern.gif
```

- `--frame-count` is 2-16 and `--frame-duration-ms` is 20-2000. Both require `--output-format gif`.
- GIF frames share one pixel budget per request; keep the default `size=auto` unless you need a specific frame size.
- GIF output does not work with `--stream`, `--mask`, `--output-compression`, or `--downscale-max-dim`.

SVG (`generate` and `generate-batch` only):

```bash
python "$IMAGE_GEN" generate \
  --prompt "Flat vector logo for a bakery called Field & Flour" \
  --output-format svg --background transparent \
  --out output/imagegen/logo.svg
```

- SVG requires `--n 1` and does not work with `--stream`, `--output-compression`, or `--downscale-max-dim`.
- `--svg-model` (and optional `--svg-reasoning-effort none|minimal|low|medium|high|xhigh|max`) select the SVG author model. Use them only when the API project has SVG model selection.

Streaming (`generate` and `edit`, `--n 1` only):

```bash
python "$IMAGE_GEN" generate \
  --prompt "A river made of white owl feathers in a winter landscape" \
  --stream --partial-images 2 \
  --out output/imagegen/river.png
```

- Partial images are saved as `<name>-partial-<index>.<ext>` next to the final file. The final image may arrive before all requested partials.
- `--partial-images` (0-3) requires `--stream`.

## Output handling
- Use `tmp/imagegen/` for temporary JSONL inputs or scratch files.
- Use `output/imagegen/` for final outputs.
- Reruns fail if a target file already exists unless you pass `--force`.
- After each call the CLI prints a `Result:` line (`[job N/M] result:` in batch mode) with the size, quality, background, and output format the API reports, plus token usage. Use it to see what `auto` resolved to and what the call cost in tokens.
- When the result differs from the request, the CLI prints `Warning: ... differs from the request:` with each difference: size or quality (when not `auto`), background, fewer images than `--n`, or no partial images while streaming. Some proxies and backends ignore these settings.
- When the API returns another file format than requested (for example PNG for `--output-format svg`), the CLI writes the file with the real format's extension and prints a warning with the path it wrote.
- `--alpha-snap` (png or webp output): after download, the CLI sets pixels with alpha 250-254 to fully opaque and prints how many it changed. Use it for transparent logos and icons whose solid areas come back slightly see-through. It re-encodes the file (WebP losslessly) and needs Pillow.
- A failed API call prints one `Error:` line with the HTTP status, the request URL, and the server's message, then exits with status 1. In batch mode the job's `failed:` line has the same text, and the other jobs continue.
- `--out-dir` changes one-off naming to `image_1.<ext>`, `image_2.<ext>`, and so on.
- `--downscale-max-dim N` also writes a copy no larger than N px on its long edge, suffixed `-web` (`--downscale-suffix` changes it); needs Pillow.

## Common recipes

Generate with augmentation fields:

```bash
python "$IMAGE_GEN" generate \
  --prompt "A minimal hero image of a ceramic coffee mug" \
  --use-case "product-mockup" \
  --style "clean product photography" \
  --composition "wide product shot with usable negative space for page copy" \
  --constraints "no logos, no text" \
  --out output/imagegen/mug-hero.png
```

Generate multiple prompts concurrently (async batch):

```bash
mkdir -p tmp/imagegen output/imagegen/batch
cat > tmp/imagegen/prompts.jsonl << 'EOF'
{"prompt":"Cavernous hangar interior with a compact shuttle parked near the center","use_case":"stylized-concept","composition":"wide-angle, low-angle","lighting":"volumetric light rays through drifting fog","constraints":"no logos or trademarks; no watermark","size":"1536x1024"}
{"prompt":"Gray wolf in profile in a snowy forest","use_case":"photorealistic-natural","composition":"eye-level","constraints":"no logos or trademarks; no watermark","size":"1024x1024"}
EOF

python "$IMAGE_GEN" generate-batch \
  --input tmp/imagegen/prompts.jsonl \
  --out-dir output/imagegen/batch \
  --concurrency 5

rm -f tmp/imagegen/prompts.jsonl
```

Notes:
- `generate-batch` requires `--out-dir`.
- Use `--concurrency` to control parallelism (default `5`).
- Per-job overrides are supported in JSONL (for example `size`, `quality`, `complexity`, `background`, `output_format`, `output_compression`, `frame_count`, `frame_duration_ms`, `svg`, `safety_identifier`, `moderation`, `n`, `model`, `out`, and prompt-augmentation fields). The CLI validates every job before it sends any request; one invalid job stops the whole batch with no API calls.
- `--n` generates multiple variants for a single prompt; `generate-batch` is for many different prompts.
- In batch mode, per-job `out` is treated as a filename under `--out-dir`.

## CLI notes
- `--prompt-file`, `--output-compression`, `--moderation`, `--safety-identifier`, `--max-attempts`, `--fail-fast`, `--force`, and `--no-augment` are supported.
