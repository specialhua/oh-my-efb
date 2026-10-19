# coding=utf-8

import re
from datetime import datetime
from typing import AbstractSet, Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple

from efb_ai_middleware import Jev

LabelRow = Tuple[int, float, str, str]
"""(message ID in the store, timestamp, author, content)"""

# Option names are stored as they are, so they stay the same in every language;
# only the descriptions Jev reads are translated. "other" is kept last, as
# Jev leans towards the options that come first.
CATEGORIES: Dict[str, Dict[str, str]] = {
    "zh": {
        "arrangement": "安排/通知：约定、计划、决定、通知，或要某人去做的事，包括“今晚几点上线”这类约定",
        "question": "提问：向群里或某人问问题、求助、征求意见",
        "answer": "回答：回应前面某个问题，给出答案或信息",
        "info": "信息/资料：具体的事实、数据、条件、链接、文章、文件",
        "opinion": "观点：有内容的看法、经历、理由，别人复述这段聊天时可能会提到",
        "filler": "附和/闲聊：去掉它，别人复述这段聊天时不会少任何内容，"
                  "如“收到”“好的”“哈哈”、表情、玩笑、调侃、吐槽、感叹",
        "ad": "广告/推销：广告、拼团、砍价、助力、推销、引流",
        "other": "其他：不属于以上任何一类",
    },
    "en": {
        "arrangement": "Arrangement: a plan, appointment, decision, announcement or something someone "
                       "is asked to do, including e.g. agreeing when to get online tonight",
        "question": "Question: asks the group or someone a question, for help or for an opinion",
        "answer": "Answer: responds to an earlier question with an answer or information",
        "info": "Information: concrete facts, numbers, requirements, links, articles or files",
        "opinion": "Opinion: a view, experience or reason with substance, which someone retelling "
                   "the conversation might mention",
        "filler": "Filler: removing it loses nothing when someone retells the conversation, e.g. "
                  "\"got it\", \"ok\", \"haha\", emoji, jokes, teasing, complaints, exclamations",
        "ad": "Advertising: ads, group-buy or bargain-help links, promotion, sales pitches",
        "other": "Other: fits none of the above",
    },
}

TASKS: Dict[str, Dict[str, str]] = {
    "zh": {
        "category": "state 是一段微信群聊天记录。判断 message 中这条消息（即 state 里 id 相同的那条）属于哪一类。"
                    "结合它前后的消息判断，例如“好的”“对”要看它在回应什么。",
        "mention": "state 是一段微信群聊天记录。如果有人总结这段聊天，message 中这条消息"
                   "（即 state 里 id 相同的那条）的内容值得被提到吗？结合它前后的消息判断。",
    },
    "en": {
        "category": "The state is a WeChat group chat log in Chinese. Classify the message given in "
                    "\"message\" (the one with the same id in the state). Use the surrounding messages "
                    "as context; e.g. whether a short reply like \"好的\" adds anything depends on what it answers.",
        "mention": "The state is a WeChat group chat log in Chinese. If someone summarized this conversation, "
                   "would the content of the message given in \"message\" (the one with the same id in the "
                   "state) be worth mentioning? Use the surrounding messages as context.",
    },
}

MENTION_CRITERIA: Dict[str, Dict[str, str]] = {
    "zh": {
        "true": "它带来了复述这段聊天时会提到的内容：一个安排、问题、答案、具体信息或有内容的观点",
        "false": "它只是附和、玩笑、情绪、重复别人的话，或离开上下文就没有意义",
    },
    "en": {
        "true": "It contributes something a retelling would include: an arrangement, a question, "
                "an answer, a concrete fact or an opinion with substance",
        "false": "It is only agreement, a joke, an emotion, a repetition, or meaningless out of context",
    },
}

MEDIA = "media"
"""Category given without asking Jev to messages with nothing but
placeholders such as [图片] or [表情], which it cannot see."""

ONLY_PLACEHOLDERS = re.compile(r"^(\s*\[[^\[\]]{1,80}\])+\s*$")
"""Content made of bracketed tokens only: placeholders of non-text messages,
notices of the slave channel like [Failed to download the sticker…], and
WeChat emoji codes like [捂脸]."""

MAX_TEXT = 1000
"""Characters of a message passed to Jev; longer ones are cut."""


class Label(NamedTuple):
    message_id: int
    category: str
    category_confidence: float
    category_probabilities: Dict[str, float]
    mention: float
    """Probability that the message is worth mentioning in a summary."""


def is_media(content: str) -> bool:
    return bool(ONLY_PLACEHOLDERS.match(content))


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
              usage: Optional[Dict[str, int]] = None, skip: AbstractSet[int] = frozenset()) -> List[Label]:
        """Label ``rows[start:end]``, using the rows before and after them as
        context. ``rows`` are the messages of one chat in chronological order.
        Rows whose message ID is in ``skip`` serve only as context and get no
        label. Token usage of the requests is added up into ``usage`` if given."""
        end = len(rows) if end is None else end
        labels: List[Label] = []
        for first, last, lo, hi in self.batches(rows, start, end):
            # IDs in the window are short positions, not the IDs in the store.
            # Media stays in the window: a reply may be about a picture.
            messages = [self.message(i - lo + 1, rows[i]) for i in range(lo, hi)]
            questions = {}
            for i in range(first, last):
                if rows[i][0] in skip or is_media(rows[i][3]):
                    continue
                message = messages[i - lo]
                questions["c{}".format(i)] = {
                    "type": "choice",
                    "instructions": {"task": TASKS[self.lang]["category"], "message": message},
                    "criteria": CATEGORIES[self.lang],
                }
                questions["m{}".format(i)] = {
                    "type": "noul",
                    "instructions": {"task": TASKS[self.lang]["mention"], "message": message},
                    "criteria": MENTION_CRITERIA[self.lang],
                }
            answers: Dict[str, dict] = {}
            if questions:
                answers, used = self.jev.ask({"chat": chat_name, "messages": messages}, questions)
                if usage is not None:
                    for key, count in used.items():
                        if isinstance(count, int):
                            usage[key] = usage.get(key, 0) + count
            for i in range(first, last):
                if rows[i][0] in skip:
                    continue
                if "c{}".format(i) not in answers:
                    labels.append(Label(rows[i][0], MEDIA, 1.0, {MEDIA: 1.0}, 0.0))
                    continue
                category, mention = answers["c{}".format(i)], answers["m{}".format(i)]
                labels.append(Label(rows[i][0], category["choice"], category["confidence"],
                                    category["probabilities"], mention["noul"]))
        return labels

    @staticmethod
    def message(position: int, row: LabelRow) -> Dict[str, object]:
        _, ts, author, content = row
        if len(content) > MAX_TEXT:
            content = content[:MAX_TEXT] + "…"
        return {"id": position, "time": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
                "author": author, "text": content}
