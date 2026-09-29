"""FishBench-2 TANK — the frozen three.js generation specification.

FishBench-1 asks "can the model keep a straight face while the world goes
absurd?" FishBench-2 asks the question that actually separates models in
2026: "can it WRITE THE WORLD?" Three.js scene generation is the modern
torture track — and this is the FishBench take on it:

    The model receives one brief: build a live aquarium with exactly N
    fish, one self-contained HTML file, three.js allowed from a CDN.
    The harness renders what it built in a headless browser (software
    WebGL — same GPU for everyone, every run), films the tank, and
    measures what actually happened:

        did it boot without errors?
        are there N fish on screen, moving?
        does the tank keep swimming for the full window?
        what framerate survives at 200 fish?

    The stage ladder is the same escalation as fishbench-1 — {1, 5, 20,
    50, 100, 200} — but the number is FISH COUNT. Naive per-mesh scenes
    that charm at N=1 die at N=200. That curve is the benchmark.

Everything a card claims is re-derivable from the raw measurements block
the harness seals into it (scoring below is a pure function of those
numbers), and every stage ships a TAPE — an mp4 of the actual rendered
tank — because a benchmark nobody can watch is a spreadsheet.

Versioning policy (inherited from fishbench-1): fishbench-2 is FROZEN on
release. Any change to the briefs, stages, weights, or any constant in
this file invalidates every TANK card ever sealed — it must ship as
fishbench-3. `tests/test_tankspec.py` pins the digests.

    TankScore (0..1000, the headline number per stage — same scale and
    shape as fishbench-1's FishScore so the arena stays readable)
        categories, each 0..100:
            clean_boot           max(0, 100 − console_errors×10
                                           − page_errors×35)
                                 (0 if the canvas never drew)
            fish_on_screen       N ≤ COUNT_SPLIT: 100 × min(est, N)/N
                                 N > COUNT_SPLIT: coverage against
                                 COV_TARGET(N) = min(COV_CAP,
                                     COV_BASE + N × COV_PER_FISH)
            sustained_swimming   100 × windows_alive / windows_total
            fps_at_scale         min(100, fps × FPS_FACTOR)
        weighted: 25% / 30% / 25% / 20%  → × 10
        adjusted: −5 per console error, −25 per page error, floor 0
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

SPEC_ID = "fishbench-2"
SPEC_DATE = "2026-09-29"

# The escalation ladder — fish count requested in the brief.
STAGES: tuple[int, ...] = (1, 5, 20, 50, 100, 200)

# Fixed adaptation. Code generation is pinned at low temperature for all
# models (fairness = identical decoding, not identical vibes).
TEMPERATURE = 0.2

# ---------------------------------------------------------------- capture
RUN_SECONDS = 20.0        # every tank is filmed for the same window
SAMPLE_HZ = 4.0           # frame sampling rate (screenshots per second)
VIEWPORT = (960, 540)     # every tank renders at the same size
ANALYSIS_SIZE = (320, 180)  # frames are analyzed downscaled
DIFF_THRESH = 24          # per-pixel gray delta that counts as "changed"
MIN_COMPONENT_AREA = 6    # px (at ANALYSIS_SIZE) for a moving blob = fish
COUNT_SPLIT = 20          # below: count blobs; at/above: measure coverage
MOTION_FLOOR = 0.0005     # fraction of pixels changed = "window alive".
                           # Measured on the reference tank (2026-09-29
                           # smoke, 320x180 gray, DIFF_THRESH 24, jpeg q80):
                           # static-scene sampling noise ~0.0002, one real
                           # swimming fish ~0.0012 — the floor sits between.
WINDOWS = 8               # sustained_swimming sample windows per run

# ----------------------------------------------------------------- scoring
CONSOLE_ERROR_COST = 10.0     # clean_boot points lost per console error
PAGE_ERROR_COST = 35.0        # clean_boot points lost per page error
COV_BASE = 0.0                # zero fish → zero required motion
COV_PER_FISH = 0.0007         # coverage target growth per requested fish.
                              # Calibrated on the reference tank (2026-09-29,
                              # full offline ladder, 320x180 gray, DIFF_THRESH
                              # 24): cov(50)=0.041, cov(100)=0.065,
                              # cov(200)=0.168 → cov/N ≈ 0.0007. The reference
                              # lands 92-100 on fish_on_screen; a tank with
                              # half the motion scores half.
COV_CAP = 0.45                # coverage target ceiling (a full tank is a
                              # full tank — past this, more fish adds no
                              # required motion area)
FPS_FACTOR = 8.0              # fps_at_scale: fps × 8, capped at 100
CATEGORY_CAP = 100.0

CATEGORY_WEIGHTS: dict[str, float] = {
    "clean_boot": 0.25,
    "fish_on_screen": 0.30,
    "sustained_swimming": 0.25,
    "fps_at_scale": 0.20,
}

CONSOLE_ERROR_PENALTY = 5    # − per console error: noise in the log
PAGE_ERROR_PENALTY = 25      # − per page error: the scene threw

# The measurement keys a stage card must carry for scoring to be honest.
MEASUREMENT_KEYS: tuple[str, ...] = (
    "console_errors", "page_errors", "canvas_drew",
    "fish_estimate", "motion_coverage",
    "windows_alive", "windows_total", "fps",
)

# ------------------------------------------------------------------ briefs

BRIEF_RULES = """RULES (the harness enforces all of them):
- ONE self-contained HTML file. Everything inline. No external assets,
  fonts, textures, models, or libraries EXCEPT three.js from a CDN
  (any version, any CDN). The harness renders offline: any other network
  request fails.
