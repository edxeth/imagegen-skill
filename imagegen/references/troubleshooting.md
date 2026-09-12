# Imagegen configuration and troubleshooting

Read this file only when setup, endpoint, auth, output paths, or API compatibility is unclear or failing.

## Defaults

- Endpoint: `http://localhost:8317/v1`
- Model: `gpt-image-2.5-flare`
- Local auth: `Authorization: Bearer dummy`
- Preview output root: `~/.pi/generated_images`

## Configuration precedence

Endpoint:
1. `--base-url`
2. `PI_IMAGEGEN_BASE_URL`
3. `OPENAI_BASE_URL`
4. `http://localhost:8317/v1`

Model:
1. `--model`
2. `PI_IMAGEGEN_MODEL`
3. script default

API key:
1. `--api-key`
2. `PI_IMAGEGEN_API_KEY`
3. `OPENAI_API_KEY`
4. `dummy` for localhost-style endpoints

Output directory:
1. `--out` / `--out-dir`
2. `PI_IMAGEGEN_OUTPUT_DIR`
3. sibling `generated_images` next to `PI_CODING_AGENT_DIR`
4. `~/.pi/generated_images`

Pi documents `PI_CODING_AGENT_DIR` as the config-dir override. Default Pi config dir is `~/.pi/agent`, so the default generated-image root is `~/.pi/generated_images`.

## Direct upstream or generic proxy

Set `PI_IMAGEGEN_BASE_URL` to the base URL for the service that owns the image routes. The client does not add a provider-specific route.

For a direct upstream service, use its documented base URL and API key:

```bash
export PI_IMAGEGEN_BASE_URL='https://api.openai.com/v1'
export PI_IMAGEGEN_API_KEY='set-this-in-your-shell'
```

For a generic local or remote proxy, use the proxy base URL and the token that it accepts:

```bash
export PI_IMAGEGEN_BASE_URL='http://localhost:8317/v1'
export PI_IMAGEGEN_API_KEY='dummy'
```

The CLI sends one bearer token. Subscription OAuth login and API-key access are different authentication paths. A subscription login does not provide universal API entitlement.

The client does not require model discovery, and it does not retry with another model or billing path after a backend error. The `models` command is optional.

If you use CLIProxyAPI, version `v7.2.156` or newer is a troubleshooting baseline. This skill has no CLIProxyAPI-specific routing or model allowlist.

On September 12, 2026, local CLIProxyAPI `v7.2.158` tests used Codex OAuth with three requested model IDs: `gpt-image-2.5-flare`, `gpt-image-2.5-sunburst`, and `gpt-image-2.5`. Each request returned an image. The responses omitted an actual model field, and all images carried C2PA version 2.0 metadata. These results are not definitive proof of the actual served model or of selectable variants.

## Expected image API

For generation and edit, the configured endpoint should expose:

```text
POST /v1/images/generations
POST /v1/images/edits
```

Optional discovery:

```bash
python "$PI_SKILL_DIR/scripts/image_gen.py" models
```

Direct curl generation test:

```bash
mkdir -p /tmp/pi-imagegen-test
curl -sS \
  -H 'Authorization: Bearer dummy' \
  -H 'Content-Type: application/json' \
  http://localhost:8317/v1/images/generations \
  -d '{"model":"gpt-image-2.5-flare","prompt":"A tiny red cube on a plain white background, no text","n":1,"size":"1024x1024","quality":"auto","output_format":"png"}' \
  -o /tmp/pi-imagegen-test/response.json
python - <<'PY'
import base64, json, pathlib
p = pathlib.Path('/tmp/pi-imagegen-test/response.json')
data = json.loads(p.read_text())
raw = base64.b64decode(data['data'][0]['b64_json'])
out = pathlib.Path('/tmp/pi-imagegen-test/cube.png')
out.write_bytes(raw)
print(out, len(raw))
PY
```

Direct curl edit test:

```bash
curl -sS \
  -H 'Authorization: Bearer dummy' \
  -F model=gpt-image-2.5-flare \
  -F 'prompt=Replace only the background with a clean white studio backdrop; keep the subject unchanged' \
  -F n=1 \
  -F size=1024x1024 \
  -F quality=auto \
  -F output_format=png \
  -F image=@input.png \
  http://localhost:8317/v1/images/edits
```

## Common failures

- `Could not reach image endpoint`: the service is not running, `--base-url` is wrong, or networking is blocked.
- `HTTP 401/403`: set `PI_IMAGEGEN_API_KEY` or `OPENAI_API_KEY`, or confirm the proxy accepts `dummy`.
- `Output already exists`: choose a new output path or intentionally pass `--force`.
- `transparent background requires output-format png or webp`: switch output format.
- `Image endpoint returned non-JSON response`: proxy route is wrong or returned an HTML/error page.
- Bad text rendering: retry with shorter exact text, spell tricky words letter-by-letter, and constrain placement/typography.

## Useful environment setup

```bash
export PI_IMAGEGEN_BASE_URL=http://localhost:8317/v1
export PI_IMAGEGEN_MODEL=gpt-image-2.5-flare
export PI_IMAGEGEN_API_KEY=dummy
export PI_IMAGEGEN_OUTPUT_DIR="$HOME/.pi/generated_images"
```

For non-local endpoints, never paste secrets into chat. Set `PI_IMAGEGEN_API_KEY` or `OPENAI_API_KEY` in the shell/session environment.
