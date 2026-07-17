# AssetServer

AssetServer is the 3D asset and scene execution backend for OmniSim Forge. It
retrieves or generates immutable assets, accepts versioned Scene IR, and runs
scene observation, physical validation, and export jobs.

Agents submit metadata and Scene IR only. Geometry generation, procedural room
shells, collision, materials, rendering, validation, and export stay inside the
service boundary. Public responses do not expose server filesystem paths.

## Capabilities

- Retrieve rigid objects, articulated assets, and materials.
- Generate objects through configured GPU backends.
- Publish immutable asset packages and unified `artifact/v1` resources.
- Store immutable `scene-ir/v1` revisions.
- Materialize procedural room shells with openings and collision geometry.
- Render multi-view observations through Blender EEVEE.
- Validate scenes through Drake and export portable scene packages.

AssetServer is not an Agent runtime. It does not own planning, aesthetic
judgment, workflow routing, or simulation policy.

## Requirements

- Python 3.11
- [`uv`](https://docs.astral.sh/uv/)
- An NVIDIA GPU for GPU-backed workers
- Docker only when using containerized workers

Install the host environment:

```bash
uv sync
```

Install either local worker environment only when it is needed:

```bash
uv sync --group scene-viewer
uv sync --group postprocess
uv sync --group retrieve
```

Manage the four local services with one lifecycle command:

```bash
scripts/launch_service.sh start all
scripts/launch_service.sh status
scripts/launch_service.sh logs scene-viewer
scripts/launch_service.sh stop all
```

The supported service names are `openclip`, `sam3d`, `scene-viewer`, and
`postprocess`. Use `run SERVICE` instead of `start SERVICE` to keep one service
in the foreground.

## Quick Start

Start the Gateway:

```bash
uv run asset-acquisition-server \
  --config config/server.yaml \
  --host 0.0.0.0 \
  --port 7010
```

Build and start the Blender/Drake scene worker:

```bash
scripts/docker_service.sh build scene-viewer
scripts/docker_service.sh run scene-viewer --gpu 0 --no-follow
docker logs --tail 80 assetserver-scene-viewer-worker
```

The Gateway and scene worker share `data/` and `outputs/`. Their startup logs
must report the same Scene IR model version.

Check the service:

```bash
curl --fail http://127.0.0.1:7010/health
curl --fail http://127.0.0.1:7010/v2/scene-schema
```

Additional retrieval, generation, embedding, and postprocess services are
started only when their backend configuration is enabled. See the Docker guide
for service-specific build commands, model mounts, ports, and GPU settings.

### Local SAM3D

SAM3D can run directly from the checkout without Docker. Its source dependencies,
model path, cache, and storage directories are declared by
`config/generate/sam3d.yaml`.

```bash
git submodule update --init --depth 1 \
  thirdparty/SAM3 thirdparty/sam-3d-objects thirdparty/dinov2
uv sync --extra sam3d
scripts/download_sam3d_ckpt.sh
scripts/launch_service.sh start sam3d --gpu 0
```

The local launcher only selects the Python environment and visible GPU; it does
not override backend paths. Hunyuan3D local-process management is not currently
supported.

## Configuration

The root configuration is [`config/server.yaml`](config/server.yaml). Backend
declarations live under:

```text
config/generate/*.yaml
config/retrieve/*.yaml
```

Runtime state uses these directories by default:

```text
data/assets/                    immutable asset store
data/procedural_room_shells/    generated shell geometry cache
data/scenes/                    Scene IR revisions and observations
data/jobs/                      durable scene job queue
data/runtime/                   API and worker version records
outputs/                        completed scene exports
```

Do not add complete rooms as fake assets. A room shell is submitted as Scene IR
and materialized by AssetServer.

## Development

Run formatting, lint, and tests:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run ruff format assetserver tests scripts
UV_CACHE_DIR=/tmp/uv-cache uv run ruff check assetserver tests scripts
UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q
```

Run the Blender pixel/AABB canary inside the scene worker image:

```bash
docker run --rm --gpus device=0 \
  --entrypoint python \
  assetserver/scene-viewer:dev \
  /app/scripts/check_procedural_room_canary.py
```

## Documentation

- [API.md](API.md): public HTTP, Scene IR, Artifact, observation, validation,
  and export contracts.
- [docs/docker-guide.md](docs/docker-guide.md): container architecture, service
  lifecycle, persistent mounts, and model deployment.
- [docs/RENDERER_API.md](docs/RENDERER_API.md): renderer integration boundary.
- OpenAPI UI: `http://127.0.0.1:7010/docs` when the Gateway is running.

Compatibility endpoints remain documented in `API.md`; new integrations should
use the v2 Gateway APIs.
