# AssetServer 前端集成 API

本文档描述 AssetServer Gateway 当前实际实现的公共 HTTP 契约，供前端及前端 Agent
直接集成。除“兼容接口”章节外，新代码只应使用 v2 API。

AssetServer 负责：

- 生成或检索 3D 资产，并返回不可变 `asset_ref`；
- 为刚体资产生成可用于仿真的凸碰撞内容；
- 保存具有不可变 revision 的 Scene IR；
- 异步生成观察图、物理校验结果和最终场景 ZIP；
- 在最终 ZIP 发布前验证 visual、simulation、collision、路径和摘要。

AssetServer 不规定前端必须使用哪种物理引擎。最终包中的
`compiled/simulation/scene.json` 是引擎无关的碰撞入口；Drake 文件只是附加适配器。

## 1. 快速接入

默认 Gateway 地址：

```text
http://127.0.0.1:7010
```

推荐工作流：

```text
GET /tools
  → POST /v2/retrieve/{source} 或 POST /v2/generate/{backend}
  → 检索结果需要 POST materialize，得到 asset_ref
  → 用 asset_ref 创建/更新 Scene IR
  → POST observe / validate / exports
  → 轮询 GET /v2/jobs/{job_id}
  → completed 后读取图片、校验结果或下载最终 ZIP
```

重要约束：

- `asset_ref` 是资产身份，不是 HTTP URL，格式固定为
  `asset://sha256/<64 位小写十六进制摘要>`。
- 检索 candidate 不能直接放进场景；必须先 materialize。
- Scene IR 更新是完整文档替换，不是 JSON Patch。
- revision 不可变；更新必须携带最新 `X-Base-Revision`。
- observe、validate、export 都是异步任务，提交返回 `202`，不能把提交响应当结果。
- 最终 ZIP 是前端获得完整 visual/simulation/collision 内容的公共交付物。
- v2 JSON 响应不会暴露服务器绝对路径，也不提供单独下载 GLB/COL 的公共接口。

当前 Gateway 代码没有 API Key 鉴权或 CORS 中间件。浏览器跨域部署应通过同源反向代理
或部署层显式配置 CORS；不要假设 `Authorization` 或 `X-AssetServer-Key` 已生效。

## 2. 协议约定

### 2.1 坐标与单位

- 长度：米。
- 手性：右手系。
- up axis：`+Z`。
- Scene IR 旋转：RPY 欧拉角，单位为度。
- `transform_to_asset`：把 entrypoint 自身坐标转换到 canonical asset frame 的 4×4
  行列矩阵。
- Scene IR 的实例 transform：把 canonical asset frame 放置到场景世界坐标。
- `scale` 同时作用于视觉和碰撞几何。

### 2.2 内容类型

| 内容 | Content-Type |
| --- | --- |
| JSON 请求/响应 | `application/json` |
| Scene IR | `application/yaml`、`application/x-yaml` 或 `text/yaml` |
| 生成输入 | `multipart/form-data` |
| 观察图 | `image/webp` 或 `image/png` |
| 最终场景 | `application/zip` |

### 2.3 错误格式

Gateway 自己处理的业务错误通常为：

```json
{
  "error": "invalid_scene_ir",
  "message": "Human-readable explanation",
  "retryable": false
}
```

FastAPI 参数校验、部分代理错误或结果未就绪可能为：

```json
{
  "detail": "Result is not ready"
}
```

客户端应同时兼容 `error/message/retryable` 和 `detail`。HTTP 状态码始终是首要判断依据；
不要解析人类可读文本来决定业务分支。

### 2.4 长耗时请求

`POST /v2/generate/...` 和 materialize 会同步等待强制碰撞后处理，可能持续数分钟。
客户端应使用至少 360 秒的请求超时，并且不要因前端组件卸载自动重复生成请求。

## 3. 能力发现与运行状态

### `GET /health`

返回 Gateway 状态、服务配置和已启用 backend 数量。

### `GET /tools`