- The tank must contain EXACTLY {n} fish, all visible and swimming from
  the first seconds (no "click to start", no loading screens).
- Fish must be recognizable fish (body + tail fin at minimum), not dots,
  sprites, or particles.
- The scene must animate continuously via requestAnimationFrame for the
  entire judging window. A frozen frame scores zero.
- No performance overlays, stats panels, text watermarks, or postMessage
  tricks. Just the tank.
- Output ONLY the HTML file contents. No markdown fences, no commentary."""


def build_brief(n: int) -> str:
    """The exact prompt every model receives for stage N."""
    return (
        "You are building one frame of an art installation: a public "
        "aquarium security monitor. Write a complete, self-contained HTML "
        f"file using three.js that renders a live aquarium tank with "
        f"EXACTLY {n} fish in it.\n\n"
        "Aesthetics: an aquarium worth staring at — water you can feel, "
        "light through the surface, fish with personality in their motion. "
        "The camera looks at the tank from the front; the viewer cannot "
        "interact.\n\n"
        + BRIEF_RULES.format(n=n)
    )


BRIEF_PACK: tuple[str, ...] = tuple(build_brief(n) for n in STAGES)


# ------------------------------------------------------------------ hashing

def canonical_json(obj: Any) -> bytes:
    """Identical discipline to fishbench-1: sorted keys, compact, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def brief_pack_digest() -> str:
    """sha256 over the canonical brief pack (order included)."""
    return sha256_hex(list(BRIEF_PACK))


def scoring_digest() -> str:
    """sha256 over every constant that can move a number."""
    return sha256_hex({
        "stages": STAGES,
        "temperature": TEMPERATURE,
        "run_seconds": RUN_SECONDS,
        "sample_hz": SAMPLE_HZ,
        "viewport": VIEWPORT,
        "analysis_size": ANALYSIS_SIZE,
        "diff_thresh": DIFF_THRESH,
        "min_component_area": MIN_COMPONENT_AREA,
        "count_split": COUNT_SPLIT,
        "motion_floor": MOTION_FLOOR,
        "windows": WINDOWS,
        "console_error_cost": CONSOLE_ERROR_COST,
        "page_error_cost": PAGE_ERROR_COST,
        "cov_base": COV_BASE,
        "cov_per_fish": COV_PER_FISH,
        "cov_cap": COV_CAP,
        "fps_factor": FPS_FACTOR,
        "category_cap": CATEGORY_CAP,
        "category_weights": CATEGORY_WEIGHTS,
        "console_error_penalty": CONSOLE_ERROR_PENALTY,
        "page_error_penalty": PAGE_ERROR_PENALTY,
    })


