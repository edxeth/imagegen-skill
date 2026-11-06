---
name: "imagegen"
description: "Generate or edit raster images (photos, illustrations, textures, sprites, mockups, transparent cutouts) with AI: a new image, an edit of an existing image, or variants from reference images, when the deliverable is a bitmap. Not for SVG/vector/code-native assets, an existing icon or logo system, or visuals built in HTML/CSS/canvas."
---

# Image Generation Skill

## How it works

`scripts/image_gen.py` calls the OpenAI Image API with GPT Image models (default `gpt-image-2.6-sunburst`; `IMAGE_GEN_MODEL` changes it). It needs `OPENAI_API_KEY`.

Rules:
- Use the CLI for every image request this skill handles.
- A tool named `image_gen` in pi comes from another extension; use it only when the user asks for it by name.
- Without `OPENAI_API_KEY`, give the user the steps under "Dependencies and environment" and wait. Produce the image only after the key is set; use another tool or method, such as a local edit or a drawn image, only when the user asks for it.

CLI work goes through `scripts/image_gen.py` as shipped. When a flag or feature is missing, ask the user instead of writing an SDK script or editing the CLI.

## When not to use
- Extending or matching an existing SVG/vector icon set, logo system, or illustration library inside the repo
- Creating simple shapes, diagrams, wireframes, or icons that are better produced directly in SVG, HTML/CSS, or canvas
- Making a small project-local asset edit when the source file already exists in an editable native format
- Any task where the user clearly wants deterministic code-native output instead of a generated bitmap

## Workflow
1. Intent: `edit` when the user wants an existing image changed with parts preserved; otherwise `generate`, and any supplied images are references for style, composition, mood, or subject.
2. Execution: one call or batch job per distinct asset; `n` only for variants of one prompt. `generate-batch` runs many prompts.
3. Collect inputs: prompt(s), exact text verbatim, constraints and avoid list, and each input image labeled by role (reference, edit target, or insert/style/compositing input).
4. Keep raster requests raster: a photo, illustration, sprite, product image, or banner is generated with the CLI, never replaced by an SVG/HTML/CSS placeholder. An icon, logo, or UI graphic that must match existing repo-native vector or code assets is edited in that format instead.
5. Shape the prompt with "Prompt augmentation" and the "Shared prompt schema".
6. Read `references/cli.md` before the first call. It holds the flags, model and quality choice, sizes, output paths, and the `Result:` line.
7. Transparent output: follow "Transparent image requests".
8. Validate every output: subject, style, composition, text accuracy, invariants, and avoid items. Read the `Result:` line (size, quality, background, output format, token usage) and any `differs from the request` or format warning, and tell the user which requested settings the API did not apply. In pi, view the file as "Inspecting outputs in pi" shows.
9. Iterate with one targeted change, then re-check.
10. Deliver: a project-bound asset goes into the workspace (every requested deliverable of a batch) and consuming code or references get updated; a preview stays where it was written. Replace an existing asset only when the user asked for replacement; otherwise write a sibling such as `hero-v2.png`. Report the final saved path(s) and the final prompt(s).

## Transparent image requests

Pass `--background transparent` with `--output-format png`, `webp`, or `svg`, and preserve the alpha. GPT Image 2.5 and 2.6 support this natively. If solid areas come back slightly see-through (alpha 250-254), add `--alpha-snap`.

## Prompt augmentation

Reformat the user's prompt into a short, structured, production-oriented spec; add only what materially improves the result.
- A specific, detailed prompt is normalized into a clear spec with no added creative requirements.
- A generic prompt may get tasteful augmentation: composition or framing hints, polish level or intended use, practical layout guidance, and reasonable scene concreteness that supports the request.
- Never add extra characters or objects, brand names, slogans, palettes, or narrative beats that the request does not imply, or side-specific placement the surrounding layout does not support.
- For edits, list invariants explicitly (`change only X; keep Y unchanged`) and repeat them every iteration.
- When a missing critical detail blocks success, ask; otherwise proceed.

## Use-case taxonomy (exact slugs)

Classify each request into one of these buckets and keep the slug consistent across prompts and references.

Generate:
- photorealistic-natural — candid/editorial lifestyle scenes with real texture and natural lighting.
- product-mockup — product/packaging shots, catalog imagery, merch concepts.
- ui-mockup — app/web interface mockups and wireframes; specify the desired fidelity.
- infographic-diagram — diagrams/infographics with structured layout and text.
- scientific-educational — classroom explainers, scientific diagrams, and learning visuals with required labels and accuracy constraints.
- ads-marketing — campaign concepts and ad creatives with audience, brand position, scene, and exact tagline/copy.
- productivity-visual — slide, chart, workflow, and data-heavy business visuals.
- logo-brand — logo/mark exploration, vector-friendly.
- illustration-story — comics, children’s book art, narrative scenes.
- stylized-concept — style-driven concept art, 3D/stylized renders.
- historical-scene — period-accurate/world-knowledge scenes.

