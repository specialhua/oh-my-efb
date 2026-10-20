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
"""(key, name, alias, chat type, rank, updated at)

Chat types: ``Group``, ``Private``, ``MP`` (an official account, which the
WeChat slave channel makes a private chat) and ``System``.
"""

STATS_VERSION = "2"
"""Version of what the ``chats`` table holds; on a change, the next sync
counts it again from the messages."""

HINT_PRIVATE = 1
HINT_GROUP = 2

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
    senders INTEGER NOT NULL,
    hint INTEGER,
    last_tg TEXT
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
    columns = {row[1] for row in db.execute("PRAGMA table_info(chats)")}
    for column in ("hint INTEGER", "last_tg TEXT"):
        if column.split()[0] not in columns:
            try:
                db.execute("ALTER TABLE chats ADD COLUMN " + column)
            except sqlite3.OperationalError:  # added by the other process meanwhile
                pass
    db.create_function("sender_hint", 3, sender_hint, deterministic=True)
    return db


def sender_hint(chat_key: str, sender_key: Optional[str], outgoing: int) -> int:
    """What a message tells of its chat by the form of its sender:
    ``module member group`` with a member other than the chat is someone in
    a group; ``module uid uid`` (or ``module uid``) with the uid of the chat
    is the other side of a private chat."""
    if outgoing or not sender_key:
        return 0
    parts = sender_key.split(" ")
    if len(parts) < 2 or parts[1].startswith("__"):  # __self__, __system__
        return 0
    chat_uid = chat_key.split(" ")[-1]
    if len(parts) > 2 and parts[2] == chat_uid and parts[1] != chat_uid:
        return HINT_GROUP
    if parts[1] == chat_uid and (len(parts) < 3 or parts[2] == chat_uid):
        return HINT_PRIVATE
    return 0


# An official account is a private chat to EFB and to ``slavechatinfo``;
# whichever source knows it is one wins over those that say private.
_CHAT_TYPE = (
    "CASE WHEN 'MP' IN (names.chat_type, excluded.chat_type) "
    "AND IFNULL(names.chat_type, 'Private') IN ('Private', 'MP') "
    "AND IFNULL(excluded.chat_type, 'Private') IN ('Private', 'MP') THEN 'MP' "
    "WHEN excluded.rank >= names.rank THEN COALESCE(excluded.chat_type, names.chat_type) "
    "ELSE names.chat_type END")


def put_names(db: sqlite3.Connection, rows: Iterable[NameRow]):
    """Save names, unless one from a better source is already there."""
    better = "excluded.rank >= names.rank"
    db.executemany(
        "INSERT INTO names (key, name, alias, chat_type, rank, updated) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET "
        "name = CASE WHEN {0} THEN excluded.name ELSE names.name END, "
        "alias = CASE WHEN {0} THEN excluded.alias ELSE names.alias END, "
        "chat_type = {1}, "
        "rank = CASE WHEN {0} THEN excluded.rank ELSE names.rank END, "
        "updated = CASE WHEN {0} THEN excluded.updated ELSE names.updated END "
        "WHERE {0} AND (excluded.name IS NOT names.name OR excluded.alias IS NOT names.alias "
        "OR excluded.chat_type IS NOT names.chat_type) OR ({1}) IS NOT names.chat_type".format(better, _CHAT_TYPE),
        list(rows))


def get_meta(db: sqlite3.Connection, key: str) -> Optional[str]:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db: sqlite3.Connection, key: str, value: str):
    """Save a value; an unchanged one is not written again."""
    db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value WHERE value IS NOT excluded.value",
               (key, value))
