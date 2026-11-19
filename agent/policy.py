"""Rules the code enforces around the model: which slot values to accept, owner questions, owner answers."""
from __future__ import annotations

import re

from agent import guard, texts
from agent.facts import FactStore

KEY_ALIASES = {
    "name": "guest_name", "guest": "guest_name", "full_name": "guest_name", "surname": "guest_name",
    "booking_name": "guest_name", "last_name": "guest_name", "guest_name": "guest_name",
    "phone": "phone", "phone_number": "phone", "contact_number": "phone", "mobile": "phone",
    "contact_phone": "phone", "telephone": "phone", "number": "phone",
    "alternative_time": "alternative_time", "time": "alternative_time", "new_time": "alternative_time",
    "offered_time": "alternative_time", "other_time": "alternative_time",
    "email": "email", "email_address": "email",
}
OPTIONAL_KEYS = {"phone", "email"}

QUESTION_KEYS = ("guest_name", "phone", "email", "occasion", "allergies", "deposit", "card_details")
# what decisions.md says for an owner question (the bot wording stays in events.jsonl)
GLOSS = {
    "guest_name": "what name to book under",
    "phone": "a contact phone for the booking",
    "email": "an email for the booking",
    "occasion": "whether there is an occasion",
    "allergies": "allergies or diet",
    "deposit": "the manager asks for a deposit or payment; what to answer",
    "card_details": "the manager asks for card details; what to answer",
    "reply_help": "every draft was blocked; what to tell the manager",
}

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z '.-]{1,40}$")


def norm_key(key: str) -> str:
    k = re.sub(r"[^a-z0-9]+", "_", (key or "").lower()).strip("_")
    return KEY_ALIASES.get(k, k or "question")


ASKED_RE = {
    "guest_name": re.compile(r"\bname\b|\bunder (?:what|which|whose)\b|\bwho(?:'s| is) (?:it|the booking) for\b|"
                             r"\bwhose\b", re.I),
    "phone": re.compile(r"\bphone|\bnumber\b|\bcontact\b|\bmobile\b|\breach you\b|\bcall you\b|\btel\b", re.I),
    "email": re.compile(r"\be-?mail\b", re.I),
}


# the owner's answer to "another time?" in words; parsed in Ukrainian, Russian and English whatever OWNER_LANG says
YES_RE = re.compile(r"^\s*(?:(?:да|ага|ок|окей|ok|okay|yes|yep|sure|fine|подходит|годится|давай|согласен|норм|хорошо|"
                    r"ладно|конечно|пойд[её]т|можно|бер[её]м|добре|гаразд|звісно|підходить|згоден|годиться)\b|"
                    r"\+|\U0001F44D)", re.I)
NO_RE = re.compile(r"^\s*(нет|не подходит|только|ні|не підходить|тільки|лише|no|nope|only|not)\b", re.I)
# a yes that also says no or "but" ("ок, но только 17:00", "так, але краще 18:00") is not a yes
NOT_RE = re.compile(r"\b(нет|не|только|ні|тільки|лише|no|not|only|но|але|лучше|краще|but|rather|better)\b", re.I)
# "так" is a yes alone or before another yes word; "так, подожди" or "так собі" is not
TAK_RE = re.compile(r"^\s*так\b[\s,.!)]*", re.I)
WAIT_RE = re.compile(r"подожд|зачека|почека|\bстоп\b|\bwait\b|\bhold on\b", re.I)


def manager_asked(key: str, manager_texts: list[str]) -> bool:
    """Personal details go to the owner only when the manager asked for them, not because the model wants them."""
    rx = ASKED_RE.get(key)
    return rx is None or any(rx.search(t) for t in manager_texts)


# a sentence that asks for something: a question, "please", "can I take", "could you give", "I'll need"
REQUEST_RE = re.compile(r"\?|\bplease\b|\b(?:can|could|may) (?:I|we) (?:take|have|get|grab|put|ask)\b|"
                        r"\b(?:can|could|would) you (?:give|send|share|tell|provide|leave)\b|\bunder what name\b|"
                        r"\bcontact number\b|\b(?:I|we)(?:'ll| will|'d| would)? need\b", re.I)
