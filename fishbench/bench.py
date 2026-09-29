"""FishBench-1 submission runner — bring your own endpoint, get a sealed card.

This is the official submission path (the LiveBench / simple-evals model:
YOU run the probe against YOUR endpoint, then submit the sealed result —
the arena verifies it from the transcript and flags what it could not
check). The gauntlet is the probe suite; a *submission* is the full
fishbench-1 schedule: the 10-attack gauntlet at every relationship stage
(6 × 10 = 60 probes), same virtual clock, same temperature, isolated
memory — the only variable is the model behind Denise.

The output is one machine-readable JSON **card**:

    spec + spec_digest   which benchmark produced this (frozen, hashed)
    model / display_name / org / base_url_host   the model card row
    stages[]             per-stage evidence: scores, every attack, Denise's
                         actual replies, and the full timestamped transcript
    composite            FishScore (0..1000, mean over stages) + aggregates
    card_sha256          seal over the canonical card (spec.canonical_json)

`seal_card` hashes; `verify_card` re-checks a card without ever hitting the
network: spec identity, pack/scoring digests, the seal, and — the part that
makes claims weak — a from-scratch re-score of every stage transcript via
`scoring.score_transcript`. A card whose numbers don't fall out of its own
transcript is marked `unverified`, exactly like a hand-typed leaderboard
submission; the arena ranks on claimed numbers either way but shows the
badge. (Transcripts are still client-authored until hosted attestation
exists — see docs/SPEC.md, "What verification proves".)

Keys never touch the CLI: pass the NAME of the env var holding the key.

CLI:   python -m fishbench.bench --model ID --base-url URL --api-key-env KEY_VAR
       python -m fishbench.bench --submit http://host:8383     # then deliver
       (omit --base-url for the keyless offline baseline)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

from . import __version__, spec
from .gauntlet import ATTACKS, Attack, GauntletResult, run_gauntlet
from .llm import LLMConfig
from .scoring import RunScore, format_scan_row, score_transcript


# --------------------------------------------------------------- card parts

def _stage_transcript(result: GauntletResult) -> list[dict[str, Any]]:
    """The stage as re-scorable conversation rows (score_transcript format).

    Must mirror what the runner actually scored: the greeting turn at t=0,
    then you-row → denise-row per attack, with scan markers in the canonical
    `[scanned X]` form so scanned_item attaches server-side too.
    """
    rows: list[dict[str, Any]] = [
        {"role": "denise", "text": result.greeting, "t": 0.0},
    ]
    for a in result.attacks:
        you = format_scan_row(a.payload) if a.kind == "scan" else a.payload
        rows.append({"role": "you", "text": you, "t": a.t})
        rows.append({"role": "denise", "text": a.line, "t": a.t})
    return rows


def _stage_card(result: GauntletResult) -> dict[str, Any]:
    s = result.score
    return {
        "run_number": result.run_number,
        "stage": result.stage,
        "fishscore": spec.stage_fishscore(s.category_scores),
        "total_score": s.total_score,
        "categories": dict(s.category_scores),
        "straight_face_seconds": round(s.straight_face_seconds, 2),
        "character_breaks": s.character_breaks,
        "first_sir_look_s": None if s.first_sir_look_s is None
                            else round(s.first_sir_look_s, 2),
        "best_threat_score": s.best_threat_score,
        "best_threat": s.best_threat,
        "attacks_survived": sum(1 for a in result.attacks if not a.broke),
        "attacks_total": len(result.attacks),
        "greeting": result.greeting,
        "attacks": [a.to_dict() for a in result.attacks],
        "transcript": _stage_transcript(result),
    }


def _aggregate(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Composite over stage rows: FishScore = mean of stage composites."""
    n = len(rows) or 1
    cats = {
        cat: round(sum(r["categories"].get(cat, 0.0) for r in rows) / n, 2)
        for cat in spec.CATEGORY_WEIGHTS
    }
    sirs = [r["first_sir_look_s"] for r in rows if r["first_sir_look_s"] is not None]
    return {
        "fishscore": round(sum(r["fishscore"] for r in rows) / n, 2),
        "stages": {str(r["run_number"]): r["fishscore"] for r in rows},
        "categories": cats,
        "character_breaks": int(sum(r["character_breaks"] for r in rows)),
        "straight_face_seconds_total":
            round(sum(r["straight_face_seconds"] for r in rows), 2),
        "fastest_sir_look_s": min(sirs) if sirs else None,
        "best_threat_score": int(max((r["best_threat_score"] for r in rows),
                                     default=0)),
    }


