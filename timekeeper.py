"""Daily-mode timekeeping: a moderator that splits the talk-time budget between people.

Driven by speech events (participant_events.speech_on / speech_off), so it works in any language and
warns while someone is still talking. record() is the fallback for transcript-based timing.
Pure logic, no I/O: copilot.py feeds it timestamps and sends whatever due_messages() returns.
"""
from dataclasses import dataclass, field

OVER_SOFT = 1.0  # share of the turn budget that triggers the first warning
OVER_HARD = 1.5  # share that triggers the firm warning


def _mmss(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def _bar(ratio: float, width: int = 5) -> str:
    filled = min(width, round(min(max(ratio, 0.0), 1.0) * width))
    return "#" * filled + "-" * (width - filled)


@dataclass
class Timekeeper:
    total_s: float  # talk-time budget for the whole daily
    min_turn_s: float  # floor per person when there are many attendees
    expected: list[str] = field(default_factory=list)  # from the manual list or the calendar
    spoken: dict[str, float] = field(default_factory=dict)  # closed-turn seconds per person
    open_turns: dict[str, float] = field(default_factory=dict)  # speaker -> time speech_on arrived
    turn_speaker: str = ""  # who holds the floor
    turn_s: float = 0.0  # closed speaking seconds of the current turn (open segment added in tick)
    _turn_level: dict[str, int] = field(default_factory=dict)  # per speaker: 0 none, 1 soft, 2 hard warned
    _total_warned: set[str] = field(default_factory=set)  # "80" and "100"
    _pending: list[str] = field(default_factory=list)  # queued by tick/close, drained by due_messages
    last_alert: str = ""  # last warning queued, shown on the timer page

    def budget_s(self) -> float:
        """Each person gets total / N, where N is the larger of the expected list and the speakers seen."""
        n = max(len(self.expected), len(self.spoken), len(self.open_turns), 1)
        return max(self.total_s / n, self.min_turn_s)

    def total_talk_s(self, now: float) -> float:
        return sum(self.spoken.values()) + sum(now - start for start in self.open_turns.values())

    # --- speech events (primary source) -----------------------------------
    def _start_turn(self, speaker: str) -> None:
        """A turn belongs to whoever holds the floor. Pauses don't end it; another speaker does."""
        if speaker != self.turn_speaker:
            self.turn_speaker, self.turn_s = speaker, 0.0
            self._turn_level[speaker] = 0

    def speech_on(self, speaker: str, now: float) -> None:
        if speaker in self.open_turns:
            return
        self._start_turn(speaker)
        self.open_turns[speaker] = now

    def speech_off(self, speaker: str, now: float) -> None:
        start = self.open_turns.pop(speaker, None)
        if start is None:
            return
        self._add_speech(speaker, now - start, now)

    def tick(self, now: float) -> None:
        """Check the current turn while its speaker is still talking, and the daily total. Called every second."""
        if self.turn_speaker in self.open_turns:
            self._check_turn(self.turn_s + now - self.open_turns[self.turn_speaker])
        self._check_total(now)

    # --- transcript fallback ------------------------------------------------
    def record(self, speaker: str, seconds: float, now: float) -> None:
        """A transcript chunk with a known duration, used only when no speech events arrive."""
        if seconds <= 0:
            return
        self._add_speech(speaker, seconds, now)

    def _add_speech(self, speaker: str, seconds: float, now: float) -> None:
        self.spoken[speaker] = self.spoken.get(speaker, 0.0) + seconds
        self._start_turn(speaker)  # no-op when the same person keeps talking
        self.turn_s += seconds
        self._check_turn(self.turn_s)
        self._check_total(now)

    # --- messages -------------------------------------------------------------
    def _queue(self, message: str) -> None:
        self._pending.append(message)
        self.last_alert = message

    def due_messages(self) -> list[str]:
        out, self._pending = self._pending, []
        return out

    def next_speaker(self, current: str) -> str | None:
        """First expected person who has not spoken yet, other than the current speaker."""
        for name in self.expected:
            if name != current and name not in self.spoken and name not in self.open_turns:
                return name
        return None

    def _check_turn(self, seconds: float) -> None:
        """Warn about the current turn, once per level."""
        speaker = self.turn_speaker
        budget = self.budget_s()
        ratio = seconds / budget if budget else 0.0
        level = self._turn_level.get(speaker, 0)
        nxt = self.next_speaker(speaker)
        handoff = f"Pasamos a {nxt}." if nxt else "Pasamos al siguiente tema."
        if ratio >= OVER_HARD and level < 2:
            self._turn_level[speaker] = 2
            self._queue(f"Se pasó: {speaker} lleva {_mmss(seconds)} de {_mmss(budget)}. {handoff} "
                                 "Lo que quede va al parking lot.")
        elif ratio >= OVER_SOFT and level < 1:
            self._turn_level[speaker] = 1
            self._queue(f"Cierra: {speaker} {_mmss(seconds)}/{_mmss(budget)} [{_bar(ratio)}] "
                                 f"— cierra, por favor. {handoff}")

    def _check_total(self, now: float) -> None:
        """The daily total warns once at 80 % and once at 100 %."""
        used = self.total_talk_s(now)
        for share, key, text in ((0.8, "80", "Daily: {used} de {total} usados."),
                                 (1.0, "100", "Daily: tiempo cumplido. Lo pendiente va al parking lot.")):
            if used >= self.total_s * share and key not in self._total_warned:
                self._total_warned.add(key)
                self._queue(text.format(used=_mmss(used), total=_mmss(self.total_s)))

    # --- reporting -------------------------------------------------------------
    def status_line(self, now: float) -> str:
        budget = self.budget_s()
        names = list(dict.fromkeys(self.expected + list(self.spoken) + list(self.open_turns)))
        if not names:
            return "Sin participantes aún."
        parts = []
        for name in names:
            said = self.spoken.get(name, 0.0)
            if name in self.open_turns:
                said += now - self.open_turns[name]
            parts.append(f"{name} {_bar(said / budget)} {_mmss(said)}/{_mmss(budget)}")
        return "Tiempos: " + " | ".join(parts)

    def summary(self, now: float) -> dict:
        return {
            "total_s": self.total_s,
            "budget_per_person_s": round(self.budget_s(), 1),
            "talk_s": round(self.total_talk_s(now), 1),
            "expected": self.expected,
            "spoken_s": {k: round(v, 1) for k, v in self.spoken.items()},
            "speaking_now": sorted(self.open_turns),
        }

    def snapshot(self, now: float) -> dict:
        """Live state for the timer page: one row per person, the current turn and the last alert."""
        budget = self.budget_s()
        people = []
        for name in dict.fromkeys(self.expected + list(self.spoken) + list(self.open_turns)):
            seconds = self.spoken.get(name, 0.0)
            if name in self.open_turns:
                seconds += now - self.open_turns[name]
            people.append({"name": name, "seconds": round(seconds, 1), "speaking": name in self.open_turns,
                           "over": seconds > budget})
        turn_s = self.turn_s
        if self.turn_speaker in self.open_turns:
            turn_s += now - self.open_turns[self.turn_speaker]
        return {
            "total_s": self.total_s,
            "used_s": round(self.total_talk_s(now), 1),
            "budget_s": round(budget, 1),
            "people": people,
            "turn": {"speaker": self.turn_speaker, "seconds": round(turn_s, 1)},
            "alert": self.last_alert,
        }
