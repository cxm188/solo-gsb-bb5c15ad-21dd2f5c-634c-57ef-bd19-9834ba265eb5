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


# ---------- 预占 POST /namespaces/{namespace}/reservations ----------


def reserve(client, name, ttl_seconds=120, namespace="chat"):
    return client.post(
        f"/namespaces/{namespace}/reservations",
        json={"name": name, "ttl_seconds": ttl_seconds},
    )


def claim(client, token, namespace="chat"):
    return client.post(
        f"/namespaces/{namespace}/reservations/{token}/claim"
    )


def test_reserve_returns_201_with_unguessable_token_and_expires_at(client):
    resp = reserve(client, "Alice", ttl_seconds=60)
    assert resp.status_code == 201
    body = resp.json()
    assert body["namespace"] == "chat"
    assert body["original_name"] == "Alice"
    assert body["normalized_name"] == "alice"
    assert body["ttl_seconds"] == 60
    # token 不可猜测：32 字节随机 -> urlsafe base64 至少 40 字符
    assert len(body["token"]) >= 40
    assert body["expires_at"].endswith("Z")
    assert body["id"] >= 1

    # 两个独立预占的 token 必须不同
    other = reserve(client, "Bob")
    assert other.status_code == 201
    assert other.json()["token"] != body["token"]

    # 预占期间名称尚未注册
    assert client.get("/namespaces/chat/names").json() == []


def test_reserve_ttl_bounds_validated(client):
    assert reserve(client, "a", ttl_seconds=30).status_code == 201
    assert reserve(client, "b", ttl_seconds=600).status_code == 201
    assert reserve(client, "c", ttl_seconds=29).status_code == 422
    assert reserve(client, "d", ttl_seconds=601).status_code == 422
    assert reserve(client, "e", ttl_seconds=0).status_code == 422


def test_reserve_mixed_script_returns_422_and_does_not_persist(client):
    resp = reserve(client, MIXED_ADMIN)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "mixed_script_violation"
    assert detail["restricted_scripts_used"] == ["Cyrillic", "Latin"]

    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_reservations").fetchone()[0] == 0
    finally:
        conn.close()


def test_active_reservation_blocks_single_register_normalized(client):
    r = reserve(client, "Alice").json()

    # 大小写折叠后相同 -> 409，冲突来源标记为 reservation
    resp = register(client, "ALICE")
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "normalized_match"
    assert conflict["source"] == "reservation"
    assert conflict["original_name"] == "Alice"
    assert conflict["expires_at"] == r["expires_at"]

    # 注册没有落库
    assert client.get("/namespaces/chat/names").json() == []


def test_active_reservation_blocks_single_register_confusable(client):
    reserve(client, "app")

    resp = register(client, CYRILLIC_APP)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "confusable"
    assert conflict["source"] == "reservation"


def test_active_reservation_blocks_new_reservation(client):
    reserve(client, "app")

    resp = reserve(client, CYRILLIC_APP)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["source"] == "reservation"
    assert conflict["reason"] == "confusable"


def test_active_reservation_blocks_batch_register(client):
    reserve(client, "app")

    resp = register_batch(client, ["hello", CYRILLIC_APP])
    assert resp.status_code == 422
    items = resp.json()["detail"]["items"]
    assert items[0]["index"] == 1
    assert items[0]["conflicts"][0]["source"] == "reservation"
    assert items[0]["conflicts"][0]["reason"] == "confusable"
    # 整批不落库，预占也未被消费
    assert client.get("/namespaces/chat/names").json() == []


def test_registered_name_blocks_reservation_with_registered_source(client):
    assert register(client, "app").status_code == 201

    resp = reserve(client, CYRILLIC_APP)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["source"] == "registered"
    assert conflict["reason"] == "confusable"
    assert "expires_at" not in conflict


def test_check_reports_active_reservation_conflict(client):
    reserve(client, "app")

    resp = client.post("/namespaces/chat/check", json={"name": CYRILLIC_APP})
    assert resp.status_code == 200
    conflicts = resp.json()["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["source"] == "reservation"
    assert conflicts[0]["reason"] == "confusable"


def test_reservations_are_namespace_isolated(client):
    reserve(client, "app", namespace="a")
    assert register(client, "app", namespace="b").status_code == 201
    assert reserve(client, CYRILLIC_APP, namespace="b").status_code == 409
    # 命名空间 a 中预占仍然有效
    assert register(client, "app", namespace="a").status_code == 409


# ---------- 确认注册 POST .../reservations/{token}/claim ----------


def test_claim_success_writes_display_name_and_marks_consumed(client):
    r = reserve(client, "Alice", ttl_seconds=60).json()

    resp = claim(client, r["token"])
    assert resp.status_code == 201
    body = resp.json()
    assert body["original_name"] == "Alice"
    assert body["normalized_name"] == "alice"
    assert body["namespace"] == "chat"
    assert body["token"] == r["token"]
    assert body["expires_at"] == r["expires_at"]
    assert body["consumed_at"].endswith("Z")
    assert body["id"] >= 1

    # 名称已真正注册
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["Alice"]


def test_claim_unknown_token_returns_404(client):
    resp = claim(client, "does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "reservation_not_found"


def test_claim_token_from_other_namespace_returns_404(client):
    r = reserve(client, "app", namespace="a").json()
    resp = claim(client, r["token"], namespace="b")
    assert resp.status_code == 404
    # 预占仍可在原命名空间确认
    assert claim(client, r["token"], namespace="a").status_code == 201


def test_claim_twice_only_first_succeeds(client):
    r = reserve(client, "Alice").json()

    assert claim(client, r["token"]).status_code == 201
    second = claim(client, r["token"])
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "reservation_already_claimed"

    # 只注册了一次
    assert len(client.get("/namespaces/chat/names").json()) == 1


def test_claim_concurrent_requests_exactly_one_wins(client):
    r = reserve(client, "Alice").json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: claim(client, r["token"]), range(8)))

    codes = [resp.status_code for resp in responses]
    assert codes.count(201) == 1
    assert set(codes) <= {201, 409}
    assert not any(code >= 500 for code in codes)
    assert len(client.get("/namespaces/chat/names").json()) == 1