def seal_card(card: dict[str, Any]) -> dict[str, Any]:
    """Seal the card: sha256 over the canonical form of everything else.

    The seal deliberately excludes nothing else — no timestamp — so two
    runs of the same model against the same deterministic backend produce
    byte-identical, same-hash cards. Wall-clock belongs to the arena's
    `received` stamp, not to the evidence.
    """
    body = {k: v for k, v in card.items() if k != "card_sha256"}
    sealed = dict(card)
    sealed["card_sha256"] = spec.sha256_hex(body)
    return sealed


def run_submission(
    config: LLMConfig,
    *,
    display_name: str = "",
    org: str = "",
    stages: Iterable[int] = spec.STAGES,
    pace_s: float = spec.PACE_S,
    attacks: Iterable[Attack] = ATTACKS,
    base_url: str = "",
) -> dict[str, Any]:
    """Run the full fishbench-1 schedule against one backend → sealed card."""
    attacks = tuple(attacks)
    stage_numbers = [int(s) for s in stages]
    if not stage_numbers or any(s < 1 for s in stage_numbers):
        raise ValueError(f"bad stage schedule: {stage_numbers!r}")

    # Fixed adaptation: every submission runs at the spec temperature,
    # regardless of what the caller's config said.
    if config.backend == "http":
        config = replace(config, temperature=spec.TEMPERATURE)

    stage_cards = [
        _stage_card(run_gauntlet(config, run_number=run_number,
                                 pace_s=pace_s, attacks=attacks))
        for run_number in stage_numbers
    ]

    backend_name = "http" if config.backend == "http" else "offline"
    model = config.model if backend_name == "http" else "offline"
    host = urlparse(base_url).netloc if base_url else ""
    composite = _aggregate(stage_cards)
    composite["attacks_survived"] = sum(s["attacks_survived"] for s in stage_cards)
    composite["attacks_total"] = sum(s["attacks_total"] for s in stage_cards)

    return seal_card({
        "spec": spec.SPEC_ID,
        "spec_digest": spec.spec_digest(spec.attack_pack_digest(attacks)),
        "harness": __version__,
        "model": model,
        "display_name": display_name or model,
        "org": org,
        "backend": backend_name,
        "base_url_host": host,
        "temperature": spec.TEMPERATURE,
        "pace_s": pace_s,
        "stages": stage_cards,
        "composite": composite,
    })


# ------------------------------------------------------------- verification

def _stage_row_from_score(run_number: int, rs: RunScore) -> dict[str, Any]:
    return {
        "run_number": run_number,
        "fishscore": spec.stage_fishscore(rs.category_scores),
        "categories": dict(rs.category_scores),
        "character_breaks": rs.character_breaks,
        "straight_face_seconds": round(rs.straight_face_seconds, 2),
        "first_sir_look_s": None if rs.first_sir_look_s is None
                            else round(rs.first_sir_look_s, 2),
        "best_threat_score": rs.best_threat_score,
    }


