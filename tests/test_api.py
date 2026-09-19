"""端到端 API 测试：每个用例使用独立的临时 SQLite 数据库。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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


# ---------- 预占 + 确认注册 POST /namespaces/{namespace}/reservations ----------


def reserve(client, name, ttl_seconds=300, namespace="chat"):
    return client.post(
        f"/namespaces/{namespace}/reservations",
        json={"name": name, "ttl_seconds": ttl_seconds},
    )


def claim(client, token, namespace="chat"):
    return client.post(f"/namespaces/{namespace}/reservations/{token}/claim")


@pytest.fixture()
def fake_clock(monkeypatch):
    """可推进的假时钟：替换 service._now，所有预占/确认逻辑共用。"""
    current = {"t": datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)}

    def fake_now():
        return current["t"]

    monkeypatch.setattr(service, "_now", fake_now)

    def advance(seconds):
        current["t"] += timedelta(seconds=seconds)

    current["advance"] = advance
    return current


def test_reservation_returns_201_with_unguessable_token_and_expires_at(client, fake_clock):
    resp = reserve(client, "Alice", ttl_seconds=120)
    assert resp.status_code == 201
    body = resp.json()
    assert body["namespace"] == "chat"
    assert body["original_name"] == "Alice"
    assert body["normalized_name"] == "alice"
    assert body["id"] >= 1
    # secrets.token_urlsafe(32) -> 约 43 字符的高熵随机串，不可猜测
    assert len(body["token"]) >= 40
    assert body["expires_at"] == "2026-01-01T12:02:00.000Z"
    assert body["created_at"] == "2026-01-01T12:00:00.000Z"

    # 预占本身不出现在已注册名称列表中
    assert client.get("/namespaces/chat/names").json() == []


def test_two_reservations_get_distinct_tokens(client):
    first = reserve(client, "Alice").json()["token"]
    second = reserve(client, "Bob").json()["token"]
    assert first != second


def test_reservation_ttl_bounds_accepted(client):
    assert reserve(client, "lower-bound", ttl_seconds=30).status_code == 201
    assert reserve(client, "upper-bound", ttl_seconds=600).status_code == 201


def test_reservation_ttl_out_of_range_or_missing_rejected(client):
    for payload in (
        {"name": "x", "ttl_seconds": 29},
        {"name": "x", "ttl_seconds": 601},
        {"name": "x", "ttl_seconds": 0},
        {"name": "x"},  # ttl_seconds 必填
    ):
        assert client.post(
            "/namespaces/chat/reservations", json=payload
        ).status_code == 422

    # 校验失败不落库任何预占
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM name_reservations").fetchone()["c"] == 0
    finally:
        conn.close()


def test_reservation_mixed_script_returns_422(client):
    resp = reserve(client, MIXED_ADMIN, ttl_seconds=60)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "mixed_script_violation"
    assert detail["restricted_scripts_used"] == ["Cyrillic", "Latin"]

    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM name_reservations").fetchone()["c"] == 0
    finally:
        conn.close()


def test_claim_writes_display_name_and_marks_consumed(client, fake_clock):
    token = reserve(client, "Alice", ttl_seconds=120).json()["token"]

    resp = claim(client, token)
    assert resp.status_code == 201
    body = resp.json()
    assert body["namespace"] == "chat"
    assert body["original_name"] == "Alice"
    assert body["normalized_name"] == "alice"
    assert "token" not in body
    assert body["id"] >= 1

    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["Alice"]

    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT consumed FROM name_reservations WHERE token = ?", (token,)
        ).fetchone()
        assert row["consumed"] == 1
    finally:
        conn.close()


def test_claim_twice_second_returns_409_and_writes_once(client, fake_clock):
    token = reserve(client, "Alice").json()["token"]

    assert claim(client, token).status_code == 201
    resp = claim(client, token)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "reservation_consumed"

    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["Alice"]


def test_claim_unknown_token_returns_404(client):
    resp = claim(client, "no-such-token")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "reservation_not_found"


def test_claim_token_from_other_namespace_returns_404(client):
    token = reserve(client, "Alice", namespace="a").json()["token"]
    resp = claim(client, token, namespace="b")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "reservation_not_found"

    # 命名空间不匹配的 404 不得消费预占：原命名空间仍可确认
    assert claim(client, token, namespace="a").status_code == 201


def test_claim_expired_returns_410_and_does_not_write(client, fake_clock):
    token = reserve(client, "Alice", ttl_seconds=30).json()["token"]
    fake_clock["advance"](31)

    resp = claim(client, token)
    assert resp.status_code == 410
    assert resp.json()["detail"]["error"] == "reservation_expired"

    assert client.get("/namespaces/chat/names").json() == []
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT consumed FROM name_reservations WHERE token = ?", (token,)
        ).fetchone()
        assert row["consumed"] == 0
    finally:
        conn.close()


def test_claim_at_exact_expiry_boundary_is_410(client, fake_clock):
    token = reserve(client, "Alice", ttl_seconds=30).json()["token"]
    fake_clock["advance"](30)  # expires_at <= now 即过期
    assert claim(client, token).status_code == 410


def test_claim_within_ttl_succeeds(client, fake_clock):
    token = reserve(client, "Alice", ttl_seconds=30).json()["token"]
    fake_clock["advance"](29)
    assert claim(client, token).status_code == 201


# 有效预占必须与已注册名称一样参与冲突判断


def test_registered_name_blocks_reservation_normalized_match(client):
    assert register(client, "Alice").status_code == 201
    resp = reserve(client, "ALICE")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "name_conflict"
    conflict = detail["conflicts"][0]
    assert conflict["reason"] == "normalized_match"
    assert conflict["source"] == "registered"
    assert conflict["original_name"] == "Alice"
    assert conflict["expires_at"] is None


def test_registered_name_blocks_reservation_confusable(client):
    assert register(client, "app").status_code == 201
    resp = reserve(client, CYRILLIC_APP)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "confusable"
    assert conflict["source"] == "registered"


def test_active_reservation_blocks_single_register(client):
    assert reserve(client, "Alice").status_code == 201

    resp = register(client, "ALICE")
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "normalized_match"
    assert conflict["source"] == "reservation"
    assert conflict["original_name"] == "Alice"
    assert conflict["expires_at"]

    # 注册被拦截，display_names 仍为空
    assert client.get("/namespaces/chat/names").json() == []


def test_active_reservation_blocks_single_register_confusable(client):
    assert reserve(client, "app").status_code == 201
    resp = register(client, CYRILLIC_APP)
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "confusable"
    assert conflict["source"] == "reservation"


def test_active_reservation_blocks_new_reservation(client):
    assert reserve(client, "Alice").status_code == 201
    resp = reserve(client, "Ａｌｉｃｅ")  # 全角折叠后同规范名
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "normalized_match"
    assert conflict["source"] == "reservation"
    assert conflict["original_name"] == "Alice"
    assert "token" not in conflict  # 不回传他人 token


def test_active_reservation_confusable_blocks_new_reservation(client):
    assert reserve(client, "app").status_code == 201
    assert reserve(client, CYRILLIC_APP).status_code == 409


def test_active_reservation_blocks_batch_register(client):
    assert reserve(client, "app").status_code == 201

    resp = register_batch(client, ["alice", CYRILLIC_APP])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert [item["index"] for item in detail["items"]] == [1]
    conflict = detail["items"][0]["conflicts"][0]
    assert conflict["reason"] == "confusable"
    assert conflict["source"] == "reservation"
    assert client.get("/namespaces/chat/names").json() == []


def test_check_endpoint_reports_reservation_conflicts(client):
    assert reserve(client, "app").status_code == 201
    resp = client.post("/namespaces/chat/check", json={"name": CYRILLIC_APP})
    assert resp.status_code == 200
    conflicts = resp.json()["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["source"] == "reservation"
    assert conflicts[0]["original_name"] == "app"
    assert conflicts[0]["expires_at"]


def test_conflicts_are_namespace_isolated_for_reservations(client):
    assert reserve(client, "root", namespace="a").status_code == 201
    assert register(client, "root", namespace="b").status_code == 201
    assert reserve(client, "root", namespace="b").status_code == 409
    assert reserve(client, "other", namespace="a").status_code == 201


# 过期预占不再参与冲突（惰性清理）


def test_expired_reservation_does_not_block_register_after_lazy_purge(client, fake_clock):
    assert reserve(client, "Alice", ttl_seconds=30).status_code == 201
    fake_clock["advance"](31)

    # 新预占触发惰性清理，过期记录被删除
    assert reserve(client, "Bob").status_code == 201

    assert register(client, "ALICE").status_code == 201
    names = [n["original_name"] for n in client.get("/namespaces/chat/names").json()]
    assert names == ["ALICE"]

    conn = database.get_connection()
    try:
        count = conn.execute("SELECT COUNT(*) AS c FROM name_reservations").fetchone()["c"]
        assert count == 1  # 只剩 Bob 的预占
    finally:
        conn.close()


def test_expired_reservation_token_returns_410_before_purge_and_404_after(client, fake_clock):
    token = reserve(client, "Alice", ttl_seconds=30).json()["token"]
    fake_clock["advance"](31)

    assert claim(client, token).status_code == 410  # 行仍在：过期
    assert reserve(client, "Bob").status_code == 201  # 触发惰性清理
    assert claim(client, token).status_code == 404  # 行已删除


def test_consumed_reservation_releases_the_normalized_name(client, fake_clock):
    token = reserve(client, "Alice").json()["token"]
    assert claim(client, token).status_code == 201

    # 已消费预占不再占位：同规范名的新预占只与已注册的 Alice 冲突
    resp = reserve(client, "ALICE")
    assert resp.status_code == 409
    sources = [c["source"] for c in resp.json()["detail"]["conflicts"]]
    assert sources == ["registered"]


# claim 事务内复查当前冲突


def test_claim_returns_409_and_writes_nothing_when_name_registered_meantime(client, fake_clock):
    token = reserve(client, "Alice").json()["token"]

    # 模拟预占之后、确认之前被并发抢注
    conn = database.get_connection()
    try:
        service.insert_name(conn, "chat", "ALICE", "alice")
    finally:
        conn.close()

    resp = claim(client, token)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "name_conflict"
    assert detail["normalized_name"] == "alice"
    conflict = detail["conflicts"][0]
    assert conflict["source"] == "registered"
    assert conflict["original_name"] == "ALICE"

    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["ALICE"]  # 预占未写入
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT consumed FROM name_reservations WHERE token = ?", (token,)
        ).fetchone()
        assert row["consumed"] == 0  # 未消费，语义上可重试（但冲突仍在）
    finally:
        conn.close()


def test_claim_returns_409_when_other_active_reservation_conflicts_meantime(client, fake_clock):
    token_a = reserve(client, "app").json()["token"]

    # 直接插入另一条同形（confusable）的有效预占，模拟确认期间出现的并发预占
    conn = database.get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO name_reservations
                (namespace, token, original_name, normalized_name, expires_at, consumed)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            ("chat", "other-token", CYRILLIC_APP, CYRILLIC_APP, "2026-01-01T12:30:00.000Z"),
        )
        conn.commit()
    finally:
        conn.close()

    resp = claim(client, token_a)
    assert resp.status_code == 409
    conflicts = resp.json()["detail"]["conflicts"]
    assert any(c["source"] == "reservation" for c in conflicts)
    assert client.get("/namespaces/chat/names").json() == []


def test_concurrent_claims_same_token_single_winner(client, fake_clock):
    token = reserve(client, "Alice").json()["token"]

    def post_claim(_):
        return client.post(f"/namespaces/chat/reservations/{token}/claim").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(post_claim, range(8)))

    assert codes.count(201) == 1
    assert set(codes) <= {201, 409}
    assert not any(code >= 500 for code in codes)
    names = client.get("/namespaces/chat/names").json()
    assert [n["original_name"] for n in names] == ["Alice"]


def test_concurrent_reservations_same_normalized_name_single_winner(client):
    def post(_):
        return reserve(client, "Alice").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(post, range(8)))

    assert codes.count(201) == 1
    assert set(codes) <= {201, 409}
    assert not any(code >= 500 for code in codes)
    assert client.get("/namespaces/chat/names").json() == []
