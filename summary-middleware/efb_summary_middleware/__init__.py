# coding=utf-8

import atexit
import html
import io
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ruamel.yaml import YAML

from ehforwarderbot import Middleware, Message, MsgType, coordinator
from ehforwarderbot.channel import SlaveChannel
from ehforwarderbot.chat import SelfChatMember, SystemChatMember
from ehforwarderbot.types import ModuleID, InstanceID
from ehforwarderbot.utils import extra, get_config_path, get_data_path

from . import render
from .store import Store
from .summarizer import Summarizer

__version__ = "0.1.0"

DAY = 86400

COMMAND = re.compile(r"^/summary(?:@\w+)?(?:\s+(.*))?$", re.S)
COMMAND_ARGS = re.compile(r"^(\d+)\s*(天|条)?$")
USAGE = ("用法：\n"
         "<code>/summary</code> 按本群配置的范围汇总\n"
         "<code>/summary 7</code> 汇总最近 7 天\n"
         "<code>/summary 500条</code> 汇总最近 500 条")

Destination = Tuple[int, Optional[int]]
"""Telegram chat ID and, in a forum, the topic to write to."""

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
                 lookback_days: Optional[float], max_messages: Optional[int],
                 retention_days: Optional[float] = None):
        self.name = name
        """Chat name, alias or ID as written in the config; also its key in the store."""
        self.interval_days = interval_days
        self.lookback_days = lookback_days
        self.max_messages = max_messages
        self.retention_days = retention_days
        self.retry_at = 0.0
        self.failing = False