def verify_card(card: Mapping[str, Any],
                attacks: Iterable[Attack] = ATTACKS) -> dict[str, Any]:
    """Re-check a sealed card offline. Never hits the network.

    Returns {"verified": "verified"|"unverified", "checks": [...],
    "rescore": composite-or-None}. `verified` means every check passed —
    spec identity, digests, seal, and each stage's claimed numbers falling
    out of its own transcript. It still does NOT mean this verifier watched
    the run happen (see docs/SPEC.md); it means the card is internally true.
    """
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: Any, detail: str = "") -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail[:200]})
        return bool(ok)

    ok_spec = check("spec_id", card.get("spec") == spec.SPEC_ID,
                    f"card says {card.get('spec')!r}, verifier is {spec.SPEC_ID}")
    digest = card.get("spec_digest") or {}
    ok_pack = check("attack_pack",
                    digest.get("attack_pack_sha256")
                    == spec.attack_pack_digest(attacks),
                    "attack pack hash differs from this spec")
    ok_scoring = check("scoring",
                       digest.get("scoring_sha256") == spec.scoring_digest(),
                       "scoring constants differ from this spec")
    body = {k: v for k, v in card.items() if k != "card_sha256"}
    ok_seal = check("seal",
                    card.get("card_sha256") == spec.sha256_hex(body),
                    "card_sha256 mismatch — card edited after sealing")

    rescored_rows: list[dict[str, Any]] = []
    stages_ok = True
    for st in card.get("stages") or []:
        run_number = int(st.get("run_number") or 0)
        rs = score_transcript(st.get("transcript") or [], run_number=run_number)
        if rs is None:
            stages_ok = False
            check(f"stage_{run_number}_rescore", False,
                  "transcript missing or malformed — nothing to re-score")
            continue
        claimed_cats = st.get("categories") or {}
        ok = (
            rs.character_breaks == st.get("character_breaks")
            and abs(rs.total_score - float(st.get("total_score") or 0)) < 0.5
            and all(
                abs(rs.category_scores.get(c, 0.0)
                    - float(claimed_cats.get(c) or 0.0)) < 0.01
                for c in spec.CATEGORY_WEIGHTS
            )
        )
        stages_ok = stages_ok and ok
        check(f"stage_{run_number}_rescore", ok,
              "claimed numbers vs re-scored transcript")
        rescored_rows.append(_stage_row_from_score(run_number, rs))

    # The composite is a claim like any other: it must fall out of the
    # re-scored stages too, or a re-sealed card could invent its headline.
    ok_comp = False
    rescore = None
    if rescored_rows:
        rescore = _aggregate(rescored_rows)
        comp = card.get("composite") or {}
        try:
            ok_comp = (
                abs(rescore["fishscore"] - float(comp.get("fishscore") or 0)) < 0.5
                and rescore["character_breaks"]
                == int(comp.get("character_breaks") or 0)
                and rescore["best_threat_score"]
                == int(comp.get("best_threat_score") or 0)
            )
        except (TypeError, ValueError):
            ok_comp = False
        check("composite", ok_comp,
              "claimed composite vs the re-scored stages")

    verified = "verified" if (ok_spec and ok_pack and ok_scoring
                              and ok_seal and stages_ok and ok_comp
                              and rescored_rows) else "unverified"
    return {"verified": verified, "checks": checks, "rescore": rescore}


# --------------------------------------------------------------------- CLI

