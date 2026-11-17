"""Build a small static website from fixed templates.

Used in the Build step (milestone 3). The model writes no HTML: it returns a
content object, code validates it, and code renders it into the templates in
site_templates/. That makes the step fast and keeps the pages safe:

- every page carries the draft banner;
- the buy control does nothing and is labelled MOCKED;
- the templates contain no testimonial, review, countdown or earnings block;
- all text is escaped, so content cannot inject markup or scripts;
- nothing is fetched from another host: styles, the logo mark and the icons
  are inline, and the colour theme is picked by code from the site name.

Lesson text may use a small Markdown-like subset (paragraphs, bullet and
numbered lists, tables, sub-headings, bold, inline code). Code parses it into
blocks; every piece of text is escaped before any tag is built around it.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape

TEMPLATE_DIR = Path(__file__).parent / "site_templates"
OUTPUT_PREFIX = "phase3_step4_site"

KIND_LABELS = {
    "notes": "Notes", "course": "Course", "templates": "Template pack",
    "guide": "Guide", "checklist": "Checklist",
}
MAX_ITEMS, MAX_LESSONS, MAX_FAQ = 6, 12, 8
MAX_BODY, MAX_TABLE_COLUMNS, MAX_STEPS = 12000, 8, 4
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TAG_RE = re.compile(r"<[^>]{0,200}>")

# Colour tokens. BASE holds the neutral palette, THEMES the accent presets; each has a light and a dark variant.
# tests/test_site_builder.py computes the contrast of every text pair in both variants.
BASE = {
    "light": {"bg": "#fbfaf8", "surface": "#ffffff", "ink": "#1b1b1f", "muted": "#5c5a55", "line": "#e4e1d9",
              "note-bg": "#fbefd6", "note-ink": "#7a4b00"},
    "dark": {"bg": "#131417", "surface": "#1c1d22", "ink": "#ecebe6", "muted": "#a9a8a2", "line": "#34353c",
             "note-bg": "#3a2d10", "note-ink": "#f0cd85"},
}
THEMES = {
    "indigo": {"light": {"accent": "#3b4cca", "on-accent": "#ffffff", "tint": "#eceefb"},
               "dark": {"accent": "#a5b0ff", "on-accent": "#14173a", "tint": "#23264a"}},
    "teal": {"light": {"accent": "#0b6b63", "on-accent": "#ffffff", "tint": "#e2f3f0"},
             "dark": {"accent": "#5fd4c6", "on-accent": "#04231f", "tint": "#12332f"}},
    "plum": {"light": {"accent": "#8a2a6b", "on-accent": "#ffffff", "tint": "#f8e9f2"},
             "dark": {"accent": "#f0a3d6", "on-accent": "#34092a", "tint": "#3d1c33"}},
    "ember": {"light": {"accent": "#a8401a", "on-accent": "#ffffff", "tint": "#fbece4"},
              "dark": {"accent": "#ffb089", "on-accent": "#3a1404", "tint": "#40231a"}},
    "forest": {"light": {"accent": "#2c6a2f", "on-accent": "#ffffff", "tint": "#e7f3e6"},
               "dark": {"accent": "#9bd69c", "on-accent": "#0b260c", "tint": "#1d341e"}},
}

# A small fixed icon set: simple line shapes on a 24 by 24 grid, drawn here so no image is fetched.
ICONS = {
    "book": '<path d="M12 6v14M12 6C10 4.5 7 4 4 4.5v14c3-.5 6 0 8 1.5 2-1.5 5-2 8-1.5v-14C17 4 14 4.5 12 6z"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2"/><circle cx="4.5" cy="12" r="1.2"/>'
            '<circle cx="4.5" cy="18" r="1.2"/>',
    "check": '<rect x="3.5" y="3.5" width="17" height="17" rx="4"/><path d="M8 12.5l3 3 5-6.5"/>',
    "grid": '<rect x="3.5" y="3.5" width="17" height="17" rx="2.5"/><path d="M3.5 9.5h17M9.5 3.5v17"/>',
    "layers": '<path d="M12 3.5l8.5 4.5-8.5 4.5L3.5 8z"/><path d="M3.5 12.5l8.5 4.5 8.5-4.5"/>'
              '<path d="M3.5 16.5L12 21l8.5-4.5"/>',
    "pencil": '<path d="M4 20l1-4.5L16.5 4l3.5 3.5L8.5 19z"/><path d="M14 6.5l3.5 3.5"/>',
    "wave": '<path d="M2.5 12c2-7 4.5-7 6.5 0s4.5 7 6.5 0 4-7 6 0"/>',
    "spark": '<path d="M12 3v5M12 16v5M3 12h5M16 12h5M6 6l3 3M15 15l3 3M18 6l-3 3M9 15l-3 3"/>',
}
KIND_ICONS = {"notes": "book", "course": "layers", "templates": "grid", "guide": "list", "checklist": "check"}
_ICON_WORDS = (
    ("check", ("checklist", "check", "step")),
    ("grid", ("template", "table", "sheet", "planner", "board", "tracker")),
    ("pencil", ("practice", "exercise", "question", "worked", "example", "quiz")),
    ("wave", ("signal", "circuit", "formula", "equation", "frequency")),
    ("layers", ("lesson", "module", "course", "structure", "chapter")),
    ("book", ("note", "summary", "summaries", "guide", "reading")),
)
DEFAULT_CTA = "See what is inside"
DEFAULT_STEPS = [
    {"title": "Pick what you need", "text": "Open a product page to see what is inside and who it is for."},
    {"title": "Read it in order", "text": "Lessons are numbered, and every lesson links to the one before and after it."},
    {"title": "Buy when checkout is ready", "text": "Checkout is MOCKED in this version. No payment is taken."},
]


class SiteContentError(ValueError):
    """The content object does not fit the templates."""


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:60].strip("-")


def _text(value, field: str, required: bool = True, limit: int = 600) -> str:
    """Plain text only: tags removed, whitespace collapsed, length capped."""
    if value is None:
        value = ""
    if not isinstance(value, (str, int, float)):
        raise SiteContentError(f"{field} must be text")
    text = re.sub(r"\s+", " ", _TAG_RE.sub("", str(value))).strip()
    if required and not text:
        raise SiteContentError(f"{field} is missing")
    return text[:limit]


def _block_text(value, field: str, required: bool = True, limit: int = 1500) -> str:
    """Like _text, but line breaks are kept so that lists and tables can be recognised."""
    if value is None:
        value = ""
    if not isinstance(value, (str, int, float)):
        raise SiteContentError(f"{field} must be text")
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in _TAG_RE.sub("", str(value)).replace("\r", "").split("\n")]
    text = "\n".join(lines).strip()
    if required and not text:
        raise SiteContentError(f"{field} is missing")
    return text[:limit]


def _price_text(value) -> str | None:
    """Whole-dollar price, or None when the price has not been set."""
    if value in (None, "", 0):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise SiteContentError("price_usd must be a number") from exc
    if amount <= 0:
        return None
    return f"${amount:,.0f}" if amount == int(amount) else f"${amount:,.2f}"


# ---- theme, logo mark and icons: all chosen by code -------------------------------------------------------------

def _hash(text: str) -> int:
    return int(hashlib.sha256(" ".join((text or "").lower().split()).encode("utf-8")).hexdigest(), 16)


def pick_theme(name: str, wanted: str | None = None) -> str:
    """The theme preset for a site: the one asked for when it exists, otherwise fixed by the site name."""
    key = str(wanted or "").strip().lower()
    if key in THEMES:
        return key
    return sorted(THEMES)[_hash(name) % len(THEMES)]


def theme_css(theme: str) -> Markup:
    """The colour tokens of a theme as CSS, light first and dark under prefers-color-scheme. Constants only."""
    def block(mode: str) -> str:
        return " ".join(f"--{key}:{value};" for key, value in {**BASE[mode], **THEMES[theme][mode]}.items())
    return Markup(f":root {{ color-scheme: light dark; {block('light')} }}\n"
                  f"@media (prefers-color-scheme: dark) {{ :root {{ {block('dark')} }} }}")


def monogram(name: str) -> str:
    words = re.findall(r"[^\W_]+", name or "")
    if not words:
        return "?"
    return (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper()


def logo_svg(name: str) -> Markup:
    """An inline logo mark: the monogram in a rounded shape. Corner radius and the dot position follow the name."""
    seed = _hash(name)
    radius = (8, 12, 16, 20)[(seed >> 8) % 4]
    dot_x, dot_y = ((31, 9), (9, 9), (31, 31), (9, 31))[(seed >> 16) % 4]
    letters = monogram(name)
    return Markup(
        '<svg class="logo" viewBox="0 0 40 40" width="36" height="36" aria-hidden="true" focusable="false">'
        f'<rect class="logo-shape" width="40" height="40" rx="{radius}"/>'
        f'<circle class="logo-dot" cx="{dot_x}" cy="{dot_y}" r="5"/>'
        f'<text class="logo-text" x="20" y="{26 if len(letters) == 2 else 27}" text-anchor="middle" '
        f'font-size="{16 if len(letters) == 2 else 20}">{escape(letters)}</text></svg>')


def icon_name(text: str) -> str:
    """The icon for a line of text: by keyword when one fits, otherwise fixed by the text."""
    lowered = (text or "").lower()
    for name, words in _ICON_WORDS:
        if any(word in lowered for word in words):
            return name
    return sorted(ICONS)[_hash(text) % len(ICONS)]


def icon_svg(name: str) -> Markup:
    shape = ICONS.get(name) or ICONS["spark"]
    return Markup('<svg class="icon" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" '
                  'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" '
                  f'focusable="false">{shape}</svg>')


# ---- lesson bodies: Markdown-like text to blocks ----------------------------------------------------------------

_BULLET_RE = re.compile(r"^[-*+•]\s+(.*)$")
_NUMBER_RE = re.compile(r"^\d{1,3}[.)]\s+(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE_RE = re.compile(r"^(?:[-*_]\s*){3,}$")
_SEPARATOR_CELL_RE = re.compile(r"^:?-{1,}:?$")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")


def inline(text: str) -> Markup:
    """Bold, emphasis and inline code. The text is escaped first; tags are then built around escaped text only."""
    safe = str(escape(text))
    safe = re.sub(r"\[([^\]\n]+)\]\([^)\n]*\)", r"\1", safe)  # a Markdown link keeps its words; no address is linked
    parts = _CODE_SPAN_RE.split(safe)
    for index, part in enumerate(parts):
        if index % 2:
            parts[index] = f"<code>{part}</code>"
        else:
            part = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", part)
            parts[index] = re.sub(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])", r"<em>\1</em>", part)
    return Markup("".join(parts))


def plain(text: str) -> str:
    """Text without the Markdown marks, for excerpts."""
    text = re.sub(r"\[([^\]\n]+)\]\([^)\n]*\)", r"\1", text)
    return re.sub(r"\s+", " ", re.sub(r"[*`#|]+", " ", text)).strip()


def _expand_collapsed(paragraph: str) -> str:
    """A list or table whose line breaks were collapsed into one line gets them back. Other text is unchanged."""
    if "\n" in paragraph:
        return paragraph
    marker = paragraph[:2]
    if marker in ("- ", "* ", "• ") and f" {marker}" in paragraph:
        return "\n".join(marker + part.strip() for part in paragraph[2:].split(f" {marker}") if part.strip())
    if re.match(r"^1[.)] ", paragraph):
        close, lines, rest, number = paragraph[1], [], paragraph, 2
        while f" {number}{close} " in rest:
            head, _, tail = rest.partition(f" {number}{close} ")
            lines.append(head)
            rest, number = f"{number}{close} {tail}", number + 1
        return "\n".join(lines + [rest])
    if paragraph.startswith("|") and "| |" in paragraph:
        return paragraph.replace("| |", "|\n|")
    return paragraph


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")][:MAX_TABLE_COLUMNS]


def _starts_block(line: str) -> bool:
    return bool(not line or line.startswith(("|", "```")) or _BULLET_RE.match(line) or _NUMBER_RE.match(line)
                or _HEADING_RE.match(line) or _RULE_RE.match(line))


def parse_blocks(text: str) -> list[dict]:
    """Split lesson text into blocks: p, h, ul, ol, table, code. Every string in a block is already escaped."""
    lines = [line.strip() for line in text.split("\n")]
    blocks: list[dict] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line or _RULE_RE.match(line):
            index += 1
        elif line.startswith("```"):
            index += 1
            code = []
            while index < len(lines) and not lines[index].startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            if any(code):
                blocks.append({"type": "code", "text": "\n".join(code)})
        elif (heading := _HEADING_RE.match(line)) and len(heading.group(2)) <= 100:
            if heading.group(2).strip("# "):
                blocks.append({"type": "h", "html": inline(heading.group(2).strip("# "))})
            index += 1
        elif line.startswith("|") and line.count("|") >= 2:
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                rows.append(_cells(lines[index]))
                index += 1
            has_head = len(rows) > 1 and all(_SEPARATOR_CELL_RE.match(cell) for cell in rows[1])
            rows = [row for row in rows if not all(_SEPARATOR_CELL_RE.match(cell) or not cell for cell in row)]
            if rows:
                width = max(len(row) for row in rows)
                rows = [[inline(cell) for cell in row + [""] * (width - len(row))] for row in rows]
                blocks.append({"type": "table", "head": rows[0] if has_head else [], "rows": rows[1:] if has_head else rows})
        elif _BULLET_RE.match(line) or _NUMBER_RE.match(line):
            pattern, kind = (_BULLET_RE, "ul") if _BULLET_RE.match(line) else (_NUMBER_RE, "ol")
            entries = []
            while index < len(lines) and (pattern.match(lines[index]) or (entries and not _starts_block(lines[index]))):
                found = pattern.match(lines[index])
                if found:
                    entries.append(found.group(1))
                else:
                    entries[-1] += " " + lines[index]
                index += 1
            entries = [inline(entry) for entry in entries if entry.strip()]
            if entries:
                blocks.append({"type": kind, "items": entries})
        else:
            words = [_HEADING_RE.sub(r"\2", line)]
            index += 1
            while index < len(lines) and not _starts_block(lines[index]):
                words.append(lines[index])
                index += 1
            blocks.append({"type": "p", "html": inline(" ".join(words)), "text": plain(" ".join(words))})
    return blocks


def _lesson(raw: dict, where: str) -> dict:
    """One lesson. `paragraphs` is today's shape; an optional `body` holds the whole text with its line breaks."""
    raw_paragraphs = raw.get("paragraphs") or []
    if isinstance(raw_paragraphs, str):
        raw_paragraphs = [raw_paragraphs]
    if not isinstance(raw_paragraphs, list):
        raise SiteContentError(f"{where}.paragraphs must be a list")
    paragraphs = [_block_text(p, f"{where}.paragraphs", limit=1500) for p in raw_paragraphs]
    body = _block_text(raw.get("body"), f"{where}.body", required=False, limit=MAX_BODY)
    if not paragraphs and not body:
        raise SiteContentError(f"{where}.paragraphs is missing")
    blocks = parse_blocks(body or "\n\n".join(_expand_collapsed(p) for p in paragraphs))
    if not blocks:
        raise SiteContentError(f"{where} has no readable content")
    first = next((block["text"] for block in blocks if block["type"] == "p" and block["text"]), "")
    excerpt = first if len(first) <= 150 else first[:147].rsplit(" ", 1)[0] + "..."
    return {"title": _text(raw.get("title"), f"{where}.title", limit=120), "paragraphs": paragraphs,
            "blocks": blocks, "excerpt": excerpt}


