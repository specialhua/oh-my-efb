# coding=utf-8

"""``/todo``: make a to-do from a message, in the private chat of the user
with the bot, and remind of it there when its time comes.

Everything here runs on the bot of the Telegram master channel, ahead of
the handlers of ETM, so neither the commands nor what the user types while
filling in a to-do reach a slave chat.
"""

import html
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

from ehforwarderbot import coordinator
from ehforwarderbot.chat import SelfChatMember

from . import extract
from .store import Store, Todo
from .timeutil import (INPUT_FORMAT, OFFSET_BY_KEY, OFFSETS, ago, at_default_time, fmt, fmt_short,
                       future_offsets, offset_time, parse_input)

CALLBACK = "todo:"
HANDLER_GROUP = -2
"""Ahead of ETM's handlers in group 0 and of ``update_locale`` in group -1;
the same group as ``/ask`` of the summary middleware, whose handler only
takes its own buttons."""

LOCK_SECONDS = 600
"""While the bot waits for the user to type something, their private chat
is held back from ETM; after this long without a word it is let go."""
DRAFT_SECONDS = 86400
"""A to-do not saved by then is forgotten."""
RETRY_SECONDS = 300
"""Wait before sending a reminder again after it failed."""
LATE_SECONDS = 120
"""A reminder sent later than this says how late it is."""
LIST_LIMIT = 20
SOURCE_PREVIEW = 300
REMINDER_PREVIEW = 800

USAGE = ("用法：回复一条消息发 <code>/todo</code>，把它设为待办；"
         "也可以直接写 <code>/todo 明天下午三点开会</code>。\n"
         "<code>/todos</code> 列出未完成的待办。")

Rows = List[List[Tuple[str, str]]]
"""Rows of buttons: label and callback data after :data:`CALLBACK`, or
label and ``url:`` plus a link."""

FIELDS = ("content", "event", "location", "note")
"""What the user is asked in turn when filling in a to-do by hand."""
QUESTIONS = {
    "content": "✏️ 提醒内容是什么？（必填）",
    "event": "🕐 事项开始时间是什么？（选填）\n格式：{}，例如 <code>{}</code>",
    "location": "📍 事项地点在哪里？（选填）",
    "note": "📝 备注？（选填）",
    "custom": "⏰ 自定义提醒时间是什么？\n格式：{}，例如 <code>{}</code>",
}


class Source(NamedTuple):
    """The message a to-do is made from."""
    chat: Optional[str]
    """Name of the WeChat group or contact, or of the Telegram chat."""
    author: Optional[str]
    text: str
    sent_at: Optional[datetime]
    tg_chat_id: Optional[int]
    tg_thread_id: Optional[int]
    tg_msg_id: Optional[int]

    def label(self) -> str:
        return " · ".join(i for i in (self.chat, self.author) if i)


class Draft:
    """A to-do being made."""

    def __init__(self, owner_id: int, source: Optional[Source], hint: str):
        self.key = secrets.token_hex(4)
        self.owner_id = owner_id
        self.source = source
        self.hint = hint
        """What the user wrote after ``/todo``."""
        self.mode = ""
        """``ai`` or ``manual`` once chosen."""
        self.content: Optional[str] = None
        self.event_at: Optional[datetime] = None
        self.guessed_time = False
        self.location: Optional[str] = None
        self.note: Optional[str] = None
        self.reason: Optional[str] = None
        self.offsets: Set[str] = set()
        self.custom: List[datetime] = []
        self.asked: Set[str] = set()
        """Fields of :data:`FIELDS` the user answered or skipped."""
        self.awaiting: Optional[str] = None
        """What the bot asked the user to type: a field, or ``custom``."""
        self.editing = False
        """The question came from a button of the card, which is shown
        again once it is answered."""
        self.card_id: Optional[int] = None
        self.prompt_id: Optional[int] = None
        self.touched = time.time()
        self.created = self.touched
        self.busy = False
        """While the model is working, the buttons do nothing."""

    def reminders(self, now: datetime) -> List[datetime]:
        """The chosen reminder times still ahead of ``now``, in order."""
        times = [offset_time(self.event_at, k) for k in self.offsets] if self.event_at else []
        return sorted({i for i in times + self.custom if i > now})


