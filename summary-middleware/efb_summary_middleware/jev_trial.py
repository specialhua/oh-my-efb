# coding=utf-8
"""Label recent messages of a chat with Jev and print the result, to check
how well the labels fit before they are used for summaries.

Reads the config and the message database of the middleware (the database
read-only, so EFB can keep running)::

    ~/efb-venv/bin/python -m efb_summary_middleware.jev_trial --chat 某个群 --limit 200
"""

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from typing import Dict, List

from ruamel.yaml import YAML

from ehforwarderbot.utils import get_base_path

from .jev import DEFAULT_BASE_URL, Jev
from .labeler import CATEGORIES, MEDIA, Label, Labeler, LabelRow

MODULE_ID = "specialhua.summary"
PRICE_PER_MTOK = 0.042
"""US dollars per million input tokens; output tokens are free."""
NOISE = {"filler", "ad"}
"""Categories a summary could leave out."""

NAMES = {key: text.split("：", 1)[0] for key, text in CATEGORIES["zh"].items()}
NAMES[MEDIA] = "媒体"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", default="default", help="EFB profile (default: default)")
    parser.add_argument("--chat", help="chat as written in \"chats\" of the config; omit to list them")
    parser.add_argument("--limit", type=int, default=200, help="label the latest N messages (default: 200)")
    parser.add_argument("--lang", choices=["zh", "en"], default="zh",
                        help="language of the questions; messages stay as they are (default: zh)")
    parser.add_argument("--model", help="override jev.model of the config, e.g. jev-preview")
    parser.add_argument("--batch", type=int, default=30, help="messages labeled per request (default: 30)")
    parser.add_argument("--before", type=int, default=20, help="messages of context before a batch (default: 20)")
    parser.add_argument("--after", type=int, default=5, help="messages of context after a batch (default: 5)")
    parser.add_argument("--min-confidence", type=float, default=0.6,
                        help="confidence needed to count a category as sure (default: 0.6)")
    parser.add_argument("--highlight", type=float, default=0.2,
                        help="share of the kept messages most worth mentioning to mark with ★ (default: 0.2)")
    parser.add_argument("--out", help="also write the labels to this file, one JSON object per line")
    args = parser.parse_args()

    folder = get_base_path() / "profiles" / args.profile / MODULE_ID
    with (folder / "config.yaml").open(encoding="utf-8") as f:
        config = YAML(typ="safe").load(f) or {}
    section = config.get("jev")
    if not isinstance(section, dict) or not section.get("api_key"):
        sys.exit("No jev.api_key in {}".format(folder / "config.yaml"))
    jev = Jev(api_key=section["api_key"], model=args.model or section.get("model") or "jev-latest",
              base_url=section.get("base_url") or DEFAULT_BASE_URL, timeout=float(section.get("timeout", 60)))

    db = sqlite3.connect("file:{}?mode=ro".format(folder / "messages.db"), uri=True)
    if not args.chat:
        print("Chats in the database (use one with --chat):")
        for target, chat_name, count in db.execute(
                "SELECT target, MAX(chat_name), COUNT(*) FROM messages GROUP BY target"):
            print("  {}  ({}, {} messages)".format(target, chat_name, count))
        return
    # The newest rows to label, plus the ones before them as their context.
    rows: List[LabelRow] = db.execute(
        "SELECT id, ts, author, content FROM messages WHERE target = ? ORDER BY ts DESC, id DESC LIMIT ?",
        (args.chat, args.limit + args.before)).fetchall()[::-1]
    if not rows:
        sys.exit("No messages of chat \"{}\". Run without --chat to list the chats.".format(args.chat))
    start = max(0, len(rows) - args.limit)
    chat_name = db.execute("SELECT chat_name FROM messages WHERE target = ? ORDER BY id DESC LIMIT 1",
                           (args.chat,)).fetchone()[0]

    labeler = Labeler(jev, lang=args.lang, batch=args.batch,
                      context_before=args.before, context_after=args.after)
    usage: Dict[str, int] = {}
    began = time.monotonic()
    labels = labeler.label(chat_name, rows, start, usage=usage)
    elapsed = time.monotonic() - began

    out = open(args.out, "w", encoding="utf-8") if args.out else None
    sure = args.min_confidence
    kept = sorted((i.mention for i in labels if not is_noise(i, sure)), reverse=True)
    # Ranked within the chat, as how much a chat has worth mentioning varies.
    cut = kept[max(0, round(len(kept) * args.highlight) - 1)] if kept and args.highlight > 0 else 2.0
    for row, label in zip(rows[start:], labels):
        drop = is_noise(label, sure)
        star = not drop and label.mention >= cut
        print_label(row, label, sure, "  ✂" if drop else "  ★" if star else "")
        if out:
            out.write(json.dumps(dict(label._asdict(), author=row[2], content=row[3], ts=row[1],
                                      dropped=drop, highlighted=star), ensure_ascii=False) + "\n")
    if out:
        out.close()

    print("\n==== {} · {} 条 · model {} · lang {} ====".format(chat_name, len(labels), jev.model, args.lang))
    counts = Counter(i.category for i in labels)
    for key in list(CATEGORIES["zh"]) + [MEDIA]:
        if counts[key]:
            unsure = sum(1 for i in labels if i.category == key and i.category_confidence < sure)
            print("  {:<6} {:>4}  ({} 条把握不足)".format(NAMES[key], counts[key], unsure))
    dropped = len(labels) - len(kept)
    print("可以剔除（媒体，或附和/闲聊/广告且把握 ≥ {}）：{} 条，占 {:.0%}".format(
        sure, dropped, dropped / len(labels)))
    bands = [0, 0.2, 0.4, 0.6, 0.8, 1.01]
    print("其余 {} 条“值得提”的分布：{}".format(len(kept), "  ".join(
        "{:.1f}–{:.1f} {}".format(lo, min(hi, 1), sum(1 for i in kept if lo <= i < hi))
        for lo, hi in zip(bands, bands[1:]))))
    if kept and args.highlight > 0:
        print("★ 前 {:.0%}：值得提 ≥ {:.2f} 的 {} 条".format(args.highlight, cut, sum(1 for i in kept if i >= cut)))
    asked = sum(1 for i in labels if i.category != MEDIA)
    tokens = usage.get("input_tokens", 0)
    print("{} 个请求，耗时 {:.1f} 秒；输入 {} token，约 ${:.5f}；平均每条问过 Jev 的消息 {:.0f} token".format(
        len(list(labeler.batches(rows, start, len(rows)))), elapsed, tokens,
        tokens * PRICE_PER_MTOK / 1e6, tokens / max(asked, 1)))


def is_noise(label: Label, sure: float) -> bool:
    return label.category == MEDIA or label.category in NOISE and label.category_confidence >= sure


def print_label(row: LabelRow, label: Label, sure: float, mark: str):
    _, ts, author, content = row
    text = content.replace("\n", " ")
    if len(text) > 80:
        text = text[:80] + "…"
    print("[{}] {}: {}".format(datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"), author, text))
    ranked = sorted(label.category_probabilities.items(), key=lambda i: -i[1])
    category = "{} {:.2f}".format(NAMES.get(ranked[0][0], ranked[0][0]), ranked[0][1])
    if label.category_confidence < sure and len(ranked) > 1:
        category += " / {} {:.2f} ?".format(NAMES.get(ranked[1][0], ranked[1][0]), ranked[1][1])
    print("    {}  · 值得提 {:.2f}{}".format(category, label.mention, mark))


if __name__ == "__main__":
    main()
