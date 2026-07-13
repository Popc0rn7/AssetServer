# AssetServer Gateway API

This document defines the public HTTP API exposed by the AssetServer gateway.
Backend service APIs, container paths, and storage implementation details are not
part of this contract.

> Status: this is the target v1 contract. The SAM3D generate-and-download behavior
> described here is being used as the implementation specification.

## Planned scene viewer API

> Status: design record only. These routes are not implemented yet and are not
> part of the current public v1 contract.

The scene viewer will combine fast Blender EEVEE previews with optional Drake
physical validation. Rendering and validation remain independently callable even
when they run in the same Docker image.

Planned gateway operations:

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/v1/scenes/render` | Submit a scene for asynchronous multi-view rendering. |
| `POST` | `/v1/scenes/validate` | Submit a scene for Drake physical validation. |
| `POST` | `/v1/scenes/inspect` | Run validation and rendering as one asynchronous job. |
| `GET` | `/v1/scene-jobs/{job_id}` | Read job phase, progress, timing, and errors. |
| `GET` | `/v1/scene-jobs/{job_id}/views` | List generated preview views and camera metadata. |

The initial render job state model is:

```text
queued -> loading_scene -> rendering -> completed
                                  \-> failed
```

The default preview renderer will be Blender EEVEE Next. Cycles may be added as
an explicit final-quality mode later. The scene request schema, durable output
contract, and validation issue schema will be specified before these routes are
implemented.

## Conventions

The examples assume the gateway is available at:

```text
http://127.0.0.1:7010
```

If `ASSETSERVER_API_KEY` is configured, send either:

```http
Authorization: Bearer <key>
```

or:

```http
X-AssetServer-Key: <key>
```

Errors use JSON. Clients should branch on `error`, not on the human-readable
`message` or `detail` field.

```json
{
  "error": "error_code",
  "message": "Human-readable explanation.",
  "retryable": false
}
```

## Generate with SAM3D

```http
POST /v1/generate/sam3d
Content-Type: multipart/form-data
```

This endpoint supports two delivery modes through the `download` form field.
Generation always creates a durable asset. The field controls whether the gateway
also downloads and returns that asset in the same request.

### Form fields

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `image` | file | yes | — | PNG, JPEG, or WebP source image. |
| `mode` | string | no | `foreground` | `foreground` or `object_description`. |
| `prompt` | string | conditional | — | Required when `mode=object_description`. |
| `threshold` | number | no | `0.5` | Segmentation threshold in the range `[0, 1]`. |
| `download` | boolean | no | `true` | Return the GLB when true; return asset metadata when false. |

Boolean form values are `true` and `false`, case-insensitive.

### Generate and download

With `download=true`, the gateway does not stream directly from SAM3D. It first
downloads the complete GLB to gateway-local temporary storage and verifies its
size and SHA-256 digest. Only then does it send a successful response to the
client.

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:7010/v1/generate/sam3d \
  -F 'image=@input.png' \
  -F 'mode=foreground' \
  -F 'threshold=0.5' \
  -F 'download=true' \
  -o model.glb
```

Successful response:

```http
HTTP/1.1 200 OK
Content-Type: model/gltf-binary
Content-Disposition: attachment; filename="<asset_id>.glb"
X-Generation-ID: <generation_id>
X-Asset-ID: <asset_id>
X-Asset-SHA256: <sha256>
Content-Length: <size_bytes>
```

The response body is the GLB file.

### Generate without downloading

With `download=false`, generation still creates and persists the asset, but the
gateway returns metadata instead of transferring the GLB.

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:7010/v1/generate/sam3d \
  -F 'image=@input.png' \
  -F 'mode=foreground' \
  -F 'download=false'
```

Successful response:

```http
HTTP/1.1 201 Created
Content-Type: application/json
```

```json
{
  "generation_id": "01f4...",
  "backend": "sam3d",
  "backend_version": "dev",
  "model_bundle_version": "sam3d-2026-07",
  "asset": {
    "asset_id": "29ab...",
    "media_type": "model/gltf-binary",
    "size_bytes": 123456,
    "sha256": "...",
    "download_url": "/v1/assets/sam3d/29ab..."
  }
}
```

## Download a SAM3D asset

```http
GET /v1/assets/sam3d/{asset_id}
```

This endpoint downloads an asset created by an earlier request. As with
generate-and-download, the gateway first obtains and verifies the complete file
in temporary storage before returning `200 OK`.

```bash
curl --fail-with-body \
  http://127.0.0.1:7010/v1/assets/sam3d/29ab... \
  -o model.glb
