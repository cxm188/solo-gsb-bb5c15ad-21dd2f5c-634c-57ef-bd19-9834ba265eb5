"""业务逻辑：名称分析、注册与“预占后确认注册”。

判定顺序（单条注册 / 预占时）：
1. 混脚本违规（Latin/Cyrillic/Greek 出现两种及以上）-> 422
2. 与同命名空间已有名称及未过期、未消费预占逐条比较：规范名相同或
   confusables 判定为同形 -> 409，并返回全部冲突记录
3. 落库；display_names 的 (namespace, normalized_name) 唯一约束兜底并发写入

预占（create_reservation）：
1. 先做与注册一致的只读分析（混脚本 -> 422，冲突 -> 409）
2. BEGIN IMMEDIATE 取写锁，惰性清理过期预占后复查冲突，生成不可猜测
   token 并写入 name_reservations

确认注册（claim_reservation）：
1. BEGIN IMMEDIATE 单事务内按序校验：token 存在且命名空间匹配
   （不匹配/未知 -> 404）、有效期（过期 -> 410，顺手惰性删除）、
   消费状态（已消费 -> 409）、当前冲突（与已注册名称或其他有效预占
   冲突 -> 409）
2. 通过则在同一事务写入 display_names、回写 consumed_at；唯一约束
   兜底并发，整段失败即回滚，同一 token 并发或重复确认只会成功一次

批量注册（analyze_batch / insert_batch）：
1. 整批先复用单条分析（已含有效预占冲突），再对批内两两做规范名 /
   同形判定，命中时按原请求索引互相标注在双方 batch_conflicts 中
2. 任一项混脚本或存在（库内 / 预占 / 批内）冲突 -> 422 汇总明细，不落库
3. 全部通过 -> BEGIN IMMEDIATE 单事务落库，事务内复查冲突以锁定
   判定；并发竞争使判定失效 -> 回滚 -> 由调用方重新分析并返回 409
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from confusables import is_confusable

from .analysis import analyze_scripts, normalize_name

REASON_NORMALIZED_MATCH = "normalized_match"
REASON_CONFUSABLE = "confusable"

SOURCE_REGISTERED = "registered"
SOURCE_RESERVATION = "reservation"

# token 使用 URL 安全随机字节；仅含 A-Za-z0-9_-，可直接放在路径中
TOKEN_BYTES = 32

# 预占 TTL 边界（与 schemas.ReservationRequest 的校验保持一致）
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 600


class NameConflictError(Exception):
    """数据库唯一约束冲突或事务内复查发现冲突（并发写入）。

    conflicts 可选：事务内复查发现冲突时带上最新冲突明细，供调用方直接
    返回 409，避免回滚后再查一次。
    """

    def __init__(self, value: str | None = None, conflicts: list[dict] | None = None):
        super().__init__(value or "name_conflict")
        self.conflicts = conflicts


class ReservationNotFoundError(Exception):
    """token 不存在或不属于该命名空间。"""


class ReservationExpiredError(Exception):
    """预占已过期（HTTP 410 Gone）。"""


class ReservationConsumedError(Exception):
    """预占已被确认消费：重复或并发确认（HTTP 409 Conflict）。"""


def utc_now() -> datetime:
    """当前 UTC 时刻（naive，按 UTC 解释）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_now_iso() -> str:
    """当前 UTC 时刻的 ISO 8601 字符串（毫秒精度，Z 后缀）。"""
    return utc_now().isoformat(timespec="milliseconds") + "Z"


def parse_expires_at(value: str) -> datetime:
    """解析 expires_at（Z 结尾的 UTC ISO 8601）为 naive datetime。"""
    return datetime.fromisoformat(value.removesuffix("Z")).replace(tzinfo=None)


def _conflict_entry(row, source: str, reason: str) -> dict:
    entry = {
        "id": row["id"],
        "original_name": row["original_name"],
        "normalized_name": row["normalized_name"],
        "reason": reason,
        "source": source,
    }
    if source == SOURCE_RESERVATION:
        entry["expires_at"] = row["expires_at"]
    return entry


