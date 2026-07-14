# Container service guide

AssetServer containerizes only heavyweight model and 3D worker processes. The
Gateway and local Materials/Articulated retrieval engine run directly in the
host Python environment. There is no Gateway image, root Dockerfile, Compose
deployment, or Docker-socket backend launcher.

## Authoritative files

```text
docker/
├── Dockerfile       # every container image target
├── services.yaml    # image, port, GPU, mount and environment registry
└── versions.env     # shared dependency and source revisions

scripts/
├── docker_service.sh
└── docker_service.py
```

`scripts/docker_service.sh` is the only build and lifecycle interface. The
Python helper is an implementation detail which reads `docker/services.yaml`.

For managed HTTP backends, the host address is owned by the backend YAML. SAM3D,
Hunyuan3D, and OpenCLIP read `server.host` and `server.port` from their respective
files under `config/`; `docker/services.yaml` keeps only the fixed container port
and readiness path. The lifecycle helper derives both the Docker port mapping
and readiness URL, so changing a backend port requires one edit.

## Stage graph

```text
python-base (CUDA development image, Python and uv)
├── builder-base (NumPy, Torch and Torchvision)
│   ├── sam3d-builder ── sam3d-runtime
│   ├── openclip-runtime
│   └── hunyuan3d-builder ── hunyuan3d-runtime
└── scene-viewer (Blender and Drake, without Torch)
```

The model images reuse the expensive CUDA/Python/Torch layers. Backend source is
copied only after dependency installation so ordinary application edits replace
small final layers. Scene Viewer deliberately branches before Torch.

## Commands

```bash
scripts/docker_service.sh build sam3d
scripts/docker_service.sh run sam3d --gpu 0

scripts/docker_service.sh build openclip
scripts/docker_service.sh run openclip --gpu 0

scripts/docker_service.sh build hunyuan3d
scripts/docker_service.sh run hunyuan3d --gpu 0

scripts/docker_service.sh build scene-viewer
scripts/docker_service.sh run scene-viewer --no-gpu
```

Lifecycle commands:

```bash
scripts/docker_service.sh status
scripts/docker_service.sh logs openclip
scripts/docker_service.sh stop openclip
```

Build options are uniform:

```bash
UV_HTTP_TIMEOUT=600 PYPI=https://mirror.example/simple \
  scripts/docker_service.sh build sam3d \
  --proxy http://host.docker.internal:7890 \
  --progress=plain
```

Use `--sudo` when the Docker daemon requires it and `--clean` only when an
intentional cache-free build is required.

## Images and runtime access

| Service | Image | GPU | Runtime mounts |
| --- | --- | --- | --- |
| SAM3D | `assetserver/sam3d:dev` | Required | `checkpoints:/models:ro`, `data:/data:rw`, host-owned cache |
| OpenCLIP | `assetserver/openclip:dev` | Required | `checkpoints/open_clip:/models:ro`, named runtime cache |
| Hunyuan3D | `assetserver/hunyuan3d:dev` | Required | `checkpoints:/models:ro`, `data:/data:rw`, host-owned cache |
| Scene Viewer | `assetserver/scene-viewer:dev` | Optional | `data:/data:rw`, `data/assets:/data/assets:ro`, `outputs:/outputs:rw` |

SAM3D, Hunyuan3D, and Scene Viewer run with the host UID/GID when touching bind
mounts. Their scripts do not recursively change ownership of shared `data/`.

OpenCLIP intentionally does not mount `data/`. Materials and Articulated read
dataset embeddings and metadata in the host Gateway, send only query text to
OpenCLIP, perform similarity search locally, and materialize the selected files
into `data/assets`. Its image endpoint likewise accepts uploaded image bytes,
not filesystem paths. A future offline dataset indexer must be a separate target
with an explicit read-only dataset mount.

## Shared storage contract

```text
data/assets          immutable content-addressed assets
data/jobs/staging    temporary producer staging
data/jobs            SQLite queue and job state
data/scenes          Scene IR revisions and observations
checkpoints          host-managed model weights
outputs              completed expanded exports and ZIPs only
```

Containers exchange immutable `asset_ref` values through the shared data store;
the Gateway never downloads intermediate 3D models from one backend and uploads
them to another.

The root `.dockerignore` remains intentional because every target uses the
repository root as its Docker build context.

## Host Gateway

Start backend containers explicitly, then run the Gateway on the host:

```bash
uv run asset-acquisition-server \
  --config config/server.yaml \
  --host 0.0.0.0 \
  --port 7010
```

The Gateway reports configured HTTP backends but never starts or stops Docker
containers.
