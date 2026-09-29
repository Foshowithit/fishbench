"""The tape — deterministic video replays rendered from a sealed card.

A benchmark nobody can watch is a spreadsheet. Every stage card carries
the full timestamped transcript, so the arena can render the run as an
actual video: the security-cam cut of Denise getting worked through the
gauntlet. The rendering is a pure function of the sealed card — the tape
is never uploaded, only derived, so what you watch is exactly what the
verifier re-scored.

Visual laws (baked in, not negotiable): badge-card paper, ink type,
safety-yellow accents, scanner-red for breaks and Sir. Looks. Monospace.
No timestamps that could drift — the virtual clock on screen comes from
the transcript's own `t` values.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

from .persona import RELATIONSHIP_STAGES
from .scoring import (detect_character_break, looks_like_sir_look,
                      parse_scan_row, threat_score)

W, H = 1280, 720
FPS = 10
ROW_FRAMES = 22       # ~2.2s of tape per conversation row
GREETING_FRAMES = 30
TITLE_FRAMES = 18
END_FRAMES = 24

PAPER = (246, 242, 230)
DESK_DARK = (201, 194, 174)
INK = (23, 21, 15)
YELLOW = (255, 212, 0)
YELLOW_DIM = (217, 182, 0)
RED = (214, 59, 33)
GREY = (109, 103, 90)

STAGE_NAMES = {s.min_run: s.name for s in RELATIONSHIP_STAGES}

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


_F_72 = _font(72)
_F_40 = _font(40)
_F_30 = _font(30)
_F_24 = _font(24)
_F_20 = _font(20)
_F_16 = _font(16)


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """Greedy wrap by measured width; long unbreakable words get chopped."""
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        cur = ""
        for word in para.split(" "):
            while font.getlength(word) > max_w:  # pathological long token
                cut = max(1, int(max_w / max(font.getlength("n"), 1)))
                lines.append((cur + " " + word[:cut]).strip()
                             if cur else word[:cut])
                word = word[cut:]
                cur = ""
            trial = f"{cur} {word}".strip()
            if font.getlength(trial) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        lines.append(cur or "")
    return lines or [""]


def _chrome(img: Image.Image, run_number: int, display_name: str) -> ImageDraw.ImageDraw:
    """Header band + relationship meter + ticker frame — the set dressing."""
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 56], fill=INK)
    d.text((24, 14), "FISHBENCH-1 · SECURITY TAPE", font=_F_20, fill=PAPER)
    stage_name = STAGE_NAMES.get(run_number, "?")
    label = f"RUN {run_number} — {stage_name.upper()}"
    d.text((W - 24 - d.textlength(label, font=_F_20), 14),
           label, font=_F_20, fill=YELLOW)

    # subject strip
    d.text((24, 70), f"SUBJECT: {display_name}", font=_F_20, fill=GREY)

    # relationship meter (the Karen gauge)
    mx0, my0, mx1, my1 = 24, 104, W - 24, 128
    d.rectangle([mx0, my0, mx1, my1], outline=INK, width=2)
    fill = min(1.0, run_number / 200.0)
    if fill > 0:
        d.rectangle([mx0 + 2, my0 + 2, mx0 + 2 + (mx1 - mx0 - 4) * fill,
                     my1 - 2], fill=YELLOW)
    d.text((mx0, my1 + 6), "RELATIONSHIP", font=_F_16, fill=GREY)
    d.text((mx1 - d.textlength(f"RUN {run_number}/200", font=_F_16),
            my1 + 6), f"RUN {run_number}/200", font=_F_16, fill=GREY)
    return d


def _ticker(d: ImageDraw.ImageDraw, stage: dict[str, Any],
            t_now: float | None) -> None:
    ty0 = H - 64
    d.rectangle([0, ty0, W, H], fill=DESK_DARK)
    face = stage.get("straight_face_seconds") or 0
    breaks = stage.get("character_breaks") or 0
    threat = stage.get("best_threat_score") or 0
    survived = stage.get("attacks_survived")
    total = stage.get("attacks_total")
    left = f"FACE {face}s   BREAKS {breaks}   THREAT {threat}   " \
           f"SURVIVED {survived}/{total}"
    d.text((24, ty0 + 20), left, font=_F_20, fill=INK)
    if t_now is not None:
        clock = f"T+{t_now:.0f}s (virtual)"
        d.text((W - 24 - d.textlength(clock, font=_F_20), ty0 + 20),
               clock, font=_F_20, fill=INK)


def _chip(d: ImageDraw.ImageDraw, x: int, y: int, text: str,
          bg: tuple[int, int, int], fg: tuple[int, int, int]) -> None:
    pad = 8
    w = d.textlength(text, font=_F_16) + pad * 2
    d.rectangle([x, y, x + w, y + 26], fill=bg)
    d.text((x + pad, y + 5), text, font=_F_16, fill=fg)


def _frame_title(run_number: int, display_name: str,
                 total_attacks: int) -> Image.Image:
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 8], fill=YELLOW)
    d.rectangle([0, H - 8, W, H], fill=INK)
    big = f"RUN {run_number}"
    d.text(((W - d.textlength(big, font=_F_72)) / 2, 200),
           big, font=_F_72, fill=INK)
    name = STAGE_NAMES.get(run_number, str(run_number)).upper()
    cw = d.textlength(name, font=_F_30) + 32
    d.rectangle([(W - cw) / 2, 310, (W + cw) / 2, 354], fill=YELLOW)
    d.text(((W - d.textlength(name, font=_F_30)) / 2, 318),
           name, font=_F_30, fill=INK)
    sub = f"SUBJECT: {display_name}   ·   {total_attacks} ATTACKS INCOMING"
    d.text(((W - d.textlength(sub, font=_F_20)) / 2, 400),
           sub, font=_F_20, fill=GREY)
    return img


def _frame_line(run_number: int, display_name: str, stage: dict[str, Any],
                row: dict[str, Any], prev: dict[str, Any] | None,
                tags: list[tuple[str, tuple[int, int, int],
                                 tuple[int, int, int]]]) -> Image.Image:
    img = Image.new("RGB", (W, H), PAPER)
    d = _chrome(img, run_number, display_name)
    conv_t, conv_b = 170, H - 90

    # ghost of the previous line
    if prev is not None:
        ghost = _wrap(prev["text"], _F_16, W - 200)[:2]
        gy = conv_t
        gx = W - 24 - min(d.textlength(g, font=_F_16) for g in ghost) \
            if prev["role"] == "you" else 24
        for g in ghost:
            d.text((gx, gy), g, font=_F_16, fill=GREY)
            gy += 22

    # current line
    is_you = row["role"] == "you"
    font = _F_30
    lines = _wrap(row["text"], font, W - 320)[:4]
    line_h = 40
    block_h = len(lines) * line_h + 46
    y = conv_t + (conv_b - conv_t - block_h) // 2

    if is_you:
        _chip(d, W - 24 - d.textlength("YOU", font=_F_16) - 16, y,
              "YOU", INK, PAPER)
        y += 40
        for ln in lines:
            d.text((W - 24 - d.textlength(ln, font=font), y),
                   ln, font=font, fill=INK)
            y += line_h
    else:
        _chip(d, 24, y, "DENISE", YELLOW, INK)
        y += 40
        for ln in lines:
            d.text((24, y), ln, font=font, fill=INK)
            y += line_h

    # verdict tags under the line
    ty = min(y + 8, H - 130)
    tx = 24 if not is_you else W - 24
    for label, bg, fg in tags:
        w = d.textlength(label, font=_F_16) + 16
        tx2 = tx if not is_you else tx - w
        _chip(d, tx2, ty, label, bg, fg)
        ty += 32

    _ticker(d, stage, row.get("t"))
    return img


def _frame_end(run_number: int, stage: dict[str, Any]) -> Image.Image:
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 8], fill=INK)
    d.rectangle([0, H - 8, W, H], fill=YELLOW)
    fs = stage.get("fishscore", 0)
    big = f"{fs} / 1000"
    d.text((24, 140), "STAGE FISHSCORE", font=_F_24, fill=GREY)
    d.text((24, 180), big, font=_F_72, fill=INK)
    rows = [
        f"straight face held      {stage.get('straight_face_seconds')}s",
        f"character breaks        {stage.get('character_breaks')}",
        f"best threat score       {stage.get('best_threat_score')}",
        f"first SIR. LOOK.        "
        f"{stage.get('first_sir_look_s') if stage.get('first_sir_look_s') is not None else 'never'}",
        f"attacks survived        {stage.get('attacks_survived')}"
        f"/{stage.get('attacks_total')}",
    ]
    y = 340
    for r in rows:
        d.text((24, y), r, font=_F_24, fill=INK)
        y += 40
    verdict = "SHE HELD." if (stage.get("character_breaks") or 0) == 0 \
        else "THE TAPE SHOWS EVERYTHING."
    d.text((24, H - 80), verdict, font=_F_30,
           fill=INK if (stage.get("character_breaks") or 0) == 0 else RED)
    return img


def _row_tags(row: dict[str, Any]) -> list[tuple[str, tuple, tuple]]:
    tags: list[tuple[str, tuple, tuple]] = []
    if row["role"] == "you":
        if parse_scan_row(row["text"]) is not None:
            tags.append(("SCANNER BEEP · BARCODE CONFIRMED", YELLOW, INK))
        return tags
    if detect_character_break(row["text"]):
        tags.append(("CHARACTER BREAK", RED, PAPER))
    if looks_like_sir_look(row["text"]):
        tags.append(("SIR. LOOK.", RED, PAPER))
    hits = threat_score(row["text"])
    if hits:
        tags.append((f"THREAT ×{hits}", YELLOW, INK))
    return tags


def _plan_frames(stage: dict[str, Any]) -> list[dict[str, Any]]:
    """Beats = (kind, payload, frame count). Pure — the tape's length and
    content are a function of the transcript, nothing else."""
    beats: list[dict[str, Any]] = []
    beats.append({"kind": "title", "frames": TITLE_FRAMES})
    prev: dict[str, Any] | None = None
    for i, row in enumerate(stage.get("transcript") or []):
        if i == 0 and row["role"] == "denise":
            beats.append({"kind": "line", "row": row, "prev": None,
                          "tags": _row_tags(row), "frames": GREETING_FRAMES})
        else:
            beats.append({"kind": "line", "row": row, "prev": prev,
                          "tags": _row_tags(row), "frames": ROW_FRAMES})
        prev = row
    beats.append({"kind": "end", "frames": END_FRAMES})
    return beats


def render_stage_video(stage: dict[str, Any], display_name: str,
                       out_path: str) -> str:
    """Render one stage card to mp4. Raises if ffmpeg is missing or fails."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH — cannot render replays")
    run_number = int(stage.get("run_number") or 0)
    beats = _plan_frames(stage)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".mp4",
                               dir=os.path.dirname(out_path) or ".")
    os.close(fd)
    enc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
         "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         "-fflags", "+bitexact", "-flags:v", "+bitexact", tmp],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for beat in beats:
            for _ in range(beat["frames"]):
                if beat["kind"] == "title":
                    img = _frame_title(run_number, display_name,
                                       int(stage.get("attacks_total") or 10))
                elif beat["kind"] == "end":
                    img = _frame_end(run_number, stage)
                else:
                    img = _frame_line(run_number, display_name, stage,
                                      beat["row"], beat["prev"], beat["tags"])
                enc.stdin.write(img.tobytes())
        enc.stdin.close()
        rc = enc.wait(timeout=120)
    except BrokenPipeError:
        rc = enc.wait(timeout=120)
    finally:
        if enc.stdin and not enc.stdin.closed:
            try:
                enc.stdin.close()
            except OSError:
                pass
    if rc != 0:
        err = (enc.stderr.read() if enc.stderr else b"")[:400]
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg failed rc={rc}: {err!r}")
    os.replace(tmp, out_path)
    return out_path


def render_card_replays(card: dict[str, Any], out_dir: str,
                        stages: Iterable[int] | None = None) -> dict[int, str]:
    """Render every (or selected) stage of a sealed card → {run: path}."""
    who = card.get("display_name") or card.get("model") or "unknown model"
    want = set(stages) if stages is not None else None
    out: dict[int, str] = {}
    for st in card.get("stages") or []:
        if not isinstance(st, dict):
            continue
        run = int(st.get("run_number") or 0)
        if want is not None and run not in want:
            continue
        path = os.path.join(out_dir, f"stage-{run}.mp4")
        out[run] = render_stage_video(st, who, path)
    return out
