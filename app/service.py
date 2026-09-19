"""业务逻辑：名称分析与注册。

判定顺序（单条注册时）：
1. 混脚本违规（Latin/Cyrillic/Greek 出现两种及以上）-> 422
2. 与同命名空间已有名称逐条比较：规范名相同或 confusables 判定
   为同形 -> 409，并返回全部冲突记录
3. 落库；(namespace, normalized_name) 唯一约束兜底并发写入

批量注册（analyze_batch / insert_batch）：
1. 整批先复用单条分析，再对批内两两做规范名 / 同形判定，
   命中时按原请求索引互相标注在双方 batch_conflicts 中
2. 任一项混脚本或存在（库内 / 批内）冲突 -> 422 汇总明细，不落库
3. 全部通过 -> BEGIN IMMEDIATE 单事务落库，事务内复查库内冲突以锁定
   判定；并发竞争使判定失效 -> 回滚 -> 由调用方重新分析并返回 409
"""
from __future__ import annotations

import sqlite3

from confusables import is_confusable

from .analysis import analyze_scripts, normalize_name

REASON_NORMALIZED_MATCH = "normalized_match"
REASON_CONFUSABLE = "confusable"


class NameConflictError(Exception):
    """数据库唯一约束冲突（并发下同规范名重复插入）。"""


def find_conflicts(conn: sqlite3.Connection, namespace: str, normalized_name: str) -> list[dict]:
    """与同命名空间内已有名称逐条做规范名相等 / 同形字符判定。"""
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
            }
        )
    return conflicts


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

    BEGIN IMMEDIATE 锁定写入后在事务内复查库内冲突，保证并发下此前的
    分析判定仍然成立；复查不通过、唯一约束冲突或锁竞争时整批回滚，
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
        # 唯一约束兜底 / 锁竞争：整批回滚
        conn.rollback()
        raise NameConflictError(str(exc)) from exc

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