```

Successful response:

```http
HTTP/1.1 200 OK
Content-Type: model/gltf-binary
Content-Disposition: attachment; filename="<asset_id>.glb"
X-Asset-ID: <asset_id>
X-Asset-SHA256: <sha256>
Content-Length: <size_bytes>
```

## Partial success and recovery

Generation and delivery are separate internal stages even when the caller uses
`download=true`.

If generation succeeds but the gateway cannot obtain or verify the GLB, it must
not run generation a second time. It returns a recoverable error containing the
created asset ID:

```http
HTTP/1.1 502 Bad Gateway
Content-Type: application/json
```

```json
{
  "error": "asset_delivery_failed",
  "message": "Generation succeeded, but the generated asset could not be delivered.",
  "generation_id": "01f4...",
  "asset_id": "29ab...",
  "download_url": "/v1/assets/sam3d/29ab...",
  "retryable": true
}
```

Clients should retry the supplied `download_url`; they should not resubmit the
generate request.

## Retrieve existing resources

Gateway currently supports the `materials` and `articulated` sources. Retrieval
returns ranked candidates by default; source dataset paths are never exposed.

```http
POST /v1/retrieve/{source}
Content-Type: application/json
```

### Materials

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:7010/v1/retrieve/materials \
  -H 'Content-Type: application/json' \
  -d '{"description":"warm hardwood floor","num_candidates":3}'
```

### Articulated

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:7010/v1/retrieve/articulated \
  -H 'Content-Type: application/json' \
  -d '{"description":"wooden wardrobe cabinet","object_type":"FURNITURE","desired_dimensions":[1.2,0.5,2.0],"num_candidates":3}'
```

Fields common to both sources:

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `description` | string | yes | — | Semantic retrieval query. |
| `num_candidates` | integer | no | `1` | Number of ranked candidates, from 1 to 20. |
| `download` | boolean | no | `false` | Return a ZIP directly; valid only with `num_candidates=1`. |

Articulated also accepts `object_type` and optional
`desired_dimensions: [width, depth, height]`.

Candidate response:

```json
{
  "source": "materials",
  "query": "warm hardwood floor",
  "results": [
    {
      "asset_id": "stable-id",
      "score": 0.83,
      "metadata": {
        "category": "Wood"
      },
      "download_url": "/v1/assets/materials/stable-id"
    }
  ]
}
```

With `download=true` and exactly one candidate, the response is the validated
ZIP file rather than JSON. Materials ZIPs contain the PBR textures and a
manifest; articulated ZIPs contain the selected object's SDF directory and a
manifest.

### Download a retrieved asset

```http
GET /v1/assets/{source}/{asset_id}
```

The gateway packages an asset from its read-only dataset into a local cache,
validates the package, then returns:

```http
Content-Type: application/zip
Content-Disposition: attachment; filename="<asset_id>.zip"
X-Asset-ID: <asset_id>
X-Asset-SHA256: <sha256>
```

The package cache is derived data. It can be deleted and regenerated from the
source dataset.

## Error responses

| Status | Error | Meaning | Retry guidance |
| --- | --- | --- | --- |
| `400` | `invalid_request` | Invalid form value or incompatible fields. | Correct the request. |
| `401` | `unauthorized` | Missing or invalid gateway API key. | Correct credentials. |
| `404` | `asset_not_found` | The requested asset does not exist. | Do not retry unchanged. |
| `413` | `image_too_large` | Uploaded image exceeds the configured limit. | Upload a smaller image. |
| `415` | `unsupported_image_type` | Unsupported source image media type. | Use PNG, JPEG, or WebP. |
| `422` | `invalid_generation_options` | Generation fields failed validation. | Correct the fields. |
| `429` | `rate_limit_exceeded` | Gateway rate limit exceeded. | Retry with backoff. |
| `502` | `asset_delivery_failed` | Generation succeeded, but delivery failed. | Retry `download_url`. |
| `502` | `backend_protocol_error` | Backend returned an invalid response. | Retry; inspect gateway logs if persistent. |
| `503` | `backend_unavailable` | SAM3D is unavailable or not ready. | Retry with backoff. |
| `503` | `embedding_provider_unavailable` | OpenCLIP is unavailable or not ready. | Retry with backoff. |
| `500` | `generation_failed` | Generation failed before an asset was created. | Inspect `retryable`. |

## Static SDF scenes

Enable the scene API with `runtime.scene_server.enabled=true`. Version 1 stores a
static SDF scene package, lets one agent replace the SDF through immutable
revisions, obtains synchronous previews from an external renderer, and downloads
any revision as a complete package.

The initial ZIP must contain exactly one `scene.sdf` at its root. Referenced
meshes and textures use relative paths such as `meshes/chair.glb`. Absolute paths,
`file://` and network URIs, symbolic links, and paths containing `..` are rejected.
Version 1 supports static `model`, `link`, `visual`, `pose`, and mesh content; it
does not define articulated joint state or animation behavior.

