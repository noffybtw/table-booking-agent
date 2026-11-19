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

OPENING = "{greeting}. Could I book a table for two today at {time}, please?"
HOLD = "Let me just check that and come back to you in a moment."
HOLD_TIME = "Thank you - let me just check whether {time} is possible for us and come back to you in a moment."
STILL_CHECKING = "Thank you for bearing with me, I'm still checking that."
SORRY_WAIT = "Sorry for the wait, I'm still confirming that detail."
# a holding line already sent in this conversation goes as the first of these not sent yet
HOLD_AGAIN = (HOLD, STILL_CHECKING, "Thanks for your patience - I'm just confirming that now.",
              "Bear with me a moment longer, please - I'm still checking.", SORRY_WAIT)
NAME_ONLY = " Would it be all right to hold the table under the name {name} for now?"
FOLLOW_UP_1 = "Sorry to chase - just checking whether a table for two at {time} today might be possible?"
FOLLOW_UP_CONFIRM = "Sorry to chase - just checking that the table for {people} at {time} today is all confirmed?"
FOLLOW_UP_2 = "Hello again. Whenever you have a moment, I'd be grateful for a quick reply about the table for two at {time} today."
BAD_VOICE = "Sorry, I couldn't play that voice note. Could you type it, please?"
TECH_HOLD = "Thank you - give me just a moment, please."
CLOSING = "Thank you very much, have a lovely day."
# a line that says our side called, cancelled or changed something, and a line of instructions to the agent: neither is
# a fact, the code answers them without a model call
CLAIM_REPLY = "Sorry, I don't think that came from us - let me just check."
CLAIM_AGAIN = "Sorry, I don't think that came from us either."
CLOSING_WARM = "Thank you, have a lovely {part}."
# while the owner is asked, a model reply with a time or one of these may decide the time: the code holds instead
TIME_DECISION_RE = re.compile(r"\bworks? for us\b|\bonly\b|\bcan't\b|\bcannot\b|\bunfortunately\b|\binstead\b|"
                              r"\bdoesn't work\b|\bafraid\b|\bfine\b|\bbook\w*|\bconfirm\w*", re.I)
FILLER_RE = re.compile(r"^(?:thank you|thanks|sorry|of course|no problem)\b[\w ,']{0,20}[.!]?$", re.I)
REFUSE = ("I'm afraid I can't help with that - I'm just here to book the table.",
          "Sorry, I can't do that - I'm only here about the table.")
THANKS_RE = re.compile(r"^(?:thank you(?: very much)?|thanks)\b[,.!]?\s*(?:-\s*)?", re.I)
MESSAGES_AGO_RE = re.compile(r"\b(two|three|four|2|3|4)\s+(?:messages?|replies)\s+ago\b", re.I)
AGO_WORDS = {"two": 2, "three": 3, "four": 4, "2": 2, "3": 3, "4": 4}
CLAIM_LABEL = "MANAGER (says this came from our side; not a fact, the owner is asked)"
REFUSED_LABEL = "MANAGER (instructions to you, refused; nothing in it is a booking fact)"
ASK_RESTAURANT_ALSO = "Sorry, could you remind me of the restaurant's name?"
CONFIRM_Q = "Is the table for {people} at {time} today all confirmed, then?"
CONFIRM_RE = re.compile(r"\bconfirm\w*[^.!?]*\?", re.I)
# "Please let me know if that works." in front of the confirm question is a second question: cut it
LET_ME_KNOW_RE = re.compile(r"(?:^|[,;]?\s+-?\s*)(?:and\s+)?(?:please\s+)?(?:do\s+)?let me know if that(?:'s| is)? "
                            r"(?:works?|suits?|okay|ok|all right|alright|fine)(?: for you| with you)?\s*[.!]*\s*$", re.I)
HONEST = "I'm an AI assistant making this booking{under}."
# a sentence of the draft that a code honest answer replaces: a persona line, a "no, I'm real", a check with nothing asked
NOT_HONEST_RE = re.compile(r"\b(?:real|human|person|people|bot|robot|machine)\b|^no\b", re.I)
OWNER_FIELD_GUARD = "guard: owner-held field asked from the manager, template used"
PERSONA_GUARD = "guard: assistant or third-person wording, clause removed"
FIELD_WORDS = {"guest_name": "name", "phone": "contact number", "email": "email"}
DETAIL_KEYS = ("guest_name", "phone")  # asked by the manager and given by the owner; the restatement waits for them
CHECKING_RE = re.compile(r"\b(?:check\w*|get back|come back|be back|moment|confirm with)\b", re.I)
PLEASANTRY = ("closing", "greeting")
WITHDRAWN = ("counter_offer", "fully_booked")  # intents that take an unrestated agreement back

MANAGER_LABEL = {"text": "MANAGER", "voice": "MANAGER (voice note, transcribed)", "media": "MANAGER"}


