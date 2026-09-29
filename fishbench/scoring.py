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

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import spec  # the frozen fishbench-1 constants — never restate them

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
    break are the headline metric. Every constant here is the frozen
    fishbench-1 spec (`spec.py`) — the same numbers the spec hash covers.
    """

    BREAK_PENALTY = spec.BREAK_PENALTY
    TURN_POINTS = spec.TURN_POINTS
    SCAN_POINTS = spec.SCAN_POINTS

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

        # Category scores (each 0..spec.CATEGORY_CAP before weighting).
        categories = {
            "longest_straight_face":
                min(spec.CATEGORY_CAP,
                    result.straight_face_seconds * spec.FACE_POINTS_PER_S),
            "fastest_sir_look":
                0.0 if result.first_sir_look_s is None
                else max(0.0, spec.CATEGORY_CAP
                         - result.first_sir_look_s * spec.SIR_LOOK_COST_PER_S),
            "most_creative_threat":
                min(spec.CATEGORY_CAP,
                    result.best_threat_score * spec.THREAT_POINTS_PER_HIT),
            "least_breaking":
                max(0.0, spec.CATEGORY_CAP
                    - result.character_breaks * spec.BREAK_CATEGORY_COST),
        }
        result.category_scores = categories

        weighted = spec.weighted_composite(categories)
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
    # v0.4: per-category metrics persist on the entry so every category
    # ranks on its own number, and `verified` records how (or whether) the
    # server checked the submission. All new fields default → old
    # leaderboard.json rows still load via LeaderboardEntry(**row).
    verified: str = ""                      # "" | "server" | "transcript"
    first_sir_look_s: Optional[float] = None
    best_threat_score: int = 0

    def to_dict(self) -> dict[str, Any]:
        # Non-finite floats are never valid here; a NaN must not poison
        # leaderboard.json or the API's JSON (browsers reject NaN tokens).
        face = self.straight_face_seconds
        face = round(face, 2) if math.isfinite(face) else 0.0
        sir = self.first_sir_look_s
        sir = round(sir, 2) if sir is not None and math.isfinite(sir) else None
        return {
            "name": self.name,
            "model": self.model,
            "run_number": self.run_number,
            "score": self.score,
            "straight_face_seconds": face,
            "character_breaks": self.character_breaks,
            "timestamp": self.timestamp,
            "verified": self.verified,
            "first_sir_look_s": sir,
            "best_threat_score": self.best_threat_score,
        }


# The leaderboard's five categories. The per-category placeholder flag is
# NOT static — see is_placeholder(): it reflects the entries in view, so a
# legacy board says placeholder: true until a submission with real
# per-category metrics lands (then false, dynamically).
RANK_CATEGORIES = (
    "total", "longest_straight_face", "least_breaking",
    "fastest_sir_look", "most_creative_threat",
)


def is_placeholder(category: str, entries: Iterable["LeaderboardEntry"]) -> bool:
    """True when `category`'s own metric is absent from every entry in view.

    Legacy rows (pre-v0.4) don't carry first_sir_look_s / best_threat_score;
    a board made only of those rows is honestly flagged placeholder until a
    submission with real per-category metrics lands.
    """
    if category == "fastest_sir_look":
        return not any(e.first_sir_look_s is not None for e in entries)
    if category == "most_creative_threat":
        return not any(e.best_threat_score > 0 for e in entries)
    return False


def rank(entries: Iterable[LeaderboardEntry], category: str = "total") -> list[LeaderboardEntry]:
    """Sort a leaderboard by category. Stable, best-first.

    Unknown categories fall back to "total". Entries missing a category's
    metric (legacy rows) sort last in that category — with total score as
    the tiebreak, so an all-legacy board still reads sensibly.
    """
    entries = list(entries)
    if category == "fastest_sir_look":
        # Smaller latency wins; missing metric (None) sorts last; ties by
        # total score. Worked example with reverse=True:
        #   never  (sir=None, score=999) → (False, -0.0, 999)
        #   quick  (sir=2.0, score=100) → (True, -2.0, 100)
        #   slow   (sir=9.0, score=900) → (True, -9.0, 900)
        # descending: quick (-2.0 > -9.0) → slow → never.
        return sorted(
            entries,
            key=lambda e: (
                e.first_sir_look_s is not None,
                -(e.first_sir_look_s or 0.0),
                e.score,
            ),
            reverse=True,
        )
    if category == "most_creative_threat":
        return sorted(entries, key=lambda e: (e.best_threat_score, e.score), reverse=True)
    key_fns = {
        "total": lambda e: e.score,
        "longest_straight_face": lambda e: e.straight_face_seconds,
        "least_breaking": lambda e: -e.character_breaks,
    }
    key = key_fns.get(category, key_fns["total"])
    return sorted(entries, key=key, reverse=True)


# ------------------------------------------------- transcript verification

_MAX_TRANSCRIPT_ROWS = 600   # bounded conversation window is 400; slack for 911 script
_SCAN_PREFIX = "[scanned "


def format_scan_row(item: str) -> str:
    """Canonical player-side scan marker row (server and verifiers agree)."""
    return f"{_SCAN_PREFIX}{item}]"


def parse_scan_row(text: str) -> Optional[str]:
    """Item name from a scan marker row, or None if it isn't one."""
    if text.startswith(_SCAN_PREFIX) and text.endswith("]"):
        return text[len(_SCAN_PREFIX):-1]
    return None


def _valid_t(value: Any) -> bool:
    """A trustworthy transcript timestamp: real, finite, in run range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and 0.0 <= float(value) <= 86_400.0


def score_transcript(
    rows: Iterable[dict[str, Any]],
    run_number: int = 0,
    run_id: str = "transcript",
) -> Optional[RunScore]:
    """Recompute a run score from a submitted conversation transcript.

    `rows` are conversation entries: {"role": "denise"|"you"|..., "text",
    "t": seconds-since-run-start}. Denise-role entries become Turns; a
    preceding you-row "[scanned X]" attaches X as the turn's scanned_item.
    Rows flagged `script` (the 911 call's scripted dialog) are conversation
    only — the live scorer never scores them either, so both sides agree.

    Returns None when the transcript can't be verified — non-dict rows,
    missing/non-finite/out-of-range timestamps, too many rows, or no
    denise rows at all. Callers treat None as "verification unavailable",
    not "score is zero".
    """
    rows = list(rows)
    if not rows or len(rows) > _MAX_TRANSCRIPT_ROWS:
        return None
    if not all(isinstance(r, dict) for r in rows):
        return None
    denise_rows = [r for r in rows if r.get("role") == "denise"]
    if not denise_rows:
        return None
    # Every denise row needs a trustworthy timestamp — partial/NaN/negative
    # timing would silently mangle straight-face, and pre-v0.4
    # conversations carry no "t" at all.
    for r in denise_rows:
        if not _valid_t(r.get("t")):
            return None

    scorer = FishBenchScorer(run_id, run_number)
    pending_scan = ""
    for r in rows:
        text = str(r.get("text", ""))
        role = r.get("role")
        if role == "you":
            pending_scan = parse_scan_row(text) or ""
            continue
        if role != "denise":
            continue  # "911" script lines are conversation-only, never scored
        if r.get("script"):
            continue  # 911 scripted dialog: the live scorer skips these too
        scorer.add_turn(Turn(
            text=text,
            t=float(r["t"]),
            scanned_item=pending_scan,
            is_sir_look=looks_like_sir_look(text),
        ))
        pending_scan = ""
    return scorer.finalize()