前端启动时应调用此接口发现可用路由和能力。

```json
{
  "enabled": [
    {
      "name": "sam3d",
      "type": "geometry_generation",
      "role": "generate",
      "enabled": true,
      "config": {}
    }
  ],
  "all": [],
  "routes": {
    "generate": "/v2/generate/{backend}",
    "retrieve": "/v2/retrieve/{source}",
    "materialize": "/v2/retrieve/{source}/{candidate_id}/materialize",
    "assets": "/v2/assets/{digest}",
    "scene_ir": "/v2/scenes",
    "scene_ir_schema": "/v2/scene-schema",
    "scene_jobs": "/v2/jobs/{job_id}",
    "observations": "/v2/observations/{observation_id}",
    "exports": "/v2/exports/{export_id}"
  },
  "deprecated_routes": {
    "generate": "/generate/{backend}",
    "retrieve": "/v1/retrieve/{source}",
    "assets": "/v1/assets/{source}/{asset_id}"
  }
}
```

实际字段取决于部署配置。前端不应硬编码某个 backend 一定启用。

FastAPI 还提供 `GET /openapi.json` 和交互式 `/docs`。它们适合生成基础 HTTP client，
但 generate/retrieve 的 backend-specific body 和最终 ZIP 语义仍以本文档及 `/tools` 为准。

### `GET /backends`

返回当前启用的 backend 声明：

```json
{"enabled": []}
```

### `GET /history`

返回 Gateway 最近最多 500 条代理请求记录。该接口用于诊断，不应作为业务状态源。

### `POST /shutdown`

仅返回 `{"status":"shutdown_requested"}`。当前实现不包含公共鉴权，生产环境应在反向
代理层禁止前端访问此路由。

## 4. 资产获取 v2

### 4.1 公共资产对象

生成或 materialize 成功后返回：

```json
{
  "asset_ref": "asset://sha256/0123...abcd",
  "kind": "object",
  "category": "chair",
  "description": "wood chair",
  "dimensions": [0.8, 0.9, 1.1],
  "preview_url": "/v2/assets/0123...abcd/preview",
  "source": {
    "type": "dataset",
    "name": "hssd",
    "resource_id": "chair-1",
    "dataset_version": "...",
    "conversion_version": "..."
  },
  "articulation": {
    "articulated": false,
    "joint_count": 0,
    "joints": []
  },
  "license": {}
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `asset_ref` | 场景中保存的不可变资产引用。 |
| `kind` | `object` 或 `material`；material 不能作为 Scene IR object/shell。 |
| `dimensions` | canonical frame 下的 `[x, y, z]` 尺寸；未知时为 `null`。 |
| `preview_url` | 可选预览图 URL；没有预览时为 `null`。 |
| `articulation` | 关节摘要；完整拓扑保存在最终包的资产 simulation 文件中。 |

当仿真后处理启用为 required 时，对刚体 object 返回的通常是新的派生 `asset_ref`，而不是
backend 最初生成的 raw ref。前端必须保存响应中的最终 ref，不能缓存或推断 raw ref。

### 4.2 生成资产

```http
POST /v2/generate/{backend}
Content-Type: multipart/form-data
```

当前几何生成 backend（如 `sam3d`、`hunyuan3d`）接受：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `image` | file | 是 | PNG、JPEG 或 WebP，最大 25 MiB。 |
| `prompt` | string | 否 | 生成提示词，默认空字符串。 |

示例：

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:7010/v2/generate/sam3d \
  -F 'image=@chair.png;type=image/png' \
  -F 'prompt=wooden dining chair'
```

成功：`201 Created`，返回公共资产对象并附加 `generation_id`。

```json
{
  "generation_id": "8bc9...",
  "asset_ref": "asset://sha256/...",
  "kind": "object",
  "category": "generated",
  "description": "wooden dining chair",
  "dimensions": [1.0, 0.8, 1.2],
  "preview_url": null,
  "source": {},
  "articulation": {"articulated": false, "joint_count": 0, "joints": []},
  "license": {}
}
```