def test_claim_expired_returns_410_and_lazily_deletes(client):
    r = reserve(client, "Alice", ttl_seconds=30).json()

    # 直接把 expires_at 改成过去时刻，模拟 TTL 到期
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_reservations SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    resp = claim(client, r["token"])
    assert resp.status_code == 410
    assert resp.json()["detail"]["error"] == "reservation_expired"

    # 惰性清理：过期记录已删除，名称可被重新注册
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_reservations").fetchone()[0] == 0
    finally:
        conn.close()
    assert register(client, "Alice").status_code == 201


def test_expired_reservation_does_not_block_and_is_purged_on_next_write(client):
    reserve(client, "app", ttl_seconds=30)
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE name_reservations SET expires_at = '2000-01-01T00:00:00.000Z'"
        )
        conn.commit()
    finally:
        conn.close()

    # 过期预占不阻止新预占；创建时惰性清理旧记录
    resp = reserve(client, "app")
    assert resp.status_code == 201
    conn = database.get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM name_reservations WHERE original_name = 'app'"
        ).fetchone()[0]
        assert count == 1  # 旧的已删除，只剩新的
    finally:
        conn.close()


def test_claim_conflict_with_registered_name_returns_409_and_writes_nothing(client):
    r = reserve(client, "app").json()
    # 预占期间另一个名字（不与预占冲突的）被注册后，再注册与预占同规范名的名字
    # —— 单条注册接口会被预占挡住，因此这里直接走服务层制造并发抢注
    conn = database.get_connection()
    try:
        service.insert_name(conn, "chat", "APP", "app")
    finally:
        conn.close()

    resp = claim(client, r["token"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "name_conflict"
    assert detail["conflicts"][0]["original_name"] == "APP"
    assert detail["conflicts"][0]["source"] == "registered"

    # 冲突不写入，预占未被消费（但名称仍被注册方占着，后续确认依旧失败）
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["APP"]
    assert claim(client, r["token"]).status_code == 409


def test_claim_conflict_with_other_reservation_returns_409(client):
    # 两个互相同形的预占不能同时存在：先预占 app，再用同形名会被 409。
    # 因此冲突场景用“注册名 vs 预占”之外的手段：同规范名预占经直接插入制造，
    # 模拟并发下两笔预占都通过了各自的事务前分析。
    first = reserve(client, "app").json()
    conn = database.get_connection()
    try:
        conn.execute(
            """
            INSERT INTO name_reservations
                (namespace, original_name, normalized_name, token, expires_at)
            VALUES ('chat', 'APP', 'app', ?, '2999-01-01T00:00:00.000Z')
            """,
            ("second-conflicting-token",),
        )
        conn.commit()
    finally:
        conn.close()

    resp = claim(client, first["token"])
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["source"] == "reservation"
    assert conflict["original_name"] == "APP"
    assert client.get("/namespaces/chat/names").json() == []


def test_claim_after_success_name_blocks_others(client):
    r = reserve(client, "app").json()
    assert claim(client, r["token"]).status_code == 201

    # 确认后与普通注册完全等价：同形名 409
    assert register(client, CYRILLIC_APP).status_code == 409
    assert reserve(client, CYRILLIC_APP).status_code == 409
    # 已消费预占不再出现在冲突明细中
    resp = client.post("/namespaces/chat/check", json={"name": CYRILLIC_APP})
    assert resp.json()["conflicts"][0]["source"] == "registered"


def test_service_claim_rolls_back_when_conflict_detected_in_transaction(client):
    """服务层：claim 事务内复查发现冲突时抛 NameConflictError 且不写入。"""
    r = reserve(client, "app").json()

    # 直接制造与预占同规范名的已注册记录（绕过 API 的预占拦截）
    conn = database.get_connection()
    try:
        service.insert_name(conn, "chat", "APP", "app")
    finally:
        conn.close()

    claim_conn = database.get_connection()
    try:
        with pytest.raises(service.NameConflictError) as exc_info:
            service.claim_reservation(claim_conn, "chat", r["token"])
        conflicts = exc_info.value.conflicts
        assert conflicts and conflicts[0]["source"] == "registered"

        # 事务已回滚：预占仍是未消费状态，可重复进入确认（依旧冲突）
        row = claim_conn.execute(
            "SELECT consumed_at FROM name_reservations WHERE token = ?", (r["token"],)
        ).fetchone()
        assert row["consumed_at"] is None
    finally:
        claim_conn.close()


def test_reserve_concurrent_race_returns_409_with_refreshed_conflicts(client, monkeypatch):
    """并发竞争：只读分析通过后、写事务复查前另一连接抢注同名 -> 409，不落预占。"""
    real_create_reservation = service.create_reservation

    def racing_create_reservation(conn, namespace, name, ttl_seconds):
        # 模拟并发写入：另一个连接在预占写事务前抢注 "Alice"
        other = database.get_connection()
        try:
            service.insert_name(other, namespace, "Alice", "alice")
        finally:
            other.close()
        return real_create_reservation(conn, namespace, name, ttl_seconds)

    monkeypatch.setattr(service, "create_reservation", racing_create_reservation)

    resp = reserve(client, "Alice")
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["original_name"] == "Alice"
    assert conflict["source"] == "registered"

    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM name_reservations").fetchone()[0] == 0
    finally:
        conn.close()
