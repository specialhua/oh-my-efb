# coding=utf-8

"""Render the constrained Markdown produced by the model (``## `` headings,
``- `` items, ``**bold**``) for Telegram and for a standalone HTML file."""

import html
import re
from typing import List, Tuple

HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
ITEM = re.compile(r"^\s*[-*•]\s+(.*)$")
BOLD = re.compile(r"\*\*(.+?)\*\*")

TELEGRAM_LIMIT = 4000
"""Kept under Telegram's 4096 so the message is never split by ETM."""

PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font: 16px/1.7 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       max-width: 46em; margin: 0 auto; padding: 16px; color: #222; background: #fff; }}
h1 {{ font-size: 1.3em; margin-bottom: 0; }}
h2 {{ font-size: 1.1em; margin-top: 1.6em; border-bottom: 1px solid #ddd; padding-bottom: .2em; }}
.meta {{ color: #888; font-size: .85em; }}
ul {{ padding-left: 1.3em; }} li {{ margin: .35em 0; }}
@media (prefers-color-scheme: dark) {{ body {{ color: #ddd; background: #1c1c1e; }} h2 {{ border-color: #444; }} }}
</style></head><body>
<h1>{title}</h1>
<p class="meta">{meta}</p>
{body}
</body></html>
"""


def _inline(text: str, tag: str) -> str:
    return BOLD.sub(r"<{0}>\1</{0}>".format(tag), html.escape(text, quote=False))


def telegram_lines(markdown: str) -> List[str]:
    """Convert to Telegram HTML. Tags never span lines, so any prefix of the
    result is still well-formed."""
    lines = []
    for line in markdown.splitlines():
        heading = HEADING.match(line)
        item = ITEM.match(line)
        if heading:
            lines.append("<b>{}</b>".format(html.escape(heading.group(1).replace("**", ""), quote=False)))
        elif item:
            lines.append("• " + _inline(item.group(1), "b"))
        else:
            lines.append(_inline(line, "b"))
    return lines


def telegram_message(header: str, markdown: str, notice: str) -> Tuple[str, bool]:
    """Build the message text.

    Returns:
        The text, and whether the summary had to be cut to fit (in which case
        ``notice`` is appended).
    """
    lines = telegram_lines(markdown)
    full = header + "\n".join(lines)
    if len(full) <= TELEGRAM_LIMIT:
        return full, False
    text = header
    budget = TELEGRAM_LIMIT - len(notice) - 2
    for line in lines:
        if len(text) + len(line) + 1 > budget:
            break
        text += line + "\n"
    return text.rstrip() + "\n\n" + notice, True


def html_page(title: str, meta: str, markdown: str) -> str:
    body: List[str] = []
    in_list = False
    for line in markdown.splitlines():
        heading = HEADING.match(line)
        item = ITEM.match(line)
        if in_list and not item:
            body.append("</ul>")
            in_list = False
        if heading:
            body.append("<h2>{}</h2>".format(html.escape(heading.group(1).replace("**", ""))))
        elif item:
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append("<li>{}</li>".format(_inline(item.group(1), "strong")))
        elif line.strip():
            body.append("<p>{}</p>".format(_inline(line, "strong")))
    if in_list:
        body.append("</ul>")
    return PAGE.format(title=html.escape(title), meta=html.escape(meta), body="\n".join(body))
