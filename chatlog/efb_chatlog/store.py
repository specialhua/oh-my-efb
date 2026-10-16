# coding=utf-8
"""The archive database: every message relayed by EFB, with names of chats
and senders and a full-text index.

Written by the sync of ``tgdata.db`` (messages) in the web service, and by
the middleware inside EFB (names); both processes open it in WAL mode.
"""

import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Tuple

RANK_PUID = 1
"""Name from the PUID map of the WeChat slave channel, for history."""
RANK_CHAT_INFO = 2
"""Name from ``slavechatinfo`` of the Telegram master channel."""
RANK_LIVE = 3
"""Name seen on a message by the middleware, the most current."""

NameRow = Tuple[str, str, Optional[str], Optional[str], int, float]
"""(key, name, alias, chat type, rank, updated at)"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    master_msg_id TEXT NOT NULL UNIQUE,
    chat_key TEXT NOT NULL,
    sender_key TEXT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    text TEXT NOT NULL,
    link_text TEXT,
    outgoing INTEGER NOT NULL,
    file_id TEXT,
    mime TEXT,
    bot_id TEXT,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS messages_chat_ts ON messages (chat_key, ts, id);
CREATE INDEX IF NOT EXISTS messages_ts ON messages (ts, id);

-- Trigrams: any substring of 3 characters or more is found, which suits
-- Chinese without a word segmenter. Shorter terms fall back to LIKE.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, link_text, content='messages', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, text, link_text) VALUES (new.id, new.text, new.link_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text, link_text)
    VALUES ('delete', old.id, old.text, old.link_text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text, link_text ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text, link_text)
    VALUES ('delete', old.id, old.text, old.link_text);
    INSERT INTO messages_fts (rowid, text, link_text) VALUES (new.id, new.text, new.link_text);
END;

CREATE TABLE IF NOT EXISTS chats (
    key TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    first_ts REAL NOT NULL,
    last_ts REAL NOT NULL,
    senders INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS names (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    alias TEXT,
    chat_type TEXT,
    rank INTEGER NOT NULL,
    updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: Path) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < (3, 34):
        raise RuntimeError("SQLite {} is too old: the trigram tokenizer of FTS5 needs 3.34 or later."
                           .format(sqlite3.sqlite_version))
    db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    return db


def put_names(db: sqlite3.Connection, rows: Iterable[NameRow]):
    """Save names, unless one from a better source is already there."""
    db.executemany(
        "INSERT INTO names (key, name, alias, chat_type, rank, updated) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET name = excluded.name, alias = excluded.alias, "
        "chat_type = COALESCE(excluded.chat_type, names.chat_type), "
        "rank = excluded.rank, updated = excluded.updated "
        "WHERE excluded.rank >= names.rank AND (excluded.name IS NOT names.name "
        "OR excluded.alias IS NOT names.alias OR excluded.chat_type IS NOT names.chat_type)",
        list(rows))


def get_meta(db: sqlite3.Connection, key: str) -> Optional[str]:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db: sqlite3.Connection, key: str, value: str):
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