OWN_NAME_RE = re.compile(r"\b(?:my|our) name(?:'s|\s+is)\b", re.I)


def manager_asked_now(key: str, said: list[str]) -> bool:
    """The manager asks for the name or phone in this message: a sentence names the field and is a request.
    "Hi, my name is Vlad.", "Table number 12 is free", "For what number of people?" do not ask."""
    rx = FIELD_RE.get(key)
    if key not in ("guest_name", "phone") or rx is None:
        return False
    for t in said:
        for sent in re.split(r"(?<=[.!?])\s+", OWN_NAME_RE.sub(" ", ASKS_RESTAURANT_RE.sub(" ", t or ""))):
            if rx.search(sent) and REQUEST_RE.search(sent):
                return True
    return False


def time_sure(hhmm: str, texts_: list[str]) -> bool:
    """The time stands in the texts in one reading only ("18:30", "6:30pm", "1830"), not "6:30", "half six" or "7"."""
    return any(c == {hhmm} for t in texts_ for _, c in guard.find_times(t))


def owner_question(key: str, facts: FactStore, manager_last: str) -> tuple[str, list[tuple[str, str]], bool]:
    """The question for the owner in OWNER_LANG, inline buttons (label, value), and whether to offer a contact button."""
    said = texts.t("manager_said", text=manager_last.strip()[:300]) if manager_last.strip() else ""
    if key == "alternative_time":
        only = (texts.t("btn.only", time=facts.get("time")), "only")
        if not facts.get("offered_time"):
            # no time was taken from the manager's words: an accept button would say nothing, the owner types it
            q = texts.t("q.alternative_time", offered=texts.t("other_time"), time=facts.get("time"))
            return f"{q} {texts.t('time_type')}{said}", [only], False
        offered = facts.get("offered_time")
        q = texts.t("q.alternative_time", offered=offered, time=facts.get("time")) + said
        return q, [(texts.t("btn.accept", offered=offered), "accept"), only], False
    if key == "reply_help":
        return texts.t("q.reply_help") + said, [], False
    base = texts.t(f"q.{key}") if key in QUESTION_KEYS else texts.t("q.other", key=key.replace("_", " "))
    return f"{base}{said}", [], key == "phone"


def owner_key(pending: list[str], text: str) -> str | None:
    """Which open question a text without a reply answers: one open question -> that one; several -> a phone if
    the text has 7 or more digits and the phone is asked; otherwise None (ask the owner to reply to a question)."""
    if len(pending) == 1:
        return pending[0]
    if "phone" in pending and len(re.sub(r"\D", "", text or "")) >= 7:
        return "phone"
    return None


def which_question(pending: list[str]) -> str:
    return texts.t("which", keys=", ".join(key_name(k) for k in pending))


def key_name(key: str) -> str:
    return texts.t(f"key.{key}") if texts.has(f"key.{key}") else key.replace("_", " ")


def gloss(key: str, facts: FactStore) -> str:
    if key == "alternative_time":
        offered = facts.get("offered_time") or "another time"
        return (f"the manager offers {offered} instead of {facts.get('time')}; buttons \"{offered} works\" / "
                f"\"Only {facts.get('time')}\"")
    return GLOSS.get(key, f"the manager asks about {key.replace('_', ' ')}")


def strip_label(text: str) -> str:
    return re.sub(r"^\s*(name|имя|ім['ʼ]?я|phone|телефон|номер|email|e-mail|почта|пошта)\s*[:\-]\s*", "", text or "",
                  flags=re.I).strip()


def _yes(text: str) -> bool:
    m = TAK_RE.match(text)
    if m:
        rest = text[m.end():]
        return not rest.strip() or _yes(rest)
    return bool(YES_RE.match(text))


