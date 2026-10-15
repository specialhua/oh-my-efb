# coding=utf-8

from datetime import datetime
from typing import Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple

from .jev import Jev

LabelRow = Tuple[int, float, str, str]
"""(message ID in the store, timestamp, author, content)"""

# Option names are stored as they are, so they stay the same in every language;
# only the descriptions Jev reads are translated. "other" is kept last, as
# Jev leans towards the options that come first.
CATEGORIES: Dict[str, Dict[str, str]] = {
    "zh": {
        "notice": "通知/公告：面向群里所有人的通知、安排、规则、提醒",
        "decision": "结论/决定：大家达成的结论、做出的决定、确认下来的方案",
        "question": "提问：向群里或某人询问信息、求助、征求意见",
        "answer": "回答：针对前面某个问题给出的信息或解答",
        "task": "待办/日程：分派任务、提到截止时间、约定活动的时间地点",
        "resource": "链接/资料：分享有实质内容的链接、文章、文件、资料",
        "discussion": "讨论/观点：有实质内容的观点、经验、信息，但不是在回答问题",
        "ack": "附和/收到：“收到”“好的”“+1”“哈哈”、表情、单纯的赞同或感谢，没有新信息",
        "chitchat": "闲聊：寒暄、玩笑、与群里正事无关的闲谈",
        "ad": "广告/推销：广告、拼团、砍价、助力、推销、引流",
        "other": "其他：不属于以上任何一类",
    },
    "en": {
        "notice": "Notice: an announcement, arrangement, rule or reminder addressed to the whole group",
        "decision": "Decision: a conclusion the group reached, a decision made, or a plan confirmed",
        "question": "Question: asks the group or someone for information, help or an opinion",
        "answer": "Answer: gives information that answers an earlier question",
        "task": "Task/schedule: assigns a task, mentions a deadline, or sets the time or place of an event",
        "resource": "Resource: shares a substantive link, article, file or material",
        "discussion": "Discussion: a substantive opinion, experience or piece of information "
                      "that is not answering a question",
        "ack": "Acknowledgement: \"got it\", \"ok\", \"+1\", \"haha\", a sticker, plain agreement "
               "or thanks, with no new information",
        "chitchat": "Chitchat: greetings, jokes, small talk unrelated to the group's business",
        "ad": "Advertising: ads, group-buy or bargain-help links, promotion, sales pitches",
        "other": "Other: fits none of the above",
    },
}

VALUE_LEVELS: Dict[str, List[str]] = {
    "zh": [
        "没有信息：删掉它，任何人对这段聊天的理解都不受影响",
        "背景：提供了一点上下文，但单独看并不重要",
        "有用：包含具体的信息、观点或问题，总结里值得提一句",
        "关键：决定、通知、截止时间、需要有人去做的事，总结里必须出现",
    ],
    "en": [
        "No information: removing it changes nobody's understanding of the conversation",
        "Background: adds a little context but does not matter on its own",
        "Useful: contains a concrete piece of information, opinion or question worth a mention in a summary",
        "Key: a decision, announcement, deadline or action someone must take; a summary must include it",
    ],
}

TASKS: Dict[str, Dict[str, str]] = {
    "zh": {
        "category": "state 是一段微信群聊天记录。判断 message 中这条消息（即 state 里 id 相同的那条）属于哪一类。"
                    "结合它前后的消息判断，例如“好的”“对”要看它在回应什么。",
        "value": "state 是一段微信群聊天记录。评估 message 中这条消息（即 state 里 id 相同的那条）"
                 "对于总结这段聊天有多重要。结合它前后的消息判断。",
    },
    "en": {
        "category": "The state is a WeChat group chat log in Chinese. Classify the message given in "
                    "\"message\" (the one with the same id in the state). Use the surrounding messages "
                    "as context; e.g. whether a short reply like \"好的\" adds anything depends on what it answers.",
        "value": "The state is a WeChat group chat log in Chinese. Rate how important the message given in "
                 "\"message\" (the one with the same id in the state) is for a summary of this conversation. "
                 "Use the surrounding messages as context.",
    },
}

MAX_TEXT = 1000
"""Characters of a message passed to Jev; longer ones are cut."""


class Label(NamedTuple):
    message_id: int
    category: str
    category_confidence: float
    category_probabilities: Dict[str, float]
    value: float
    """Probability-weighted level, from 0 (no information) to 3 (key)."""
    value_confidence: float


class Labeler:
    """Classify the messages of a chat with Jev, each judged together with
    the messages around it."""

    def __init__(self, jev: Jev, lang: str = "zh", batch: int = 30,
                 context_before: int = 20, context_after: int = 5):
        if lang not in TASKS:
            raise ValueError("lang must be one of {}".format(", ".join(TASKS)))
        self.jev = jev
        self.lang = lang
        self.batch = batch
        self.context_before = context_before
        self.context_after = context_after

    def batches(self, rows: Sequence[LabelRow], start: int, end: int) -> Iterator[Tuple[int, int, int, int]]:
        """Split ``rows[start:end]`` into batches; yields the bounds of the
        messages to label and of the window shown as their context."""
        for first in range(start, end, self.batch):
            last = min(first + self.batch, end)
            yield first, last, max(0, first - self.context_before), min(len(rows), last + self.context_after)

    def label(self, chat_name: str, rows: Sequence[LabelRow], start: int = 0, end: Optional[int] = None,
              usage: Optional[Dict[str, int]] = None) -> List[Label]:
        """Label ``rows[start:end]``, using the rows before and after them as
        context. ``rows`` are the messages of one chat in chronological order.
        Token usage of the requests is added up into ``usage`` if given."""
        end = len(rows) if end is None else end
        labels: List[Label] = []
        for first, last, lo, hi in self.batches(rows, start, end):
            # IDs in the window are short positions, not the IDs in the store.
            messages = [self.message(i - lo + 1, rows[i]) for i in range(lo, hi)]
            questions = {}
            for i in range(first, last):
                message = messages[i - lo]
                questions["c{}".format(i)] = {
                    "type": "choice",
                    "instructions": {"task": TASKS[self.lang]["category"], "message": message},
                    "criteria": CATEGORIES[self.lang],
                }
                questions["v{}".format(i)] = {
                    "type": "score",
                    "instructions": {"task": TASKS[self.lang]["value"], "message": message},
                    "criteria": VALUE_LEVELS[self.lang],
                }
            answers, used = self.jev.ask({"chat": chat_name, "messages": messages}, questions)
            if usage is not None:
                for key, count in used.items():
                    if isinstance(count, int):
                        usage[key] = usage.get(key, 0) + count
            for i in range(first, last):
                category, value = answers["c{}".format(i)], answers["v{}".format(i)]
                labels.append(Label(rows[i][0], category["choice"], category["confidence"],
                                    category["probabilities"], value["score"], value["confidence"]))
        return labels

    @staticmethod
    def message(position: int, row: LabelRow) -> Dict[str, object]:
        _, ts, author, content = row
        if len(content) > MAX_TEXT:
            content = content[:MAX_TEXT] + "…"
        return {"id": position, "time": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
                "author": author, "text": content}
