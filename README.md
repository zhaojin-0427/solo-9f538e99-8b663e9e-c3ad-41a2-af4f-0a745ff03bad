# OSM 洪水分析水域面 API

按 OSM 关系号提交**逐次递增修订**的完整节点经纬度与无序 way（含 `outer` /
`inner` 角色），服务按节点引用拼接 way（允许线段反向），生成一个外环、至多
一个内环的 GeoJSON Polygon，并管理"当前供数面"的状态机。

技术栈：**FastAPI + Shapely + SQLite（WAL 模式）**，可 `docker compose` 启动。

## 快速启动

```bash
docker compose up --build
# 打开交互文档（可直接上传、取面）
open http://localhost:8000/docs
```

本地无 Docker 时：

```bash
pip install -r requirements.txt
FLOOD_DB_PATH=./flood.db uvicorn app.main:app --reload
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/relations/{relation_id}/revisions` | 上传一个完整修订（修订号必须严格递增） |
| GET  | `/relations/{relation_id}/revisions/{revision}` | 按修订号读取**原始输入 + 几何 + 状态** |
| GET  | `/relations/{relation_id}/revisions` | 列出该关系全部修订号与状态 |
| GET  | `/relations/{relation_id}/current` | 取当前供数面（最新有效修订） |
| GET  | `/relations/{relation_id}/revisions/{revision}/boundary-source` | 按修订号取**边界溯源**（逐边对应原始 way） |
| GET  | `/relations/{relation_id}/current/boundary-source` | 取当前供数面的边界溯源 |
| GET  | `/health` | 健康检查 |

### 上传请求体

```json
{
  "revision": 1,
  "nodes": [
    {"id": 1, "lat": 0.0, "lon": 0.0},
    {"id": 2, "lat": 0.0, "lon": 1.0},
    {"id": 3, "lat": 1.0, "lon": 1.0},
    {"id": 4, "lat": 1.0, "lon": 0.0}
  ],
  "ways": [
    {"id": 10, "role": "outer", "node_refs": [1, 2, 3, 4, 1]}
  ]
}
```

约束：每次提交为该修订的**完整快照**；最多 **8 条 way、32 个节点**；way
无序，拼接时可反向遍历；`node_refs` 闭合方式沿用 OSM 惯例（首尾节点相同）。

## 校验与状态机

以下情况修订标记为 `invalid`，**原始输入完整保留**，响应中返回错误码及相关
way id，当前面**停止供数**（`/current` 返回 409）：

- `missing_nodes`：way 引用了未提交的节点
- `broken_ring`：单 way 未闭合、way 间断环、外环拆成多个连通分量
- `fork`：三条以上 way 端点汇聚，或 way 端点落在另一条 way 内部（T 型分叉）
- `self_intersection`：环自交 / 节点重复访问（8 字形等）
- `inner_ring_violation`：内环触碰外环边界、越过外环或位于外环之外
- `missing_outer`：没有 `outer` 角色的 way

停止供数期间，**上一个有效面仍可按修订号读取**，响应里的
`last_valid_revision` 指向它；后续出现有效修订后 `/current` 自动恢复供数。

输入约束违例（超 8/32 上限、角色非法、修订号非递增等）返回 422 / 409，
**不落库**。

## 边界溯源

数据员发现异常边时，用下面的接口把**供数 Polygon 的每一条边**追回原始
OSM way 与节点对：

```bash
curl -s http://localhost:8000/relations/1/revisions/4/boundary-source
```

```json
{
  "relation_id": 1,
  "revision": 4,
  "source": "revision",
  "geometry": {"type": "Polygon", "coordinates": [[[1,0], [1,1], [0,1], [0,0], [1,0]]]},
  "rings": [
    {
      "ring_index": 0,
      "role": "outer",
      "edges": [
        {"edge_index": 0, "from_node": 2, "to_node": 3, "way_id": 11, "way_forward": true},
        {"edge_index": 1, "from_node": 3, "to_node": 4, "way_id": 13, "way_forward": false},
        {"edge_index": 2, "from_node": 4, "to_node": 1, "way_id": 12, "way_forward": false},
        {"edge_index": 3, "from_node": 1, "to_node": 2, "way_id": 14, "way_forward": true}
      ]
    }
  ]
}
```

- `rings` 的顺序与 `geometry.coordinates` 完全一致（外环在前，内环在后），
  `ring_index` 即其下标；`role` 为 `outer` / `inner`。
- `edge_index` 是该环内的边序号，`0` 对应首两点，`n` 对应第 `n` 与
  `n+1` 个坐标；环的闭合重复点不额外产生边，故边数 = 坐标数 - 1。
- `from_node` / `to_node` 是这条边在原 OSM 节点上的起止号。
- `way_id` 是贡献该边的原始 way；`way_forward` 表示拼接时是否沿原 way
  的 `node_refs` 顺序（`true`）还是反向遍历（`false`）。
- 溯源在**拼接时**逐段记录，而不是事后按坐标反查：way 被拆成多段、需要
  反向拼接、或上传时 way 顺序被打乱，每边仍准确对应其贡献 way；即便
  多条边坐标重合也不会错配。

`/current/boundary-source` 与 `/current` 同状态机：当前修订有效时返回
（`source` 为 `"current"`）；当前修订无效时返回 409 并给出
`last_valid_revision`，该旧有效修订的溯源仍可通过
`/revisions/{revision}/boundary-source` 按号读取；无效修订本身**不提供**
边界溯源（409，附其错误详情）。

## 一致性、原子性与持久化

- SQLite 启用 **WAL + `synchronous=FULL`**：提交即 fsync，重启后数据可查；
  读不阻塞写，读者只能看到完整提交的修订。
- 每次写入在单个 `BEGIN IMMEDIATE` 事务内完成"递增检查 + 插入"，进程内
  再以锁串行化；存储失败整体回滚，**不会留下半份修订**。
- 原始输入、几何、状态、**边界溯源**同事务同行写入（`provenance_json`），
  因此并发上传与读取看到的同一修订四者必然一致；存储层拒绝"有几何无溯源"
  或"有溯源无几何"的半份写入。旧库启动时自动补列迁移。
- 数据库位于挂载卷 `/data/flood.db`（环境变量 `FLOOD_DB_PATH` 可改）。

## 自测

```bash
pip install -r requirements.txt
pip install pytest httpx
pytest -q
```

覆盖：单/多 way 拼接与反向、内环挖洞、缺节点、断环、分叉、T 型分叉、自交、
内环触碰/越界、状态机恢复、修订号冲突、上下限校验、重启恢复、事务原子性、
并发上传/读取、`/docs` 可用性；以及边界溯源：逐边 way/节点对/方向、分段
反向拼接、输入 way 换序、内外环顺序、坐标重合不误配、无效修订 409、旧有效
版按号可查、重启与并发读写不串版、旧库迁移。
