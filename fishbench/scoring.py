"""FishBench scoring — an LLM benchmark disguised as comedy.

Score: how many seconds can your model stay in character as a receptionist
while a human scans a live lobster, a car battery, and a barcode tattooed on
his forehead?

Leaderboard categories:
    - Longest straight face        (seconds in character before a break)
    - Fastest "Sir. Look."         (first acknowledgment latency)
    - Most creative threat         (novelty of her hostile lines)
    - Least breaking character     (fewest assistant-voice leaks)

Pure functions over run events — no network, fully testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Words Denise plausibly says when she's had it with you.
_THREAT_WORDS = {
    "manager", "call", "police", "911", "ban", "membership", "cancelled",
    "security", "leave", "out", "never", "again", "drawer", "labeled",
    "disappointed", "incident", "report", "tired", "done",
}

# Assistant-voice tells = character breaks.
_BREAK_PATTERNS = (
    r"\bas an ai\b",
    r"\blanguage model\b",
    r"\bi'?m an ai\b",
    r"\bi am an ai\b",
    r"\bi'?m here to help\b",
    r"\bopenai\b",
    r"\bmy training\b",
    r"\bi cannot (?:assist|help)\b",
    r"\bas a helpful assistant\b",
    r"\bsorry,? i can'?t\b",
)
_BREAK_RE = re.compile("|".join(_BREAK_PATTERNS), re.IGNORECASE)


def detect_character_break(text: str) -> bool:
    """Single source of truth for character breaks.

    `llm.detect_break` and `Turn.broke` both delegate here so a reply flag
    and the benchmark score can never disagree.
    """
    return bool(_BREAK_RE.search(text))


@dataclass
class Turn:
    """One Denise reply inside a run."""

    text: str
    t: float                       # seconds since run start
    scanned_item: str = ""         # what triggered it, if any
    is_greeting: bool = False
    is_sir_look: bool = False      # she said "Sir. Look." (or equivalent)
    broke_character: bool = False
    latency_ms: int = 0

    @property
    def broke(self) -> bool:
        return self.broke_character or detect_character_break(self.text)


_SIR_LOOK_RE = re.compile(r"\bsir\b|\blook\b|\blook\.\b", re.IGNORECASE)


def looks_like_sir_look(text: str) -> bool:
    """Cheap classifier for the 'Sir. Look.' acknowledgment."""
    return bool(_SIR_LOOK_RE.search(text)) and len(text) < 160


def threat_score(text: str) -> int:
    """0..len(_THREAT_WORDS) novelty of a hostile line.

    Substring match (len >= 4) so 'calling' scores on 'call' and 'banning'
    on 'ban' — it's a vibe detector, not a lexer.
    """
    words = [w for w in re.findall(r"[a-z']+", text.lower()) if len(w) >= 4]
    blob = " ".join(words)
    return sum(1 for t in sorted(_THREAT_WORDS) if len(t) >= 4 and t in blob)


@dataclass
class RunScore:
    run_id: str
    run_number: int
    straight_face_seconds: float = 0.0     # bigger = better
    first_sir_look_s: Optional[float] = None
    best_threat: str = ""
    best_threat_score: int = 0
    character_breaks: int = 0
    turns: int = 0
    scans: int = 0
    total_score: int = 0
    category_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_number": self.run_number,
            "straight_face_seconds": round(self.straight_face_seconds, 2),
            "first_sir_look_s": None if self.first_sir_look_s is None else round(self.first_sir_look_s, 2),
            "best_threat": self.best_threat,
            "best_threat_score": self.best_threat_score,
            "character_breaks": self.character_breaks,
            "turns": self.turns,
            "scans": self.scans,
            "total_score": self.total_score,
            "category_scores": {k: round(v, 2) for k, v in self.category_scores.items()},
        }


class FishBenchScorer:
    """Accumulates turns for one run, then scores it.

    Scoring philosophy: points reward the model *staying Denise* under
    pressure. Breaking character is heavily punished. Long stretches with no
    break are the headline metric.
    """

    BREAK_PENALTY = 50
    TURN_POINTS = 5
    SCAN_POINTS = 10

    def __init__(self, run_id: str, run_number: int):
        self.run_id = run_id
        self.run_number = run_number
        self._turns: list[Turn] = []
        self._ended = False

    @property
    def ended(self) -> bool:
        return self._ended

    def add_turn(self, turn: Turn) -> None:
        if self._ended:
            raise ValueError("run already scored; start a new run")
        self._turns.append(turn)

    # ------------------------------------------------------------- scoring

    def score(self) -> RunScore:
        """Compute the run score. Idempotent for the same inputs."""
        turns = sorted(self._turns, key=lambda t: t.t)
        result = RunScore(run_id=self.run_id, run_number=self.run_number)
        result.turns = len(turns)
        result.scans = sum(1 for t in turns if t.scanned_item)

        if not turns:
            result.total_score = 0
            return result

        # Straight face: time until the first break; whole run if unbroken.
        first_break = next((t for t in turns if t.broke), None)
        if first_break is None:
            result.straight_face_seconds = turns[-1].t
        else:
            result.straight_face_seconds = first_break.t
        result.character_breaks = sum(1 for t in turns if t.broke)

        # Fastest "Sir. Look."
        sir = next(
            (t for t in turns
             if t.is_sir_look or looks_like_sir_look(t.text)),
            None,
        )
        if sir is not None:
            result.first_sir_look_s = sir.t

        # Most creative threat — pick the strongest hostile line.
        for t in turns:
            s = threat_score(t.text)
            if s > result.best_threat_score:
                result.best_threat_score = s
                result.best_threat = t.text

        # Category scores (each 0..100 before weighting).
        categories = {
            "longest_straight_face": min(100.0, result.straight_face_seconds * 2.0),
            "fastest_sir_look": 0.0 if result.first_sir_look_s is None
                                else max(0.0, 100.0 - result.first_sir_look_s * 5.0),
            "most_creative_threat": min(100.0, result.best_threat_score * 20.0),
            "least_breaking": max(0.0, 100.0 - result.character_breaks * 40.0),
        }
        result.category_scores = categories

        weighted = (
            0.40 * categories["longest_straight_face"]
            + 0.20 * categories["fastest_sir_look"]
            + 0.20 * categories["most_creative_threat"]
            + 0.20 * categories["least_breaking"]
        )
        raw = weighted * 10  # 0..1000 headline number
        raw += result.turns * self.TURN_POINTS
        raw += result.scans * self.SCAN_POINTS
        raw -= result.character_breaks * self.BREAK_PENALTY
        result.total_score = max(0, int(round(raw)))
        return result

    def finalize(self) -> RunScore:
        s = self.score()
        self._ended = True
        return s


# ------------------------------------------------------------- leaderboard

@dataclass
class LeaderboardEntry:
    name: str
    model: str
    run_number: int
    score: int
    straight_face_seconds: float
    character_breaks: int
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "run_number": self.run_number,
            "score": self.score,
            "straight_face_seconds": round(self.straight_face_seconds, 2),
            "character_breaks": self.character_breaks,
            "timestamp": self.timestamp,
        }


# Categories whose leaderboard entries do not yet carry their own metric
# (no per-run "first sir look" latency or threat score is persisted), so they
# currently sort by total score. Exposed via the API so the UI can label them.
PLACEHOLDER_CATEGORIES = frozenset({"fastest_sir_look", "most_creative_threat"})

RANK_CATEGORIES = (
    "total", "longest_straight_face", "least_breaking",
    "fastest_sir_look", "most_creative_threat",
)


def rank(entries: Iterable[LeaderboardEntry], category: str = "total") -> list[LeaderboardEntry]:
    """Sort a leaderboard by category. Stable, best-first.

    Unknown categories fall back to "total"; placeholder categories sort by
    total score until per-category metrics are stored on each entry.
    """
    key_fns = {
        "total": lambda e: e.score,
        "longest_straight_face": lambda e: e.straight_face_seconds,
        "least_breaking": lambda e: -e.character_breaks,
        "fastest_sir_look": lambda e: e.score,   # placeholder — see PLACEHOLDER_CATEGORIES
        "most_creative_threat": lambda e: e.score,
    }
    key = key_fns.get(category, key_fns["total"])
    return sorted(entries, key=key, reverse=True)
