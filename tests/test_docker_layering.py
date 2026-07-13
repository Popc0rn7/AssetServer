from pathlib import Path

import yaml


def test_one_project_dockerfile_and_no_gateway_or_compose_definitions():
    assert Path("docker/Dockerfile").is_file()
    assert not Path("Dockerfile").exists()
    assert not Path("Dockerfile.hunyuan3d").exists()
    assert not Path("docker/gateway").exists()
    assert not list(Path("docker").glob("*compose*"))
    assert not Path("docker/3d").exists()


def test_all_container_services_are_targets_in_one_stage_graph():
    dockerfile = Path("docker/Dockerfile").read_text()

    assert dockerfile.count("FROM nvidia/cuda:") == 1
    assert "FROM builder-base AS sam3d-builder" in dockerfile
    assert "FROM builder-base AS openclip-runtime" in dockerfile
    assert "FROM builder-base AS hunyuan3d-builder" in dockerfile
    assert "FROM python-base AS scene-viewer" in dockerfile
    assert "FROM hunyuan3d-builder AS hunyuan3d-runtime" in dockerfile


def test_shared_base_pins_numpy_and_has_configurable_timeout():
    dockerfile = Path("docker/Dockerfile").read_text()
    builder = dockerfile.split("FROM python-base AS builder-base", 1)[1].split(
        "FROM builder-base AS sam3d-builder", 1
    )[0]

    assert "ARG UV_HTTP_TIMEOUT=300" in dockerfile
    assert "UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT}" in dockerfile
    assert '"numpy>=1.26,<2.0"' in builder
    assert builder.index('"numpy>=1.26,<2.0"') < builder.index(
        '"torch==${TORCH_VERSION}"'
    )


def test_hunyuan_reuses_torch_base_and_copies_source_last():
    dockerfile = Path("docker/Dockerfile").read_text()
    hunyuan = dockerfile.split("FROM builder-base AS hunyuan3d-builder", 1)[1]

    assert "uv sync --active --frozen" in hunyuan
    assert "--no-install-project" in hunyuan
    assert hunyuan.index("RUN bash scripts/install_hunyuan3d_docker.sh") < hunyuan.index(
        "COPY assetserver/__init__.py"
    )


def test_sam3d_runtime_contains_its_lazy_pipeline_dependencies():
    dockerfile = Path("docker/Dockerfile").read_text()
    runtime = dockerfile.split("FROM sam3d-builder AS sam3d-runtime", 1)[1].split(
        "FROM builder-base AS openclip-runtime", 1
    )[0]

    assert "COPY assetserver/geometry_generation_server" in runtime
    assert "assetserver/mesh_utils.py" in runtime
    assert "COPY assetserver/utils" in runtime


def test_registry_is_the_only_runtime_container_source():
    registry = yaml.safe_load(Path("docker/services.yaml").read_text())["services"]

    assert set(registry) == {"sam3d", "openclip", "hunyuan3d", "scene-viewer"}
    assert {item["target"] for item in registry.values()} == {
        "sam3d-runtime",
        "openclip-runtime",
        "hunyuan3d-runtime",
        "scene-viewer",
    }
    assert all(item["image"].startswith("assetserver/") for item in registry.values())
    assert not Path("scripts/run_backend_docker.py").exists()


def test_gateway_and_local_retrieval_have_no_container_definition():
    assert not Path("docker/gateway").exists()
    server = Path("config/server.yaml").read_text()
    materials = Path("config/retrieve/materials.yaml").read_text()
    articulated = Path("config/retrieve/articulated.yaml").read_text()

    assert "docker:" not in server
    assert "docker:" not in materials
    assert "docker:" not in articulated
    assert "assetserver:latest" not in "\n".join(
        path.read_text() for path in Path("config").rglob("*.yaml")
    )