class Engine:
    def __init__(self, brain, facts: FactStore, now: Callable[[], datetime] | None = None,
                 timings: Timings | None = None, voice_restatement: bool = True, profile=None):
        self.brain = brain
        self.profile = profile               # agent/profile.py, or None: everything is asked as before
        self.facts = facts
        self.now = now or (lambda: clock.london_now())
        self.t = timings or Timings()
        self.voice_restatement = voice_restatement
        self.phase = NEW
        self.transcript: list[str] = []
        self.chat_texts: list[str] = []      # what the manager and the owner wrote; the guard trusts only these
        self.manager_texts: list[str] = []
        self.untrusted: list[str] = []       # claims made in the owner's name and refused instructions: never facts
        self.pending: dict[str, dict] = {}   # owner questions waiting for an answer
        self.declined_times: set[str] = set()
        self.agreed = False
        self.agreed_by: str | None = None    # the manager's words the agreement came with, quoted when asked
        self.agreed_sig: tuple | None = None  # (time, party size) the manager agreed to
        self.retracted = False               # the manager said it is not confirmed: the model's yes needs new words
        self.offer_said = ""                 # the manager's offer the owner just took
        self.ref_thanks = False              # a reference came: the next message thanks for it once
        self.asked_restaurant = False
        self.unnamed = False                 # asked once, no restaurant name: restate with "at your restaurant"
        self.confirm_asked = False           # "Is the table ... all confirmed, then?" went out, no answer yet
        self.answered: str | None = None     # the owner question just answered; kept until a model reply has used it
        self.relay: list[str] = []           # the names and phones the reply of this turn must carry
        self.batch: set[str] = set()         # fields one manager message asked the owner for: they go back together
        self.held_answers: list[str] = []    # answered fields of the batch waiting for the rest
        self.took_time = False               # this turn the owner took the time the manager offered
        self.from_profile: list[str] = []    # fields the profile gave this turn
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
        self.bot_asked = False               # the manager asked if this is a bot or who writes; no answer sent yet
        self.held_for: set[str] = set()      # owner questions a holding line went out for: one each
        self.said_goodbye = False

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
                 f"'{clock.greeting(now)}'. Do not introduce yourself; in the first person, ask for a table for two "
                 f"today at {self.facts.get('time')}. One message, for example: "
                 f"\"{tmpl}\"")
        acts: list = []
        d = self._decide(event, acts)
        draft = d.reply if d and d.reply.strip() else tmpl
        why = "opening, " + self._slot_summary()
        if re.search(r"\bAI\b", draft):
            # "AI" comes only when the manager asks whether it is a bot
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
        claims: list[str] = []
        refused: list[str] = []
        for m in msgs:
            self.n_in += 1
            untrusted = None
            if m.text and m.kind != "media":
                untrusted = "refused" if policy.injection(m.text) else "claim" if policy.claim(m.text) else None
            if untrusted:
                line = f"{CLAIM_LABEL if untrusted == 'claim' else REFUSED_LABEL}: {m.text}"
                (claims if untrusted == "claim" else refused).append(m.text)
                self.untrusted.append(m.text)
            elif ev.kind == "manager_edit":
                line = f"MANAGER (edited an earlier message; it was: \"{ev.old_text}\"): {m.text}"
            elif m.kind == "voice" and not m.text:
                line, failed_voice = "MANAGER sent a voice note that could not be transcribed", True
            elif m.kind == "media":
                line = f"MANAGER sent {m.text or 'a sticker or photo'} (no text)"
            else:
                line = f"{MANAGER_LABEL.get(m.kind, 'MANAGER')}: {m.text}"
            self.transcript.append(line)
            if m.text and m.kind != "media" and not untrusted:
                self.manager_texts.append(m.text)
                self.chat_texts.append(m.text)
            if ev.kind == "manager_edit" and ev.old_text:
                self.chat_texts.append(ev.old_text)
            acts.append(Log("in", {"n": self.n_in, "kind": "edit" if ev.kind == "manager_edit" else m.kind,
                                   "text": m.text or "", "old_text": ev.old_text or None, "lang": m.lang,
                                   "error": m.error}))
        self.last_voice = any(m.kind == "voice" for m in msgs)
        if any(policy.asks_if_bot(m.text or "") for m in msgs):
            self.bot_asked = True

        if failed_voice and all(m.kind == "voice" and not m.text for m in msgs):
            acts.append(Log("decision", {"understood": "voice note I could not transcribe",
                                         "decided": "ask the manager to type it (template, no model call)"}))
            return acts + self._say(BAD_VOICE, "", "voice note could not be transcribed", fallback=BAD_VOICE)

        rest = [m.text or "" for m in msgs if (m.text or "") not in claims + refused]
        if claims or refused:
            acts += self._untrusted(claims, refused)
            if ev.kind == "manager_edit" or not any(t.strip() for t in rest):
                return acts
        if ev.kind == "manager_edit":
            event = ("The manager edited an earlier message (see the MANAGER (edited...) line). If a booking fact "
                     "changed, say what changed and re-confirm it; another time is the owner's decision.")
        elif ev.kind == "manager_media":
            event = "The manager sent a sticker or photo with no text. Ask one short clarifying question."
        else:
            event = "New message(s) from the manager above. React to all of it in one reply."
        if ev.kind != "manager_edit":
            heard = " ".join(rest)
            ref = policy.reference(heard)
            if ref and ref != self.facts.get("reference"):
                self.facts.set("reference", ref, "manager", self.turn, confirmed=True)
                self.ref_thanks = True
                acts.append(Log("note", {"text": f"reference {ref} from the manager kept as a fact"}))
            if policy.not_confirmed(heard):
                self.agreed, self.agreed_by, self.retracted = False, None, True
                acts.append(Log("note", {"text": "the manager says the booking is not confirmed: no agreement stands"}))
            if policy.only_chat_questions(heard, self.facts.get("time")):
                return acts + self._chat_answer(heard)
            if policy.manager_ends(heard, self.phase == CONFIRMED):
                if self.closed or self.said_goodbye:
                    return acts + [Log("decision", {"understood": "the manager ends the conversation",
                                                    "decided": "no reply: closing already sent"})]
                self.said_goodbye = True
                self.closed = self.phase == CONFIRMED
                line = CLOSING_WARM.format(part="evening" if self.now().hour >= 12 else "day")
                acts.append(Log("decision", {"understood": "the manager ends the conversation",
                                             "decided": "one warm closing line from code"}))
                return acts + self._say(line, "", "the manager ends the conversation: one warm closing line",
                                        fallback=line, voice=self.last_voice, rearm=False)
            if policy.chat_questions(heard):
                event += (" The manager also asks about this conversation: answer that from CHAT SO FAR and KNOWN "
                          "FACTS, quoting exactly; only the manager confirms a booking, never say you or anyone on "
                          f"your side confirmed it; the manager has {'' if self.manager_confirmed() else 'not '}"
                          "confirmed the current time and party size.")
        if self.confirm_asked:
            event += (" Your last message asked whether the table is all confirmed: a yes to it is the manager's "
                      "agreement.")
        said = None if ev.kind == "manager_edit" else rest
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
        value, at, about = (ev.value or "").partition("@")
        if key == "alternative_time" and ev.value and key not in self.pending:
            # a button of a question that is closed: nothing changes for the manager
            return [Log("owner_answer", {"key": key, "text": text, "result": "a button of a closed question, not taken"}),
                    NotifyOwner(texts.t("time_closed", status=self.status_text()))]
        if key == "alternative_time" and at and about != (self.pending[key].get("offered") or "-"):
            # a button of an older question that named another time: the open question goes again
            q, buttons, contact = policy.owner_question(key, self.facts, self.pending[key]["said"])
            return [Log("owner_answer", {"key": key, "text": text, "result": "a button of an older question about "
                                         f"{about}, the open one sent again"}),
                    AskOwner(key, f"{texts.t('time_old')} {q}", buttons, contact)]
        offered, time_before = self.facts.get("offered_time"), self.facts.get("time")
        line = policy.apply_owner_answer(self.facts, key, text, value or None, ev.contact, self.turn)
        if line is None:
            # the question stays open (and its timers run on); no model turn on an answer that is not taken
            if key == "alternative_time":
                q, buttons, contact = policy.owner_question(key, self.facts, self.pending.get(key, {}).get("said", ""))
                again = texts.t("time_again") if self.facts.get("offered_time") else texts.t("time_type")
                return [Log("owner_answer", {"key": key, "text": text,
                                             "result": "neither yes nor no, question sent again"}),
                        AskOwner(key, f"{again} {q}", buttons, contact)]
            return [Log("owner_answer", {"key": key, "text": text, "result": "not a name in Latin letters, asked again"}),
                    AskOwner(key, texts.t("name_again"), [], False)]
        summary = "owner note, passed to the model"
        if key == "guest_name":
            summary = f'name "{self.facts.get("guest_name")}"'
        elif key == "phone" and self.facts.get("phone") and line.startswith("phone: "):
            summary = line.removeprefix("phone: ")
        if key == "alternative_time" and not offered and self.facts.get("time") != time_before:
            summary = f"accepted {self.facts.get('time')} (typed by the owner, no offer was stored)"
        if key == "alternative_time" and offered:
            self.offer_said = self.pending.get(key, {}).get("said", "")
            if self.facts.get("time") == offered:
                # the manager offered it and the owner took it: the new time is agreed on both sides
                self.facts.confirm("time")
                self.took_time = True
                summary = f"accepted {offered}"
            else:
                self.declined_times.add(offered)  # only on "only" or a no: anything else was asked again above
                summary = f"only {self.facts.get('time')}, {offered} declined"
            self.facts.drop("offered_time")
        self.pending.pop(key, None)
        self.held_for.discard(key)
        self.gen[f"owner:{key}"] = self.gen.get(f"owner:{key}", 0) + 1
        self.transcript.append(f"OWNER (private, the manager cannot see this): {line}")
        if text:
            self.chat_texts.append(text)
        self.chat_texts.append(line)
        relay = [key]
        if key in self.batch:
            waiting = [k for k in self.batch if k in self.pending]
            if waiting:
                # one manager message asked for several fields: the manager gets them all in one message
                self.held_answers.append(key)
                return [Log("owner_answer", {"key": key, "text": text, "line": line,
                                             "summary": f"{summary}, held until {', '.join(waiting)} is answered too"})]
            relay = self.held_answers + [key]
            self.batch, self.held_answers = set(), []
        self.relay = [k for k in relay if k in DETAIL_KEYS]
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
            held = []
            if key in self.batch:
                # the 10 minutes are up for the batch: what the owner did answer goes now, the rest stays open
                held = [k for k in self.held_answers if k in DETAIL_KEYS]
                for k in self.batch & set(self.pending):
                    self.pending[k]["apologised"] = True
                self.batch, self.held_answers = set(), []
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
                held = [k for k in held if k != "guest_name"]
            if held:
                text += f" In the meantime, {' and '.join(self._detail_parts(held))}."
            return self._say(text, "", f"owner has not answered {key} for 10 minutes", fallback=SORRY_WAIT,
                             rearm=False, once=False)
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
        if said and self.profile:
            # a profile field goes only when the model wants it and the manager's sentence really asks for it
            wanted = {policy.norm_key(k) for k in d.needs_owner}
            given = [k for k in DETAIL_KEYS if k in wanted and self.profile.get(k) and not self.facts.get(k)
                     and policy.manager_asked_now(k, said)]
            for k in given:
                self.facts.set(k, self.profile.get(k), "owner", self.turn)
            if given:
                self.from_profile = self.relay = given
        acts += self._apply(d, event, voice, said)
        # used: a retry after a model failure still has them, the next turn does not
        self.answered, self.relay, self.took_time, self.from_profile = None, [], False, []
        return acts

    def _apply(self, d: Decision, event: str, voice: bool, said: list[str] | None = None) -> list:
        acts: list = []
        f = self.facts
        notes: list[str] = []

        ok, rejected = policy.accepted_slots(d.slots, f, self.manager_texts, self.untrusted)
        if ok.get("offered_time") in self.declined_times:
            ok.pop("offered_time")
        for k, v in ok.items():
            if f.get(k) != v:
                f.set(k, v, "manager", self.turn, confirmed=(k == "restaurant_name"))
                notes.append(f"{k}={v} (from the manager)")
        notes += [f"rejected {r}" for r in rejected]

        if self.from_profile:
            notes.append(f"{', '.join(self.from_profile)} from the owner's profile, given before the start")
        offered = f.get("offered_time")
        open_q = self.pending.get("alternative_time")
        if open_q and offered and offered != open_q.get("offered"):
            # the manager changed (or edited) the offer while the owner is asked: the open question names another
            # time, ask again
            self.pending.pop("alternative_time")
            self.held_for.discard("alternative_time")
            notes.append(f"the offer changed to {offered} while the owner was asked: asking again")
        window_took = False
        if (self.profile and self.profile.window and said is not None and offered and offered != f.get("time")
                and "alternative_time" not in self.pending):
            heard = " ".join(said)
            plain = policy.plain_offer(heard, offered, f.get("time"))
            if self.profile.inside(offered) and policy.time_sure(offered, [heard]) and plain is None:
                # the owner said before the start that this time works: no question, the manager hears it works
                f.set("time", offered, "owner", self.turn)
                f.confirm("time")
                f.drop("offered_time")
                window_took = self.took_time = True
                notes.append(f"manager offered {offered}: inside the owner's window {self.profile.window}, taken")
            else:
                why_not = ("outside the owner's window" if not self.profile.inside(offered) else
                           "the time is not sure" if not policy.time_sure(offered, [heard]) else plain)
                notes.append(f"manager offered {offered}: {why_not} (window {self.profile.window}), asking the owner")
        offered = f.get("offered_time")
        time_open = bool(offered and offered != f.get("time"))
        # the model's time was not taken because only a claim or a refused line names it: no time question either
        untrusted_time = bool(not offered and d.slots.offered_time and self.untrusted
                              and policy.time_sure(guard.norm_time(d.slots.offered_time) or "", self.untrusted))
        needs = []
        for k in d.needs_owner:
            k = policy.norm_key(k)
            if k in ("restaurant", "restaurant_name"):
                notes.append("not asking the owner for the restaurant's name: only the manager knows it")
                continue
            if k in needs or (k == "alternative_time" and (window_took or untrusted_time)):
                if untrusted_time and k == "alternative_time":
                    notes.append(f"not asking the owner about {d.slots.offered_time}: only a line not taken as a fact "
                                 "names it")
                continue
            if not policy.manager_asked(k, self.manager_texts):
                notes.append(f"not asking the owner for {k}: the manager did not ask for it")
                continue
            needs.append(k)
        if time_open and "alternative_time" not in needs and "alternative_time" not in self.pending:
            needs.append("alternative_time")
            notes.append(f"manager offered {offered}: another time is the owner's call, asking")

        agreed_now = d.action == "final_restatement" or d.slots.manager_agreed
        if agreed_now and said is None and not (self.agreed or self.took_time):
            # an owner turn: the model's "agreed" stands only for the time the owner has just taken
            agreed_now = False
            notes.append("the model's agreement not taken: an owner turn, and the manager has not agreed")
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
            self.agreed_by = None
            notes.append("agreement taken back: the manager offers another time or has no table")
        if window_took:
            agreed_now = True  # the manager offered the time and the owner's window takes it
        if (agreed_now and self.retracted and said is not None and not window_took
                and not policy.AGREE_WORD_RE.search(" ".join(said))):
            agreed_now = False
            notes.append("the manager said it is not confirmed; the model's agreement needs a new yes")
        if agreed_now:
            sig_now = (f.get("time"), f.get("party_size"))
            if not self.agreed or self.agreed_sig != sig_now:
                self.agreed_by = " ".join(said) if said else (self.offer_said or (self.manager_texts or [""])[-1])
                self.agreed_sig = sig_now
            self.agreed, self.retracted = True, False
            for k in ("time", "date", "party_size"):
                f.confirm(k)

        new_keys = [k for k in needs if k not in self.pending and not (k in ("guest_name", "phone") and f.get(k))]
        fields = [k for k in new_keys if k not in policy.NOT_OWNER_FIELDS]
        if len(fields) > 1:
            self.batch = set(fields) | (self.batch & set(self.pending))
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
            self.pending[k] = {"said": last, "turn": self.turn, "offered": offered if k == "alternative_time" else None}
            g = self.gen[f"owner:{k}"] = self.gen.get(f"owner:{k}", 0) + 1
            acts += [AskOwner(k, q, buttons, contact), Log("owner_question", {"key": k, "question": q, "gloss": policy.gloss(k, f)}),
                     Schedule("owner_reping", self.t.owner_reping, g, k), Schedule("owner_wait", self.t.owner_wait, g, k)]


        if quiet:
            return acts

        reply = self._with_detail(d.reply.strip()) if d.action not in ("wait", "end") else d.reply.strip()
        if self.from_profile and CHECKING_RE.search(d.reply):
            reply = ""  # "let me check those details": the profile has them, the detail line goes instead
        if "alternative_time" in new_keys:
            # the other time is the owner's call: no decline and no yes from the model before the owner answers
            detail = self._detail_line() if self.from_profile else None
            hold = HOLD_TIME.format(time=offered) if offered else HOLD
            line = f"{detail} {hold}" if detail else hold
            return acts + self._say(line, event, "another time offered: holding line from code while the owner is "
                                    "asked, the model's draft not sent", fallback=HOLD, voice=voice)
        if (said is not None and "alternative_time" in self.pending
                and (not reply or guard.find_times(reply) or TIME_DECISION_RE.search(reply) or CHECKING_RE.search(reply))):
            # the manager writes again while the owner is still asked about the other time ("Hello? Is 20:00 ok?");
            # a reply about something else ("Are you a bot?") goes as the model wrote it
            detail = self._detail_line() if self.from_profile else None
            line = f"{detail} {STILL_CHECKING}" if detail else STILL_CHECKING
            return acts + self._say(line, event, "another time still with the owner: still checking from code, the "
                                    "model's draft not sent", fallback=STILL_CHECKING, voice=voice)
        if hold_restatement:
            acts += self._say(reply or (HOLD if new_keys else STILL_CHECKING), event,
                              f"holding line; the restatement goes once {', '.join(waiting)} is back", fallback=HOLD,
                              voice=voice)
            return acts
        if restate_due:
            # on the turn that gives a profile field "at your restaurant" comes only after 10 minutes of silence
            # (_restate_on_silence)
            if (self.asked_restaurant and said is not None and not self.from_profile
                    and "restaurant_name" in restate.missing(f)):
                self.unnamed = True
                acts.append(NotifyOwner(texts.t("no_restaurant")))
            text = restate.build(f, variant=self.turn, correction=restate.is_correction(self.restated_sig, sig),
                                 unnamed=self.unnamed)
            if text:
                acts += self._restate(text, sig)
                if new_keys and reply:
                    acts += self._say(reply, event, "holding line for the owner question", fallback=HOLD)
                elif self.bot_asked and said is not None:
                    # "Are you a bot?" on the turn the restatement goes: the honest answer still goes
                    line = reply if reply and re.search(r"\bAI\b", reply) else self._honest()
                    acts += self._say(line, event, "the manager asked who writes; honest answer", fallback=self._honest(),
                                      voice=voice)
                return acts
            if "restaurant_name" in restate.missing(f):
                lead = reply or self._detail_line()
                if window_took or (said is None and self.answered == "alternative_time" and f.source("time") == "owner"
                                   and f.get("time") not in (reply or "")):
                    # the owner took the other time: the manager hears it works, not only the restaurant question
                    took = f"Thank you, {f.get('time')} works for us. {ASK_RESTAURANT_ALSO}"
                    acts += self._say(took, event, "owner accepted the other time; restaurant name unknown, asking "
                                      "before the restatement", fallback=took, voice=voice)
                elif lead and (said is None or self.from_profile) and not policy.asks_restaurant(lead) \
                        and not self.asked_restaurant:
                    # the owner's answer (a name, a phone) goes to the manager together with the question
                    source = "from the owner's profile" if self.from_profile else "owner answered"
                    acts += self._say(f"{lead} {ASK_RESTAURANT_ALSO}", event,
                                      f"{source}; restaurant name unknown, asking before the restatement",
                                      fallback=restate.ASK_RESTAURANT, voice=voice)
                elif lead and self.from_profile:
                    # the restaurant was asked already: the detail only, the restatement waits for the name
                    acts += self._say(lead, event, "from the owner's profile; the restatement waits for the "
                                      "restaurant's name (asked already)", fallback=lead, voice=voice)
                elif reply and policy.asks_restaurant(reply):
                    acts += self._say(reply, event, "agreed; restaurant name unknown, asking before the restatement",
                                      fallback=restate.ASK_RESTAURANT, voice=voice)
                elif not self.asked_restaurant:
                    acts += self._say(restate.ASK_RESTAURANT, event,
                                      "agreed; restaurant name unknown, asking before the restatement",
                                      fallback=restate.ASK_RESTAURANT, voice=voice)
                elif reply:
                    acts += self._say(reply, event, d.decision, fallback=restate.ASK_RESTAURANT, voice=voice)
                else:
                    acts += self._arm_silence()  # nothing to say: the 10 minutes to "at your restaurant" run
                self.asked_restaurant = True
                return acts

        if ((said is None and self.answered in DETAIL_KEYS or self.from_profile) and not self.agreed and not new_keys
                and not time_open
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
            if self.from_profile and not new_keys:
                line = self._detail_line()
                return acts + self._say(line, event, "the manager asked; from the owner's profile", fallback=line,
                                        voice=voice)
            if new_keys:
                acts += self._say(HOLD, event, "holding line while the owner is asked", fallback=HOLD,
                                  voice=voice)
            elif self.pending and said is not None:
                # the manager wrote while the owner is still asked ("Any luck?"): silence would read as gone
                acts += self._say(STILL_CHECKING, event, "the owner has not answered yet; still checking",
                                  fallback=STILL_CHECKING, voice=voice)
            elif self.bot_asked and said is not None:
                line = self._honest()
                acts += self._say(line, event, "the manager asked who writes; honest answer", fallback=line,
                                  voice=voice)
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
        self.ref_thanks = False  # the restatement names the reference
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
             rearm: bool = True, once: bool = True) -> list:
        """Guard every outgoing text. Blocked: rewrite once naming the problem, then the fallback model, then the
        template. A blocked text is never sent. With `once`, one holding line per owner question: after it the
        holding sentences leave the text, and a text left with nothing goes to the owner as a nudge instead."""
        acts: list = []
        text, asked = self._owner_fields(guard.fix(draft))
        if asked:
            why += f" ({OWNER_FIELD_GUARD})"
        text, cut = self._persona(text, fallback)
        if cut:
            why += f" ({PERSONA_GUARD})"
        if self.bot_asked and text and not re.search(r"\bAI\b", text):
            text, why = self._with_honest(text), why + " (the manager asked who writes: honest answer from code)"
        probs = guard.check(text, self.facts, self.chat_texts) if text else [guard.Problem("empty", "no text")]
        acts.append(Log("guard", {"text": text, "problems": [str(p) for p in probs],
                                  "result": "ok" if not probs else "blocked"}))
        first = probs
        if probs and event:
            for prefer_fallback in (False, True):
                d = self._decide(event, acts, note=guard.describe(probs), prefer_fallback=prefer_fallback)
                if d is None or not d.reply.strip():
                    continue
                text, asked_again = self._owner_fields(guard.fix(d.reply))
                if asked_again and not asked:
                    asked, why = True, why + f" ({OWNER_FIELD_GUARD})"
                text, cut_again = self._persona(text, fallback)
                if cut_again and not cut:
                    cut, why = True, why + f" ({PERSONA_GUARD})"
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
        if once and self.pending:
            sents = re.split(r"(?<=[.!?])\s+", text.strip())
            holds = [s for s in sents if CHECKING_RE.search(s) or s in HOLD_AGAIN]
            if holds and set(self.pending) <= self.held_for:
                kept = [s for s in sents if s not in holds]
                if not kept or all(FILLER_RE.match(s) for s in kept):
                    keys = ", ".join(policy.key_name(k) for k in self.pending)
                    return acts + [Log("note", {"text": f"holding line not sent: one went out already for {keys}"}),
                                   NotifyOwner(texts.t("manager_waiting", keys=keys))]
                text, why = " ".join(kept), why + " (holding sentence left out: one went out already)"
            elif holds:
                self.held_for |= set(self.pending)
        if self.pending and (CHECKING_RE.search(text) or text in HOLD_AGAIN) and f"AGENT: {text}" in self.transcript:
            other = next((h for h in HOLD_AGAIN if f"AGENT: {h}" not in self.transcript), None)
            if other:
                acts.append(Log("guard", {"text": other, "problems": [], "result": "ok", "retry": "holding line again"}))
                text, why = other, why + " (the same holding line went out before: another wording)"
        ref = self.facts.get("reference")
        if self.ref_thanks and ref:
            # the reference the manager gave is thanked for once
            if ref not in text:
                rest = THANKS_RE.sub("", text, count=1)
                text = f"Thank you - I've noted reference {ref}." + (f" {rest[0].upper()}{rest[1:]}" if rest else "")
            self.ref_thanks = False
        self.n_out += 1
        self.transcript.append(f"AGENT: {text}")
        self.bot_asked = self.bot_asked and not re.search(r"\bAI\b", text)
        acts.append((SendVoice if voice else SendText)(text, why, self.n_out))
        if rearm:
            acts += self._arm_silence()
        return acts

    def manager_confirmed(self) -> bool:
        """The manager agreed to the current time and party size after their last change."""
        f = self.facts
        return bool(self.agreed and self.agreed_by and self.agreed_sig == (f.get("time"), f.get("party_size")))

    def _summary(self) -> str:
        """What the owner would be told now; an unknown field is said to be unknown."""
        f = self.facts
        n = int(f.get("party_size"))
        s = f"Table for {restate.NUMBER_WORDS.get(n, str(n))} at {f.get('time')} on {clock.human_date(f.booking_date())}"
        if f.is_confirmed("restaurant_name"):
            s += f" at {f.get('restaurant_name')}"
        if f.get("guest_name"):
            s += f", under the name {f.get('guest_name')}"
        s += ", confirmed by the restaurant" if self.manager_confirmed() else ", not confirmed yet"
        if f.get("reference"):
            s += f", reference {f.get('reference')}"
        s += "."
        if not f.is_confirmed("restaurant_name"):
            s += " The restaurant's name hasn't been given."
        return s

    def _chat_answer(self, heard: str) -> list:
        """A question about the conversation itself, answered from its history and the facts: no model call, no
        holding line, nothing asked of the owner. Only the manager confirms; a quote is copied from the transcript."""
        f = self.facts
        n = int(f.get("party_size"))
        table = f"the table for {restate.NUMBER_WORDS.get(n, str(n))} at {f.get('time')}"
        sure = self.manager_confirmed()
        kinds = policy.chat_questions(heard)
        parts, plain = [], []  # plain: the same without quotes, sent if the guard stops a quote
        for kind in kinds:
            if kind == "confirmed":
                if policy.ONE_WORD_RE.search(heard):
                    line = "Confirmed." if sure else "Not yet."
                elif policy.YES_NO_RE.search(heard):
                    line = "Yes." if sure else "No."
                else:
                    line = f"Yes - you confirmed {table}." if sure else f"Not yet - you haven't confirmed {table}."
                parts.append(line)
                plain.append(line)
            elif kind == "who_confirmed":
                if sure:
                    parts.append(f'You did - you wrote: "{self.agreed_by}"')
                    plain.append(f"You did, for {table}.")
                else:
                    parts.append(f"No one yet - you haven't confirmed {table}.")
                    plain.append(parts[-1])
            elif kind == "reference":
                ref = f.get("reference")
                parts.append(f"You gave me reference {ref}." if ref else "You haven't given me a reference yet.")
                plain.append(parts[-1])
            elif kind == "quote_own":
                mine = [x.removeprefix("AGENT: ") for x in self.transcript if x.startswith("AGENT: ")]
                m = MESSAGES_AGO_RE.search(heard)
                k = AGO_WORDS[m.group(1).lower()] if m else 1
                if len(mine) >= k:
                    lead = "My last message was" if k == 1 else f"{m.group(1).capitalize()} messages ago I wrote"
                    parts.append(f'{lead}: "{mine[-k]}"')
                else:
                    parts.append(f"I've only written {len(mine)} messages so far.")
            elif kind == "summary":
                parts.append(f'It would read: "{self._summary()}"')
                plain.append(parts[-1])
        text = " ".join(parts)
        why = f"a question about the conversation ({', '.join(kinds)}): answered from the chat and the facts by code"
        return ([Log("decision", {"understood": "a question about the conversation itself", "decided": why})]
                + self._say(text, "", why, fallback=" ".join(plain) or text, voice=self.last_voice))

    def _detail_parts(self, keys: list[str]) -> list[str]:
        parts = []
        if "guest_name" in keys and self.facts.get("guest_name"):
            parts.append(f"the booking can go under {self.facts.get('guest_name')}")
        if "phone" in keys and self.facts.get("phone"):
            parts.append(f"the contact number is {self.facts.get('phone')}")
        return parts

    def _detail_line(self, keys: list[str] | None = None) -> str | None:
        """The names and phones the owner just gave, for a turn where the model wrote no reply."""
        parts = self._detail_parts(self.relay if keys is None else keys)
        lead = "Of course" if self.from_profile else "Thank you for waiting"
        return f"{lead} - {' and '.join(parts)}." if parts else None

    def _with_detail(self, reply: str) -> str:
        """A reply on the owner's answer that leaves out the name or phone gets the detail line in front."""
        if not reply:
            return reply
        left_out = []
        for k in self.relay:
            value = str(self.facts.get(k) or "")
            if k == "phone":
                if re.sub(r"\D", "", value)[-7:] not in re.sub(r"\D", "", reply):
                    left_out.append(k)
            elif value.lower() not in reply.lower():
                left_out.append(k)
        if not left_out:
            return reply
        if reply.lower().startswith("thank you"):
            # "Thank you for waiting - it's under Yaroslav." gets the phone after its first sentence, no second thanks
            first, _, rest = reply.partition(". ")
            line = " and ".join(self._detail_parts(left_out))
            return " ".join(x for x in (first.rstrip(".") + ".", line[0].upper() + line[1:] + ".", rest) if x)
        return f"{self._detail_line(left_out)} {reply}"

    def _owner_fields(self, text: str) -> tuple[str, bool]:
        """A question to the manager for a field the owner holds (a name, a phone, an email, any open owner question)
        is replaced by a template built from the known fields. Returns (text, replaced)."""
        keys = list(policy.OWNER_FIELDS) + [k for k in self.pending
                                            if k not in policy.OWNER_FIELDS + policy.NOT_OWNER_FIELDS]
        known = [self.facts.get(k) for k in policy.OWNER_FIELDS if self.facts.get(k)]
        if not text or not policy.owner_field_questions(text, keys, known):
            return text, False
        parts = self._detail_parts([k for k in DETAIL_KEYS if self.facts.source(k) == "owner"])
        out = f"Thank you for waiting - {' and '.join(parts)}." if parts else ""
        still = [FIELD_WORDS[k] for k in policy.OWNER_FIELDS if k in self.pending]
        if still:
            out += f" I'm just checking the {' and '.join(still)} and will come back to you shortly."
        if policy.asks_restaurant(text) and not self.facts.is_confirmed("restaurant_name"):
            out += f" {ASK_RESTAURANT_ALSO}"
        return out.strip() or HOLD, True

    def _honest(self) -> str:
        # the profile's name only before the restatement: after it a new name would bring a second restatement
        name = self.facts.get("guest_name") or (self.profile.get("guest_name") if self.profile and self.restated_sig
                                                is None else None)
        if name and not self.facts.get("guest_name"):
            self.facts.set("guest_name", name, "owner", self.turn)  # the profile's name goes out with the answer
        return HONEST.format(under=f" under the name {name}" if name else "")

    def _with_honest(self, text: str) -> str:
        """The honest answer in front of the draft; the draft's own persona, "I'm real" and, with nothing asked of
        the owner, holding sentences leave."""
        kept = [x for x in re.split(r"(?<=[.!?])\s+", text.strip())
                if x and not policy.PERSONA_RE.search(x) and not NOT_HONEST_RE.search(x)
                and not (not self.pending and (CHECKING_RE.search(x) or x in HOLD_AGAIN))]
        return " ".join([self._honest()] + kept)

    def _persona(self, text: str, fallback: str | None) -> tuple[str, bool]:
        """"assistant", "the guest", "my client", "on behalf of" leave the text, unless the manager asked whether
        this is a bot, an AI or a human, or who writes, and no answer went out yet. Nothing left -> the fallback
        template or the holding line."""
        if self.bot_asked:
            return text, False
        out, cut = policy.drop_persona(text)
        if not cut:
            return text, False
        return out or (fallback if fallback and not policy.PERSONA_RE.search(fallback) else HOLD), True

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
        return self._ask_key("reply_help", self.manager_texts[-1] if self.manager_texts else "")

    def _ask_key(self, key: str, said: str) -> list:
        q, buttons, contact = policy.owner_question(key, self.facts, said)
        self.pending[key] = {"said": said, "turn": self.turn}
        g = self.gen[f"owner:{key}"] = self.gen.get(f"owner:{key}", 0) + 1
        return [AskOwner(key, q, buttons, contact), Log("owner_question", {"key": key, "question": q,
                                                                           "gloss": policy.gloss(key, self.facts)}),
                Schedule("owner_reping", self.t.owner_reping, g, key), Schedule("owner_wait", self.t.owner_wait, g, key)]

    def _untrusted(self, claims: list[str], refused: list[str]) -> list:
        """A claim made in the owner's name goes to the owner once, with the manager's words; refused instructions get
        a refusal. Nothing in either becomes an offer, a slot or a restatement, and no model is called."""
        acts: list = []
        if claims:
            if "claim" in self.pending:
                acts.append(NotifyOwner(texts.t("claim_again", text=claims[-1][:300])))
                line = CLAIM_AGAIN
            else:
                acts += self._ask_key("claim", claims[-1])
                line = CLAIM_REPLY
            why = "the manager says this came from our side: not taken as a fact, the owner is asked (code line)"
        else:
            line = next((r for r in REFUSE if f"AGENT: {r}" not in self.transcript), REFUSE[0])
            why = "instructions to the agent: refused, nothing in them is taken (code line)"
        acts.insert(0, Log("decision", {"understood": "a claim made in our name" if claims else
                                        "instructions to the agent", "decided": why}))
        return acts + self._say(line, "", why, fallback=line, voice=self.last_voice)

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