### 4.3 检索候选

```http
POST /v2/retrieve/{source}
Content-Type: application/json
```

可用 source 以 `/tools` 为准。常用请求字段：

```json
{
  "description": "modern wooden desk",
  "num_candidates": 5,
  "object_type": "FURNITURE",
  "desired_dimensions": [1.2, 0.6, 0.75]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `description` | 是 | 非空语义检索文本。 |
| `num_candidates` | 否 | `1..20`，默认 `1`。 |
| `object_type` | 否 | articulated/HSSD 等 source 的过滤或排序条件。 |
| `desired_dimensions` | 否 | 期望 `[x,y,z]` 米尺寸，用于排序。 |

响应只包含轻量 candidate，不会复制 3D 文件：

```json
{
  "source": "hssd",
  "query": "modern wooden desk",
  "candidates": [
    {
      "candidate_id": "stable-candidate-id",
      "category": "desk",
      "description": "modern wooden desk",
      "dimensions": [1.2, 0.6, 0.75],
      "preview_url": null,
      "source": "hssd",
      "score": 0.91,
      "articulation": {
        "articulated": false,
        "joint_count": 0,
        "joints": []
      },
      "materialize_url": "/v2/retrieve/hssd/stable-candidate-id/materialize"
    }
  ]
}
```

### 4.4 Materialize 候选

```http
POST /v2/retrieve/{source}/{candidate_id}/materialize
```

也可以直接请求 candidate 返回的 `materialize_url`。请求无 body。成功为 `201 Created`，
返回公共资产对象。object 会在返回前完成所需仿真碰撞后处理；material 直接物化，不做刚体
碰撞处理。

同一个 candidate 可重复 materialize；CAS 和派生缓存保证相同内容得到相同 `asset_ref`。

### 4.5 读取资产元数据

```http
GET /v2/assets/{digest}
```

`digest` 是 `asset_ref` 最后一段，不包含 `asset://sha256/`。响应是公共资产对象。

```http
GET /v2/assets/{digest}/preview
```

有预览时返回 PNG/JPEG/WebP；没有时返回 `404`。该接口不是 GLB 或碰撞文件下载接口。

### 4.6 资产获取错误

| HTTP | 错误 | retryable | 处理方式 |
| --- | --- | --- | --- |
| `404` | backend/source/candidate/asset 不存在 | 否 | 刷新 `/tools` 或重新检索。 |
| `413` | image too large | 否 | 压缩或缩小输入。 |
| `415` | unsupported image type | 否 | 使用 PNG/JPEG/WebP。 |
| `422` | `postprocess_invalid_asset` | 否 | 该资产缺少有效 simulation/link/mesh；换候选或重新生成。 |
| `502` | 无法解析 backend 发布的资产 | 通常是 | 可退避重试；持续失败应检查服务。 |
| `503` | `postprocess_unavailable` | 是 | 指数退避，保留原请求上下文。 |

required 模式下后处理失败不会降级返回 visual triangle mesh。

## 5. Scene IR v2

### 5.1 Schema

```http
GET /v2/scene-schema
```

返回当前 Scene IR 的 JSON Schema。前端表单和 Agent 应优先使用此接口生成校验规则。

当前 schema version 是 `scene-ir/v1`，传输格式是 YAML。完整示例：

```yaml
schema_version: scene-ir/v1
description: furnished living room
rooms:
  - id: living_room
    type: room
    name: Living room
    transform:
      translation: [0.0, 0.0, 0.0]
      rotation_rpy_degrees: [0.0, 0.0, 0.0]
    shell:
      asset_ref: asset://sha256/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    metadata: {}
objects:
  - id: chair_1
    room_id: living_room
    name: Chair
    description: wooden dining chair
    category: furniture
    asset_ref: asset://sha256/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    transform:
      translation: [1.0, 2.0, 0.0]
      rotation_rpy_degrees: [0.0, 0.0, 90.0]
    scale: 1.0
    mobility: static
    initial_joints: {}
    placement: null
    immutable: false
    metadata: {}
metadata: {}
```

