"""Everything the owner reads in the bot, in two tables: "uk" (default) and "ru". OWNER_LANG picks one.

Only the wording for the owner lives here. The owner's answers are parsed in Ukrainian, Russian and English
whatever OWNER_LANG says (agent/policy.py), and decisions.md gets an English gloss by key, never this wording.
"""
from __future__ import annotations

import os

UK = {
    "q.guest_name": "На яке ім'я записати бронь?",
    "q.phone": "Потрібен телефон для броні. Відповідай повідомленням або кнопкою «Надіслати мій номер».",
    "q.email": "Потрібен email для броні.",
    "q.occasion": "Питають, чи є привід (день народження тощо).",
    "q.allergies": "Питають про алергії чи дієту.",
    "q.deposit": "Просять депозит або оплату. Агент гроші не обіцяє. Що відповісти?",
    "q.card_details": "Просять дані картки. Агент їх не дає. Що відповісти?",
    "q.other": "Менеджер питає: {key}. Відповідай повідомленням.",
    "q.reply_help": "Не можу відповісти менеджеру, нічого не вигадавши. Що йому сказати?",
    "q.alternative_time": "Менеджер пропонує {offered} замість {time}. Що відповісти?",
    "other_time": "інший час",
    "btn.accept": "{offered} підходить",
    "btn.only": "Тільки {time}",
    "manager_said": "\nМенеджер написав: «{text}»",
    "name_again": "Напиши ім'я латиницею, так, як його сказати менеджеру (наприклад, Yaroslav).",
    "which": "Не зрозумів, до якого питання це. Відкриті: {keys}. Відповідай реплаєм на потрібне питання.",
    "time_again": "Не зрозумів відповідь. Натисни кнопку.",
    "restated_without": "Відповіді немає 10 хвилин: підтвердження пішло менеджеру без поля «{keys}». Якщо відповіси, "
                        "агент передасть.",
    "note_kept": "Зараз питань немає, тож менеджеру нічого не пишу. Записав як нотатку для наступної відповіді. {status}",
    "key.guest_name": "ім'я для броні",
    "key.phone": "телефон",
    "key.email": "email",
    "key.alternative_time": "інший час",
    "key.reply_help": "що відповісти менеджеру",
    "contact_button": "Надіслати мій номер",
    "reply_hint": "(відповідай реплаєм на це повідомлення)",
    "accepted": "Прийнято",
    "phone_received": "Номер отримано.",
    "silent_30": "Менеджер мовчить 30 хвилин. Більше не нагадую, чекаю на відповідь.",
    "reminder": "Нагадую: ",
    "status": "{state} Повідомлень: від менеджера {n_in}, від агента {n_out}.",
    "groq_down": "Groq недоступний, агент чекає і пробує знову кожні 30 с.",
    "restatement_blocked": "Фінальне підтвердження не пройшло перевірку: {problems}",
    "booked": "Бронь підтверджена: {text}",
    "guard_template": "Guard заблокував відповідь моделі ({why}), надіслано шаблон: «{text}»",
    "guard_nothing": "Guard заблокував відповідь, нічого не надіслано: {why}",
    "no_restaurant": "Менеджер не назвав ресторан, підтвердження пішло з «at your restaurant».",
    "send_failed": "Не вдалося надіслати повідомлення #{n}: {error}",
    "send_by_hand": "Підтвердження #{n} не дійшло до менеджера ({error}), бронь не підтверджена. Надішли йому "
                    "вручну цей текст:\n{text}",
    "mirror.voice": "голосове",
    "mirror.manager": "менеджер",
    "mirror.no_text": "(без тексту)",
    "mirror.agent": "агент #{n}{voice}: {text}",
    "mirror.voice_tag": " (голос)",
    "start": "▶️ Старт: режим {mode}, менеджер @{peer}. Команди: /pause /resume /stop /status",
    "time_up": "Час вийшов ({until} Лондон), бронь не підтверджена.",
    "turn_error": "Помилка в ході {kind}: {error}. Агент продовжує.",
    "end": "⏹ Кінець: {reason}. Лог: {log}",
    "end.stop": "команда /stop",
    "end.linger": "після підтвердження {minutes} хв без повідомлень",
    "end.until": "час вийшов",
    "end.other": "роботу завершено",
}

