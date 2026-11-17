"""Three guardrail layers. Each call writes a decision log entry.

1. screen_brief   - before Phase 1: excluded product categories and founder fit.
2. screen_request - every founder instruction during a run.
3. check_output   - every generated file before it is saved.

Order of work in every layer:
- secrets (card numbers, passwords, API keys) are replaced with [REDACTED] before
  anything else sees the text, and nothing from the text is written to the log;
- the text is normalised (Unicode NFKC, invisible characters removed, lowercase) and
  each rule is matched against it and against a leetspeak-mapped copy;
- deterministic pattern rules decide first. Only a request that hits an "ambiguous"
  rule and no refuse rule goes to the optional model classifier, and anything other
  than a clear "allow" from it is refused (fail closed).

Framing does not change the outcome: role-play, "for testing" or a claimed approval
inside the instruction text do not unlock anything on the never-list. Founder approval
happens only through the approval step in the app, never through instruction text.
Borderline matches stop with the reason stated.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from decision_log import DecisionLog

AGENT = "guardrails"
REDACTED = "[REDACTED]"


def _rx(*words: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)


# ---------------------------------------------------------------- normalisation

# Zero-width and other invisible format characters, bidi controls, and Unicode tag characters.
_INVISIBLE = re.compile(
    "[­᠎​-‏‪-‮⁠-⁤⁦-⁩﻿\U000E0000-\U000E007F]"
)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_LEET_TOKEN = re.compile(r"[a-z0-9@$]+")
_HAS_LETTER = re.compile(r"[a-z]")
_HAS_LEET = re.compile(r"[013457@$]")


def _clean(text: str) -> str:
    """NFKC plus removal of invisible characters. Case is kept (used for stored text)."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text or ""))


def normalise_text(text: str) -> str:
    """The text every pattern rule is matched against: NFKC, no invisible characters, lowercase."""
    return _clean(text).lower()


def leet_variant(normalised: str) -> str:
    """Map leetspeak inside words that mix letters and digits/symbols ("fr33 r3v1ews" -> "free reviews").

    Tokens without any letter ("$9", "5,000", "3") are left alone, so prices and amounts keep
    their meaning; income and price patterns therefore still match on the normalised text.
    The mapping is one character for one character, so offsets line up with the input.
    """

    def fix(m: re.Match[str]) -> str:
        tok = m.group(0)
        if _HAS_LETTER.search(tok) and _HAS_LEET.search(tok):
            return tok.translate(_LEET)
        return tok

    return _LEET_TOKEN.sub(fix, normalised)


_SPLIT_WS = re.compile(r"(\s+)")
_ONE_LETTER = re.compile(r"[a-z]")
_MIN_SPACED_LETTERS = 4
# Words the refuse rules look for. When letters are spaced out with no word breaks left
# ("f a k e r e v i e w s"), these are cut back out of the joined letters.
_SPACED_WORDS = re.compile(
    r"(testimonials?|countdown|guaranteed?|impersonate|password|customers?|reviews?|ratings?|promise|pretend|"
    r"founder|refund|income|invent|write|timer|fake|make|five|star|earn)")


def unspaced_variant(normalised: str) -> str:
    """Join letters that were written one by one ("f a k e  r e v i e w s" -> "fake reviews").

    Only a run of at least four single letters is touched, so ordinary text stays as it is.
    A wider gap inside the run is kept as a word break; without one, known words are cut out.
    """
    parts = _SPLIT_WS.split(normalised)
    out: list[str] = []
    i = 0
    while i < len(parts):
        j, letters = i, 0
        while j < len(parts) and len(parts[j]) == 1 and _ONE_LETTER.fullmatch(parts[j]):
            letters += 1
            if j + 2 < len(parts) and len(parts[j + 2]) == 1 and _ONE_LETTER.fullmatch(parts[j + 2]):
                j += 2
            else:
                break
        if letters >= _MIN_SPACED_LETTERS:
            run = "".join(parts[k] if k % 2 == i % 2 else (" " if len(parts[k]) > 1 else "") for k in range(i, j + 1))
            if " " not in run:
                run = " ".join(_SPACED_WORDS.sub(r" \1 ", run).split())
            out.append(run)
            i = j + 1
        else:
            out.append(parts[i])
            i += 1
    return "".join(out)


