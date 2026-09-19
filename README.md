# Unicode 显示名防仿冒 API

命名空间（namespace）内的显示名注册服务，基于 **FastAPI + SQLite + confusables + unicodedataplus**，在注册时拦截三类仿冒：

| 判定 | 规则 | 结果 |
| --- | --- | --- |
| 规范名查重 | NFKC + casefold 后与同命名空间已有规范名（含**未过期预占**）比较 | 相同 → `409` |
| 同形字符 | 与已有名称及**未过期预占**逐条做 Unicode confusables 判定 | 可混淆 → `409` |
| 混脚本 | 忽略 Common / Inherited 后，同时含 Latin、Cyrillic、Greek 中两种及以上脚本 | `422`，并列出脚本及对应码点 |

原名始终原样落库，规范名（`normalized_name`）一并保存；`(namespace, normalized_name)` 在数据库层有唯一约束兜底并发写入。

需要"先占名、后完成注册"的场景（如多步注册向导）可使用**预占（reservation）**：预占成功返回不可猜测的 `token` 与 `expires_at`，有效期内该名称与已注册名称完全等价地参与冲突判断；随后用 token 确认（claim）即可正式注册。

## 快速开始（Docker，单条命令）

```bash
docker compose up --build
```

该命令完成镜像构建、依赖安装与数据库初始化（应用启动时自动建库建表并对旧版本库幂等补建预占表，SQLite 文件落在命名卷 `names-data` 中）。

- **API 地址：http://localhost:8000**
- **交互式文档（Swagger UI）：http://localhost:8000/docs**
- **端口：8000**（可在 `docker-compose.yml` 的 `ports` 中调整）

## 本地开发（单条命令）

```bash
./scripts/setup.sh    # 安装依赖 + 初始化数据库（./names.db）
uvicorn app.main:app --reload --port 8000
```

`setup.sh` 内部执行 `pip install -r requirements.txt` 与 `python -m app.database`；如需虚拟环境，先自行激活再运行该脚本即可。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/namespaces/{namespace}/names` | 注册显示名：`201` 成功；`409` 规范名相同或同形冲突（含未过期预占）；`422` 混脚本违规 |
| `POST` | `/namespaces/{namespace}/names/batch` | 批量注册（1–50 个）：全部通过才在**单个事务**中落库 → `201`；任一项混脚本或与库内/预占/批内名称冲突 → `422` 汇总明细；并发竞争使判定失效 → 回滚重新分析 → `409` |
| `POST` | `/namespaces/{namespace}/reservations` | **预占**显示名（`ttl_seconds` 30–600 秒）：`201` 返回不可猜测 `token` 与 `expires_at`；`409` 与已注册名称或其他未过期预占冲突；`422` 混脚本或 TTL 非法 |
| `POST` | `/namespaces/{namespace}/reservations/{token}/claim` | **确认预占并注册**：单事务校验后写入 → `201`；`404` token 不存在/不属于该命名空间；`409` 已消费（重复/并发确认）或当前冲突（不写入）；`410` 已过期 |
| `POST` | `/namespaces/{namespace}/check` | **只分析不落库**：返回规范名、脚本分析、码点与潜在冲突 |
| `GET` | `/namespaces/{namespace}/names` | 列出命名空间内已注册名称 |
| `GET` | `/health` | 健康检查 |

单条接口请求体为 `{"name": "<显示名>"}`（1–64 字符，非空白）；批量接口为 `{"names": ["<显示名>", ...]}`（1–50 个）；预占接口为 `{"name": "<显示名>", "ttl_seconds": 30–600}`。

### 示例

注册成功：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "paypal"}'
# 201 {"id":1,"namespace":"chat","original_name":"paypal","normalized_name":"paypal",...}
```

规范名相同（大小写 / 全半角等兼容字符折叠后重复）→ `409`：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "PAYPAL"}'
# 409 {"detail":{"error":"name_conflict","normalized_name":"paypal",
#      "conflicts":[{"id":1,"original_name":"paypal","reason":"normalized_match",...}]}}
```

同形字符（纯西里尔 `арр` 仿冒拉丁 `app`）→ `409`：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "арр"}'
# 409 {"detail":{"error":"name_conflict",
#      "conflicts":[{"original_name":"app","reason":"confusable",...}]}}
```

