# coding=utf-8

import atexit
import html
import io
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ruamel.yaml import YAML

from ehforwarderbot import Middleware, Message, MsgType, coordinator
from ehforwarderbot.chat import SelfChatMember, SystemChatMember
from ehforwarderbot.types import ModuleID, InstanceID
from ehforwarderbot.utils import extra, get_config_path, get_data_path

from . import render
from .store import Store
from .summarizer import Summarizer

__version__ = "0.1.0"

DAY = 86400

PLACEHOLDERS = {
    MsgType.Image: "[图片]",
    MsgType.Voice: "[语音]",
    MsgType.Animation: "[动图]",
    MsgType.Video: "[视频]",
    MsgType.File: "[文件]",
    MsgType.Location: "[位置]",
    MsgType.Link: "[链接]",
    MsgType.Sticker: "[表情]",
    MsgType.Unsupported: "[不支持的消息]",
}


class Target:
    """A chat to summarize, with its own schedule and window."""

    def __init__(self, name: str, interval_days: float,
                 lookback_days: Optional[float], max_messages: Optional[int]):
        self.name = name
        """Chat name, alias or ID as written in the config; also its key in the store."""
        self.interval_days = interval_days
        self.lookback_days = lookback_days
        self.max_messages = max_messages
        self.retry_at = 0.0
        self.failing = False


