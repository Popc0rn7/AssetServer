# Multi-stage Dockerfile for assetserver.
# Single-container setup for HTTP generate/retrieve model services.

# =============================================================================
# Stage 1: Base system with Python 3.11 and system dependencies.
# =============================================================================
FROM nvidia/cuda:12.4.0-devel-ubuntu22.04 AS base

ENV DEBIAN_FRONTEND=noninteractive

# Install Python 3.11 via deadsnakes PPA and system packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-dev \
    python3.11-venv \
    python3.11-distutils \
    libpython3.11-dev \
    git \
    git-lfs \
    wget \
    unzip \
    cmake \
    build-essential \
    # Minimal GL/EGL libs used by mesh/image dependencies in headless mode.
    libgl1 \
    libegl1 \
    libxrender1 \
    libxkbcommon0 \
    libsm6 \
    libxext6 \
    libxi6 \
    libxxf86vm1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Set Python 3.11 as default.
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

# Install uv package manager.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Set environment variables.
ENV CUDA_HOME=/usr/local/cuda-12.4
ENV PATH="${CUDA_HOME}/bin:${PATH}"
ENV LD_LIBRARY_PATH="${CUDA_HOME}/lib64:${LD_LIBRARY_PATH}"
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics

WORKDIR /app

# =============================================================================
# Stage 2: Python dependencies.
# =============================================================================
FROM base AS deps

COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --group retrieve --group generate

# =============================================================================
# Stage 3: Application code.
# =============================================================================
FROM deps AS app

# Copy full repo source.
COPY . .

# Remove stale bytecode.
RUN find /app/assetserver -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true

# Activate the virtual environment by prepending it to PATH.
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Default command: print usage help.
CMD ["python", "-c", \
    "print('assetserver Docker container\\n')\n\
print('Usage examples:\\n')\n\
print('  # Smoke test')\n\
print('  docker run --gpus all assetserver python -c \"import torch; print(torch.cuda.is_available()); import assetserver\"\\n')\n\
print('  # Run unit tests')\n\
print('  docker run --gpus all assetserver pytest tests/unit/ -x\\n')\n\
print('  # Run asset backend services (requires data volumes and API keys)')\n\
print('  docker compose up\\n')\n\
print('See README.md for full documentation.')"]