def _active_reservation_rows(
    conn: sqlite3.Connection, namespace: str, now: datetime | None = None
) -> list[sqlite3.Row]:
    """读取未消费预占并在 Python 侧剔除已过期记录。

    expires_at 由应用以 UTC ISO 8601 写入，直接按字符串比较即可；放在
    Python 侧过滤可避免 SQLite 内置 strftime 毫秒格式（%f）差异带来的
    边界问题。
    """
    moment = now or utc_now()
    rows = conn.execute(
        """
        SELECT id, original_name, normalized_name, expires_at
        FROM name_reservations
        WHERE namespace = ? AND consumed_at IS NULL
        ORDER BY id
        """,
        (namespace,),
    ).fetchall()
    return [
        row
        for row in rows
        if parse_expires_at(row["expires_at"]) > moment
    ]


def purge_expired_reservations(
    conn: sqlite3.Connection, namespace: str | None = None, now: datetime | None = None
) -> None:
    """惰性清理：删除已过期且尚未消费的预占记录。

    必须在已取得写锁的事务中调用（调用方负责 BEGIN IMMEDIATE）。
    过期判定在 Python 侧完成（见 _active_reservation_rows），按 id 删除。
    namespace 为 None 时清理所有命名空间。
    """
    moment = now or utc_now()
    if namespace is None:
        rows = conn.execute(
            "SELECT id, expires_at FROM name_reservations WHERE consumed_at IS NULL"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, expires_at FROM name_reservations "
            "WHERE namespace = ? AND consumed_at IS NULL",
            (namespace,),
        ).fetchall()
    expired_ids = [
        row["id"]
        for row in rows
        if parse_expires_at(row["expires_at"]) <= moment
    ]
    if expired_ids:
        conn.executemany(
            "DELETE FROM name_reservations WHERE id = ?",
            [(id_,) for id_ in expired_ids],
        )