class SummaryMiddleware(Middleware):
    """
    Log messages of the configured chats, and for each of them, every
    ``interval_days`` days (or on demand) send an LLM summary of the last
    ``lookback_days`` days / ``max_messages`` messages to the first admin
    through the bot of the Telegram master channel.
    """

    middleware_id: ModuleID = ModuleID("specialhua.summary")
    middleware_name: str = "Chat Summary Middleware"
    __version__: str = __version__

    poll_seconds: float = 60
    """How often the schedule is checked."""
    retry_seconds: float = 3600
    """Delay before a failed scheduled summary is attempted again."""
    retention_margin_days: float = 7
    """Messages are kept this long beyond the window of their chat."""

    def __init__(self, instance_id: Optional[InstanceID] = None):
        super().__init__(instance_id)
        self.logger = logging.getLogger(self.middleware_id)

        config = self.load_config()
        self.targets: List[Target] = config['chats']
        self.summarizer = Summarizer(
            base_url=config['base_url'], api_key=config['api_key'], model=config['model'],
            prompt=config.get('prompt') or "",
            max_chars=int(config.get('max_chars', 40000)),
            timeout=float(config.get('timeout', 180)))
        self.store = Store(get_data_path(self.middleware_id) / "messages.db")

        # A restart must not reset the cycle, so the last run is persisted.
        for target in self.targets:
            if self.store.get_last_run(target.name) is None:
                self.store.set_last_run(target.name, time.time())

        self.run_lock = threading.Lock()
        self.stop_event = threading.Event()
        atexit.register(self.stop_event.set)
        threading.Thread(target=self.schedule_loop, name="summary schedule", daemon=True).start()

    def load_config(self) -> Dict[str, Any]:
        path = get_config_path(self.middleware_id)
        if not path.exists():
            raise FileNotFoundError("Config file of {} does not exist. ({})".format(self.middleware_id, path))
        with path.open(encoding="utf-8") as f:
            data = YAML(typ="safe").load(f) or {}
        for key in ("base_url", "api_key", "model"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError("{}: \"{}\" must be a non-empty string.".format(self.middleware_id, key))
        chats = data.get("chats")
        if isinstance(chats, (str, int, dict)):
            chats = [chats]
        if not isinstance(chats, list) or not chats:
            raise ValueError("{}: \"chats\" must list at least one chat name or ID.".format(self.middleware_id))
        data["chats"] = [self.load_target(i, data) for i in chats]
        return data

    def load_target(self, entry: Any, defaults: Dict[str, Any]) -> Target:
        """Build a target from an item of ``chats``: a chat name, or a mapping
        with ``name`` and settings overriding the top-level ones."""
        if not isinstance(entry, dict):
            entry = {"name": entry}
        if isinstance(entry.get("name"), bool) or not isinstance(entry.get("name"), (str, int)) \
                or not str(entry["name"]):
            raise ValueError("{}: every item of \"chats\" needs a \"name\".".format(self.middleware_id))
        name = str(entry["name"])

        def number(key: str, kind: tuple) -> Any:
            value = entry.get(key, defaults.get(key))
            if value is not None and (isinstance(value, bool) or not isinstance(value, kind) or value <= 0):
                raise ValueError("{}: \"{}\" of chat \"{}\" must be a positive {}.".format(
                    self.middleware_id, key, name, "integer" if kind == (int,) else "number"))
            return value

        interval_days = number("interval_days", (int, float))
        lookback_days = number("lookback_days", (int, float))
        max_messages = number("max_messages", (int,))
        if interval_days is None:
            raise ValueError("{}: \"interval_days\" is not set for chat \"{}\".".format(self.middleware_id, name))
        if lookback_days is None and max_messages is None:
            raise ValueError("{}: chat \"{}\" needs \"lookback_days\" and/or \"max_messages\".".format(
                self.middleware_id, name))
        return Target(name, interval_days, lookback_days, max_messages)

    # region Logging messages

    def process_message(self, message: Message) -> Optional[Message]:
        try:
            self.log_message(message)
        except Exception:  # logging must never stop a message from being delivered
            self.logger.exception("Failed to log message %s", message)
        return message

    def match_target(self, message: Message) -> Optional[str]:
        chat = message.chat
        for target in self.targets:
            if target.name in (chat.uid, chat.name, chat.alias):
                return target.name
        return None

    def log_message(self, message: Message):
        if message.type == MsgType.Status:
            return
        target = self.match_target(message)
        if target is None:
            return
        content = self.describe(message)
        if not content:
            return
        author = message.author
        if isinstance(author, SelfChatMember):
            name = "我"
        elif isinstance(author, SystemChatMember):
            name = "系统"
        else:
            name = author.display_name
        self.store.add(target, message.chat.display_name, message.uid, time.time(),
                       name, content, edit=message.edit)

    @staticmethod
    def describe(message: Message) -> str:
        """Flatten a message into one line of text for the transcript."""
        parts = [PLACEHOLDERS.get(message.type, "")]
        attributes = message.attributes
        if message.type == MsgType.Link and attributes is not None:
            parts += [getattr(attributes, "title", ""), getattr(attributes, "description", ""),
                      getattr(attributes, "url", "")]
        elif message.type == MsgType.File:
            parts.append(message.filename or "")
        parts.append(message.text or "")
        return " ".join(" ".join(p.split()) for p in parts if p).strip()

    # endregion
    # region Schedule

    def schedule_loop(self):
        while not self.stop_event.wait(self.poll_seconds):
            for target in self.targets:
                now = time.time()
                last_run = self.store.get_last_run(target.name) or now
                if now < target.retry_at or now - last_run < target.interval_days * DAY:
                    continue
                with self.run_lock:
                    # Report only the first of consecutive failures.
                    ok = self.run(target, report_errors=not target.failing)
                if ok:
                    self.store.set_last_run(target.name, time.time())
                else:
                    target.retry_at = time.time() + self.retry_seconds
                target.failing = not ok

    @extra(name="立即汇总",
           desc="立即汇总配置的群聊，窗口与定时汇总相同，不影响定时周期。\n\n"
                "用法:\n    {function_name}\n        汇总所有配置的群\n"
                "    {function_name} 群名\n        只汇总配置中的这一个群")
    def summary(self, name: str = "") -> str:
        name = name.strip()
        targets = [i for i in self.targets if not name or i.name == name]
        if not targets:
            return "配置中没有“{}”，可选：{}".format(name, "、".join(i.name for i in self.targets))
        if not self.run_lock.acquire(blocking=False):
            return "已有一次汇总正在进行，请稍候。"

        def run():
            try:
                for target in targets:
                    self.run(target, report_errors=True)
            finally:
                self.run_lock.release()

        # The model can take minutes; don't hold up the master channel's handler.
        threading.Thread(target=run, name="summary on demand", daemon=True).start()
        return "已开始汇总，完成后会发送结果。"

    # endregion
    # region Summarizing

    def run(self, target: Target, report_errors: bool) -> bool:
        """Summarize a target and prune its log.

        Returns:
            Whether the summary was made and sent.
        """
        ok = True
        try:
            self.summarize(target)
        except Exception as e:
            ok = False
            self.logger.exception("Failed to summarize %s", target.name)
            if report_errors:
                try:
                    self.send_text("⚠️ <b>{}</b> 汇总失败：{}".format(
                        html.escape(target.name), html.escape(str(e)[:500])))
                except Exception:
                    self.logger.exception("Failed to report the error")
        if target.lookback_days is not None:
            self.store.prune(target.name, before=time.time() - (target.lookback_days
                                                                + self.retention_margin_days) * DAY)
        else:
            self.store.prune(target.name, keep=target.max_messages)
        return ok

    def summarize(self, target: Target):
        now = time.time()
        since = now - target.lookback_days * DAY if target.lookback_days is not None else None
        rows = self.store.fetch(target.name, since, target.max_messages)
        chat_name = self.store.chat_name(target.name) or target.name
        title = "{} 群聊总结".format(chat_name)
        if not rows:
            self.send_text("📋 <b>{}</b>\n{}没有记录到消息。".format(
                html.escape(title),
                "还" if target.lookback_days is None else "最近 {:g} 天".format(target.lookback_days)))
            return

        lines = ["[{}] {}: {}".format(datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"), author, content)
                 for ts, author, content in rows]
        summary = self.summarizer.summarize(chat_name, lines)

        meta = "{} ~ {} · {} 条消息 · {} 人发言".format(
            datetime.fromtimestamp(rows[0][0]).strftime("%m-%d %H:%M"),
            datetime.fromtimestamp(rows[-1][0]).strftime("%m-%d %H:%M"),
            len(rows), len({author for _, author, _ in rows}))
        header = "📋 <b>{}</b>\n{}\n\n".format(html.escape(title), html.escape(meta))
        text, truncated = render.telegram_message(header, summary, "<i>内容较长，完整版见附件。</i>")
        self.send_text(text)
        if truncated:
            page = io.BytesIO(render.html_page(title, meta, summary).encode("utf-8"))
            filename = "{}_{}.html".format(chat_name.replace("/", "_"),
                                           datetime.fromtimestamp(now).strftime("%Y-%m-%d"))
            bot, chat_id = self.bot()
            bot.send_document(chat_id, page, filename=filename)

    @staticmethod
    def bot():
        """The bot manager of the Telegram master channel, and the admin to write to."""
        master = coordinator.master
        return master.bot_manager, master.config['admins'][0]  # type: ignore[attr-defined]

    def send_text(self, text: str):
        bot, chat_id = self.bot()
        bot.send_message(chat_id, text, parse_mode="HTML")

    # endregion
