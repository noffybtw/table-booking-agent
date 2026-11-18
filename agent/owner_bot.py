"""The owner channel: a separate Telegram bot (Bot API over httpx long polling) that talks only to OWNER_CHAT_ID.

Questions go out in Russian with buttons when there is a choice; answers come back as owner_answer events.
/pause, /resume, /stop and /status are commands. Every conversation line is mirrored here, silently.
"""
from __future__ import annotations

import asyncio

import httpx

from agent.events import Event

API = "https://api.telegram.org"
COMMANDS = {"pause", "resume", "stop", "status"}


class OwnerBotError(Exception):
    """Carries the method and status only: httpx errors include the URL, and the URL includes the token."""


class OwnerBot:
    def __init__(self, token: str, chat_id: int, queue: asyncio.Queue, http: httpx.AsyncClient | None = None,
                 poll_timeout: int = 25):
        self._base = f"{API}/bot{token}"
        self.chat_id = int(chat_id)
        self.queue = queue
        # IPv6 from the host to api.telegram.org drops SYNs (measured on the VPS 04.10.2026), IPv4 answers at once
        self.http = http or httpx.AsyncClient(timeout=poll_timeout + 10,
                                              transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"))
        self.poll_timeout = poll_timeout
        self.offset: int | None = None
        self.asked: dict[int, str] = {}  # message_id of a question -> key
        self.running = False

    def __repr__(self) -> str:
        return f"OwnerBot(chat_id={self.chat_id})"

    async def call(self, method: str, **params):
        try:
            r = await self.http.post(f"{self._base}/{method}", json=params)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise OwnerBotError(f"{method}: {type(e).__name__}") from None
        if not data.get("ok"):
            raise OwnerBotError(f"{method}: {r.status_code} {str(data.get('description', ''))[:120]}")
        return data["result"]

    # ---------- out ----------

    async def send(self, text: str, silent: bool = False, markup: dict | None = None) -> int:
        params = {"chat_id": self.chat_id, "text": text[:4000], "disable_notification": silent}
        if markup:
            params["reply_markup"] = markup
        msg = await self.call("sendMessage", **params)
        return msg.get("message_id") if isinstance(msg, dict) else None

    async def ask(self, key: str, question: str, buttons: list | None = None, contact: bool = False) -> None:
        markup = None
        if buttons:
            markup = {"inline_keyboard": [[{"text": label, "callback_data": f"{key}|{value}"[:64]}
                                           for label, value in buttons]]}
        elif contact:
            markup = {"keyboard": [[{"text": "Отправить мой номер", "request_contact": True}]],
                      "one_time_keyboard": True, "resize_keyboard": True}
        mid = await self.send(f"❓ {question}\n\n(ответь реплаем на это сообщение)", markup=markup)
        self.asked[mid] = key

    async def notify(self, text: str) -> None:
        await self.send(f"ℹ️ {text}")

    async def mirror(self, line: str) -> None:
        await self.send(line, silent=True)

    # ---------- in ----------

    async def skip_old(self) -> None:
        """Answers typed before this run must not be taken as answers to its questions."""
        old = await self.call("getUpdates", offset=-1, timeout=0)
        self.offset = old[-1]["update_id"] + 1 if old else None

    async def poll(self) -> None:
        self.running = True
        while self.running:
            try:
                params = {"timeout": self.poll_timeout, "allowed_updates": ["message", "callback_query"]}
                if self.offset is not None:
                    params["offset"] = self.offset
                updates = await self.call("getUpdates", **params)
            except OwnerBotError:
                await asyncio.sleep(5)
                continue
            for u in updates:
                self.offset = u["update_id"] + 1
                ev = await self.handle_update(u)
                if ev is not None:
                    await self.queue.put(ev)

    async def handle_update(self, u: dict) -> Event | None:
        if "callback_query" in u:
            cq = u["callback_query"]
            msg = cq.get("message") or {}
            if (msg.get("chat") or {}).get("id") != self.chat_id or (cq.get("from") or {}).get("id") != self.chat_id:
                return None
            key, _, value = (cq.get("data") or "").partition("|")
            label = next((b["text"] for row in (msg.get("reply_markup") or {}).get("inline_keyboard", [])
                          for b in row if b.get("callback_data") == cq.get("data")), value)
            try:
                await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Принято")
                await self.call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=msg["message_id"],
                                reply_markup={"inline_keyboard": []})
            except OwnerBotError:
                pass
            return Event("owner_answer", key=key, value=value, text=label)
        msg = u.get("message")
        if not msg or (msg.get("chat") or {}).get("id") != self.chat_id:
            return None
        if "contact" in msg:
            phone = msg["contact"].get("phone_number", "")
            try:
                await self.send("Номер получен.", markup={"remove_keyboard": True})
            except OwnerBotError:
                pass
            return Event("owner_answer", key="phone", text=phone, contact=True)
        text = (msg.get("text") or "").strip()
        if not text:
            return None
        if text.startswith("/"):
            cmd = text[1:].split()[0].split("@")[0].lower()
            if cmd in COMMANDS:
                return Event("owner_command", text=cmd)
            return None
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        return Event("owner_answer", key=self.asked.get(reply_to), text=text)

    async def close(self) -> None:
        self.running = False
        await self.http.aclose()