规则：

- 至少一个 room；room/object ID 必须唯一。
- `id`、`room_id`、joint name 只能使用字母、数字、`_`、`.`、`-`，最长 128。
- object 的 `room_id` 必须存在。
- 所有 `asset_ref` 必须已经存在且 `kind=object`。
- `scale` 必须大于 0。
- `mobility` 只能是 `static` 或 `dynamic`。
- `initial_joints` 的值是弧度或米，具体取决于关节类型，并必须处于资产声明 limits 内。
- `placement.parent_object_id` 必须引用另一个 object；不能引用自身。
- 未声明字段会被拒绝，不能把 UI 临时状态写进 Scene IR。
- `scene_id` 创建时可以省略；服务器写入后不能改成其他 ID。

`placement` 示例：

```yaml
placement:
  parent_object_id: table_1
  support_surface: tabletop
```

### 5.2 创建场景

```http
POST /v2/scenes
Content-Type: application/yaml
```

body 是完整 YAML。成功：

```http
HTTP/1.1 201 Created
```

```json
{
  "scene_id": "5d4992dc-6957-43d6-a43f-76f36db50c66",
  "revision": 1,
  "sha256": "...",
  "scene_url": "/v2/scenes/5d4992dc-6957-43d6-a43f-76f36db50c66"
}
```

### 5.3 读取场景

```http
GET /v2/scenes/{scene_id}
GET /v2/scenes/{scene_id}?revision=2
```

响应为 YAML，并包含：

```http
X-Scene-ID: <scene_id>
X-Scene-Revision: 2
ETag: "<scene yaml sha256>"
```

不传 revision 时读取最新 revision。

### 5.4 更新场景

```http
PUT /v2/scenes/{scene_id}
Content-Type: application/yaml
X-Base-Revision: 2
```

body 必须是完整新文档。`X-Base-Revision` 必须等于当前最新 revision。成功为
`201 Created`：

```json
{
  "scene_id": "...",
  "revision": 3,
  "sha256": "...",
  "size_bytes": 1234
}
```

若返回 `409 scene_revision_conflict`，前端应重新 GET 最新场景、重新应用用户修改，再提交；
不要盲目覆盖。

历史 revision 永远不会被后处理或 materialize 静默改写。替换 asset 必须创建新 revision。

## 6. 异步场景任务

### 6.1 提交与去重

```http
POST /v2/scenes/{scene_id}/observe
POST /v2/scenes/{scene_id}/validate
POST /v2/scenes/{scene_id}/exports
Content-Type: application/json
```

所有请求都可包含 `revision`；省略时固定到提交时的最新 revision。服务会对相同
`job_type + scene_id + revision + options` 去重。

```json
{
  "job_id": "...",
  "job_type": "observe",
  "scene_id": "...",
  "scene_revision": 3,
  "status": "queued",
  "status_url": "/v2/jobs/...",
  "deduplicated": false
}
```

提交成功总是 `202 Accepted`。`deduplicated=true` 表示返回了已有任务。

### 6.2 Observe 选项

```json
{
  "revision": 3,
  "views": ["top", "front", "side", "perspective"],
  "width": 512,
  "height": 512,
  "format": "webp"
}
```

| 字段 | 默认 | 约束 |
| --- | --- | --- |
| `views` | 四个标准视角 | 非空字符串数组。 |
| `width` / `height` | `512` | `1..4096`。 |
| `format` | `webp` | `webp` 或 `png`。 |

Observe 只消费 visual 内容。碰撞派生变化不会改变视觉渲染输入或无意义地触发重渲染。

### 6.3 Validate 选项

```json
{
  "revision": 3,
  "penetration_epsilon": 0.000001,
  "static_static": true,
  "support_contact_tolerance": 0.0001
}
```

校验结果示例：