Edit:
- text-localization — translate/replace in-image text, preserve layout.
- identity-preserve — try-on, person-in-scene; lock face/body/pose.
- precise-object-edit — remove/replace a specific element (including interior swaps).
- lighting-weather — time-of-day/season/atmosphere changes only.
- background-extraction — transparent background / clean cutout. Ask for actual transparency (see "Transparent image requests").
- style-transfer — apply reference style while changing subject/scene.
- compositing — multi-image insert/merge with matched lighting/perspective.
- sketch-to-render — drawing/line art to photoreal render.

## Shared prompt schema

Use the following labeled spec as prompt scaffolding:

```text
Use case: <taxonomy slug>
Asset type: <where the asset will be used>
Primary request: <user's main prompt>
Input images: <Image 1: role; Image 2: role> (optional)
Scene/backdrop: <environment>
Subject: <main subject>
Style/medium: <photo/illustration/3D/etc>
Composition/framing: <wide/close/top-down; placement>
Lighting/mood: <lighting + mood>
Color palette: <palette notes>
Materials/textures: <surface details>
Text (verbatim): "<exact text>"
Constraints: <must keep/must avoid>
Avoid: <negative constraints>
```

Notes:
- `Asset type` and `Input images` are prompt scaffolding, not dedicated CLI flags.
- `Scene/backdrop` refers to the visual setting. It is not the same as the CLI `background` parameter, which controls output transparency behavior.

## Examples

### Generation example (hero image)
```text
Use case: product-mockup
Asset type: landing page hero
Primary request: a minimal hero image of a ceramic coffee mug
Style/medium: clean product photography
Composition/framing: wide composition with usable negative space for page copy if needed
Lighting/mood: soft studio lighting
Constraints: no logos, no text, no watermark
```

### Edit example (invariants)
```text
Use case: precise-object-edit
Asset type: product photo background replacement
Primary request: replace only the background with a warm sunset gradient
Constraints: change only the background; keep the product and its edges unchanged; no text; no watermark
```

## Prompting best practices
- Structure the prompt as scene/backdrop -> subject -> details -> constraints, and state the intended use (ad, UI mock, infographic) to set polish.
- Use camera and composition language for photorealism.
- Quote exact text, specify typography and placement, spell tricky words letter-by-letter, and require verbatim rendering.
- For multi-image inputs, reference images by index and say how each is used.

## Setup

### Dependencies and environment
- Python packages: `openai>=3.46.0` (`--complexity`, GIF, SVG, and `--markup` need it) and Pillow for `--alpha-snap`, `--downscale-max-dim`, and previews. Without a project environment, run `uv run --with "openai>=3.46.0" --with pillow python "$IMAGE_GEN" ...`; otherwise install them with the project's package manager (`uv pip install "openai>=3.46.0" pillow` in uv-managed environments). If installation is impossible here, tell the user which package is missing and how to install it.
- `OPENAI_API_KEY` must be set. Ask the user to set it locally and confirm when ready; the full key never goes into the chat. Steps for the user: create a key at https://platform.openai.com/api-keys, set `OPENAI_API_KEY` in their environment (offer OS/shell-specific guidance), then confirm.
- Optional `OPENAI_BASE_URL`: any OpenAI-compatible endpoint, such as a proxy or gateway; `OPENAI_API_KEY` is then that endpoint's key. Some endpoints ignore settings such as size or quality; the CLI warns when that happens.

### Inspecting outputs in pi
In pi's codemode, `tools.read` returns an image file as an image block that `image()` shows. When it returns a text note such as `Read image file [image/png]` instead, convert the output to a small JPEG data URL first:

```js
await tools.bash({command: `uv run --no-project --quiet --with pillow python -c "from PIL import Image; im = Image.open('output/imagegen/hero.png'); im = Image.alpha_composite(Image.new('RGBA', im.size, 'white'), im.convert('RGBA')).convert('RGB'); im.thumbnail((640, 640)); im.save('/tmp/imagegen-preview.jpg', quality=60)"`});
const preview = await tools.bash({command: "base64 -w0 /tmp/imagegen-preview.jpg"});
image("data:image/jpeg;base64," + preview.output.trim());
```

## Reference map
- `references/cli.md`: CLI flags, model and quality choice, sizes, GIF/SVG/streaming, output handling, batch recipes. Read before the first CLI call.
- `references/image-api.md`: Image API parameter reference, when `cli.md` does not answer a parameter question.
- `references/prompting.md`: prompting principles and per-use-case tips.
- `references/sample-prompts.md`: copy/paste prompt recipes and asset-type templates.