def find_conflicts(
    conn: sqlite3.Connection,
    namespace: str,
    normalized_name: str,
    exclude_reservation_id: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """与同命名空间内已注册名称及有效预占逐条做规范名相等 / 同形判定。

    已注册名称冲突排在前（按 id），有效预占冲突排在后（按 id）。
    exclude_reservation_id：确认注册时排除正在确认的那条预占自身。
    """
    conflicts: list[dict] = []
    rows = conn.execute(
        """
        SELECT id, original_name, normalized_name
        FROM display_names
        WHERE namespace = ?
        ORDER BY id
        """,
        (namespace,),
    ).fetchall()
    for row in rows:
        existing_normalized = row["normalized_name"]
        if existing_normalized == normalized_name:
            reason = REASON_NORMALIZED_MATCH
        elif is_confusable(normalized_name, existing_normalized):
            reason = REASON_CONFUSABLE
        else:
            continue
        conflicts.append(_conflict_entry(row, SOURCE_REGISTERED, reason))

    for row in _active_reservation_rows(conn, namespace, now):
        if exclude_reservation_id is not None and row["id"] == exclude_reservation_id:
            continue
        existing_normalized = row["normalized_name"]
        if existing_normalized == normalized_name:
            reason = REASON_NORMALIZED_MATCH
        elif is_confusable(normalized_name, existing_normalized):
            reason = REASON_CONFUSABLE
        else:
            continue
        conflicts.append(_conflict_entry(row, SOURCE_RESERVATION, reason))
    return conflicts


def analyze_name(conn: sqlite3.Connection, namespace: str, name: str) -> dict:
    """完整分析：规范名 + 脚本分析 + 冲突检测（只读，不落库）。

    冲突同时覆盖已注册名称与未过期预占；过期预占不参与，也不会在此被删除。
    """
    normalized = normalize_name(name)
    return {
        "namespace": namespace,
        "original_name": name,
        "normalized_name": normalized,
        **analyze_scripts(name),
        "conflicts": find_conflicts(conn, namespace, normalized),
    }


def analyze_batch(conn: sqlite3.Connection, namespace: str, names: list[str]) -> list[dict]:
    """整批联合分析：逐项复用单条分析，再对批内两两做规范名 / 同形判定。

    返回与请求顺序一致的分析结果列表；批内冲突按原请求索引互相标注在
    双方的 batch_conflicts 中（字段与库内冲突记录一致，只是以索引定位）。
    """
    analyses = [
        {"index": index, **analyze_name(conn, namespace, name), "batch_conflicts": []}
        for index, name in enumerate(names)
    ]
    for left in range(len(analyses)):
        for right in range(left + 1, len(analyses)):
            a, b = analyses[left], analyses[right]
            if a["normalized_name"] == b["normalized_name"]:
                reason = REASON_NORMALIZED_MATCH
            elif is_confusable(a["normalized_name"], b["normalized_name"]):
                reason = REASON_CONFUSABLE
            else:
                continue
            a["batch_conflicts"].append(
                {
                    "index": right,
                    "original_name": b["original_name"],
                    "normalized_name": b["normalized_name"],
                    "reason": reason,
                }
            )
            b["batch_conflicts"].append(
                {
                    "index": left,
                    "original_name": a["original_name"],
                    "normalized_name": a["normalized_name"],
                    "reason": reason,
                }
            )
    return analyses


def insert_name(conn: sqlite3.Connection, namespace: str, name: str, normalized_name: str) -> dict:
    """写入新名称，返回完整记录。唯一约束冲突时抛 NameConflictError。"""
    try:
        cursor = conn.execute(
            """
            INSERT INTO display_names (namespace, original_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (namespace, name, normalized_name),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise NameConflictError(normalized_name) from exc

    row = conn.execute(
        "SELECT * FROM display_names WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return dict(row)


def register_name(
    conn: sqlite3.Connection, namespace: str, name: str, normalized_name: str
) -> dict:
    """持写锁注册：BEGIN IMMEDIATE 事务内惰性清理过期预占并复查冲突。

    与预占 / 批量 / 确认共用同一套并发判定：有效预占与已注册名称一样
    阻止注册。复查不通过、唯一约束冲突或锁竞争时回滚并抛 NameConflictError。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        purge_expired_reservations(conn, namespace)
        conflicts = find_conflicts(conn, namespace, normalized_name)
        if conflicts:
            raise NameConflictError(normalized_name, conflicts)
        cursor = conn.execute(
            """
            INSERT INTO display_names (namespace, original_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (namespace, name, normalized_name),
        )
        new_id = cursor.lastrowid
        conn.commit()
    except NameConflictError:
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 唯一约束兜底 / 锁竞争：回滚
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    row = conn.execute(
        "SELECT * FROM display_names WHERE id = ?", (new_id,)
    ).fetchone()
    return dict(row)


def insert_batch(conn: sqlite3.Connection, namespace: str, names: list[str]) -> list[dict]:
    """单个事务批量落库，返回与请求顺序一致的完整记录。

    BEGIN IMMEDIATE 锁定写入后在事务内复查冲突（已注册名称 + 有效预占），
    保证并发下此前的分析判定仍然成立；复查不通过、唯一约束冲突或锁竞争时
    整批回滚，抛 NameConflictError 由调用方重新分析。
    """
    normalized_names = [normalize_name(name) for name in names]
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 持锁期间顺带惰性清理过期预占
        purge_expired_reservations(conn, namespace)
        # 事务内复查：持锁期间状态不变，判定结果可信
        for normalized_name in normalized_names:
            conflicts = find_conflicts(conn, namespace, normalized_name)
            if conflicts:
                raise NameConflictError(normalized_name, conflicts)
        ids = []
        for name, normalized_name in zip(names, normalized_names):
            cursor = conn.execute(
                """
                INSERT INTO display_names (namespace, original_name, normalized_name)
                VALUES (?, ?, ?)
                """,
                (namespace, name, normalized_name),
            )
            ids.append(cursor.lastrowid)
        conn.commit()
    except NameConflictError:
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 唯一约束兜底 / 锁竞争：整批回滚
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM display_names WHERE id IN ({placeholders}) ORDER BY id",
        ids,
    ).fetchall()
    return [dict(row) for row in rows]


def create_reservation(
    conn: sqlite3.Connection, namespace: str, name: str, ttl_seconds: int
) -> dict:
    """预占名称：返回含不可猜测 token 与 expires_at 的预占记录。

    调用方需先完成混脚本 / 冲突的只读分析；本函数在 BEGIN IMMEDIATE
    事务中惰性清理过期预占并复查冲突后写入，复查不通过抛 NameConflictError。
    """
    normalized_name = normalize_name(name)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    created = utc_now()
    expires_at = (created + timedelta(seconds=ttl_seconds)).isoformat(
        timespec="milliseconds"
    ) + "Z"

    try:
        conn.execute("BEGIN IMMEDIATE")
        purge_expired_reservations(conn, namespace)
        conflicts = find_conflicts(conn, namespace, normalized_name, now=created)
        if conflicts:
            raise NameConflictError(normalized_name, conflicts)
        cursor = conn.execute(
            """
            INSERT INTO name_reservations
                (namespace, original_name, normalized_name, token, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (namespace, name, normalized_name, token, expires_at),
        )
        new_id = cursor.lastrowid
        conn.commit()
    except NameConflictError:
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # token 极小概率碰撞（唯一约束）/ 锁竞争：回滚由调用方重试或报冲突
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    row = conn.execute(
        "SELECT * FROM name_reservations WHERE id = ?", (new_id,)
    ).fetchone()
    record = dict(row)
    record["ttl_seconds"] = ttl_seconds
    return record


def claim_reservation(conn: sqlite3.Connection, namespace: str, token: str) -> dict:
    """确认预占：单事务校验并注册，返回 ClaimedNameRecord 形状的记录。

    事务内校验顺序：
    1. token 存在且命名空间匹配，否则 ReservationNotFoundError -> 404
    2. 未过期，否则 ReservationExpiredError -> 410（惰性删除过期记录）
    3. 未消费，否则 ReservationConsumedError -> 409
    4. 与当前已注册名称 / 其他有效预占无冲突，否则 NameConflictError -> 409

    全部通过后在同一事务写入 display_names 并回写 consumed_at；
    唯一约束兜底并发，任何失败都回滚，同一 token 至多成功一次。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM name_reservations WHERE token = ?",
            (token,),
        ).fetchone()

        # 1. token 未知或不属于该命名空间
        if row is None or row["namespace"] != namespace:
            raise ReservationNotFoundError(token)

        reservation_id = row["id"]

        # 2. 过期：惰性删除后提交清理结果，返回 410
        now = utc_now()
        if parse_expires_at(row["expires_at"]) <= now:
            conn.execute("DELETE FROM name_reservations WHERE id = ?", (reservation_id,))
            conn.commit()
            raise ReservationExpiredError(token)

        # 3. 已消费：并发 / 重复确认
        if row["consumed_at"] is not None:
            raise ReservationConsumedError(token)

        # 4. 事务内复查当前冲突（排除自身预占）
        conflicts = find_conflicts(
            conn,
            namespace,
            row["normalized_name"],
            exclude_reservation_id=reservation_id,
            now=now,
        )
        if conflicts:
            raise NameConflictError(row["normalized_name"], conflicts)

        cursor = conn.execute(
            """
            INSERT INTO display_names (namespace, original_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (namespace, row["original_name"], row["normalized_name"]),
        )
        name_id = cursor.lastrowid
        consumed_at = utc_now_iso()
        mark_cursor = conn.execute(
            "UPDATE name_reservations SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (consumed_at, reservation_id),
        )
        # 条件更新兜底：若影响行数为 0，说明并发确认抢先，整体回滚
        if mark_cursor.rowcount == 0:
            raise ReservationConsumedError(token)
        conn.commit()
    except (ReservationNotFoundError, ReservationConsumedError, NameConflictError):
        # 只读校验失败 / 冲突：回滚无副作用
        conn.rollback()
        raise
    except ReservationExpiredError:
        # 过期分支已提交惰性删除，保持提交即可
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 唯一约束兜底（注册名被抢先）/ 锁竞争：回滚，按冲突处理
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

    name_row = conn.execute(
        "SELECT * FROM display_names WHERE id = ?", (name_id,)
    ).fetchone()
    record = dict(name_row)
    record["token"] = token
    record["expires_at"] = row["expires_at"]
    record["consumed_at"] = consumed_at
    return record


def list_names(conn: sqlite3.Connection, namespace: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM display_names WHERE namespace = ? ORDER BY id",
        (namespace,),
    ).fetchall()
    return [dict(row) for row in rows]
