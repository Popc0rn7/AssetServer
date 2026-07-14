# Configuration cleanup TODO

This document records configuration issues before changing behavior. Work through
the items independently; do not treat this as one large migration.

## Decisions

- `config/server.yaml` remains the main AssetServer configuration entry point.
- Keep `runtime` as a forward-looking compatibility layer. Do not delete it just
  because every field is not wired into the current process yet.
- OpenCLIP provider configuration lives in `config/openclip.yaml`.
- Scene API configuration lives under `server.scenes` because it is part of the
  main AssetServer process.
- The Blender/Drake scene worker intentionally has no user-facing YAML. It is a
  fixed SQLite pull worker managed by `docker/services.yaml`.
- Backend-specific definitions remain under `config/generate` and
  `config/retrieve`.
- Configuration shown to users must either affect behavior or be clearly marked
  as declarative metadata/compatibility configuration.

## P0: remove old backend directory compatibility

Completed:

- Removed the singular YAML key `backend_dir`.
- Removed the Python argument `load_assetserver_config(..., backend_dir=...)`.
- Updated stale `config/backend/*.yaml` loader documentation.

Completed naming and ownership change:

- Renamed `tool_dirs` to the top-level `backend` directory list.
- Backend YAML files own their `enabled` state.
- Removed inline backend enablement overrides from `server.yaml` and the loader.

## P1: make the dedicated OpenCLIP configuration truthful

Completed file split:

- `server.yaml` references `config/openclip.yaml` through its top-level
  `openclip` key.
- The loader resolves the reference into `cfg.openclip`.

Current behavior:

- `openclip.server` and `timeout_s` affect server behavior.
- `type`, `model`, `revision`, and `dimension` do not affect or validate Gateway
  behavior.
- The OpenCLIP service actually selects its model from
  `model-manifest.json`; `OPENCLIP_MODEL_ROOT` selects the bundle directory.
- Embedding responses already return `model`, `revision`, and `dimension`, so
  the Gateway has enough information to validate the declared contract.

Target split:

```yaml
# config/openclip.yaml
type: openclip_http
server:
  host: 127.0.0.1
  port: 7006
timeout_s: 30
expected_model: ViT-H-14-378-quickgelu
expected_revision: dfn5b
expected_dimension: 1024
```

Tasks:

- Decide whether model identity belongs in `server.yaml` as an expected service
  contract. Prefer the explicit `expected_*` names if it does.
- Validate embedding response metadata before accepting a vector; fail with a
  clear backend-contract error on model/revision/dimension mismatch.
- Optionally add a model-info endpoint so configuration can be checked during
  readiness rather than on the first retrieval request.
- Keep actual model selection in the OpenCLIP bundle manifest. If switching
  models is required, make the download/manifest tooling accept model,
  checkpoint, revision, and dimension instead of changing only Gateway config.
- Add tests for a matching contract, mismatched dimension, mismatched model, and
  configurations that omit the optional expectations.

## P2: define the `runtime` compatibility contract

Keep these sections for forward compatibility:

- `runtime.convex_decomposition`
- `runtime.postprocess_server`

Tasks:

- Document which fields are currently consumed and which are reserved.
- Do not present a reserved field as operational configuration in the README.
- Reconcile built-in defaults with `server.yaml`; currently the built-in
  `convex_decomposition.enabled` and `postprocess_server.enabled` values differ
  from the YAML values.
- Decide whether `convex_decomposition` is a lower-level implementation of
  `postprocess_server` or a separately configurable service. Avoid two active
  sources for the same host and port.
- Wire reserved fields only when service lifecycle/config ownership is clear.

## P3: untangle scene configuration

Completed:

- Kept scene APIs in the main server process.
- Kept scene settings under `server.scenes`; shared storage and job settings
  remain under `server.storage` and `server.jobs`.
- Renamed the switches to `legacy_sdf_api_enabled` and
  `scene_ir_api_enabled`.
- Unified v1 and v2 scene storage below `server.storage.data_root/scenes`.
- Removed unused lease/heartbeat settings and low-frequency size/render timeout
  settings from YAML; safety limits remain code constants.

Scene worker boundary:

- The worker exposes no HTTP API and needs no host/port configuration.
- Scene API and worker communicate only through the shared SQLite queue and
  shared `data`/`outputs` mounts.
- Queue path, handlers, lease, heartbeat, and container paths remain fixed by
  the worker implementation and Docker service definition.
- Do not add `config/scene-viewer.yaml`; GPU selection remains a lifecycle
  option of `scripts/docker_service.sh`.

## P4: remove duplicated backend enablement

Completed: backend YAML files are the single source of truth for `enabled`.

- Removed the inline `backends` enablement block from `server.yaml`.
- Stopped merging inline backend overrides in the loader.
- Each backend's complete declaration, including `enabled`, stays in its child
  YAML.

Add a resolved-config inspection command or startup summary so users can see the
effective backend state and the source of each override.

## P5: naming and documentation

- Completed: removed the separate `gateway` config section and kept the process
  configuration under `server`.
- Completed: removed API-key and rate-limit behavior.
- Completed: replaced configurable history/upstream/render limits with code
  constants.
- Once names and compatibility rules are settled, rewrite the README around one
  minimal working profile and link advanced configuration separately.