RU = {
    "q.guest_name": "На какое имя записать бронь?",
    "q.phone": "Нужен телефон для брони. Ответь сообщением или кнопкой «Отправить мой номер».",
    "q.email": "Нужен email для брони.",
    "q.occasion": "Спрашивают, есть ли повод (день рождения и т.п.).",
    "q.allergies": "Спрашивают про аллергии или диету.",
    "q.deposit": "Просят депозит или оплату. Агент деньги не обещает. Что ответить?",
    "q.card_details": "Просят данные карты. Агент их не даёт. Что ответить?",
    "q.other": "Менеджер спрашивает: {key}. Ответь сообщением.",
    "q.reply_help": "Не могу ответить менеджеру без выдумки. Что ему сказать?",
    "q.alternative_time": "Менеджер предлагает {offered} вместо {time}. Что ответить?",
    "other_time": "другое время",
    "btn.accept": "{offered} подходит",
    "btn.only": "Только {time}",
    "manager_said": "\nМенеджер написал: «{text}»",
    "name_again": "Напиши имя латиницей, так, как его сказать менеджеру (например, Yaroslav).",
    "which": "Не понял, к какому вопросу это. Открыты: {keys}. Ответь реплаем на нужный вопрос.",
    "time_again": "Не понял ответ. Нажми кнопку.",
    "restated_without": "Ответа нет 10 минут: подтверждение ушло менеджеру без поля «{keys}». Если ответишь, "
                        "агент передаст.",
    "note_kept": "Сейчас вопросов нет, поэтому менеджеру ничего не пишу. Записал как заметку для следующего ответа. "
                 "{status}",
    "key.guest_name": "имя для брони",
    "key.phone": "телефон",
    "key.email": "email",
    "key.alternative_time": "другое время",
    "key.reply_help": "что ответить менеджеру",
    "contact_button": "Отправить мой номер",
    "reply_hint": "(ответь реплаем на это сообщение)",
    "accepted": "Принято",
    "phone_received": "Номер получен.",
    "silent_30": "Менеджер молчит 30 минут. Больше не напоминаю, жду ответа.",
    "reminder": "Напоминаю: ",
    "status": "{state} Сообщений: от менеджера {n_in}, от агента {n_out}.",
    "groq_down": "Groq недоступен, агент ждёт и пробует снова каждые 30 с.",
    "restatement_blocked": "Финальное подтверждение не прошло проверку: {problems}",
    "booked": "Бронь подтверждена: {text}",
    "guard_template": "Guard заблокировал ответ модели ({why}), отправлен шаблон: «{text}»",
    "guard_nothing": "Guard заблокировал ответ, ничего не отправлено: {why}",
    "no_restaurant": "Менеджер не назвал ресторан, подтверждение ушло с «at your restaurant».",
    "send_failed": "Не удалось отправить сообщение #{n}: {error}",
    "send_by_hand": "Подтверждение #{n} не дошло до менеджера ({error}), бронь не подтверждена. Отправь ему "
                    "вручную этот текст:\n{text}",
    "mirror.voice": "голосовое",
    "mirror.manager": "менеджер",
    "mirror.no_text": "(без текста)",
    "mirror.agent": "агент #{n}{voice}: {text}",
    "mirror.voice_tag": " (голос)",
    "start": "▶️ Старт: режим {mode}, менеджер @{peer}. Команды: /pause /resume /stop /status",
    "time_up": "Время вышло ({until} Лондон), бронь не подтверждена.",
    "turn_error": "Ошибка в ходе {kind}: {error}. Агент продолжает.",
    "end": "⏹ Конец: {reason}. Лог: {log}",
    "end.stop": "команда /stop",
    "end.linger": "после подтверждения {minutes} мин без сообщений",
    "end.until": "время вышло",
    "end.other": "работа завершена",
}

TABLES = {"uk": UK, "ru": RU}
_from_env_file: str | None = None


def use(lang: str | None) -> None:
    """OWNER_LANG from .env; the process environment, when set, still wins."""
    global _from_env_file
    _from_env_file = (lang or "").strip().lower() or None


def lang() -> str:
    v = (os.environ.get("OWNER_LANG") or _from_env_file or "uk").strip().lower()
    return v if v in TABLES else "uk"


def t(key: str, **kw) -> str:
    text = TABLES[lang()].get(key) or UK[key]
    return text.format(**kw) if kw else text


def has(key: str) -> bool:
    return key in UK