class SummaryMiddleware(Middleware):
    """
    Log messages of the configured chats, and for each of them, every
    ``interval_days`` days send an LLM summary of the last ``lookback_days``
    days / ``max_messages`` messages through the bot of the Telegram master
    channel, to where the chat is linked in Telegram.

    A summary is also made on ``/summary`` sent to a chat from Telegram (the
    command itself is not delivered to the slave channel), and on the
    additional feature listed in ``/extra``.
    """

    middleware_id: ModuleID = ModuleID("specialhua.summary")
    middleware_name: str = "Chat Summary Middleware"
    __version__: str = __version__

    poll_seconds: float = 60
    """How often the schedule is checked."""
    retry_seconds: float = 3600
    """Delay before a failed scheduled summary is attempted again."""
    retention_margin_days: float = 7
    """Unless ``retention_days`` is set, messages are kept this long beyond
    the window of their chat."""

    def __init__(self, instance_id: Optional[InstanceID] = None):
        super().__init__(instance_id)
        self.logger = logging.getLogger(self.middleware_id)

        self.store = Store(get_data_path(self.middleware_id) / "messages.db")
        self.targets: List[Target] = []
        self.config_path = get_config_path(self.middleware_id)
        self.config_lock = threading.Lock()
        config = self.load_config()
        self.config_mtime = self.config_path.stat().st_mtime
        self.apply_config(config)

        self.run_lock = threading.Lock()
        self.stop_event = threading.Event()
        atexit.register(self.stop_event.set)
        threading.Thread(target=self.schedule_loop, name="summary schedule", daemon=True).start()

    def load_config(self) -> Dict[str, Any]:
        path = self.config_path
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
        retention_days = number("retention_days", (int, float))
        if interval_days is None:
            raise ValueError("{}: \"interval_days\" is not set for chat \"{}\".".format(self.middleware_id, name))
        if lookback_days is None and max_messages is None:
            raise ValueError("{}: chat \"{}\" needs \"lookback_days\" and/or \"max_messages\".".format(
                self.middleware_id, name))
        return Target(name, interval_days, lookback_days, max_messages, retention_days)

    def apply_config(self, config: Dict[str, Any]):
        targets: List[Target] = config['chats']
        running = {i.name: i for i in self.targets}
        for target in targets:
            if target.name in running:
                target.retry_at = running[target.name].retry_at
                target.failing = running[target.name].failing
            # A restart must not reset the cycle, so the last run is persisted.
            if self.store.get_last_run(target.name) is None:
                self.store.set_last_run(target.name, time.time())
        self.summarizer = Summarizer(
            base_url=config['base_url'], api_key=config['api_key'], model=config['model'],
            prompt=config.get('prompt') or "",
            max_chars=int(config.get('max_chars', 40000)),
            timeout=float(config.get('timeout', 180)))
        self.targets = targets

    def reload_config(self):
        """Pick up a changed config file. An invalid one is reported and the
        running config stays in place."""
        with self.config_lock:
            try:
                mtime = self.config_path.stat().st_mtime
            except OSError:
                return
            if mtime == self.config_mtime:
                return
            self.config_mtime = mtime
            try:
                self.apply_config(self.load_config())
                self.logger.info("Config reloaded.")
            except Exception as e:
                self.logger.exception("Failed to reload config")
                self.report("⚠️ 群聊总结的配置文件有误，继续使用之前的配置：{}".format(html.escape(str(e)[:500])))

    # region Logging messages

    def process_message(self, message: Message) -> Optional[Message]:
        args = self.command_args(message)
        if args is not None:
            try:
                self.handle_command(message, args)
            except Exception:
                self.logger.exception("Failed to handle command %s", message)
            # Whatever happened, the command is not for the slave chat to see.
            return None
        try:
            self.log_message(message)
        except Exception:  # logging must never stop a message from being delivered
            self.logger.exception("Failed to log message %s", message)
        return message

    def match_target(self, message: Message) -> Optional[Target]:
        chat = message.chat
        for target in self.targets:
            if target.name in (chat.uid, chat.name, chat.alias):
                return target
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
        self.store.add(target.name, message.chat.display_name, message.uid, time.time(),
                       name, content, edit=message.edit,
                       module_id=message.chat.module_id, chat_uid=message.chat.uid)

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
            self.reload_config()
            for target in self.targets:
                now = time.time()
                last_run = self.store.get_last_run(target.name) or now
                if now < target.retry_at or now - last_run < target.interval_days * DAY:
                    continue
                with self.run_lock:
                    # Report only the first of consecutive failures.
                    ok = self.run(target, not target.failing, self.target_destination(target),
                                  target.lookback_days, target.max_messages)
                if ok:
                    self.store.set_last_run(target.name, time.time())
                else:
                    target.retry_at = time.time() + self.retry_seconds
                target.failing = not ok

    @staticmethod
    def command_args(message: Message) -> Optional[str]:
        """Arguments of a ``/summary`` command sent from the master channel,
        ``None`` for any other message."""
        try:
            if message.type != MsgType.Text or not isinstance(message.deliver_to, SlaveChannel) \
                    or not isinstance(message.author, SelfChatMember):
                return None
            match = COMMAND.match((message.text or "").strip())
            return (match.group(1) or "").strip() if match else None
        except Exception:
            return None

    def handle_command(self, message: Message, args: str):
        self.reload_config()
        destination = self.destination(message.chat.module_id, message.chat.uid)
        target = self.match_target(message)
        if target is None:
            return self.report("这个群没有配置总结。", destination)
        match = COMMAND_ARGS.match(args)
        if not args:
            lookback_days, max_messages = target.lookback_days, target.max_messages
        elif match and int(match.group(1)) > 0 and match.group(2) == "条":
            lookback_days, max_messages = None, int(match.group(1))
        elif match and int(match.group(1)) > 0:
            lookback_days, max_messages = float(match.group(1)), target.max_messages
        else:
            return self.report(USAGE, destination)
        if not self.run_lock.acquire(blocking=False):
            return self.report("已有一次汇总正在进行，请稍候。", destination)

        def run():
            try:
                self.report("⏳ 正在汇总…", destination)
                self.run(target, True, destination, lookback_days, max_messages)
            finally:
                self.run_lock.release()

        threading.Thread(target=run, name="summary on command", daemon=True).start()

    @extra(name="立即汇总",
           desc="立即汇总配置的群聊，范围与定时汇总相同，结果发到这里，不影响定时周期。\n"
                "也可以在群对应的话题里直接发 /summary。\n\n"
                "用法:\n    {function_name}\n        汇总所有配置的群\n"
                "    {function_name} 群名\n        只汇总配置中的这一个群")
    def summary(self, name: str = "") -> str:
        self.reload_config()
        name = name.strip()
        targets = [i for i in self.targets if not name or i.name == name]
        if not targets:
            return "配置中没有“{}”，可选：{}".format(name, "、".join(i.name for i in self.targets))
        if not self.run_lock.acquire(blocking=False):
            return "已有一次汇总正在进行，请稍候。"

        def run():
            try:
                for target in targets:
                    self.run(target, True, self.admin(), target.lookback_days, target.max_messages)
            finally:
                self.run_lock.release()

        # The model can take minutes; don't hold up the master channel's handler.
        threading.Thread(target=run, name="summary on demand", daemon=True).start()
        return "已开始汇总，完成后会发送结果。"

    # endregion
    # region Summarizing

    def run(self, target: Target, report_errors: bool, destination: Destination,
            lookback_days: Optional[float], max_messages: Optional[int]) -> bool:
        """Summarize a target and prune its log.

        Returns:
            Whether the summary was made and sent.
        """
        ok = True
        try:
            self.summarize(target, destination, lookback_days, max_messages)
        except Exception as e:
            ok = False
            self.logger.exception("Failed to summarize %s", target.name)
            if report_errors:
                self.report("⚠️ <b>{}</b> 汇总失败：{}".format(
                    html.escape(target.name), html.escape(str(e)[:500])), destination)
        now = time.time()
        if target.retention_days is not None:
            self.store.prune(target.name, before=now - max(target.retention_days, target.lookback_days or 0) * DAY)
        elif target.lookback_days is not None:
            self.store.prune(target.name, before=now - (target.lookback_days + self.retention_margin_days) * DAY)
        else:
            self.store.prune(target.name, keep=target.max_messages)
        return ok

    def summarize(self, target: Target, destination: Destination,
                  lookback_days: Optional[float], max_messages: Optional[int]):
        now = time.time()
        since = now - lookback_days * DAY if lookback_days is not None else None
        rows = self.store.fetch(target.name, since, max_messages)
        chat_name = self.store.chat_info(target.name)[0] or target.name
        title = "{} 群聊总结".format(chat_name)
        if not rows:
            self.send_text("📋 <b>{}</b>\n{}没有记录到消息。".format(
                html.escape(title),
                "还" if lookback_days is None else "最近 {:g} 天".format(lookback_days)), destination)
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
        self.send_text(text, destination)
        if truncated:
            page = io.BytesIO(render.html_page(title, meta, summary).encode("utf-8"))
            filename = "{}_{}.html".format(chat_name.replace("/", "_"),
                                           datetime.fromtimestamp(now).strftime("%Y-%m-%d"))
            chat_id, thread_id = destination
            coordinator.master.bot_manager.send_document(  # type: ignore[attr-defined]
                chat_id, page, filename=filename, **self.thread_kwargs(thread_id))

    # endregion
    # region Telegram

    @staticmethod
    def admin() -> Destination:
        """The private chat with the first admin of the master channel."""
        return coordinator.master.config['admins'][0], None  # type: ignore[attr-defined]

    def destination(self, module_id: Optional[str], chat_uid: Optional[str]) -> Destination:
        """Where the master channel shows a slave chat: the group it is linked
        to, or its topic in the topic group; failing that, the admin."""
        if not module_id or not chat_uid:
            return self.admin()
        try:
            from efb_telegram_master import utils as etm_utils
            master: Any = coordinator.master
            slave_uid = etm_utils.chat_id_to_str(channel_id=module_id, chat_uid=chat_uid)
            linked = master.db.get_chat_assoc(slave_uid=slave_uid)
            chat_id = int(etm_utils.chat_id_str_to_id(linked[0])[1]) if linked else master.topic_group
            if not chat_id:
                return self.admin()
            thread_id = master.db.get_topic_thread_id(slave_uid=slave_uid, topic_chat_id=chat_id)
            if thread_id is None and not linked:  # no topic made for the chat yet
                return self.admin()
            return chat_id, thread_id
        except Exception:
            self.logger.exception("Failed to locate chat %s %s in Telegram", module_id, chat_uid)
            return self.admin()

    def target_destination(self, target: Target) -> Destination:
        _, module_id, chat_uid = self.store.chat_info(target.name)
        return self.destination(module_id, chat_uid)

    @staticmethod
    def thread_kwargs(thread_id: Optional[int]) -> Dict[str, Any]:
        return {"message_thread_id": thread_id} if thread_id else {}

    def send_text(self, text: str, destination: Optional[Destination] = None):
        chat_id, thread_id = destination or self.admin()
        coordinator.master.bot_manager.send_message(  # type: ignore[attr-defined]
            chat_id, text, parse_mode="HTML", **self.thread_kwargs(thread_id))

    def report(self, text: str, destination: Optional[Destination] = None):
        """Send a notice, to the admin if it can't reach its destination; never raises."""
        for dest in dict.fromkeys([destination or self.admin(), self.admin()]):
            try:
                return self.send_text(text, dest)
            except Exception:
                self.logger.exception("Failed to send to %s: %s", dest, text)

    # endregion