混脚本（拉丁 `ad` + 西里尔 `м` + 拉丁 `in`）→ `422`，列出脚本及码点：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "adмin"}'
# 422 {"detail":{"error":"mixed_script_violation",
#      "restricted_scripts_used":["Cyrillic","Latin"],
#      "script_details":[{"script":"Cyrillic","characters":["м"],"code_points":["U+043C"]},
#                        {"script":"Latin","characters":["a","d","i","n"],
#                         "code_points":["U+0061","U+0064","U+0069","U+006E"]}]}}
```

只分析不落库：

```bash
curl -X POST http://localhost:8000/namespaces/chat/check \
  -H 'Content-Type: application/json' -d '{"name": "арр"}'
# 200 {"normalized_name":"арр","mixed_script_violation":false,
#      "scripts":["Cyrillic"],"conflicts":[{"reason":"confusable",...}], ...}
```

批量注册：全部通过 → `201`，`items` 按原请求顺序返回并带 `index`：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names/batch \
  -H 'Content-Type: application/json' \
  -d '{"names": ["alice", "bob", "世界-123"]}'
# 201 {"namespace":"chat","items":[
#   {"index":0,"id":1,"original_name":"alice","normalized_name":"alice",...},
#   {"index":1,"id":2,"original_name":"bob","normalized_name":"bob",...},
#   {"index":2,"id":3,"original_name":"世界-123","normalized_name":"世界-123",...}]}
```

批量注册：任一项有问题 → `422` 汇总所有问题项（库内冲突 `conflicts`、
批内冲突 `batch_conflicts` 按原请求索引互相标注、混脚本明细），**一条都不落库**：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names/batch \
  -H 'Content-Type: application/json' \
  -d '{"names": ["app", "арр", "adмin"]}'
# 422 {"detail":{"error":"batch_validation_failed",
#      "message":"批次中 3/3 个名称存在混脚本或冲突问题，未写入任何记录",
#      "items":[
#        {"index":0,"original_name":"app",...,
#         "batch_conflicts":[{"index":1,"original_name":"арр","reason":"confusable",...}]},
#        {"index":1,"original_name":"арр",...,
#         "batch_conflicts":[{"index":0,"original_name":"app","reason":"confusable",...}]},
#        {"index":2,"original_name":"adмin","mixed_script_violation":true,
#         "restricted_scripts_used":["Cyrillic","Latin"],...}]}}
```

批量注册：并发竞争（另一请求在批次事务提交前抢先写入冲突名称）→ 整批回滚、
重新分析后返回 `409`：

```bash
# 409 {"detail":{"error":"name_conflict",
#      "message":"并发写入使批次判定失效，事务已回滚；以下为重新分析后的冲突明细",
#      "items":[{"index":0,"original_name":"alice",...,
#                "conflicts":[{"original_name":"Alice","reason":"normalized_match",...}]}]}}
```

### 预占后确认注册（reservation / claim）

第一步预占：提交名称与有效期（`ttl_seconds` 取 30–600），复用同一套规范化、
混脚本与同形判定；成功返回不可猜测的 `token` 与 UTC `expires_at`：

```bash
curl -X POST http://localhost:8000/namespaces/chat/reservations \
  -H 'Content-Type: application/json' \
  -d '{"name": "paypal", "ttl_seconds": 120}'
# 201 {"id":1,"namespace":"chat","original_name":"paypal","normalized_name":"paypal",
#      "token":"8xP9...（32 字节随机值的 URL-safe 编码）",
#      "expires_at":"2026-09-19T12:02:00.000Z","created_at":"..."}
```

预占有效期内，该名称与已注册名称完全等价地参与冲突判断——单条注册、批量注册
及新预占都会被拦截，冲突明细以 `source: "reservation"` 标注（已注册名称为
`source: "registered"`，并带 `expires_at`）：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "PAYPAL"}'
# 409 {"detail":{"error":"name_conflict","normalized_name":"paypal",
#      "conflicts":[{"original_name":"paypal","reason":"normalized_match",
#                    "source":"reservation","expires_at":"2026-09-19T12:02:00.000Z",...}]}}
```

第二步确认：在**单个 `BEGIN IMMEDIATE` 事务**中校验命名空间、有效期、消费状态
并复查当前冲突，通过后写入 `display_names` 并标记预占已消费：

