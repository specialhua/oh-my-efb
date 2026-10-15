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
from .jev import DEFAULT_BASE_URL as JEV_BASE_URL, Jev
from .labeler import MEDIA, TASKS, Labeler
from .store import LabeledRow, Store
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


NOISE = {"filler", "ad"}
"""Categories left out of a summary when Jev is sure enough of them."""
TAGS = {"arrangement": "安排", "question": "提问", "answer": "回答", "info": "信息"}
"""Categories marked in the transcript given to the model."""
DUPLICATE_MIN_CHARS = 15
"""Repeats of a message at least this long are left out of a summary;
shorter ones like "1" or "收到" from different people are not repeats."""

LABEL_NOTE = """聊天记录已经过预处理：
- 行首带 ★ 的是预先判断最值得提到的消息，总结要覆盖到它们的内容（不必逐条罗列）
- 〔安排〕〔提问〕〔回答〕〔信息〕是消息类别标记，可以借此把问题和回答对应起来
- 纯附和、闲聊、广告、图片表情占位和重复消息已经省略，开头会注明省略了多少
这些标记是自动生成的，可能有误，一切以消息原文为准。"""


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
    label_settle_seconds: float = 600
    """A message is labeled once this old, or once enough messages after it
    have arrived to serve as its context, whichever comes first."""
    label_max_per_run: int = 300
    """Messages of a chat labeled at most in one pass, so a backlog (e.g. on
    enabling Jev) is worked off over several passes."""

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
        self.label_lock = threading.Lock()
        self.label_failing = False
        self.stop_event = threading.Event()
        atexit.register(self.stop_event.set)
        threading.Thread(target=self.schedule_loop, name="summary schedule", daemon=True).start()
        threading.Thread(target=self.label_loop, name="summary labeling", daemon=True).start()

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
        data["jev"] = self.load_jev(data.get("jev"))
        return data

    def load_jev(self, entry: Any) -> Optional[Dict[str, Any]]:
        """Settings of labeling from the optional ``jev`` section: the
        ``labeler``, and ``min_confidence`` and ``highlight``."""
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise ValueError("{}: \"jev\" must be a mapping.".format(self.middleware_id))
        for key, required in (("api_key", True), ("model", False), ("base_url", False)):
            value = entry.get(key)
            if (required or value is not None) and (not isinstance(value, str) or not value):
                raise ValueError("{}: \"jev.{}\" must be a non-empty string.".format(self.middleware_id, key))

        def number(key: str, default: float, low: float, high: float) -> float:
            value = entry.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low < value <= high:
                raise ValueError("{}: \"jev.{}\" must be a number in ({:g}, {:g}].".format(
                    self.middleware_id, key, low, high))
            return value

        timeout = number("timeout", 60, 0, float("inf"))
        min_confidence = number("min_confidence", 0.6, 0, 1)
        # 0 marks nothing; allowed although it is the open end of the range.
        highlight = entry.get("highlight", 0.2)
        if highlight != 0:
            highlight = number("highlight", 0.2, 0, 1)
        lang = entry.get("lang", "zh")
        if lang not in TASKS:
            raise ValueError("{}: \"jev.lang\" must be one of {}.".format(self.middleware_id, ", ".join(TASKS)))
        jev = Jev(api_key=entry["api_key"], model=entry.get("model") or "jev-latest",
                  base_url=entry.get("base_url") or JEV_BASE_URL, timeout=timeout)
        return {"labeler": Labeler(jev, lang=lang), "min_confidence": min_confidence, "highlight": highlight}

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
        jev = config['jev'] or {}
        self.labeler: Optional[Labeler] = jev.get("labeler")
        self.min_confidence: float = jev.get("min_confidence", 0.6)
        self.highlight: float = jev.get("highlight", 0.2)
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

    def label_loop(self):
        while not self.stop_event.wait(self.poll_seconds):
            for target in self.targets:
                if self.stop_event.is_set():
                    return
                try:
                    self.label_target(target)
                except Exception as e:
                    self.logger.exception("Failed to label messages of %s", target.name)
                    # Report only the first of consecutive failures; summaries
                    # go on with the messages left unlabeled.
                    if not self.label_failing:
                        self.report("⚠️ Jev 给 <b>{}</b> 的消息分类失败，总结会照常进行，只是不做筛选：{}".format(
                            html.escape(target.name), html.escape(str(e)[:500])))
                    self.label_failing = True
                    break
            else:
                self.label_failing = False

    def label_target(self, target: Target, final: bool = False):
        """Label the messages of a chat that have settled, see
        :attr:`label_settle_seconds`; with ``final``, all of them."""
        labeler = self.labeler
        if labeler is None:
            return
        with self.label_lock:
            pending = self.store.for_labeling(target.name, labeler.context_before, self.label_max_per_run,
                                              labeler.context_after)
            if pending is None:
                return
            rows, start, labeled = pending
            end = min(len(rows), start + self.label_max_per_run)
            if not final:
                now = time.time()
                while end > start and len(rows) - end < labeler.context_after \
                        and now - rows[end - 1][1] < self.label_settle_seconds:
                    end -= 1
            if all(rows[i][0] in labeled for i in range(start, end)):
                return
            chat_name = self.store.chat_info(target.name)[0] or target.name
            labels = labeler.label(chat_name, rows, start, end, skip=labeled)
            self.store.add_labels(labels, labeler.jev.model, time.time())
            self.logger.debug("Labeled %s messages of %s", len(labels), target.name)


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
        if self.labeler is not None:
            try:  # include the latest messages; on failure they stay unlabeled
                self.label_target(target, final=True)
            except Exception:
                self.logger.exception("Failed to label messages of %s before summarizing", target.name)
        rows = self.store.fetch_labeled(target.name, since, max_messages)
        chat_name = self.store.chat_info(target.name)[0] or target.name
        title = "{} 群聊总结".format(chat_name)
        if not rows:
            self.send_text("📋 <b>{}</b>\n{}没有记录到消息。".format(
                html.escape(title),
                "还" if lookback_days is None else "最近 {:g} 天".format(lookback_days)), destination)
            return

        lines, omitted = self.transcript(rows)
        meta = "{} ~ {} · {} 条消息{} · {} 人发言".format(
            datetime.fromtimestamp(rows[0][0]).strftime("%m-%d %H:%M"),
            datetime.fromtimestamp(rows[-1][0]).strftime("%m-%d %H:%M"),
            len(rows), "（筛掉 {}）".format(sum(omitted.values())) if omitted else "",
            len({row[1] for row in rows}))
        if not lines:
            self.send_text("📋 <b>{}</b>\n{}\n\n全是附和、闲聊、图片表情之类，没有需要总结的内容。".format(
                html.escape(title), html.escape(meta)), destination)
            return
        if omitted:
            lines.insert(0, "（已省略：{}）".format("，".join(
                "{} {} 条".format(kind, count) for kind, count in omitted.items())))
        summary = self.summarizer.summarize(chat_name, lines, LABEL_NOTE if self.labeler is not None else "")

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

    def transcript(self, rows: List[LabeledRow]) -> Tuple[List[str], Dict[str, int]]:
        """Lines of the transcript given to the model, and how many messages
        were left out of it by kind. Without labels it is the plain log."""
        omitted: Dict[str, int] = {}
        kept: List[LabeledRow] = []
        seen = set()
        for row in rows:
            _, _, content, category, confidence, _ = row
            kind = None
            if category == MEDIA:
                kind = "图片表情等"
            elif category in NOISE and confidence >= self.min_confidence:
                kind = "附和闲聊广告"
            elif len(content) >= DUPLICATE_MIN_CHARS and content in seen:
                kind = "重复"
            if kind:
                omitted[kind] = omitted.get(kind, 0) + 1
                continue
            seen.add(content)
            kept.append(row)
        # Ranked within the window, as how much a chat has worth mentioning varies.
        mentions = sorted((row[5] for row in kept if row[5] is not None), reverse=True)
        cut = mentions[max(0, round(len(mentions) * self.highlight) - 1)] \
            if mentions and self.highlight > 0 else float("inf")
        lines = []
        for ts, author, content, category, _, mention in kept:
            tag = "〔{}〕".format(TAGS[category]) if category in TAGS else ""
            lines.append("{}[{}] {}: {}{}".format("★ " if mention is not None and mention >= cut else "",
                                                  datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
                                                  author, tag, content))
        return lines, omitted

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
