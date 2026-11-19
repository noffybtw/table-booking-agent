"""Rules the code enforces around the model: which slot values to accept, owner questions, owner answers."""
from __future__ import annotations

import re

from agent import guard
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

QUESTIONS_RU = {
    "guest_name": "На какое имя записать бронь?",
    "phone": "Нужен телефон для брони. Ответь сообщением или кнопкой «Отправить мой номер».",
    "email": "Нужен email для брони.",
    "occasion": "Спрашивают, есть ли повод (день рождения и т.п.).",
    "allergies": "Спрашивают про аллергии или диету.",
    "deposit": "Просят депозит или оплату. Агент деньги не обещает. Что ответить?",
    "card_details": "Просят данные карты. Агент их не даёт. Что ответить?",
}
NAME_AGAIN_RU = "Напиши имя латиницей, так, как его сказать менеджеру (например, Yaroslav)."
WHICH_RU = "Не понял, к какому вопросу это. Открыты: {keys}. Ответь реплаем на нужный вопрос."
KEY_NAMES_RU = {"guest_name": "имя для брони", "phone": "телефон", "email": "email",
                "alternative_time": "другое время", "reply_help": "что ответить менеджеру"}

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


YES_RE = re.compile(r"^\s*(да|ага|ок|окей|ok|okay|yes|yep|подходит|годится|давай|согласен|норм)\b", re.I)
NO_RE = re.compile(r"^\s*(нет|не подходит|только|no|nope|only)\b", re.I)


def manager_asked(key: str, manager_texts: list[str]) -> bool:
    """Personal details go to the owner only when the manager asked for them, not because the model wants them."""
    rx = ASKED_RE.get(key)
    return rx is None or any(rx.search(t) for t in manager_texts)


def owner_question(key: str, facts: FactStore, manager_last: str) -> tuple[str, list[tuple[str, str]], bool]:
    """Russian question for the owner, inline buttons (label, value), and whether to offer a contact button."""
    said = f"\nМенеджер написал: «{manager_last.strip()[:300]}»" if manager_last.strip() else ""
    if key == "alternative_time":
        offered = facts.get("offered_time") or "другое время"
        q = f"Менеджер предлагает {offered} вместо {facts.get('time')}. Что ответить?{said}"
        return q, [(f"{offered} подходит", "accept"), (f"Только {facts.get('time')}", "only")], False
    if key == "reply_help":
        return f"Не могу ответить менеджеру без выдумки. Что ему сказать?{said}", [], False
    base = QUESTIONS_RU.get(key, f"Менеджер спрашивает: {key.replace('_', ' ')}. Ответь сообщением.")
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
    return WHICH_RU.format(keys=", ".join(KEY_NAMES_RU.get(k, k.replace("_", " ")) for k in pending))


def strip_label(text: str) -> str:
    return re.sub(r"^\s*(name|имя|phone|телефон|номер|email|почта)\s*[:\-]\s*", "", text or "", flags=re.I).strip()


def apply_owner_answer(facts: FactStore, key: str, text: str, value: str | None, contact: bool,
                       turn: int) -> str | None:
    """Stores the answer; returns the line the model sees (OWNER ...), or None when the answer is not taken and
    the owner has to be asked again (a name not in Latin letters)."""
    key = norm_key(key)
    text = (text or "").strip()
    if key == "alternative_time":
        offered = facts.get("offered_time")
        if value is None and YES_RE.match(text):
            value = "accept"
        elif value is None and NO_RE.match(text):
            value = "only"
        if value == "accept" or (offered and guard.norm_time(text) == offered):
            if offered:
                facts.set("time", offered, "owner", turn)
                facts.drop("owner_note")
                return f"{offered} works, please book it for {offered}."
        if value == "only":
            instead = f"{offered} does not work, only" if offered else "Only"
            note = (f"{instead} {facts.get('time')} works. Decline politely and ask whether {facts.get('time')} "
                    f"might free up.")
            facts.set("owner_note", note, "owner", turn)
            return note
        facts.set("owner_note", text, "owner", turn)
        return text
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
        t = guard.norm_time(slots.offered_time)
        said = set().union(*[c for m in manager_texts for _, c in guard.find_times(m)]) if manager_texts else set()
        if t and t in said:
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


ASKS_RESTAURANT_RE = re.compile(r"restaurant'?s name|name of (?:the|your) restaurant|which restaurant|"
                                r"what(?:'s| is) the restaurant", re.I)


def asks_restaurant(text: str) -> bool:
    return bool(ASKS_RESTAURANT_RE.search(text or ""))
