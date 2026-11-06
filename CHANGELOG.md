# Changelog

## [Unreleased]

## [2.0.0] - 2026-11-06

### Breaking Changes

- The skill files moved from `imagegen/` to the repository root, and skills CLI 1.5 installs only `SKILL.md` from this layout. Reinstall with skills CLI 1.7 or newer: `npx skills add edxeth/imagegen-skill --skill imagegen --agent pi --global --yes`. See [Install](https://github.com/edxeth/imagegen-skill/blob/v2.0.0/README.md#install).
- Without `--out` or `--out-dir`, `generate` and `edit` now write to `output/imagegen/output.png` in the current folder, and `generate-batch` now needs `--out-dir`. Version 1 wrote new timestamped files to `PI_IMAGEGEN_OUTPUT_DIR` or `~/.pi/generated_images`. Pass `--out` with a new file name for each image; the CLI replaces an existing file only with `--force`.
- Model names must now start with `gpt-image-`. Version 1 accepted any model name, including provider aliases. If you used another name, set a GPT Image model that your endpoint offers in `--model`, `IMAGE_GEN_MODEL`, or the `model` of a batch job.
- The CLI no longer expands `~` in file and folder paths, including `out` values in batch jobs. A quoted path such as `--out '~/image.png'` now writes to a folder named `~` in the current folder, and input paths in that form are not found. Use an absolute path or `$HOME`, for example `--out "$HOME/image.png"`.
- Image URLs in API responses must now use `http` or `https`, also after redirects. Version 1 also opened other URL types, such as `data:`, `file:`, and `ftp:` URLs. If your endpoint returns another URL type, configure it to return an `http` or `https` URL or base64 data in `b64_json`.
- Removed the `models` subcommand, which listed the models of an endpoint. Query the endpoint's `/models` route instead.
- Removed `--timeout`; remove it from saved commands. API requests now use the defaults of the `openai` package: a 600-second timeout and up to 2 automatic retries. Version 1 waited 300 seconds and did not retry.
- Removed `--asset-type` and `--input-images`. Write `Asset type:` and `Input images:` lines in the prompt instead.

#### Endpoint, key, and model come from new environment variables
Refs: `7ed40db`

The CLI no longer reads the `PI_IMAGEGEN_*` variables or the `--base-url` and `--api-key` options. Set `OPENAI_API_KEY` for the key, `OPENAI_BASE_URL` for the endpoint, and `IMAGE_GEN_MODEL` for the default model. See [Setup](https://github.com/edxeth/imagegen-skill/blob/v2.0.0/README.md#setup).

- Every call except `--dry-run` needs `OPENAI_API_KEY`. Version 1 sent the key `dummy` when none was set. For an endpoint without authentication, set any value.
- Without `OPENAI_BASE_URL`, requests go to the OpenAI API. Version 1 sent them to `http://localhost:8317/v1`. To keep using a local proxy, set `OPENAI_BASE_URL` to its URL.

#### The CLI needs the openai Python package
Refs: `7ed40db`

Version 1 sent requests with the Python standard library. The CLI now sends them through the `openai` package, version 3.46.0 or newer. Without the package, every call except `--dry-run` stops with an error before it sends a request.

Install the package in the project environment, or run the CLI with `uv run --with "openai>=3.46.0" --with pillow python scripts/image_gen.py`. See [Setup](https://github.com/edxeth/imagegen-skill/blob/v2.0.0/README.md#setup).

#### Model limits are checked before the request
Refs: `7ed40db`

The CLI now checks `--size`, `--quality`, and `--input-fidelity` against the selected model, and it stops before the request when a value is not supported. Version 1 checked only the format of these values. See [Model, quality, and complexity](https://github.com/edxeth/imagegen-skill/blob/v2.0.0/references/cli.md#model-quality-and-complexity).

Custom sizes for GPT Image 2 and newer need edges that are multiples of 16. Except for GIF frames, they also need edges of 3840 pixels or less, an aspect ratio from 1:3 to 3:1, and 655,360 to 8,294,400 pixels in total. GPT Image 1.x models accept only `1024x1024`, `1536x1024`, `1024x1536`, and `auto`. `xhigh` and `max` quality do not work with `gpt-image-2` or GPT Image 1.x, and `--input-fidelity` works only with GPT Image 1.x.

Update saved commands and batch jobs to use values that the selected model accepts, and omit `--input-fidelity` for GPT Image 2 and newer.

### Added

- Added `--complexity low|medium|high`, which sets how much the model plans before it renders, separately from quality.
- Added GIF output with `--output-format gif`, `--frame-count` (2 to 16 frames), and `--frame-duration-ms` (20 to 2000 ms per frame). The API project may need preview access for it. See [GIF, SVG, and streaming](https://github.com/edxeth/imagegen-skill/blob/v2.0.0/references/cli.md#gif-svg-and-streaming).
- Added SVG output with `--output-format svg` for `generate` and `generate-batch`. `--svg-model` and `--svg-reasoning-effort` select the model that writes the SVG; the API project may need preview access for them.
- Added `--markup` to `edit`: a transparent PNG drawn over the first input image, for example a circle around the object to change. It does not work with `--mask`.
- Added `--stream` with `--partial-images` (0 to 3) to `generate` and `edit` (needs `--n 1`). The CLI saves the partial images next to the final file.
- Added `--safety-identifier`, which sends a stable hashed ID of the end user, up to 128 characters.
- Added `--alpha-snap` for PNG and WebP output. It makes pixels with alpha 250 to 254 fully opaque, for transparent images whose solid areas come back slightly see-through.
- Added `--max-attempts` (1 to 10, default 3) to `generate-batch`. The CLI retries a job after a rate limit, a timeout, or a reset connection.
- Added a warning when the result differs from the request, for example in size, quality, background, or number of images. Some endpoints ignore these settings.
- Added a warning for the deprecated GPT Image 1.x models. It gives the shutdown date and suggests `gpt-image-2.6-sunburst` or `gpt-image-2.6-flare`.
- Added the `scientific-educational`, `ads-marketing`, and `productivity-visual` use cases to the prompt taxonomy, with sample prompts.

### Changed

- Changed the default model from `gpt-image-2.5-flare` to `gpt-image-2.6-sunburst`. Set `IMAGE_GEN_MODEL` to keep another model.
- Changed the default size from `1024x1024` to `auto`.
- Changed the default `generate-batch` concurrency from 3 to 5.
- Changed the report after each call to a `Result:` line with the size, quality, background, output format, and token usage that the API reports. Version 1 reported the requested and returned model and size, and the measured size of each file.
- Changed the setup: the skill no longer needs the `pi-better-skills` extension, because the instructions no longer use `$PI_SKILL_DIR`.

#### Agent instructions follow OpenAI's imagegen skill
Refs: `7ed40db`

The instructions are now based on OpenAI's imagegen skill and adapted for pi.

- Without `OPENAI_API_KEY`, the agent gives the user the setup steps and waits. It uses another tool or method, such as an `image_gen` tool from another pi extension, only when the user asks for it.
- The agent reads `references/cli.md` before its first call, checks the `Result:` line, and tells the user which requested settings the API did not apply. In pi, it can preview an image as a small JPEG when `read` does not show the file.

### Fixed

- Fixed `--dry-run` and batches rejected by validation creating empty output folders. The CLI now creates a folder only when it writes a file.
- Fixed files that got the requested extension when the API returned another format, for example PNG data in a `.webp` file. The file now gets the extension of the returned format, and the CLI prints a warning with the path. If a file with that name exists, the CLI writes a numbered copy such as `pic-2.png` instead.
- Fixed images being lost when another program created a file with the same name during the request. Without `--force`, the CLI now writes a numbered copy, such as `hero-2.png`, and does not replace the other file. This also applies to downscaled copies and to partial images from a stream.
- Fixed `--downscale-max-dim` stopping with a Pillow error after the paid request when Pillow was not installed. The CLI now checks for Pillow before it sends a request.

### Removed

- Removed `references/troubleshooting.md`. The README and `SKILL.md` describe the setup.
- Removed the rule that the agent states the planned size, quality, and image count before an expensive request, and asks first when the cost can rise without an agreed budget.
