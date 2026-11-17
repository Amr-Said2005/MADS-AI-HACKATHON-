"""Three guardrail layers. Each call writes a decision log entry.

1. screen_brief   - before Phase 1: excluded product categories and founder fit.
2. screen_request - every founder instruction during a run.
3. check_output   - every generated file before it is saved.

These are deterministic pattern checks: they do not depend on a model and they
err on the side of stopping. Borderline matches stop with the reason stated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from decision_log import DecisionLog

AGENT = "guardrails"


def _rx(*words: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)


_NEGATIONS = {"no", "not", "never", "without", "don't", "dont", "won't", "wont", "avoid", "avoids",
              "avoiding", "refuse", "refuses", "nor", "zero"}
_NOT_A_NEGATION = {"worry", "hesitate", "forget", "mind"}  # "don't worry, just ..." still asks for it
_WORD = re.compile(r"[A-Za-z'’]+")


_CLAUSE_BREAK = re.compile(r"[.;:!?,\n]|\b(?:but|just|instead|then|and then)\b", re.IGNORECASE)


def _negated(text: str, start: int) -> bool:
    """True when a negation in the same clause, up to six words back, governs the match
    ("we never use fake reviews or countdown timers"). "Don't worry, just add a timer" is not negated."""
    before = text[max(0, start - 80):start]
    breaks = list(_CLAUSE_BREAK.finditer(before))
    clause = before[breaks[-1].end():] if breaks else before
    words = [w.lower().replace("’", "'") for w in _WORD.findall(clause)][-6:]
    for i, w in enumerate(words):
        if w in _NEGATIONS:
            after = words[i + 1] if i + 1 < len(words) else ""
            if after not in _NOT_A_NEGATION:
                return True
    return False


def _find(rx: re.Pattern[str], text: str) -> re.Match[str] | None:
    """First match of rx in text that is not negated."""
    for m in rx.finditer(text):
        if not _negated(text, m.start()):
            return m
    return None


# ---------------------------------------------------------------- 1. brief screen

EXCLUDED = {
    "physical goods": _rx(
        r"physical (?:goods?|products?)", r"t-?shirts?", r"merch(?:andise)?", r"print[- ]on[- ]demand",
        r"drop-?shipping", r"hardware", r"candles?", r"jewell?ery", r"cosmetics?", r"skincare",
        r"apparel", r"mugs?", r"stickers? shop", r"ship(?:ping)? (?:boxes|products)", r"handmade",
        r"food (?:products?|delivery)", r"bakery", r"3d[- ]printed",
    ),
    "financial, investment or crypto product": _rx(
        r"crypto(?:currency|currencies)?", r"bitcoin", r"ethereum", r"nfts?", r"tokens? sale", r"defi",
        r"forex", r"day[- ]?trad(?:e|ing)", r"trading (?:signals?|bot|course|strategy)", r"stocks? picks?",
        r"invest(?:ing|ment)? (?:advice|tips|signals|course|portfolio)", r"financial advice", r"loans?",
        r"credit repair", r"insurance", r"betting", r"gambling", r"get[- ]rich",
    ),
    "health advice product": _rx(
        r"medical", r"diagnos(?:is|e|ing)", r"symptoms?", r"treatment plans?", r"supplements?",
        r"weight[- ]loss", r"diet plans?", r"meal plans? for (?:diabetes|weight)", r"therapy", r"mental health",
        r"cure", r"health advice", r"medication", r"detox",
    ),
    "legal advice product": _rx(
        r"legal advice", r"lawyer", r"attorney", r"immigration advice", r"visa advice",
        r"(?:write|draft) (?:your )?(?:own )?wills?", r"divorce", r"tax advice", r"lawsuit",
    ),
}

BORDERLINE = {
    "may be a health product": _rx(r"fitness", r"nutrition", r"wellness", r"workout", r"sleep", r"anxiety"),
    "may be a financial product": _rx(r"budget(?:ing)? for", r"personal finance", r"retirement", r"savings? plan"),
    "may be legal advice": _rx(r"contracts?", r"legal", r"compliance", r"gdpr"),
    "may be a physical product": _rx(r"printable", r"planner", r"kit"),
}
# "printable" and "planner" are usually digital; they are borderline only when the
# brief also mentions shipping or printing for the customer.
_PHYSICAL_CONTEXT = _rx(r"ship", r"shipping", r"we print", r"printed and mailed", r"warehouse")

BRIEF_KEYS = ("idea", "skills", "budget_usd", "hours_per_week", "target_market", "values")


class BriefError(ValueError):
    pass


def validate_brief(brief: object) -> dict:
    if not isinstance(brief, dict):
        raise BriefError("the founder brief must be a JSON object")
    missing = [k for k in BRIEF_KEYS if k not in brief]
    if missing:
        raise BriefError(f"the founder brief is missing: {', '.join(missing)} (required: {', '.join(BRIEF_KEYS)})")
    problems = []
    if not isinstance(brief["idea"], str) or not brief["idea"].strip():
        problems.append("idea must be a non-empty string")
    for key in ("skills", "values"):
        if not isinstance(brief[key], list) or not all(isinstance(x, str) for x in brief[key]):
            problems.append(f"{key} must be a list of strings")
    for key in ("budget_usd", "hours_per_week"):
        if isinstance(brief[key], bool) or not isinstance(brief[key], (int, float)):
            problems.append(f"{key} must be a number")
    if not isinstance(brief["target_market"], str) or not brief["target_market"].strip():
        problems.append("target_market must be a non-empty string")
    if problems:
        raise BriefError("; ".join(problems))
    return brief


@dataclass
class ScreenResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    category: str | None = None


def _brief_text(brief: dict) -> str:
    return " ".join([brief["idea"], brief["target_market"], " ".join(brief["skills"]), " ".join(brief["values"])])


def screen_brief(brief: dict, log: DecisionLog) -> ScreenResult:
    validate_brief(brief)
    text = _brief_text(brief)
    reasons: list[str] = []
    category = None
    for cat, rx in EXCLUDED.items():
        m = _find(rx, text)
        if m:
            category = cat
            reasons.append(f"excluded category: {cat} (matched '{m.group(0)}')")
    if category is None:
        for cat, rx in BORDERLINE.items():
            m = _find(rx, text)
            if not m:
                continue
            if cat == "may be a physical product" and not _PHYSICAL_CONTEXT.search(text):
                continue
            category = "borderline"
            reasons.append(f"borderline: {cat} (matched '{m.group(0)}'); stopping rather than guessing")
    if brief["hours_per_week"] <= 0:
        reasons.append("founder fit: the brief gives no weekly hours to build or run the product")
    if brief["budget_usd"] < 0:
        reasons.append("founder fit: budget cannot be negative")
    if not brief["skills"]:
        reasons.append("founder fit: the brief lists no skills")
    allowed = not reasons
    if allowed:
        reasons = ["no excluded category matched; founder has hours, budget and at least one skill"]
    log.record(AGENT, "screen_brief", outcome="go" if allowed else "stop")
    return ScreenResult(allowed, reasons, category)


# ---------------------------------------------------------------- 2. request screen

REQUEST_RULES = {
    "fake reviews or testimonials": _rx(
        r"(?:fake|invent|make up|made[- ]up|fabricate|generate|write) (?:\w+ ){0,3}(?:reviews?|testimonials?|ratings?|quotes? from (?:customers|users))",
        r"(?:reviews?|testimonials?) (?:from|by) (?:happy |real-sounding |satisfied )?(?:customers|users|buyers) (?:we|that) (?:don't|do not) have",
        r"five[- ]star reviews?", r"5[- ]star reviews?",
    ),
    "fake sign-ups or social proof": _rx(
        r"(?:fake|inflate|pad|invent|bump up|make up) (?:\w+ ){0,3}(?:sign-?ups?|signups|subscribers?|waitlist|followers|users|numbers|metrics)",
        r"(?:say|claim|show) (?:that )?(?:\d[\d,]* |thousands of |hundreds of )(?:people|users|customers) (?:have )?(?:signed up|joined|bought)",
    ),
    "countdown timers or false urgency": _rx(
        r"countdown(?: timer)?", r"fake (?:scarcity|urgency|deadline)", r"only \d+ (?:left|spots|copies)",
        r"(?:offer|sale|price) (?:ends|expires) (?:in|tonight|soon)", r"limited[- ]time (?:offer|only)",
    ),
    "income or success promises": _rx(
        r"(?:promise|guarantee)s? (?:\w+ ){0,4}(?:income|earnings|revenue|results|success|profit|money)",
        r"(?:make|earn) \$?\d[\d,]*k?(?: dollars)? (?:a|per|every) (?:day|week|month)",
        r"guaranteed (?:income|results|returns|success)", r"passive income guaranteed",
    ),
    "impersonation": _rx(
        r"(?:pretend(?:ing)?|pos(?:e|ing)|act(?:ing)?|sign(?:ing)?|post(?:ing)?|repl(?:y|ying)|writ(?:e|ing)) (?:\w+ ){0,4}(?:as|to be) (?:the founder|me|a customer|a user|an? (?:real )?buyer|\w+ (?:inc|corp|ltd))",
        r"impersonat\w*", r"posing as", r"from (?:the )?(?:official )?(?:notion|gumroad|etsy|google|apple|amazon) team",
        r"use (?:their|another|a competitor'?s?) (?:brand|logo|name)",
    ),
    "moving money": _rx(
        r"(?:send|move|transfer|wire|withdraw|pay out|refund) (?:\w+ ){0,4}(?:money|funds|payment|payout|\$\d+|dollars|the customer|to my account)",
        r"(?:charge|bill) (?:the )?(?:customer|card|user)s?", r"issue (?:a )?refunds?", r"process (?:the )?refunds?",
        r"buy (?:ads|ad credits)", r"spend (?:the )?budget",
    ),
    "storing credentials or card numbers": _rx(
        r"(?:store|save|keep|remember|log|record) (?:[\w'’]+ ){0,3}(?:passwords?|credentials|card numbers?|credit cards?|cvv|api keys?|logins?)",
        r"(?:here(?:'s| is) )?my password", r"card numbers?",
    ),
    "creating accounts": _rx(
        r"(?:create|open|register|sign up for|make) (?:\w+ ){0,3}(?:accounts?|profiles?) (?:on|at|with|for)",
    ),
}


@dataclass
class RequestScreen:
    refused: bool
    rule: str | None = None
    reason: str = ""


def screen_request(text: str, log: DecisionLog) -> RequestScreen:
    for rule, rx in REQUEST_RULES.items():
        m = _find(rx, text)
        if m:
            log.record(AGENT, "screen_request", outcome="refused")
            return RequestScreen(True, rule, f"refused: {rule} (matched '{m.group(0)}')")
    for cat, rx in EXCLUDED.items():
        m = _find(rx, text)
        if m:
            log.record(AGENT, "screen_request", outcome="refused")
            return RequestScreen(True, f"excluded category: {cat}", f"refused: would steer toward {cat} (matched '{m.group(0)}')")
    log.record(AGENT, "screen_request", outcome="allowed")
    return RequestScreen(False, None, "allowed")


# ---------------------------------------------------------------- 3. output check

OUTPUT_RULES = {
    "income guarantee": _rx(
        r"guaranteed? (?:\w+ ){0,3}(?:income|earnings|results|returns|success|profit)",
        r"(?:make|earn) \$?\d[\d,]*k?(?: dollars)? (?:a|per|every|in a) (?:day|week|month|year)",
        r"you will (?:make|earn|be (?:rich|successful))", r"risk[- ]free (?:income|profit|returns)",
        r"quit your (?:day )?job",
    ),
    "invented testimonial": re.compile(
        r"(?:^#+\s*(?:testimonials?|reviews|what (?:our )?(?:customers|users|buyers) say)\b"
        r"|\bverified (?:buyer|customer|purchase)\b"
        r"|\bwhat (?:our )?(?:customers|users|buyers) (?:are )?saying\b"
        r"|[\"“][^\"”\n]{10,300}[\"”]\s*[—–-]{1,2}\s*[A-Z][a-z]+(?:\s[A-Z]\.?)?"
        r"|[★⭐]{3,})",
        re.IGNORECASE | re.MULTILINE,
    ),
    "urgency trick": _rx(
        r"countdown", r"only \d+ (?:left|spots|copies|seats)", r"(?:offer|sale|price|deal) (?:ends|expires) (?:in|tonight|today|soon)",
        r"limited[- ]time", r"act now", r"hurry", r"last chance", r"before it'?s gone", r"\d+ people are viewing",
    ),
}


@dataclass
class OutputCheck:
    ok: bool
    hits: list[str] = field(default_factory=list)


def check_output(name: str, text: str, log: DecisionLog) -> OutputCheck:
    hits = []
    for rule, rx in OUTPUT_RULES.items():
        m = _find(rx, text)
        if m:
            hits.append(f"{rule}: '{m.group(0)[:80]}'")
    log.record(AGENT, f"check_output:{name}", outcome="pass" if not hits else "blocked")
    return OutputCheck(not hits, hits)
