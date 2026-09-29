"""FishBench-2 TANK submission runner — build it, run it, film it, seal it.

The official fishbench-2 path, same discipline as fishbench-1: YOU run
the probe against YOUR endpoint, then submit the sealed card. The probe
is one brief per stage: "an aquarium with exactly N fish". The harness
does not trust the model's HTML — it renders it headless (software
WebGL, offline, three.js served from the vendored copies), films the
window, measures what actually happened, and seals the raw measurements
into the card so the arena can re-derive every claimed number offline.

The output card (same trust tiers as fishbench-1):

    spec + spec_digest   fishbench-2 identity (brief pack + scoring hashes)
    model / display_name / org / base_url_host   the arena row
    stages[]             per-stage: the FULL source the model wrote,
                         raw measurements, categories, TankScore,
                         and the tape manifest (mp4 sha256 — the film
                         is deposited next to the arena for /watch)
    composite            TankScore (0..1000, mean over stages)
    card_sha256          seal over the canonical card

Offline baseline: with no --base-url the harness builds the reference
tank (tests/fixtures/tank_good.html) at each stage count — the keyless
denominator, same role Offline Denise plays for fishbench-1.

Keys never touch the CLI: pass the NAME of the env var holding the key.

CLI:   python -m fishbench.tank --model ID --base-url URL --api-key-env VAR
       python -m fishbench.tank --submit http://host:8383 --tapes DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import replace
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from . import __version__, tankspec
from .capture import TankCapture, strip_code_fences
from .llm import LLMConfig, HTTPBackend

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "tests", "fixtures")

SYSTEM_PROMPT = ("You are a graphics engineer who ships working three.js "
                 "scenes. You follow output rules exactly.")


def _load_fixture(name: str, n: int) -> str:
    with open(os.path.join(FIXTURE_DIR, name), "r", encoding="utf-8") as f:
        return f.read().replace("__TANK_N__", str(n))


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def run_tank_stage(backend: Any, n: int, run_number: int,
                   workroot: str) -> dict[str, Any]:
    """One stage: brief → source → headless run → measurements → stage card.

    The source is captured verbatim (fence-stripped) and sealed — it is
    the transcript of this benchmark.
    """
    brief = tankspec.build_brief(n)
    if backend is None:  # offline run — the keyless reference-tank baseline
        source = _load_fixture("tank_good.html", n)
        gen = {"lane": "offline-reference", "latency_ms": 0}
    else:
        reply = backend.complete(system=SYSTEM_PROMPT,
                                 messages=[{"role": "user",
                                            "content": brief}])
        source = strip_code_fences(reply.text)
        gen = {"lane": "http", "latency_ms": reply.latency_ms}

    workdir = tempfile.mkdtemp(prefix=f"tank-n{n}-", dir=workroot)
    cap = TankCapture(html=source, workdir=workdir).run()
    m = cap.measurements(n)
    cats = tankspec.category_scores(m, n)
    score = tankspec.stage_tankscore(m, n)

    tape_path = os.path.join(workdir, "tape.mp4")
    tape: dict[str, Any] = {"file": None, "sha256": None,
                            "frames": len(cap.frame_files),
                            "duration_s": round(
                                len(cap.frame_files) / tankspec.SAMPLE_HZ, 2),
                            "sample_hz": tankspec.SAMPLE_HZ}
    try:
        cap.write_tape(tape_path)
        tape["file"] = tape_path
        tape["sha256"] = _sha256_file(tape_path)
    except RuntimeError as e:  # no ffmpeg / no frames — card still seals
        tape["error"] = str(e)[:200]

    return {
        "run_number": run_number,
        "fish_requested": n,
        "tankscore": score,
        "categories": cats,
        "measurements": m,
        "tank_crash": bool(m.get("tank_crash")),
        "generation": gen,
        "source_bytes": len(source.encode("utf-8")),
        "source_sha256": hashlib.sha256(
            source.encode("utf-8")).hexdigest(),
        "source": source,
        "tape": tape,
    }


def seal_card(card: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in card.items() if k != "card_sha256"}
    sealed = dict(card)
    sealed["card_sha256"] = tankspec.sha256_hex(body)
    return sealed


def run_submission(
    config: LLMConfig,
    *,
    display_name: str = "",
    org: str = "",
    stages: Any = tankspec.STAGES,
    base_url: str = "",
    workroot: str = "",
) -> dict[str, Any]:
    """Run the full fishbench-2 schedule against one backend → sealed card."""
    stage_numbers = [int(s) for s in stages]
    if not stage_numbers or any(s < 1 for s in stage_numbers):
        raise ValueError(f"bad stage schedule: {stage_numbers!r}")

    if config.backend == "http":
        config = replace(config, temperature=tankspec.TEMPERATURE)
        backend: Any = HTTPBackend(config)
    else:
        backend = None  # offline: stages run against the reference fixture

    workroot = workroot or tempfile.mkdtemp(prefix="fishbench-tank-")
    os.makedirs(workroot, exist_ok=True)

    stage_cards = [
        run_tank_stage(backend, n, i + 1, workroot)
        for i, n in enumerate(stage_numbers)
    ]

    backend_name = "http" if config.backend == "http" else "offline"
    model = config.model if backend_name == "http" else "offline"
    host = urlparse(base_url).netloc if base_url else ""
    composite = tankspec.aggregate(stage_cards)
    composite["fish_requested_total"] = sum(stage_numbers)
    composite["stages_total"] = len(stage_cards)

    return seal_card({
        "spec": tankspec.SPEC_ID,
        "spec_digest": tankspec.spec_digest(),
        "harness": __version__,
        "model": model,
        "display_name": display_name or model,
        "org": org,
        "backend": backend_name,
        "base_url_host": host,
        "temperature": tankspec.TEMPERATURE,
        "run_seconds": tankspec.RUN_SECONDS,
        "sample_hz": tankspec.SAMPLE_HZ,
        "stages": stage_cards,
        "composite": composite,
    })


def deposit_tapes(card: Mapping[str, Any], tapes_dir: str) -> dict[int, str]:
    """Move each stage's film into DIR (the arena's replays tree for this
    card: data/replays/<sha8>/stage-<run>.mp4). Idempotent."""
    sha8 = str(card.get("card_sha256") or "")[:8]
    if len(sha8) != 8:
        raise ValueError("card is not sealed — cannot deposit tapes")
    out: dict[int, str] = {}
    for st in card.get("stages") or []:
        run = int(st.get("run_number") or 0)
        tape = st.get("tape") or {}
        src = tape.get("file")
        if not src or not os.path.exists(src):
            continue
        dest_dir = os.path.join(tapes_dir, sha8)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"stage-{run}.mp4")
        if not os.path.exists(dest):
            shutil.move(src, dest)
        else:
            shutil.rmtree(os.path.dirname(src), ignore_errors=True)
        out[run] = dest
    return out


# ------------------------------------------------------------- verification

def verify_tank_card(card: Mapping[str, Any]) -> dict[str, Any]:
    """Re-check a sealed TANK card offline. Never hits the network, never
    re-renders: every claimed number must fall out of the sealed raw
    measurements via the pure tankspec functions — exactly the discipline
    fishbench-1 applies to transcripts."""
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: Any, detail: str = "") -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail[:200]})
        return bool(ok)

    ok_spec = check("spec_id", card.get("spec") == tankspec.SPEC_ID,
                    f"card says {card.get('spec')!r}, verifier is "
                    f"{tankspec.SPEC_ID}")
    digest = card.get("spec_digest") or {}
    ok_brief = check("brief_pack",
                     digest.get("brief_pack_sha256")
                     == tankspec.brief_pack_digest(),
                     "brief pack hash differs from this spec")
    ok_scoring = check("scoring",
                       digest.get("scoring_sha256")
                       == tankspec.scoring_digest(),
                       "scoring constants differ from this spec")
    body = {k: v for k, v in card.items() if k != "card_sha256"}
    ok_seal = check("seal",
                    card.get("card_sha256") == tankspec.sha256_hex(body),
                    "card_sha256 mismatch — card edited after sealing")

    stages_ok = True
    rescored_rows: list[dict[str, Any]] = []
    for st in card.get("stages") or []:
        run_number = int(st.get("run_number") or 0)
        n = int(st.get("fish_requested") or 0)
        m = st.get("measurements") or {}
        missing = [k for k in tankspec.MEASUREMENT_KEYS
                   if k not in m]
        if missing:
            stages_ok = False
            check(f"stage_{run_number}_measurements", False,
                  f"missing measurement keys: {missing}")
            continue
        src = st.get("source") or ""
        ok_src = check(
            f"stage_{run_number}_source",
            bool(src) and hashlib.sha256(
                src.encode("utf-8")).hexdigest()
            == st.get("source_sha256"),
            "sealed source hash mismatch")
        cats = tankspec.category_scores(m, n)
        score = tankspec.stage_tankscore(m, n)
        ok = (
            ok_src
            and all(abs(float((st.get("categories") or {}).get(c, 0.0))
                        - cats[c]) < 0.01
                    for c in tankspec.CATEGORY_WEIGHTS)
            and abs(float(st.get("tankscore") or 0) - score) < 0.5
        )
        stages_ok = stages_ok and ok
        check(f"stage_{run_number}_rescore", ok,
              "claimed numbers vs re-derived measurements")
        rescored_rows.append({
            "fish_requested": n,
            "tankscore": score,
            "categories": cats,
            "tank_crash": bool(m.get("tank_crash")),
            "measurements": {k: m[k] for k in tankspec.MEASUREMENT_KEYS},
        })

    ok_comp = False
    rescore = None
    if rescored_rows:
        rescore = tankspec.aggregate(rescored_rows)
        comp = card.get("composite") or {}
        try:
            ok_comp = abs(rescore["tankscore"]
                          - float(comp.get("tankscore") or 0)) < 0.5
        except (TypeError, ValueError):
            ok_comp = False
        check("composite", ok_comp,
              "claimed composite vs the re-derived stages")

    verified = "verified" if (ok_spec and ok_brief and ok_scoring
                              and ok_seal and stages_ok and ok_comp
                              and rescored_rows) else "unverified"
    return {"verified": verified, "checks": checks, "rescore": rescore}


# --------------------------------------------------------------------- CLI

def _post_json(url: str, card: Mapping[str, Any],
               timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/fishbench/arena",
        data=json.dumps(card, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m fishbench.tank",
        description="Run the FishBench-2 TANK submission (six aquarium "
                    "builds, 1→200 fish) against an OpenAI-compatible "
                    "endpoint and seal the card.",
    )
    ap.add_argument("--model", default="deepseek-v4.1-flash",
                    help="model id at the endpoint (default "
                         "deepseek-v4.1-flash)")
    ap.add_argument("--base-url", default="",
                    help="OpenAI-compatible base URL; omit for the offline "
                         "keyless reference-tank baseline")
    ap.add_argument("--api-key-env", default="",
                    help="NAME of the env var holding the API key")
    ap.add_argument("--name", default="",
                    help="display name on the arena (default: model id)")
    ap.add_argument("--org", default="", help="org / lab line on the arena")
    ap.add_argument("--stages",
                    default=",".join(str(s) for s in tankspec.STAGES),
                    help=f"fish counts to build (default "
                         f"{','.join(str(s) for s in tankspec.STAGES)})")
    ap.add_argument("--timeout", type=float, default=180.0,
                    help="per-request LLM timeout seconds (default 180 — "
                         "one full HTML file per request)")
    ap.add_argument("--header", action="append", default=[],
                    metavar="NAME=VALUE",
                    help="extra request header for the LLM endpoint, "
                         "repeatable")
    ap.add_argument("--out", default="",
                    help="write the sealed card JSON here")
    ap.add_argument("--tapes", default="",
                    help="deposit the stage films into DIR (for a local "
                         "arena: its data/replays tree — cards carry the "
                         "mp4 hashes so the arena can check the deposit)")
    ap.add_argument("--submit", default="",
                    help="arena base URL — POSTs the sealed card after "
                         "the run")
    args = ap.parse_args(argv)

    stages = [int(x) for x in args.stages.split(",") if x.strip()]
    if not stages or any(s < 1 for s in stages):
        print(f"fishbench-tank: bad --stages {args.stages!r}",
              file=sys.stderr)
        return 2

    api_key = ""
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env, "")
        if not api_key:
            print(f"fishbench-tank: env var {args.api_key_env} is not set",
                  file=sys.stderr)
            return 2

    extra_headers: dict[str, str] = {}
    for raw in args.header:
        name, sep, value = raw.partition("=")
        if not sep or not name.strip():
            print(f"fishbench-tank: bad --header {raw!r}",
                  file=sys.stderr)
            return 2
        extra_headers[name.strip()] = value.strip()

    if args.base_url:
        config = LLMConfig(backend="http",
                           base_url=args.base_url.rstrip("/"),
                           api_key=api_key, model=args.model,
                           timeout=args.timeout,
                           extra_headers=extra_headers)
    else:
        config = LLMConfig(backend="offline")

    workroot = tempfile.mkdtemp(prefix="fishbench-tank-")
    try:
        card = run_submission(config, display_name=args.name, org=args.org,
                              stages=stages, base_url=args.base_url,
                              workroot=workroot)
    except Exception as e:
        print(f"fishbench-tank: run failed: {e}", file=sys.stderr)
        return 2

    comp = card["composite"]
    who = card["display_name"] + (f" ({card['org']})" if card["org"] else "")
    print(f"fishbench-2 TANK submission — {who}")
    print(f"  backend {card['backend']}"
          + (f" via {card['base_url_host']}" if card["base_url_host"] else "")
          + f"  ·  temperature {card['temperature']}"
          + f"  ·  filmed {tankspec.RUN_SECONDS:g}s @ "
            f"{tankspec.SAMPLE_HZ:g}hz software-WebGL")
    for st in card["stages"]:
        m = st["measurements"]
        mark = "✗" if st["tank_crash"] else "·"
        est = m.get("fish_estimate")
        est_s = f"{est:g}" if isinstance(est, (int, float)) else "?"
        print(f"  {mark} n={st['fish_requested']:<3} "
              f"tankscore {st['tankscore']:>7.2f}   "
              f"est {est_s:>4}   fps {m.get('fps', 0):>5.1f}   "
              f"cov {100 * (m.get('motion_coverage') or 0):>5.1f}%   "
              f"err {m.get('console_errors', 0)}/{m.get('page_errors', 0)}")
    print(f"  TANKSCORE {comp['tankscore']} / 1000   "
          f"crashes {comp['tank_crashes']}/{comp['stages_total']}   "
          f"errors {comp['console_errors_total']}c/"
          f"{comp['page_errors_total']}p")
    print(f"  card sha256 {card['card_sha256'][:16]}…")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".",
                    exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(card, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"  card → {args.out}")

    if args.tapes:
        try:
            deposited = deposit_tapes(card, args.tapes)
        except Exception as e:
            print(f"fishbench-tank: tape deposit failed: {e}",
                  file=sys.stderr)
            return 2
        for run in sorted(deposited):
            print(f"  tape → {deposited[run]}")

    if args.submit:
        try:
            resp = _post_json(args.submit, card,
                              timeout=max(30.0, args.timeout))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                ValueError) as e:
            detail = getattr(e, "read", lambda: b"")()
            print(f"fishbench-tank: submit failed: {e} {detail[:200]}".rstrip(),
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
    sys.exit(main(sys.argv[1:]))
