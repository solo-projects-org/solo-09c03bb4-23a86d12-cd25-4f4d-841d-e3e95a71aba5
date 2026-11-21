"""SQLite 存取层。

设计要点：
- 报价明细（prices）与指数结果（index_months）分表存放。
- 每个报价批次（upload）带自增 id；重报按摊位/品种增量写入，只有新增、
  改价、撤下的行会改动 prices，明细同时记入 price_changes 备查。
- 指数结果表保存了算该结果时所用的报价版本（source_upload_id），因此
  「查指数」只读结果表，不碰报价明细。
- 一次重报的「登记批次 → 增量写报价 → 重算受影响的环比 → 写指数」在单个
  事务内完成。任何一步失败回滚，此前生效的报价、批次和指数都保留。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    month           TEXT    NOT NULL,        -- 'YYYY-MM'
    filename        TEXT    NOT NULL,
    row_count       INTEGER NOT NULL,        -- 本次文件中的有效行数（=重报后的整月行数）
    uploaded_at     TEXT    NOT NULL,        -- UTC ISO8601
    is_base_month   INTEGER NOT NULL DEFAULT 0,
    added_count     INTEGER NOT NULL DEFAULT 0,  -- 相对上一版新增
    changed_count   INTEGER NOT NULL DEFAULT 0,  -- 相对上一版改价
    removed_count   INTEGER NOT NULL DEFAULT 0,  -- 相对上一版撤下
    unchanged_count INTEGER NOT NULL DEFAULT 0   -- 与上一版完全相同
);

CREATE INDEX IF NOT EXISTS idx_uploads_month ON uploads(month, id);

-- 当前生效的报价：同月同摊同品种唯一。
-- upload_id 是最后一次物理写入该价格行的批次；整月报价的生效批次以
-- uploads 中该月最新批次为准（指数 source_upload_id 同步指向它）。
CREATE TABLE IF NOT EXISTS prices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id  INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    month      TEXT    NOT NULL,
    stall      TEXT    NOT NULL,
    item       TEXT    NOT NULL,
    price      REAL    NOT NULL CHECK (price > 0),
    UNIQUE (month, stall, item)
);

CREATE INDEX IF NOT EXISTS idx_prices_month ON prices(month);
CREATE INDEX IF NOT EXISTS idx_prices_key ON prices(month, stall, item);

-- 指数结果缓存。查询指数只读此表。
CREATE TABLE IF NOT EXISTS index_months (
    month              TEXT PRIMARY KEY,
    is_base_month      INTEGER NOT NULL DEFAULT 0,
    has_upload         INTEGER NOT NULL DEFAULT 0,  -- 该月是否有已提交报价
    pair_count         INTEGER,              -- NULL = 断链或无报价
    mom_factor         REAL,                 -- 环比系数
    index_value        REAL,                 -- NULL = 留空
    source_upload_id   INTEGER REFERENCES uploads(id) ON DELETE SET NULL,
    computed_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 每次重报实际改动过的报价行（新增 / 改价 / 撤下）。
-- 内容未变的行不写这里，但仍会随整月归入新批次。
CREATE TABLE IF NOT EXISTS price_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id   INTEGER NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    month       TEXT    NOT NULL,
    stall       TEXT    NOT NULL,
    item        TEXT    NOT NULL,
    change_type TEXT    NOT NULL CHECK (change_type IN ('add', 'change', 'remove')),
    old_price   REAL,
    new_price   REAL
);

CREATE INDEX IF NOT EXISTS idx_price_changes_upload
    ON price_changes(upload_id, id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补上重报统计列（CREATE TABLE IF NOT EXISTS 不会改旧表）。"""
    columns = {
        r["name"] for r in conn.execute("PRAGMA table_info(uploads)").fetchall()
    }
    for name in ("added_count", "changed_count", "removed_count", "unchanged_count"):
        if name not in columns:
            conn.execute(
                f"ALTER TABLE uploads ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
            )


def connect(database: str | Path) -> sqlite3.Connection:
    database = str(database)
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(database: str | Path) -> None:
    conn = connect(database)
    try:
        conn.executescript(SCHEMA)
        with transaction(conn):
            _migrate(conn)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务；异常时回滚，正常时提交。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def all_months(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM index_months ORDER BY month").fetchall()


def max_uploaded_month(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(month) AS m FROM uploads").fetchone()
    return row["m"] if row else None


def previous_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    if m == 1:
        return f"{y - 1:04d}-12"
    return f"{y:04d}-{m - 1:02d}"


def next_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    if m == 12:
        return f"{y + 1:04d}-01"
    return f"{y:04d}-{m + 1:02d}"


def month_range(start: str, end: str) -> list[str]:
    """闭区间 [start, end] 的月份序列。"""
    out = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur = next_month(cur)
    return out


def month_prices(conn: sqlite3.Connection, month: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        "SELECT stall, item, price FROM prices WHERE month = ?", (month,)
    ).fetchall()
    return {(r["stall"], r["item"]): r["price"] for r in rows}


def upsert_index_rows(
    conn: sqlite3.Connection, rows: Sequence[dict]
) -> None:
    conn.executemany(
        """
        INSERT INTO index_months
            (month, is_base_month, has_upload, pair_count, mom_factor, index_value,
             source_upload_id, computed_at)
        VALUES (:month, :is_base_month, :has_upload, :pair_count, :mom_factor,
                :index_value, :source_upload_id, :computed_at)
        ON CONFLICT(month) DO UPDATE SET
            is_base_month    = excluded.is_base_month,
            has_upload       = excluded.has_upload,
            pair_count       = excluded.pair_count,
            mom_factor       = excluded.mom_factor,
            index_value      = excluded.index_value,
            source_upload_id = excluded.source_upload_id,
            computed_at      = excluded.computed_at
        """,
        list(rows),
    )


def recent_uploads(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM uploads ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def month_upload_count(conn: sqlite3.Connection, month: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM uploads WHERE month = ?", (month,)
    ).fetchone()
    return int(row["c"])


def insert_price_changes(
    conn: sqlite3.Connection,
    upload_id: int,
    month: str,
    changes: Sequence[dict],
) -> None:
    conn.executemany(
        """
        INSERT INTO price_changes
            (upload_id, month, stall, item, change_type, old_price, new_price)
        VALUES (:upload_id, :month, :stall, :item, :change_type, :old_price,
                :new_price)
        """,
        [{**c, "upload_id": upload_id, "month": month} for c in changes],
    )
