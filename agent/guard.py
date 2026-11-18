"""Deterministic check of every outgoing text. The model writes; this decides whether it may be sent.

fix() repairs style slips (long dashes, curly quotes, emoji). check() returns the facts in a text that are not
backed by the FactStore or by what the manager or owner wrote. Anything it returns blocks the text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from agent.facts import FactStore

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "a couple": 2, "couple": 2}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS)} | {"sept": 9}

# capitalised words that are not names: allowed anywhere
ALWAYS_OK = {"I", "AI", "OK", "London", "English", "British", "Telegram", "Mr", "Mrs", "Ms", "Sir", "Madam", "GMT", "BST"}
ALWAYS_OK |= {w.capitalize() for w in WEEKDAYS + MONTHS}
# words that commonly open a sentence; at a sentence start they are not taken as names
SENTENCE_OK = set("""
A About Absolutely Actually After Afternoon Again Ah Alas All Also Alright Although Always Am An And Another Any
Anything Apologies Appreciate Are Aren As At Awesome Back Be Because Before Best Both Brilliant But By Can Cannot
Cheers Could Couldn Dear Delighted Did Didn Do Does Doesn Don Done Each Either Enjoy Especially Evening Even Ever Every
Everything Excellent Fab Fabulous Fair Fantastic Fine First For Forgive From Glad Good Goodbye Got Grand Great Had Happy
Has Have Having He Hello Her Here Hey Hi His Hope Hopefully How However If In Indeed Is Isn It Its Just Keep Kind
Kindly Let Lovely Many Marvellous May Maybe Me Meanwhile Might Morning Much Must My Never Nevertheless Next Nice No Noted
Not Nothing Now Of Oh Ok Okay On Once One Only Or Other Otherwise Our Perfect Perhaps Please Pleased Possibly Quick
Quite Rather Really Regarding Right Sadly See Shall She Should Since So Some Something Sorry Sounds Splendid Still
Such Super Sure Terrific Thank Thanks That Thats The Their Them Then There These They This Those Though Thought
Thursday Till To Today Tomorrow Tonight Totally Two Understood Unfortunately Until Unless Very Was We Well Were What
Whatever When Where Whether Which While Who Why Will With Without Wonderful Would Wouldn Yes Yet You Your Yours
Afraid Agreed Already Alternatively Apologies Booked Booking Certainly Confirmed Could Definitely Excuse Hold
Ha Hmm Anyway Lastly Lucky Ooh Haha Fair Gosh Righto Sure Naturally Of Shame Speaking Thankfully Totally Understandable What Whoops Wow
Whenever Call Give Tell Feel Happy Hopefully Wishing Have Take Bear Bless Apologies Ah Aha Cheerio Enjoy Mind Shame
""".split())

DASH_RE = re.compile(r"\s*[‒–—―−]\s*")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️‍]+")


@dataclass(frozen=True)
class Problem:
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


def fix(text: str) -> str:
    """Style repairs that need no judgement: long dashes, curly quotes, emoji, stray spaces."""
    t = DASH_RE.sub(" - ", text)
    t = t.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    t = t.replace("…", "...").replace(" ", " ")
    t = EMOJI_RE.sub("", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r" +([,.!?])", r"\1", t)
    return t.strip()


# ---------- extraction helpers (also used by policy) ----------

def _num(word: str) -> int | None:
    w = word.lower().strip()
    if w.isdigit():
        return int(w)
    return NUM_WORDS.get(w)


AMPM = r"(a\.?m\.?|p\.?m\.?)"
TIME_PATTERNS = [
    # 17:00, 5:30pm, 5.30pm, 5 pm
    re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*" + AMPM + r"?(?![\d:])", re.I),
    re.compile(r"\b(1[0-2]|0?[1-9])\.([0-5]\d)\s*" + AMPM, re.I),
    re.compile(r"\b(1[0-2]|0?[1-9])()\s*" + AMPM + r"(?!\w)", re.I),
]
WORD_HOUR = r"(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|1[0-2]|[1-9])"
OCLOCK_RE = re.compile(WORD_HOUR + r"\s*o'?\s?clock", re.I)
HALF_RE = re.compile(r"\b(half|quarter)\s+(past|to)\s+" + WORD_HOUR + r"\b", re.I)
AT_HOUR_RE = re.compile(r"\b(?:at|by|around|about|from|until|till|after|before)\s+(1[0-2]|[1-9]|" +
                        r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b(?![:.]\d|\s*(?:" +
                        AMPM + r"|o'?\s?clock|people|persons|guests|of us|adults|diners|covers|minutes?|mins?|"
                        r"hours?|days?|weeks?|seconds?|more|or|and|-))", re.I)
NOON_RE = re.compile(r"\b(noon|midday)\b", re.I)


def _cands(h: int, m: int, ap: str | None) -> set[str]:
    if ap:
        pm = ap.lower().startswith("p")
        hh = h % 12 + (12 if pm else 0)
        return {f"{hh:02d}:{m:02d}"}
    if h > 12 or h == 0:
        return {f"{h:02d}:{m:02d}"}
    return {f"{h % 12:02d}:{m:02d}", f"{h % 12 + 12:02d}:{m:02d}", f"{h:02d}:{m:02d}"}


def find_times(text: str) -> list[tuple[str, set[str]]]:
    """Each time mention with its possible 24h values ("5" -> 05:00 or 17:00)."""
    out: list[tuple[str, set[str]]] = []
    taken: list[tuple[int, int]] = []

    def add(m, cands):
        if any(a < m.end() and m.start() < b for a, b in taken):
            return
        taken.append((m.start(), m.end()))
        out.append((m.group(0).strip(), cands))

    for pat in TIME_PATTERNS:
        for m in pat.finditer(text):
            h, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
            if pat is TIME_PATTERNS[0] and not ap and h <= 23:
                add(m, {f"{h:02d}:{mm:02d}"} if h > 12 or h == 0 or mm or len(m.group(1)) == 2 else _cands(h, mm, None))
            else:
                add(m, _cands(h, mm, ap))
    for m in OCLOCK_RE.finditer(text):
        add(m, _cands(_num(m.group(1)), 0, None))
    for m in HALF_RE.finditer(text):
        h = _num(m.group(3))
        if m.group(1).lower() == "half":
            add(m, _cands(h, 30, None))
        elif m.group(2).lower() == "past":
            add(m, _cands(h, 15, None))
        else:
            add(m, _cands(h - 1 or 12, 45, None))
    for m in NOON_RE.finditer(text):
        add(m, {"12:00"})
    for m in AT_HOUR_RE.finditer(text):
        add(m, _cands(_num(m.group(1)), 0, None))
    return out


def norm_time(text: str) -> str | None:
    """'17:00', '5pm', '5.30 pm' -> '17:00' / '17:30'; None if not exactly one clear time."""
    found = find_times(text or "")
    if not found:
        return None
    cands = found[0][1]
    if len(cands) == 1:
        return next(iter(cands))
    pm = [c for c in cands if int(c[:2]) >= 12]
    return pm[0] if pm else sorted(cands)[0]


DAY_MONTH_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + "|".join(MONTHS) +
                          r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b\.?", re.I)
MONTH_DAY_RE = re.compile(r"\b(" + "|".join(MONTHS) + r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+"
                          r"(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
NUM_DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
REL_DAYS = ("tomorrow", "yesterday", "next week", "the day after")


def _month(word: str) -> int:
    w = word.lower().rstrip(".")
    return MONTHS.index(w) + 1 if w in MONTHS else MONTH_ABBR[w[:4] if w == "sept" else w[:3]]


def find_dates(text: str) -> list[tuple[str, str]]:
    """Date mentions as (shown text, key). Keys: 'd:19-11', 'w:thursday', 'r:tomorrow'."""
    out = []
    for m in DAY_MONTH_RE.finditer(text):
        out.append((m.group(0), f"d:{int(m.group(1))}-{_month(m.group(2))}"))
    for m in MONTH_DAY_RE.finditer(text):
        out.append((m.group(0), f"d:{int(m.group(2))}-{_month(m.group(1))}"))
    for m in NUM_DATE_RE.finditer(text):
        d, mo = int(m.group(1)), int(m.group(2))
        before = text[max(0, m.start() - 1):m.start()]
        if 1 <= d <= 31 and 1 <= mo <= 12 and before not in ("+", ":") and not re.match(r"\s*" + AMPM, text[m.end():], re.I):
            out.append((m.group(0), f"d:{d}-{mo}"))
    low = text.lower()
    for w in WEEKDAYS:
        for m in re.finditer(rf"\b{w}\b", low):
            out.append((text[m.start():m.end()], f"w:{w}"))
    for r in REL_DAYS:
        if re.search(rf"\b{r}\b", low):
            out.append((r, f"r:{r}"))
    return out


PARTY_PATTERNS = [
    re.compile(r"\btable for (\w+)", re.I),
    re.compile(r"\b(?:booking|reservation) for (\w+)(?!\s*(?:at|on|pm|am|:))", re.I),
    re.compile(r"\bparty of (\w+)", re.I),
    re.compile(r"\b(\w+) (?:people|persons|guests|adults|diners|covers)\b", re.I),
    re.compile(r"\bthe (\w+) of us\b", re.I),
    re.compile(r"\bfor (\w+) (?:people|persons|guests|adults)\b", re.I),
]


def find_party(text: str) -> list[tuple[str, int]]:
    out = []
    for pat in PARTY_PATTERNS:
        for m in pat.finditer(text):
            n = _num(m.group(1))
            if n is not None:
                out.append((m.group(0), n))
    return out


PHONE_RE = re.compile(r"(?<![\w:])\+?\d[\d ()\-]{5,}\d(?![\d:])")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL_RE = re.compile(r"\bhttps?://\S+|\bwww\.\S+|\b[\w-]+\.(?:com|co\.uk|uk|org|net|io|london)\b", re.I)

EXPERIENCE_RES = [re.compile(p, re.I) for p in (
    r"\bI (?:did|do) (?:catch|watch|see|follow|go)\b",
    r"\bI (?:watched|caught|went to|ate|tried|visited|attended|loved the|enjoyed the|saw the|saw it|saw that)\b",
    r"\bI(?:'ve| have) (?:been to|been there|been here|been in|eaten|tried|visited|watched|seen|dined|"
    r"heard (?:great|good|lovely|wonderful|so much|lots|a lot))\b",
    r"\bI was (?:there|at the|at your|watching|in)\b",
    r"\b(?:my|our) favou?rite\b",
    r"\bI(?:'m| am) (?:a |such a )?(?:big |huge )?fan\b",
    r"\bwe (?:went|ate|visited|watched|were there|have been)\b",
    r"\bI (?:still )?remember\b",
    r"\blast time I\b",
    r"\bwhat a (?:game|match|goal|result)\b",
    r"\b(?:it|that) was (?:quite |such |really |truly |a |an |some )*(?:\w+ )?(?:match|game|result|goal|show|film)\b",
    r"\bquite the (?:game|match|result|goal)\b",
    r"\bI (?:missed|skipped) (?:it|that|the)\b",
)]

PAY_WORDS = re.compile(r"\b(deposit|card|payment|pay|paid|prepay|pre-pay|charge|fee|iban|sort code|account number|"
                       r"cvv|cvc|expiry|bank|transfer)\b|[£$€]", re.I)
PAY_PROMISE = re.compile(r"\b(?:I|we)(?:'ll| will| can| shall| would)? (?:happily )?(?:pay|prepay|transfer|send "
                         r"(?:the |a )?(?:money|deposit|payment|card))\b", re.I)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _sentence_starts(text: str) -> set[int]:
    starts = {0}
    for m in re.finditer(r"[.!?:;]\s+|\n+\s*|\"\s*", text):
        starts.add(m.end())
    return starts


def allowed_times(facts: FactStore, chat_texts: list[str]) -> set[str]:
    ok = {facts.get("time", "17:00")}
    if facts.get("offered_time"):
        ok.add(facts.get("offered_time"))
    for t in chat_texts:
        for _, c in find_times(t):
            ok |= c
    return ok


def allowed_dates(facts: FactStore, chat_texts: list[str]) -> set[str]:
    d: date = facts.booking_date()
    ok = {f"d:{d.day}-{d.month}", f"w:{WEEKDAYS[d.weekday()]}"}
    for t in chat_texts:
        ok |= {k for _, k in find_dates(t)}
    return ok


def check(text: str, facts: FactStore, chat_texts: list[str]) -> list[Problem]:
    """Facts in `text` not backed by `facts` or by `chat_texts` (what the manager and the owner wrote)."""
    probs: list[Problem] = []
    chat = "\n".join(chat_texts)
    known_blob = (" ".join(str(v) for v in facts.known().values()) + "\n" + chat).lower()
    phone = facts.get("phone")

    if re.search(r"[‒-―−]", text):
        probs.append(Problem("dash", "long dash left in the text"))

    time_spans = []
    for shown, cands in find_times(text):
        time_spans.append(shown)
        if not cands & allowed_times(facts, chat_texts):
            probs.append(Problem("time", f"{shown!r} is not the booking time and the manager did not say it"))

    for run in PHONE_RE.findall(text):
        dg = _digits(run)
        if len(dg) < 7 or any(run.strip() in s for s in time_spans):
            continue
        if phone and dg in _digits(phone):
            continue
        if any(dg and dg in _digits(t) for t in chat_texts):
            continue
        probs.append(Problem("phone", f"number {run.strip()!r} is not the known phone"))

    for m in EMAIL_RE.findall(text) + URL_RE.findall(text):
        m = m.rstrip(".,;:!?)")
        if m.lower() not in known_blob:
            probs.append(Problem("contact", f"{m!r} is not in the facts or the chat"))

    okd = allowed_dates(facts, chat_texts)
    for shown, key in find_dates(text):
        if key not in okd:
            probs.append(Problem("date", f"{shown!r} is not the booking date and the manager did not say it"))

    want = int(facts.get("party_size", 2))
    for shown, n in find_party(text):
        if n != want:
            probs.append(Problem("party", f"{shown!r} - the booking is for {want}"))

    starts = _sentence_starts(text)
    for m in re.finditer(r"\b([A-Z][a-zA-Z]+)(?:'[a-z]+)?\b", text):
        w = m.group(1)
        if w in ALWAYS_OK or re.search(rf"\b{re.escape(w.lower())}\b", known_blob):
            continue
        at_start = m.start() in starts
        if at_start and (w in SENTENCE_OK or w.endswith("ly")):
            continue
        probs.append(Problem("name", f"{w!r} is not in the facts or the chat"))

    for rx in EXPERIENCE_RES:
        m = rx.search(text)
        if m:
            probs.append(Problem("experience", f"{m.group(0)!r} claims a personal experience"))
            break

    for sent in re.split(r"(?<=[.!?])\s+", text):
        if PAY_WORDS.search(sent) and re.search(r"\d", sent):
            probs.append(Problem("payment", f"money with numbers: {sent.strip()!r}"))
    m = PAY_PROMISE.search(text)
    if m:
        probs.append(Problem("payment", f"{m.group(0)!r} promises a payment"))

    rest = facts.get("restaurant_name")
    if rest and facts.source("restaurant_name") != "manager" and str(rest).lower() in text.lower():
        probs.append(Problem("restaurant", "restaurant name used before the manager gave it"))

    seen, uniq = set(), []
    for p in probs:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def describe(problems: list[Problem]) -> str:
    return "; ".join(str(p) for p in problems)