def time_answer(text: str, offered: str | None, time: str | None) -> str | None:
    """"accept", "only" or None (not understood: the question goes again with its buttons) for an answer typed
    instead of a button. A question, a wait word or a time other than the one the answer is about is None."""
    text = text or ""
    if "?" in text or WAIT_RE.search(text):
        return None
    times = [c for _, c in guard.find_times(text)]

    def only(t):
        return bool(t) and all(t in c for c in times)

    if NO_RE.match(text):
        return "only" if only(time) or not times else None
    if _yes(text) and not NOT_RE.search(text):
        return "accept" if only(offered) or not times else None
    if times and only(time):
        return "only"
    if times and only(offered):
        return "accept"
    return None


def typed_time(text: str, time: str | None) -> str | None:
    """The one time in an owner's answer like "18:30" or "давай 6:30", read as the evening one; None for a question,
    a wait, a no or a "but", more than one time, or the booking time itself."""
    text = text or ""
    found = guard.find_times(text)
    if "?" in text or WAIT_RE.search(text) or NOT_RE.search(text) or NO_RE.match(text) or len(found) != 1:
        return None
    t = guard.norm_time(text)
    return t if t != time else None


def apply_owner_answer(facts: FactStore, key: str, text: str, value: str | None, contact: bool,
                       turn: int) -> str | None:
    """Stores the answer; returns the line the model sees (OWNER ...), or None when the answer is not taken and
    the owner has to be asked again (a name not in Latin letters, a time answer that is neither yes nor no)."""
    key = norm_key(key)
    text = (text or "").strip()
    if key == "alternative_time":
        offered = facts.get("offered_time")
        if value is None:
            value = time_answer(text, offered, facts.get("time"))
        if value in (None, "accept") and not offered:
            # no offer was stored (the model's time was not taken): a time the owner types is the one accepted
            offered = typed_time(text, facts.get("time"))
            value = "accept" if offered else value
        if value == "accept" and offered:
            facts.set("time", offered, "owner", turn)
            facts.drop("owner_note")
            return f"{offered} works, please book it for {offered}."
        if value == "only":
            instead = f"{offered} does not work, only" if offered else "Only"
            note = (f"{instead} {facts.get('time')} works. Decline politely and ask whether {facts.get('time')} "
                    f"might free up.")
            facts.set("owner_note", note, "owner", turn)
            return note
        return None  # neither yes nor no: the question goes again with its buttons
    if key == "phone":
        raw = text if contact else strip_label(text)
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 7:
            phone = raw if not contact or raw.startswith("+") else f"+{raw}"
            facts.set("phone", phone, "owner", turn)
            return f"phone: {phone}"
        facts.set("owner_note", text, "owner", turn)
        return text
    if key == "guest_name":
        name = strip_label(text)
        if not NAME_RE.match(name):
            return None  # "Ярослав" or "на Ярослава, на двоих" would go to the manager as it is
        facts.set("guest_name", name, "owner", turn)
        return f"name for the booking: {name}"
    if key == "reply_help":
        facts.set("owner_note", text, "owner", turn)
        return text
    facts.set(key, strip_label(text), "owner", turn)
    return f"{key.replace('_', ' ')}: {strip_label(text)}"


def find_in(texts: list[str], value: str) -> str | None:
    """The value as written in one of the texts (case-insensitive), or None."""
    v = (value or "").strip().strip(".")
    if len(v) < 2:
        return None
    for t in reversed(texts):
        i = t.lower().find(v.lower())
        if i >= 0:
            return t[i:i + len(v)]
    return None


MORNING_RE = re.compile(r"\b(?:in the|this) morning\b", re.I)
_HOUR = r"(1[0-2]|[1-9]|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
# "I can do 7", "7 instead?", "6ish?": a bare hour, read only to back the model's own offered time
BARE_HOUR_RE = re.compile(r"(?<![\d:.,£$€#+/-])\b" + _HOUR + r"(?:\s*ish)?\b(?![:.,/]?\d)(?!\s*(?:people|persons|"
                          r"guests|of us|adults|diners|covers|minutes?|mins?|hours?|days?|weeks?|seconds?|%|st\b|nd\b|"
                          r"rd\b|th\b|a\.?m|p\.?m|o'?\s?clock))", re.I)
WORD_MINUTES_RE = re.compile(r"\b" + _HOUR + r"[\s-]+(fifteen|thirty|forty[\s-]five)\b", re.I)
H_MINUTES_RE = re.compile(r"\b([01]?\d|2[0-3])h([0-5]\d)\b", re.I)
WORD_MINUTE = {"fifteen": 15, "thirty": 30}


