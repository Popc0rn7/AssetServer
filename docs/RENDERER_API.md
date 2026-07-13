# Internal Static Scene Renderer API

This legacy v1 contract separates AssetServer scene storage from the heavy
Blender/Drake runtime. New integrations use Scene IR v2 and resolve
`asset://sha256/...` references from the shared `data/assets` volume. Agents do
not upload model binaries to either contract.

## Render

```http
POST /v1/render
Content-Type: multipart/form-data
```

Multipart fields:

| Field | Content | Required |
| --- | --- | --- |
| `package` | ZIP containing root `scene.sdf` and its relative assets | yes |
| `options` | JSON string with `views`, `width`, `height`, and `format` | yes |

Example options:

```json
{
  "views": ["top", "front", "side", "perspective"],
  "width": 512,
  "height": 512,
  "format": "webp"
}
```

A successful response is `200 application/zip`. It must contain exactly one
image per requested view, named `<view>.<format>`. AssetServer validates the
media type, ZIP integrity, and returned view names before forwarding it.

Failures use the AssetServer error envelope:

```json
{
  "error": "render_failed",
  "message": "The scene could not be loaded.",
  "retryable": false
}
```

The renderer owns SDF-to-render-engine conversion, cameras, lighting, Blender or
Drake installation, GPU assignment, and temporary-file cleanup. It must not rely
on AssetServer filesystem paths; the uploaded ZIP is the complete input.

Version 1 only requires static SDF visual geometry. Joint state, animation,
collision validation, and physics simulation are outside this contract.
