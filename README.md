# Unicode 显示名防仿冒 API

命名空间（namespace）内的显示名注册服务，基于 **FastAPI + SQLite + confusables + unicodedataplus**，在注册时拦截三类仿冒：

| 判定 | 规则 | 结果 |
| --- | --- | --- |
| 规范名查重 | NFKC + casefold 后与同命名空间已有规范名比较 | 相同 → `409` |
| 同形字符 | 与已有名称**逐条**做 Unicode confusables 判定 | 可混淆 → `409` |
| 混脚本 | 忽略 Common / Inherited 后，同时含 Latin、Cyrillic、Greek 中两种及以上脚本 | `422`，并列出脚本及对应码点 |

原名始终原样落库，规范名（`normalized_name`）一并保存；`(namespace, normalized_name)` 在数据库层有唯一约束兜底并发写入。

此外支持**预占后确认注册**：先 `POST .../reservations` 拿到不可猜测的 token 与过期时间，在 TTL（30–600 秒）内调用 `.../reservations/{token}/claim` 确认。**未过期且未消费的预占与已注册名称完全等价地参与冲突判定**（单条注册、批量注册、新预占、`check` 均会命中），确认成功后才真正写入 `display_names`；过期记录在后续写入时惰性清理。

## 快速开始（Docker，单条命令）

```bash
docker compose up --build
```

该命令完成镜像构建、依赖安装与数据库初始化（应用启动时自动建库建表，**含向预占表 `name_reservations` 的幂等迁移**，升级老版本数据库无需手动执行 SQL；SQLite 文件落在命名卷 `names-data` 中）。

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
| `POST` | `/namespaces/{namespace}/names` | 注册显示名：`201` 成功；`409` 规范名相同或同形冲突（含有效预占）；`422` 混脚本违规 |
| `POST` | `/namespaces/{namespace}/names/batch` | 批量注册（1–50 个）：全部通过才在**单个事务**中落库 → `201`；任一项混脚本或与库内/有效预占/批内名称冲突 → `422` 汇总明细；并发竞争使判定失效 → 回滚重新分析 → `409` |
| `POST` | `/namespaces/{namespace}/reservations` | **预占**名称：`{"name": ..., "ttl_seconds": 30–600}` → `201` 返回不可猜测 `token` 与 `expires_at`；冲突 → `409`；混脚本 / TTL 越界 → `422` |
| `POST` | `/namespaces/{namespace}/reservations/{token}/claim` | **确认预占**：单个 `BEGIN IMMEDIATE` 事务内校验命名空间（`404`）、有效期（过期 `410`）、消费状态（重复/并发确认 `409`）与当前冲突（`409`，不写入），通过后注册并标记已消费 → `201` |
| `POST` | `/namespaces/{namespace}/check` | **只分析不落库**：返回规范名、脚本分析、码点与潜在冲突（含有效预占） |
| `GET` | `/namespaces/{namespace}/names` | 列出命名空间内已注册名称（预占未确认前不出现在此） |
| `GET` | `/health` | 健康检查 |

单条接口请求体为 `{"name": "<显示名>"}`（1–64 字符，非空白）；批量接口为 `{"names": ["<显示名>", ...]}`（1–50 个）；预占接口为 `{"name": "<显示名>", "ttl_seconds": 30–600}`。

冲突明细中的 `source` 字段区分冲突来源：`registered`（已注册名称，默认值，老客户端无需改动）或 `reservation`（有效预占，附带该预占的 `expires_at`）。

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

预占名称：返回不可猜测的 `token`（`secrets.token_urlsafe(32)`，43 字符）与 UTC
`expires_at`；预占期间名称**尚未**出现在已注册列表中，但已开始占位：

```bash
curl -X POST http://localhost:8000/namespaces/chat/reservations \
  -H 'Content-Type: application/json' \
  -d '{"name": "paypal", "ttl_seconds": 120}'
# 201 {"id":1,"namespace":"chat","original_name":"paypal","normalized_name":"paypal",
#      "token":"k8m0...（43 字符随机串）","ttl_seconds":120,
#      "expires_at":"2026-09-19T12:02:00.000Z","created_at":"..."}
```

预占期内，规范名相同或同形的名称在**所有**入口都会冲突（注意 `source` 字段）：

```bash
curl -X POST http://localhost:8000/namespaces/chat/names \
  -H 'Content-Type: application/json' -d '{"name": "PAYPAL"}'
# 409 {"detail":{"error":"name_conflict","normalized_name":"paypal",
#      "conflicts":[{"id":1,"original_name":"paypal","reason":"normalized_match",
#                    "source":"reservation","expires_at":"2026-09-19T12:02:00.000Z"}]}}

curl -X POST http://localhost:8000/namespaces/chat/reservations \
  -H 'Content-Type: application/json' -d '{"name": "раypal", "ttl_seconds": 60}'
# 409 同形判定同样命中（source: "reservation", reason: "confusable"）
```

TTL 越界（<30 或 >600）或混脚本 → `422`：

```bash
curl -X POST http://localhost:8000/namespaces/chat/reservations \
  -H 'Content-Type: application/json' -d '{"name": "paypal", "ttl_seconds": 601}'
# 422 {"detail":[{"type":"less_than_equal",...,"ctx":{"le":600},...}]}
```