def _variants(text: str) -> list[str]:
    norm = normalise_text(text)
    out = [norm]
    for variant in (leet_variant(norm), unspaced_variant(norm)):
        if variant not in out:
            out.append(variant)
    return out


# ---------------------------------------------------------------- secret redaction

_DIGIT_RUN = re.compile(r"(?<!\d)\d(?:[ -]?\d)*")
_SECRET_WORD = r"(?:password|passwd|passphrase|passcode|pass code|pwd|pin code|pin)"
_PASSWORD = re.compile(
    r"\b(" + _SECRET_WORD + r"(?:\s+is|\s*[:=]|\s+-)\s*)([\"'“‘]?)(\S+)", re.IGNORECASE
)
_PASSWORD_STOP = {"required", "needed", "optional", "stored", "saved", "not", "never", "a", "the", "is",
                  "missing", "wrong", "incorrect", "set", "reset", "changed", "protected", "manager"}
_API_KEYS = [
    re.compile(r"\bsk-(?:[A-Za-z0-9_-]{16,})"),
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
]
_KEY_ASSIGN = re.compile(
    r"\b((?:api[ _-]?key|secret[ _-]?key|access[ _-]?token|auth[ _-]?token|bearer)\b(?:\s+is|\s*[:=])?\s*)"
    r"([A-Za-z0-9_\-./+]{12,})",
    re.IGNORECASE,
)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Requirement R-NO-MONEY: card numbers, passwords and API keys are replaced with [REDACTED];
# validate_brief, screen_request and ApprovalStore.choose call this before text is logged or stored.
# Verified by: tests/test_safeguards.py::test_instructions_and_notes_never_store_secrets
def redact_secrets(text: str) -> tuple[str, list[str]]:
    """Replace card numbers, passwords and API keys with [REDACTED].

    Returns (redacted_text, kinds) where kinds lists each kind found once, in order
    ("card number", "password", "api key"). Invisible characters are removed first so a
    secret cannot hide behind zero-width characters.
    """
    out = _clean(text)
    kinds: list[str] = []

    def found(kind: str) -> None:
        if kind not in kinds:
            kinds.append(kind)

    def card(m: re.Match[str]) -> str:
        # Digit groups split by spaces or dashes; redact the longest Luhn-valid 13-19 digit
        # sequence of whole groups starting at each group ("4111 1111 1111 1111 2025" -> first 16).
        run = m.group(0)
        groups = list(re.finditer(r"\d+", run))
        if sum(len(g.group()) for g in groups) < 13:
            return run
        parts, pos, i = [], 0, 0
        while i < len(groups):
            best, digits = None, ""
            for j in range(i, len(groups)):
                digits += groups[j].group()
                if len(digits) > 19:
                    break
                if len(digits) >= 13 and _luhn_ok(digits):
                    best = j
            if best is None:
                i += 1
                continue
            found("card number")
            parts += [run[pos:groups[i].start()], REDACTED]
            pos, i = groups[best].end(), best + 1
        parts.append(run[pos:])
        return "".join(parts)

    out = _DIGIT_RUN.sub(card, out)

    def password(m: re.Match[str]) -> str:
        value = m.group(3)
        core = value.rstrip(".,;!?)\"'”’")
        if not core or core.lower() in _PASSWORD_STOP or core == REDACTED:
            return m.group(0)
        found("password")
        return m.group(1) + m.group(2) + REDACTED + value[len(core):]

    out = _PASSWORD.sub(password, out)

    def key(m: re.Match[str]) -> str:
        found("api key")
        return REDACTED

    for rx in _API_KEYS:
        out = rx.sub(key, out)

    def key_assign(m: re.Match[str]) -> str:
        found("api key")
        return m.group(1) + REDACTED

    out = _KEY_ASSIGN.sub(key_assign, out)
    return out, kinds


def _not_stored_note(kinds: list[str]) -> str:
    what = " and ".join(kinds)
    return (f"It contained a {what}; that part was replaced with {REDACTED} and was not stored. "
            "Never paste passwords, card numbers or keys into instructions.")


# ---------------------------------------------------------------- negation

_NEGATIONS = {"no", "not", "never", "without", "don't", "dont", "won't", "wont", "avoid", "avoids",
              "avoiding", "refuse", "refuses", "nor", "zero"}
