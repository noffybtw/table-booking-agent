"""Transport-agnostic core: one Event in, a list of Actions out.

All conversation state lives here; the model call is stateless (facts + whole chat + the new event). The model
proposes a reply and slot values, the code decides what is accepted, what goes to the owner and what is sent.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable

from agent import clock, guard, policy, restate, texts
from agent.brain import BrainDown, Decision
from agent.config import Timings
from agent.events import AskOwner, Event, Log, Msg, NotifyOwner, Schedule, SendText, SendVoice, Stop
from agent.facts import FactStore

NEW, OPEN, CONFIRMED = "new", "open", "confirmed"

OPENING = "{greeting}. I'm an assistant - could I book a table for two today at {time}, please?"
HOLD = "Let me just check that and come back to you in a moment."
STILL_CHECKING = "Thank you for bearing with me, I'm still checking that."
SORRY_WAIT = "Sorry for the wait, I'm still confirming that detail."
NAME_ONLY = " Would it be all right to hold the table under the name {name} for now?"
FOLLOW_UP_1 = "Sorry to chase - just checking whether a table for two at {time} today might be possible?"
FOLLOW_UP_CONFIRM = "Sorry to chase - just checking that the table for {people} at {time} today is all confirmed?"
FOLLOW_UP_2 = "Hello again. Whenever you have a moment, I'd be grateful for a quick reply about the table for two at {time} today."
BAD_VOICE = "Sorry, I couldn't play that voice note. Could you type it, please?"
TECH_HOLD = "Thank you - give me just a moment, please."
CLOSING = "Thank you very much, have a lovely day."
ASK_RESTAURANT_ALSO = "Could I also take the restaurant's name for my note?"
CONFIRM_Q = "Is the table for {people} at {time} today all confirmed, then?"
CONFIRM_RE = re.compile(r"\bconfirm\w*[^.!?]*\?", re.I)
# "Please let me know if that works." in front of the confirm question is a second question: cut it
LET_ME_KNOW_RE = re.compile(r"(?:^|[,;]?\s+-?\s*)(?:and\s+)?(?:please\s+)?(?:do\s+)?let me know if that(?:'s| is)? "
                            r"(?:works?|suits?|okay|ok|all right|alright|fine)(?: for you| with you)?\s*[.!]*\s*$", re.I)
DETAIL_KEYS = ("guest_name", "phone")  # asked by the manager and given by the owner; the restatement waits for them
PLEASANTRY = ("closing", "greeting")
WITHDRAWN = ("counter_offer", "fully_booked")  # intents that take an unrestated agreement back

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
        self.unnamed = False                 # asked once, no restaurant name: restate with "at your restaurant"
        self.confirm_asked = False           # "Is the table ... all confirmed, then?" went out, no answer yet
        self.answered: str | None = None     # the owner question just answered; kept until a model reply has used it
        self.restated_sig: tuple | None = None
        self.before_restate: tuple = (None, NEW, False)  # restated_sig, phase, closed before the last restatement
        self.n_restated = 0
        self.closed = False                  # one closing reply went out after the restatement
        self.n_out = self.n_in = self.turn = 0
        self.followups = 0
        self.gen: dict[str, int] = {"silence": 0}
        self.paused = self.stopped = False
        self.held: list[Event] = []
        self.last_voice = False
        self.retry: tuple[str, bool, list[str] | None] | None = None
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
                f"pending owner questions: {pend}; final restatement sent: {'yes' if self.restated_sig else 'no'}; "
                f"closing reply sent: {'yes' if self.closed else 'no'}.")

    def status_text(self) -> str:
        """The same state for the owner, in OWNER_LANG."""
        yes_no = {True: texts.t("yes"), False: texts.t("no")}
        pending = ", ".join(policy.key_name(k) for k in self.pending) or texts.t("nothing")
        return texts.t("status", phase=texts.t(f"phase.{self.phase}"), agreed=yes_no[self.agreed], pending=pending,
                       restated=yes_no[self.restated_sig is not None], n_in=self.n_in, n_out=self.n_out)

    # ---------- events ----------

    def _opening(self) -> list:
        now = self.now()
        tmpl = OPENING.format(greeting=clock.greeting(now), time=self.facts.get("time"))
        event = (f"Open the conversation now. In London it is {clock.hhmm(now)}, so greet with "
                 f"'{clock.greeting(now)}'. Introduce yourself just as an assistant, with no name (do not say AI "
                 f"here), and ask for a table for two today at {self.facts.get('time')}. One message, for example: "
                 f"\"{tmpl}\"")
        acts: list = []
        d = self._decide(event, acts)
        draft = d.reply if d and d.reply.strip() else tmpl
        why = "opening, " + self._slot_summary()
        if re.search(r"\bAI\b", draft):
            # the opening says just "assistant"; "AI" comes only when the manager asks whether it is a bot
            draft, why = tmpl, why + " (model draft said AI, template sent)"
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
        if self.confirm_asked:
            event += (" Your last message asked whether the table is all confirmed: a yes to it is the manager's "
                      "agreement.")
        said = None if ev.kind == "manager_edit" else [m.text or "" for m in msgs]
        return acts + self._respond(event, self.last_voice, said)

    def _owner(self, ev: Event) -> list:
        acts: list = []
        text = (ev.text or ev.value or "").strip()
        if ev.key:
            key = policy.norm_key(ev.key)
        elif self.pending:
            key = policy.owner_key(list(self.pending), text)
            if key is None:
                # several questions are open and the text fits none of them for sure: ask, store nothing
                return [Log("owner_answer", {"key": "?", "text": text,
                                             "result": "not a reply to a question, several are open; asked which"}),
                        NotifyOwner(policy.which_question(list(self.pending)))]
        else:
            # nothing is asked: "ну як там?" must not start a message to the manager. The owner gets the status;
            # a note of 3 words or more waits in the facts for the next model turn, "дякую", "ок" or a question does not.
            if len(text.split()) < 3 or "?" in text:
                return [Log("owner_answer", {"key": "note", "text": text,
                                             "summary": "owner message with no open question, under 3 words or a "
                                                        "question: not kept, status sent to the owner"}),
                        NotifyOwner(texts.t("note_skipped", status=self.status_text()))]
            self.facts.set("owner_note", text, "owner", self.turn)
            self.transcript.append(f"OWNER (private, the manager cannot see this): {text}")
            self.chat_texts.append(text)
            return [Log("owner_answer", {"key": "note", "text": text,
                                         "summary": "owner note with no open question: kept for the next model "
                                                    "turn, status sent to the owner"}),
                    NotifyOwner(texts.t("note_kept", status=self.status_text()))]
        offered, time_before = self.facts.get("offered_time"), self.facts.get("time")
        line = policy.apply_owner_answer(self.facts, key, text, ev.value, ev.contact, self.turn)
        if line is None:
            # the question stays open (and its timers run on); no model turn on an answer that is not taken
            if key == "alternative_time":
                q, buttons, contact = policy.owner_question(key, self.facts, self.pending.get(key, {}).get("said", ""))
                return [Log("owner_answer", {"key": key, "text": text,
                                             "result": "neither yes nor no, question sent again"}),
                        AskOwner(key, f"{texts.t('time_again')} {q}", buttons, contact)]
            return [Log("owner_answer", {"key": key, "text": text, "result": "not in Latin letters, asked again"}),
                    AskOwner(key, texts.t("name_again"), [], False)]
        summary = "owner note, passed to the model"
        if key == "guest_name":
            summary = f'name "{self.facts.get("guest_name")}"'
        elif key == "phone" and self.facts.get("phone") and line.startswith("phone: "):
            summary = line.removeprefix("phone: ")
        if key == "alternative_time" and not offered and self.facts.get("time") != time_before:
            summary = f"accepted {self.facts.get('time')} (typed by the owner, no offer was stored)"
        if key == "alternative_time" and offered:
            if self.facts.get("time") == offered:
                # the manager offered it and the owner took it: the new time is agreed on both sides
                self.facts.confirm("time")
                summary = f"accepted {offered}"
            else:
                self.declined_times.add(offered)  # only on "only" or a no: anything else was asked again above
                summary = f"only {self.facts.get('time')}, {offered} declined"
            self.facts.drop("offered_time")
        self.pending.pop(key, None)
        self.gen[f"owner:{key}"] = self.gen.get(f"owner:{key}", 0) + 1
        self.transcript.append(f"OWNER (private, the manager cannot see this): {line}")
        if text:
            self.chat_texts.append(text)
        self.chat_texts.append(line)
        n_before = self.n_out
        self.answered = key
        event = (f"{self.who} answered privately (the OWNER line above). Continue with the manager using it; "
                 f"do not ask the manager anything you already know.")
        acts += self._respond(event, self.last_voice)
        used = self.n_out if self.n_out > n_before else None
        if summary.startswith("owner note"):
            understood = next((a.data.get("understood") for a in acts if isinstance(a, Log) and a.kind == "decision"), "")
            if understood and understood != "-":
                summary += f"; the model understood: {understood}"
        acts.insert(0, Log("owner_answer", {"key": key, "text": text, "line": line, "summary": summary,
                                            "used_in": n_before + 1 if used else None}))
        return acts

    def _timer(self, ev: Event) -> list:
        name = ev.text
        if name == "silence":
            if ev.gen != self.gen["silence"] or self.pending or self.phase == CONFIRMED:
                return []
            t = self.facts.get("time")
            if self.agreed and self.restated_sig is None:
                # the manager agreed and went quiet before naming the restaurant: restate now, no "might be possible"
                return self._restate_on_silence()
            if self.followups == 0:
                self.followups = 1
                first = FOLLOW_UP_1.format(time=t)
                if self.confirm_asked:
                    n = int(self.facts.get("party_size"))
                    first = FOLLOW_UP_CONFIRM.format(people=restate.NUMBER_WORDS.get(n, str(n)), time=t)
                acts = self._say(first, "", "no reply for 10 minutes, one polite follow-up", fallback=first,
                                 rearm=False)
                return acts + [Schedule("silence", self.t.silence_second - self.t.silence_first, ev.gen)]
            if self.followups == 1:
                self.followups = 2
                acts = self._say(FOLLOW_UP_2.format(time=t), "", "no reply for 30 minutes, second and last follow-up",
                                 fallback=FOLLOW_UP_2.format(time=t), rearm=False)
                return acts + [NotifyOwner(texts.t("silent_30")),
                               Log("note", {"text": "silence: no more follow-ups, waiting"})]
            return []
        if name in ("owner_reping", "owner_wait"):
            key = ev.key or ""
            if key not in self.pending or ev.gen != self.gen.get(f"owner:{key}"):
                return []
            if name == "owner_reping":
                q, buttons, contact = policy.owner_question(key, self.facts, self.pending[key]["said"])
                return [AskOwner(key, texts.t("reminder") + q, buttons, contact, reminder=True)]
            if self.pending[key].get("apologised"):
                return []
            self.pending[key]["apologised"] = True
            if key in DETAIL_KEYS and self.agreed and self.restated_sig is None:
                text = restate.build(self.facts, variant=self.turn, unnamed=self.unnamed)
                if text:
                    # the owner is silent: restate without the detail, keep the question open for a late answer
                    missing = [k for k in DETAIL_KEYS if k in self.pending]
                    for k in missing:
                        self.pending[k]["apologised"] = True
                    names = ", ".join(policy.key_name(k) for k in missing)
                    return ([Log("decision", {"understood": "-", "decided": f"owner has not answered {', '.join(missing)} "
                                              "for 10 minutes; restatement without it"})]
                            + self._restate(text, restate.signature(self.facts))
                            + [NotifyOwner(texts.t("restated_without", keys=names))])
            text = SORRY_WAIT
            if key in policy.OPTIONAL_KEYS and self.facts.get("guest_name"):
                text += NAME_ONLY.format(name=self.facts.get("guest_name"))
            return self._say(text, "", f"owner has not answered {key} for 10 minutes", fallback=SORRY_WAIT,
                             rearm=False)
        if name == "brain_retry":
            if not self.retry or ev.gen != self.gen.get("retry"):
                return []
            return self._respond(*self.retry)
        return [Log("note", {"text": f"unknown timer {name}"})]

    def _restate_on_silence(self) -> list:
        acts: list = []
        if "restaurant_name" in restate.missing(self.facts):
            self.unnamed = True
            acts.append(NotifyOwner(texts.t("no_restaurant")))
        text = restate.build(self.facts, variant=self.turn, unnamed=self.unnamed)
        if not text:
            return [Log("note", {"text": "silence after the agreement, the restatement cannot be built yet"})]
        return ([Log("decision", {"understood": "-", "decided": "manager agreed and has been quiet for 10 minutes; "
                                  "restatement now"})] + self._restate(text, restate.signature(self.facts)) + acts)

    def _command(self, cmd: str) -> list:
        cmd = (cmd or "").strip().lower().lstrip("/")
        if cmd == "stop":
            self.stopped = True
            return [Log("note", {"text": "owner: /stop, the agent sends nothing more"}), Stop("owner /stop")]
        if cmd == "status":
            return [NotifyOwner(self.status_text())]
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

    def _respond(self, event: str, voice: bool, said: list[str] | None = None) -> list:
        """`said` is the manager's text of this turn; None for owner answers and edits."""
        acts: list = []
        d = self._decide(event, acts)
        if d is None:
            self.retry = (event, voice, said)
            acts.append(Log("decision", {"understood": "-", "decided": "model unavailable, retry in "
                                         f"{self.t.brain_retry:.0f}s"}))
            self.gen["retry"] = self.gen.get("retry", 0) + 1  # one pending retry at a time
            acts.append(Schedule("brain_retry", self.t.brain_retry, self.gen["retry"]))
            if not self.brain_down_sent:
                self.brain_down_sent = True
                acts.append(NotifyOwner(texts.t("groq_down")))
                acts += self._say(TECH_HOLD, "", "model unavailable, holding line from a template",
                                  fallback=TECH_HOLD, rearm=False)
            return acts
        self.retry = None
        self.brain_down_sent = False
        acts += self._apply(d, event, voice, said)
        self.answered = None  # used: a retry after a model failure still has it, the next turn does not
        return acts

    def _apply(self, d: Decision, event: str, voice: bool, said: list[str] | None = None) -> list:
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
            notes.append(f"manager offered {offered}: another time is the owner's call, asking")

        agreed_now = d.action == "final_restatement" or d.slots.manager_agreed
        if (not agreed_now and self.confirm_asked and said is not None
                and policy.manager_yes(" ".join(said), f.get("time"), f.get("guest_name"))):
            agreed_now = True
            notes.append("a plain yes to the confirm question: agreed")
        if said is not None and not policy.manager_stall(" ".join(said), f.get("time")):
            self.confirm_asked = False
        if agreed_now and (time_open or "alternative_time" in self.pending):
            agreed_now = False
            notes.append("agreement ignored while another time is open")
        if self.agreed and self.restated_sig is None and (
                time_open or (said is not None and d.intent in WITHDRAWN)):
            # "Oh sorry, 17:00 has just gone. I can do 18:00?" before the restatement: the yes no longer stands
            self.agreed = agreed_now = False
            notes.append("agreement taken back: the manager offers another time or has no table")
        if agreed_now:
            self.agreed = True
            for k in ("time", "date", "party_size"):
                f.confirm(k)

        new_keys = [k for k in needs if k not in self.pending and not (k in ("guest_name", "phone") and f.get(k))]
        last = self.manager_texts[-1] if self.manager_texts else ""
        sig = restate.signature(f)
        restate_due = (self.agreed and not time_open and "alternative_time" not in self.pending
                       and (self.restated_sig is None or (self.phase == CONFIRMED and sig != self.restated_sig)))
        # one restatement: while a name or phone the manager asked for is with the owner, only a holding line goes
        waiting = [k for k in DETAIL_KEYS if k in new_keys or k in self.pending]
        hold_restatement = restate_due and self.restated_sig is None and bool(waiting)
        if hold_restatement:
            notes.append(f"manager agreed; the restatement waits for {', '.join(waiting)} from the owner")
        quiet = self._pleasantry(d, said, new_keys, restate_due)
        if quiet:
            notes.append(quiet)
        acts.append(Log("decision", {"understood": d.understood, "intent": d.intent,
                                     "decided": d.decision + (f" [code: {'; '.join(notes)}]" if notes else ""),
                                     "action": d.action, "owner_keys": new_keys}))
        for k in new_keys:
            q, buttons, contact = policy.owner_question(k, f, last)
            self.pending[k] = {"said": last, "turn": self.turn}
            g = self.gen[f"owner:{k}"] = self.gen.get(f"owner:{k}", 0) + 1
            acts += [AskOwner(k, q, buttons, contact), Log("owner_question", {"key": k, "question": q, "gloss": policy.gloss(k, f)}),
                     Schedule("owner_reping", self.t.owner_reping, g, k), Schedule("owner_wait", self.t.owner_wait, g, k)]


        if quiet:
            return acts

        reply = self._with_detail(d.reply.strip()) if d.action not in ("wait", "end") else d.reply.strip()
        if hold_restatement:
            acts += self._say(reply or (HOLD if new_keys else STILL_CHECKING), event,
                              f"holding line; the restatement goes once {', '.join(waiting)} is back", fallback=HOLD,
                              voice=voice)
            return acts
        if restate_due:
            if self.asked_restaurant and said is not None and "restaurant_name" in restate.missing(f):
                self.unnamed = True
                acts.append(NotifyOwner(texts.t("no_restaurant")))
            text = restate.build(f, variant=self.turn, correction=restate.is_correction(self.restated_sig, sig),
                                 unnamed=self.unnamed)
            if text:
                acts += self._restate(text, sig)
                if new_keys and reply:
                    acts += self._say(reply, event, "holding line for the owner question", fallback=HOLD)
                return acts
            if "restaurant_name" in restate.missing(f):
                lead = reply or self._detail_line()
                if lead and said is None and not policy.asks_restaurant(lead) and not self.asked_restaurant:
                    # the owner's answer (a name, a phone) goes to the manager together with the question
                    acts += self._say(f"{lead} {ASK_RESTAURANT_ALSO}", event,
                                      "owner answered; restaurant name unknown, asking before the restatement",
                                      fallback=restate.ASK_RESTAURANT, voice=voice)
                elif reply and policy.asks_restaurant(reply):
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

        if (said is None and self.answered in DETAIL_KEYS and not self.agreed and not new_keys and not time_open
                and not any(k in self.pending for k in DETAIL_KEYS + ("alternative_time",))):
            # the agreement is not recorded (the model may read "Yes, 17:00 is fine. What name?" as a question only):
            # the name or phone goes with a confirm question, and a plain yes to it is the agreement
            return acts + self._ask_confirm(reply if d.action not in ("wait", "end") else "", voice)

        # one closing reply after the restatement is owed even when the model chose to stay quiet
        owed = (self.phase == CONFIRMED and not self.closed and said is not None and d.intent in PLEASANTRY
                and not new_keys and (d.action in ("wait", "end") or not reply))
        if (not reply and self.phase == CONFIRMED and d.action == "final_restatement") or owed:
            # already restated and nothing changed: a short closing instead of silence
            n_before = self.n_out
            acts += self._say(CLOSING, "", "booking already restated, short closing", fallback=CLOSING,
                              voice=voice, rearm=False)
            self.closed = self.closed or self.n_out > n_before
            return acts
        if d.action in ("wait", "end") or not reply:
            if new_keys:
                acts += self._say(HOLD, event, "holding line while the owner is asked", fallback=HOLD,
                                  voice=voice)
            elif self.pending and said is not None:
                # the manager wrote while the owner is still asked ("Any luck?"): silence would read as gone
                acts += self._say(STILL_CHECKING, event, "the owner has not answered yet; still checking",
                                  fallback=STILL_CHECKING, voice=voice)
            return acts
        why = d.decision or d.action
        # every draft blocked: a closing after the restatement, otherwise a holding line and the owner is asked
        closing = self.phase == CONFIRMED and d.intent in PLEASANTRY and not new_keys and not self.pending
        fallback = CLOSING if closing else HOLD
        n_before = self.n_out
        acts += self._say(reply, event, why, fallback=fallback, voice=voice)
        if self.phase == CONFIRMED and d.intent in PLEASANTRY and self.n_out > n_before:
            self.closed = True
        return acts

    def _pleasantry(self, d: Decision, said: list[str] | None, new_keys: list[str], restate_due: bool) -> str | None:
        """After the restatement and one closing reply, a pure pleasantry from the manager gets no answer.
        Returns the reason for the decision line, or None when the message must be answered."""
        if self.phase != CONFIRMED or not self.closed or said is None:
            return None
        if new_keys or self.pending or restate_due or any("?" in t for t in said):
            return None
        # the closing template again (final_restatement with no reply and nothing changed) is a pleasantry too
        again = d.action == "final_restatement" and not d.reply.strip()
        if d.intent not in PLEASANTRY and not again:
            return None
        return "no reply: closing already sent after the restatement; a pleasantry with no question, nothing pending"

    def _restate(self, text: str, sig: tuple) -> list:
        text = guard.fix(text)
        probs = restate.verify(text, self.facts, self.unnamed) + [str(p) for p in guard.check(text, self.facts, self.chat_texts)]
        if probs:
            return [Log("guard", {"text": text, "problems": probs, "result": "restatement blocked"}),
                    NotifyOwner(texts.t("restatement_blocked", problems="; ".join(probs)))]
        self.before_restate = (self.restated_sig, self.phase, self.closed)
        self.restated_sig = sig
        self.n_restated += 1
        self.phase = CONFIRMED
        self.closed = False  # a corrected restatement may get its own thank-you
        self.gen["silence"] += 1
        why = "manager agreed; restatement built by code from confirmed slots: " + self._slot_summary()
        if self.unnamed and not self.facts.is_confirmed("restaurant_name"):
            why += " (the manager gave no restaurant name after one ask: \"at your restaurant\")"
        self.n_out += 1
        self.transcript.append(f"AGENT: {text}")
        cls = SendVoice if self.voice_restatement else SendText
        # the runner tells the owner "booked" once the transport has delivered it
        return [Log("guard", {"text": text, "problems": [], "result": "ok"}), cls(text, why, self.n_out, restatement=True)]

    def restatement_not_sent(self, text: str) -> bool:
        """The transport failed on the restatement, retry included: the state goes back to the last delivered one
        and the next turn restates again. True when an earlier restatement stands (this one was a correction)."""
        sig, phase, closed = self.before_restate
        self.restated_sig, self.phase, self.closed = sig, (phase if sig else OPEN), closed
        self.n_restated -= 1
        line = f"AGENT: {text}"
        if line in self.transcript:
            del self.transcript[len(self.transcript) - 1 - self.transcript[::-1].index(line)]
        return sig is not None

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
                acts.append(NotifyOwner(texts.t("guard_template", why=guard.describe(first), text=text)))
                if not self.pending and self.phase != NEW and fallback == HOLD:
                    acts += self._ask_help()
        if probs:
            acts.append(NotifyOwner(texts.t("guard_nothing", why=guard.describe(first))))
            return acts
        self.n_out += 1
        self.transcript.append(f"AGENT: {text}")
        acts.append((SendVoice if voice else SendText)(text, why, self.n_out))
        if rearm:
            acts += self._arm_silence()
        return acts

    def _detail_line(self) -> str | None:
        """The name or phone the owner just gave, for a turn where the model wrote no reply."""
        if self.answered == "guest_name" and self.facts.get("guest_name"):
            return f"Thank you for waiting - the booking can go under {self.facts.get('guest_name')}."
        if self.answered == "phone" and self.facts.get("phone"):
            return f"Thank you for waiting - the contact number is {self.facts.get('phone')}."
        return None

    def _with_detail(self, reply: str) -> str:
        """A reply on the owner's answer that leaves out the name or phone gets the detail line in front."""
        detail = self._detail_line()
        if not reply or not detail:
            return reply
        value = str(self.facts.get(self.answered))
        if self.answered == "phone":
            if re.sub(r"\D", "", value)[-7:] in re.sub(r"\D", "", reply):
                return reply
        elif value.lower() in reply.lower():
            return reply
        return f"{detail} {reply}"

    def _ask_confirm(self, reply: str, voice: bool) -> list:
        """The model's reply (the name or phone in it) plus one question: is it all confirmed. The restaurant's name
        is asked, or "at your restaurant" used, on the way to the restatement."""
        f = self.facts
        detail = self._detail_line() or ""
        lead = LET_ME_KNOW_RE.sub("", reply or "").strip()
        if lead and lead != (reply or "").strip() and lead[-1] not in ".!?":
            lead += "."
        lead = lead or detail
        n = int(f.get("party_size"))
        q = CONFIRM_Q.format(people=restate.NUMBER_WORDS.get(n, str(n)), time=f.get("time"))
        text = lead if CONFIRM_RE.search(lead) else " ".join(x for x in (lead, q) if x)
        n_before = self.n_out
        # no model rewrite here: a blocked draft goes as the template, so the confirm question always goes out
        acts = self._say(text, "", "owner answered; the manager's agreement is not recorded yet, the detail goes with "
                         "a confirm question", fallback=" ".join(x for x in (detail, q) if x), voice=voice)
        if self.n_out > n_before:
            self.confirm_asked = True
            self.asked_restaurant = self.asked_restaurant or policy.asks_restaurant(lead)
        return acts

    def _ask_help(self) -> list:
        """The owner is asked what to say; the same reminder and the apology to the manager as any question."""
        last = self.manager_texts[-1] if self.manager_texts else ""
        q, buttons, contact = policy.owner_question("reply_help", self.facts, last)
        self.pending["reply_help"] = {"said": last, "turn": self.turn}
        g = self.gen["owner:reply_help"] = self.gen.get("owner:reply_help", 0) + 1
        return [AskOwner("reply_help", q, buttons, contact), Log("owner_question", {"key": "reply_help", "question": q,
                                                                    "gloss": policy.gloss("reply_help", self.facts)}),
                Schedule("owner_reping", self.t.owner_reping, g, "reply_help"),
                Schedule("owner_wait", self.t.owner_wait, g, "reply_help")]

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
