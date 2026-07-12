#!/bin/bash

# Non-interactive SAM3D installation for Docker builds.
# Differences from install_sam3d.sh:
# - No interactive prompts (auto-accept everything).
# - Skips CUDA detection/installation (already in base image).
# - Skips HuggingFace checkpoint download (mounted at runtime).
# - Keeps: repo cloning, dependency installation, CUDA package builds.

set -euo pipefail

SAM3D_OBJECTS_COMMIT="${SAM3D_OBJECTS_COMMIT:-81a82373a3a7f4cbb00bd5b32aaf6b4d0f659ddd}"
SAM3_COMMIT="${SAM3_COMMIT:-11dec2936de97f2857c1f76b66d982d5a001155d}"
DINOV2_COMMIT="${DINOV2_COMMIT:-7764ea0f912e53c92e82eb78a2a1631e92725fc8}"
GITHUB_URL_PREFIX="${GITHUB_URL_PREFIX:-https://github.com/}"
GITHUB_URL_PREFIX="${GITHUB_URL_PREFIX%/}/"
PYPI_INDEX_URL="${PYPI_INDEX_URL:-}"
UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-300}"
export UV_HTTP_TIMEOUT
if [ -n "$PYPI_INDEX_URL" ]; then
    export UV_INDEX_URL="$PYPI_INDEX_URL"
    export PIP_INDEX_URL="$PYPI_INDEX_URL"
fi
STAGE="all"

usage() {
    cat <<'EOF'
Usage: scripts/install_sam3d_docker.sh [--stage N|NAME]

Stages:
  1, repos       Fetch both SAM3 / SAM 3D Objects repositories.
  repos-objects  Fetch only SAM 3D Objects.
  repos-sam3     Fetch only SAM3.
  repos-dinov2   Fetch only DINOv2.
  2, sam3        Install SAM3 editable package and torch_generic_nms.
  3, core        Install SAM 3D Objects core requirements.
  4, gsplat      Install gsplat.
  5, nvdiffrast  Install and optionally precompile nvdiffrast.
  6, kaolin      Install kaolin build tools and kaolin.
  7, pytorch3d   Install pytorch3d.
  8, inference   Install inference dependencies and MoGe.
  all            Run every stage. Default.

Environment:
  GITHUB_URL_PREFIX    GitHub URL prefix. Default: https://github.com/
  PYPI_INDEX_URL       Python package index URL.
  UV_HTTP_TIMEOUT      uv network timeout in seconds. Default: 300.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --stage|-s)
            if [ "$#" -lt 2 ]; then
                echo "Error: --stage requires a value." >&2
                exit 1
            fi
            STAGE="$2"
            shift 2
            ;;
        --stage=*)
            STAGE="${1#*=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$1'." >&2
            usage
            exit 1
            ;;
    esac
done

github_url() {
    local path="$1"
    printf "%s%s" "$GITHUB_URL_PREFIX" "$path"
}

fetch_repo() {
    local destination="$1"
    local repository="$2"
    local revision="$3"
    local attempt

    for attempt in 1 2 3; do
        rm -rf "$destination"
        mkdir -p "$destination"
        (
            cd "$destination"
            git init --quiet
            git remote add origin "$(github_url "$repository")"
            GIT_LFS_SKIP_SMUDGE=1 git -c http.version=HTTP/1.1 fetch --depth 1 origin "$revision"
            git checkout --detach FETCH_HEAD
        ) && return 0
        echo "Git fetch failed for ${repository} (attempt ${attempt}/3)" >&2
        sleep $((attempt * 5))
    done
    echo "Failed to fetch ${repository} at ${revision}" >&2
    return 1
}

uv_pip_install() {
    local attempt
    local args=(uv pip install)
    for attempt in 1 2 3; do
        if "${args[@]}" "$@"; then
            return 0
        fi
        echo "uv pip install failed (attempt ${attempt}/3): $*" >&2
        sleep $((attempt * 5))
    done
    "${args[@]}" "$@"
}

print_header() {
    echo "========================================="
    echo "SAM3D Docker Installation - stage ${STAGE}"
    echo "========================================="
    echo ""
    echo "Using CUDA_HOME: ${CUDA_HOME}"
    nvcc --version

    if [ "$GITHUB_URL_PREFIX" != "https://github.com/" ]; then
        echo "Using GitHub URL prefix: ${GITHUB_URL_PREFIX}"
        git config --global url."${GITHUB_URL_PREFIX}".insteadOf https://github.com/
    fi
    if [ -n "$PYPI_INDEX_URL" ]; then
        echo "Using PyPI index: ${PYPI_INDEX_URL}"
    fi
    echo "Using UV_HTTP_TIMEOUT: ${UV_HTTP_TIMEOUT}"
}

run_stage_1_objects() {
    echo ""
    echo "Stage 1a: Fetching SAM 3D Objects..."
    mkdir -p external
    fetch_repo external/sam-3d-objects facebookresearch/sam-3d-objects.git \
        "$SAM3D_OBJECTS_COMMIT"
}

run_stage_1_sam3() {
    echo ""
    echo "Stage 1b: Fetching SAM3..."
    mkdir -p external
    fetch_repo external/SAM3 facebookresearch/sam3.git "$SAM3_COMMIT"
}