def validate(content: dict) -> dict:
    """Check and normalise the content object. Raises SiteContentError."""
    if not isinstance(content, dict):
        raise SiteContentError("content must be an object")
    site_in = content.get("site") or {}
    if not isinstance(site_in, dict):
        raise SiteContentError("site must be an object")
    site = {
        "name": _text(site_in.get("name"), "site.name", limit=80),
        "tagline": _text(site_in.get("tagline"), "site.tagline", limit=160),
        "audience": _text(site_in.get("audience"), "site.audience", limit=200),
        "about": _text(site_in.get("about"), "site.about", limit=600),
    }
    site["theme"] = pick_theme(site["name"], site_in.get("theme") if isinstance(site_in.get("theme"), str) else None)
    site["cta"] = _text(site_in.get("cta"), "site.cta", required=False, limit=40) or DEFAULT_CTA
    items_in = content.get("items") or []
    if not isinstance(items_in, list) or not 1 <= len(items_in) <= MAX_ITEMS:
        raise SiteContentError(f"items must hold 1 to {MAX_ITEMS} entries")
    items, seen = [], set()
    for index, raw in enumerate(items_in, start=1):
        where = f"items[{index}]"
        if not isinstance(raw, dict):
            raise SiteContentError(f"{where} must be an object")
        title = _text(raw.get("title"), f"{where}.title", limit=100)
        slug = str(raw.get("slug") or slugify(title))
        if not _SLUG_RE.match(slug):
            raise SiteContentError(f"{where}.slug must be lower-case words joined by hyphens")
        if slug in seen:
            raise SiteContentError(f"{where}.slug repeats: {slug}")
        seen.add(slug)
        kind = str(raw.get("kind") or "").lower()
        if kind not in KIND_LABELS:
            raise SiteContentError(f"{where}.kind must be one of {sorted(KIND_LABELS)}")
        inside = [_text(line, f"{where}.whats_inside", limit=200) for line in (raw.get("whats_inside") or [])]
        if not 2 <= len(inside) <= 8:
            raise SiteContentError(f"{where}.whats_inside must hold 2 to 8 lines")
        lessons_in = raw.get("lessons") or []
        if not isinstance(lessons_in, list) or len(lessons_in) > MAX_LESSONS:
            raise SiteContentError(f"{where}.lessons must hold at most {MAX_LESSONS} entries")
        lessons = []
        for number, lesson in enumerate(lessons_in, start=1):
            lwhere = f"{where}.lessons[{number}]"
            if not isinstance(lesson, dict):
                raise SiteContentError(f"{lwhere} must be an object")
            lessons.append(_lesson(lesson, lwhere))
        items.append({
            "slug": slug, "kind": kind, "kind_label": KIND_LABELS[kind], "icon": KIND_ICONS[kind], "title": title,
            "summary": _text(raw.get("summary"), f"{where}.summary", limit=300),
            "for_whom": _text(raw.get("for_whom"), f"{where}.for_whom", limit=300),
            "whats_inside": inside, "inside": [{"text": line, "icon": icon_name(line)} for line in inside],
            "lessons": lessons, "price_text": _price_text(raw.get("price_usd")),
        })
    faq_in = content.get("faq") or []
    if not isinstance(faq_in, list) or len(faq_in) > MAX_FAQ:
        raise SiteContentError(f"faq must hold at most {MAX_FAQ} entries")
    faq = [{"q": _text(e.get("q"), "faq.q", limit=200), "a": _text(e.get("a"), "faq.a", limit=500)}
           for e in faq_in if isinstance(e, dict)]
    steps_in = content.get("steps") or []
    if not isinstance(steps_in, list) or len(steps_in) > MAX_STEPS:
        raise SiteContentError(f"steps must hold at most {MAX_STEPS} entries")
    steps = [{"title": _text(e.get("title"), "steps.title", limit=80), "text": _text(e.get("text"), "steps.text", limit=240)}
             for e in steps_in if isinstance(e, dict)] or [dict(step) for step in DEFAULT_STEPS]
    return {"site": site, "items": items, "faq": faq, "steps": steps}


