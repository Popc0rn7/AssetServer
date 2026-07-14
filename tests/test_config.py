from assetserver.config import config_to_container, load_assetserver_config


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
    assert config.backends.sam3d.enabled is True
    assert config.backends.materials.enabled is True
    assert config.backends.hunyuan3d.enabled is False