### Create a scene

```http
POST /v1/scenes
Content-Type: multipart/form-data
```

The multipart field is `package` and must contain a ZIP archive.

```bash
curl --fail-with-body -X POST http://127.0.0.1:7010/v1/scenes \
  -F 'package=@scene.zip;type=application/zip'
```

Successful response (`201 Created`):

```json
{
  "scene_id": "5d4992dc-6957-43d6-a43f-76f36db50c66",
  "revision": 1,
  "sdf_url": "/v1/scenes/5d4992dc-6957-43d6-a43f-76f36db50c66/sdf",
  "render_url": "/v1/scenes/5d4992dc-6957-43d6-a43f-76f36db50c66/render"
}
```

### Read or update the SDF

```http
GET /v1/scenes/{scene_id}/sdf
GET /v1/scenes/{scene_id}/sdf?revision=1
```

The response is `application/xml`. `X-Scene-Revision` identifies the returned
revision and `ETag` contains the SDF SHA-256 digest.

Create a new immutable revision by sending the complete SDF document:

```http
PUT /v1/scenes/{scene_id}/sdf
Content-Type: application/xml
X-Base-Revision: 1

<sdf version="1.10">...</sdf>
```

`X-Base-Revision` must be the latest revision. A stale write returns
`409 scene_revision_conflict`. Referenced assets must already exist in the
original scene package; version 1 has no separate asset mutation endpoint.

### Render a preview

```http
POST /v1/scenes/{scene_id}/render
Content-Type: application/json
```

```json
{
  "revision": 2,
  "views": ["top", "front", "side", "perspective"],
  "width": 512,
  "height": 512,
  "format": "webp"
}
```

All fields are optional. The revision defaults to latest and the remaining
values default to the example above. Rendering is synchronous. A successful
response is an `application/zip` containing one image named after each requested
view. The gateway forwards the complete scene package to the configured renderer;
see [the internal renderer contract](docs/RENDERER_API.md).

### Download a final scene package

```http
GET /v1/scenes/{scene_id}/final
GET /v1/scenes/{scene_id}/final?revision=2
```

The response ZIP contains `scene.sdf` for the selected revision plus the shared
asset tree. The response includes `X-Scene-ID`, `X-Scene-Revision`, and
`X-Scene-SHA256`. There is no separate publish/finalize state in version 1.

Scene API errors add the following codes to the common error envelope:

| Status | Error | Meaning |
| --- | --- | --- |
| `404` | `scene_not_found` | Scene or selected revision does not exist. |
| `409` | `scene_revision_conflict` | Base revision is stale. |
| `415` | `unsupported_scene_media_type` | Initial upload is not a ZIP. |
| `422` | `invalid_scene_package` | ZIP, SDF XML, path, or asset reference is invalid. |
| `422` | `invalid_sdf` | Updated SDF XML or an asset reference is invalid. |
| `502` | `render_failed` | Renderer rejected or returned an invalid result. |
| `503` | `render_backend_unavailable` | Renderer is absent or unreachable. |
| `504` | `render_timed_out` | Synchronous rendering exceeded its timeout. |

An error response must preserve the backend status and structured detail when
possible. The gateway must not replace a backend error with a generic JSON parse
error.

## Operational endpoints

These endpoints describe the gateway itself. They do not expose backend-private
HTTP routes.

### Health

```http
GET /health
```

Returns gateway health and high-level configuration state.

### Tools

```http
GET /tools
```

Lists configured generation and retrieval capabilities.

### Backends

```http
GET /backends
```

Returns gateway-visible backend availability and lifecycle state.

### Request history

```http
GET /history
```

Returns recent gateway requests. SAM3D entries should distinguish generation
from delivery so a partial success is visible:

```json
{
  "request_id": "...",
  "backend": "sam3d",
  "generation_status": "completed",
  "delivery_status": "failed",
  "asset_id": "29ab...",
  "status_code": 502,
  "duration_ms": 1234.5
}
```

## Gateway delivery guarantees

For responses containing an asset, the gateway must:

1. Download into a uniquely named temporary `.part` file.
2. Enforce a configured maximum asset size.
3. Verify the byte count and SHA-256 digest supplied by the backend.
4. Rename the verified file before beginning the client response.
5. Return `Content-Length` and asset identity headers.
6. Remove gateway-local temporary files after the response completes.

The gateway's temporary copy is not an authoritative asset store. A durable
asset remains addressable through `GET /v1/assets/sam3d/{asset_id}` until the
backend's retention policy removes it.
