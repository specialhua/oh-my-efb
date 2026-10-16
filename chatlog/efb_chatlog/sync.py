# coding=utf-8
"""Copy messages from ``tgdata.db`` of the Telegram master channel into the
archive, and names of chats from where the channels keep them."""

import io
import json
import logging
import pickle
import sqlite3
import threading
from collections import UserDict
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from . import store
from .paths import Paths

logger = logging.getLogger(__name__)

TG_COLUMNS = ("rowid, master_msg_id, text, slave_origin_uid, slave_member_uid, msg_type, "
              "mime, file_id, pickle, time, sender_bot_id")


class _Stub:
    """Stand-in for a class of EFB in a pickle: keeps the attributes only."""

    def __init__(self, *args, **kwargs):
        pass

    def __setstate__(self, state):
        if isinstance(state, tuple) and len(state) == 2:  # (__dict__, __slots__)
            state = dict(state[0] or {}, **(state[1] or {}))
        if isinstance(state, dict):
            self.__dict__.update(state)


class _TwoWayDict(UserDict):
    pass


class _Unpickler(pickle.Unpickler):
    """Load pickles of ``tgdata.db`` and ``wxpy_puid.pkl`` without running
    anything from them."""

    SAFE = {("builtins", "set"): set, ("builtins", "frozenset"): frozenset,
            ("builtins", "object"): object, ("collections", "UserDict"): UserDict}

    def find_class(self, module: str, name: str):
        if (module, name) in self.SAFE:
            return self.SAFE[module, name]
        if (module, name) == ("copyreg", "_reconstructor"):
            return lambda cls, base, state: base.__new__(cls) if base is object else cls()
        if name == "TwoWayDict":
            return _TwoWayDict
        if module.split(".")[0] in ("ehforwarderbot", "efb_telegram_master", "efb_wechat_slave"):
            return type(name, (_Stub,), {})
        raise pickle.UnpicklingError("{}.{} is not allowed".format(module, name))


def load_pickle(data: bytes) -> Any:
    return _Unpickler(io.BytesIO(data)).load()


def message_extra(blob: Optional[bytes]) -> Dict[str, Any]:
    """Reply target, link and location from the misc data of a message."""
    extra: Dict[str, Any] = {}
    if not blob:
        return extra
    try:
        data = load_pickle(bytes(blob))
    except Exception:
        return extra
    if not isinstance(data, dict):
        return extra
    if isinstance(data.get("target"), str):
        extra["reply_to"] = data["target"]
    if data.get("is_system"):
        extra["system"] = True
    attributes = getattr(data.get("attributes"), "__dict__", None) or {}
    for key in ("title", "description", "url", "image"):
        if isinstance(attributes.get(key), str) and attributes[key]:
            extra[key] = attributes[key]
    for key in ("latitude", "longitude"):
        if isinstance(attributes.get(key), (int, float)):
            extra[key] = attributes[key]
    return extra