def bare_hours(text: str) -> set[str]:
    """Times written as a bare hour ("7", "seven", "6ish"), "six thirty" or "18h30", each as one value (as_written)."""
    out: set[str] = set()
    taken: list[tuple[int, int]] = []
    for m in H_MINUTES_RE.finditer(text or ""):
        out.add(as_written(guard._cands(int(m.group(1)), int(m.group(2)), None)))
        taken.append(m.span())
    for m in WORD_MINUTES_RE.finditer(text or ""):
        mm = WORD_MINUTE.get(m.group(2).lower(), 45)
        out.add(as_written(guard._cands(guard._num(m.group(1)), mm, None)))
        taken.append(m.span())
    for m in BARE_HOUR_RE.finditer(text or ""):
        if not any(a <= m.start() < b for a, b in taken):
            out.add(as_written(guard._cands(guard._num(m.group(1)), 0, None)))
    return out


def as_written(cands: set[str]) -> str:
    """One value for a time with no am/pm: 1 to 9 is the evening ("6:30" is 18:30, the booking is for 17:00 today),
    10 and 11 are as written ("11:30" is 11:30, not 23:30)."""
    if len(cands) == 1:
        return next(iter(cands))
    return min(cands) if int(min(cands)[:2]) in (10, 11) else max(cands)


def manager_times(manager_texts: list[str]) -> set[str]:
    """Each time the manager wrote, as one value (see as_written); "6:30" is 06:30 only when the manager wrote
    "06:30", "6:30am" or "6:30 in the morning"."""
    out: set[str] = set()
    for m in manager_texts:
        for _, c in guard.find_times(m):
            out.add(min(c) if MORNING_RE.search(m) else as_written(c))
    return out


def accepted_slots(slots, facts: FactStore, manager_texts: list[str]) -> tuple[dict, list[str]]:
    """Slot values from the model that the manager really wrote. Returns ({key: value}, rejected notes)."""
    ok, rejected = {}, []
    if slots.restaurant_name:
        found = find_in(manager_texts, slots.restaurant_name)
        if found is None and slots.restaurant_name.lower().startswith("the "):
            found = find_in(manager_texts, slots.restaurant_name[4:])
        if found:
            ok["restaurant_name"] = found
        else:
            rejected.append(f"restaurant_name {slots.restaurant_name!r} not in the manager's messages")
    if slots.offered_time:
        # the model's "06:30" or "6:30" is the time the manager wrote with those digits, morning or evening
        cands = next((c for _, c in guard.find_times(slots.offered_time)), set())
        cands |= {f"{(int(c[:2]) + 12) % 24:02d}{c[2:]}" for c in cands if int(c[:2]) % 12}
        said = cands & manager_times(manager_texts)
        if not said and manager_texts:
            # "I can do 7": the model's 19:00 stands when its hour is a bare number in the manager's last message
            said = cands & bare_hours(manager_texts[-1])
        own = guard.norm_time(slots.offered_time)
        t = own if own in said else max(said) if said else None
        if t:
            if t != facts.get("time"):
                ok["offered_time"] = t
        else:
            rejected.append(f"offered_time {slots.offered_time!r} not said by the manager")
    if slots.condition:
        nums = re.findall(r"\d+", slots.condition)
        blob = " ".join(manager_texts)
        if all(n in blob for n in nums) and guard.check(slots.condition, facts, manager_texts) == []:
            ok["condition"] = slots.condition.strip().rstrip(".")
        else:
            rejected.append(f"condition {slots.condition!r} not backed by the manager's words")
    return ok, rejected


# the manager's plain yes to "Is the table ... all confirmed, then?": no question, no "but", no other time
MANAGER_YES_RE = re.compile(r"^[\s,.!]*(?:(?:ok|okay|yes|yep|yeah|yup|sure|correct|confirmed|all confirmed|that's right|"
                            r"that's correct|that's fine|right|fine|perfect|great|lovely|absolutely|definitely|"
                            r"indeed|of course|all good|done|booked|all booked|it is|that is|"
                            r"it's (?:all )?(?:confirmed|booked|fine|done|set|sorted))\b|\U0001F44D)", re.I)
