"""Turn the small amount of markup models write into HTML.

Summaries and answers use *this* and **this** for emphasis, and sometimes a
heading or a bullet list. Escaping the whole string leaves the asterisks on
the page. This is not a Markdown engine. It covers the marks Yarn actually
asks the model for, and it escapes everything else.
"""

from __future__ import annotations

import re

# Do not cross a tag or another asterisk. A star inside `code` must not
# swallow the HTML that was just written around it.
_DOUBLE = re.compile(r"\*\*([^*<\n]+?)\*\*")
_SINGLE = re.compile(r"(?<!\*)\*([^*<\n]+?)\*(?!\*)")
_CODE = re.compile(r"`([^`]+)`")
_HASH = re.compile(r"^#{1,3}\s+(.+)$")
_STAR_HEAD = re.compile(r"^\*\*([^*]+)\*\*$")
_BULLET = re.compile(r"^[-*•]\s+(.+)$")
_NUMBER = re.compile(r"^\d+[.)]\s+(.+)$")


def html_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def markup_inline(text: str) -> str:
    """Emphasis inside one line. The result is safe to drop into HTML."""
    escaped = html_escape(text.strip())
    escaped = _CODE.sub(r"<code>\1</code>", escaped)
    escaped = _DOUBLE.sub(r"<strong>\1</strong>", escaped)
    escaped = _SINGLE.sub(r"<strong>\1</strong>", escaped)
    return escaped


def markup_to_html(text: str) -> str:
    """A summary or an answer, as a few HTML blocks. Asterisks do not remain."""
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    blocks: list[str] = []
    lines = raw.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        hashed = _HASH.match(line)
        if hashed:
            blocks.append(f"<h3>{markup_inline(hashed.group(1))}</h3>")
            index += 1
            continue
        headed = _STAR_HEAD.match(line)
        if headed:
            blocks.append(f"<h3>{html_escape(headed.group(1).strip())}</h3>")
            index += 1
            continue
        if _BULLET.match(line):
            items: list[str] = []
            while index < len(lines) and _BULLET.match(lines[index].strip()):
                item = _BULLET.match(lines[index].strip()).group(1)
                items.append(f"<li>{markup_inline(item)}</li>")
                index += 1
            blocks.append("<ul>" + "".join(items) + "</ul>")
            continue
        if _NUMBER.match(line):
            items = []
            while index < len(lines) and _NUMBER.match(lines[index].strip()):
                item = _NUMBER.match(lines[index].strip()).group(1)
                items.append(f"<li>{markup_inline(item)}</li>")
                index += 1
            blocks.append("<ol>" + "".join(items) + "</ol>")
            continue
        paragraph = [line]
        index += 1
        while index < len(lines):
            nxt = lines[index].strip()
            if (
                not nxt
                or _HASH.match(nxt)
                or _STAR_HEAD.match(nxt)
                or _BULLET.match(nxt)
                or _NUMBER.match(nxt)
            ):
                break
            paragraph.append(nxt)
            index += 1
        blocks.append(f"<p>{markup_inline(' '.join(paragraph))}</p>")
    return "".join(blocks)
