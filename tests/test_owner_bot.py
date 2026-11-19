import asyncio
import json

import httpx
import pytest

from agent.owner_bot import OwnerBot, OwnerBotError

OWNER = 5550001
TOKEN = "123456:SECRET-TOKEN-VALUE"


def make(handler):
    q = asyncio.Queue()
    return OwnerBot(TOKEN, OWNER, q, http=httpx.AsyncClient(transport=httpx.MockTransport(handler))), q


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def api(req):
    """sendMessage returns a Message, everything else True, as the Bot API does."""
    return ok({"message_id": 1} if req.url.path.endswith("/sendMessage") else True)


def test_ask_with_buttons_and_reply_mapping():
    sent = []

    def handler(req):
        sent.append((req.url.path.rsplit("/", 1)[-1], json.loads(req.content)))
        return ok({"message_id": 42})

    async def go():
        bot, _ = make(handler)
        await bot.ask("alternative_time", "Менеджер предлагает 18:30", [("18:30 подходит", "accept"),
                                                                       ("Только 17:00", "only")])
        ev = await bot.handle_update({"update_id": 1, "message": {"chat": {"id": OWNER}, "text": "лучше 17:00",
                                                                  "reply_to_message": {"message_id": 42}}})
        return bot, ev
    bot, ev = asyncio.run(go())
    method, body = sent[0]
    assert method == "sendMessage" and body["chat_id"] == OWNER
    assert body["reply_markup"]["inline_keyboard"][0][1]["callback_data"] == "alternative_time|only"
    assert ev.kind == "owner_answer" and ev.key == "alternative_time" and ev.text == "лучше 17:00"


def test_other_chats_ignored():
    async def go():
        bot, _ = make(api)
        a = await bot.handle_update({"update_id": 1, "message": {"chat": {"id": 999}, "text": "/stop"}})
        b = await bot.handle_update({"update_id": 2, "callback_query": {
            "id": "c", "from": {"id": 999}, "data": "alternative_time|accept",
            "message": {"message_id": 1, "chat": {"id": OWNER}}}})
        return a, b
    assert asyncio.run(go()) == (None, None)


def test_button_contact_and_commands():
    async def go():
        bot, _ = make(api)
        btn = await bot.handle_update({"update_id": 1, "callback_query": {
            "id": "c", "from": {"id": OWNER}, "data": "alternative_time|accept",
            "message": {"message_id": 7, "chat": {"id": OWNER}, "reply_markup": {"inline_keyboard": [[
                {"text": "18:30 подходит", "callback_data": "alternative_time|accept"}]]}}}})
        contact = await bot.handle_update({"update_id": 2, "message": {
            "chat": {"id": OWNER}, "contact": {"phone_number": "447700900123"}}})
        stop = await bot.handle_update({"update_id": 3, "message": {"chat": {"id": OWNER}, "text": "/stop"}})
        return btn, contact, stop
    btn, contact, stop = asyncio.run(go())
    assert (btn.key, btn.value, btn.text) == ("alternative_time", "accept", "18:30 подходит")
    assert contact.key == "phone" and contact.contact and contact.text == "447700900123"
    assert stop.kind == "owner_command" and stop.text == "stop"


def test_errors_never_carry_the_token():
    def handler(req):
        raise httpx.ConnectError(f"cannot reach {req.url}")

    async def go():
        bot, _ = make(handler)
        with pytest.raises(OwnerBotError) as e:
            await bot.notify("hi")
        return str(e.value)
    msg = asyncio.run(go())
    assert TOKEN not in msg and "SECRET" not in msg
    assert "OwnerBot(chat_id=" in repr(make(api)[0])


def test_skip_old_updates():
    def handler(req):
        return ok([{"update_id": 77, "message": {"chat": {"id": OWNER}, "text": "old"}}])

    async def go():
        bot, _ = make(handler)
        await bot.skip_old()
        return bot.offset
    assert asyncio.run(go()) == 78


def test_default_client_goes_over_ipv4():
    async def go():
        bot = OwnerBot(TOKEN, OWNER, asyncio.Queue())
        transport = bot.http._transport
        await bot.close()
        return transport

    transport = asyncio.run(go())
    assert isinstance(transport, httpx.AsyncHTTPTransport)
    assert transport._pool._local_address == "0.0.0.0"  # binds to IPv4, so only A records are tried


def test_a_lone_slash_or_an_empty_command_does_not_raise():
    async def go():
        bot, _ = make(api)
        return [await bot.handle_update({"update_id": i, "message": {"chat": {"id": OWNER}, "text": t}})
                for i, t in enumerate(["/", "/ ", "/@yk_booking_console_bot", "//"])]
    assert asyncio.run(go()) == [None, None, None, None]


def test_an_update_that_raises_is_logged_and_skipped_and_polling_goes_on():
    notes, calls = [], []

    def handler(req):
        if req.url.path.endswith("/getUpdates"):
            calls.append(1)
            if len(calls) == 1:
                return ok([{"update_id": 10, "message": {"chat": {"id": OWNER}, "text": "boom"}},
                           {"update_id": 11, "message": {"chat": {"id": OWNER}, "text": "Kostyk"}}])
            bot.running = False
            return ok([])
        return api(req)

    bot, q = make(handler)
    bot.on_note = notes.append
    real = bot.handle_update

    async def flaky(u):
        if u["update_id"] == 10:
            raise KeyError("message_id")
        return await real(u)

    bot.handle_update = flaky
    asyncio.run(bot.poll())
    assert q.qsize() == 1 and q.get_nowait().text == "Kostyk" and bot.offset == 12
    assert len(notes) == 1 and "update 10 skipped" in notes[0] and TOKEN not in notes[0]


def test_the_poller_is_restarted_when_it_ends():
    notes, runs = [], []

    async def go():
        bot, q = make(api)
        bot.on_note = notes.append

        async def poll():
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("odd")
            if len(runs) == 2:
                return  # ended by itself
            await q.put("polling again")
            await bot.close()

        bot.poll = poll
        await asyncio.wait_for(bot.poll_forever(delay=0), timeout=2)
        return q.get_nowait()

    assert asyncio.run(go()) == "polling again" and len(runs) == 3
    assert [n.split(" (")[0] for n in notes] == ["owner bot poller ended", "owner bot poller ended"]
    assert "RuntimeError: odd" in notes[0] and "returned" in notes[1]
