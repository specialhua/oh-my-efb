# coding=utf-8

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from ehforwarderbot import Message, Middleware
from ehforwarderbot.chat import ChatMember, GroupChat, PrivateChat, SelfChatMember, SystemChatMember
from ehforwarderbot.types import InstanceID, ModuleID
from ehforwarderbot.utils import get_data_path

from . import __version__, store


class ChatlogMiddleware(Middleware):
    """
    Note the names of chats and senders of the messages passing through,
    for the chat log web service. The messages themselves are copied by the
    web service from the database of the Telegram master channel.

    Nothing is sent or changed, and no chat list is requested from the
    slave channel.
    """

    middleware_id: ModuleID = ModuleID("specialhua.chatlog")
    middleware_name: str = "Chat Log Middleware"
    __version__: str = __version__

    def __init__(self, instance_id: Optional[InstanceID] = None):
        super().__init__(instance_id)
        self.logger = logging.getLogger(self.middleware_id)
        self.db = store.connect(get_data_path(self.middleware_id) / "archive.db")
        self.lock = threading.Lock()
        self.seen: Dict[str, Tuple[str, Optional[str], Optional[str]]] = {}
        """Names last saved by key, to skip writes when nothing changed."""

    def process_message(self, message: Message) -> Optional[Message]:
        try:
            self.note_names(message)
        except Exception:  # noting names must never stop a message from being delivered
            self.logger.exception("Failed to note names of message %s", message)
        return message

    def note_names(self, message: Message):
        chat = message.chat
        if chat is None:
            return
        chat_type = "Group" if isinstance(chat, GroupChat) else "Private" if isinstance(chat, PrivateChat) \
            else "System"
        rows: List[Tuple[str, str, Optional[str], Optional[str]]] = [
            ("{} {}".format(chat.module_id, chat.uid), chat.name, chat.alias, chat_type)]
        author = message.author
        # Only members of groups: the other side of a private chat is a copy
        # of the chat made when it was created, not renamed with it.
        if isinstance(chat, GroupChat) and isinstance(author, ChatMember) \
                and not isinstance(author, (SelfChatMember, SystemChatMember)):
            # Same format as the sender in tgdata.db.
            rows.append(("{} {} {}".format(author.module_id, author.uid, chat.uid), author.name, author.alias, None))
        now = time.time()
        with self.lock:
            changed = [i for i in rows if i[0] and i[1] and self.seen.get(i[0]) != i[1:]]
            if not changed:
                return
            with self.db:
                store.put_names(self.db, [(key, name, alias or None, kind, store.RANK_LIVE, now)
                                          for key, name, alias, kind in changed])
            for key, *value in changed:
                self.seen[key] = tuple(value)
