# coding=utf-8

import sqlite3
import threading
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

from .labeler import Label, LabelRow

Row = Tuple[float, str, str]
"""(timestamp, author, content)"""
LabeledRow = Tuple[float, str, str, Optional[str], Optional[float], Optional[float]]
"""(timestamp, author, content, category, category confidence, mention);
the label fields are ``None`` for a message not labeled yet."""


class Store:
    """Message log of the watched chats, kept in the middleware's own database."""

    def __init__(self, path: Path):
        # Written from channel threads, read from the summary thread.
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        with self.lock, self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "id INTEGER PRIMARY KEY, target TEXT NOT NULL, chat_name TEXT NOT NULL, "
                "msg_uid TEXT, ts REAL NOT NULL, author TEXT NOT NULL, content TEXT NOT NULL)")
            # Identity of the chat in its slave channel, added after the first release.
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
            for column in ("module_id", "chat_uid"):
                if column not in columns:
                    self.db.execute("ALTER TABLE messages ADD COLUMN {} TEXT".format(column))
            self.db.execute("CREATE INDEX IF NOT EXISTS messages_target_ts ON messages (target, ts)")
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            # Labels given by Jev, see labeler.py; a message without one is not labeled yet.
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS labels ("
                "message_id INTEGER PRIMARY KEY, category TEXT NOT NULL, confidence REAL NOT NULL, "
                "mention REAL NOT NULL, model TEXT, labeled_at REAL NOT NULL)")

    def add(self, target: str, chat_name: str, msg_uid: Optional[str], ts: float,
            author: str, content: str, edit: bool = False,
            module_id: Optional[str] = None, chat_uid: Optional[str] = None):
        with self.lock, self.db:
            if edit and msg_uid:
                cur = self.db.execute(
                    "UPDATE messages SET content = ? WHERE target = ? AND msg_uid = ?",
                    (content, target, msg_uid))
                if cur.rowcount:
                    # Label the new content again.
                    self.db.execute(
                        "DELETE FROM labels WHERE message_id IN "
                        "(SELECT id FROM messages WHERE target = ? AND msg_uid = ?)", (target, msg_uid))
                    return
            self.db.execute(
                "INSERT INTO messages (target, chat_name, msg_uid, ts, author, content, module_id, chat_uid) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (target, chat_name, msg_uid, ts, author, content, module_id, chat_uid))

    def fetch(self, target: str, since: Optional[float] = None, limit: Optional[int] = None) -> List[Row]:
        """Messages of a chat in chronological order: those not older than
        ``since``, and of them only the latest ``limit``."""
        with self.lock:
            rows = self.db.execute(
                "SELECT ts, author, content FROM messages WHERE target = ? AND ts >= ? "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (target, since or 0, -1 if limit is None else limit)).fetchall()
        return rows[::-1]

    def fetch_labeled(self, target: str, since: Optional[float] = None,
                      limit: Optional[int] = None) -> List[LabeledRow]:
        """Like :meth:`fetch`, with the label of each message."""
        with self.lock:
            rows = self.db.execute(
                "SELECT m.ts, m.author, m.content, l.category, l.confidence, l.mention "
                "FROM messages m LEFT JOIN labels l ON l.message_id = m.id "
                "WHERE m.target = ? AND m.ts >= ? ORDER BY m.ts DESC, m.id DESC LIMIT ?",
                (target, since or 0, -1 if limit is None else limit)).fetchall()
        return rows[::-1]

    def for_labeling(self, target: str, before: int, limit: int,
                     after: int) -> Optional[Tuple[List[LabelRow], int, Set[int]]]:
        """The oldest messages of a chat not labeled yet: up to ``limit`` of
        them and the messages between them, from the oldest one on, with
        ``before`` messages before and ``after`` messages after them as context.

        Returns:
            The rows, the index of the oldest unlabeled one, and the IDs of the
            rows already labeled; ``None`` if every message is labeled.
        """
        with self.lock:
            first = self.db.execute(
                "SELECT m.ts, m.id FROM messages m LEFT JOIN labels l ON l.message_id = m.id "
                "WHERE m.target = ? AND l.message_id IS NULL ORDER BY m.ts, m.id LIMIT 1",
                (target,)).fetchone()
            if first is None:
                return None
            ts, msg_id = first
            older = self.db.execute(
                "SELECT id, ts, author, content FROM messages "
                "WHERE target = ? AND (ts < ? OR ts = ? AND id < ?) ORDER BY ts DESC, id DESC LIMIT ?",
                (target, ts, ts, msg_id, before)).fetchall()[::-1]
            newer = self.db.execute(
                "SELECT m.id, m.ts, m.author, m.content, l.message_id IS NOT NULL "
                "FROM messages m LEFT JOIN labels l ON l.message_id = m.id "
                "WHERE m.target = ? AND (m.ts > ? OR m.ts = ? AND m.id >= ?) ORDER BY m.ts, m.id LIMIT ?",
                (target, ts, ts, msg_id, limit + after)).fetchall()
        rows = older + [tuple(i[:4]) for i in newer]
        return rows, len(older), {i[0] for i in newer if i[4]}

    def add_labels(self, labels: Iterable[Label], model: str, labeled_at: float):
        with self.lock, self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO labels (message_id, category, confidence, mention, model, labeled_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(i.message_id, i.category, i.category_confidence, i.mention, model, labeled_at) for i in labels])

    def chat_info(self, target: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Name, module ID and chat ID of a chat, as of its latest message."""
        with self.lock:
            row = self.db.execute(
                "SELECT chat_name, module_id, chat_uid FROM messages WHERE target = ? ORDER BY id DESC LIMIT 1",
                (target,)).fetchone()
        return row or (None, None, None)

    def prune(self, target: str, before: Optional[float] = None, keep: Optional[int] = None):
        """Drop messages of a chat older than ``before``, or beyond its latest ``keep``."""
        with self.lock, self.db:
            if before is not None:
                self.db.execute("DELETE FROM messages WHERE target = ? AND ts < ?", (target, before))
            if keep is not None:
                self.db.execute(
                    "DELETE FROM messages WHERE target = ? AND id NOT IN ("
                    "SELECT id FROM messages WHERE target = ? ORDER BY ts DESC, id DESC LIMIT ?)",
                    (target, target, keep))
            self.db.execute("DELETE FROM labels WHERE message_id NOT IN (SELECT id FROM messages)")

    def get_last_run(self, target: str) -> Optional[float]:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", ("last_run:" + target,)).fetchone()
        return float(row[0]) if row else None

    def set_last_run(self, target: str, ts: float):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            ("last_run:" + target, str(ts)))
