# AssetServer

AssetServer is an HTTP backend for 3D model acquisition. It accepts reference
images or text descriptions, runs generation or retrieval backends, and returns
visual model files plus mandatory collision assets for physics systems.

It is not an agent runtime and does not own scene planning, placement,
validation loops, or simulation policy.

## What It Serves

- Image-conditioned 3D generation through SAM3D and Hunyuan3D.
- Text retrieval from HSSD and Objaverse/ObjectThor datasets.
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

SAM3D requires its external repositories and checkpoints:

```bash
bash scripts/install_sam3d.sh
```

Hunyuan3D requires `external/Hunyuan3D-2`:

```bash
bash scripts/install_hunyuan3d.sh
```

## Configuration

The main config is `config/server.yaml`. Backend declarations are discovered from:

- `config/generate/*.yaml`
- `config/retrieve/*.yaml`

SAM3D, Hunyuan3D, and the postprocess server are enabled by default. HSSD and
Objaverse are disabled by default because they require local dataset downloads.

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

It exposes:

- `GET /health`
- `GET /tools`
- `GET /backends`
- `GET /history`
- `POST /generate/{backend}`
- `POST /retrieve/{backend}`
- `GET /assets/{backend}/{asset_id}`

The gateway does not perform model generation, retrieval, or collision
decomposition. It forwards requests to enabled backend servers and records
gateway-level state.

## Gateway Docker API Mode

The gateway can manage backend containers through the Docker SDK. This mode is
off by default. Enable it only on trusted machines because access to
`/var/run/docker.sock` is effectively host-level control.

Build the shared image:

```bash
docker compose build
```

Run the gateway with Docker API access:

```bash
docker compose -f docker-compose.yaml -f docker-compose.docker-api.yaml up assetserver
```

When Docker mode is enabled, the gateway checks the requested backend before
proxying:

1. Start configured dependencies, currently `postprocess`.
2. Start the requested backend container if it is missing or stopped.
3. Wait for the backend `/health` endpoint.
4. Forward the original HTTP request.

The gateway never accepts image names or commands from request bodies. Docker
container settings come only from local YAML config:

- global services in `config/server.yaml`
- backend containers in `config/generate/*.yaml`
- backend containers in `config/retrieve/*.yaml`

Useful status endpoint:

```bash
curl http://127.0.0.1:7010/backends
```

Important environment variables:

```bash
export ASSETSERVER_DOCKER_ENABLED=true
export ASSETSERVER_DOCKER_IMAGE=assetserver:latest
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

curl -N http://127.0.0.1:7010/retrieve/hssd \
  -H 'content-type: application/json' \
  -d '[{"object_description":"modern chair","object_type":"FURNITURE","output_dir":"outputs/hssd_example"}]'
```

## HTTP API

Gateway:

- `POST /generate/{backend}` forwards to the backend's `POST /generate_geometries`.
- `POST /retrieve/{backend}` forwards to the backend's `POST /retrieve_objects`.
- `GET /assets/{backend}/{asset_id}` forwards to the backend's
  `GET /assets/{asset_id}`.
- `GET /history` returns recent gateway requests with backend, status, duration,
  and error information.

Backend generation:

- `POST /generate_geometries`
- Body: list of requests with `image_path`, `output_dir`, `prompt`, and optional
  `backend`, `scene_id`, `output_filename`.
- Response: NDJSON stream. Successful rows include `geometry_path`, `asset_id`,
  `download_url`, and `collision`.

Backend retrieval:

- `POST /retrieve_objects`
- Body: list of requests with `object_description`, `object_type`, `output_dir`,
  optional `desired_dimensions`, `scene_id`, `num_candidates`.
- Response: NDJSON stream. Result objects include `mesh_path`, `asset_id`,
  `download_url`, and `collision`.

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

Build and run the gateway image:

```bash
docker compose up assetserver
```

Run a backend in the image:

```bash
docker compose run --rm -p 7000:7000 assetserver \
  python -m assetserver.geometry_generation_server.standalone_server \
  --host 0.0.0.0 --port 7000 --backend sam3d
```

Mount dataset and checkpoint directories as shown in `docker-compose.yaml`.