def _post_json(url: str, card: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/fishbench/arena",
        data=json.dumps(card, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m fishbench.bench",
        description="Run the FishBench-1 submission (6 stages × 10 attacks) "
                    "against an OpenAI-compatible endpoint and seal the card.",
    )
    ap.add_argument("--model", default="deepseek-v4.1-flash",
                    help="model id at the endpoint (default deepseek-v4.1-flash)")
    ap.add_argument("--base-url", default="",
                    help="OpenAI-compatible base URL; omit for the offline "
                         "keyless baseline run")
    ap.add_argument("--api-key-env", default="",
                    help="NAME of the env var holding the API key — the key "
                         "itself never goes on a command line")
    ap.add_argument("--name", default="",
                    help="display name on the arena (default: model id)")
    ap.add_argument("--org", default="", help="org / lab line on the arena")
    ap.add_argument("--stages",
                    default=",".join(str(s) for s in spec.STAGES),
                    help=f"relationship stages to probe (default "
                         f"{','.join(str(s) for s in spec.STAGES)} — the full "
                         f"fishbench-1 schedule)")
    ap.add_argument("--pace", type=float, default=spec.PACE_S,
                    help="virtual seconds between attacks (default 5)")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="per-request LLM timeout seconds (default 30)")
    ap.add_argument("--header", action="append", default=[], metavar="NAME=VALUE",
                    help="extra request header for the LLM endpoint, repeatable "
                         "(e.g. --header x-opencode-session=fishbench-1) — "
                         "for providers that require routing headers")
    ap.add_argument("--out", default="",
                    help="write the sealed card JSON here")
    ap.add_argument("--replay", default="",
                    help="render security-cam tape mp4s of every probed "
                         "stage into DIR (needs Pillow + ffmpeg)")
    ap.add_argument("--submit", default="",
                    help="arena base URL (e.g. http://host:8383) — POSTs the "
                         "sealed card to /api/fishbench/arena after the run")
    args = ap.parse_args(argv)

    stages = [int(x) for x in args.stages.split(",") if x.strip()]
    if not stages or any(s < 1 for s in stages):
        print(f"fishbench: bad --stages {args.stages!r}", file=sys.stderr)
        return 2

    api_key = ""
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env, "")
        if not api_key:
            print(f"fishbench: env var {args.api_key_env} is not set — "
                  f"export it first (keys never go on the command line)",
                  file=sys.stderr)
            return 2

    extra_headers: dict[str, str] = {}
    for raw in args.header:
        name, sep, value = raw.partition("=")
        if not sep or not name.strip():
            print(f"fishbench: bad --header {raw!r} (want NAME=VALUE)",
                  file=sys.stderr)
            return 2
        extra_headers[name.strip()] = value.strip()

    if args.base_url:
        config = LLMConfig(backend="http", base_url=args.base_url.rstrip("/"),
                           api_key=api_key, model=args.model,
                           timeout=args.timeout,
                           extra_headers=extra_headers)
    else:
        config = LLMConfig(backend="offline")

    try:
        card = run_submission(config, display_name=args.name, org=args.org,
                              stages=stages, pace_s=max(0.1, args.pace),
                              base_url=args.base_url)
    except Exception as e:  # endpoint unreachable / protocol nonsense
        print(f"fishbench: run failed: {e}", file=sys.stderr)
        return 2

    comp = card["composite"]
    who = card["display_name"] + (f" ({card['org']})" if card["org"] else "")
    print(f"fishbench-1 submission — {who}")
    print(f"  backend {card['backend']}"
          + (f" via {card['base_url_host']}" if card["base_url_host"] else "")
          + f"  ·  temperature {card['temperature']}  ·  pace {card['pace_s']}s"
          f"  ·  {comp['attacks_total']} probes")
    for st in card["stages"]:
        mark = "✗" if st["character_breaks"] else "·"
        print(f"  {mark} run #{st['run_number']:<3} {st['stage']:<22} "
              f"fishscore {st['fishscore']:>7.2f}   breaks {st['character_breaks']}")
        for a in st["attacks"]:
            if a["broke"]:
                print(f"      BREAK at {a['id']}: {a['line'][:90]!r}")
    sir = comp["fastest_sir_look_s"]
    print(f"  FISHSCORE {comp['fishscore']} / 1000   "
          f"breaks {comp['character_breaks']}/{comp['attacks_total']}   "
          f"face {comp['straight_face_seconds_total']}s   "
          f"sir_look {f'{sir:g}s' if sir is not None else 'never'}   "
          f"threat {comp['best_threat_score']}")
    print(f"  card sha256 {card['card_sha256'][:16]}…")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(card, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"  card → {args.out}")

    if args.replay:
        try:
            from .replay import render_card_replays
            os.makedirs(args.replay, exist_ok=True)
            tapes = render_card_replays(card, args.replay)
        except Exception as e:  # no ffmpeg / no Pillow / encode failure
            print(f"fishbench: --replay failed: {e}", file=sys.stderr)
            return 2
        for run in sorted(tapes):
            print(f"  tape → {tapes[run]}")

    if args.submit:
        try:
            resp = _post_json(args.submit, card, timeout=max(30.0, args.timeout))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                ValueError) as e:
            detail = getattr(e, "read", lambda: b"")()
            print(f"fishbench: submit failed: {e} {detail[:200]}".rstrip(),
                  file=sys.stderr)
            return 3
        if resp.get("ok"):
            print(f"  submitted → position #{resp.get('position')} "
                  f"({resp.get('verified')})")
            if resp.get("watch"):
                print(f"  tapes → {args.submit.rstrip('/')}{resp['watch']}")
        else:
            print(f"  arena refused the card: {resp.get('error')}",
                  file=sys.stderr)
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
