from assetserver.config import backend_specs, config_to_container, load_assetserver_config


def test_server_config_embeds_scenes_and_loads_openclip_file():
    config = config_to_container(load_assetserver_config())

    assert "gateway" not in config
    assert "scene_server" not in config["runtime"]
    assert config["server"]["scenes"] == {
        "legacy_sdf_api_enabled": False,
        "scene_ir_api_enabled": True,
        "renderer_url": None,
    }
    assert config["openclip"]["type"] == "openclip_http"
    assert config["openclip"]["server"] == {"host": "127.0.0.1", "port": 7006}
    assert config["server"]["storage"] == {
        "data_root": "data",
        "output_root": "outputs",
    }
    assert config["server"]["jobs"] == {"max_attempts": 3}


def test_backend_state_comes_from_child_yaml_files():
    config = load_assetserver_config()

    assert [path.rsplit("/", 1)[-1] for path in config.backend] == [
        "generate",
        "retrieve",
    ]
    assert config.backends.sam3d.role == "generate"
    assert config.backends.sam3d.generation.pipeline.endswith(".sam3d")
    assert config.backends.sam3d.generation.preload is True
    assert config.backends.sam3d.generation.model.root == "checkpoints"
    assert dict(config.backends.sam3d.generation.sources) == {
        "sam3": "thirdparty/SAM3",
        "sam3d_objects": "thirdparty/sam-3d-objects",
        "dinov2": "thirdparty/dinov2",
    }
    assert config.backends.sam3d.generation.cache.root == "data/cache/sam3d"
    assert config.backends.hunyuan3d.generation.pipeline.endswith(".hunyuan3d")
    assert "params" not in config.backends.hunyuan3d


def test_backend_public_profiles_exclude_runtime_configuration():
    specs = {spec.name: spec for spec in backend_specs(load_assetserver_config())}

    materials = specs["materials"].to_dict()
    assert materials["config"]["output_kind"] == "material"
    assert materials["config"]["best_for"]
    assert "dataset" not in materials["config"]
    assert "server" not in materials["config"]