run_stage_1_dinov2() {
    echo ""
    echo "Stage 1c: Fetching DINOv2..."
    fetch_repo /opt/dinov2 facebookresearch/dinov2.git "$DINOV2_COMMIT"
    rm -rf /opt/dinov2/.git
}

run_stage_1() {
    run_stage_1_objects
    run_stage_1_sam3
}

run_stage_2() {
    echo ""
    echo "Stage 2: Installing SAM3..."
    cd external/SAM3
    uv_pip_install .
    echo "SAM3 installed"

    echo ""
    echo "Installing torch_generic_nms for CUDA mask NMS..."
    uv_pip_install --no-build-isolation \
        "git+$(github_url ronghanghu/torch_generic_nms.git)"
}

run_stage_3() {
    echo ""
    echo "Stage 3: Installing SAM 3D Objects core dependencies..."
    cd external/sam-3d-objects
    grep -v -E "^(torch|torchvision|torchaudio|cuda-python|nvidia-|MoGe|flash_attn|bpy|wandb|jupyter|tensorboard|Flask|webdataset|sagemaker)" requirements.txt > /tmp/filtered_requirements.txt
    uv_pip_install -r /tmp/filtered_requirements.txt
}

run_stage_4() {
    echo ""
    echo "Stage 4: Installing gsplat..."
    uv_pip_install --no-build-isolation \
        "git+$(github_url nerfstudio-project/gsplat.git)@2323de5905d5e90e035f792fe65bad0fedd413e7"
}

run_stage_5() {
    echo ""
    echo "Stage 5: Installing nvdiffrast..."
    uv_pip_install --no-build-isolation \
        "git+$(github_url NVlabs/nvdiffrast.git)@${NVDIFFRAST_REVISION:-v0.4.0}"

    echo ""
    echo "Pre-compiling nvdiffrast CUDA extensions..."
    python3 << 'PYEOF'
import sys
import os

try:
    import torch

    if not torch.cuda.is_available():
        print("SKIP: CUDA not available - pre-compilation will happen on first use")
        sys.exit(0)

    print(f"GPU: {torch.cuda.get_device_name()}")
    print(f"CUDA: {torch.version.cuda}")
    print("Compiling nvdiffrast CUDA kernels...")

    import nvdiffrast.torch as dr
    ctx = dr.RasterizeCudaContext()

    import torch.utils.cpp_extension as cpp_ext
    build_dir = cpp_ext._get_build_directory("nvdiffrast_plugin", False)
    so_path = os.path.join(build_dir, "nvdiffrast_plugin.so")

    if os.path.exists(so_path):
        size_mb = os.path.getsize(so_path) / (1024 * 1024)
        print(f"SUCCESS: {so_path} ({size_mb:.1f} MB)")
    else:
        print("WARNING: .so file not found, compilation may have failed")
        sys.exit(1)

except Exception as e:
    print(f"Pre-compilation failed: {e}")
    print("NOTE: nvdiffrast will compile on first SAM3D use")
    sys.exit(0)  # Non-fatal.
PYEOF
}

run_stage_6() {
    echo ""
    echo "Stage 6: Installing kaolin 0.17.0..."
    uv_pip_install pip wheel cython==0.29.37
    uv_pip_install --no-build-isolation \
        "git+$(github_url NVIDIAGameWorks/kaolin.git)@v0.17.0"
}

run_stage_7() {
    echo ""
    echo "Stage 7: Installing pytorch3d from source..."
    uv_pip_install --no-build-isolation \
        "git+$(github_url facebookresearch/pytorch3d.git)@${PYTORCH3D_REVISION:-33824be3cbc87a7dd1db0f6a9a9de9ac81b2d0ba}"
}

run_stage_8() {
    echo ""
    echo "Stage 8: Installing inference dependencies..."
    uv_pip_install imageio utils3d

    echo ""
    echo "Installing MoGe depth model..."
    uv_pip_install "git+$(github_url microsoft/MoGe.git)@${MOGE_REVISION:-a8c37341bc0325ca99b9d57981cc3bb2bd3e255b}"
}

run_selected_stage() {
    case "$STAGE" in
        1|repos) run_stage_1 ;;
        repos-objects) run_stage_1_objects ;;
        repos-sam3) run_stage_1_sam3 ;;
        repos-dinov2) run_stage_1_dinov2 ;;
        2|sam3) run_stage_2 ;;
        3|core) run_stage_3 ;;
        4|gsplat) run_stage_4 ;;
        5|nvdiffrast) run_stage_5 ;;
        6|kaolin) run_stage_6 ;;
        7|pytorch3d) run_stage_7 ;;
        8|inference) run_stage_8 ;;
        all)
            run_stage_1
            run_stage_2
            run_stage_3
            run_stage_4
            run_stage_5
            run_stage_6
            run_stage_7
            run_stage_8
            ;;
        *)
            echo "Error: unknown stage '${STAGE}'." >&2
            usage
            exit 1
            ;;
    esac
}

print_header
run_selected_stage

echo ""
echo "========================================="
echo "SAM3D Docker Installation Stage Complete!"
echo "========================================="
echo ""
echo "Checkpoints must be mounted at runtime:"
echo "  -v ./checkpoints:/app/checkpoints"
echo ""