```json
{
  "valid": false,
  "model_count": 4,
  "issues": [
    {
      "type": "penetration",
      "severity": "error",
      "object_ids": ["chair_1", "table_1"],
      "depth": 0.012,
      "metric": 0.012,
      "message": "objects penetrate by 0.012 m",
      "retryable": false
    }
  ]
}
```

### 6.4 Export 选项

Export 接受与 observe 相同的 `views/width/height/format`，用于 ZIP 内 previews 和
`.blend`。碰撞内容始终包含，不能通过选项关闭。

### 6.5 查询任务

```http
GET /v2/jobs/{job_id}
```

状态：`queued`、`running`、`completed`、`failed`、`cancelled`。

```json
{
  "job_id": "...",
  "job_type": "export",
  "scene_id": "...",
  "scene_revision": 3,
  "request": {"views": ["perspective"]},
  "status": "completed",
  "progress": 1.0,
  "attempt": 1,
  "max_attempts": 3,
  "result": {
    "export_id": "...",
    "download_url": "/v2/exports/...",
    "sha256": "...",
    "size_bytes": 12345678
  },
  "error": null,
  "created_at": 0.0,
  "started_at": 0.0,
  "finished_at": 0.0,
  "updated_at": 0.0
}
```

推荐轮询：运行中每 1 秒一次，连续 10 秒无变化后退避到 2–5 秒。页面刷新后只需保留
`job_id` 即可恢复。`failed` 时读取任务内：

```json
{
  "error": {
    "code": "invalid_collision_asset",
    "message": "...",
    "retryable": false
  }
}
```

注意：任务失败通常仍通过 `GET /jobs` 返回 HTTP `200`，状态在 body 中。

### 6.6 取消任务

```http
POST /v2/jobs/{job_id}/cancel
```

只能取消可取消状态的任务。成功返回完整 job；已经完成/失败/取消的任务返回 `409`。

## 7. 观察结果

任务 `completed` 后可以使用 job result 中的 URL。

### `GET /v2/observations/{observation_id}`

返回 observation manifest。每个 view 包含公开 URL，不包含服务器路径：

```json
{
  "schema_version": "observation/v2",
  "scene_id": "...",
  "scene_revision": 3,
  "scene_sha256": "...",
  "observation_id": "...",
  "renderer_version": "scene-ir-eevee/v2",
  "blender_version": "...",
  "render_device": "...",
  "options": {"views": ["top"], "width": 512, "height": 512, "format": "webp"},
  "views": [
    {
      "view": "top",
      "url": "/v2/observations/.../views/top",
      "camera_location": [1.0, 2.0, 3.0],
      "target": [0.0, 0.0, 0.0]
    }
  ]
}
```

### `GET /v2/observations/{observation_id}/views/{view}`

返回 `image/webp` 或 `image/png`，`Content-Disposition` 为 inline。结果未完成时返回
`409`；view 不存在时返回 `404`。

## 8. 最终场景 ZIP

### 8.1 下载

```http
GET /v2/exports/{export_id}
```

只有 export job completed 后可下载，否则返回 `409`。成功响应：

```http
Content-Type: application/zip
Content-Disposition: attachment; filename="<scene>-r<revision>.zip"
X-Scene-ID: <scene_id>
X-Scene-Revision: 3
X-Export-SHA256: <whole ZIP sha256>
Content-Length: <bytes>
```

前端下载完成后应计算整个 ZIP 的 SHA-256，并与 `X-Export-SHA256` 或 job result 中的
`sha256` 比较，再上传或进入下一阶段。

### 8.2 ZIP 目录结构

ZIP 所有条目位于顶层 `package/`：

```text
package/
  scene.yaml
  manifest.json
  checksums.sha256
  assets/
    sha256/<prefix>/<asset_digest>/
      manifest.json
      files/
        visual/...
        simulation/...
        collision/hull_000.obj
        preview/...
  compiled/
    simulation/scene.json
    drake/scene.dmd.yaml
    blender/scene.blend
  previews/
    top.webp
    front.webp
    ...
```

