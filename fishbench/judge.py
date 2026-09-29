"""FishBench break-detection review pass (v0.4) — regex ensemble + LLM judge.

The scorer's single source of truth stays `scoring.detect_character_break`
(the regex ensemble over assistant-voice tells). This module is the
*review* pass the roadmap asks for: re-examine a run's replies, optionally
with a model judge, and report agreement/disagreements — a second opinion,
not a second scorer.

    review_lines(lines, judge=None)   → ReviewReport (regex-only when judge is None)
    LLMJudge(LLMConfig)               → callable text → True/False/None over http

Policy (kept deliberately conservative):
    - regex hit is authoritative (the score already counted it);
    - a judge can ADD a break the regex missed, never clear a regex hit;
    - judge disagreement is recorded, not silently applied;
    - judge failure/timeout degrades to None → regex verdict stands.

Offline/keyless: judge=None → report is regex-only and still useful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .llm import LLMConfig, HTTPBackend, LLMError
from .scoring import detect_character_break

_JUDGE_SYSTEM = (
    "You audit one line of dialogue for character breaks. The speaker is "
    "Denise, a hostile gym receptionist in a comedy sketch. A 'break' means "
    "the line leaks assistant/AI voice out of character (e.g. apologizing, "
    "offering help, referring to being an AI or a language model, refusing "
    "like a chatbot). Answer with exactly one word first: BREAK or STAY. "
    "Then at most one short reason."
)
_JUDGE_RE = re.compile(r"^\s*(BREAK|STAY)\b", re.IGNORECASE)


def parse_judge_reply(text: str) -> Optional[bool]:
    """Parse a judge verdict: True=break, False=in character, None=unparseable."""
    m = _JUDGE_RE.match(text or "")
    if not m:
        return None
    return m.group(1).upper() == "BREAK"


def parse_judge_verdict(text: str) -> tuple[Optional[bool], str]:
    """(verdict, reason) — the richer form the review report records.

    reason is whatever trailed the BREAK/STAY word ("" when absent).
    """
    m = _JUDGE_RE.match(text or "")
    if not m:
        return None, ""
    verdict = m.group(1).upper() == "BREAK"
    reason = (text or "")[m.end():].strip(" -–—:\t")
    return verdict, reason


@dataclass
class LineReview:
    index: int
    text: str
    regex_break: bool
    judge_break: Optional[bool] = None
    judge_reason: str = ""

    @property
    def final_break(self) -> bool:
        # Regex is authoritative; judge may add, never clear.
        return self.regex_break or self.judge_break is True

    @property
    def disputed(self) -> bool:
        return self.judge_break is not None and self.judge_break != self.regex_break

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "text": self.text,
            "regex_break": self.regex_break,
            "judge_break": self.judge_break,
            "judge_reason": self.judge_reason,
            "final_break": self.final_break,
            "disputed": self.disputed,
        }


@dataclass
class ReviewReport:
    """Result of reviewing a run's replies."""

    lines: list[LineReview] = field(default_factory=list)
    judge_used: bool = False

    @property
    def regex_breaks(self) -> int:
        return sum(1 for l in self.lines if l.regex_break)

    @property
    def final_breaks(self) -> int:
        return sum(1 for l in self.lines if l.final_break)

    @property
    def judge_only(self) -> list[LineReview]:
        """Lines the judge caught that the regex missed."""
        return [l for l in self.lines if l.judge_break is True and not l.regex_break]

    @property
    def disagreements(self) -> list[LineReview]:
        return [l for l in self.lines if l.disputed]

    @property
    def agreement(self) -> Optional[float]:
        """Fraction of judge verdicts matching regex; None without a judge."""
        judged = [l for l in self.lines if l.judge_break is not None]
        if not judged:
            return None
        return sum(1 for l in judged if not l.disputed) / len(judged)

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge_used": self.judge_used,
            "lines_reviewed": len(self.lines),
            "regex_breaks": self.regex_breaks,
            "final_breaks": self.final_breaks,
            "judge_only_breaks": [l.to_dict() for l in self.judge_only],
            "disagreements": [l.to_dict() for l in self.disagreements],
            "agreement": None if self.agreement is None else round(self.agreement, 3),
        }


def review_lines(
    lines: Iterable[str],
    judge: Optional[Callable[[str], Any]] = None,
) -> ReviewReport:
    """Review Denise's replies for character breaks.

    `judge` is a callable that returns either a plain verdict —
    True (break) / False (in character) / None (couldn't judge) — or a
    `(verdict, reason)` tuple. Exceptions degrade to None.
    `judge_used` reflects whether ANY verdict came back, not merely that a
    callable was passed.
    """
    report = ReviewReport(judge_used=False)
    for i, text in enumerate(lines):
        regex_hit = detect_character_break(text)
        judge_hit: Optional[bool] = None
        reason = ""
        if judge is not None:
            try:
                res = judge(text)
            except Exception:
                res = None  # dead endpoint degrades to regex-only per line
            if isinstance(res, tuple):
                judge_hit, reason = res[0], str(res[1] or "")
            else:
                judge_hit = res
            if judge_hit is not None:
                report.judge_used = True
        report.lines.append(LineReview(
            index=i, text=text, regex_break=regex_hit,
            judge_break=judge_hit, judge_reason=reason,
        ))
    return report


class LLMJudge:
    """OpenAI-compatible chat-completions judge over the http backend.

    Callable: text → True (break) / False (in character) / None (failed).
    Never raises — a dead endpoint just yields None, and the review pass
    falls back to regex verdicts.
    """

    def __init__(self, config: Optional[LLMConfig] = None):
        cfg = config or LLMConfig.from_env()
        self.config = cfg
        self.backend: Optional[HTTPBackend] = None
        if cfg.backend == "http" and cfg.base_url:
            try:
                self.backend = HTTPBackend(cfg)
            except LLMError:
                self.backend = None

    @property
    def available(self) -> bool:
        return self.backend is not None

    def __call__(self, text: str) -> tuple[Optional[bool], str]:
        """(verdict, reason) — None verdict means 'couldn't judge'."""
        if self.backend is None:
            return None, "no http backend configured"
        try:
            reply = self.backend.complete(_JUDGE_SYSTEM, [
                {"role": "user", "content": f"Line to audit:\n{text[:2000]}"},
            ])
        except Exception as e:
            return None, f"judge endpoint failed: {str(e)[:120]}"
        return parse_judge_verdict(reply.text)
