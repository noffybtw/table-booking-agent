# Table-booking agent

A Telegram agent that books a table for two in a London restaurant by writing to the manager from my second Telegram account, @alexithymia0x. It writes in English in the first person, as the one booking, without introducing itself, and names nobody unless the manager asks for a name for the booking. Asked directly whether it is a bot, it says it is an AI making the booking. It answers everything the manager says, and after the manager agrees it sends one restatement: time, date, number of people, restaurant. Anything it does not know (the name for the booking, a phone number, whether a later time is fine) it asks me in a separate bot, @yk_booking_console_bot, and waits for my answer. Voice notes work both ways.

## Run

Python 3.12, `pip install -r requirements.txt` (the versions it was tested with), `playwright install chromium`, and ffmpeg with libopus on the PATH.

`.env` in the project root, the keys as in `.env.example`:

| key | what |
|---|---|
| `GROQ_API_KEY` | model, speech-to-text, Orpheus voice |
| `TG_API_ID`, `TG_API_HASH`, `TG_SESSION` | the account the agent writes from, @alexithymia0x (session file path or a StringSession) |
| `OWNER_BOT_TOKEN`, `OWNER_CHAT_ID` | the bot where the agent asks me things (@yk_booking_console_bot); its token is kept only in `.env` |
| `OWNER_LANG` | language of the bot messages to me: `uk` (default) or `ru`; my answers are understood in Ukrainian, Russian and English either way |

```
python run.py --mode sim --scenario all            # 21 scripted managers, offline, scripted model
python run.py --mode sim --scenario happy --llm groq   # the same scripts against the real model
python run.py --mode dry                           # you type as the manager, the real model answers, nothing is sent
python run.py --mode test --now 2026-11-19T08:00   # Telegram, @noffybtw (my main account) plays the manager
python run.py --mode live --live-confirm vlad_dev_7    # the real manager
python run.py --mode preflight                     # Telegram checks that write to nobody, report in runs/preflight-<id>/
python -m pytest -q                                # offline tests; -m groq adds one run on the real model
python -m agent.render runs/<run-id>               # transcript PNG of any run
```

- `sim` plays a scripted manager (and me, and the clock) against the real engine on a virtual clock. Each step has expectations, for example "the owner was asked for the phone", "nothing with 18:30 was sent", "the restatement went as a voice note".
- `dry` reads lines from the console: a plain line is a manager message; `voice: ...`, `edit: ...`, `media: a sticker`, `owner: ...`, `owner phone: ...`, `/stop` cover the rest. `--tts` writes real voice files to `runs/<id>/raw/`.
- `test` talks only to usernames in `ALLOWED_TEST_PEERS` (`agent/config.py`). `--now` moves "today" so an evening test still books a 17:00 that has not passed.
- `preflight` uses the agent's session under the same lock, checks that it is the agent account, resolves @noffybtw and @vlad_dev_7 without sending them anything, sends a short voice note only to the agent's own Saved Messages, downloads it and runs it through speech-to-text, and calls `getMe` on my bot. It exits 0 only if every check passes.
- `live` refuses unless the username is typed again in `--live-confirm`, the clock is real, and it is not later than 16:30 in London.
- `--profile <file>` (dry, test, live; off by default) reads a JSON file kept outside the repo with any of `guest_name` (Latin letters), `phone`, `accept_from` and `accept_to` (HH:MM, London, today). The agent then gives the name or phone as soon as the manager asks, without asking me, and takes another time inside that window without the buttons; a time outside it, or one it cannot read for sure ("6:30", "half six", "7"), still comes to me. The bot's start message lists what the profile holds, and the phone is masked in the logs and on screen.
- Both Telegram modes take a lock in `state/`, so two processes never share the session. They do not start before 08:00 London or after `--until` (17:00 London by default) and stop there if the booking is not confirmed; after the restatement they stay until 60 minutes (`--linger`) pass with no message from the manager or me.

Each run gets a new `runs/<mode>-<time>/` folder with `decisions.md`, `events.jsonl` and, for Telegram runs, `transcript.png`.

## Rerun at the interview

- `python3 run.py --mode live --live-confirm vlad_dev_7`, or `--mode test` to play the manager from @noffybtw (the live limits are under Run).
- In @yk_booking_console_bot press the button under a question, or reply to the question message with the answer (a name in Latin letters).
- To stop: `/stop` in the bot, or Ctrl-C in the terminal.

## How a turn works