```bash
curl -X POST http://localhost:8000/namespaces/chat/reservations/8xP9.../claim
# 201 {"id":2,"namespace":"chat","original_name":"paypal",
#      "normalized_name":"paypal","created_at":"..."}
```

确认的错误语义：

| 场景 | 状态码 | `detail.error` |
| --- | --- | --- |
| token 不存在 / 不属于该命名空间 | `404` | `reservation_not_found` |
| 预占已过期（不写入、不消费） | `410` | `reservation_expired` |
| 同一 token 重复或并发确认（仅一次成功） | `409` | `reservation_consumed` |
| 确认时与已注册名称 / 其他预占冲突（不写入） | `409` | `name_conflict`（带最新冲突明细） |

```bash
curl -X POST http://localhost:8000/namespaces/chat/reservations/8xP9.../claim
# 409 {"detail":{"error":"reservation_consumed",
#      "message":"预占已被确认消费，同一 token 只能成功注册一次"}}
```

过期记录无需定时任务：每次创建预占时在同一事务内惰性删除已过期记录；过期预占
不再参与冲突判断，其名称可被重新注册或预占。

## 判定规则说明

1. **规范化**：`unicodedataplus.normalize("NFKC", name).casefold()`。NFKC 折叠全半角、合字等兼容字符，casefold 折叠大小写（比 lower 更彻底，如 `ß`→`ss`）。
2. **冲突检测**：候选规范名与同命名空间内每条已注册名称、以及每条未消费且未过期的预占比较——完全相等记为 `normalized_match`；否则用 `confusables.is_confusable` 做基于 Unicode `confusables.txt` 的同形判定，命中记为 `confusable`。冲突记录用 `source` 区分来源（`registered` / `reservation`，后者附带 `expires_at`）。命名空间之间相互隔离。
3. **混脚本检测**：逐字符取 `unicodedataplus.script()`，忽略 `Common`（数字、标点、空白等）与 `Inherited`（组合符）后，若出现的脚本中包含 Latin / Cyrillic / Greek 中的两种及以上，判定违规。纯西里尔、纯希腊或与 Han 等其他脚本混用不触发本规则（仍受同形判定约束）。
4. **批量注册**：先把整批与库内已有名称及有效预占联合分析——每项复用上述单条规则，再对批内两两做规范名相等 / 同形判定，命中时按原请求索引互相标注在双方的 `batch_conflicts` 中。任一项存在混脚本或（库内 / 预占 / 批内）冲突即整体返回 `422` 并汇总所有问题项，不写入任何记录；全部通过时在**单个 SQLite 事务**（`BEGIN IMMEDIATE`）中批量落库，事务内会复查冲突以锁定判定，并发竞争导致判定失效时整批回滚、重新分析并返回 `409` 冲突明细。
5. **预占与确认**：预占复用与注册完全相同的规范化、混脚本与冲突判定，在 `BEGIN IMMEDIATE` 事务内惰性清理过期记录、复查冲突后写入 `name_reservations`；`token` 由 `secrets.token_urlsafe(32)` 生成，不可猜测。数据库以部分唯一索引 `UNIQUE (namespace, normalized_name) WHERE consumed = 0` 兜底并发预占。确认（claim）在单个 `BEGIN IMMEDIATE` 事务中按序校验 token 与命名空间、`expires_at`、`consumed` 标记，并复查排除自身后的当前冲突，全部通过才写入 `display_names` 并置 `consumed = 1`，因此同一 token 的并发或重复确认恰有一次成功；过期返回 `410`、冲突或重复消费返回 `409` 且均不写入。

## 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `./names.db`（容器内 `/data/names.db`） | SQLite 数据库文件路径 |

## 项目结构

```
app/
  main.py       # FastAPI 路由与错误响应（201/404/409/410/422）
  service.py    # 注册/分析/预占/确认业务逻辑，逐条冲突判定
  analysis.py   # NFKC+casefold 规范化、脚本与码点分析
  database.py   # SQLite 连接与建表（含预占表与部分唯一索引；python -m app.database 可独立初始化）
  schemas.py    # Pydantic 请求/响应模型
tests/test_api.py
Dockerfile  docker-compose.yml  scripts/setup.sh
requirements.txt  requirements-dev.txt
```
