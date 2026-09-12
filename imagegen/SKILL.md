---
name: imagegen
description: "Generate or edit raster images. Use for bitmap visuals: photos, illustrations, textures, sprites, product mockups, UI mockups, or transparent-background cutouts. Do not use when SVG/vector/code-native assets are the better fit."
---

# Image Generation Skill for Pi

Use the bundled CLI for raster image generation and editing:

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" ...
```

## Core contract

- The CLI is the implementation boundary: `scripts/image_gen.py`.
- The client sends direct HTTP to the configured OpenAI-compatible endpoint. It does not require the OpenAI Python SDK.
- Let the CLI/env choose endpoint, model, auth, and default preview output path. Generation does not require model discovery.
- For setup, env precedence, current defaults, curl tests, or failures, read `references/troubleshooting.md` lazily.
- For command details beyond the minimal examples below, read `references/cli.md` lazily.

## Safety rules

- Never ask the user to paste secrets in chat; ask them to set env vars locally.
- Never overwrite an existing file unless the user asked for replacement or `--force` is intentional.
- Preview-only images may stay in the configured generated-images directory.
- Project-referenced assets must be saved inside the project via `--out` or `--out-dir` and then wired into consuming code.
- Edits must preserve invariants aggressively and write a new file by default.

## When to use

Use this for new raster images, raster edits, variants, and batches.

Do not use it for SVG/vector/code-native assets, deterministic diagrams, or editable native source assets.

## Decision tree

1. **Intent:** generate or edit?
   - Change an existing image while preserving parts of it → `edit`.
   - Use supplied images only as references → `generate`, and label their roles in the prompt.
   - No images → `generate`.
2. **Ownership:** preview-only or project-bound?
   - Preview → omit `--out` unless the user requested a path.
   - Project asset → use `--out` or `--out-dir` inside the project.
3. **Scale:** one prompt, variants, or batch?
   - One image → `generate` / `edit`.
   - Variants → `generate --n <count>`.
   - Many prompts → `generate-batch`.

## Workflow

1. Confirm the task needs a raster image.
2. Choose quality and dimensions using the rules below.
3. Build a short structured prompt using only useful fields from the schema below.
4. Run the CLI; use `--dry-run` first for complex commands.
5. Check output path and file metadata; visually inspect when possible.
6. Iterate with one targeted change if needed, within the agreed budget and output count.
7. Report final path, requested model, backend model when available, endpoint, and final prompt.

The CLI reports the requested size and backend-reported size when available. It measures saved-image size when it can inspect the file bytes. It reports `unavailable` instead of guessing. C2PA metadata alone is not definitive proof of the actual served model.

## Choose quality and dimensions

Infer ordinary settings from the request and target layout. Do not ask the user to choose CLI flags when their intent is clear. Set quality and dimensions with CLI flags, not words such as "4K" in the prompt.

- Preserve explicit model, quality, dimensions, and output-count choices. Do not switch providers or billing paths to satisfy a parameter.
- Keep `quality=auto` for ordinary requests without a stronger requirement. For explicitly requested drafts or cheap exploration, choose `low` unless the user specifies another quality.
- Consider `high` for a concrete requirement such as legible small text. Use `xhigh` or `max` only to address a named requirement that lower settings leave unmet. Higher quality settings do not guarantee better results.
- When an approved iteration falls short, change one setting at a time. Do not generate extra comparisons or variants without authorization.
- Infer orientation and dimensions from the target layout. Keep the default `1024x1024` when no layout requirement exists. For custom dimensions or higher quality, read the provider guidance in `references/image-api.md` before choosing values.
- For edits, inspect the input dimensions and preserve its framing unless the user requests a change. Use matching dimensions when supported. Do not assume `size=auto` preserves framing on every endpoint.
- Ask one concise question if a missing requirement blocks success, or an increase exceeds an agreed cost or latency limit. If a proposed expensive increase has no clear budget, ask before proceeding. Do not silently round an unsupported size, crop the output, or substitute another model. Explain the limitation and ask when an alternative changes the result.

Before an expensive request, state the planned dimensions, quality, and count. This CLI's `--dry-run` previews the request only. It does not estimate cost or confirm provider support. Compare the returned dimensions with the request and report any mismatch.

## Minimal examples

Generate preview:

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" generate \
  --prompt "A tiny red cube on a plain white background, no text"
```

Generate project asset:

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" generate \
  --prompt "A minimal hero image of a ceramic coffee mug" \
  --use-case product-mockup \
  --asset-type "landing page hero" \
  --constraints "no logos, no text, no watermark" \
  --out "public/images/mug-hero.png"
```

Edit:

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" edit \
  --image "input.png" \
  --prompt "Replace only the background with warm sunset light" \
  --constraints "change only the background; keep the subject and edges unchanged; no text; no watermark"
```

## Prompting rules

- Preserve detailed user prompts; only normalize them.
- For generic prompts, add only details that materially improve output quality.
- Allowed additions: composition, framing, intended use, polish level, practical layout guidance, and reasonable scene concreteness.
- Do not add unrelated objects, characters, brands, slogans, palettes, story beats, or arbitrary left/right placement.
- For edits, always state the invariant: `change only X; keep Y unchanged`.

## Prompt schema

Use only lines that help:

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

Taxonomy and copy/paste prompt recipes live in `references/prompting.md` and `references/sample-prompts.md`; read them only when needed.

## Lazy reference map

- `references/troubleshooting.md`: setup, env vars, defaults, direct curl tests, failure recovery.
- `references/cli.md`: command usage and batch/advanced examples.
- `references/image-api.md`: API parameters, model-specific selection guidance, and endpoint behavior.
- `references/prompting.md`: prompting principles and taxonomy.
- `references/sample-prompts.md`: copy/paste prompt recipes.
