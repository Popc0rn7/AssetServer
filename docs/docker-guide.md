# Container service guide

AssetServer containerizes only heavyweight model and 3D worker processes. The
Gateway and local Materials/Articulated retrieval engine run directly in the
host Python environment. There is no Gateway image, root Dockerfile, Compose
deployment, or Docker-socket backend launcher.

Scene IR API 与 `scene-viewer` worker 必须部署同一版本的 `assetserver` Python 包。
Scene worker 镜像复制完整包，并在启动日志和共享 `data/runtime/scene-worker.json` 中记录
Scene IR schema/model 与镜像 build version。更新 Scene IR 或 procedural shell generator
后应执行：

```bash
scripts/docker_service.sh build scene-viewer
scripts/docker_service.sh run scene-viewer --gpu 0
docker logs -f assetserver-scene-viewer-worker
```

日志必须显示与 API 一致的 `scene_ir_model`；版本不一致属于部署错误，不应通过重试 scene
job 规避。

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

## Prepare checkpoints and datasets

Building an image does **not** download model weights or retrieval datasets.
Prepare only the services enabled in the deployment, before running their
containers or starting the Gateway.

Install the host tools first:

```bash
uv sync --group generate --group retrieve
```

### Model checkpoints

| Service | Required host path | Preparation |
| --- | --- | --- |
| SAM3D | `checkpoints/` | `scripts/download_sam3d_ckpt.sh` |
| OpenCLIP | `checkpoints/open_clip/` | `scripts/download_openclip_ckpt.sh` |
| Hunyuan3D | `checkpoints/Hunyuan3D-2/` | `scripts/download_hunyuan3d_checkpoints.sh` |
| Scene Viewer | none | Blender and Drake are installed in the image |
| Postprocess | none | COACD is installed in the image |

Download and verify SAM3D's complete offline bundle:

```bash
scripts/download_sam3d_ckpt.sh
scripts/download_sam3d_ckpt.sh --verify
```

The bundle includes SAM3, SAM 3D Objects, MoGe, and DINOv2 files and writes
`checkpoints/model-manifest.json`. The SAM3D container runs offline and the
lifecycle command validates this manifest before starting it. Set
`SAM3D_CHECKPOINTS=/absolute/path` only when intentionally using a different
host checkpoint root; the Docker mount in `docker/services.yaml` must match it.

Download and verify OpenCLIP:

```bash
scripts/download_openclip_ckpt.sh
uv run python -m assetserver.openclip_server.model_tool checkpoints/open_clip
```

This creates `checkpoints/open_clip/model-manifest.json`. Retrieval embeddings
must use the same model declared by the bundle.

Download Hunyuan3D:

```bash
scripts/download_hunyuan3d_checkpoints.sh
```

Add `--include-mini` only when `params.use_mini` is enabled. Use
`--checkpoint-dir PATH` when the checkpoint root is not `checkpoints/` and keep
the Docker mount consistent. The download scripts accept `HF_ENDPOINT` for an
explicit Hugging Face mirror.

### Retrieval datasets

Retrieval data is host-owned and is not copied into Docker images:

| Backend | Expected data | Preparation |
| --- | --- | --- |
| Materials | textures below `data/materials/`; embedding files below `data/materials/embeddings/` | Provision the licensed material dataset and its precomputed index |
| Articulated | SDF packages below `data/artvip_sdf/`; embedding files below `data/artvip_sdf/embeddings/` | Provision the SceneSmith-preprocessed ArtVIP dataset, then audit it |
| HSSD | `data/preprocessed/` and complete models below `data/hssd-models/` | Run the helper, then fetch the full model repository |
| Objaverse/ObjectThor | `data/objathor-assets/` with the configured `preprocessed/` index | Run the helper and verify the resulting configured paths |

The three embedding directories used by local retrieval must contain:

```text
clip_embeddings.npy
embedding_index.yaml
metadata_index.yaml
```

Audit an ArtVIP dataset before enabling articulated retrieval:

```bash
uv run python scripts/audit_artvip_dataset.py data/artvip_sdf
```

Prepare HSSD indexes and support surfaces, then download the full HSSD model
dataset into the same configured root:

```bash
scripts/download_hssd_data.sh
hf download hssd/hssd-models \
  --repo-type dataset \
  --local-dir data/hssd-models
```

The first command does not download the approximately 72 GB model repository.
Both steps are required before enabling `config/retrieve/hssd.yaml`.

Prepare ObjectThor/Objaverse data:

```bash
scripts/download_objaverse_data.sh
```

This is a large download (roughly 50 GB compressed). Before enabling the
backend, verify that `data/objathor-assets` resolves and that
`data/objathor-assets/preprocessed` contains the index expected by
`config/retrieve/objaverse.yaml`. A successful download alone is not a readiness
check.

Materials and Articulated currently have no repository-owned dataset downloader
because their source data and licenses are deployment-specific. Do not enable
those backends until the configured roots and embedding files exist.

## Commands

```bash
scripts/docker_service.sh build sam3d
scripts/docker_service.sh run sam3d --gpu 0

scripts/docker_service.sh build openclip
scripts/docker_service.sh run openclip --gpu 0

scripts/docker_service.sh build hunyuan3d
scripts/docker_service.sh run hunyuan3d --gpu 0

scripts/docker_service.sh build scene-viewer
scripts/docker_service.sh run scene-viewer --gpu 0
```

Lifecycle commands:

```bash
scripts/docker_service.sh status
scripts/docker_service.sh logs openclip
scripts/docker_service.sh stop openclip
```

Build options are uniform, and note that changing ENV may change the image layer, while changing proxy is safe:

```bash
PYPI=https://mirror.example/simple \
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
