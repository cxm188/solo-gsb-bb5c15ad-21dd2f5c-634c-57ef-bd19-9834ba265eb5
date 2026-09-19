"""SQLite 存储层：连接管理与建表。

数据库路径通过环境变量 DATABASE_PATH 配置，默认 ./names.db。
表结构对 display_names (namespace, normalized_name) 做唯一约束，在数据库
层面兜底规范名重复；同形字符（confusable）判定在应用层完成。

name_reservations 保存“预占后确认注册”的预占记录：token 不可猜测且唯一，
expires_at 为 UTC 过期时刻，consumed_at 在确认注册成功后回写（NULL 表示
尚未消费）。未过期、未消费的预占与 display_names 一样参与冲突判定。

建表语句全部幂等（IF NOT EXISTS），应用启动时执行即自动完成数据库迁移，
既有数据库无需额外迁移步骤。
"""
from __future__ import annotations

import os
import sqlite3

DATABASE_PATH = os.environ.get("DATABASE_PATH", "names.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS display_names (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace       TEXT NOT NULL,
    original_name   TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (namespace, normalized_name)
);

CREATE INDEX IF NOT EXISTS idx_display_names_namespace
    ON display_names (namespace);

CREATE TABLE IF NOT EXISTS name_reservations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace       TEXT NOT NULL,
    original_name   TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    token           TEXT NOT NULL UNIQUE,
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 冲突检测的热路径：按命名空间取未消费预占，再按 expires_at 过滤
CREATE INDEX IF NOT EXISTS idx_name_reservations_namespace_active
    ON name_reservations (namespace, consumed_at, expires_at);
"""


def init_db(path: str | None = None) -> str:
    """建库建表（幂等），返回实际使用的数据库路径。"""
    target = path or DATABASE_PATH
    if target != ":memory:":
        directory = os.path.dirname(os.path.abspath(target))
        os.makedirs(directory, exist_ok=True)
    with sqlite3.connect(target) as conn:
        conn.executescript(SCHEMA)
    return target


def get_connection() -> sqlite3.Connection:
    """新建一个连接（按行名字典化返回），由调用方负责关闭。

    check_same_thread=False：FastAPI 依赖与端点可能运行在不同线程池
    线程中；每个连接仍只属一个请求、串行使用，跨线程访问是安全的。
    """
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    # 写事务（BEGIN IMMEDIATE）竞争时等待对端提交，而不是立刻 SQLITE_BUSY
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


if __name__ == "__main__":
    print(f"database initialized: {init_db()}")
