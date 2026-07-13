# AssetServer

AssetServer is an HTTP backend for 3D model acquisition. It accepts reference
images or text descriptions, runs generation or retrieval backends, and returns
visual model files plus mandatory collision assets for physics systems.

It is not an agent runtime and does not own scene planning, placement,
validation loops, or simulation policy.

## What It Serves

- Image-conditioned 3D generation through SAM3D and Hunyuan3D.
- Text retrieval from Materials and Articulated datasets through the Gateway.
- Lightweight in-process mesh handling under `assetserver.postprocess`.
- Heavy postprocessing under `assetserver.postprocess_server`, currently
  including mandatory convex decomposition for collision geometry.

The default public transport is HTTP. Backend servers still expose their native
endpoints, but production callers should go through the gateway. The gateway
forwards traffic, keeps request history, and centralizes auth, timeout, and
rate-limit policy.

## Install

Use Python 3.11.

```bash
uv sync --group generate --group retrieve
```

SAM3D is delivered as a model-free service image plus a complete offline model
bundle. The bundle includes SAM3, SAM 3D Objects, MoGe, and DINOv2 weights; files
under runtime caches are never treated as required models.

The normal Docker workflow has three top-level shell commands:

```bash
scripts/download_sam3d_ckpt.sh
scripts/build_sam3d_docker.sh
scripts/run_sam3d_docker.sh
```

The defaults use the existing `checkpoints/` layout, image
`assetserver-sam3d:dev`, GPU 0, and `127.0.0.1:7000`. Run
`scripts/download_sam3d_ckpt.sh --verify` to validate `model-manifest.json`
without downloading anything. The manifest includes the exact MoGe and DINOv2
weight files currently stored under the historical `hf-cache` and `torch-cache`
directories. The container itself remains offline; clearing the separate
`sam3d-cache` Docker volume rebuilds runtime cache from the mounted checkpoints.

OpenCLIP is a separate GPU embedding service shared by retrieval sources. Its
weights are downloaded independently and its image reuses the CUDA, Python, uv,
and Torch layers from the SAM3D serving Dockerfile:

```bash
scripts/download_openclip_ckpt.sh
scripts/build_openclip_docker.sh
scripts/run_openclip_docker.sh
```

The explicit backend API is:

```text
POST /v1/sam3d/generations
GET  /v1/sam3d/assets/{asset_id}
GET  /health/live
GET  /health/ready
```

SAM3D requires its external repositories and checkpoints for non-Docker legacy
development:

```bash
bash scripts/install_sam3d.sh
```

To download or resume only the checkpoints, use:

```bash
bash scripts/download_sam3d_ckpt.sh
```

By default checkpoints are stored in `checkpoints/`. Use
`--checkpoint-dir /path/to/checkpoints` to place them elsewhere, then update the
SAM3D paths in `config/generate/sam3d.yaml`.

Hunyuan3D requires `external/Hunyuan3D-2`:

```bash
bash scripts/install_hunyuan3d.sh
```

Download or resume Hunyuan3D model weights into `checkpoints/Hunyuan3D-2`:

```bash
bash scripts/download_hunyuan3d_checkpoints.sh
```

Set `HF_ENDPOINT=https://hf-mirror.com` before running the script to use a
Hugging Face mirror. Add `--include-mini` if `params.use_mini` is enabled for
Hunyuan3D.

## Configuration

The main config is `config/server.yaml`. Backend declarations are discovered from:

- `config/generate/*.yaml`
- `config/retrieve/*.yaml`

SAM3D, Hunyuan3D, Materials, Articulated, and the postprocess server are
enabled by default. Materials and Articulated datasets must be present under
`data/` on the Gateway host; they are exposed only through HTTP ZIP downloads.

Enable retrieval backends with an override in `config/server.yaml`:

```yaml
backends:
  hssd:
    enabled: true
  objaverse:
    enabled: true
```

Postprocess defaults:

```yaml
runtime:
  postprocess_server:
    enabled: true
    host: 127.0.0.1
    port: 7100
    collision_method: coacd
    timeout_s: 300
```

The backend processes read these environment variables when calling
postprocess:

```bash
export ASSETSERVER_POSTPROCESS_HOST=127.0.0.1
export ASSETSERVER_POSTPROCESS_PORT=7100
export ASSETSERVER_COLLISION_METHOD=coacd
export ASSETSERVER_POSTPROCESS_TIMEOUT=300
```

## Run Servers

Start the mandatory postprocess server first:

```bash
uv run python -m assetserver.postprocess_server.standalone_server \
  --host 127.0.0.1 \
  --port 7100 \
  --omp-threads 4
```

Start SAM3D:

```bash
uv run python -m assetserver.geometry_generation_server.standalone_server \
  --host 0.0.0.0 \
  --port 7000 \
  --backend sam3d
```

Start Hunyuan3D:

```bash
uv run python -m assetserver.geometry_generation_server.standalone_server \
  --host 0.0.0.0 \
  --port 7002 \
  --backend hunyuan3d
```

Start HSSD retrieval after downloading data:

```bash
bash scripts/download_hssd_data.sh
uv run python -m assetserver.hssd_retrieval_server.standalone_server \
  --host 0.0.0.0 \
  --port 7001
```

Start Objaverse/ObjectThor retrieval after downloading data:

```bash
bash scripts/download_objaverse_data.sh
uv run python -m assetserver.objaverse_retrieval_server.standalone_server \
  --host 0.0.0.0 \
  --port 7007
```

Start the gateway:

```bash
uv run asset-acquisition-server --config config/server.yaml --host 0.0.0.0 --port 7010
```

Build and start the persistent Blender/Drake scene worker:

```bash
scripts/build_scene_viewer_docker.sh
scripts/run_scene_viewer_docker.sh --gpu 0
```

The worker shares `data/` with the gateway, claims jobs from
`data/jobs/jobs.sqlite3`, writes observations below `data/scenes`, and writes
only completed scene packages and ZIPs below `outputs/`. Use
`scripts/run_scene_viewer_docker.sh --smoke` for a one-shot Blender render, or
`--foreground` to run the worker attached. CPU-only startup is available with
`--no-gpu`.

Follow a detached worker with:

```bash
docker logs -f assetserver-scene-viewer-worker
```

It exposes operational endpoints plus the public APIs in `API.md`:

- `GET /health`
- `GET /tools`
- `GET /backends`
- `GET /history`
- `POST /v1/generate/sam3d`
- `POST /v1/retrieve/materials`
- `POST /v1/retrieve/articulated`
- `GET /v1/assets/{source}/{asset_id}`

The gateway routes model generation, coordinates lightweight local retrieval,
packages retrieved dataset assets, and records request state. OpenCLIP inference
remains a separate backend service.

Run the real retrieval smoke test only after OpenCLIP, Gateway, and local
datasets are ready:

```bash
.venv/bin/python tests/smoke/retrieve_gateway.py --download
```

## Gateway Docker Backend Launch

The gateway normally runs on the host and forwards traffic to backend servers.
Backend containers can be started manually, or the gateway can manage them
through the Docker SDK. Automatic launch is off by default. Enable it only on
trusted machines because access to
`/var/run/docker.sock` is effectively host-level control.

Build backend images as needed:

```bash
bash scripts/build_sam3d_image.sh --sudo
scripts/build_openclip_docker.sh --sudo
```

Run the gateway on the host without automatic Docker launch:

```bash
ASSETSERVER_DOCKER_LAUNCH_BACKEND=false \
uv run asset-acquisition-server --config config/server.yaml --host 0.0.0.0 --port 7010
```

Start model containers manually:

```bash
bash scripts/run_sam3d_docker.sh
scripts/run_openclip_docker.sh
```

If automatic launch is enabled, the gateway checks the requested backend before
proxying:

1. Start configured dependencies, including OpenCLIP for retrieval.
2. Start the requested model container if it is missing or stopped.
3. Wait for the model `/health` endpoint.
4. Execute or forward the original HTTP request.

The gateway never accepts image names or commands from request bodies. Docker
container settings come only from local YAML config:

- global services in `config/server.yaml`
- backend containers in `config/generate/*.yaml`
- retrieval source definitions in `config/retrieve/*.yaml`

Useful status endpoint:

```bash
curl http://127.0.0.1:7010/backends
```

Important environment variables:

```bash
export ASSETSERVER_DOCKER_LAUNCH_BACKEND=true
export ASSETSERVER_HOST_ROOT="$PWD"
```

`ASSETSERVER_HOST_ROOT` must point to the project directory on the Docker host.
The backend containers mount data, checkpoints, and outputs from that path.

## Request Scripts

Generate with SAM3D:

```bash
uv run python scripts/request_sam3d.py \
  --image-path /tmp/object.png \
  --description "red ceramic mug" \
  --output-dir outputs/sam3d_example
```

Generate with Hunyuan3D:

```bash
uv run python scripts/request_hunyuan3d.py \
  --image-path /tmp/object.png \
  --description "red ceramic mug" \
  --output-dir outputs/hunyuan3d_example
```

Retrieve from HSSD:

```bash
uv run python scripts/request_hssd.py \
  --description "modern wooden chair" \
  --object-type FURNITURE \
  --dimensions 0.6,0.6,1.0 \
  --output-dir outputs/hssd_example
```