实际 entrypoint 名称来自每个 asset manifest，不能假设固定为 `model.glb` 或
`hull_000.obj`。`checksums.sha256` 覆盖发布前的包文件；每个 asset manifest 还记录其
自身文件大小和 SHA-256。

### 8.3 `package/manifest.json`

关键字段：

```json
{
  "schema_version": "scene-export/v2",
  "scene_id": "...",
  "scene_revision": 3,
  "scene_sha256": "...",
  "asset_digests": ["..."],
  "assets": ["asset://sha256/..."],
  "simulation_manifest": "compiled/simulation/scene.json",
  "versions": {
    "exporter": "scene-export/v2",
    "renderer": "scene-ir-eevee/v2",
    "blender": "...",
    "drake": "..."
  },
  "asset_tool_versions": {},
  "parameters": {}
}
```

### 8.4 引擎无关 simulation manifest

`package/compiled/simulation/scene.json` 是前端定位仿真内容的稳定入口。文件内的所有
`path` 都相对于 `package/`，而不是相对于 ZIP 根；ZIP 条目名需要在前面加
`package/`。

```json
{
  "schema_version": "simulation-scene/v1",
  "canonical_frame": {
    "units": "m",
    "handedness": "right",
    "up_axis": "+Z"
  },
  "assets": {
    "<asset_digest>": {
      "asset_ref": "asset://sha256/<asset_digest>",
      "asset_digest": "<asset_digest>",
      "simulation": {
        "path": "assets/sha256/ab/<asset_digest>/files/simulation/model.sdf",
        "sha256": "...",
        "base_link": "base",
        "transform_to_asset": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
      },
      "collision_geometries": [
        {
          "link": "base",
          "name": "assetserver_collision_000",
          "pose": [0,0,0,0,0,0],
          "representation": "convex-mesh",
          "path": "assets/sha256/ab/<asset_digest>/files/collision/hull_000.obj",
          "entrypoint": "collision/hull_000.obj",
          "sha256": "...",
          "method": "coacd",
          "profile": "rigid-object-v1",
          "parameters_sha256": "...",
          "transform_to_asset": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
        }
      ]
    }
  },
  "instances": [
    {
      "name": "chair_1",
      "asset_digest": "<asset_digest>",
      "mobility": "dynamic",
      "scale": 1.0,
      "transform": {
        "translation": [1,2,0],
        "rotation_rpy_degrees": [0,0,90]
      },
      "initial_joints": {}
    }
  ]
}
```

`collision_geometries[].representation`：

| 值 | 含义 |
| --- | --- |
| `convex-mesh` | 已声明的凸 mesh；常见为 CoACD OBJ。 |
| `primitive` | SDF/URDF 中的 box、sphere、cylinder 或 capsule；字段另含 `shape` 和 `parameters`。 |
| `mesh` | articulated asset 自带的 mesh；具体处理由前端选择。刚体不会以此形式通过导出。 |

碰撞 mesh 的局部 pose 在 `pose`，其 link/topology 在 simulation SDF/URDF。坐标应用规则：

```text
刚体 CoACD mesh local
  → collision geometry 的 transform_to_asset
  → instance scale
  → instance transform
  → world

primitive 或 articulated geometry local
  → collision pose
  → simulation 文件中的 link/joint/model topology
  → asset simulation.transform_to_asset
  → instance scale
  → instance transform
  → world
```

`pose` 的前三项是米制 xyz，后三项是弧度制 RPY；primitive 的尺寸参数同样使用米。
不要把 collision geometry 和 simulation 的两个 `transform_to_asset` 重复应用。对于复杂
articulated asset，simulation SDF/URDF 是 link topology 的权威来源。

articulated object 仍应使用 simulation 文件中的 link/joint topology 和
`instances[].initial_joints`。

### 8.5 单个 asset manifest

