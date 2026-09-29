/* zcode-workflow
description: "FishBench-2 TANK gauntlet: runs the full fish ladder against a
  model lane (generate → headless-film → measure → seal → submit), repairs
  invocation failures (≤2 medic rounds, frozen spec untouched), verifies the
  sealed card re-derives and lands on the TankScore board, then one sighted
  reviewer per stage tape watches the mp4. Publishes the verdict report,
  per-stage scores chart, sealed card, and a sample tape."
whenToUse: Benchmarking a model on FishBench-2 TANK (three.js aquarium
  generation at scale) from this workspace's fishbench repo; runs from the
  workspace root or the fishbench dir itself, auto-starts the local arena on
  :8383 if down.
args:
  baseUrl:
    type: string
    description: OpenAI-compatible base URL of the lane provider
    default: https://openrouter.ai/api/v1
  apiKeyEnv:
    type: string
    description: Env var holding the lane key (checked before the config fallback)
    default: FB_LANE_KEY
  keyConfigPath:
    type: string
    description: Fallback config file to read the lane key from
    default: ~/.zcode/v2/config.json
  keyConfigQuery:
    type: string
    description: Dot path to the key inside the fallback config JSON
    default: provider.custom:openrouter.options.apiKey
  model:
    type: string
    description: Lane model id to benchmark, e.g. xiaomi/mimo-v2.6-flash
    required: true
  name:
    type: string
    description: Display name on the TankScore board (defaults to the model id)
  org:
    type: string
    description: Org stamped on the sealed card (optional)
  stages:
    type: string
    description: Comma-separated fish ladder for the gauntlet
    default: 1,5,20,50,100,200
  submitUrl:
    type: string
    description: Local FishBench arena base URL (auto-started if down)
    default: http://127.0.0.1:8383
*/
// fishbench-tank — the FishBench-2 "TANK" gauntlet as a house workflow.
//
// What it does, end to end, with no human steps:
//   1. preflight — local arena server (auto-starts it if down), lane key
//      present (env var, else read from the ZCode provider config; the key
//      value is never printed, never in argv, never in the journal)
//   2. gauntlet — python3 -m fishbench.tank against the model lane:
//      generate → headless-film → measure → seal → submit → deposit tapes
//   3. repair — ≤2 medic rounds on invocation/environment failures only;
//      the frozen spec (brief pack + scoring digests) is never touched
//   4. verify — the sealed card must re-derive on the server and land on
//      the TankScore board (deterministic gate, exit codes are the truth)
//   5. sighted gate — one reviewer agent per stage tape, eyes on the mp4,
//      judging what is actually on screen vs what the card claims
//   6. publish — markdown verdict (primary), scores chart, sealed card
//      receipt, sample tape
//
// args: model (required) · name · org · stages · baseUrl · submitUrl ·
//       apiKeyEnv · keyConfigPath · keyConfigQuery

interface StageRow {
  /** Gauntlet run number for the stage (1-based). */
  run: number;
  /** Fish the stage requested — the ladder value. */
  fish: number;
  /** Sealed stage TankScore 0..1000. */
  tankscore: number;
  cats: {
    clean_boot: number;
    fish_on_screen: number;
    sustained_swimming: number;
    fps_at_scale: number;
  };
  /** Absolute tape path for the Read gate (empty if missing on disk). */
  tapeAbs: string;
  /** Workspace-relative tape path for artifacts (empty if missing). */
  tapeRel: string;
}

interface Summary {
  card: string;
  name: string;
  tankscore: number;
  verified: string;
  position: number;
  stageRows: StageRow[];
}

interface MedicFix {
  /** One sentence: what was actually wrong. */
  diagnosis: string;
  /** False when the lane itself is broken (auth, model gone, quota). */
  rerunWorthwhile: boolean;
}

interface TapeVerdict {
  /** Stage fish count this tape was filmed for. */
  fish: number;
  /** Honest order-of-magnitude count of fish actually visible on tape. */
  fishSeen: number;
  /** True when fish visibly move — not a frozen frame. */
  swimming: boolean;
  /** True when the tape agrees with the card's claims for this stage. */
  agreesWithCard: boolean;
  /** One sentence for the report. */
  note: string;
}

interface Finding {
  where: string;
  what: string;
  evidence: string;
  status: "verified" | "unverified";
  severity: "info" | "low" | "high";
}