# "don't worry, just ..." and "an AI without rules" still ask for the thing.
_NOT_A_NEGATION = {"worry", "hesitate", "forget", "mind", "problem", "problems", "worries", "matter",
                   "rules", "restrictions", "limits", "limitations", "filters", "guardrails", "censorship",
                   "safeguards", "boundaries", "apply", "applies", "count", "exist"}
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


def _hits(rx: re.Pattern[str], text: str):
    """Every match of rx in text that is not negated."""
    for m in rx.finditer(text):
        if not _negated(text, m.start()):
            yield m


def _find(rx: re.Pattern[str], text: str) -> re.Match[str] | None:
    """First match of rx in text that is not negated."""
    return next(_hits(rx, text), None)


def _find_any(rx: re.Pattern[str], variants: list[str]) -> re.Match[str] | None:
    """First non-negated match in the normalised text, else in its leetspeak variant."""
    for v in variants:
        m = _find(rx, v)
        if m:
            return m
    return None


# ---------------------------------------------------------------- 1. brief screen

EXCLUDED = {
    "physical goods": _rx(
        r"physical (?:goods?|products?)", r"t-?shirts?", r"merch(?:andise)?", r"print[- ]on[- ]demand",
        r"drop-?shipping", r"hardware", r"candles?", r"jewell?ery", r"cosmetics?", r"skincare",
        r"apparel", r"mugs?", r"stickers? shop", r"ship(?:ping)? (?:boxes|products)", r"handmade",
        r"food (?:products?|delivery)", r"bakery", r"3d[- ]printed",
        r"printed (?:planners?|workbooks?|books?|cop(?:y|ies)|journals?|notebooks?|posters?|cards?|calendars?)",
        r"paperbacks?", r"hardcovers?", r"physical cop(?:y|ies)",
        r"ship(?:s|ped|ping)? to (?:their|your|the buyer'?s?|the customer'?s?) (?:door|home|address)",
        r"(?:posted|mailed|delivered) to (?:their|your|the buyer'?s?|the customer'?s?) (?:door|home|address)",
    ),
    "financial, investment or crypto product": _rx(
        r"crypto(?:currency|currencies)?", r"bitcoin", r"ethereum", r"nfts?", r"tokens? sale", r"defi",
        r"forex", r"day[- ]?trad(?:e|ing)", r"trading (?:signals?|bot|course|strategy)", r"stocks? picks?",
        r"invest(?:ing|ment)? (?:advice|tips|signals|course|portfolio)", r"financial advice", r"loans?",
        r"credit repair", r"insurance", r"betting", r"gambling", r"get[- ]rich",
        r"stock[- ]?pick\w*", r"stock market", r"index funds?", r"mutual funds?", r"etfs?", r"dividends?",
        r"trading bots?", r"(?:automated|algorithmic|algo) trading", r"shares to buy",
    ),
    "health advice product": _rx(
        r"medical", r"diagnos(?:is|e|ing)", r"symptoms?", r"treatment plans?", r"supplements?",
        r"weight[- ]loss", r"diet plans?", r"meal plans? for (?:diabetes|weight)", r"therapy", r"mental health",
        r"cure", r"health advice", r"medication", r"detox",
        r"blood (?:pressure|sugar|glucose)", r"pills?", r"dos(?:e|es|age|ages|ing)", r"prescriptions?",
        r"diabet\w*", r"insulin", r"medicines?",
    ),
    "legal advice product": _rx(
        r"legal advice", r"lawyer", r"attorney", r"immigration advice", r"visa advice",
        r"(?:write|draft) (?:your )?(?:own )?wills?", r"divorce", r"tax advice", r"lawsuit",
        r"in court", r"court (?:cases?|hearings?|claims?|orders?)", r"small claims",
        r"(?:dispute|contest|appeal|fight)(?:s|ing)? (?:\w+ ){0,3}(?:fines?|tickets?|evictions?|charges?|penalt(?:y|ies))",
        r"evict\w*", r"su(?:e|ing) (?:your|a|an|the|my)", r"legal (?:templates?|documents?|forms?|letters?)",
        r"know your rights",
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
_PHYSICAL_CONTEXT = _rx(r"ship(?:s|ped|ping|ment)?", r"we print", r"print(?:ed)? and (?:mail|post|send|ship)(?:ed)?",
                        r"(?:post|mail)(?:ed)? to (?:buyers|customers)", r"warehouse", r"inventory")

BRIEF_KEYS = ("idea", "skills", "budget_usd", "hours_per_week", "target_market", "values")
_BRIEF_TEXT_KEYS = ("idea", "target_market")
_BRIEF_LIST_KEYS = ("skills", "values")


class BriefError(ValueError):
    pass


def validate_brief(brief: object) -> dict:
    """Check the brief's shape and redact secrets from its text fields.

    The text fields (idea, target_market, skills, values) are redacted in place and the same
    dict is returned, so a caller that keeps its own reference does not keep a secret either.
    """
    if not isinstance(brief, dict):
        raise BriefError("the founder brief must be a JSON object")
    missing = [k for k in BRIEF_KEYS if k not in brief]
    if missing:
        raise BriefError(f"the founder brief is missing: {', '.join(missing)} (required: {', '.join(BRIEF_KEYS)})")
    problems = []
    if not isinstance(brief["idea"], str) or not brief["idea"].strip():
        problems.append("idea must be a non-empty string")
    for key in _BRIEF_LIST_KEYS:
        if not isinstance(brief[key], list) or not all(isinstance(x, str) for x in brief[key]):
            problems.append(f"{key} must be a list of strings")
    for key in ("budget_usd", "hours_per_week"):
        if isinstance(brief[key], bool) or not isinstance(brief[key], (int, float)):
            problems.append(f"{key} must be a number")
    if not isinstance(brief["target_market"], str) or not brief["target_market"].strip():
        problems.append("target_market must be a non-empty string")
    if problems:
        raise BriefError("; ".join(problems))
    for key in _BRIEF_TEXT_KEYS:
        brief[key] = redact_secrets(brief[key])[0]
    for key in _BRIEF_LIST_KEYS:
        brief[key] = [redact_secrets(x)[0] for x in brief[key]]
    return brief


@dataclass
class ScreenResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    category: str | None = None


def _brief_text(brief: dict) -> str:
    return " ".join([brief["idea"], brief["target_market"], " ".join(brief["skills"]), " ".join(brief["values"])])


# Requirement R-EXCLUDED: a brief that matches an excluded category (physical goods; financial,
# investment or crypto; health advice; legal advice) or a borderline one is not allowed and logs stop.
# Verified by: tests/test_guardrails.py::test_brief_screen_stops_excluded_categories
def screen_brief(brief: dict, log: DecisionLog) -> ScreenResult:
    brief = validate_brief(brief)
    variants = _variants(_brief_text(brief))
    reasons: list[str] = []
    category = None
    for cat, rx in EXCLUDED.items():
        m = _find_any(rx, variants)
        if m:
            category = cat
            reasons.append(f"excluded category: {cat} (matched '{m.group(0)}')")
    if category is None:
        for cat, rx in BORDERLINE.items():
            m = _find_any(rx, variants)
            if not m:
                continue
            if cat == "may be a physical product" and not any(_PHYSICAL_CONTEXT.search(v) for v in variants):
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

# Deterministic refuse rules. Checked in this order; the first hit names the rule.
# Requirement R-NO-IMPERSONATION: the "impersonation" rule matches an instruction to write, post
# or reply as the founder, a customer or another brand; screen_request refuses every match.
# Verified by: tests/test_guardrails.py::test_every_trick_prompt_hits_its_labelled_rule
REQUEST_RULES = {
    "fake reviews or testimonials": _rx(
        r"(?:fake|invent|make up|made[- ]up|fabricate|generate|write|draft|compose|produce) (?:\w+ ){0,3}(?:reviews?|testimonials?|ratings?|endorsements?|quotes? from (?:\w+ ){0,2}(?:customers|users|buyers|clients))",
        r"(?:reviews?|testimonials?) (?:from|by) (?:happy |real-sounding |satisfied )?(?:customers|users|buyers) (?:we|that) (?:don't|do not) have",
        r"five[- ]star reviews?", r"5[- ]star reviews?",
        # A few common phrasings in Arabic, French, Spanish and German. Other languages are not covered.
        r"(?:خمس|5|٥) ?نجوم",
        r"(?:تقييم|تقييمات|مراجعة|مراجعات|شهادة|شهادات|آراء|اراء) (?:\w+ ){0,3}(?:وهمي|وهمية|مزيف|مزيفة|مزيفه|مفبرك|مفبركة|كاذب|كاذبة)",
        r"(?:faux|fausses?) (?:avis|témoignages?|évaluations?)", r"(?:cinq|5) étoiles",
        r"reseñas? falsas?", r"testimonios? falsos?", r"(?:cinco|5) estrellas",
        r"(?:gefälschte|falsche|erfundene) (?:bewertungen|rezensionen)", r"(?:fünf|5)[- ]sterne[- ]bewertungen",
    ),
    "fake sign-ups or social proof": _rx(
        r"(?:fake|inflate|pad|invent|bump up|make up) (?:\w+ ){0,3}(?:sign-?ups?|signups|subscribers?|waitlist|followers|users|numbers|metrics)",
        r"(?:say|claim|show) (?:that )?(?:\d[\d,]* |thousands of |hundreds of )(?:people|users|customers) (?:have )?(?:signed up|joined|bought)",
        r"(?:counter|ticker|tally) of (?:\w+ ){0,3}(?:people|buyers|customers|visitors|sales|sign-?ups|orders)",
        r"start (?:it|the counter|the count) at \d+",
        r"(?:pop-?ups?|notifications?|toasts?|banners?) (?:\w+ ){0,4}(?:someone|somebody|a customer|people) (?:\w+ ){0,4}(?:bought|purchased|signed up|joined)",
        r"buy (?:\w+ ){0,2}(?:followers|likes|subscribers)",
    ),
    "countdown timers or false urgency": _rx(
        r"countdown(?: timer)?", r"fake (?:scarcity|urgency|deadline)", r"only \d+ (?:left|spots|copies)",
        r"(?:offer|sale|price) (?:ends|expires) (?:in|tonight|soon)", r"limited[- ]time (?:offer|only)",
    ),
    "income or success promises": _rx(
        r"(?:promise|guarantee)s? (?:\w+ ){0,4}(?:income|earnings|revenue|results|success|profit|money)",
        r"(?:make|earn) \$?\d[\d,]*k?(?: dollars)? (?:a|per|every) (?:day|week|month)",
        r"guaranteed (?:income|results|returns|success)", r"passive income guaranteed",
        r"(?:will|can|going to|to) make (?:them|you|buyers|readers|customers|people|anyone) (?:rich|wealthy|a fortune|money)",
        r"pays? for (?:itself|themselves)", r"quit (?:your|their|the) (?:day )?jobs?", r"financial freedom",
        r"(?:six|6|seven|7)[- ]figures?",
    ),
    "impersonation": _rx(
        r"(?:pretend(?:ing)?|pos(?:e|ing)|act(?:ing)?|sign(?:ing)?|post(?:ing)?|repl(?:y|ying)|writ(?:e|ing)|answer(?:ing)?|respond(?:ing)?|comment(?:ing)?|messag(?:e|ing)|email(?:ing)?) (?:\w+ ){0,4}(?:as|to be) (?:the founder|me|an? (?:(?:happy|satisfied|real|paying|loyal|verified|delighted|past|former|existing) )?(?:customer|user|buyer|client|reader)(?! (?:guide|manual|service|support|story|journey|persona|profile|research|interview|survey|faq|handbook))|\w+ (?:inc|corp|ltd))",
        r"as (?:if|though) (?:it|this|they) (?:came|comes?|was|were|is) (?:sent |written |posted )?(?:from|by)",
        r"(?:look|seem|appear)s? (?:like|as if|as though) (?:it|this|they) (?:comes?|came|is|was|were) (?:sent |written |posted )?(?:from|by)",
        r"without (?:telling|disclosing|saying|revealing|mentioning) (?:\w+ ){0,3}(?:it is|it's|that it is|it was|this is) (?:automated|ai|a bot|generated)",
        r"impersonat\w*", r"posing as", r"from (?:the )?(?:official )?(?:notion|gumroad|etsy|google|apple|amazon) team",
        r"use (?:their|another|a competitor'?s?) (?:brand|logo|name)",
    ),
    "moving money": _rx(
        r"(?:send|move|transfer|wire|withdraw|pay out|refund) (?:\w+ ){0,4}(?:money|funds|payment|payout|\$\d+|dollars|the customer|to my account)",
        r"(?:charge|bill) (?:the )?(?:customer|card|user)s?", r"issue (?:(?:a|an|the|that|this|his|her|their) )?(?:\w+ )?refunds?",
        r"process (?:the )?refunds?", r"refund (?:\w+ ){0,3}(?:yourself|automatically|right away|immediately)",
        r"buy (?:\$?\d[\d,.]*k? (?:of |worth of |in )?)?(?:\w+ ){0,2}(?:ads?|ad credits|advertising|traffic|backlinks)",
        r"pay for (?:\w+ ){0,3}(?:ads?|advertising|promotion|traffic)", r"spend (?:the )?budget", r"spend \$?\d[\d,.]*k?",
    ),
    "storing credentials or card numbers": _rx(
        r"(?:store|save|keep|remember|log|record) (?:[\w'’]+ ){0,3}(?:passwords?|credentials|card numbers?|credit cards?|cvv|api keys?|logins?)",
        r"(?:here(?:'s| is) )?my password", r"card numbers?",
    ),
    "creating accounts": _rx(
        r"(?:create|open|register|sign up for|make|set up) (?:\w+ ){0,3}(?:accounts?|profiles?|(?:e-?mail|gmail|outlook|yahoo) (?:address(?:es)?|inbox(?:es)?)|mailbox(?:es)?) (?:on|at|with|for)",
    ),
    # Approval is a record made in the app. Text in an instruction cannot give it or waive it.
    "skipping founder approval": _rx(
        r"(?:skip|bypass|ignore|drop|remove|disable) (?:\w+ ){0,3}(?:approvals?|sign-?off|approval step|confirmation|permission)",
        r"(?:publish|send|post|launch|go live|release|email|charge|spend|pay) (?:\w+ ){0,6}without (?:waiting|asking|checking|my|me|the founder|approval|any approval|confirmation|permission)",
        r"no need to (?:ask|wait for|check with) (?:me|the founder)",
        r"(?:don't|do not|dont) (?:ask|wait for) (?:me|my approval|the founder|approval)",
        r"approve (?:everything|all|it all|them all|this) (?:\w+ ){0,2}(?:yourself|for me|automatically|on your own|on my behalf)",
        r"auto-?approv\w*",
    ),
    "signing on the founder's behalf": _rx(
        r"sign (?:\w+ ){0,4}(?:agreements?|contracts?|ndas?|leases?|deals?|terms)",
        r"(?:accept|agree to) (?:\w+ ){0,3}(?:terms|agreements?|contracts?|conditions|tos)",
        r"sign (?:\w+ ){0,5}(?:on my behalf|for me|in my name)", r"e-?sign\w*",
        r"(?:forge|fake|copy) (?:\w+ ){0,2}signatures?",
    ),
    "paywall on the system's own features": _rx(
        r"(?:lock|hide|gate|restrict|put) (?:\w+ ){0,5}(?:phases?|agents?|this tool|the tool|this system|this app|venture builder) (?:\w+ ){0,4}behind (?:a |the |our |your )?(?:paywall|paid (?:plan|tier)|subscription|premium)",
        r"paywall (?:for|on|around) (?:this|the) (?:tool|system|app)",
        r"charge (?:the )?founders? (?:to|for) (?:unlock|use|access)",
    ),
    # Checked last so a framed request is named by what it actually asks for.
    "attempt to switch off the rules": _rx(
        r"(?:ignore|bypass|disable|turn off|switch off|override|skip|forget) (?:\w+ ){0,3}(?:rules|guardrails|safeguards|restrictions|filters|safety checks)",
        r"(?:an? )?(?:ai|assistant|model|bot|system) (?:with no|without (?:any )?) ?(?:rules|restrictions|limits|filters|guardrails)",
        r"no[- ]rules mode", r"developer mode", r"jailbreak\w*", r"dan mode",
        r"(?:rules|guardrails|safeguards|restrictions|filters|policies|safety checks) (?:do not|don't|dont|no longer|does not|doesn't|won't) apply",
        r"pretend (?:\w+ ){0,4}(?:no|without) (?:rules|restrictions|limits)",
    ),
}

# Requests that touch a never-list topic without clearly asking for the forbidden thing.
# They are not refused by pattern alone: the classifier decides, and no clear "allow" means refuse.
AMBIGUOUS_RULES = {
    "reviews or testimonials": _rx(
        r"reviews", r"(?:customer|user|buyer|product|star|5-star|five-star) reviews?", r"testimonials?",
        r"star ratings?", r"ratings", r"endorsements?",
        r"quotes? from (?:\w+ ){0,2}(?:customers|users|buyers|clients)", r"(?:customer|buyer|user|client) quotes?",
    ),
    "social proof": _rx(
        r"social proof", r"trusted by", r"(?:sign-?ups?|signups|subscribers?|waitlist) (?:numbers?|counts?|figures?)",
        r"(?:\d[\d,]*|thousands|hundreds) of (?:happy )?(?:customers|users|buyers)",
    ),
    "scarcity or urgency": _rx(
        r"scarcity", r"urgency", r"fomo", r"sell(?:s|ing)? out", r"running out", r"last chance", r"hurry",
        r"limited (?:stock|spots|seats|copies|availability|time)",
    ),
    "earnings or results claims": _rx(
        r"(?:real|proven|amazing|fast|quick|instant|life[- ]changing|massive) results", r"before (?:and|&) after",
        r"success stor(?:y|ies)", r"(?:income|earnings) (?:claims?|figures?|potential|screenshots?|proof)",
        r"how much (?:buyers|customers|users|they|you) (?:can|could|will) (?:make|earn)",
        r"(?:make|earn) (?:money|income|a living)", r"passive income", r"side income",
    ),
}

# Clearly legitimate uses of an ambiguous topic. An ambiguous match that overlaps one of these is fine.
LEGITIMATE = _rx(
    r"(?:collect|collecting|gather|gathering|request|requesting|ask for|asking for|invite|inviting) (?:\w+ ){0,2}(?:reviews?|feedback|testimonials?|ratings?)",
    r"ask (?:\w+ ){0,2}(?:buyers|customers|users) (?:\w+ ){0,3}(?:reviews?|feedback|ratings?)",
    r"(?:honest|real|genuine|verified|actual) (?:\w+ ){0,1}(?:reviews?|testimonials?|ratings?|feedback)",
    r"(?:reviews?|testimonials?|ratings?) (?:from|by) (?:real|actual|verified) (?:buyers|customers|users)",
    r"(?:reviews?|ratings?) (?:after|once) (?:launch|purchase|they buy)",
    r"results (?:may )?vary", r"no (?:income|earnings) claims?", r"code reviews?", r"review (?:the|my|our|this)",
)

Classifier = Callable[[str], str]


@dataclass
class RequestScreen:
    refused: bool
    rule: str | None = None
    reason: str = ""
    redacted_text: str = ""
    redactions: list[str] = field(default_factory=list)
    ambiguous: bool = False


def _legitimate(m: re.Match[str], text: str) -> bool:
    return any(lm.start() < m.end() and m.start() < lm.end() for lm in LEGITIMATE.finditer(text))


def _ambiguous_hit(variants: list[str]) -> tuple[str, re.Match[str]] | None:
    for rule, rx in AMBIGUOUS_RULES.items():
        for v in variants:
            for m in _hits(rx, v):
                if not _legitimate(m, v):
                    return rule, m
    return None


# Requirement R-NO-FAKE-PROOF: an instruction asking for invented reviews, testimonials, sign-up
# numbers or a countdown timer is refused and logged, also when it claims approval or a test.
# Verified by: tests/test_guardrails.py::test_every_trick_prompt_is_refused_and_logged
def screen_request(text: str, log: DecisionLog, classifier: Classifier | None = None) -> RequestScreen:
    """Screen one founder instruction. Never raises for a refusal; only the offending request is blocked.

    `classifier(text) -> "allow" | "refuse"` is consulted only for ambiguous requests and is given the
    redacted text. If it is missing, raises, or returns anything but exactly "allow", the request is refused.
    """
    redacted, kinds = redact_secrets(text)
    note = (" " + _not_stored_note(kinds)) if kinds else ""
    variants = _variants(redacted)

    def done(refused: bool, rule: str | None, reason: str, ambiguous: bool = False) -> RequestScreen:
        log.record(AGENT, "screen_request", outcome="refused" if refused else "allowed")
        return RequestScreen(refused, rule, reason, redacted, list(kinds), ambiguous)

    for rule, rx in REQUEST_RULES.items():
        m = _find_any(rx, variants)
        if m:
            return done(True, rule, f"refused: {rule} (matched '{m.group(0)}').{note}")
    for cat, rx in EXCLUDED.items():
        m = _find_any(rx, variants)
        if m:
            return done(True, f"excluded category: {cat}",
                        f"refused: would steer toward {cat} (matched '{m.group(0)}').{note}")
    if kinds:
        return done(True, "storing credentials or card numbers", f"refused: secrets are never kept.{note}")

    hit = _ambiguous_hit(variants)
    if hit:
        rule, m = hit
        verdict = None
        try:
            verdict = classifier(redacted) if classifier is not None else None
        except Exception:  # noqa: BLE001 - any classifier failure fails closed
            verdict = None
        if isinstance(verdict, str) and verdict == "allow":
            return done(False, None, "allowed", ambiguous=True)
        why = ("no classifier was available" if classifier is None
               else "the classifier did not clearly allow it")
        return done(True, f"ambiguous: {rule}",
                    f"refused to be safe: the request touches {rule} (matched '{m.group(0)}') and {why}. "
                    "Rephrase it to say exactly what you want, for example collecting honest reviews from real buyers.",
                    ambiguous=True)
    return done(False, None, "allowed")


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
    "customer voice": _rx(
        r"as an? (?:happy|satisfied|delighted|loyal|long[- ]?time|real|verified|paying|actual) (?:customer|user|buyer|client)",
        r"i (?:bought|purchased) (?:this|it|the \w+|these|your \w+)(?: \w+){0,3} and",
        r"(?:this|the|your) (?:\w+ ){0,2}(?:product|template|guide|pack|course|kit|tool|system|app|bundle) (?:has |really |totally )?(?:changed|transformed|saved) my (?:life|business|career|workflow|year)",
        r"changed my life", r"life[- ]changing purchase",
        r"i was (?:so |really |a bit |very )?(?:skeptical|sceptical|doubtful|hesitant|unsure) (?:at first )?(?:but|until)",
        r"i (?:can't|cannot|couldn't|can not) (?:recommend|thank) (?:it|this|them|you)(?: \w+)? enough",
        r"best purchase (?:i've|i have|i) ever made", r"since i (?:started using|bought|purchased) (?:this|it)",
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


# Generated text a customer would read. Nothing checks its facts, so each of these files must say so
# at the top, and the output check blocks one that does not.
DRAFT_HEADER = "Draft: facts not verified. Founder must review before use."
DRAFT_FILES = ("phase3_step2_product", "phase3_step3_user_docs", "phase5_step1_store_page", "phase5_step2_launch_posts")
_DRAFT_HEAD_CHARS = 400


def needs_draft_header(name: str) -> bool:
    return name.startswith(DRAFT_FILES)


# Requirement R-DRAFT-FACTS: the product, user docs, store page and launch posts open with the draft
# header; the output check blocks a file that lacks it, so that file is not written.
# Verified by: tests/test_safeguards.py::test_a_generated_product_file_without_the_draft_header_is_blocked
def has_draft_header(text: str) -> bool:
    """True when the header stands at the top of the file: within the first visible characters,
    before the generated text (a short header written by code may come first)."""
    body = re.split(r"<body[^>]*>", text, maxsplit=1, flags=re.IGNORECASE)[-1]
    visible = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    return DRAFT_HEADER in visible[:_DRAFT_HEAD_CHARS]


# Requirement R-NO-INCOME-CLAIMS: generated text that matches the "income guarantee" rule, such as
# a guaranteed income or "you will earn", is returned as not ok and logged as blocked.
# Verified by: tests/test_guardrails.py::test_output_check_blocks_and_passes
def check_output(name: str, text: str, log: DecisionLog) -> OutputCheck:
    variants = _variants(text)
    hits = []
    if needs_draft_header(name) and not has_draft_header(text):
        hits.append(f"missing draft header: '{DRAFT_HEADER}' must stand at the top of {name}")
    for rule, rx in OUTPUT_RULES.items():
        m = _find_any(rx, variants)
        if m:
            hits.append(f"{rule}: '{redact_secrets(m.group(0))[0][:80]}'")
    log.record(AGENT, f"check_output:{name}", outcome="pass" if not hits else "blocked")
    return OutputCheck(not hits, hits)