每个 `assets/sha256/.../<digest>/manifest.json` 都是 `asset/v2`，用于定位 visual 和
simulation 内容。关键结构：

```json
{
  "schema_version": "asset/v2",
  "digest": "<asset_digest>",
  "kind": "object",
  "canonical_frame": {
    "units": "m",
    "handedness": "right",
    "up_axis": "+Z",
    "origin": "ground-center"
  },
  "visual": {
    "entrypoint": "visual/model.glb",
    "transform_to_asset": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
    "parts": []
  },
  "simulation": {
    "entrypoint": "simulation/model.sdf",
    "base_link": "base",
    "transform_to_asset": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
  },
  "collision": [
    {
      "entrypoint": "collision/hull_000.obj",
      "method": "coacd",
      "profile": "rigid-object-v1",
      "parameters_sha256": "...",
      "transform_to_asset": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
    }
  ],
  "bounds": {"min": [-0.5,-0.5,0], "max": [0.5,0.5,1]},
  "joints": [],
  "support_surfaces": [],
  "files": [
    {
      "path": "visual/model.glb",
      "size_bytes": 12345,
      "sha256": "..."
    }
  ],
  "parent": {
    "asset_ref": "asset://sha256/<raw_digest>",
    "operation": "collision:rigid-object-v1",
    "operation_version": "1"
  }
}
```

asset manifest 内的 entrypoint 相对于该资产的 `files/`。前端展示视觉内容时应读取
`visual.entrypoint` 和 `visual.transform_to_asset`，不能根据文件名猜测。
`parent` 只用于 provenance；parent asset 不保证同时被打进最终 ZIP，前端运行时不得依赖它。

### 8.6 碰撞完整性保证

最终 ZIP 发布前，AssetServer 会验证：

- 每个必需 link 至少有一个 collision geometry；
- 所有 collision URI 都是资产内安全相对路径且能够解析；
- 刚体不能继续把 visual triangle mesh 当碰撞；
- 刚体 mesh collision 必须声明为 convex；
- collision、simulation 和所有复制文件的大小与 SHA-256 正确；
- `scene.json` 中每个实例引用已打包的 asset digest；
- ZIP 不包含主机绝对路径；
- checksums、`.blend` 和 Drake package 在相应运行时可用时通过校验。

导出失败时不会发布一个缺 COL 的“部分成功” ZIP。

### 8.7 Visual 与 collision 隔离

- Blender recipe 和 `.blend` 只加载 visual，不加载 `collision/`。
- 增加或重新生成 COL 不应改变视觉渲染结果。
- 前端视觉展示应读取 asset manifest 的 `visual` entrypoint；不要把 collision OBJ 当可见模型。
- 前端仿真是否采用 COL、采用哪个引擎，由前端决定。

## 9. 前端可靠性建议

1. 启动时调用 `/tools`，不要硬编码 backend 列表。
2. 保存完整 `asset_ref`；仅在调用 `/v2/assets/{digest}` 时拆出 digest。
3. candidate 选择后先 materialize，再更新 Scene IR。
4. 更新场景时保存 `X-Scene-Revision`，使用乐观并发控制处理 `409`。
5. 将 `job_id` 持久化到路由或状态仓库，刷新页面后继续轮询。
6. 下载 ZIP 时优先流式写入 Blob/文件，避免复制多份大 ArrayBuffer。
7. 校验 `X-Export-SHA256`，再解析 `compiled/simulation/scene.json`。
8. ZIP 解压时拒绝绝对路径和 `..`；不要仅依赖服务端已经验证。
9. 对 `retryable=true` 的 503/任务失败使用有上限的指数退避。
10. 不要自动重试非幂等 generate；应让用户明确重新生成。

## 10. 常见状态码

