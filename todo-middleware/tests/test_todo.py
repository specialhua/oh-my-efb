# coding=utf-8

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ehforwarderbot import coordinator
from efb_todo_middleware import extract, flow
from efb_todo_middleware.store import Store
from efb_todo_middleware.timeutil import fmt, future_offsets, parse_input

TZ = ZoneInfo("Asia/Shanghai")
OWNER = 42


# region Pure functions

def test_parse_input():
    assert parse_input("2026-10-20 15:00", TZ) == datetime(2026, 10, 20, 15, 0, tzinfo=TZ)
    assert parse_input("2026-10-20 15:00:30", TZ) == datetime(2026, 10, 20, 15, 0, 30, tzinfo=TZ)
    assert parse_input(" 2026/1/5 8:05 ", TZ) == datetime(2026, 1, 5, 8, 5, tzinfo=TZ)
    assert parse_input("2026-02-30 08:00", TZ) is None
    assert parse_input("明天 8 点", TZ) is None


def test_future_offsets():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)
    assert future_offsets(now + timedelta(hours=2), now) == ["1h", "10m"]
    assert future_offsets(now + timedelta(days=2), now) == ["1d", "6h", "3h", "1h", "10m"]
    assert future_offsets(None, now) == []


def test_extract_parse():
    result = extract.parse('好的：{"content": "交材料", "date": "2026-10-26", "time": "09:00", '
                           '"location": null, "note": "带发票", "reason": "下周一"}', TZ)
    assert result.event_at == datetime(2026, 10, 26, 9, 0, tzinfo=TZ)
    assert not result.guessed_time and result.location is None and result.note == "带发票"

    guessed = extract.parse('{"content": "买菜", "date": "2026-10-20", "time": null}', TZ)
    assert guessed.event_at == datetime(2026, 10, 20, 8, 0, tzinfo=TZ) and guessed.guessed_time

    assert extract.parse('{"content": "想想", "date": null}', TZ).event_at is None
    with pytest.raises(ValueError):
        extract.parse("没有 JSON", TZ)
    with pytest.raises(ValueError):
        extract.parse('{"date": "2026-10-20"}', TZ)


def test_user_message_has_both_times():
    now = datetime(2026, 10, 19, 17, 12, tzinfo=TZ)
    sent = datetime(2026, 10, 19, 9, 30, tzinfo=TZ)
    text = extract.user_message(now, sent, TZ, "Asia/Shanghai", "工作群 · 张三", "下周一交材料")
    assert "当前时间：2026-10-19 17:12 星期一" in text and "消息发送时间：2026-10-19 09:30 星期一" in text


def test_store(tmp_path):
    store = Store(tmp_path / "t.db")
    todo_id = store.add(OWNER, "交材料", None, None, None, "群", "张三", "原文", -1001, 5, 9, [100.0, 50.0, 100.0])
    assert [r.remind_at for r in store.due(75)] == [50.0]
    store.mark_sent(store.due(75)[0].id)
    assert store.pending(todo_id) == [100.0]
    store.snooze(todo_id, 60.0)
    assert store.pending(todo_id) == [60.0, 100.0]
    assert [t.id for t in store.active(OWNER)] == [todo_id]
    assert store.close(todo_id, "done") and not store.close(todo_id, "done")
    assert store.due(1000) == [] and store.active(OWNER) == []

# endregion
# region Conversation, against a fake bot


class FakeBot:
    admins = [OWNER]
    me = SimpleNamespace(username="efb_bot")

    def __init__(self):
        self.sent = []
        self.edited = {}
        self.next_id = 1000

    def send_message(self, chat_id, text, **kwargs):
        self.next_id += 1
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, kwargs=kwargs, message_id=self.next_id))
        return self.sent[-1]

    def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edited[message_id] = (text, kwargs.get("reply_markup"))

    def edit_message_reply_markup(self, **kwargs):
        pass

    def delete_message(self, chat_id, message_id):
        pass

    def answer_callback_query(self, query_id, text=None):
        self.answered = text


class FakeMiddleware:
    middleware_id = "specialhua.todo"
    tz = TZ
    tz_name = "Asia/Shanghai"
    prompt = extract.PROMPT

    def __init__(self, reply):
        self.reply = reply

    def reload_config(self):
        pass

    def ai_ready(self):
        return True

    def chat_client(self):
        return SimpleNamespace(complete=lambda system, user: self.reply)


@pytest.fixture
def bot():
    fake = FakeBot()
    coordinator.master = SimpleNamespace(bot_manager=fake, db=None)
    yield fake
    del coordinator.master


def todoer(tmp_path, reply="{}"):
    return flow.Todoer(FakeMiddleware(reply), Store(tmp_path / "t.db"))


def text_update(text, chat_id=OWNER, reply=None):
    message = SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id, type="private"), message_id=1,
                              reply_to_message=reply, is_topic_message=False, message_thread_id=None)
    return SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=OWNER),
                           edited_message=None)


def press(t, data):
    query = SimpleNamespace(id="q", data=data, message=SimpleNamespace(chat=SimpleNamespace(id=OWNER),
                                                                      message_id=1, text_html=""),
                            from_user=SimpleNamespace(id=OWNER))
    t.on_button(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=OWNER)))


