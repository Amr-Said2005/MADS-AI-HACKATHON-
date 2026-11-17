"""Build a small static website from fixed templates.

Used in the Build step (milestone 3). The model writes no HTML: it returns a
content object, code validates it, and code renders it into the templates in
site_templates/. That makes the step fast and keeps the pages safe:

- every page carries the draft banner;
- the buy control does nothing and is labelled MOCKED;
- the templates contain no testimonial, review, countdown or earnings block;
- all text is escaped, so content cannot inject markup or scripts.
"""
from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATE_DIR = Path(__file__).parent / "site_templates"
OUTPUT_PREFIX = "phase3_step4_site"

KIND_LABELS = {
    "notes": "Notes", "course": "Course", "templates": "Template pack",
    "guide": "Guide", "checklist": "Checklist",
}
MAX_ITEMS, MAX_LESSONS, MAX_FAQ = 6, 12, 8
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TAG_RE = re.compile(r"<[^>]{0,200}>")


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


def validate(content: dict) -> dict:
    """Check and normalise the content object. Raises SiteContentError."""
    if not isinstance(content, dict):
        raise SiteContentError("content must be an object")
    site_in = content.get("site") or {}
    site = {
        "name": _text(site_in.get("name"), "site.name", limit=80),
        "tagline": _text(site_in.get("tagline"), "site.tagline", limit=160),
        "audience": _text(site_in.get("audience"), "site.audience", limit=200),
        "about": _text(site_in.get("about"), "site.about", limit=600),
    }
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
            paragraphs = [_text(p, f"{lwhere}.paragraphs", limit=1500) for p in (lesson.get("paragraphs") or [])]
            if not paragraphs:
                raise SiteContentError(f"{lwhere}.paragraphs is missing")
            lessons.append({"title": _text(lesson.get("title"), f"{lwhere}.title", limit=120),
                            "paragraphs": paragraphs})
        items.append({
            "slug": slug, "kind": kind, "kind_label": KIND_LABELS[kind], "title": title,
            "summary": _text(raw.get("summary"), f"{where}.summary", limit=300),
            "for_whom": _text(raw.get("for_whom"), f"{where}.for_whom", limit=300),
            "whats_inside": inside, "lessons": lessons,
            "price_text": _price_text(raw.get("price_usd")),
        })
    faq_in = content.get("faq") or []
    if not isinstance(faq_in, list) or len(faq_in) > MAX_FAQ:
        raise SiteContentError(f"faq must hold at most {MAX_FAQ} entries")
    faq = [{"q": _text(e.get("q"), "faq.q", limit=200), "a": _text(e.get("a"), "faq.a", limit=500)}
           for e in faq_in if isinstance(e, dict)]
    return {"site": site, "items": items, "faq": faq}


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
            lesson["href"] = names["lessons"][(item["slug"], number)]
            lesson["prev_href"] = names["lessons"][(item["slug"], number - 1)] if number > 1 else None
            lesson["next_href"] = names["lessons"][(item["slug"], number + 1)] if number < count else None
    pages = {home_href: env.get_template("home.html.j2").render(
        site=data["site"], items=data["items"], faq=data["faq"], home_href=home_href)}
    for item in data["items"]:
        pages[item["href"]] = env.get_template("item.html.j2").render(
            site=data["site"], item=item, home_href=home_href)
        for lesson in item["lessons"]:
            pages[lesson["href"]] = env.get_template("lesson.html.j2").render(
                site=data["site"], item=item, lesson=lesson, home_href=home_href)
    return pages


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
        if re.search(r"<form|<input", html, re.I):
            problems.append(f"{name}: form found")
        for href in re.findall(r"""href\s*=\s*["']([^"'#]+)["']""", html):
            if href not in pages:
                problems.append(f"{name}: broken link to {href}")
    for name, html in pages.items():
        if "_item_" in name and "MOCKED" not in html:
            problems.append(f"{name}: MOCKED label missing on the buy control")
    return problems