def page_names(content: dict) -> dict:
    """File names for every page, in the outputs/ naming convention."""
    names = {"home": f"{OUTPUT_PREFIX}_home.html", "items": {}, "lessons": {}}
    for item in content["items"]:
        token = item["slug"].replace("-", "_")
        names["items"][item["slug"]] = f"{OUTPUT_PREFIX}_item_{token}.html"
        for number, _ in enumerate(item["lessons"], start=1):
            names["lessons"][(item["slug"], number)] = f"{OUTPUT_PREFIX}_lesson_{token}_{number}.html"
    return names


def build_site(content: dict, template_dir: Path | str = TEMPLATE_DIR) -> dict[str, str]:
    """Validate the content and render every page. Returns {file name: html}."""
    data = validate(content)
    names = page_names(data)
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True,
                      undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    home_href = names["home"]
    for item in data["items"]:
        item["href"] = names["items"][item["slug"]]
        count = len(item["lessons"])
        for number, lesson in enumerate(item["lessons"], start=1):
            lesson["number"], lesson["count"] = number, count
            lesson["percent"] = round(100 * number / count)
            lesson["href"] = names["lessons"][(item["slug"], number)]
            lesson["prev_href"] = names["lessons"][(item["slug"], number - 1)] if number > 1 else None
            lesson["next_href"] = names["lessons"][(item["slug"], number + 1)] if number < count else None
    inside, seen = [], set()
    for item in data["items"]:
        for entry in item["inside"]:
            if entry["text"].lower() not in seen and len(inside) < 8:
                seen.add(entry["text"].lower())
                inside.append(entry)
    env.globals.update(site=data["site"], home_href=home_href, start_href=data["items"][0]["href"],
                       theme_css=theme_css(data["site"]["theme"]), logo=logo_svg(data["site"]["name"]), icon=icon_svg)
    pages = {home_href: env.get_template("home.html.j2").render(
        items=data["items"], faq=data["faq"], steps=data["steps"], inside=inside)}
    for item in data["items"]:
        pages[item["href"]] = env.get_template("item.html.j2").render(item=item)
        for lesson in item["lessons"]:
            pages[lesson["href"]] = env.get_template("lesson.html.j2").render(item=item, lesson=lesson)
    return pages


