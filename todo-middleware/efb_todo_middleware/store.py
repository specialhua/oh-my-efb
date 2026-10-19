# coding=utf-8

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional


class Todo(NamedTuple):
    id: int
    owner_id: int
    """Telegram user to remind, in their private chat with the bot."""
    content: str
    event_at: Optional[float]
    location: Optional[str]
    note: Optional[str]
    source_chat: Optional[str]
    source_author: Optional[str]
    source_text: Optional[str]
    tg_chat_id: Optional[int]
    tg_thread_id: Optional[int]
    tg_msg_id: Optional[int]
    """Where the message the to-do was made from is in Telegram."""
    status: str
    """``active``, ``done`` or ``cancelled``."""
    created_at: float


class Reminder(NamedTuple):
    id: int
    todo_id: int
    remind_at: float
    status: str
    """``pending``, ``sent`` or ``cancelled``."""


TODO_COLUMNS = ", ".join(Todo._fields)
REMINDER_COLUMNS = ", ".join(Reminder._fields)


class Store:
    """To-dos and their reminders; times are UNIX timestamps."""

    def __init__(self, path: Path):
        # Written from the handler threads of the bot, read by the scheduler.
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        with self.lock, self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS todos ("
                "id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, content TEXT NOT NULL, event_at REAL, "
                "location TEXT, note TEXT, source_chat TEXT, source_author TEXT, source_text TEXT, "
                "tg_chat_id INTEGER, tg_thread_id INTEGER, tg_msg_id INTEGER, "
                "status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL)")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS reminders ("
                "id INTEGER PRIMARY KEY, todo_id INTEGER NOT NULL, remind_at REAL NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'pending', sent_at REAL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS reminders_due ON reminders (status, remind_at)")

    def add(self, owner_id: int, content: str, event_at: Optional[float], location: Optional[str],
            note: Optional[str], source_chat: Optional[str], source_author: Optional[str],
            source_text: Optional[str], tg_chat_id: Optional[int], tg_thread_id: Optional[int],
            tg_msg_id: Optional[int], remind_at: Iterable[float]) -> int:
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO todos (owner_id, content, event_at, location, note, source_chat, source_author, "
                "source_text, tg_chat_id, tg_thread_id, tg_msg_id, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (owner_id, content, event_at, location, note, source_chat, source_author, source_text,
                 tg_chat_id, tg_thread_id, tg_msg_id, time.time()))
            todo_id = cur.lastrowid
            self.db.executemany("INSERT INTO reminders (todo_id, remind_at) VALUES (?, ?)",
                                [(todo_id, i) for i in sorted(set(remind_at))])
        return todo_id

    def get(self, todo_id: int) -> Optional[Todo]:
        with self.lock:
            row = self.db.execute("SELECT {} FROM todos WHERE id = ?".format(TODO_COLUMNS), (todo_id,)).fetchone()
        return Todo(*row) if row else None

    def due(self, now: float) -> List[Reminder]:
        """Pending reminders of active to-dos due by ``now``, oldest first."""
        with self.lock:
            rows = self.db.execute(
                "SELECT r.id, r.todo_id, r.remind_at, r.status FROM reminders r "
                "JOIN todos t ON t.id = r.todo_id "
                "WHERE r.status = 'pending' AND r.remind_at <= ? AND t.status = 'active' "
                "ORDER BY r.remind_at", (now,)).fetchall()
        return [Reminder(*i) for i in rows]

    def mark_sent(self, reminder_id: int):
        with self.lock, self.db:
            self.db.execute("UPDATE reminders SET status = 'sent', sent_at = ? WHERE id = ?",
                            (time.time(), reminder_id))

    def snooze(self, todo_id: int, remind_at: float):
        """Remind of an active to-do again at ``remind_at``."""
        with self.lock, self.db:
            self.db.execute("INSERT INTO reminders (todo_id, remind_at) VALUES (?, ?)", (todo_id, remind_at))

    def close(self, todo_id: int, status: str) -> bool:
        """Mark an active to-do ``done`` or ``cancelled``, with the reminders
        it still has; ``False`` if it was not active."""
        with self.lock, self.db:
            cur = self.db.execute("UPDATE todos SET status = ? WHERE id = ? AND status = 'active'",
                                  (status, todo_id))
            self.db.execute("UPDATE reminders SET status = 'cancelled' WHERE todo_id = ? AND status = 'pending'",
                            (todo_id,))
        return bool(cur.rowcount)

    def active(self, owner_id: int) -> List[Todo]:
        """Active to-dos, those with the nearest reminder first, then those
        reminded of already, by when they were made."""
        with self.lock:
            rows = self.db.execute(
                "SELECT {} FROM todos t WHERE owner_id = ? AND status = 'active' ORDER BY "
                "(SELECT MIN(remind_at) FROM reminders r WHERE r.todo_id = t.id AND r.status = 'pending') "
                "IS NULL, "
                "(SELECT MIN(remind_at) FROM reminders r WHERE r.todo_id = t.id AND r.status = 'pending'), "
                "created_at".format(", ".join("t." + i for i in Todo._fields)), (owner_id,)).fetchall()
        return [Todo(*i) for i in rows]

    def pending(self, todo_id: int) -> List[float]:
        """Times of the reminders still to come for a to-do."""
        with self.lock:
            rows = self.db.execute(
                "SELECT remind_at FROM reminders WHERE todo_id = ? AND status = 'pending' ORDER BY remind_at",
                (todo_id,)).fetchall()
        return [i[0] for i in rows]
