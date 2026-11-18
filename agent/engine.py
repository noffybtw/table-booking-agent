"""Transport-agnostic core: one Event in, a list of Actions out.

All conversation state lives here; the model call is stateless (facts + whole chat + the new event). The model
proposes a reply and slot values, the code decides what is accepted, what goes to the owner and what is sent.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from agent import clock, guard, policy, restate
from agent.brain import BrainDown, Decision
from agent.config import Timings
from agent.events import AskOwner, Event, Log, Msg, NotifyOwner, Schedule, SendText, SendVoice, Stop
from agent.facts import FactStore

NEW, OPEN, CONFIRMED = "new", "open", "confirmed"

OPENING = "{greeting}. I'm writing on behalf of {who} - could I book a table for two today at {time}, please?"
HOLD = "Let me just check that with {who}, I'll be back in a moment."
STILL_CHECKING = "Thank you for bearing with me, I'm still checking that with {who}."
SORRY_WAIT = "Sorry for the wait, I'm still confirming that detail."
NAME_ONLY = " Would it be all right to hold the table under the name {name} for now?"
FOLLOW_UP_1 = "Sorry to chase - just checking whether a table for two at {time} today might be possible?"
FOLLOW_UP_2 = "Hello again. Whenever you have a moment, I'd be grateful for a quick reply about the table for two at {time} today."
BAD_VOICE = "Sorry, I couldn't play that voice note. Could you type it, please?"
TECH_HOLD = "Thank you - give me just a moment, please."
CLOSING = "Thank you very much, have a lovely day."

MANAGER_LABEL = {"text": "MANAGER", "voice": "MANAGER (voice note, transcribed)", "media": "MANAGER"}


class Engine:
    def __init__(self, brain, facts: FactStore, now: Callable[[], datetime] | None = None,
                 timings: Timings | None = None, voice_restatement: bool = True):
        self.brain = brain
        self.facts = facts
        self.now = now or (lambda: clock.london_now())
        self.t = timings or Timings()
        self.voice_restatement = voice_restatement
        self.phase = NEW
        self.transcript: list[str] = []
        self.chat_texts: list[str] = []      # what the manager and the owner wrote; the guard trusts only these
        self.manager_texts: list[str] = []
        self.pending: dict[str, dict] = {}   # owner questions waiting for an answer
        self.declined_times: set[str] = set()
        self.agreed = False
        self.asked_restaurant = False
        self.restated_sig: tuple | None = None
        self.n_out = self.n_in = self.turn = 0
        self.followups = 0
        self.gen: dict[str, int] = {"silence": 0}
        self.paused = self.stopped = False
        self.held: list[Event] = []
        self.last_voice = False
        self.retry: tuple[str, bool] | None = None
        self.brain_down_sent = False

    # ---------- public ----------

    @property
    def who(self) -> str:
        return str(self.facts.get("on_behalf_of", "my client"))

    def handle(self, ev: Event) -> list:
        if ev.kind == "owner_command":
            return self._command(ev.text)
        if self.stopped:
            return [Log("note", {"text": f"stopped, {ev.kind} ignored"})]
        if self.paused:
            if ev.kind == "timer":
                return [Log("note", {"text": f"paused, timer {ev.key or ev.text} dropped"})]
            self.held.append(ev)
            return [Log("note", {"text": f"paused, {ev.kind} kept for later"})]
        self.turn += 1
        if ev.kind == "opening":
            return self._opening()
        if ev.kind in ("manager_message", "manager_voice", "manager_media", "manager_edit"):
            return self._manager(ev)
        if ev.kind == "owner_answer":
            return self._owner(ev)
        if ev.kind == "timer":
            return self._timer(ev)
        return [Log("note", {"text": f"unknown event {ev.kind}"})]

    def state_text(self) -> str:
        pend = ", ".join(self.pending) or "none"
        return (f"STATE: phase {self.phase}; manager agreed: {'yes' if self.agreed else 'no'}; "
                f"pending owner questions: {pend}; final restatement sent: {'yes' if self.restated_sig else 'no'}.")

    # ---------- events ----------

    def _opening(self) -> list:
        now = self.now()
        tmpl = OPENING.format(greeting=clock.greeting(now), who=self.who, time=self.facts.get("time"))
        event = (f"Open the conversation now. In London it is {clock.hhmm(now)}, so greet with "
                 f"'{clock.greeting(now)}'. Say you are writing on behalf of {self.who} and ask for a table for two "
                 f"today at {self.facts.get('time')}. One message, for example: \"{tmpl}\"")
        acts: list = []
        d = self._decide(event, acts)
        draft = d.reply if d and d.reply.strip() else tmpl
        why = "opening, " + self._slot_summary()
        acts += self._say(draft, event, why, fallback=tmpl)
        self.phase = OPEN
        acts += self._arm_silence()
        return acts

    def _manager(self, ev: Event) -> list:
        acts: list = []
        msgs = ev.messages or [Msg(ev.text, ev.msg_id)]
        self.gen["silence"] += 1
        self.followups = 0
        failed_voice = False
        for m in msgs:
            self.n_in += 1
            if ev.kind == "manager_edit":
                line = f"MANAGER (edited an earlier message; it was: \"{ev.old_text}\"): {m.text}"
            elif m.kind == "voice" and not m.text:
                line, failed_voice = "MANAGER sent a voice note that could not be transcribed", True
            elif m.kind == "media":
                line = f"MANAGER sent {m.text or 'a sticker or photo'} (no text)"
            else:
                line = f"{MANAGER_LABEL.get(m.kind, 'MANAGER')}: {m.text}"
            self.transcript.append(line)
            if m.text and m.kind != "media":
                self.manager_texts.append(m.text)
                self.chat_texts.append(m.text)
            if ev.kind == "manager_edit" and ev.old_text:
                self.chat_texts.append(ev.old_text)
            acts.append(Log("in", {"n": self.n_in, "kind": "edit" if ev.kind == "manager_edit" else m.kind,
                                   "text": m.text or "", "old_text": ev.old_text or None, "lang": m.lang,
                                   "error": m.error}))
        self.last_voice = any(m.kind == "voice" for m in msgs)

        if failed_voice and all(m.kind == "voice" and not m.text for m in msgs):
            acts.append(Log("decision", {"understood": "voice note I could not transcribe",
                                         "decided": "ask the manager to type it (template, no model call)"}))
            return acts + self._say(BAD_VOICE, "", "voice note could not be transcribed", fallback=BAD_VOICE)

        if ev.kind == "manager_edit":
            event = ("The manager edited an earlier message (see the MANAGER (edited...) line). If a booking fact "
                     "changed, say what changed and re-confirm it; another time is the owner's decision.")
        elif ev.kind == "manager_media":
            event = "The manager sent a sticker or photo with no text. Ask one short clarifying question."
        else:
            event = "New message(s) from the manager above. React to all of it in one reply."
        return acts + self._respond(event, self.last_voice)

    def _owner(self, ev: Event) -> list:
        acts: list = []
        key = policy.norm_key(ev.key) if ev.key else (next(iter(self.pending)) if self.pending else "owner_note")
        text = (ev.text or ev.value or "").strip()
        offered = self.facts.get("offered_time")
        if key == "owner_note":
            self.facts.set("owner_note", text, "owner", self.turn)
            line = text
        else:
            line = policy.apply_owner_answer(self.facts, key, text, ev.value, ev.contact, self.turn)
        if key == "alternative_time" and offered:
            if self.facts.get("time") == offered:
                # the manager offered it and the owner took it: the new time is agreed on both sides
                self.facts.confirm("time")
            else:
                self.declined_times.add(offered)
            self.facts.drop("offered_time")
        self.pending.pop(key, None)
        self.gen[f"owner:{key}"] = self.gen.get(f"owner:{key}", 0) + 1
        self.transcript.append(f"OWNER (private, the manager cannot see this): {line}")
        if text:
            self.chat_texts.append(text)
        self.chat_texts.append(line)
        n_before = self.n_out
        event = (f"{self.who} answered privately (the OWNER line above). Continue with the manager using it; "
                 f"do not ask the manager anything you already know.")
        acts += self._respond(event, self.last_voice)
        used = self.n_out if self.n_out > n_before else None
        acts.insert(0, Log("owner_answer", {"key": key, "text": text, "line": line, "used_in": n_before + 1 if used else None}))
        return acts

    def _timer(self, ev: Event) -> list:
        name = ev.text
        if name == "silence":
            if ev.gen != self.gen["silence"] or self.pending or self.phase == CONFIRMED:
                return []
            t = self.facts.get("time")
            if self.followups == 0:
                self.followups = 1
                acts = self._say(FOLLOW_UP_1.format(time=t), "", "no reply for 10 minutes, one polite follow-up",
                                 fallback=FOLLOW_UP_1.format(time=t), rearm=False)
                return acts + [Schedule("silence", self.t.silence_second - self.t.silence_first, ev.gen)]
            if self.followups == 1:
                self.followups = 2
                acts = self._say(FOLLOW_UP_2.format(time=t), "", "no reply for 30 minutes, second and last follow-up",
                                 fallback=FOLLOW_UP_2.format(time=t), rearm=False)
                return acts + [NotifyOwner("Менеджер молчит 30 минут. Больше не напоминаю, жду ответа."),
                               Log("note", {"text": "silence: no more follow-ups, waiting"})]
            return []
        if name in ("owner_reping", "owner_wait"):
            key = ev.key or ""
            if key not in self.pending or ev.gen != self.gen.get(f"owner:{key}"):
                return []
            if name == "owner_reping":
                q, buttons, contact = policy.owner_question(key, self.facts, self.pending[key]["said"])
                return [AskOwner(key, "Напоминаю: " + q, buttons, contact, reminder=True)]
            if self.pending[key].get("apologised"):
                return []
            self.pending[key]["apologised"] = True
            text = SORRY_WAIT
            if key in policy.OPTIONAL_KEYS and self.facts.get("guest_name"):
                text += NAME_ONLY.format(name=self.facts.get("guest_name"))
            return self._say(text, "", f"owner has not answered {key} for 10 minutes", fallback=SORRY_WAIT,
                             rearm=False)
        if name == "brain_retry":
            if not self.retry or ev.gen != self.gen.get("retry"):
                return []
            event, voice = self.retry
            return self._respond(event, voice)
        return [Log("note", {"text": f"unknown timer {name}"})]

    def _command(self, cmd: str) -> list:
        cmd = (cmd or "").strip().lower().lstrip("/")
        if cmd == "stop":
            self.stopped = True
            return [Log("note", {"text": "owner: /stop, the agent sends nothing more"}), Stop("owner /stop")]
        if cmd == "status":
            return [NotifyOwner(f"{self.state_text()} Сообщений: от менеджера {self.n_in}, от агента {self.n_out}.")]
        if cmd == "pause":
            self.paused = True
            return [Log("note", {"text": "owner: /pause"})]
        if cmd == "resume":
            self.paused = False
            acts: list = [Log("note", {"text": f"owner: /resume, {len(self.held)} held event(s)"})]
            held, self.held = self.held, []
            for ev in held:
                acts += self.handle(ev)
            if not held:
                acts += self._arm_silence()
            return acts
        return [Log("note", {"text": f"unknown owner command {cmd!r}"})]

    # ---------- the turn ----------

    def _decide(self, event: str, acts: list, note: str | None = None, prefer_fallback: bool = False) -> Decision | None:
        try:
            d = self.brain.decide(self.facts, self.transcript, event, self.state_text(), note=note,
                                  prefer_fallback=prefer_fallback)
        except BrainDown as e:
            acts.append(Log("model", dict(getattr(self.brain, "last_meta", {}) or {}, ok=False, error=str(e))))
            return None
        acts.append(Log("model", dict(getattr(self.brain, "last_meta", {}) or {}, ok=True, action=d.action,
                                      intent=d.intent, needs_owner=d.needs_owner, reply=d.reply,
                                      slots=d.slots.model_dump(exclude_none=True), note=note)))
        return d

    def _respond(self, event: str, voice: bool) -> list:
        acts: list = []
        d = self._decide(event, acts)
        if d is None:
            self.retry = (event, voice)
            acts.append(Log("decision", {"understood": "-", "decided": "model unavailable, retry in "
                                         f"{self.t.brain_retry:.0f}s"}))
            self.gen["retry"] = self.gen.get("retry", 0) + 1  # one pending retry at a time
            acts.append(Schedule("brain_retry", self.t.brain_retry, self.gen["retry"]))
            if not self.brain_down_sent:
                self.brain_down_sent = True
                acts.append(NotifyOwner("Groq недоступен, агент ждёт и пробует снова каждые 30 с."))
                acts += self._say(TECH_HOLD, "", "model unavailable, holding line from a template",
                                  fallback=TECH_HOLD, rearm=False)
            return acts
        self.retry = None
        self.brain_down_sent = False
        return acts + self._apply(d, event, voice)

    def _apply(self, d: Decision, event: str, voice: bool) -> list:
        acts: list = []
        f = self.facts
        notes: list[str] = []

        ok, rejected = policy.accepted_slots(d.slots, f, self.manager_texts)
        if ok.get("offered_time") in self.declined_times:
            ok.pop("offered_time")
        for k, v in ok.items():
            if f.get(k) != v:
                f.set(k, v, "manager", self.turn, confirmed=(k == "restaurant_name"))
                notes.append(f"{k}={v} (from the manager)")
        notes += [f"rejected {r}" for r in rejected]

        offered = f.get("offered_time")
        time_open = bool(offered and offered != f.get("time"))
        needs = []
        for k in d.needs_owner:
            k = policy.norm_key(k)
            if k in needs:
                continue
            if not policy.manager_asked(k, self.manager_texts):
                notes.append(f"not asking the owner for {k}: the manager did not ask for it")
                continue
            needs.append(k)
        if time_open and "alternative_time" not in needs and "alternative_time" not in self.pending:
            needs.append("alternative_time")
            notes.append(f"manager offered {offered}: another time is {self.who}'s call, asking")

        agreed_now = d.action == "final_restatement" or d.slots.manager_agreed
        if agreed_now and (time_open or "alternative_time" in self.pending):
            agreed_now = False
            notes.append("agreement ignored while another time is open")
        if agreed_now:
            self.agreed = True
            for k in ("time", "date", "party_size"):
                f.confirm(k)

        new_keys = [k for k in needs if k not in self.pending and not (k in ("guest_name", "phone") and f.get(k))]
        last = self.manager_texts[-1] if self.manager_texts else ""
        acts.append(Log("decision", {"understood": d.understood, "intent": d.intent,
                                     "decided": d.decision + (f" [code: {'; '.join(notes)}]" if notes else ""),
                                     "action": d.action, "owner_keys": new_keys}))
        for k in new_keys:
            q, buttons, contact = policy.owner_question(k, f, last)
            self.pending[k] = {"said": last, "turn": self.turn}
            g = self.gen[f"owner:{k}"] = self.gen.get(f"owner:{k}", 0) + 1
            acts += [AskOwner(k, q, buttons, contact), Log("owner_question", {"key": k, "question": q}),
                     Schedule("owner_reping", self.t.owner_reping, g, k), Schedule("owner_wait", self.t.owner_wait, g, k)]


        reply = d.reply.strip()
        sig = restate.signature(f)
        restate_due = self.agreed and (self.restated_sig is None or (self.phase == CONFIRMED and sig != self.restated_sig))
        if restate_due:
            text = restate.build(f, variant=self.turn, correction=self.restated_sig is not None)
            if text:
                acts += self._restate(text, sig)
                if new_keys and reply:
                    acts += self._say(reply, event, "holding line for the owner question", fallback=self._hold())
                return acts
            if "restaurant_name" in restate.missing(f):
                if reply and policy.asks_restaurant(reply):
                    acts += self._say(reply, event, "agreed; restaurant name unknown, asking before the restatement",
                                      fallback=restate.ASK_RESTAURANT, voice=voice)
                elif not self.asked_restaurant:
                    acts += self._say(restate.ASK_RESTAURANT, event,
                                      "agreed; restaurant name unknown, asking before the restatement",
                                      fallback=restate.ASK_RESTAURANT, voice=voice)
                elif reply:
                    acts += self._say(reply, event, d.decision, fallback=restate.ASK_RESTAURANT, voice=voice)
                self.asked_restaurant = True
                return acts

        if not reply and self.phase == CONFIRMED and d.action == "final_restatement":
            # already restated and nothing changed: a short closing instead of silence
            return acts + self._say(CLOSING, "", "booking already restated, short closing", fallback=CLOSING,
                                    voice=voice, rearm=False)
        if d.action in ("wait", "end") or not reply:
            if new_keys:
                acts += self._say(self._hold(), event, "holding line while the owner is asked", fallback=self._hold(),
                                  voice=voice)
            return acts
        why = d.decision or d.action
        fallback = self._hold() if (new_keys or self.pending) else None
        return acts + self._say(reply, event, why, fallback=fallback, voice=voice)

    def _restate(self, text: str, sig: tuple) -> list:
        text = guard.fix(text)
        probs = restate.verify(text, self.facts) + [str(p) for p in guard.check(text, self.facts, self.chat_texts)]
        if probs:
            return [Log("guard", {"text": text, "problems": probs, "result": "restatement blocked"}),
                    NotifyOwner(f"Финальное подтверждение не прошло проверку: {'; '.join(probs)}")]
        self.restated_sig = sig
        self.phase = CONFIRMED
        self.gen["silence"] += 1
        why = "manager agreed; restatement built by code from confirmed slots: " + self._slot_summary()
        self.n_out += 1
        self.transcript.append(f"AGENT: {text}")
        cls = SendVoice if self.voice_restatement else SendText
        return [Log("guard", {"text": text, "problems": [], "result": "ok"}), cls(text, why, self.n_out),
                NotifyOwner(f"Бронь подтверждена: {text}")]

    def _say(self, draft: str, event: str, why: str, fallback: str | None, voice: bool = False,
             rearm: bool = True) -> list:
        """Guard every outgoing text. Blocked: rewrite once naming the problem, then the fallback model, then the
        template. A blocked text is never sent."""
        acts: list = []
        text = guard.fix(draft)
        probs = guard.check(text, self.facts, self.chat_texts) if text else [guard.Problem("empty", "no text")]
        acts.append(Log("guard", {"text": text, "problems": [str(p) for p in probs],
                                  "result": "ok" if not probs else "blocked"}))
        first = probs
        if probs and event:
            for prefer_fallback in (False, True):
                d = self._decide(event, acts, note=guard.describe(probs), prefer_fallback=prefer_fallback)
                if d is None or not d.reply.strip():
                    continue
                text = guard.fix(d.reply)
                probs = guard.check(text, self.facts, self.chat_texts)
                acts.append(Log("guard", {"text": text, "problems": [str(p) for p in probs],
                                          "result": "ok" if not probs else "blocked",
                                          "retry": "fallback model" if prefer_fallback else "rewrite"}))
                if not probs:
                    why += f" (first draft blocked: {guard.describe(first)}; rewritten)"
                    break
        if probs and fallback:
            text = guard.fix(fallback)
            probs = guard.check(text, self.facts, self.chat_texts)
            acts.append(Log("guard", {"text": text, "problems": [str(p) for p in probs],
                                      "result": "ok" if not probs else "blocked", "retry": "template"}))
            if not probs:
                why += f" (drafts blocked: {guard.describe(first)}; template sent)"
                acts.append(NotifyOwner(f"Guard заблокировал ответ модели ({guard.describe(first)}), "
                                        f"отправлен шаблон: «{text}»"))
                if not self.pending and self.phase != NEW and fallback == self._hold():
                    acts += self._ask_help()
        if probs:
            acts.append(NotifyOwner(f"Guard заблокировал ответ, ничего не отправлено: {guard.describe(first)}"))
            return acts
        self.n_out += 1
        self.transcript.append(f"AGENT: {text}")
        acts.append((SendVoice if voice else SendText)(text, why, self.n_out))
        if rearm:
            acts += self._arm_silence()
        return acts

    def _ask_help(self) -> list:
        last = self.manager_texts[-1] if self.manager_texts else ""
        q, buttons, contact = policy.owner_question("reply_help", self.facts, last)
        self.pending["reply_help"] = {"said": last, "turn": self.turn}
        return [AskOwner("reply_help", q, buttons, contact), Log("owner_question", {"key": "reply_help", "question": q})]

    def _hold(self) -> str:
        return HOLD.format(who=self.who)

    def _arm_silence(self) -> list:
        if self.phase == CONFIRMED or self.pending:
            return []
        self.gen["silence"] += 1
        return [Schedule("silence", self.t.silence_first, self.gen["silence"])]

    def _slot_summary(self) -> str:
        f = self.facts
        parts = [f"time={f.get('time')}", f"date={clock.human_date(f.booking_date())}", f"party={f.get('party_size')}"]
        for k in ("restaurant_name", "guest_name", "condition"):
            if f.get(k):
                parts.append(f"{k.split('_')[0]}={f.get(k)}")
        return "slots " + " ".join(parts)
