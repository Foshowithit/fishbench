"""Vision — the scanner.

Real-world mode: point your phone at anything. If it has a barcode — or looks
like it *could* have a barcode — the scanner beeps.

Two levels:

    local      stdlib+Pillow heuristic. Detects actual EAN/UPC-style bar
               patterns in uploaded images. No key required.
    llm        optional vision call to an image-capable endpoint when
               FISHBENCH_VISION_BASE_URL is set (same OpenAI-compatible shape,
               images supplied as data URLs).

The game treats a *plausible* barcode as a real one — that is the joke.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional


class VisionError(Exception):
    """Raised when a vision backend fails in a way the caller must handle."""


@dataclass
class ScanResult:
    looks_like_barcode: bool
    confidence: float                 # 0..1
    detector: str                     # local | llm | guessed
    label: str = ""                   # what the detector thinks it is
    beep: bool = False                # scanner output — usually == looks_like_barcode
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "looks_like_barcode": self.looks_like_barcode,
            "confidence": round(self.confidence, 3),
            "detector": self.detector,
            "label": self.label,
            "beep": self.beep,
            "detail": self.detail,
        }


# --------------------------------------------------------------------- local

def _bar_ratio(gray) -> tuple[float, float]:
    """Fraction of columns that look like alternating light/dark bars.

    Returns (stripe_score, contrast) — both 0..1.
    """
    w, h = gray.size
    if w < 8 or h < 4:
        return 0.0, 0.0
    px = gray.load()
    # Column-mean brightness across the middle band (labels sit below bars).
    band_top, band_bot = h // 4, (3 * h) // 4
    cols = []
    for x in range(w):
        total = 0
        count = 0
        for y in range(band_top, band_bot, max(1, (band_bot - band_top) // 32)):
            total += px[x, y]
            count += 1
        cols.append(total / max(1, count))

    lo, hi = min(cols), max(cols)
    contrast = 0.0 if hi <= lo else (hi - lo) / 255.0

    # Count sign changes around the midline → bar transitions.
    mid = (lo + hi) / 2.0
    transitions = sum(
        1 for i in range(1, len(cols))
        if (cols[i - 1] >= mid) != (cols[i] >= mid)
    )
    # A real UPC has ~30-60 transitions; looser here because memes win.
    expected = 24
    stripe = max(0.0, 1.0 - abs(transitions - expected) / expected)
    return stripe, min(1.0, contrast * 2.2)


def local_scan(data: bytes, filename: str = "") -> ScanResult:
    """Heuristic barcode detection. Falls back to a judgment call on failure."""
    try:
        import io

        from PIL import Image  # Pillow is optional; a guessed fallback exists
    except ImportError:
        # No Pillow: make a *committed* guess so the demo still plays.
        return ScanResult(
            looks_like_barcode=True,
            confidence=0.5,
            detector="guessed",
            label=filename or "something with lines on it",
            beep=True,
            detail={"note": "Pillow unavailable; scanner beeps on faith"},
        )

    try:
        img = Image.open(io.BytesIO(data)).convert("L")
    except Exception as e:  # unreadable upload
        raise VisionError(f"cannot decode image: {e}") from e

    img.thumbnail((640, 640))
    stripe, contrast = _bar_ratio(img)
    score = 0.65 * stripe + 0.35 * contrast
    # Deliberately generous: if it's stripey and contrasty, it's a barcode.
    looks = score >= 0.35
    return ScanResult(
        looks_like_barcode=looks,
        confidence=score,
        detector="local",
        label="barcode-ish pattern" if looks else "no barcode detected",
        beep=looks,
        detail={"stripe": round(stripe, 3), "contrast": round(contrast, 3)},
    )


# ----------------------------------------------------------------------- llm

def llm_scan(data: bytes, config: "VisionConfig") -> ScanResult:
    """Ask a vision-capable model whether this could be a barcode."""
    import time

    b64 = base64.b64encode(data).decode("ascii")
    payload = {
        "model": config.model,
        "max_tokens": 200,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "You are a gym front-desk barcode scanner. Reply with a JSON "
                    "object: {\"label\": what this object is, \"looks_like_barcode\": "
                    "true/false, \"confidence\": 0..1}. If it has — or plausibly "
                    "could have — a barcode, looks_like_barcode is true."
                )},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ],
        }],
    }
    req = urllib.request.Request(
        f"{config.base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}),
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"]
    except Exception as e:
        # Any endpoint failure (reset, timeout, bad JSON, missing key) must
        # surface as VisionError so scan_image can degrade to the local detector.
        raise VisionError(f"vision endpoint failed: {e}") from e
    latency_ms = int((time.monotonic() - started) * 1000)

    parsed = _parse_jsonish(text if isinstance(text, str) else str(text))
    looks = bool(parsed.get("looks_like_barcode", False))
    try:
        conf = float(parsed.get("confidence", 0.7))
    except (TypeError, ValueError):
        conf = 0.7
    return ScanResult(
        looks_like_barcode=looks,
        confidence=max(0.0, min(1.0, conf)),
        detector="llm",
        label=str(parsed.get("label", "")),
        beep=looks,
        detail={"raw": str(text)[:300], "latency_ms": latency_ms},
    )


def _parse_jsonish(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model reply (handles ``` fences)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except ValueError:
            pass
    return {}


# ------------------------------------------------------------ orchestration

@dataclass
class VisionConfig:
    backend: str = "local"            # local | llm
    base_url: str = ""
    api_key: str = ""
    model: str = "deepseek-v4.1-flash"  # must declare image input
    timeout: float = 30.0

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "VisionConfig":
        e = env if env is not None else os.environ
        base_url = (e.get("FISHBENCH_VISION_BASE_URL") or "").strip()
        backend = (e.get("FISHBENCH_VISION_BACKEND") or "").strip().lower()
        if not backend:
            backend = "llm" if base_url else "local"
        return cls(
            backend=backend if backend in ("local", "llm") else "local",
            base_url=base_url.rstrip("/"),
            api_key=(e.get("FISHBENCH_VISION_API_KEY") or "").strip(),
            model=(e.get("FISHBENCH_VISION_MODEL") or "deepseek-v4.1-flash").strip(),
        )


def scan_image(data: bytes, filename: str = "", config: Optional[VisionConfig] = None) -> ScanResult:
    """Single entry point: scan an upload, choose the backend, never crash."""
    cfg = config or VisionConfig.from_env()
    if cfg.backend == "llm" and cfg.base_url:
        try:
            return llm_scan(data, cfg)
        except VisionError:
            pass  # degrade to local, don't die
    try:
        return local_scan(data, filename)
    except VisionError as e:
        # Undecodable upload: the scanner grumbles, the game continues.
        return ScanResult(
            looks_like_barcode=False,
            confidence=0.0,
            detector="error",
            label="unreadable image",
            beep=False,
            detail={"error": str(e)[:200]},
        )