Retrieve from Objaverse/ObjectThor:

```bash
uv run python scripts/request_objaverse.py \
  --description "white ceramic mug" \
  --object-type MANIPULAND \
  --num-candidates 3 \
  --output-dir outputs/objaverse_example
```

All wrappers call the shared backend script:

```bash
uv run python scripts/request_backend.py sam3d --image-path /tmp/object.png --description "red cup"
uv run python scripts/request_backend.py hunyuan3d --image-path /tmp/object.png --description "red cup"
uv run python scripts/request_backend.py hssd --description "modern chair"
uv run python scripts/request_backend.py objaverse --description "ceramic mug"
```

Equivalent gateway calls use the same JSON payloads but route through port 7010:

```bash
curl -N http://127.0.0.1:7010/generate/sam3d \
  -H 'content-type: application/json' \
  -d '[{"image_path":"/tmp/object.png","output_dir":"outputs/sam3d_example","prompt":"red cup","backend":"sam3d"}]'

curl -N http://127.0.0.1:7010/generate/hunyuan3d \
  -H 'content-type: application/json' \
  -d '[{"image_path":"/tmp/object.png","output_dir":"outputs/hunyuan3d_example","prompt":"red cup","backend":"hunyuan3d"}]'

curl http://127.0.0.1:7010/v1/retrieve/materials \
  -H 'content-type: application/json' \
  -d '{"description":"warm hardwood floor","num_candidates":3}'
```

## HTTP API

Gateway:

- `POST /generate/{backend}` forwards to the backend's `POST /generate_geometries`.
- `POST /v1/retrieve/materials` and `POST /v1/retrieve/articulated` return
  Gateway-owned candidate metadata.
- `GET /v1/assets/{source}/{asset_id}` returns a packaged ZIP asset.
- `GET /history` returns recent gateway requests with backend, status, duration,
  and error information.

Backend generation:

- `POST /generate_geometries`
- Body: list of requests with `image_path`, `output_dir`, `prompt`, and optional
  `backend`, `scene_id`, `output_filename`.
- Response: NDJSON stream. Successful rows include `geometry_path`, `asset_id`,
  `download_url`, and `collision`.

Retrieval requests never accept `output_dir` or return container filesystem
paths. The full public contract is maintained in `API.md`.

Collision metadata:

```json
{
  "required": true,
  "status": "complete",
  "method": "coacd",
  "piece_count": 12,
  "assets": [
    {
      "index": 0,
      "mesh_path": "/abs/path/model_collision_0.obj",
      "asset_id": "abc123",
      "download_url": "/assets/abc123"
    }
  ]
}
```

Artifact download:

- `GET /assets/{asset_id}`
- Returns the registered GLB, GLTF, OBJ, STL, or binary artifact for the current
  server process.

## Docker

Build dedicated serving images:

```bash
bash scripts/build_sam3d_image.sh --sudo
bash scripts/build_hunyuan3d_image.sh --sudo
```

Both build scripts accept the same network environment variables. If GitHub is
slow or blocked, use a GitHub URL prefix:

```bash
GITHUB=https://gh-proxy.com/https://github.com/ \
  bash scripts/build_sam3d_image.sh --sudo
GITHUB=https://gh-proxy.com/https://github.com/ \
  bash scripts/build_hunyuan3d_image.sh --sudo
```

or an HTTP/SOCKS proxy reachable from the build container:

```bash
bash scripts/build_sam3d_image.sh --sudo \
  --proxy http://host.docker.internal:7890
bash scripts/build_hunyuan3d_image.sh --sudo \
  --proxy http://host.docker.internal:7890
```

Use a PyPI mirror for Python package downloads:

```bash
PYPI=https://pypi.tuna.tsinghua.edu.cn/simple \
  bash scripts/build_sam3d_image.sh --sudo
PYPI=https://pypi.tuna.tsinghua.edu.cn/simple \
  bash scripts/build_hunyuan3d_image.sh --sudo
```

The Hunyuan3D image clones `Tencent-Hunyuan/Hunyuan3D-2` during build. Hunyuan3D
model weights should be downloaded to `checkpoints/Hunyuan3D-2` before running
the container.

Run backends from their YAML config:

```bash
bash scripts/run_sam3d_docker.sh
bash scripts/run_hunyuan3d_docker.sh
```

The scripts read `config/generate/sam3d.yaml` and
`config/generate/hunyuan3d.yaml`, including image, port, command, GPU flag, and
volume mounts. Use `scripts/run_backend_docker.py BACKEND --print` to inspect
the generated `docker run` command.