def test_manual(tmp_path, bot):
    t = todoer(tmp_path)
    t.on_todo(text_update("/todo"))  # no message: straight to filling in by hand
    draft = t.drafts[OWNER]
    assert draft.awaiting == "content" and t.locked(OWNER)

    t.on_input(text_update("去医院复查"))
    assert draft.awaiting == "event"
    t.on_input(text_update("明天下午"))
    assert draft.awaiting == "event" and "格式不对" in bot.sent[-1].text
    t.on_input(text_update("2000-01-01 08:00"))
    assert "已经过去" in bot.sent[-1].text
    when = (datetime.now(TZ) + timedelta(days=2)).replace(hour=15, minute=0, second=0, microsecond=0)
    t.on_input(text_update(when.strftime("%Y-%m-%d %H:%M")))
    assert draft.event_at == when and draft.awaiting == "location"
    press(t, "todo:skip:" + draft.key)
    t.on_input(text_update("-"))
    # All asked: the card, with 08:00 of that day suggested; the chat is let go.
    assert draft.awaiting is None and not t.locked(OWNER) and draft.location is None and draft.note is None
    assert [i.hour for i in draft.custom] == [8]
    assert "确认待办" in bot.sent[-1].text

    press(t, "todo:off:1h:" + draft.key)
    press(t, "todo:save:" + draft.key)
    assert OWNER not in t.drafts
    todo = t.store.active(OWNER)[0]
    assert todo.content == "去医院复查"
    assert t.store.pending(todo.id) == [(when.replace(hour=8)).timestamp(), (when - timedelta(hours=1)).timestamp()]


def test_end_releases(tmp_path, bot):
    t = todoer(tmp_path)
    t.on_todo(text_update("/todo"))
    key = t.drafts[OWNER].key
    press(t, "todo:end:" + key)
    assert OWNER not in t.drafts and not t.locked(OWNER)
    assert "恢复正常转发" in bot.sent[-1].text


def test_ai(tmp_path, bot):
    day = (datetime.now(TZ) + timedelta(days=3)).strftime("%Y-%m-%d")
    t = todoer(tmp_path, '{"content": "交报销材料", "date": "%s", "time": "09:00", "reason": "下周一"}' % day)
    t.on_todo(text_update("/todo 下周一前把报销材料交给财务"))
    draft = t.drafts[OWNER]
    assert draft.source.text == "下周一前把报销材料交给财务" and draft.card_id is not None
    draft.mode = "ai"
    t.run_extraction(draft)  # what start_ai runs in a thread
    assert draft.event_at.hour == 9 and not t.locked(OWNER)
    text, _ = bot.edited[draft.card_id]
    assert "交报销材料" in text and "AI：下周一" in text

    press(t, "todo:save:" + draft.key)
    assert "至少选一个" in bot.answered and OWNER in t.drafts
    press(t, "todo:off:1d:" + draft.key)
    press(t, "todo:custom:" + draft.key)
    assert t.locked(OWNER)
    custom = (datetime.now(TZ) + timedelta(days=1)).replace(second=0, microsecond=0)
    t.on_input(text_update(custom.strftime("%Y-%m-%d %H:%M")))
    assert not t.locked(OWNER) and draft.custom == [custom]
    press(t, "todo:save:" + draft.key)
    assert len(t.store.pending(t.store.active(OWNER)[0].id)) == 2


def test_ai_without_date(tmp_path, bot):
    t = todoer(tmp_path, '{"content": "想想周末去哪", "date": null, "location": "公园"}')
    t.on_todo(text_update("/todo 周末去哪玩"))
    draft = t.drafts[OWNER]
    t.run_extraction(draft)
    assert "手动填写" in bot.edited[draft.card_id][0]
    press(t, "todo:manual:" + draft.key)
    # Content and place came from the model; only the rest is asked.
    assert draft.awaiting == "event"


def test_reminder(tmp_path, bot):
    t = todoer(tmp_path)
    todo_id = t.store.add(OWNER, "交材料", None, None, None, "工作群", "张三", "下周一交材料",
                          -1001234, 7, 99, [1.0])
    t.remind_due()
    sent = bot.sent[-1]
    assert sent.chat_id == OWNER and "交材料" in sent.text and "https://t.me/c/1234/7/99" in sent.text
    assert "晚了" in sent.text
    assert sent.kwargs["reply_parameters"].chat_id == -1001234
    assert t.store.due(10 ** 10) == []

    press(t, "todo:r:s10:{}".format(todo_id))
    assert len(t.store.pending(todo_id)) == 1
    press(t, "todo:r:done:{}".format(todo_id))
    assert t.store.active(OWNER) == [] and t.store.pending(todo_id) == []


def test_list(tmp_path, bot):
    t = todoer(tmp_path)
    first = t.store.add(OWNER, "A", None, None, None, None, None, None, None, None, None, [2e9])
    t.store.add(OWNER, "B", None, None, None, None, None, None, None, None, None, [1.9e9])
    t.on_list(text_update("/todos"))
    text = bot.sent[-1].text
    assert text.index("B") < text.index("A")
    press(t, "todo:l:cancel:{}".format(first))
    assert [i.content for i in t.store.active(OWNER)] == ["B"]

# endregion


def test_lock_times_out(tmp_path, bot):
    t = todoer(tmp_path)
    t.on_todo(text_update("/todo"))
    t.drafts[OWNER].touched -= flow.LOCK_SECONDS
    # A message after the time is still held back, and lets the chat go.
    assert t.locked(OWNER)
    t.on_input(text_update("给微信群的话"))
    assert not t.locked(OWNER) and "没有发出" in bot.sent[-1].text

    t.on_todo(text_update("/todo"))
    t.drafts[OWNER].touched -= flow.LOCK_SECONDS
    t.expire_drafts()
    assert not t.locked(OWNER) and "10 分钟没有操作" in bot.sent[-1].text