1. Telethon gives only messages from the one manager chat. The account is shared with another tool, so the handlers are bound to that chat id and checked again; other chats are not read, answered or marked as read. Messages from before the opening are ignored.
2. Voice notes go through Groq `whisper-large-v3` first. A burst of messages becomes one turn (5 s after the last one, 15 s at most).
3. The goal is fixed in code: two people, 17:00, today's date in London (`agent/clock.py`). The model call is stateless: system prompt + known facts + the whole chat + the new event. `openai/gpt-oss-120b` on Groq answers in a strict JSON schema: what it understood, intent, slot values, what to ask me, action, reply, and a one-line reason. `qwen/qwen3.8-27b` is the fallback on 429, timeouts and 5xx. I compared both on one scripted chat first: gpt-oss-120b invented nothing in 11 turns; qwen once claimed it had watched a football match.
4. Code, not the model, decides what is accepted:
   - a restaurant name or an offered time counts only if it appears in the manager's own words;
   - any time other than 17:00 goes to me with two buttons ("18:30 works" / "Only 17:00"); a typed yes or no counts too, anything else brings the buttons back; the agent never accepts another time alone;
   - name, phone or email go to me only if the manager asked for them, and the manager is never asked for them. When one message asks for several, my answers go back together in one message (or what I answered goes after 10 minutes);
   - the final restatement is built by code from confirmed slots. If the manager agreed but never said the restaurant's name, the agent asks for it once; with no name after that it restates "at your restaurant" and tells me. While a name or phone the manager asked for is still with me, only a holding line goes, and the restatement follows my answer;
   - after the restatement the agent answers one thank-you or goodbye. Further thanks, goodbyes and emoji get no reply, and the decision line says why. Questions and changes always get an answer.
5. Every outgoing text goes through `agent/guard.py`. It fixes long dashes and curly quotes and blocks: phone numbers and emails that are not mine, times and dates nobody said, a party size other than two, capitalised names that are not in the facts or the chat, my name unless I gave it as the booking name, payment promises, and first-person experience ("I did catch it", "I've been there"). A clause with "assistant", "the guest", "my client" or "on behalf of" is cut before the guard (kept only in the answer to whether it is a bot), and `decisions.md` says so. A blocked draft is rewritten once with the problem named, then tried on the fallback model, then replaced by a safe template, and I get a note. A blocked text is never sent.
6. If I don't answer, the agent reminds me after 3 minutes and after 10 tells the manager it is still confirming. If the manager goes quiet, it sends one follow-up after 10 minutes and a second after 30, then only waits. After the manager has agreed, silence gets the confirmation instead; if the agreement was not clear, the agent asks once to confirm before it restates.
7. My bot also gets a silent copy of every message and takes `/pause`, `/resume`, `/stop`, `/status`.

## Voice

When the manager sends a voice note, the reply is a voice note with the same text as its caption, so the screenshot stays readable. The restatement always goes as a voice note. TTS is edge-tts `en-GB-RyanNeural`, with Groq `canopylabs/orpheus-v1-english` (voice `daniel`) as the fallback, converted to OGG/Opus by ffmpeg. If both fail, the same text goes as a normal message. The voice reads the text after the guard has checked it.

## Logs

`decisions.md` has one line per message, written by the agent as it runs (these lines are from `--mode sim --scenario name_phone`):

```
08:00:30 IN manager: "Good morning! Yes, we can do 17:00 for two. What name shall I put it under?" | understood: asks info | decided: name not known, ask the owner
08:00:30 OWNER asked (guest_name): what name to book under
08:00:30 OUT agent #2: "Wonderful, thank you. Let me just check the name, I'll be back in a moment." | why: name not known, ask the owner
08:02:00 OWNER answered guest_name: name "Yaroslav", used in reply #5
08:02:30 OWNER answered phone: +44 77** ***123, used in reply #6
```

The bot talks to me in Ukrainian; `decisions.md` names each question and answer in English by its key, and quotes the manager's messages as written, in any language, with the model's English reading next to each. `events.jsonl` keeps the original wording and everything else: each model call with model, latency, tokens and rate-limit headers, each guard result, each timer. Phone numbers are masked in both files, and values from `.env` are scrubbed before anything is written.

## Limits

- The guard is regular expressions. It sometimes blocks a harmless capitalised word; then the rewrite or the template goes out and I'm asked what to say. The model's understanding of the chat is not checked, only what it sends.
- While I was building it, edge-tts 7.2.8 returned 403 for every voice; 7.3.1 fixed that and is the version in `requirements.txt`. Orpheus works only after its terms are accepted in the Groq console. If both fail, the text goes as a normal message.
- Groq's free tier is 8k tokens a minute per model, about four turns a minute on gpt-oss-120b. Fast bursts are merged into one turn, and a busy primary hands over to the fallback.
- One manager, one booking, English replies only. The booking card widget for the owner bot (`--widgets`) is not built.