class Syncer:
    batch = 5000
    tail = 2000
    """Rows at the end of ``tgdata.db`` copied again on each sync, to catch
    edits and media filled in after a message was logged."""

    def __init__(self, paths: Paths, timezone: Optional[str] = None):
        self.paths = paths
        self.tz = None
        if timezone:
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(timezone)
        self.db = store.connect(paths.archive)
        self.lock = threading.Lock()
        self.puid_mtime = 0.0

    def timestamp(self, value: Any) -> float:
        if not value:
            return 0.0
        if isinstance(value, datetime):
            dt = value
        else:
            try:
                dt = datetime.fromisoformat(str(value))
            except ValueError:
                return 0.0
        if self.tz is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.timestamp()

    def convert(self, row: tuple) -> tuple:
        (_, master_msg_id, text, origin, member, msg_type, mime, file_id, blob,
         time, bot_id) = row
        text = text or ""
        extra = message_extra(blob)
        link_text = " ".join(" ".join(i.split()) for i in (
            extra.get("title", ""), extra.get("description", "")) if i) or None
        member_parts = (member or "").split(" ")
        outgoing = len(member_parts) > 1 and member_parts[1] == "__self__"
        return (master_msg_id, origin, member, self.timestamp(time), msg_type, text, link_text,
                int(outgoing), file_id, mime, bot_id, json.dumps(extra, ensure_ascii=False) if extra else None)

    def upsert(self, rows: List[tuple]) -> Set[str]:
        """Save converted rows; returns the chats touched."""
        self.db.executemany(
            "INSERT INTO messages (master_msg_id, chat_key, sender_key, ts, type, text, link_text, outgoing, "
            "file_id, mime, bot_id, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (master_msg_id) DO UPDATE SET chat_key = excluded.chat_key, "
            "sender_key = excluded.sender_key, type = excluded.type, text = excluded.text, "
            "link_text = excluded.link_text, file_id = excluded.file_id, mime = excluded.mime, "
            "bot_id = excluded.bot_id, extra = excluded.extra "
            "WHERE messages.text IS NOT excluded.text OR messages.file_id IS NOT excluded.file_id "
            "OR messages.extra IS NOT excluded.extra OR messages.type IS NOT excluded.type",
            rows)
        return {i[1] for i in rows}

    def update_stats(self, chats: Optional[Iterable[str]] = None):
        if chats is None:
            self.db.execute("DELETE FROM chats")
            self.db.execute("INSERT INTO chats SELECT chat_key, count(*), min(ts), max(ts), "
                            "count(DISTINCT sender_key) FROM messages GROUP BY chat_key")
            return
        for key in chats:
            self.db.execute("INSERT OR REPLACE INTO chats SELECT chat_key, count(*), min(ts), max(ts), "
                            "count(DISTINCT sender_key) FROM messages WHERE chat_key = ? GROUP BY chat_key", (key,))

    def run(self, progress: Optional[Callable[[int, int], None]] = None) -> int:
        """Copy what is new in ``tgdata.db``; returns the number of new rows."""
        with self.lock:
            # Not mode=ro: a read-only connection cannot open a WAL database
            # whose -shm file is gone, as it is while EFB is stopped.
            tg = sqlite3.connect(str(self.paths.tgdata), timeout=30)
            tg.execute("PRAGMA query_only = 1")
            try:
                self.sync_names(tg)
                return self.sync_messages(tg, progress)
            finally:
                tg.close()

    def sync_messages(self, tg: sqlite3.Connection, progress: Optional[Callable[[int, int], None]]) -> int:
        cursor = int(store.get_meta(self.db, "tg_rowid") or 0)
        if cursor:
            # VACUUM renumbers the rows of a table without an integer primary
            # key; if the row at the cursor is not the one seen last time,
            # start over (rows are matched by message ID, so nothing doubles).
            row = tg.execute("SELECT master_msg_id FROM msglog WHERE rowid = ?", (cursor,)).fetchone()
            if row is None or row[0] != store.get_meta(self.db, "tg_check"):
                logger.warning("tgdata.db was rewritten, copying it again from the start")
                cursor = 0
        full = cursor == 0
        total = tg.execute("SELECT max(rowid) FROM msglog").fetchone()[0] or 0
        end = cursor
        cursor = max(0, cursor - self.tail)
        touched: Set[str] = set()
        new = 0
        while True:
            rows = tg.execute("SELECT {} FROM msglog WHERE rowid > ? ORDER BY rowid LIMIT ?".format(TG_COLUMNS),
                              (cursor, self.batch)).fetchall()
            if not rows:
                break
            with self.db:
                touched |= self.upsert([self.convert(i) for i in rows])
                cursor = rows[-1][0]
                store.set_meta(self.db, "tg_rowid", str(cursor))
                store.set_meta(self.db, "tg_check", rows[-1][1])
            new += sum(1 for i in rows if i[0] > end)
            if progress:
                progress(cursor, total)
        with self.db:
            self.update_stats(None if full else touched)
            store.set_meta(self.db, "synced_at", str(datetime.now().timestamp()))
        return new

    def sync_names(self, tg: sqlite3.Connection):
        now = datetime.now().timestamp()
        rows = []
        for channel, uid, group, name, alias, chat_type in tg.execute(
                "SELECT slave_channel_id, slave_chat_uid, slave_chat_group_id, slave_chat_name, "
                "slave_chat_alias, slave_chat_type FROM slavechatinfo"):
            key = " ".join(i for i in (channel, uid, group) if i)
            rows.append((key, name or uid, alias or None, None if group else chat_type, store.RANK_CHAT_INFO, now))
        rows += self.puid_names(now)
        with self.db:
            store.put_names(self.db, rows)

    def puid_names(self, now: float) -> List[store.NameRow]:
        """Names of WeChat contacts and groups as last seen by the slave
        channel, from its PUID map; read again only when it changed."""
        path = self.paths.puid_map
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        if mtime == self.puid_mtime:
            return []
        self.puid_mtime = mtime
        try:
            with path.open("rb") as f:
                _, _, remark_names, captions = _Unpickler(f).load()
        except Exception:
            logger.exception("Failed to read the PUID map %s", path)
            return []
        names: Dict[str, tuple] = {}
        for caption, puid in getattr(captions, "data", {}).items():
            if isinstance(caption, tuple) and caption and caption[0]:
                names[puid] = (caption[0], None)
        for remark, puid in getattr(remark_names, "data", {}).items():
            if remark and puid in names:
                names[puid] = (names[puid][0], remark)
        return [("blueset.wechat " + puid, name, alias, None, store.RANK_PUID, now)
                for puid, (name, alias) in names.items()]