# "no" that means yes; taken out before the stop list
NO_PROBLEM_RE = re.compile(r"\bno (?:problem|worries)\b", re.I)
MANAGER_NOT_RE = re.compile(r"\b(?:no|not|sorry|but|unfortunately|actually|wait|hold on|only|instead)\b|n't", re.I)
# "Ok, let me check" or "Yes, one moment" is the manager stalling, not agreeing
MANAGER_STALL_RE = re.compile(r"\b(?:let me|will|check\w*|moments?|minutes?|secs?|seconds?|look\w*|see(?! you\b)|"
                              r"bear with|hang on|hold on|get back|let you know|wait\w*)\b|'ll\b", re.I)
# after the yes-words only these may stay ("Yes, that's fine, see you then"); anything else is the model's call.
# "§" stands for the booking time, written any way.
MANAGER_YES_WORDS = ("ok", "okay", "yes", "yep", "yeah", "yup", "sure", "correct", "confirmed", "all confirmed",
                     "that's right", "that's correct", "that's fine", "right", "fine", "perfect", "great", "lovely",
                     "absolutely", "definitely", "indeed", "of course", "all good", "done", "booked", "all booked",
                     "it is", "that is")
MANAGER_YES_ALLOW = ("that's", "that is", "it is", "all", "all good", "confirmed", "booked", "fine", "good", "great",
                     "perfect", "lovely", "thanks", "thank you", "cheers", "see you", "then", "the time", "for two",
                     "at §", "§", "it's", "is", "the table", "the booking", "you're", "you are", "under", "for",
                     "today", "tonight", "see you later", "see you soon", "see you tonight", "set", "sorted")
_YES_PHRASES = sorted({tuple(p.split()) for p in MANAGER_YES_WORDS + MANAGER_YES_ALLOW}, key=len, reverse=True)


def _only_yes_words(text: str, time: str | None, name: str | None = None) -> bool:
    t = text.lower().replace("\u2019", "'").replace("\U0001F44D", " ")
    phrases = _YES_PHRASES + [(w,) for w in re.findall(r"[a-z']+", (name or "").lower())]  # "confirmed under Kostyk"
    for said, cands in guard.find_times(text):
        if time in cands:
            t = t.replace(said.lower(), " § ")
    words = re.findall(r"[a-z']+|\d+|§", t)  # a number that is not the booking time is not a yes word
    i = 0
    while i < len(words):
        step = next((len(p) for p in phrases if tuple(words[i:i + len(p)]) == p), 0)
        if not step:
            return False
        i += step
    return True


def manager_yes(text: str, time: str | None, name: str | None = None) -> bool:
    t = (text or "").strip()
    if not t or "?" in t or len(t.split()) > 12:
        return False
    t = NO_PROBLEM_RE.sub(" ", t.replace("\u2019", "'")).strip()
    if not t.strip(" ,.!"):
        return True  # "No problem!"
    if MANAGER_NOT_RE.search(t) or MANAGER_STALL_RE.search(t):
        return False
    if any(time not in cands for _, cands in guard.find_times(t)):
        return False
    return bool(MANAGER_YES_RE.match(t)) and _only_yes_words(t, time, name)


STALL_WORDS_RE = re.compile(r"\b(?:wait|hold on|sorry)\b", re.I)


def manager_stall(text: str, time: str | None) -> bool:
    """"Ok, let me check" with no question, no "but" and no other time: the confirm question still stands."""
    t = (text or "").strip()
    rest = STALL_WORDS_RE.sub(" ", t)  # "Hold on", "Sorry, one moment": the stall words are not a "no" here
    return (bool(MANAGER_STALL_RE.search(t)) and "?" not in t and not MANAGER_NOT_RE.search(rest)
            and all(time in cands for _, cands in guard.find_times(t)))


ASKS_RESTAURANT_RE = re.compile(r"restaurant'?s name|name of (?:the|your) restaurant|which restaurant|"
                                r"what(?:'s| is) the restaurant", re.I)


