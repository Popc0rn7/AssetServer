ARG CUDA_VERSION=12.4.0
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu22.04 AS builder-base
ARG PYTHON_VERSION=3.11
ARG TORCH_VERSION=2.5.1
ARG TORCHVISION_VERSION=0.20.1
ARG UV_VERSION=0.8.22
ARG PYPI_INDEX_URL=""
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
       python${PYTHON_VERSION} python${PYTHON_VERSION}-dev python${PYTHON_VERSION}-venv \
       git git-lfs cmake ninja-build build-essential libgl1 libegl1 libglib2.0-0 \
       libxrender1 libsm6 libxext6 \
    && rm -rf /var/lib/apt/lists/*
RUN python${PYTHON_VERSION} -m venv /opt/venv \
    && if [ -n "$PYPI_INDEX_URL" ]; then export PIP_INDEX_URL="$PYPI_INDEX_URL"; fi \
    && /opt/venv/bin/pip install --no-cache-dir "uv==${UV_VERSION}" \
    && if [ -n "$PYPI_INDEX_URL" ]; then export UV_INDEX_URL="$PYPI_INDEX_URL"; fi \
    && /opt/venv/bin/uv pip install --python /opt/venv/bin/python \
       "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}"
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH CUDA_HOME=/usr/local/cuda \
    PYTHONUNBUFFERED=1

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
COPY pyproject.toml README.md ./
COPY assetserver assetserver
RUN uv pip install fastapi uvicorn python-multipart pillow numpy scipy \
       hydra-core omegaconf trimesh \
    && uv pip install --no-deps . \
    && python -c "import fastapi, torch, nvdiffrast, assetserver.sam3d_server.app"

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
    PYTHONPATH=/opt/sam3d/SAM3:/opt/sam3d/sam-3d-objects \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    XDG_CACHE_HOME=/var/cache/sam3d/xdg \
    XDG_CONFIG_HOME=/var/cache/sam3d/config \
    MPLCONFIGDIR=/var/cache/sam3d/matplotlib \
    HF_HOME=/var/cache/sam3d/hf TORCH_HOME=/var/cache/sam3d/torch \
    TORCH_EXTENSIONS_DIR=/var/cache/sam3d/torch-extensions \
    SAM3D_MODEL_ROOT=/models SAM3D_DINOV2_SOURCE=/opt/dinov2 \
    SAM3D_ASSET_ROOT=/var/lib/sam3d/assets SAM3D_IMAGE_VERSION=${IMAGE_VERSION}
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
COPY assetserver/openclip_server assetserver/openclip_server
RUN useradd --create-home --uid 10002 openclip \
    && mkdir -p /var/cache/openclip \
    && chown -R openclip:openclip /var/cache/openclip
ENV PYTHONPATH=/app OPENCLIP_MODEL_ROOT=/models \
    XDG_CACHE_HOME=/var/cache/openclip/xdg HF_HOME=/var/cache/openclip/hf \
    TORCH_HOME=/var/cache/openclip/torch HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1
USER openclip
EXPOSE 7006
ENTRYPOINT ["python", "-m", "assetserver.openclip_server.standalone"]