def _visible_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html)).strip()


def check_site(pages: dict[str, str]) -> list[str]:
    """Problems found in rendered pages. An empty list means the site passes."""
    problems = []
    for name, html in pages.items():
        if "Draft: facts not verified" not in html:
            problems.append(f"{name}: draft banner missing")
        if re.search(r"<script|\son\w+\s*=|javascript:", html, re.I):
            problems.append(f"{name}: script found")
        if re.search(r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//""", html, re.I):
            problems.append(f"{name}: links to an outside address")
        if re.search(r"""url\(\s*["']?\s*(?:https?:)?//|@import""", html, re.I):
            problems.append(f"{name}: style loads from an outside address")
        if re.search(r"<form|<input", html, re.I):
            problems.append(f"{name}: form found")
        for href in re.findall(r"""href\s*=\s*["']([^"'#]+)["']""", html):
            if href not in pages:
                problems.append(f"{name}: broken link to {href}")
        # Content-dependent checks: a section or a lesson that came out with no text.
        for section in re.findall(r"<section\b[^>]*>(.*?)</section>", html, re.S | re.I):
            heading = re.search(r"<h2\b[^>]*>(.*?)</h2>", section, re.S | re.I)
            label = _visible_text(heading.group(1)) if heading else "untitled"
            if not _visible_text(re.sub(r"<h2\b[^>]*>.*?</h2>", "", section, flags=re.S | re.I)):
                problems.append(f"{name}: empty section ({label})")
        if "_lesson_" in name:
            body = re.search(r'<article\b[^>]*class="prose"[^>]*>(.*?)</article>', html, re.S | re.I)
            if not body or not _visible_text(body.group(1)):
                problems.append(f"{name}: empty lesson")
    for name, html in pages.items():
        if "_item_" in name and "MOCKED" not in html:
            problems.append(f"{name}: MOCKED label missing on the buy control")
    return problems
