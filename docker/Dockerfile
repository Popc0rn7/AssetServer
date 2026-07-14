ARG CUDA_VERSION=12.4.0

FROM python:3.11-slim AS postprocess-runtime
ARG PYPI_INDEX_URL=""
WORKDIR /app
RUN if [ -n "$PYPI_INDEX_URL" ]; then export PIP_INDEX_URL="$PYPI_INDEX_URL"; fi \
    && pip install --no-cache-dir \
       "coacd>=1.0.7" "fastapi>=0.115" "numpy>=1.26,<2" \
       "trimesh>=4.7.4" "uvicorn>=0.30" \
    && useradd --create-home --uid 10004 postprocess \
    && mkdir -p /data/postprocess/staging \
    && chown -R postprocess:postprocess /data/postprocess
ENV PYTHONPATH=/app ASSETSERVER_DATA_ROOT=/data \
    ASSETSERVER_POSTPROCESS_STAGING=/data/postprocess/staging \
    OMP_NUM_THREADS=4
COPY assetserver/__init__.py /app/assetserver/__init__.py
COPY assetserver/postprocess_server /app/assetserver/postprocess_server
COPY assetserver/postprocess /app/assetserver/postprocess
USER postprocess
EXPOSE 7100
ENTRYPOINT ["python", "-m", "assetserver.postprocess_server.standalone_server", "--host", "0.0.0.0", "--port", "7100"]

FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04 AS python-base
ARG PYTHON_VERSION=3.11
ARG UV_VERSION=0.8.22
ARG PYPI_INDEX_URL=""
ARG UV_HTTP_TIMEOUT=300
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
       python${PYTHON_VERSION} python${PYTHON_VERSION}-dev python${PYTHON_VERSION}-venv \
       git git-lfs cmake ninja-build build-essential libgl1 libegl1 libglib2.0-0 \
       libxrender1 libxkbcommon0 libsm6 libxext6 libxi6 libxxf86vm1 \
    && rm -rf /var/lib/apt/lists/*
RUN python${PYTHON_VERSION} -m venv /opt/venv \
    && if [ -n "$PYPI_INDEX_URL" ]; then export PIP_INDEX_URL="$PYPI_INDEX_URL"; fi \
    && /opt/venv/bin/pip install --no-cache-dir "uv==${UV_VERSION}"
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH CUDA_HOME=/usr/local/cuda \
    PYTHONUNBUFFERED=1 UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT}

FROM python-base AS builder-base
ARG TORCH_VERSION=2.5.1
ARG TORCHVISION_VERSION=0.20.1
ARG PYPI_INDEX_URL=""
RUN if [ -n "$PYPI_INDEX_URL" ]; then export UV_INDEX_URL="$PYPI_INDEX_URL"; fi \
    && /opt/venv/bin/uv pip install --python /opt/venv/bin/python \
       "numpy>=1.26,<2.0" \
       "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}"

FROM builder-base AS sam3d-builder
ARG TORCH_CUDA_ARCH_LIST
ARG SAM3_REVISION
ARG SAM3D_OBJECTS_REVISION
ARG NVDIFFRAST_REVISION
ARG PYTORCH3D_REVISION
ARG MOGE_REVISION
ARG DINOV2_REVISION
ARG GITHUB_URL_PREFIX=""
ARG PYPI_INDEX_URL=""
ENV TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST} VIRTUAL_ENV=/opt/venv \
    GITHUB_URL_PREFIX=${GITHUB_URL_PREFIX} PYPI_INDEX_URL=${PYPI_INDEX_URL} \
    SAM3_COMMIT=${SAM3_REVISION} SAM3D_OBJECTS_COMMIT=${SAM3D_OBJECTS_REVISION} \
    NVDIFFRAST_REVISION=${NVDIFFRAST_REVISION} \
    PYTORCH3D_REVISION=${PYTORCH3D_REVISION} MOGE_REVISION=${MOGE_REVISION} \
    DINOV2_COMMIT=${DINOV2_REVISION}
WORKDIR /app
COPY scripts/install_sam3d_docker.sh scripts/install_sam3d_docker.sh
RUN ln -s /opt/venv .venv
RUN bash scripts/install_sam3d_docker.sh --stage repos-objects
RUN bash scripts/install_sam3d_docker.sh --stage repos-sam3
RUN bash scripts/install_sam3d_docker.sh --stage repos-dinov2
RUN bash scripts/install_sam3d_docker.sh --stage sam3
RUN bash scripts/install_sam3d_docker.sh --stage core
RUN bash scripts/install_sam3d_docker.sh --stage gsplat
RUN bash scripts/install_sam3d_docker.sh --stage nvdiffrast
RUN bash scripts/install_sam3d_docker.sh --stage kaolin
RUN bash scripts/install_sam3d_docker.sh --stage pytorch3d
RUN bash scripts/install_sam3d_docker.sh --stage inference
RUN uv pip install fastapi uvicorn python-multipart pillow numpy scipy \
       hydra-core omegaconf trimesh \
    && python -c "import fastapi, torch, nvdiffrast, trimesh"

FROM sam3d-builder AS sam3d-runtime
ARG IMAGE_VERSION=dev
ARG TORCH_CUDA_ARCH_LIST
COPY docker/versions.env /etc/sam3d-versions.env
RUN mkdir -p /opt/sam3d \
    && mv /app/external/sam-3d-objects /opt/sam3d/sam-3d-objects \
    && mv /app/external/SAM3 /opt/sam3d/SAM3 \
    && find /opt/sam3d /opt/dinov2 -name .git -type d -prune -exec rm -rf {} + \
    && useradd --create-home --uid 10001 sam3d \
    && mkdir -p /var/lib/sam3d/assets /var/cache/sam3d/xdg \
       /var/cache/sam3d/config /var/cache/sam3d/matplotlib \
       /var/cache/sam3d/hf /var/cache/sam3d/torch \
       /var/cache/sam3d/torch-extensions \
    && ln -s /var/cache/sam3d/xdg /home/sam3d/.cache \
    && chown -R sam3d:sam3d /var/lib/sam3d /var/cache/sam3d \
    && printf '{"image_version":"%s","cuda_arch":"%s"}\n' \
       "${IMAGE_VERSION}" "${TORCH_CUDA_ARCH_LIST}" > /etc/sam3d-image.json
LABEL org.opencontainers.image.title="AssetServer SAM3D" \
      org.opencontainers.image.version="${IMAGE_VERSION}"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app:/opt/sam3d/SAM3:/opt/sam3d/sam-3d-objects \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    XDG_CACHE_HOME=/var/cache/sam3d/xdg \
    XDG_CONFIG_HOME=/var/cache/sam3d/config \
    MPLCONFIGDIR=/var/cache/sam3d/matplotlib \
    HF_HOME=/var/cache/sam3d/hf TORCH_HOME=/var/cache/sam3d/torch \
    TORCH_EXTENSIONS_DIR=/var/cache/sam3d/torch-extensions \
    SAM3D_MODEL_ROOT=/models SAM3D_DINOV2_SOURCE=/opt/dinov2 \
    SAM3D_ASSET_ROOT=/data/sam3d-v1 \
    ASSETSERVER_ASSET_ROOT=/data/assets \
    ASSETSERVER_STAGING_ROOT=/data/jobs/staging \
    SAM3D_IMAGE_VERSION=${IMAGE_VERSION}
COPY assetserver/__init__.py \
     assetserver/artifacts.py \
     assetserver/asset_store.py \
     assetserver/asset_normalization.py \
     assetserver/mesh_utils.py \
     assetserver/scheduler.py \
     assetserver/staging.py \
     /app/assetserver/
COPY assetserver/sam3d_server /app/assetserver/sam3d_server
COPY assetserver/geometry_generation_server /app/assetserver/geometry_generation_server
COPY assetserver/postprocess /app/assetserver/postprocess
COPY assetserver/utils /app/assetserver/utils
USER sam3d
EXPOSE 7000
ENTRYPOINT ["python", "-m", "assetserver.sam3d_server.standalone"]

FROM builder-base AS openclip-runtime
ARG OPENCLIP_VERSION=3.2.0
ARG PYPI_INDEX_URL=""
WORKDIR /app
RUN if [ -n "$PYPI_INDEX_URL" ]; then export UV_INDEX_URL="$PYPI_INDEX_URL"; fi \
    && uv pip install "open-clip-torch==${OPENCLIP_VERSION}" fastapi uvicorn \
       python-multipart pillow numpy
RUN useradd --create-home --uid 10002 openclip \
    && mkdir -p /var/cache/openclip \
    && chown -R openclip:openclip /var/cache/openclip
ENV PYTHONPATH=/app OPENCLIP_MODEL_ROOT=/models \
    XDG_CACHE_HOME=/var/cache/openclip/xdg HF_HOME=/var/cache/openclip/hf \
    TORCH_HOME=/var/cache/openclip/torch HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1
COPY assetserver/openclip_server /app/assetserver/openclip_server
USER openclip
EXPOSE 7006
ENTRYPOINT ["python", "-m", "assetserver.openclip_server.standalone"]

FROM python-base AS scene-viewer
ARG BLENDER_VERSION=4.5.4
ARG DRAKE_VERSION=1.47.0
ARG PYPI_INDEX_URL=""
WORKDIR /app
RUN if [ -n "$PYPI_INDEX_URL" ]; then export UV_INDEX_URL="$PYPI_INDEX_URL"; fi \
    && uv pip install \
       --index https://download.blender.org/pypi/ \
       "bpy==${BLENDER_VERSION}" "drake==${DRAKE_VERSION}" \
       "pydantic>=2" "PyYAML>=6" \
    && python -c "import bpy, pydrake, pydantic, yaml; print(bpy.app.version_string)"
RUN useradd --create-home --uid 10003 scene-viewer \
    && mkdir -p /var/lib/scene-viewer /var/cache/scene-viewer \
    && chown -R scene-viewer:scene-viewer \
       /var/lib/scene-viewer /var/cache/scene-viewer
ENV PYTHONPATH=/app \
    ASSETSERVER_DATA_ROOT=/data \
    ASSETSERVER_OUTPUT_ROOT=/outputs \
    XDG_CACHE_HOME=/var/cache/scene-viewer/xdg \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics
COPY assetserver/__init__.py \
     assetserver/asset_store.py \
     assetserver/blender_scene_worker.py \
     assetserver/jobs.py \
     assetserver/job_worker.py \
     assetserver/scene_compilers.py \
     assetserver/scene_ir.py \
     assetserver/scene_job_handlers.py \
     assetserver/simulation_assets.py \
     /app/assetserver/
COPY scripts/build_scene_from_ir.py /app/scripts/build_scene_from_ir.py
COPY scripts/smoke_scene_viewer.py /app/scripts/smoke_scene_viewer.py
USER scene-viewer
CMD ["python", "-m", "assetserver.job_worker", "--database", "/data/jobs/jobs.sqlite3", "--handler", "observe=assetserver.scene_job_handlers:observe", "--handler", "validate=assetserver.scene_job_handlers:validate", "--handler", "export=assetserver.scene_job_handlers:export", "--lease-seconds", "300", "--heartbeat-seconds", "30"]

FROM builder-base AS hunyuan3d-builder
ARG GITHUB_URL_PREFIX=""
ARG HUNYUAN3D_COMMIT=""
ARG PYPI_INDEX_URL=""
RUN apt-get update && apt-get install -y --no-install-recommends wget unzip \
    && rm -rf /var/lib/apt/lists/*
ENV GITHUB_URL_PREFIX=${GITHUB_URL_PREFIX} \
    HUNYUAN3D_COMMIT=${HUNYUAN3D_COMMIT} \
    PYPI_INDEX_URL=${PYPI_INDEX_URL}
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN if [ -n "$PYPI_INDEX_URL" ]; then export UV_INDEX_URL="$PYPI_INDEX_URL"; fi \
    && uv sync --active --frozen --inexact --no-dev --group generate --no-install-project
COPY scripts/install_hunyuan3d.sh /app/scripts/install_hunyuan3d.sh
COPY scripts/install_hunyuan3d_docker.sh /app/scripts/install_hunyuan3d_docker.sh
RUN bash scripts/install_hunyuan3d_docker.sh

FROM hunyuan3d-builder AS hunyuan3d-runtime
ENV PYTHONPATH=/app \
    HUNYUAN3D_MODEL_DIR=/models/Hunyuan3D-2 \
    HUNYUAN3D_MINI_MODEL_DIR=/models/Hunyuan3D-2mini \
    ASSETSERVER_ASSET_ROOT=/data/assets \
    ASSETSERVER_STAGING_ROOT=/data/jobs/staging \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics
COPY assetserver/__init__.py \
     assetserver/artifacts.py \
     assetserver/asset_store.py \
     assetserver/asset_normalization.py \
     assetserver/mesh_utils.py \
     assetserver/scheduler.py \
     assetserver/staging.py \
     /app/assetserver/
COPY assetserver/geometry_generation_server /app/assetserver/geometry_generation_server
COPY assetserver/postprocess /app/assetserver/postprocess
COPY assetserver/utils /app/assetserver/utils
EXPOSE 7002
CMD ["python", "-m", "assetserver.geometry_generation_server.standalone_server", "--host", "0.0.0.0", "--port", "7002", "--backend", "hunyuan3d"]