def spec_digest() -> dict[str, str]:
    """The identity block every TANK card carries."""
    return {
        "spec": SPEC_ID,
        "date": SPEC_DATE,
        "brief_pack_sha256": brief_pack_digest(),
        "scoring_sha256": scoring_digest(),
    }


# ----------------------------------------------------------------- scoring

def cov_target(n: int) -> float:
    """Expected moving-pixel fraction for a healthy N-fish tank."""
    return min(COV_CAP, COV_BASE + n * COV_PER_FISH)


def clean_boot(m: Mapping[str, Any]) -> float:
    if not m.get("canvas_drew"):
        return 0.0
    score = (CATEGORY_CAP
             - CONSOLE_ERROR_COST * float(m.get("console_errors") or 0)
             - PAGE_ERROR_COST * float(m.get("page_errors") or 0))
    return max(0.0, min(CATEGORY_CAP, score))


def fish_on_screen(m: Mapping[str, Any], n: int) -> float:
    if not m.get("canvas_drew"):
        return 0.0
    est = float(m.get("fish_estimate") or 0)
    if n <= COUNT_SPLIT:
        return max(0.0, min(CATEGORY_CAP, CATEGORY_CAP * min(est, n) / n))
    cov = float(m.get("motion_coverage") or 0)
    target = cov_target(n)
    return max(0.0, min(CATEGORY_CAP, CATEGORY_CAP * min(cov, target) / target))


def sustained_swimming(m: Mapping[str, Any]) -> float:
    total = int(m.get("windows_total") or 0)
    alive = int(m.get("windows_alive") or 0)
    if total <= 0:
        return 0.0
    return max(0.0, min(CATEGORY_CAP, CATEGORY_CAP * alive / total))


def fps_at_scale(m: Mapping[str, Any]) -> float:
    if not m.get("canvas_drew"):
        return 0.0
    fps = max(0.0, float(m.get("fps") or 0))
    return min(CATEGORY_CAP, fps * FPS_FACTOR)


def category_scores(m: Mapping[str, Any], n: int) -> dict[str, float]:
    return {
        "clean_boot": round(clean_boot(m), 2),
        "fish_on_screen": round(fish_on_screen(m, n), 2),
        "sustained_swimming": round(sustained_swimming(m), 2),
        "fps_at_scale": round(fps_at_scale(m), 2),
    }


def weighted_composite(category: Mapping[str, float]) -> float:
    """0..100 before ×10. Missing categories count as 0."""
    return sum(w * float(category.get(cat, 0.0))
               for cat, w in CATEGORY_WEIGHTS.items())


def stage_tankscore(m: Mapping[str, Any], n: int) -> float:
    """One stage's TankScore, 0..1000, with error adjustments (floor 0)."""
    base = weighted_composite(category_scores(m, n)) * 10.0
    adj = (CONSOLE_ERROR_PENALTY * int(m.get("console_errors") or 0)
           + PAGE_ERROR_PENALTY * int(m.get("page_errors") or 0))
    return round(max(0.0, base - adj), 2)


def aggregate(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Composite over stage rows: TankScore = mean of stage scores."""
    rows = list(rows)
    n = len(rows) or 1
    cats = {
        cat: round(sum(float((r.get("categories") or {}).get(cat, 0.0))
                       for r in rows) / n, 2)
        for cat in CATEGORY_WEIGHTS
    }
    return {
        "tankscore": round(sum(float(r["tankscore"]) for r in rows) / n, 2),
        "stages": {str(r["fish_requested"]): r["tankscore"] for r in rows},
        "categories": cats,
        "tank_crashes": int(sum(1 for r in rows if r.get("tank_crash"))),
        "console_errors_total":
            int(sum(int((r.get("measurements") or {})
                        .get("console_errors") or 0) for r in rows)),
        "page_errors_total":
            int(sum(int((r.get("measurements") or {})
                        .get("page_errors") or 0) for r in rows)),
        "fish_estimates":
            {str(r["fish_requested"]):
             (r.get("measurements") or {}).get("fish_estimate")
             for r in rows},
        "fps_by_stage":
            {str(r["fish_requested"]): (r.get("measurements") or {}).get("fps")
             for r in rows},
    }
