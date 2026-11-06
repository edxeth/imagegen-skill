# imagegen-skill

A pi skill for generating and editing raster images with OpenAI's GPT Image models (`gpt-image-2.6-sunburst` by default), through the OpenAI Image API or any OpenAI-compatible endpoint.

Use it for bitmap deliverables: hero images, product shots, mockups, textures, sprites, transparent cutouts, and edits of existing pictures. For SVG, vector, or code-native assets, edit those directly instead.

The everyday instructions live in `imagegen/SKILL.md`; the agent reads `imagegen/references/` only when it needs the details. All image work goes through one CLI, `imagegen/scripts/image_gen.py`.

## Install

With the skills CLI:

```bash
npx skills add edxeth/imagegen-skill --skill imagegen --agent pi --global --yes
```

The skills CLI keys installs by skill name, and `openai/skills` also ships a skill named `imagegen`. Installing or updating one of them silently replaces the other, so keep only one source.

To work on the skill itself, clone the repository and link its `imagegen/` folder into pi's skills directory (`~/.agents/skills` or `~/.pi/agent/skills`):

```bash
git clone https://github.com/edxeth/imagegen-skill.git
ln -s "$PWD/imagegen-skill/imagegen" ~/.agents/skills/imagegen
```

## Setup

- Python 3.10 or newer with the `openai` package 3.46.0 or newer. Pillow is optional; `--alpha-snap`, `--downscale-max-dim`, and image previews need it. Without a project environment, `uv` runs the CLI with both:

  ```bash
  uv run --with "openai>=3.46.0" --with pillow python imagegen/scripts/image_gen.py --help
  ```

- Environment variables:

  | Variable | Required | Purpose |
  | --- | --- | --- |
  | `OPENAI_API_KEY` | yes | Key for the endpoint |
  | `OPENAI_BASE_URL` | no | Any OpenAI-compatible endpoint, such as a proxy or gateway |
  | `IMAGE_GEN_MODEL` | no | Default model; `--model` overrides it for one call |

  Version 1 read `PI_IMAGEGEN_*` variables; this version reads only the variables above.

## Quick start

```bash
# Preview the request without calling the API (needs neither a key nor the openai package)
python imagegen/scripts/image_gen.py generate --prompt "A tiny red cube on a plain white background, no text" --dry-run

# Generate into a project
python imagegen/scripts/image_gen.py generate \
  --prompt "A clean product photo of a matte ceramic coffee mug, no text" \
  --out public/images/mug-hero.png

# Edit an image; the result is written to a new file
python imagegen/scripts/image_gen.py edit \
  --image input.png \
  --prompt "Replace only the background with warm sunset light; keep the subject unchanged"
```

`imagegen/references/cli.md` documents every option: quality and complexity, custom sizes, transparent output and `--alpha-snap`, GIF and SVG output, markup-guided edits, streaming, and JSONL batches.

After each call the CLI prints a `Result:` line with the size, quality, background, output format, and token usage the API reports. It warns when the result differs from the request, because some endpoints ignore settings such as size or quality.

## Verification

```bash
bash scripts/verify.sh
```

`scripts/verify.sh` installs the skill from a temporary clone with the skills CLI in a temporary `HOME`, checks the installed files, runs an isolated dry run without a paid request, and runs the behavior tests. It does not modify user configuration.

Run the behavior tests alone with:

```bash
python -m unittest discover -s tests -v
```

The tests use fake API clients and deterministic image bytes and make no paid calls. The alpha-snap tests need Pillow.