def asks_restaurant(text: str) -> bool:
    return bool(ASKS_RESTAURANT_RE.search(text or ""))


# what the owner holds and the manager does not: asked from the manager, these go to the owner instead
OWNER_FIELDS = ("guest_name", "phone", "email")
# open owner questions whose subject is the manager's to name, not the owner's data
NOT_OWNER_FIELDS = ("alternative_time", "reply_help", "deposit", "card_details")
FIELD_RE = {
    "guest_name": re.compile(r"(?<!restaurant's )(?<!restaurant )\bname\b(?! of (?:the|your) restaurant)|\bunder (?:what|which|whose)\b|"
                             r"\bwho(?:'s| is) (?:it|the booking|the table) for\b", re.I),
    "phone": re.compile(r"\bphone\b|\bmobile\b|\btelephone\b|\bcontact (?:number|details)\b|\b(?:a|the|your|best) number\b"
                        r"(?! of\b)|\bnumber (?:to|for|on)\b(?! (?:two|people|guests))", re.I),
    "email": re.compile(r"\be-?mail\b", re.I),
    "occasion": re.compile(r"\boccasion\b|\bcelebrat", re.I),
    "allergies": re.compile(r"\ballerg|\bdiet", re.I),
}
ASKING_RE = re.compile(r"\?\s*$|\b(?:please |kindly )?let (?:me|us) know\b|\b(?:could|can|would) you (?:send|share|tell|give|"
                       r"provide|confirm)\b|\bplease (?:send|share|tell|give|provide|confirm)\b", re.I)
# a sentence that gives the detail ("under the name Yaroslav", a number) is not a question for it
GIVES_RE = re.compile(r"\bunder (?:the name )?[A-Z][a-z]|\d[\d* ]{4,}\d")


def owner_field_questions(text: str, keys, known=()) -> list[tuple[str, str]]:
    """(key, sentence) for each sentence of `text` that asks the manager for a field the owner holds. A sentence
    with a known value or a name or number in it gives the field, it does not ask for it."""
    out = []
    for sent in re.split(r"(?<=[.!?;])\s+|\s+-\s+", (text or "").strip()):
        if not ASKING_RE.search(sent) or asks_restaurant(sent) and not re.search(r"\b(?:guest|booking|your) name\b", sent):
            continue
        if GIVES_RE.search(sent) or any(v and str(v).lower() in sent.lower() for v in known):
            continue
        for k in keys:
            rx = FIELD_RE.get(k) or re.compile(r"\b" + r"\s+".join(map(re.escape, k.split("_"))) + r"\b", re.I)
            if rx.search(sent):
                out.append((k, sent))
                break
    return out


# the agent writes as the one booking: no "assistant", no third person, except when the manager asks if it is a bot
PERSONA_RE = re.compile(r"\bassistant\b|\bthe guests?\b|\bmy client\b|\bon behalf of\b", re.I)
BOT_QUESTION_RE = re.compile(r"\b(?:bot|robot|ai|a\.i\.|artificial|automated|machine|chat ?gpt|human|real person|"
                             r"a person|computer)\b", re.I)


def asks_if_bot(text: str) -> bool:
    """The manager asks whether the agent is a bot, an AI or a human."""
    return bool(text and "?" in text and BOT_QUESTION_RE.search(text))


def drop_persona(text: str) -> tuple[str, bool]:
    """Remove every sentence or " - " clause that says "assistant", "the guest", "my client" or "on behalf of".
    Returns (text left, removed)."""
    if not text or not PERSONA_RE.search(text):
        return text, False
    kept = []
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        parts = re.split(r"\s+-\s+", sent)
        kept_parts = [p for p in parts if not PERSONA_RE.search(p)]
        if kept_parts:
            part = " - ".join(kept_parts)
            if kept_parts[-1] is not parts[-1] and part[-1] not in ".!?":
                part += sent[-1] if sent[-1] in ".!?" else "."
            kept.append(part[0].upper() + part[1:])
    out = " ".join(kept)
    return out if re.search(r"[A-Za-z]{2}", out) else "", True