class Todoer:
    """The ``/todo`` and ``/todos`` commands, their buttons, and the reminders."""

    poll_seconds: float = 20

    def __init__(self, middleware: Any, store: Store):
        self.middleware = middleware
        self.store = store
        self.logger = logging.getLogger(middleware.middleware_id + ".flow")
        self.drafts: Dict[int, Draft] = {}
        """The draft of each user; a new ``/todo`` replaces it."""
        self.lock = threading.Lock()
        self.failed: Dict[int, float] = {}
        """Reminders whose sending failed, and when to try again."""
        self.stop_event = threading.Event()

    @property
    def tz(self) -> tzinfo:
        return self.middleware.tz

    def now(self) -> datetime:
        return datetime.now(self.tz)

    @staticmethod
    def bot() -> Any:
        return coordinator.master.bot_manager  # type: ignore[attr-defined]

    # region Setup

    def register(self):
        from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, MessageHandler, filters

        bot = self.bot()
        todoer = self

        class Locked(filters.MessageFilter):
            """Messages in the private chat of a user the bot waits on."""

            def filter(self, message) -> bool:
                user = message.from_user
                return message.chat.type == "private" and user is not None and todoer.locked(user.id)

        def wrap(fn):
            def handle(update, context):
                try:
                    fn(update)
                except Exception:
                    self.logger.exception("Failed to handle %s", update)
                # Not for ETM: neither a slave chat nor its "session expired".
                raise ApplicationHandlerStop()
            return bot.as_async_callback(handle)

        admins = filters.User(user_id=bot.admins)
        new = filters.UpdateType.MESSAGE
        dispatcher = bot.dispatcher
        dispatcher.add_handler(CommandHandler("todo", wrap(self.on_todo), filters=admins & new),
                               group=HANDLER_GROUP)
        dispatcher.add_handler(CommandHandler("todos", wrap(self.on_list), filters=admins & new),
                               group=HANDLER_GROUP)
        dispatcher.add_handler(MessageHandler(admins & Locked(name="todo locked"), wrap(self.on_input)),
                               group=HANDLER_GROUP)
        dispatcher.add_handler(CallbackQueryHandler(wrap(self.on_button), pattern="^" + CALLBACK),
                               group=HANDLER_GROUP)
        threading.Thread(target=self.loop, name="todo reminders", daemon=True).start()

    def locked(self, user_id: int) -> bool:
        draft = self.drafts.get(user_id)
        return draft is not None and draft.awaiting is not None

    # endregion
    # region Telegram

    def send(self, chat_id: int, text: str, rows: Optional[Rows] = None, key: str = "", **kwargs) -> Any:
        return self.bot().send_message(chat_id, text, parse_mode="HTML", reply_markup=self.markup(rows, key),
                                       disable_web_page_preview=True, _force_main_bot=True, **kwargs)

    def edit(self, chat_id: int, message_id: Optional[int], text: str, rows: Optional[Rows] = None,
             key: str = ""):
        if message_id is None:
            return self.send(chat_id, text, rows, key)
        try:
            self.bot().edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
                                         reply_markup=self.markup(rows, key), disable_web_page_preview=True)
        except Exception as e:
            if "not modified" not in str(e):
                raise

    def delete(self, chat_id: int, message_id: Optional[int]):
        if message_id is None:
            return
        try:
            self.bot().delete_message(chat_id, message_id)
        except Exception:
            self.logger.debug("Failed to delete message %s in %s", message_id, chat_id, exc_info=True)

    @staticmethod
    def markup(rows: Optional[Rows], key: str) -> Any:
        if not rows:
            return None
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        buttons = []
        for row in rows:
            buttons.append([InlineKeyboardButton(label, url=action[4:]) if action.startswith("url:")
                            else InlineKeyboardButton(label, callback_data=CALLBACK + action + (":" + key if key else ""))
                            for label, action in row])
        return InlineKeyboardMarkup(buttons)

    def answer(self, query: Any, text: Optional[str] = None):
        try:
            self.bot().answer_callback_query(query.id, text=text)
        except Exception:
            self.logger.debug("Failed to answer callback query", exc_info=True)

    # endregion
    # region /todo

    def on_todo(self, update: Any):
        message = update.effective_message
        user = update.effective_user
        hint = " ".join(message.text.split()[1:]).strip() if message.text else ""
        reply = message.reply_to_message
        if reply is not None and message.is_topic_message and reply.message_id == message.message_thread_id:
            reply = None  # every message in a topic "replies" to the start of the topic
        source = self.source_of(reply) if reply is not None else None
        if source is None and hint:
            source = Source(None, None, hint, None, None, None, None)
            hint = ""
        draft = Draft(user.id, source, hint)
        with self.lock:
            old = self.drafts.get(user.id)
            self.drafts[user.id] = draft
        if old is not None:
            self.drop_buttons(old)
        try:
            if source is None:
                self.edit(user.id, None, "📝 <b>新待办</b>\n\n" + USAGE + "\n\n下面开始手动填写。")
                self.start_manual(draft)
            else:
                draft.card_id = self.send(user.id, self.mode_text(draft), self.mode_rows(), draft.key).message_id
        except Exception as e:
            self.logger.exception("Failed to start a to-do in the private chat of %s", user.id)
            with self.lock:
                self.drafts.pop(user.id, None)
            if message.chat.id != user.id:
                self.send(message.chat.id, "⚠️ 没法在私聊里发消息给你，请先私聊 bot 发一次 /start。\n{}".format(
                    html.escape(str(e)[:200])), reply_to_message_id=message.message_id)
            return
        if message.chat.id != user.id:
            rows: Rows = []
            username = getattr(self.bot().me, "username", None)
            if username:
                rows = [[("去私聊", "url:https://t.me/{}".format(username))]]
            self.send(message.chat.id, "📝 已在和 bot 的私聊中继续设置待办。", rows,
                      reply_to_message_id=message.message_id)

    def source_of(self, reply: Any) -> Source:
        """What is known of the message ``/todo`` replied to: from the log
        of ETM for one that came from or went to a slave chat, otherwise
        from Telegram alone."""
        chat_name, author, text = None, None, ""
        try:
            master: Any = coordinator.master
            log = master.db.get_msg_log(master_msg_id="{}.{}".format(reply.chat.id, reply.message_id))
            if log is not None and log.slave_origin_uid:
                msg = log.build_etm_msg(master.chat_manager, recur=False)
                chat_name = msg.chat.display_name
                author = "我" if isinstance(msg.author, SelfChatMember) else msg.author.display_name
                if author == chat_name:  # a private chat
                    author = None
                text = msg.text or ""
        except Exception:
            self.logger.exception("Failed to look up message %s.%s in the log of ETM",
                                  reply.chat.id, reply.message_id)
        if not text:
            text = reply.text or reply.caption or ""
        if not text:
            text = "[{}]".format("图片" if reply.photo else "文件" if reply.document else "非文字消息")
        origin = getattr(reply, "forward_origin", None)
        if chat_name is None:
            if reply.chat.type == "private":
                chat_name = "和 bot 的私聊"
            else:
                chat_name = reply.chat.title or "Telegram"
            if origin is not None:
                author = getattr(getattr(origin, "sender_user", None), "full_name", None) \
                    or getattr(origin, "sender_user_name", None) \
                    or getattr(getattr(origin, "sender_chat", None), "title", None) \
                    or getattr(getattr(origin, "chat", None), "title", None)
            elif reply.from_user is not None and not reply.from_user.is_bot:
                author = reply.from_user.full_name
        sent_at = origin.date if origin is not None else reply.date
        thread_id = reply.message_thread_id if reply.is_topic_message else None
        return Source(chat_name, author, text, sent_at, reply.chat.id, thread_id, reply.message_id)

    def mode_text(self, draft: Draft) -> str:
        text = "📝 <b>新待办</b>\n" + self.source_block(draft.source)
        if draft.hint:
            text += "\n补充：{}".format(html.escape(draft.hint))
        return text + "\n\n适合让 AI 自动提取关键信息吗？"

    @staticmethod
    def mode_rows() -> Rows:
        return [[("✅ 是，AI 提取", "ai"), ("✍️ 否，手动填写", "manual")], [("❌ 取消", "cancel")]]

    def source_block(self, source: Optional[Source]) -> str:
        if source is None:
            return ""
        lines = []
        if source.label():
            lines.append("来源：{}".format(html.escape(source.label())))
        if source.sent_at is not None:
            lines.append("时间：{}".format(fmt(source.sent_at, self.tz)))
        text = source.text if len(source.text) <= SOURCE_PREVIEW else source.text[:SOURCE_PREVIEW] + "…"
        lines.append("<blockquote>{}</blockquote>".format(html.escape(text)))
        return "\n".join(lines)

    # endregion
    # region AI

    def start_ai(self, draft: Draft):
        draft.mode = "ai"
        draft.busy = True
        self.edit(draft.owner_id, draft.card_id, self.mode_text(draft).rsplit("\n\n", 1)[0] + "\n\n⏳ 正在提取…")
        threading.Thread(target=self.run_extraction, args=(draft,), name="todo extract", daemon=True).start()

    def run_extraction(self, draft: Draft):
        try:
            result = self.ask_model(draft)
        except Exception as e:
            self.logger.exception("Failed to extract a to-do")
            draft.busy = False
            return self.edit(draft.owner_id, draft.card_id,
                             self.mode_text(draft).rsplit("\n\n", 1)[0]
                             + "\n\n⚠️ AI 提取失败：{}".format(html.escape(str(e)[:300])),
                             [[("🔄 重试", "ai"), ("✍️ 手动填写", "manual")], [("❌ 取消", "cancel")]], draft.key)
        draft.content = result.content
        draft.location = result.location
        draft.note = result.note
        draft.reason = result.reason
        draft.busy = False
        if result.event_at is None:
            # Keep what the model found; the user fills in the rest.
            draft.asked.update(i for i in ("content", "location", "note") if getattr(draft, i))
            return self.edit(draft.owner_id, draft.card_id,
                             self.mode_text(draft).rsplit("\n\n", 1)[0]
                             + "\n\n🤔 AI 没能判断出事项日期{}，请改为手动填写。".format(
                                 "（{}）".format(html.escape(result.reason)) if result.reason else ""),
                             [[("✍️ 手动填写", "manual")], [("❌ 取消", "cancel")]], draft.key)
        draft.event_at = result.event_at
        draft.guessed_time = result.guessed_time
        self.show_card(draft, replace=False)

    def ask_model(self, draft: Draft) -> extract.Extraction:
        middleware = self.middleware
        middleware.reload_config()
        client = middleware.chat_client()
        source = draft.source
        assert source is not None
        user = extract.user_message(self.now(), source.sent_at, self.tz, middleware.tz_name,
                                    source.label(), source.text, draft.hint)
        error: Exception = RuntimeError("no attempt made")
        for _ in range(2):  # a second try for a reply that is not the JSON asked for
            try:
                return extract.parse(client.complete(middleware.prompt, user), self.tz)
            except ValueError as e:
                self.logger.warning("Unusable extraction: %s", e)
                error = e
        raise error

    # endregion
    # region Filling in by hand

    def start_manual(self, draft: Draft):
        draft.mode = "manual"
        if draft.card_id is not None:
            self.edit(draft.owner_id, draft.card_id,
                      self.mode_text(draft).rsplit("\n\n", 1)[0] + "\n\n✍️ 手动填写")
            draft.card_id = None
        self.ask_next(draft, first=True)

    def ask_next(self, draft: Draft, first: bool = False):
        for field in FIELDS:
            if field not in draft.asked:
                return self.ask(draft, field, first=first)
        draft.awaiting = None
        if draft.event_at is not None and not draft.custom and not draft.offsets:
            default = at_default_time(draft.event_at, self.tz)
            if self.now() < default <= draft.event_at:
                draft.custom.append(default)
        self.show_card(draft, replace=True)

    def ask(self, draft: Draft, field: str, first: bool = False, error: str = ""):
        draft.awaiting = field
        draft.touched = time.time()
        example = (self.now() + timedelta(days=1)).replace(hour=15, minute=0).strftime("%Y-%m-%d %H:%M")
        text = QUESTIONS[field].format(INPUT_FORMAT, example)
        if error:
            text = "⚠️ {}\n\n{}".format(error, text)
        if first:
            text += "\n\n<i>填写期间你在这里发的文字不会转到微信。点“结束填写”或 10 分钟不操作会恢复正常。</i>"
        rows: Rows = []
        if draft.editing or field == "custom":
            rows.append([("↩️ 返回", "back")])
        elif field != "content":
            rows.append([("⏭ 跳过", "skip")])
        rows.append([("🛑 结束填写", "end")])
        old = draft.prompt_id
        draft.prompt_id = self.send(draft.owner_id, text, rows, draft.key).message_id
        self.strip(draft.owner_id, old)

    def strip(self, chat_id: int, message_id: Optional[int]):
        """Remove the buttons of an old question."""
        if message_id is None:
            return
        try:
            self.bot().edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception:
            self.logger.debug("Failed to remove buttons of %s", message_id, exc_info=True)

    def on_input(self, update: Any):
        message = update.effective_message
        if update.edited_message is not None:
            return  # what was typed has been taken already
        user_id = update.effective_user.id
        with self.lock:
            draft = self.drafts.get(user_id)
        if draft is None or draft.awaiting is None:
            return
        if time.time() - draft.touched >= LOCK_SECONDS:
            # Timed out just before the scheduler would have let the chat go.
            return self.end(draft, "10 分钟没有操作，待办没有保存，刚才这条消息也没有发出。")
        draft.touched = time.time()
        field = draft.awaiting
        text = (message.text or "").strip()
        if text.split(maxsplit=1)[:1] in (["/cancel"], ["/end"]):
            return self.end(draft, "已结束填写，待办没有保存。")
        if not text:
            return self.ask(draft, field, error="请发送文字。")
        if field in ("event", "custom"):
            when = parse_input(text, self.tz)
            if when is None:
                return self.ask(draft, field, error="格式不对：{}".format(html.escape(text[:50])))
            if when <= self.now():
                return self.ask(draft, field, error="{} 已经过去了。".format(fmt(when, self.tz, seconds=True)))
            if field == "custom":
                draft.custom.append(when)
            else:
                draft.event_at = when
                draft.guessed_time = False
                # Offsets that fall in the past for the new time go.
                draft.offsets &= set(future_offsets(when, self.now()))
        elif field == "content":
            draft.content = text[:200]
        elif text not in ("-", "－"):
            setattr(draft, field, text[:200])
        self.answered(draft, field)

    def answered(self, draft: Draft, field: str):
        if draft.editing or field == "custom":
            draft.editing = False
            draft.awaiting = None
            return self.show_card(draft, replace=True)
        draft.asked.add(field)
        self.ask_next(draft)

    def end(self, draft: Draft, text: str):
        with self.lock:
            if self.drafts.get(draft.owner_id) is draft:
                del self.drafts[draft.owner_id]
        self.drop_buttons(draft)
        self.send(draft.owner_id, "🛑 {}私聊已恢复正常转发。".format(text))

    def drop_buttons(self, draft: Draft):
        self.strip(draft.owner_id, draft.prompt_id)
        self.strip(draft.owner_id, draft.card_id)

    # endregion
    # region Card

    def card_text(self, draft: Draft) -> str:
        tz = self.tz
        lines = ["📝 <b>确认待办</b>", "内容：{}".format(html.escape(draft.content or "（未填写）"))]
        if draft.event_at is not None:
            lines.append("事项时间：{}{}".format(
                fmt(draft.event_at, tz), "（没有具体时间，按 08:00 计）" if draft.guessed_time else ""))
        else:
            lines.append("事项时间：未设置")
        if draft.location:
            lines.append("地点：{}".format(html.escape(draft.location)))
        if draft.note:
            lines.append("备注：{}".format(html.escape(draft.note)))
        if draft.source is not None and draft.source.label():
            lines.append("来源：{}".format(html.escape(draft.source.label())))
        if draft.reason and draft.mode == "ai":
            lines.append("<i>AI：{}</i>".format(html.escape(draft.reason)))
        lines.append("")
        now = self.now()
        chosen = []
        if draft.event_at is not None:
            chosen += [(offset_time(draft.event_at, k), OFFSET_BY_KEY[k][0]) for k in draft.offsets]
        chosen += [(i, "自定义") for i in draft.custom]
        chosen = sorted((i for i in chosen if i[0] > now), key=lambda i: i[0])
        lines.append("⏰ 提醒时间（可多选）：")
        if chosen:
            lines += ["· {}（{}）".format(fmt_short(when, tz), label) for when, label in chosen]
        else:
            lines.append("还没选，至少选一个")
        if draft.event_at is None:
            lines.append("<i>没有事项时间，只能自定义提醒时间。</i>")
        return "\n".join(lines)

    def card_rows(self, draft: Draft) -> Rows:
        rows: Rows = []
        keys = future_offsets(draft.event_at, self.now())
        offsets = [("✅ " if k in draft.offsets else "") + OFFSET_BY_KEY[k][0].replace(" ", "") for k in keys]
        for i in range(0, len(keys), 3):
            rows.append([(label, "off:" + k) for label, k in zip(offsets[i:i + 3], keys[i:i + 3])])
        custom = [("➕ 自定义时间", "custom")]
        if draft.custom:
            custom.append(("🗑 清空自定义", "clear"))
        rows.append(custom)
        rows.append([("🕐 改事项时间", "event"), ("✏️ 改内容", "content")])
        rows.append([("💾 保存", "save"), ("❌ 取消", "cancel")])
        return rows

    def show_card(self, draft: Draft, replace: bool):
        """Show the card for confirming; with ``replace``, as a new message
        below what was typed, dropping the old card."""
        draft.awaiting = None
        draft.touched = time.time()
        text, rows = self.card_text(draft), self.card_rows(draft)
        if replace or draft.card_id is None:
            old = draft.card_id
            draft.card_id = self.send(draft.owner_id, text, rows, draft.key).message_id
            self.delete(draft.owner_id, old)
            self.strip(draft.owner_id, draft.prompt_id)
            draft.prompt_id = None
        else:
            self.edit(draft.owner_id, draft.card_id, text, rows, draft.key)

    def save(self, draft: Draft, query: Any):
        now = self.now()
        times = draft.reminders(now)
        if not draft.content:
            return self.answer(query, "还没有提醒内容。")
        if not times:
            self.answer(query, "至少选一个还没过去的提醒时间。")
            return self.show_card(draft, replace=False)
        source = draft.source or Source(None, None, "", None, None, None, None)
        todo_id = self.store.add(
            draft.owner_id, draft.content, draft.event_at.timestamp() if draft.event_at else None,
            draft.location, draft.note, source.chat, source.author, source.text or None,
            source.tg_chat_id, source.tg_thread_id, source.tg_msg_id, [i.timestamp() for i in times])
        with self.lock:
            if self.drafts.get(draft.owner_id) is draft:
                del self.drafts[draft.owner_id]
        self.answer(query, "已保存")
        text = self.card_text(draft).split("\n\n", 1)[0].replace("📝 <b>确认待办</b>", "✅ <b>待办已设置</b> #{}".format(
            todo_id), 1)
        text += "\n\n⏰ 提醒：\n" + "\n".join("· " + fmt_short(i, self.tz) for i in times)
        self.edit(draft.owner_id, draft.card_id, text)

    # endregion
    # region Buttons

    def on_button(self, update: Any):
        query = update.callback_query
        bot = self.bot()
        if update.effective_user is None or update.effective_user.id not in bot.admins:
            return self.answer(query, "只有管理员可以操作。")
        parts = query.data[len(CALLBACK):].split(":")
        if parts[0] in ("r", "l"):
            return self.on_todo_button(query, parts)
        action, arg, key = parts[0], parts[1] if len(parts) > 2 else "", parts[-1]
        with self.lock:
            draft = self.drafts.get(update.effective_user.id)
        if draft is None or draft.key != key:
            self.answer(query, "这个待办已过期，请重新 /todo。")
            return self.strip(query.message.chat.id, query.message.message_id)
        if draft.busy:
            return self.answer(query, "正在处理，请稍候。")
        draft.touched = time.time()
        if action == "ai":
            if not self.middleware.ai_ready():
                return self.answer(query, "AI 中间件没有配置可用的模型，请改用手动填写。")
            self.answer(query)
            return self.start_ai(draft)
        if action == "manual":
            self.answer(query)
            return self.start_manual(draft)
        if action in ("cancel", "end"):
            self.answer(query, "已取消")
            with self.lock:
                if self.drafts.get(draft.owner_id) is draft:
                    del self.drafts[draft.owner_id]
            self.strip(draft.owner_id, draft.prompt_id)
            if draft.card_id is not None:
                self.edit(draft.owner_id, draft.card_id, "❌ 已取消\n\n" + html.escape(draft.content or "")
                          if draft.content else "❌ 已取消")
            if action == "end" or draft.prompt_id is not None:
                self.send(draft.owner_id, "🛑 已结束填写，待办没有保存。私聊已恢复正常转发。")
            return
        if action == "skip" and draft.awaiting is not None:
            self.answer(query)
            return self.answered(draft, draft.awaiting)
        if action == "back":
            self.answer(query)
            draft.editing = False
            return self.show_card(draft, replace=True)
        if action == "off":
            if arg in OFFSET_BY_KEY:
                draft.offsets ^= {arg}
            self.answer(query)
            return self.show_card(draft, replace=False)
        if action == "clear":
            draft.custom.clear()
            self.answer(query)
            return self.show_card(draft, replace=False)
        if action in ("custom", "event", "content"):
            self.answer(query)
            draft.editing = action != "custom"
            return self.ask(draft, action, first=True)
        if action == "save":
            return self.save(draft, query)
        self.answer(query)

    def on_todo_button(self, query: Any, parts: List[str]):
        """Buttons of a reminder (``r``) and of the list (``l``)."""
        kind, action, todo_id = parts[0], parts[1], int(parts[2])
        todo = self.store.get(todo_id)
        if todo is None or todo.status != "active":
            self.answer(query, "这个待办已经结束了。")
            if kind == "r":
                self.strip(query.message.chat.id, query.message.message_id)
            else:
                self.show_list(query.from_user.id, query.message.message_id)
            return
        now = self.now()
        note = ""
        if action == "done":
            self.store.close(todo_id, "done")
            self.answer(query, "已完成")
            note = "✅ 已完成"
        elif action == "cancel":
            self.store.close(todo_id, "cancelled")
            self.answer(query, "已取消")
            note = "❌ 已取消"
        else:
            if action == "morning":
                when = at_default_time(now, self.tz)
                if when <= now:
                    when = at_default_time(now + timedelta(days=1), self.tz)
            else:
                when = now + timedelta(minutes={"s10": 10, "s60": 60}.get(action, 10))
            self.store.snooze(todo_id, when.timestamp())
            self.answer(query, "好的")
            note = "⏰ 将在 {} 再提醒".format(fmt_short(when, self.tz))
        if kind == "l":
            return self.show_list(query.from_user.id, query.message.message_id)
        text = (query.message.text_html or "").split("\n\n<i>", 1)[0]
        try:
            self.edit(query.message.chat.id, query.message.message_id, text + "\n\n<i>{}</i>".format(note))
        except Exception:
            self.logger.debug("Failed to update reminder %s", query.message.message_id, exc_info=True)
            self.strip(query.message.chat.id, query.message.message_id)

    # endregion
    # region /todos

    def on_list(self, update: Any):
        self.show_list(update.effective_user.id, None)

    def show_list(self, owner_id: int, message_id: Optional[int]):
        todos = self.store.active(owner_id)
        if not todos:
            return self.edit(owner_id, message_id, "📋 没有未完成的待办。")
        lines = ["📋 <b>未完成的待办</b>（{}）".format(len(todos))]
        rows: Rows = []
        for todo in todos[:LIST_LIMIT]:
            pending = self.store.pending(todo.id)
            when = "下次提醒 " + fmt_short(datetime.fromtimestamp(pending[0], timezone.utc), self.tz) \
                if pending else "提醒已全部发出"
            lines.append("<b>#{}</b> {}\n　{}{}".format(
                todo.id, html.escape(todo.content), when,
                "，事项 " + fmt_short(datetime.fromtimestamp(todo.event_at, timezone.utc), self.tz)
                if todo.event_at else ""))
            rows.append([("✅ #{} 完成".format(todo.id), "l:done:{}".format(todo.id)),
                         ("❌ #{} 取消".format(todo.id), "l:cancel:{}".format(todo.id))])
        if len(todos) > LIST_LIMIT:
            lines.append("…另有 {} 条".format(len(todos) - LIST_LIMIT))
        self.edit(owner_id, message_id, "\n".join(lines), rows)

    # endregion
    # region Reminders

    def loop(self):
        # Let ETM start polling before the first reminders go out.
        self.stop_event.wait(30)
        while not self.stop_event.is_set():
            try:
                self.expire_drafts()
                self.remind_due()
            except Exception:
                self.logger.exception("Failed to check reminders")
            self.stop_event.wait(self.poll_seconds)

    def expire_drafts(self):
        now = time.time()
        with self.lock:
            idle = [d for d in self.drafts.values()
                    if (d.awaiting is not None and now - d.touched >= LOCK_SECONDS)
                    or now - d.touched >= DRAFT_SECONDS]
            for draft in idle:
                del self.drafts[draft.owner_id]
        for draft in idle:
            self.drop_buttons(draft)
            if draft.awaiting is not None:
                self.send(draft.owner_id, "⌛ 10 分钟没有操作，待办没有保存，私聊已恢复正常转发。")

    def remind_due(self):
        now = time.time()
        for reminder in self.store.due(now):
            if self.failed.get(reminder.id, 0) > now:
                continue
            todo = self.store.get(reminder.todo_id)
            if todo is None:
                continue
            try:
                self.remind(todo, reminder.remind_at, now)
            except Exception:
                self.logger.exception("Failed to send reminder %s of to-do %s", reminder.id, todo.id)
                self.failed[reminder.id] = now + RETRY_SECONDS
                continue
            self.failed.pop(reminder.id, None)
            self.store.mark_sent(reminder.id)

    def reminder_text(self, todo: Todo, remind_at: float, now: float) -> str:
        tz = self.tz
        lines = ["⏰ <b>待办提醒</b>：{}".format(html.escape(todo.content))]
        if todo.event_at:
            lines.append("🕐 事项时间：{}".format(fmt(datetime.fromtimestamp(todo.event_at, timezone.utc), tz)))
        if todo.location:
            lines.append("📍 地点：{}".format(html.escape(todo.location)))
        if todo.note:
            lines.append("📝 备注：{}".format(html.escape(todo.note)))
        label = " · ".join(i for i in (todo.source_chat, todo.source_author) if i)
        if label:
            lines.append("💬 来源：{}".format(html.escape(label)))
        if todo.source_text:
            text = todo.source_text
            if len(text) > REMINDER_PREVIEW:
                text = text[:REMINDER_PREVIEW] + "…"
            lines.append("<blockquote>{}</blockquote>".format(html.escape(text)))
        link = self.link(todo)
        if link:
            lines.append('<a href="{}">打开原消息</a>'.format(html.escape(link)))
        if now - remind_at > LATE_SECONDS:
            lines.append("<i>本应在 {} 提醒，晚了 {}</i>".format(
                fmt_short(datetime.fromtimestamp(remind_at, timezone.utc), tz), ago(now - remind_at)))
        return "\n".join(lines)

    @staticmethod
    def link(todo: Todo) -> Optional[str]:
        """Link to the message in a supergroup; other chats have none."""
        chat = str(todo.tg_chat_id or "")
        if not chat.startswith("-100") or not todo.tg_msg_id:
            return None
        if todo.tg_thread_id:
            return "https://t.me/c/{}/{}/{}".format(chat[4:], todo.tg_thread_id, todo.tg_msg_id)
        return "https://t.me/c/{}/{}".format(chat[4:], todo.tg_msg_id)

    def remind(self, todo: Todo, remind_at: float, now: float):
        from telegram import ReplyParameters
        text = self.reminder_text(todo, remind_at, now)
        rows: Rows = [[("✅ 完成", "r:done:{}".format(todo.id))],
                      [("10 分钟后", "r:s10:{}".format(todo.id)), ("1 小时后", "r:s60:{}".format(todo.id)),
                       ("明早 8 点", "r:morning:{}".format(todo.id))]]
        kwargs: Dict[str, Any] = dict(parse_mode="HTML", reply_markup=self.markup(rows, ""), disable_web_page_preview=True,
                                      _force_main_bot=True)
        if todo.tg_chat_id and todo.tg_msg_id:
            # Quote the message, across chats when it is not in this one.
            other = {} if todo.tg_chat_id == todo.owner_id else {"chat_id": todo.tg_chat_id}
            try:
                return self.bot().send_message(
                    todo.owner_id, text, reply_parameters=ReplyParameters(
                        todo.tg_msg_id, allow_sending_without_reply=True, **other), **kwargs)
            except Exception as e:
                self.logger.info("Failed to quote the message of to-do %s, sending without: %s", todo.id, e)
        self.bot().send_message(todo.owner_id, text, **kwargs)

    # endregion
