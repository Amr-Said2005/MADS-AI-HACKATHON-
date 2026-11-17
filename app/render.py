"""Readable HTML for a run's output files, shown in the web UI.

Output files hold text written by a model, so nothing in them is trusted: every piece of text is
escaped first, and the only tags in the result are the ones this module writes. Links are made
only for http and https addresses.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_ITEM = re.compile(r"^(\s*)(?:[-*+]|(\d+)[.)])\s+(.*)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")
_TOKEN = re.compile(r"`([^`\n]+)`|\[([^\]\n]+)\]\((https?://[^\s)]+)\)|(https?://[^\s<>\"']+)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])_([^_\n]+)_(?![\w*])")
_LABELS = ("ASSUMPTION", "MOCKED", "STUBBED")
_LABEL = re.compile(r"\b(" + "|".join(_LABELS) + r")\b")


def _emphasis(text: str) -> str:
    """Escape plain text, then mark bold, italics and the ASSUMPTION / MOCKED / STUBBED labels."""
    out = html.escape(text, quote=True)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    return _LABEL.sub(r'<span class="label \1">\1</span>', out)


def _link(url: str, label_html: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{label_html}</a>'


def inline(text: str) -> str:
    """One line of Markdown as HTML: code spans, links, bare addresses, bold and italics."""
    out, pos = [], 0
    for m in _TOKEN.finditer(text):
        out.append(_emphasis(text[pos:m.start()]))
        code, label, url, bare = m.groups()
        if code is not None:
            out.append(f"<code>{html.escape(code)}</code>")
        elif url is not None:
            out.append(_link(url, _emphasis(label)))
        else:
            trail = ""
            while bare and bare[-1] in ".,;:!?)":
                trail, bare = bare[-1] + trail, bare[:-1]
            out.append(_link(bare, html.escape(bare)) + html.escape(trail))
        pos = m.end()
    out.append(_emphasis(text[pos:]))
    return "".join(out)


def _cells(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def _table(lines: list[str]) -> str:
    head, body = _cells(lines[0]), [_cells(l) for l in lines[2:]]
    out = ['<div class="tablewrap"><table class="stack">', "<thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead>", "<tbody>"]
    for row in body:
        row = (row + [""] * len(head))[:max(len(head), len(row))]
        cells = []
        for i, c in enumerate(row):
            name = html.escape(head[i], quote=True) if i < len(head) else ""
            cells.append(f'<td data-label="{name}">{inline(c)}</td>')
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table></div>")
    return "\n".join(out)


def _list(lines: list[str]) -> str:
    """Nested lists from indentation. A line that is not an item continues the item above it."""
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, tag)
    open_item = False
    for line in lines:
        m = _ITEM.match(line)
        if not m:
            out.append(" " + inline(line.strip()))
            continue
        indent, tag = len(m.group(1).expandtabs(4)), ("ol" if m.group(2) else "ul")
        while stack and indent < stack[-1][0]:
            out.append("</li></" + stack.pop()[1] + ">")
        if stack and indent == stack[-1][0]:
            if open_item:
                out.append("</li>")
            if tag != stack[-1][1]:
                out.append("</" + stack.pop()[1] + ">")
                out.append(f"<{tag}>")
                stack.append((indent, tag))
        else:
            out.append(f"<{tag}>")
            stack.append((indent, tag))
        out.append("<li>" + inline(m.group(3)))
        open_item = True
    while stack:
        out.append("</li></" + stack.pop()[1] + ">")
    return "".join(out)


def markdown_to_html(text: str) -> str:
    """A small, safe subset of Markdown: headings, paragraphs, lists, tables, quotes, rules and code."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip() or _COMMENT.match(line):
            i += 1
        elif _FENCE.match(line):
            j = i + 1
            while j < n and not _FENCE.match(lines[j]):
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(lines[i + 1:j])) + "</code></pre>")
            i = j + 1
        elif _HEADING.match(line):
            m = _HEADING.match(line)
            level = min(len(m.group(1)) + 1, 6)  # the page has its own h1
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
        elif _RULE.match(line):
            out.append("<hr>")
            i += 1
        elif line.lstrip().startswith("|") and i + 1 < n and _TABLE_RULE.match(lines[i + 1]):
            j = i + 2
            while j < n and lines[j].lstrip().startswith("|"):
                j += 1
            out.append(_table(lines[i:j]))
            i = j
        elif line.lstrip().startswith(">"):
            j = i
            quoted = []
            while j < n and lines[j].lstrip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[j]))
                j += 1
            out.append("<blockquote>" + markdown_to_html("\n".join(quoted)) + "</blockquote>")
            i = j
        elif _ITEM.match(line):
            j = i + 1
            while j < n and lines[j].strip() and (_ITEM.match(lines[j]) or lines[j].startswith((" ", "\t"))):
                j += 1
            out.append(_list(lines[i:j]))
            i = j
        else:
            j = i + 1
            while (j < n and lines[j].strip() and not _HEADING.match(lines[j]) and not _FENCE.match(lines[j])
                   and not _ITEM.match(lines[j]) and not lines[j].lstrip().startswith(("|", ">")) and not _RULE.match(lines[j])):
                j += 1
            out.append("<p>" + "<br>".join(inline(l.strip()) for l in lines[i:j]) + "</p>")
            i = j
    return "\n".join(out)


def _name(key: Any) -> str:
    return html.escape(str(key).replace("_", " "))


def _value(v: Any) -> str:
    if isinstance(v, dict):
        rows = "".join(f"<tr><th>{_name(k)}</th><td>{_value(x)}</td></tr>" for k, x in v.items())
        return f'<table class="kv">{rows}</table>' if rows else "—"
    if isinstance(v, list):
        if v and all(isinstance(x, dict) for x in v):
            cols: list[str] = []
            for x in v:
                cols += [k for k in x if k not in cols]
            head = "".join(f"<th>{_name(c)}</th>" for c in cols)
            body = "".join("<tr>" + "".join(f'<td data-label="{_name(c)}">{_value(x.get(c, ""))}</td>' for c in cols) + "</tr>" for x in v)
            return f'<div class="tablewrap"><table class="stack"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
        return "<ul>" + "".join(f"<li>{_value(x)}</li>" for x in v) + "</ul>" if v else "—"
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return inline(str(v))


def json_to_html(text: str) -> str:
    """A JSON file as nested tables. Text that is not valid JSON is shown as it is."""
    try:
        data = json.loads(text)
    except ValueError:
        return "<pre>" + html.escape(text) + "</pre>"
    return _value(data)