// offline fixture ceiling — the reference tank, card sha8 741796bd
const REFERENCE_TS = 988.65;

const model = String(args.model ?? "");
if (!model) {
  return {
    conclusion:
      "refused: args.model is required (lane model id, e.g. xiaomi/mimo-v2.6-flash)",
    findings: [],
    verified: [],
    notCovered: ["no lane model given — nothing ran"],
  };
}

const name = String(args.name ?? model);
const org = String(args.org ?? "");
const stagesArg = String(args.stages ?? "1,5,20,50,100,200");
const baseUrl = String(args.baseUrl ?? "https://openrouter.ai/api/v1");
const submitUrl = String(args.submitUrl ?? "http://127.0.0.1:8383");
const apiKeyEnv = String(args.apiKeyEnv ?? "FB_LANE_KEY");
const keyConfigPath = String(args.keyConfigPath ?? "~/.zcode/v2/config.json");
const keyConfigQuery = String(
  args.keyConfigQuery ?? "provider.custom:openrouter.options.apiKey");

const slug = model.replace(/[^A-Za-z0-9._-]+/g, "-");
const port = /:([0-9]+)\/?$/.exec(submitUrl)?.[1] ?? "8383";
const tail = (s: string, n: number) => (s.length <= n ? s : s.slice(-n));
// escape a TS value for embedding inside a python double-quoted string
const q = (v: string) => v.replace(/\\/g, "\\\\").replace(/"/g, '\\"');

artifact.chart("scores", {
  title: `${name} — TankScore per stage (reference ${REFERENCE_TS})`,
  x: { field: "fish", label: "fish requested" },
  y: { field: "tankscore", label: "TankScore" },
  baseline: { field: "reference", label: "reference tank" },
});

const findings: Finding[] = [];
const verified: string[] = [];
const notCovered: string[] = [];

// ---------------------------------------------------------------- 1 preflight
phase("Preflight the arena server and the model lane");
// the workflow cwd may be the workspace root OR the fishbench repo itself —
// every later path is derived from this probe, never hardcoded
const locate = await world.run("python3", ["-c",
`import os
cand = os.path.abspath("fishbench")
root = cand if os.path.isdir(os.path.join(cand, "fishbench")) else os.getcwd()
if not os.path.isdir(os.path.join(root, "fishbench")):
    print("ROOT-MISSING from cwd " + os.getcwd())
    raise SystemExit(1)
print("ROOT " + root)
print("REL " + os.path.relpath(root, os.getcwd()))`]);
const rootLine = /^ROOT (.+)$/m.exec(locate.stdout);
const relLine = /^REL (.+)$/m.exec(locate.stdout);
if (locate.exitCode !== 0 || !rootLine || !relLine) {
  return {
    conclusion:
      "refused: could not locate the fishbench repo from the workflow cwd — " +
      "run this workflow from the workspace root or the fishbench dir",
    findings: [
      {
        where: "preflight",
        what: "fishbench package dir not found",
        evidence: tail(locate.stdout + "\n" + locate.stderr, 300),
        status: "unverified",
        severity: "high",
      },
    ],
    verified: [],
    notCovered: ["nothing ran — repo not found"],
  };
}
const rootAbs = rootLine[1].trim();
const relPrefix = relLine[1].trim() === "." ? "" : relLine[1].trim() + "/";
const outRel = `${relPrefix}out/tank-${slug}.json`;
log(`fishbench repo at ${rootAbs} (workflow-relative ${relPrefix || "./"})`);

let arena = await world.run("python3", ["-c",
`import json, urllib.request
try:
    with urllib.request.urlopen("${q(submitUrl)}/api/fishbench/arena", timeout=5) as r:
        d = json.loads(r.read().decode())
    print("ARENA-UP specs=%d" % len(d.get("specs", [])))
except Exception as e:
    print("ARENA-DOWN %s" % type(e).__name__)
    raise SystemExit(1)`]);
log(`preflight arena: exit ${arena.exitCode} — ${arena.stdout.trim()}`);

if (arena.exitCode !== 0) {
  // the :8383 demo server is ours — restarting it is in bounds
  const spawn = await world.run("python3", ["-c",
`import os, subprocess, sys
root = "${q(rootAbs)}"
os.makedirs(os.path.join(root, "out"), exist_ok=True)
subprocess.Popen([sys.executable, "-m", "fishbench.server", "--port", "${q(port)}"],
    cwd=root, stdout=open(os.path.join(root, "out", "server-${q(port)}.log"), "ab"),
    stderr=subprocess.STDOUT, start_new_session=True)
print("SPAWNED")`]);
  const poll = await world.run("python3", ["-c",
`import json, time, urllib.request
for _ in range(30):
    try:
        with urllib.request.urlopen("${q(submitUrl)}/api/fishbench/arena", timeout=3) as r:
            json.loads(r.read().decode())
        print("ARENA-UP")
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception:
        time.sleep(1)
print("ARENA-never-came-up")
raise SystemExit(1)`]);
  log(`server auto-start: ${spawn.stdout.trim()}, poll exit ${poll.exitCode}`);
  arena = poll;
}
verified.push(`preflight arena exit ${arena.exitCode} (${arena.stdout.trim()})`);
if (arena.exitCode !== 0) {
  findings.push({
    where: "preflight",
    what: "local arena server unreachable and auto-start failed",
    evidence: `${relPrefix}out/server-${port}.log · ${arena.stdout.trim()}`,
    status: "unverified",
    severity: "high",
  });
}

const keyCheck = await world.run("python3", ["-c",
`import json, os
def die(msg):
    print("KEY-MISSING " + msg)
    raise SystemExit(1)
if os.environ.get("${q(apiKeyEnv)}"):
    print("KEY-OK env")
    raise SystemExit(0)
try:
    cur = json.load(open(os.path.expanduser("${q(keyConfigPath)}")))
    for part in "${q(keyConfigQuery)}".split("."):
        cur = cur[part]
    if not isinstance(cur, str) or len(cur) < 10:
        die("config value at ${q(keyConfigQuery)} is not a key")
    print("KEY-OK config")
except SystemExit:
    raise
except Exception as e:
    die("config read failed: %s" % type(e).__name__)`]);
log(`preflight lane key: ${keyCheck.stdout.trim()}`);
verified.push(`preflight key exit ${keyCheck.exitCode} (${keyCheck.stdout.trim()})`);
if (keyCheck.exitCode !== 0) {
  findings.push({
    where: "preflight",
    what: "lane key unavailable — neither env nor provider config has it",
    evidence: keyCheck.stdout.trim(),
    status: "unverified",
    severity: "high",
  });
}

// ---------------------------------------------------------------- 2 gauntlet
// One python wrapper: resolves the lane key (env first, provider config
// fallback) into the child env, then execs the tank CLI with cwd=fishbench.
// The key value never appears in argv, stdout, or the journal.
const runArgs = ["-c",
`import json, os, subprocess, sys
root = "${q(rootAbs)}"
env = dict(os.environ)
if not env.get("${q(apiKeyEnv)}"):
    cur = json.load(open(os.path.expanduser("${q(keyConfigPath)}")))
    for part in "${q(keyConfigQuery)}".split("."):
        cur = cur[part]
    env["${q(apiKeyEnv)}"] = cur
out = os.path.join(root, "out", "tank-${q(slug)}.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
rc = subprocess.call([sys.executable, "-m", "fishbench.tank",
    "--model", "${q(model)}", "--base-url", "${q(baseUrl)}",
    "--api-key-env", "${q(apiKeyEnv)}",
    "--name", "${q(name)}", "--org", "${q(org)}",
    "--stages", "${q(stagesArg)}",
    "--out", out,
    "--tapes", os.path.join(root, "data", "replays"),
    "--submit", "${q(submitUrl)}"],
    cwd=root, env=env)
print("OUTPATH " + out)
raise SystemExit(rc)`];

// a timeout/spawn rejection must feed the medic loop like any other failure,
// not throw past it and kill the run
const gauntlet = async (): Promise<{ exitCode: number; stdout: string; stderr: string }> => {
  try {
    return await world.run("python3", runArgs, { timeoutMs: 2400000 });
  } catch (e) {
    return {
      exitCode: -1,
      stdout: "",
      stderr: `gauntlet command rejected (timeout or spawn failure): ${String(e)}`,
    };
  }
};

phase("Run the tank gauntlet (generate, film, measure, seal)");
let run = await gauntlet();
let medicRounds = 0;
while (run.exitCode !== 0 && medicRounds < 2) {
  medicRounds += 1;
  phase("Diagnose and fix the failed run");
  const medic = agent(`gauntlet-medic-r${medicRounds}`, {
    system:
      "You keep the FishBench TANK gauntlet running on this machine. " +
      "You fix ONLY invocation and environment causes: CLI flags, env vars, " +
      "the local arena server on :8383 (it is ours — restart it if it died), " +
      "network, disk. The frozen spec — the fishbench package's tankspec.py, " +
      "the brief pack, the scoring digests, the tests — is the law: NEVER " +
      "edit any of it to make a run pass. If the lane itself is broken " +
      "(auth failure, model gone, quota), do not work around it: return " +
      "rerunWorthwhile=false and say so plainly in the diagnosis. " +
      "Never touch unrelated processes.",
  });
  const fix = await medic.ask<MedicFix>(
    `The tank CLI failed (exit ${run.exitCode}). Diagnose, fix what is ` +
    `fixable, then answer.\n` +
    `Workdir ${relPrefix || "./"} · out ${outRel} · lane ${model} @ ${baseUrl} · ` +
    `arena ${submitUrl} · key env ${apiKeyEnv} (falls back to ` +
    `${keyConfigPath} → ${keyConfigQuery}).\n` +
    `Last output:\n${tail(run.stdout + "\n" + run.stderr, 4000)}`);
  log(`medic round ${medicRounds}: ${fix.diagnosis}`);
  findings.push({
    where: "repair loop",
    what: `medic round ${medicRounds}: ${fix.diagnosis}`,
    evidence: `gauntlet exit ${run.exitCode} before repair`,
    status: "unverified",
    severity: fix.rerunWorthwhile ? "low" : "high",
  });
  if (!fix.rerunWorthwhile) break;
  run = await gauntlet();
}
verified.push(`gauntlet exit ${run.exitCode} after ${medicRounds} medic round(s)`);

let summary: Summary | null = null;
if (run.exitCode === 0) {
  // -------------------------------------------------------------- 4 verify
  phase("Verify the card re-derives and lands on the board");
  const check = await world.run("python3", ["-c",
`import json, os, urllib.request
card = json.load(open("${q(outRel)}"))
sha8 = card["card_sha256"][:8]
stages = []
for s in card.get("stages", []):
    rel = "${q(relPrefix)}data/replays/%s/stage-%d.mp4" % (sha8,
                                                           s.get("run_number", 0))
    ok = os.path.exists(rel)
    c = s.get("categories", {})
    stages.append({
        "run": s.get("run_number", 0),
        "fish": s.get("fish_requested", s.get("fish", 0)),
        "tankscore": round(s.get("tankscore", 0.0), 2),
        "cats": {"clean_boot": c.get("clean_boot", 0.0),
                 "fish_on_screen": c.get("fish_on_screen", 0.0),
                 "sustained_swimming": c.get("sustained_swimming", 0.0),
                 "fps_at_scale": c.get("fps_at_scale", 0.0)},
        "tapeAbs": os.path.abspath(rel) if ok else "",
        "tapeRel": rel if ok else ""})
board = []
with urllib.request.urlopen("${q(submitUrl)}/api/fishbench/arena", timeout=10) as r:
    arena = json.loads(r.read().decode())
for b in arena.get("specs", []):
    if b.get("spec") == "fishbench-2":
        for e in b.get("entries", []):
            board.append({"sha8": str(e.get("card_sha256", ""))[:8],
                          "name": e.get("display_name", ""),
                          "score": e.get("score", 0.0),
                          "verified": e.get("verified", "")})
print("SUMMARY " + json.dumps({"card": sha8,
    "name": card.get("display_name", ""),
    "tankscore": round(card.get("composite", {}).get("tankscore", 0.0), 2),
    "stages": stages, "board": board}))`], { timeoutMs: 60000 });
  const m = /SUMMARY (\{.*\})/.exec(check.stdout);
  if (check.exitCode === 0 && m) {
    const d = JSON.parse(m[1]);
    const pos = Array.isArray(d.board)
      ? d.board.findIndex((e: { sha8: string }) => e.sha8 === d.card) + 1
      : 0;
    summary = {
      card: String(d.card),
      name: String(d.name),
      tankscore: Number(d.tankscore),
      verified: "",
      position: pos,
      stageRows: (Array.isArray(d.stages) ? d.stages : []).map(
        (s: {
          run: number; fish: number; tankscore: number;
          cats: { clean_boot: number; fish_on_screen: number;
                  sustained_swimming: number; fps_at_scale: number };
          tapeAbs: string; tapeRel: string;
        }) => ({
          run: Number(s.run),
          fish: Number(s.fish),
          tankscore: Number(s.tankscore),
          cats: {
            clean_boot: Number(s.cats.clean_boot),
            fish_on_screen: Number(s.cats.fish_on_screen),
            sustained_swimming: Number(s.cats.sustained_swimming),
            fps_at_scale: Number(s.cats.fps_at_scale),
          },
          tapeAbs: String(s.tapeAbs ?? ""),
          tapeRel: String(s.tapeRel ?? ""),
        })),
    };
    // re-read the row's verified flag cleanly
    const row = Array.isArray(d.board)
      ? d.board.find((e: { sha8: string; verified: string }) => e.sha8 === d.card)
      : undefined;
    if (summary && row) summary.verified = String(row.verified);
  }
  verified.push(`card/board verify exit ${check.exitCode}`);

  if (summary) {
    for (const s of summary.stageRows) {
      report(
        {
          model: name, stage: s.run, fish: s.fish,
          tankscore: s.tankscore, reference: REFERENCE_TS,
          clean_boot: s.cats.clean_boot,
          fish_on_screen: s.cats.fish_on_screen,
          sustained_swimming: s.cats.sustained_swimming,
          fps_at_scale: s.cats.fps_at_scale,
        },
        "scores");
    }
    findings.push({
      where: "arena board",
      what:
        summary.position > 0
          ? `card ${summary.card} landed on the TankScore board at position ` +
            `${summary.position} (verified=${summary.verified})`
          : `card ${summary.card} did NOT land on the board`,
      evidence: `GET ${submitUrl}/api/fishbench/arena · composite ` +
        `${summary.tankscore}/1000`,
      status: summary.position > 0 ? "verified" : "unverified",
      severity: summary.position > 0 ? "info" : "high",
    });
  } else {
    findings.push({
      where: "arena board",
      what: "card summary could not be parsed from the verify command",
      evidence: tail(check.stdout + check.stderr, 500),
      status: "unverified",
      severity: "high",
    });
  }
} else {
  findings.push({
    where: "gauntlet",
    what: `tank CLI failed (exit ${run.exitCode}) after ${medicRounds} medic round(s)`,
    evidence: tail(run.stdout + "\n" + run.stderr, 800),
    status: "unverified",
    severity: "high",
  });
}

// ------------------------------------------------------- 5 sighted eye gate
let verdicts: TapeVerdict[] = [];
if (summary && summary.stageRows.length > 0) {
  phase("Watch every stage tape, then publish the verdict");
  verdicts = await Promise.all(
    summary.stageRows.map((s) =>
      agent(`tape-reviewer-${s.fish}`, {
        system:
          "You are the sighted gate for FishBench TANK: an honest film " +
          "reviewer. You watch stage tapes (mp4) of model-generated three.js " +
          "aquariums and judge what is actually on screen — nothing else. " +
          "You never edit files. fishSeen is your honest order-of-magnitude " +
          "count from the tape, never a guess from the claim.",
      }).ask<TapeVerdict>(
        `Open and watch this tape with the Read tool (it accepts video): ` +
        `${s.tapeAbs}\n` +
        `The sealed card claims this stage requested ${s.fish} fish, with ` +
        `category scores fish_on_screen=${s.cats.fish_on_screen}/100 and ` +
        `sustained_swimming=${s.cats.sustained_swimming}/100.\n` +
        `Judge honestly: roughly how many fish are actually visible, are ` +
        `they visibly swimming (not a frozen frame), and does the tape ` +
        `agree with the card's claims? If the file cannot be opened, set ` +
        `agreesWithCard=false and say exactly that in the note.`)),
  );
  for (const v of verdicts) {
    log(`tape ${v.fish}: seen~${v.fishSeen} swimming=${v.swimming} ` +
        `agrees=${v.agreesWithCard} — ${v.note}`);
    if (!v.agreesWithCard) {
      findings.push({
        where: `sighted gate, stage ${v.fish} fish`,
        what: "tape disagrees with the card's claims (or could not be opened)",
        evidence: v.note,
        status: "verified",
        severity: "high",
      });
    }
  }
  for (const s of summary.stageRows) {
    if (!s.tapeRel && !s.tapeAbs) {
      notCovered.push(`stage ${s.fish} tape missing on disk — no sighted check`);
    }
  }
} else if (summary) {
  notCovered.push("no stage rows on the card — sighted gate had nothing to watch");
} else {
  notCovered.push("gauntlet failed — no card, no tapes, no sighted gate");
}

// ---------------------------------------------------------------- 6 publish
const lines: string[] = [];
lines.push(`# TANK gauntlet — ${name}`);
lines.push("");
lines.push(
  `- **lane** \`${model}\` @ \`${baseUrl}\` · org \`${org || "—"}\``);
lines.push(
  `- **card** sha8 \`${summary ? summary.card : "—"}\` · ` +
  `verified **${summary ? summary.verified || "?" : "no card"}** · ` +
  `board position **${summary && summary.position > 0 ? `#${summary.position}` : "—"}**`);
lines.push(
  `- **TankScore** **${summary ? summary.tankscore : "—"} / 1000** ` +
  `(reference tank ${REFERENCE_TS})`);
lines.push(`- stages \`${stagesArg}\` · medic rounds ${medicRounds}`);
lines.push("");
if (summary && summary.stageRows.length > 0) {
  lines.push(`| stage | fish | TankScore | clean boot | fish on screen | sustained | fps |`);
  lines.push(`|---|---|---|---|---|---|---|`);
  for (const s of summary.stageRows) {
    lines.push(
      `| ${s.run} | ${s.fish} | ${s.tankscore} | ${s.cats.clean_boot} | ` +
      `${s.cats.fish_on_screen} | ${s.cats.sustained_swimming} | ` +
      `${s.cats.fps_at_scale} |`);
  }
  lines.push("");
}
if (verdicts.length > 0) {
  lines.push(`## Sighted eye — tape by tape`);
  lines.push("");
  lines.push(`| fish | seen on tape | swimming | agrees with card | reviewer note |`);
  lines.push(`|---|---|---|---|---|`);
  for (const v of verdicts) {
    lines.push(
      `| ${v.fish} | ~${v.fishSeen} | ${v.swimming ? "yes" : "NO"} | ` +
      `${v.agreesWithCard ? "yes" : "NO"} | ${v.note} |`);
  }
  lines.push("");
}
lines.push(`## Honest receipts`);
for (const v of verified) lines.push(`- ${v}`);
lines.push(`- model lane: \`${model}\`; workflow harness: ZCode dynamic workflow`);
lines.push("");
if (notCovered.length > 0) {
  lines.push(`## Not covered`);
  for (const n of notCovered) lines.push(`- ${n}`);
  lines.push("");
}

await artifact.markdown("report", lines.join("\n"), {
  title: `TANK verdict — ${name}`,
  primary: true,
});
try {
  await artifact.file("card", outRel, { title: `Sealed card JSON — ${name}` });
} catch {
  notCovered.push(`sealed card json not published (${outRel} missing)`);
}
if (summary) {
  const sample =
    summary.stageRows.find((s) => s.tapeRel && s.fish === 5) ??
    summary.stageRows.find((s) => s.tapeRel);
  if (sample) {
    try {
      await artifact.file("tape-sample", sample.tapeRel, {
        title: `Stage tape — ${sample.fish} fish (watch it)`,
      });
    } catch {
      notCovered.push(`sample tape not published (${sample.tapeRel})`);
    }
  }
}

const conclusion = summary
  ? `${name} (\`${model}\`): TankScore ${summary.tankscore}/1000 vs reference ` +
    `${REFERENCE_TS} — card ${summary.card} ` +
    `${summary.verified === "verified" ? "re-derived and verified" : "UNVERIFIED"}` +
    `${summary.position > 0 ? `, board position #${summary.position}` : ""}` +
    `; ${verdicts.filter((v) => v.agreesWithCard).length}/${verdicts.length}` +
    ` tapes passed the sighted gate` +
    (medicRounds > 0 ? `; needed ${medicRounds} medic round(s)` : "")
  : `gauntlet FAILED for ${name} (\`${model}\`) — exit ${run.exitCode} after ` +
    `${medicRounds} medic round(s); see findings`;

return {
  conclusion,
  findings,
  verified,
  notCovered,
};