确认预占：在单个 `BEGIN IMMEDIATE` 事务中完成全部校验与写入，成功返回已注册
名称记录及消费信息：

```bash
TOKEN='k8m0...'   # 上一步返回的 token
curl -X POST "http://localhost:8000/namespaces/chat/reservations/${TOKEN}/claim"
# 201 {"id":2,"namespace":"chat","original_name":"paypal","normalized_name":"paypal",
#      "created_at":"...","token":"k8m0...","expires_at":"2026-09-19T12:02:00.000Z",
#      "consumed_at":"2026-09-19T12:00:05.123Z"}
```

确认接口的错误语义：

```bash
# token 未知或不属于 URL 中的命名空间 -> 404
# 404 {"detail":{"error":"reservation_not_found","message":"预占不存在，或不属于该命名空间"}}

# 超过 expires_at 才确认 -> 410，过期记录被惰性删除，名称随后可被重新注册/预占
# 410 {"detail":{"error":"reservation_expired","message":"预占已过期且未确认，已失效"}}

# 同一 token 重复或并发确认：只有一次 201，其余 -> 409
# 409 {"detail":{"error":"reservation_already_claimed","message":"该预占已确认注册，不能重复消费"}}

# 确认时名称已与其他已注册名称/有效预占冲突（如预占期内被同规范名抢注）-> 409，不写入
# 409 {"detail":{"error":"name_conflict",
#      "message":"确认时名称与已注册名称或其他有效预占冲突，未写入","conflicts":[...]}}
```

## 判定规则说明

1. **规范化**：`unicodedataplus.normalize("NFKC", name).casefold()`。NFKC 折叠全半角、合字等兼容字符，casefold 折叠大小写（比 lower 更彻底，如 `ß`→`ss`）。
2. **冲突检测**：候选规范名与同命名空间内每条**已注册名称**及**未过期、未消费预占**比较——完全相等记为 `normalized_match`；否则用 `confusables.is_confusable` 做基于 Unicode `confusables.txt` 的同形判定，命中记为 `confusable`。冲突记录以 `source`（`registered` / `reservation`，后者附 `expires_at`）区分来源。命名空间之间相互隔离。
3. **混脚本检测**：逐字符取 `unicodedataplus.script()`，忽略 `Common`（数字、标点、空白等）与 `Inherited`（组合符）后，若出现的脚本中包含 Latin / Cyrillic / Greek 中的两种及以上，判定违规。纯西里尔、纯希腊或与 Han 等其他脚本混用不触发本规则（仍受同形判定约束）。
4. **批量注册**：先把整批与库内已有名称及有效预占联合分析——每项复用上述单条规则，再对批内两两做规范名相等 / 同形判定，命中时按原请求索引互相标注在双方的 `batch_conflicts` 中。任一项存在混脚本或（库内 / 预占 / 批内）冲突即整体返回 `422` 并汇总所有问题项，不写入任何记录；全部通过时在**单个 SQLite 事务**（`BEGIN IMMEDIATE`）中批量落库，事务内会复查冲突以锁定判定，并发竞争导致判定失效时整批回滚、重新分析并返回 `409` 冲突明细。
5. **预占与确认**：预占复用规范化、混脚本与同形判定，先做只读分析，再在 `BEGIN IMMEDIATE` 事务中惰性清理过期预占并复查冲突，通过后写入 `name_reservations`（`token = secrets.token_urlsafe(32)`，不可猜测；`expires_at` 为 UTC ISO 8601）。确认时在**单个 `BEGIN IMMEDIATE` 事务**中按序校验：① token 存在且命名空间匹配（否则 `404`）→ ② `expires_at` 未过（过期则删除该记录并提交，返回 `410`）→ ③ `consumed_at IS NULL`（否则 `409`）→ ④ 复查当前冲突，排除自身预占（否则 `409`，不写入）；全部通过后写入 `display_names` 并以条件 `UPDATE ... WHERE consumed_at IS NULL` 回写消费时间，影响行数为 0 即说明并发确认抢先、整事务回滚。因此同一 token 的并发或重复确认至多一次成功。过期记录不在后台定时清理，而是在后续预占 / 注册 / 批量 / 确认事务中惰性删除。

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
  main.py       # FastAPI 路由与错误响应（201/409/422/410/404）
  service.py    # 注册/批量/预占/确认业务逻辑，逐条冲突判定与事务控制
  analysis.py   # NFKC+casefold 规范化、脚本与码点分析
  database.py   # SQLite 连接与建表（幂等建表即自动迁移；python -m app.database 可独立初始化）
  schemas.py    # Pydantic 请求/响应模型
tests/test_api.py
Dockerfile  docker-compose.yml  scripts/setup.sh
requirements.txt  requirements-dev.txt
```

### 数据表

- `display_names`：已注册名称，`(namespace, normalized_name)` 唯一约束兜底并发。
- `name_reservations`：预占记录（`token` 唯一、`expires_at`、`consumed_at`）；仅未消费且未过期的行参与冲突判定，过期行惰性物理删除。