| HTTP | 含义 |
| --- | --- |
| `200` | 查询成功，或 job 状态成功返回。 |
| `201` | 新资产、场景或 revision 已创建。 |
| `202` | 异步任务已接受。 |
| `400` | JSON/body 格式错误。 |
| `404` | route 对应资源、backend、candidate、scene、job 或结果不存在。 |
| `409` | revision 冲突、任务不可取消或结果尚未 ready。 |
| `413` | 上传图片过大。 |
| `415` | Content-Type 不支持。 |
| `422` | Scene IR、生成输入、simulation 或 collision 内容无效。 |
| `429` | 后端队列或限流；按 `retryable`/响应语义退避。 |
| `502` | 上游 backend 协议或已发布资产无法解析。 |
| `503` | backend、渲染器、Drake 或 collision worker 暂不可用。 |
| `504` | 同步兼容渲染超时。 |

Scene/job 常见业务错误：

| 错误 | 说明 |
| --- | --- |
| `invalid_scene_ir` | YAML/schema/引用资产无效。 |
| `scene_revision_conflict` | `X-Base-Revision` 已过期。 |
| `job_not_found` | job 不存在。 |
| `job_not_cancellable` | job 已进入不可取消状态。 |
| `invalid_collision_asset` | 最终导出发现缺失或不安全的碰撞内容。 |
| `postprocess_invalid_asset` | 获取阶段无法生成有效凸碰撞。 |
| `postprocess_unavailable` | collision worker 暂不可用，可重试。 |
| `drake_unavailable` | validate worker 缺少 Drake，可重试或检查部署。 |
| `render_device_unavailable` | 渲染设备不可用。 |
| `invalid_render_options` | observe/export 渲染参数无效。 |

## 11. v1 兼容接口

以下接口仅用于旧客户端迁移，不应用于新前端：

| Method | Route | 说明 |
| --- | --- | --- |
| `POST` | `/generate/{backend}` | 旧 backend 透明代理。 |
| `POST` | `/generate_assets` | 更早期的批量资产管理协议，可能包含服务器目录字段。 |
| `GET` | `/assets/{asset_id}` | 与早期批量协议配套的资产下载。 |
| `POST` | `/v1/generate/sam3d` | 旧 SAM3D 生成协议。 |
| `GET` | `/v1/assets/sam3d/{asset_id}` | 下载旧生成 GLB。 |
| `POST` | `/v1/retrieve/{source}` | 旧检索，可返回 candidate 或 ZIP。 |
| `GET` | `/v1/assets/{source}/{asset_id}` | 下载旧检索 ZIP。 |
| `POST` | `/v1/scenes` | 上传根目录含 `scene.sdf` 的静态 ZIP。 |
| `GET/PUT` | `/v1/scenes/{scene_id}/sdf` | 读取或替换旧 SDF revision。 |
| `POST` | `/v1/scenes/{scene_id}/render` | 同步生成旧预览 ZIP。 |
| `GET` | `/v1/scenes/{scene_id}/final` | 下载旧静态场景 ZIP。 |

v1 与 v2 的身份、Scene IR、碰撞和导出契约不同，前端不要混用 v1 candidate/asset ID
和 v2 `asset_ref`。

## 12. 最小 TypeScript 调用示例

```ts
type ApiError = {
  error?: string;
  message?: string;
  retryable?: boolean;
  detail?: unknown;
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/assetserver${path}`, init);
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiError;
    throw Object.assign(
      new Error(body.message ?? String(body.detail ?? response.statusText)),
      { status: response.status, ...body },
    );
  }
  return response.json() as Promise<T>;
}

async function waitForJob(jobId: string, signal?: AbortSignal) {
  for (;;) {
    const job = await api<any>(`/v2/jobs/${jobId}`, { signal });
    if (["completed", "failed", "cancelled"].includes(job.status)) return job;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

async function materialize(materializeUrl: string) {
  return api<any>(materializeUrl, { method: "POST" });
}
```

示例假设前端通过 `/assetserver` 同源反向代理到 Gateway。最终 ZIP 是二进制响应，下载时
不要使用上述 JSON helper，应直接检查 `response.ok`、读取响应头并使用 `response.blob()`
或流式 API。
