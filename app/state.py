"""Тонкий слой работы с состоянием бота в SQLite.

В отличие от первой версии (где каналы жили в config.yaml), теперь источник
правды — БД. config.yaml используется только для первичного засева.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
-- Каналы-источники, которые мы парсим
CREATE TABLE IF NOT EXISTS channels (
    username    TEXT    PRIMARY KEY,        -- @username без @, в нижнем регистре
    category    TEXT    NOT NULL,           -- mp_news | marketing
    mode        TEXT    NOT NULL DEFAULT 'live',  -- live | digest
    paused      INTEGER NOT NULL DEFAULT 0,
    added_by    INTEGER,                    -- user_id админа, кто добавил
    added_at    TEXT    NOT NULL,
    extra       TEXT                        -- JSON: schedule_hours, tz, header — для digest
);

-- Уже обработанные сообщения (дедуп)
CREATE TABLE IF NOT EXISTS seen (
    chat_id   INTEGER NOT NULL,
    msg_id    INTEGER NOT NULL,
    seen_at   TEXT    NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);

-- Очередь дайджест-постов (для ozon_novosti и для уикэндов)
CREATE TABLE IF NOT EXISTS digest_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket     TEXT    NOT NULL,            -- 'ozon_novosti' | 'weekend:mp_news' | 'weekend:marketing'
    source     TEXT    NOT NULL,            -- @username канала-источника
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    queued_at  TEXT    NOT NULL,
    UNIQUE (bucket, chat_id, msg_id)
);

-- Куда слать live-новости. Один таргет на категорию.
-- chat_id < 0 для каналов/групп; thread_id заполняется если это супергруппа с темами.
CREATE TABLE IF NOT EXISTS targets (
    category   TEXT    PRIMARY KEY,         -- mp_news | marketing
    chat_id    INTEGER NOT NULL,
    thread_id  INTEGER,                     -- для супергрупп с темами (форумов)
    set_by     INTEGER,
    set_at     TEXT    NOT NULL
);

-- Админы (любой кто написал /admin)
CREATE TABLE IF NOT EXISTS admins (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    granted_at TEXT    NOT NULL
);

-- Счётчик статистики по дням
CREATE TABLE IF NOT EXISTS stats (
    day        TEXT    NOT NULL,            -- YYYY-MM-DD UTC
    category   TEXT    NOT NULL,
    metric     TEXT    NOT NULL,            -- sent | blocked_ad | queued_weekend
    cnt        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, category, metric)
);

-- Статистика по каналам-источникам для рейтинга качества и авто-паузы.
CREATE TABLE IF NOT EXISTS source_stats (
    day        TEXT    NOT NULL,            -- YYYY-MM-DD
    source     TEXT    NOT NULL,            -- username без @
    category   TEXT    NOT NULL,
    metric     TEXT    NOT NULL,            -- queued_daily | blocked_ad | blocked_ai_ad | blocked_noise
    cnt        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source, category, metric)
);

-- Глобальные настройки key-value (например last_weekend_digest_at)
CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


@dataclass
class Channel:
    username: str
    category: str
    mode: str
    paused: bool
    extra: dict[str, Any]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# ---------- channels ----------------------------------------------------------

def list_channels(conn: sqlite3.Connection, category: str | None = None) -> list[Channel]:
    q = "SELECT username, category, mode, paused, extra FROM channels"
    args: tuple = ()
    if category:
        q += " WHERE category = ?"
        args = (category,)
    q += " ORDER BY category, username"
    return [
        Channel(
            username=r["username"],
            category=r["category"],
            mode=r["mode"],
            paused=bool(r["paused"]),
            extra=json.loads(r["extra"]) if r["extra"] else {},
        )
        for r in conn.execute(q, args)
    ]


def get_channel(conn: sqlite3.Connection, username: str) -> Channel | None:
    r = conn.execute(
        "SELECT username, category, mode, paused, extra FROM channels WHERE username = ?",
        (username.lower(),),
    ).fetchone()
    if not r:
        return None
    return Channel(
        username=r["username"],
        category=r["category"],
        mode=r["mode"],
        paused=bool(r["paused"]),
        extra=json.loads(r["extra"]) if r["extra"] else {},
    )


def add_channel(
    conn: sqlite3.Connection, username: str, category: str, *,
    mode: str = "live", added_by: int | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """True если добавлен, False если уже был."""
    try:
        conn.execute(
            "INSERT INTO channels (username, category, mode, paused, added_by, added_at, extra) "
            "VALUES (?, ?, ?, 0, ?, ?, ?)",
            (
                username.lower(), category, mode, added_by,
                datetime.utcnow().isoformat(),
                json.dumps(extra) if extra else None,
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def remove_channel(conn: sqlite3.Connection, username: str) -> bool:
    cur = conn.execute("DELETE FROM channels WHERE username = ?", (username.lower(),))
    return cur.rowcount > 0


def set_channel_category(conn: sqlite3.Connection, username: str, category: str) -> bool:
    cur = conn.execute(
        "UPDATE channels SET category = ? WHERE username = ?",
        (category, username.lower()),
    )
    return cur.rowcount > 0


def set_channel_paused(conn: sqlite3.Connection, username: str, paused: bool) -> bool:
    cur = conn.execute(
        "UPDATE channels SET paused = ? WHERE username = ?",
        (1 if paused else 0, username.lower()),
    )
    return cur.rowcount > 0


# ---------- seen / dedup ------------------------------------------------------

def is_seen(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> bool:
    r = conn.execute(
        "SELECT 1 FROM seen WHERE chat_id = ? AND msg_id = ?",
        (chat_id, msg_id),
    ).fetchone()
    return r is not None


def mark_seen(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> None:
    with suppress(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO seen (chat_id, msg_id, seen_at) VALUES (?, ?, ?)",
            (chat_id, msg_id, datetime.utcnow().isoformat()),
        )


# ---------- targets -----------------------------------------------------------

def set_target(
    conn: sqlite3.Connection, category: str, chat_id: int,
    thread_id: int | None, set_by: int,
) -> None:
    conn.execute(
        "INSERT INTO targets (category, chat_id, thread_id, set_by, set_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(category) DO UPDATE SET "
        "chat_id = excluded.chat_id, thread_id = excluded.thread_id, "
        "set_by = excluded.set_by, set_at = excluded.set_at",
        (category, chat_id, thread_id, set_by, datetime.utcnow().isoformat()),
    )


def get_target(conn: sqlite3.Connection, category: str) -> tuple[int, int | None] | None:
    r = conn.execute(
        "SELECT chat_id, thread_id FROM targets WHERE category = ?", (category,)
    ).fetchone()
    if not r:
        return None
    return r["chat_id"], r["thread_id"]


def remove_target(conn: sqlite3.Connection, category: str) -> bool:
    cur = conn.execute("DELETE FROM targets WHERE category = ?", (category,))
    return cur.rowcount > 0


# ---------- admins ------------------------------------------------------------

def add_admin(conn: sqlite3.Connection, user_id: int, username: str | None, first_name: str | None) -> bool:
    try:
        conn.execute(
            "INSERT INTO admins (user_id, username, first_name, granted_at) VALUES (?, ?, ?, ?)",
            (user_id, username, first_name, datetime.utcnow().isoformat()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def is_admin(conn: sqlite3.Connection, user_id: int) -> bool:
    r = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
    return r is not None


def list_admins(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT user_id, username, first_name FROM admins ORDER BY granted_at"))


# ---------- digest queue ------------------------------------------------------

def enqueue_digest(
    conn: sqlite3.Connection, bucket: str, source: str,
    chat_id: int, msg_id: int, text: str,
) -> None:
    with suppress(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO digest_queue (bucket, source, chat_id, msg_id, text, queued_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bucket, source, chat_id, msg_id, text, datetime.utcnow().isoformat()),
        )


def pop_bucket(conn: sqlite3.Connection, bucket: str) -> list[sqlite3.Row]:
    rows = list(conn.execute(
        "SELECT source, msg_id, text FROM digest_queue WHERE bucket = ? ORDER BY queued_at",
        (bucket,),
    ))
    if rows:
        conn.execute("DELETE FROM digest_queue WHERE bucket = ?", (bucket,))
    return rows


def list_bucket(conn: sqlite3.Connection, bucket: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT source, msg_id, text FROM digest_queue WHERE bucket = ? ORDER BY queued_at",
        (bucket,),
    ))


def delete_bucket(conn: sqlite3.Connection, bucket: str) -> None:
    conn.execute("DELETE FROM digest_queue WHERE bucket = ?", (bucket,))


def queue_size(conn: sqlite3.Connection, bucket: str) -> int:
    r = conn.execute(
        "SELECT COUNT(*) AS n FROM digest_queue WHERE bucket = ?", (bucket,)
    ).fetchone()
    return int(r["n"])


# ---------- stats -------------------------------------------------------------

def bump_stat(conn: sqlite3.Connection, day: str, category: str, metric: str, n: int = 1) -> None:
    conn.execute(
        "INSERT INTO stats (day, category, metric, cnt) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(day, category, metric) DO UPDATE SET cnt = cnt + excluded.cnt",
        (day, category, metric, n),
    )


def stats_for_day(conn: sqlite3.Connection, day: str) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for r in conn.execute(
        "SELECT category, metric, cnt FROM stats WHERE day = ?", (day,)
    ):
        out[(r["category"], r["metric"])] = int(r["cnt"])
    return out


def bump_source_stat(
    conn: sqlite3.Connection,
    day: str,
    source: str,
    category: str,
    metric: str,
    n: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO source_stats (day, source, category, metric, cnt) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(day, source, category, metric) DO UPDATE SET cnt = cnt + excluded.cnt",
        (day, source.lower().lstrip("@"), category, metric, n),
    )


def source_stats_since(conn: sqlite3.Connection, since_day: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT source, category, metric, SUM(cnt) AS cnt "
        "FROM source_stats WHERE day >= ? "
        "GROUP BY source, category, metric "
        "ORDER BY source, category, metric",
        (since_day,),
    ))


# ---------- settings ----------------------------------------------------------

def setting_get(conn: sqlite3.Connection, k: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT v FROM settings WHERE k = ?", (k,)).fetchone()
    return r["v"] if r else default


def setting_set(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute(
        "INSERT INTO settings (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (k, v),
    )


# ---------- seeding -----------------------------------------------------------

def seed_from_yaml(conn: sqlite3.Connection, cfg: dict[str, Any]) -> int:
    """Засевает БД списком каналов из config.yaml при первом запуске. Идемпотентно."""
    added = 0
    for cat in ("mp_news", "marketing"):
        for ch in cfg.get("channels", {}).get(cat, []):
            if add_channel(conn, ch, cat):
                added += 1
    for ch, rules in cfg.get("special", {}).items():
        if add_channel(
            conn, ch, rules["category"], mode=rules.get("mode", "live"),
            extra={k: v for k, v in rules.items() if k not in ("category", "mode")},
        ):
            added += 1
    return added
