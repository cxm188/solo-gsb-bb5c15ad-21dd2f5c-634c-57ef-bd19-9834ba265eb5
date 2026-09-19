"""端到端 API 测试：每个用例使用独立的临时 SQLite 数据库。"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import database, service
from app.main import app

# 纯西里尔字母 "арр"（а U+0430, р U+0440），与拉丁 "app" 同形
CYRILLIC_APP = "арр"
# 拉丁 "ad" + 西里尔 "м"（U+043C）+ 拉丁 "in" —— 混脚本
MIXED_ADMIN = "adмin"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DATABASE_PATH", str(db_path))
    database.init_db(str(db_path))
    with TestClient(app) as test_client:
        yield test_client


def register(client, name, namespace="chat"):
    return client.post(f"/namespaces/{namespace}/names", json={"name": name})


def test_register_returns_201_and_stores_original_and_normalized(client):
    resp = register(client, "Ａｌｉｃｅ")  # 全角字母
    assert resp.status_code == 201
    body = resp.json()
    assert body["original_name"] == "Ａｌｉｃｅ"  # 原名落库
    assert body["normalized_name"] == "alice"  # NFKC + casefold
    assert body["namespace"] == "chat"
    assert body["id"] >= 1


def test_normalized_duplicate_returns_409_with_conflict_record(client):
    assert register(client, "Alice").status_code == 201

    resp = register(client, "ALICE")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "name_conflict"
    assert detail["normalized_name"] == "alice"
    assert detail["conflicts"][0]["original_name"] == "Alice"
    assert detail["conflicts"][0]["reason"] == "normalized_match"

    # NFKC 兼容字符（全角）折叠后同样冲突
    resp = register(client, "Ａｌｉｃｅ")
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicts"][0]["reason"] == "normalized_match"


def test_confusable_name_returns_409(client):
    assert register(client, "app").status_code == 201

    resp = register(client, CYRILLIC_APP)  # 纯西里尔，单脚本不触发 422
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["conflicts"][0]["reason"] == "confusable"
    assert detail["conflicts"][0]["original_name"] == "app"

    # 反向同样成立：先注册纯西里尔，再注册拉丁
    assert register(client, CYRILLIC_APP, namespace="other").status_code == 201
    resp = register(client, "app", namespace="other")
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicts"][0]["reason"] == "confusable"


def test_mixed_latin_cyrillic_returns_422_with_code_points(client):
    resp = register(client, MIXED_ADMIN)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "mixed_script_violation"
    assert detail["restricted_scripts_used"] == ["Cyrillic", "Latin"]

    by_script = {d["script"]: d for d in detail["script_details"]}
    assert by_script["Cyrillic"]["characters"] == ["м"]
    assert by_script["Cyrillic"]["code_points"] == ["U+043C"]
    assert set(by_script["Latin"]["characters"]) == {"a", "d", "i", "n"}
    assert "U+0061" in by_script["Latin"]["code_points"]

    # 422 不落库
    assert client.get("/namespaces/chat/names").json() == []


def test_mixed_latin_greek_returns_422(client):
    resp = register(client, "Αlpha")  # 希腊大写 Alpha + 拉丁
    assert resp.status_code == 422
    assert resp.json()["detail"]["restricted_scripts_used"] == ["Greek", "Latin"]


def test_single_restricted_script_is_allowed(client):
    assert register(client, "αρρ").status_code == 201  # 纯西里尔
    assert register(client, "ΑΒΓ").status_code == 201  # 纯希腊
    assert register(client, "hello-123_世界").status_code == 201  # Latin+Han 不拦截


def test_check_endpoint_analyzes_without_persisting(client):
    assert register(client, "app").status_code == 201

    resp = client.post("/namespaces/chat/check", json={"name": CYRILLIC_APP})
    assert resp.status_code == 200
    body = resp.json()
    assert body["normalized_name"] == CYRILLIC_APP
    assert body["mixed_script_violation"] is False
    assert body["scripts"] == ["Cyrillic"]
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["reason"] == "confusable"

    # 检查接口不落库：命名空间内仍只有 1 条记录
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["app"]


def test_check_endpoint_reports_mixed_scripts_without_persisting(client):
    resp = client.post("/namespaces/chat/check", json={"name": MIXED_ADMIN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mixed_script_violation"] is True
    assert body["restricted_scripts_used"] == ["Cyrillic", "Latin"]
    assert client.get("/namespaces/chat/names").json() == []


def test_namespaces_are_isolated(client):
    assert register(client, "root", namespace="a").status_code == 201
    assert register(client, "root", namespace="b").status_code == 201
    assert len(client.get("/namespaces/a/names").json()) == 1
    assert len(client.get("/namespaces/b/names").json()) == 1


def test_blank_name_rejected(client):
    assert register(client, "   ").status_code == 422
    assert register(client, "").status_code == 422


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# ---------- 批量注册 POST /namespaces/{namespace}/names/batch ----------


def register_batch(client, names, namespace="chat"):
    return client.post(f"/namespaces/{namespace}/names/batch", json={"names": names})


def test_batch_register_returns_201_and_persists_all_in_order(client):
    resp = register_batch(client, ["Alice", "Ｂｏｂ", "世界-123"])
    assert resp.status_code == 201
    body = resp.json()
    assert body["namespace"] == "chat"
    assert [item["index"] for item in body["items"]] == [0, 1, 2]
    assert [item["original_name"] for item in body["items"]] == ["Alice", "Ｂｏｂ", "世界-123"]
    assert [item["normalized_name"] for item in body["items"]] == ["alice", "bob", "世界-123"]
    assert all(item["id"] >= 1 and item["created_at"] for item in body["items"])

    # 全部落库，且可通过列表接口查到
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["Alice", "Ｂｏｂ", "世界-123"]


def test_batch_max_size_50_accepted(client):
    resp = register_batch(client, [f"user{i}" for i in range(50)])
    assert resp.status_code == 201
    assert len(resp.json()["items"]) == 50


def test_batch_size_limits_rejected(client):
    assert register_batch(client, []).status_code == 422  # 至少 1 个
    assert register_batch(client, [f"user{i}" for i in range(51)]).status_code == 422  # 至多 50 个
    assert register_batch(client, ["ok", "   "]).status_code == 422  # 空白名称
    assert client.get("/namespaces/chat/names").json() == []


def test_batch_in_batch_normalized_duplicate_returns_422_and_marks_both(client):
    resp = register_batch(client, ["Alice", "bob", "ALICE"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "batch_validation_failed"

    items = {item["index"]: item for item in detail["items"]}
    assert set(items) == {0, 2}  # bob 无问题，不出现在明细中
    assert items[0]["batch_conflicts"][0]["index"] == 2
    assert items[0]["batch_conflicts"][0]["original_name"] == "ALICE"
    assert items[0]["batch_conflicts"][0]["reason"] == "normalized_match"
    assert items[2]["batch_conflicts"][0]["index"] == 0
    assert items[2]["batch_conflicts"][0]["original_name"] == "Alice"

    # 422 整批不落库
    assert client.get("/namespaces/chat/names").json() == []


def test_batch_in_batch_confusable_returns_422_and_marks_both(client):
    resp = register_batch(client, ["app", CYRILLIC_APP, "ok-name"])
    assert resp.status_code == 422
    items = {item["index"]: item for item in resp.json()["detail"]["items"]}
    assert set(items) == {0, 1}
    assert items[0]["batch_conflicts"][0]["index"] == 1
    assert items[0]["batch_conflicts"][0]["reason"] == "confusable"
    assert items[1]["batch_conflicts"][0]["index"] == 0
    assert client.get("/namespaces/chat/names").json() == []


def test_batch_db_conflict_returns_422_with_conflict_records(client):
    assert register(client, "app").status_code == 201

    resp = register_batch(client, ["hello", CYRILLIC_APP])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert [item["index"] for item in detail["items"]] == [1]
    conflict = detail["items"][0]["conflicts"][0]
    assert conflict["original_name"] == "app"
    assert conflict["reason"] == "confusable"

    # 只有预先注册的 app，批次未写入
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["app"]


def test_batch_mixed_script_returns_422_with_script_details(client):
    resp = register_batch(client, ["clean", MIXED_ADMIN])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert [item["index"] for item in detail["items"]] == [1]
    item = detail["items"][0]
    assert item["mixed_script_violation"] is True
    assert item["restricted_scripts_used"] == ["Cyrillic", "Latin"]
    by_script = {d["script"]: d for d in item["script_details"]}
    assert by_script["Cyrillic"]["code_points"] == ["U+043C"]
    assert client.get("/namespaces/chat/names").json() == []


def test_batch_aggregates_script_issues_and_all_conflicts(client):
    assert register(client, "Alice").status_code == 201

    # 一批同时覆盖：库内冲突、批内冲突、混脚本
    resp = register_batch(client, ["ALICE", "app", CYRILLIC_APP, MIXED_ADMIN])
    assert resp.status_code == 422
    items = {item["index"]: item for item in resp.json()["detail"]["items"]}
    assert set(items) == {0, 1, 2, 3}
    assert items[0]["conflicts"][0]["reason"] == "normalized_match"  # 与库内 Alice
    assert items[1]["batch_conflicts"][0]["index"] == 2  # 批内同形
    assert items[2]["batch_conflicts"][0]["index"] == 1
    assert items[3]["mixed_script_violation"] is True

    # 一条都不落库
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["Alice"]


def test_batch_concurrent_race_returns_409_with_refreshed_conflicts(client, monkeypatch):
    """并发竞争：批次事务开始时另一连接已抢先写入冲突名称。

    insert_batch 事务内复查发现判定失效 -> 回滚 -> 重新分析 -> 409。
    """
    real_insert_batch = service.insert_batch

    def racing_insert_batch(conn, namespace, names):
        # 模拟并发写入：另一个连接在批次事务前抢注 "Alice"
        other = database.get_connection()
        try:
            service.insert_name(other, namespace, "Alice", "alice")
        finally:
            other.close()
        return real_insert_batch(conn, namespace, names)

    monkeypatch.setattr(service, "insert_batch", racing_insert_batch)

    resp = register_batch(client, ["alice", "bob"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "name_conflict"
    assert [item["index"] for item in detail["items"]] == [0]
    assert detail["items"][0]["conflicts"][0]["original_name"] == "Alice"
    assert detail["items"][0]["conflicts"][0]["reason"] == "normalized_match"

    # 批次整体回滚：库内只有并发写入的 Alice
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["Alice"]


def test_insert_batch_rolls_back_when_conflict_appears_in_transaction(client):
    """服务层兜底：事务内复查发现库内冲突时整批回滚。"""
    conn = database.get_connection()
    try:
        service.insert_name(conn, "chat", "app", "app")
        with pytest.raises(service.NameConflictError):
            service.insert_batch(conn, "chat", ["alice", CYRILLIC_APP])
        # alice 未写入，整批回滚
        assert [r["original_name"] for r in service.list_names(conn, "chat")] == ["app"]
    finally:
        conn.close()


def test_insert_batch_unique_constraint_rolls_back_entire_batch(client):
    """服务层兜底：批内规范名重复触发唯一约束时整批回滚。"""
    conn = database.get_connection()
    try:
        with pytest.raises(service.NameConflictError):
            service.insert_batch(conn, "chat", ["dup", "DUP"])
        assert service.list_names(conn, "chat") == []
    finally:
        conn.close()


def test_batch_concurrent_requests_single_winner_no_5xx(client):
    """并发回归：同一批次并发提交，恰好一个 201，其余 409/422，无 5xx。

    同时覆盖连接跨线程使用（check_same_thread=False）的回归。
    """

    def post(_):
        return client.post(
            "/namespaces/chat/names/batch", json={"names": ["n1", "n2"]}
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(post, range(8)))

    assert not any(code >= 500 for code in codes)
    assert codes.count(201) == 1
    assert set(codes) <= {201, 409, 422}
    # 只有一个批次落库（all-or-nothing）
    assert len(client.get("/namespaces/chat/names").json()) == 2
