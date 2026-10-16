# coding=utf-8
"""Read side of the archive, for the web API."""

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import store

Cursor = Tuple[float, int]
"""(timestamp, row ID) of a message, the order of a chat."""

TYPE_GROUPS: Dict[str, Sequence[str]] = {
    "text": ("Text",),
    "link": ("Link",),
    "image": ("Image",),
    "video": ("Video", "Animation"),
    "file": ("File",),
    "voice": ("Voice",),
    "sticker": ("Sticker",),
    "other": ("Location", "Unsupported", "Status"),
}

MESSAGE_COLUMNS = ("id, master_msg_id, chat_key, sender_key, ts, type, text, link_text, outgoing, "
                   "file_id IS NOT NULL AS has_file, mime, extra")


def parse_cursor(value: Optional[str]) -> Optional[Cursor]:
    if not value:
        return None
    match = re.fullmatch(r"(-?[\d.]+):(\d+)", value)
    if not match:
        raise ValueError("bad cursor")
    return float(match.group(1)), int(match.group(2))


def format_cursor(row: sqlite3.Row) -> str:
    return "{!r}:{}".format(row["ts"], row["id"])


def like_pattern(term: str) -> str:
    return "%{}%".format(term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


def telegram_link(master_msg_id: str) -> Optional[str]:
    """Link to a message in a Telegram supergroup; ``None`` elsewhere,
    e.g. in the private chat with the bot."""
    chat_id, _, message_id = master_msg_id.partition(".")
    if chat_id.startswith("-100") and message_id.isdigit():
        return "https://t.me/c/{}/{}".format(chat_id[4:], message_id)
    return None


class Archive:
    names_seconds = 30

    def __init__(self, path: Path):
        self.path = path
        self.local = threading.local()
        self.names_lock = threading.Lock()
        self.names: Dict[str, Tuple[str, Optional[str], Optional[str]]] = {}
        self.names_at = 0.0

    @property
    def db(self) -> sqlite3.Connection:
        db = getattr(self.local, "db", None)
        if db is None:
            store.connect(self.path).close()  # creates the tables on a fresh install
            db = sqlite3.connect(str(self.path), timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only = 1")
            self.local.db = db
        return db

    # region Names

    def load_names(self):
        with self.names_lock:
            if time.time() - self.names_at < self.names_seconds:
                return
            self.names = {row[0]: (row[1], row[2], row[3]) for row in
                          self.db.execute("SELECT key, name, alias, chat_type FROM names")}
            self.names_at = time.time()

    def chat_name(self, key: str) -> Tuple[str, Optional[str]]:
        """Display name of a chat, and its own name if the display name is an alias."""
        entry = self.names.get(key)
        if entry is None:
            return key.split(" ")[-1], None
        name, alias, _ = entry
        return (alias, name) if alias and alias != name else (name, None)

    def sender_name(self, sender_key: Optional[str], outgoing: bool) -> str:
        if outgoing:
            return "我"
        if not sender_key:
            return "系统"
        parts = sender_key.split(" ")
        if len(parts) > 1 and parts[1] == "__system__":
            return "系统"
        # The WeChat slave channel gives a contact the remark name set for
        # them as alias, and a group member their nickname in the group; a
        # member is the same person (same PUID) as the contact. So: remark
        # name, then nickname in the group, then their own nickname.
        person = self.names.get(" ".join(parts[:2]))
        if len(parts) < 3 or parts[1] == parts[2]:
            # The other side of a private chat: the chat itself is current,
            # its copy as a member is not updated on a rename.
            member = None if person else self.names.get(sender_key)
        else:
            member = self.names.get(sender_key)
        if person and person[1]:
            return person[1]
        if member:
            return member[1] or member[0]
        if person:
            return person[0]
        return parts[1] if len(parts) > 1 else sender_key

    def sender_keys(self, name: str) -> List[str]:
        """Keys of the senders whose name or alias contains ``name``."""
        name = name.lower()
        keys = [k for k, (n, a, _) in self.names.items() if name in n.lower() or (a and name in a.lower())]
        return keys[:500]

    # endregion
    # region Queries

    def stats(self) -> Dict[str, Any]:
        row = self.db.execute("SELECT count(*), sum(count), max(last_ts) FROM chats").fetchone()
        synced = self.db.execute("SELECT value FROM meta WHERE key = 'synced_at'").fetchone()
        return {"chats": row[0], "messages": row[1] or 0, "last_message": row[2],
                "synced_at": float(synced[0]) if synced else None}

    def chats(self) -> List[Dict[str, Any]]:
        self.load_names()
        result = []
        for key, count, first_ts, last_ts, senders in self.db.execute(
                "SELECT key, count, first_ts, last_ts, senders FROM chats ORDER BY last_ts DESC"):
            name, original = self.chat_name(key)
            chat_type = (self.names.get(key) or (None, None, None))[2] or ("Group" if senders > 2 else "Private")
            result.append({"key": key, "name": name, "original": original, "type": chat_type,
                           "count": count, "first_ts": first_ts, "last_ts": last_ts})
        return result

    def messages(self, chat: str, before: Optional[Cursor] = None, after: Optional[Cursor] = None,
                 around: Optional[int] = None, types: Sequence[str] = (), limit: int = 60) -> Dict[str, Any]:
        """A page of a chat in chronological order: the latest messages, or
        those before or after a cursor, or around a message."""
        self.load_names()
        type_sql, type_args = self.type_filter(types)
        if around is not None:
            row = self.db.execute("SELECT ts, id FROM messages WHERE id = ? AND chat_key = ?",
                                  (around, chat)).fetchone()
            if row is None:
                raise LookupError("message not found")
            half = limit // 2
            older = self.page(chat, type_sql, type_args, (row["ts"], row["id"] + 1), None, half)
            newer = self.page(chat, type_sql, type_args, None, (row["ts"], row["id"]), half)
            return {"messages": self.render(older[::-1] + newer),
                    "more_before": len(older) == half, "more_after": len(newer) == half}
        if after is not None:
            rows = self.page(chat, type_sql, type_args, None, after, limit + 1)
            return {"messages": self.render(rows[:limit]), "more_before": True, "more_after": len(rows) > limit}
        rows = self.page(chat, type_sql, type_args, before, None, limit + 1)
        return {"messages": self.render(rows[:limit][::-1]), "more_before": len(rows) > limit,
                "more_after": before is not None}

    def page(self, chat: str, type_sql: str, type_args: list, before: Optional[Cursor],
             after: Optional[Cursor], limit: int) -> List[sqlite3.Row]:
        """Rows of a chat newest first before ``before``, or oldest first
        from ``after`` on (exclusive)."""
        sql = "SELECT {} FROM messages WHERE chat_key = ?{}".format(MESSAGE_COLUMNS, type_sql)
        args: list = [chat] + type_args
        if after is not None:
            sql += " AND (ts > ? OR ts = ? AND id > ?) ORDER BY ts, id LIMIT ?"
            args += [after[0], after[0], after[1], limit]
        else:
            if before is not None:
                sql += " AND (ts < ? OR ts = ? AND id < ?)"
                args += [before[0], before[0], before[1]]
            sql += " ORDER BY ts DESC, id DESC LIMIT ?"
            args.append(limit)
        return self.db.execute(sql, args).fetchall()

    def search(self, query: str, chat: Optional[str] = None, sender: Optional[str] = None,
               types: Sequence[str] = (), before: Optional[Cursor] = None,
               since: Optional[float] = None, until: Optional[float] = None, limit: int = 50) -> Dict[str, Any]:
        """Messages containing every term of ``query``, newest first."""
        self.load_names()
        terms = query.split()[:8]
        long_terms = [i for i in terms if len(i) >= 3]
        short_terms = [i for i in terms if len(i) < 3]
        if long_terms:
            sql = ("SELECT {} FROM messages m JOIN messages_fts f ON f.rowid = m.id "
                   "WHERE messages_fts MATCH ?").format(", ".join("m." + i.strip() for i in MESSAGE_COLUMNS.split(",")))
            # Each term quoted, so it is matched as a substring and nothing in
            # it is taken for FTS syntax.
            args: list = [" AND ".join('"{}"'.format(i.replace('"', '""')) for i in long_terms)]
        else:
            sql = "SELECT {} FROM messages m WHERE 1".format(
                ", ".join("m." + i.strip() for i in MESSAGE_COLUMNS.split(",")))
            args = []
        for term in short_terms:
            sql += " AND (m.text LIKE ? ESCAPE '\\' OR m.link_text LIKE ? ESCAPE '\\')"
            args += [like_pattern(term)] * 2
        if chat:
            sql += " AND m.chat_key = ?"
            args.append(chat)
        if sender:
            condition, condition_args = self.sender_filter(sender)
            if not condition:
                return {"messages": [], "next": None}
            sql += " AND " + condition
            args += condition_args
        type_sql, type_args = self.type_filter(types, "m.")
        sql += type_sql
        args += type_args
        if since is not None:
            sql += " AND m.ts >= ?"
            args.append(since)
        if until is not None:
            sql += " AND m.ts < ?"
            args.append(until)
        if before is not None:
            sql += " AND (m.ts < ? OR m.ts = ? AND m.id < ?)"
            args += [before[0], before[0], before[1]]
        sql += " ORDER BY m.ts DESC, m.id DESC LIMIT ?"
        args.append(limit + 1)
        rows = self.db.execute(sql, args).fetchall()
        return {"messages": self.render(rows[:limit], with_chat=True),
                "next": format_cursor(rows[limit - 1]) if len(rows) > limit else None}

    def sender_filter(self, sender: str) -> Tuple[str, list]:
        """SQL condition for messages from senders named like ``sender``."""
        keys = self.sender_keys(sender)
        members = [k for k in keys if k.count(" ") >= 2]
        # A person named only through their own chat ("module uid") is
        # matched as a member of any group ("module uid group").
        people = [k for k in keys if k.count(" ") == 1][:50]
        conditions, args = [], []
        if members:
            conditions.append("m.sender_key IN ({})".format(", ".join("?" * len(members))))
            args += members
        for key in people:
            conditions.append("m.sender_key = ? OR m.sender_key LIKE ? ESCAPE '\\'")
            args += [key, like_pattern(key + " ")[1:]]
        if sender.strip() in ("我", "me"):
            conditions.append("m.outgoing = 1")
        return ("({})".format(" OR ".join(conditions)) if conditions else ""), args

    @staticmethod
    def type_filter(types: Sequence[str], prefix: str = "") -> Tuple[str, list]:
        names = [t for group in types for t in TYPE_GROUPS.get(group, ())]
        if not names:
            return "", []
        return " AND {}type IN ({})".format(prefix, ", ".join("?" * len(names))), names

    def file_of(self, message_id: int) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT file_id, bot_id, mime, type FROM messages WHERE id = ?",
                               (message_id,)).fetchone()

    # endregion
    # region Rendering

    def render(self, rows: Sequence[sqlite3.Row], with_chat: bool = False) -> List[Dict[str, Any]]:
        extras = [json.loads(row["extra"]) if row["extra"] else {} for row in rows]
        targets = [e["reply_to"] for e in extras if "reply_to" in e]
        replies: Dict[str, sqlite3.Row] = {}
        if targets:
            replies = {r["master_msg_id"]: r for r in self.db.execute(
                "SELECT id, master_msg_id, sender_key, outgoing, type, text, link_text FROM messages "
                "WHERE master_msg_id IN ({})".format(", ".join("?" * len(targets))), targets)}
        result = []
        for row, extra in zip(rows, extras):
            item: Dict[str, Any] = {
                "id": row["id"], "cursor": format_cursor(row), "ts": row["ts"], "type": row["type"],
                "text": row["text"], "outgoing": bool(row["outgoing"]),
                "sender": self.sender_name(row["sender_key"], bool(row["outgoing"])),
                "media": bool(row["has_file"]), "mime": row["mime"],
                "tg": telegram_link(row["master_msg_id"]),
            }
            if extra.get("system"):
                item["system"] = True
            if "title" in extra or "url" in extra:
                item["link"] = {k: extra[k] for k in ("title", "description", "url", "image") if k in extra}
            if "latitude" in extra and "longitude" in extra:
                item["location"] = [extra["latitude"], extra["longitude"]]
            reply = replies.get(extra.get("reply_to", ""))
            if reply is not None:
                item["reply"] = {"id": reply["id"], "sender": self.sender_name(reply["sender_key"],
                                                                               bool(reply["outgoing"])),
                                 "type": reply["type"], "text": (reply["text"] or reply["link_text"] or "")[:200]}
            if with_chat:
                item["chat"] = row["chat_key"]
                item["chat_name"] = self.chat_name(row["chat_key"])[0]
            result.append(item)
        return result

    # endregion
