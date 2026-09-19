"""业务逻辑：名称分析、注册与预占（reservation）。

判定顺序（单条注册时）：
1. 混脚本违规（Latin/Cyrillic/Greek 出现两种及以上）-> 422
2. 与同命名空间已有名称及未过期预占逐条比较：规范名相同或
   confusables 判定为同形 -> 409，并返回全部冲突记录
3. 落库；(namespace, normalized_name) 唯一约束兜底并发写入

批量注册（analyze_batch / insert_batch）：
1. 整批先复用单条分析，再对批内两两做规范名 / 同形判定，
   命中时按原请求索引互相标注在双方 batch_conflicts 中
2. 任一项混脚本或存在（库内 / 预占 / 批内）冲突 -> 422 汇总明细，不落库
3. 全部通过 -> BEGIN IMMEDIATE 单事务落库，事务内复查冲突以锁定
   判定；并发竞争使判定失效 -> 回滚 -> 由调用方重新分析并返回 409

预占（create_reservation / claim_reservation）：
- 预占复用同一套规范化、混脚本与同形判定；未过期且未消费的预占
  与已注册名称完全等价地参与单条注册、批量注册及新预占的冲突判断。
- 预占成功返回不可猜测的 token 与 expires_at。
- 确认（claim）在单个 BEGIN IMMEDIATE 事务中校验命名空间、有效期、
  消费状态并复查当前冲突，随后写入 display_names 并标记已消费；
  同一 token 并发或重复确认只能一次成功。
- 过期记录惰性清理：创建预占时删除已过期记录。
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

# token 为 32 字节随机值的 URL-safe 编码（约 43 字符），不可猜测
TOKEN_BYTES = 32


class NameConflictError(Exception):
    """数据库唯一约束冲突（并发下同规范名重复插入）。

    可通过 normalized_name 属性携带候选规范名，便于调用方重新查询冲突明细。
    """

    def __init__(self, normalized_name: str | None = None):
        super().__init__(normalized_name)
        self.normalized_name = normalized_name


class ReservationNotFoundError(Exception):
    """token 不存在或不属于该命名空间（应返回 404 Not Found）。"""


class ReservationExpiredError(Exception):
    """预占已过期（应返回 410 Gone）。"""


class ReservationConsumedError(Exception):
    """预占已被消费（同一 token 重复或并发确认）。"""


def _now() -> datetime:
    """当前 UTC 时间；测试可 monkeypatch 此函数模拟过期。"""
    return datetime.now(timezone.utc)


def _format_time(moment: datetime) -> str:
    """统一时间格式：UTC、毫秒精度、定长 Z 结尾（可直接按字符串比较）。"""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _utc_time(now: datetime | None = None) -> str:
    moment = now or _now()
    return _format_time(moment.astimezone(timezone.utc))


def _find_registered_conflicts(
    conn: sqlite3.Connection, namespace: str, normalized_name: str
) -> list[dict]:
    """与同命名空间内已注册名称逐条做规范名相等 / 同形字符判定。"""
    rows = conn.execute(
        """
        SELECT id, original_name, normalized_name
        FROM display_names
        WHERE namespace = ?
        ORDER BY id
        """,
        (namespace,),
    ).fetchall()

    conflicts: list[dict] = []
    for row in rows:
        existing_normalized = row["normalized_name"]
        if existing_normalized == normalized_name:
            reason = REASON_NORMALIZED_MATCH
        elif is_confusable(normalized_name, existing_normalized):
            reason = REASON_CONFUSABLE
        else:
            continue
        conflicts.append(
            {
                "id": row["id"],
                "original_name": row["original_name"],
                "normalized_name": existing_normalized,
                "reason": reason,
                "source": SOURCE_REGISTERED,
                "expires_at": None,
            }
        )
    return conflicts


def _find_reservation_conflicts(
    conn: sqlite3.Connection,
    namespace: str,
    normalized_name: str,
    now: str,
    exclude_token: str | None = None,
) -> list[dict]:
    """与同命名空间内未消费、未过期的预占逐条做规范名相等 / 同形判定。

    exclude_token 用于确认（claim）时排除预占自身。
    """
    rows = conn.execute(
        """
        SELECT id, token, original_name, normalized_name, expires_at
        FROM name_reservations
        WHERE namespace = ? AND consumed = 0 AND expires_at > ?
        ORDER BY id
        """,
        (namespace, now),
    ).fetchall()

    conflicts: list[dict] = []
    for row in rows:
        if row["token"] == exclude_token:
            continue
        existing_normalized = row["normalized_name"]
        if existing_normalized == normalized_name:
            reason = REASON_NORMALIZED_MATCH
        elif is_confusable(normalized_name, existing_normalized):
            reason = REASON_CONFUSABLE
        else:
            continue
        conflicts.append(
            {
                "id": row["id"],
                "original_name": row["original_name"],
                "normalized_name": existing_normalized,
                "reason": reason,
                "source": SOURCE_RESERVATION,
                "expires_at": row["expires_at"],
            }
        )
    return conflicts


def find_conflicts(
    conn: sqlite3.Connection,
    namespace: str,
    normalized_name: str,
    *,
    now: datetime | None = None,
    exclude_token: str | None = None,
) -> list[dict]:
    """已注册名称与有效预占的全部冲突（先注册名、后预占，各自按 id 排序）。"""
    current = _utc_time(now)
    return [
        *_find_registered_conflicts(conn, namespace, normalized_name),
        *_find_reservation_conflicts(
            conn, namespace, normalized_name, current, exclude_token=exclude_token
        ),
    ]


def analyze_name(conn: sqlite3.Connection, namespace: str, name: str) -> dict:
    """完整分析：规范名 + 脚本分析 + 冲突检测（只读，不落库）。"""
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


def insert_batch(conn: sqlite3.Connection, namespace: str, names: list[str]) -> list[dict]:
    """单个事务批量落库，返回与请求顺序一致的完整记录。

    BEGIN IMMEDIATE 锁定写入后在事务内复查库内冲突（含有效预占），保证并发下
    此前的分析判定仍然成立；复查不通过、唯一约束冲突或锁竞争时整批回滚，
    抛 NameConflictError 由调用方重新分析。
    """
    normalized_names = [normalize_name(name) for name in names]
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 事务内复查：持锁期间库内状态不变，判定结果可信
        for normalized_name in normalized_names:
            if find_conflicts(conn, namespace, normalized_name):
                raise NameConflictError(normalized_name)
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
        # 唯一约束兜底 / 锁竞争：整批回滚（批量冲突明细由调用方重新分析获得）
        conn.rollback()
        raise NameConflictError(None) from exc

    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM display_names WHERE id IN ({placeholders}) ORDER BY id",
        ids,
    ).fetchall()
    return [dict(row) for row in rows]


def list_names(conn: sqlite3.Connection, namespace: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM display_names WHERE namespace = ? ORDER BY id",
        (namespace,),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------- 预占（reservation） ----------


def _purge_expired_reservations(conn: sqlite3.Connection, now: str) -> None:
    """惰性清理：删除已过期的预占记录（无论是否已消费）。

    必须在写事务（BEGIN IMMEDIATE）中调用，避免清理与新插入之间出现竞争。
    """
    conn.execute("DELETE FROM name_reservations WHERE expires_at <= ?", (now,))


def create_reservation(
    conn: sqlite3.Connection,
    namespace: str,
    name: str,
    normalized_name: str,
    ttl_seconds: int,
) -> dict:
    """创建预占：惰性清理过期记录后写入新预占，返回 token 与 expires_at。

    调用方需先完成混脚本与冲突分析。BEGIN IMMEDIATE 事务保证清理、
    复查与插入的原子性；并发下同规范名预占由部分唯一索引兜底，
    冲突时抛 NameConflictError。
    """
    now = _now()
    now_text = _utc_time(now)
    expires_at_text = _utc_time(now + timedelta(seconds=ttl_seconds))
    token = secrets.token_urlsafe(TOKEN_BYTES)

    try:
        conn.execute("BEGIN IMMEDIATE")
        _purge_expired_reservations(conn, now_text)
        # 事务内复查：持锁期间可能已有注册名或有效预占落库
        if find_conflicts(conn, namespace, normalized_name, now=now):
            raise NameConflictError(normalized_name)
        cursor = conn.execute(
            """
            INSERT INTO name_reservations
                (namespace, token, original_name, normalized_name,
                 expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (namespace, token, name, normalized_name, expires_at_text, now_text),
        )
        conn.commit()
    except NameConflictError:
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # 部分唯一索引兜底（并发预占）/ 锁竞争
        conn.rollback()
        raise NameConflictError(normalized_name) from exc

    row = conn.execute(
        "SELECT * FROM name_reservations WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return dict(row)


def claim_reservation(
    conn: sqlite3.Connection, namespace: str, token: str
) -> dict:
    """确认预占并完成注册，返回写入的 display_names 记录。

    单个 BEGIN IMMEDIATE 事务内依次：
    1. 按 token 定位预占并校验命名空间（不存在/不匹配 -> 404 语义）
    2. 校验有效期（已过期 -> ReservationExpiredError，不写入、不消费）
    3. 校验消费状态（已消费 -> ReservationConsumedError，只能成功一次）
    4. 复查当前冲突（排除预占自身；命中 -> NameConflictError，409 不写入）
    5. 写入 display_names 并把预占标记为已消费
    """
    now = _now()
    now_text = _utc_time(now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        reservation = conn.execute(
            """
            SELECT id, namespace, original_name, normalized_name,
                   expires_at, consumed
            FROM name_reservations
            WHERE token = ?
            """,
            (token,),
        ).fetchone()

        if reservation is None or reservation["namespace"] != namespace:
            raise ReservationNotFoundError(token)
        if reservation["expires_at"] <= now_text:
            raise ReservationExpiredError(token)
        if reservation["consumed"]:
            raise ReservationConsumedError(token)

        name = reservation["original_name"]
        normalized_name = reservation["normalized_name"]

        conflicts = find_conflicts(
            conn,
            namespace,
            normalized_name,
            now=now,
            exclude_token=token,
        )
        if conflicts:
            raise NameConflictError(normalized_name)

        cursor = conn.execute(
            """
            INSERT INTO display_names (namespace, original_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (namespace, name, normalized_name),
        )
        conn.execute(
            "UPDATE name_reservations SET consumed = 1 WHERE id = ?",
            (reservation["id"],),
        )
        conn.commit()
    except (
        ReservationNotFoundError,
        ReservationExpiredError,
        ReservationConsumedError,
        NameConflictError,
    ):
        conn.rollback()
        raise
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
        # display_names 唯一约束兜底 / 锁竞争：不写入、不消费
        conn.rollback()
        raise NameConflictError(normalized_name) from exc

    row = conn.execute(
        "SELECT * FROM display_names WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return dict(row)
