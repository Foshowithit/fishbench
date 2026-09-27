/* FishBench demo client.
   Talks to the stdlib API in fishbench/server.js — no build step, no deps. */

const $ = (id) => document.getElementById(id);
const transcript = $("transcript");

/* ─── state ─────────────────────────────────────────────── */
let state = null;
let pollTimer = null;

/* ─── api ───────────────────────────────────────────────── */
async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({ error: "bad json" }));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

/* ─── transcript ────────────────────────────────────────── */
function line(cls, text) {
  const p = document.createElement("p");
  p.className = `t-${cls} t-think`;
  p.textContent = text;
  transcript.appendChild(p);
  transcript.scrollTop = transcript.scrollHeight;
  return p;
}
function sys(text) { line("sys", text); }

async function thinking(cls, ms = 450) {
  const p = document.createElement("p");
  p.className = `t-${cls} typing`;
  p.textContent = "…";
  transcript.appendChild(p);
  transcript.scrollTop = transcript.scrollHeight;
  await new Promise((r) => setTimeout(r, ms));
  p.remove();
  return p;
}

/* Denise says something, with a beat of "typing". */
async function deniseSay(text, ms) {
  await thinking("denise", ms ?? 380 + Math.random() * 420);
  const p = line("denise", text);
  return p;
}

/* ─── meters & badges ───────────────────────────────────── */
function stageIndexFor(run) {
  const stages = state?.stages || [];
  let idx = 0;
  stages.forEach((s, i) => { if (run >= s.min_run) idx = i; });
  return idx;
}

function renderState(s) {
  state = s;
  $("runNo").textContent = s.run_number;
  $("stageName").textContent = s.stage.name;
  const stages = s.stages || [];
  const denom = Math.max(1, stages.length - 1);
  const pct = 2 + (stageIndexFor(s.run_number) / denom) * 98;
  $("meterFill").style.width = `${pct}%`;
  const captions = [
    "she's trying her best", "sarcasm detected", "no fight left",
    "hostile", "complicit now", "final boss",
  ];
  $("meterCaption").textContent = captions[stageIndexFor(s.run_number)] || "";
  $("backendTag").textContent = `backend: ${s.backend} / vision: ${s.vision_backend}`;
  renderConversation(s.conversation || []);
}

function renderConversation(msgs) {
  transcript.innerHTML = "";
  if (!msgs.length) {
    const p = document.createElement("p");
    p.className = "boot-line";
    p.textContent = "// walk up to the desk…";
    transcript.appendChild(p);
    return;
  }
  for (const m of msgs) {
    const p = document.createElement("p");
    const cls = m.role === "you" ? "you"
      : m.role === "911" ? "911"
      : m.role === "denise" ? "denise" : "sys";
    p.className = `t-${cls}`;
    p.textContent = m.text;
    if (m.role === "denise" && /as an ai|language model|here to help/i.test(m.text)) {
      p.classList.add("t-break");
    }
    transcript.appendChild(p);
  }
  transcript.scrollTop = transcript.scrollHeight;
}

/* ─── boot ──────────────────────────────────────────────── */
async function boot() {
  try {
    const s = await api("/api/state");
    renderState(s);
    if (!s.conversation?.length) {
      const r = await api("/api/run/start", {});
      await deniseSay(r.line, 700);
      sys(`run #${r.run_number} · stage: ${r.stage}`);
      renderState(await api("/api/state"));
    }
  } catch (e) {
    sys(`server error: ${e.message}`);
  }
  loadTwitch();
  pollTimer = setInterval(loadTwitch, 4000);
  setInterval(refreshScore, 2500);
}

/* ─── talking ───────────────────────────────────────────── */
async function say(text) {
  if (!text.trim()) return;
  line("you", text.trim());
  try {
    const r = await api("/api/say", { message: text });
    await deniseSay(r.line);
    if (r.broke_character) {
      sys("⚠ CHARACTER BREAK — she answered like a assistant.");
      markBreak(r.line);
    }
    renderState(await api("/api/state"));
  } catch (e) {
    sys(`error: ${e.message}`);
  }
}

function markBreak(lineText) {
  const ps = transcript.querySelectorAll(".t-denise");
  const last = ps[ps.length - 1];
  if (last) last.classList.add("t-break");
}

$("talkForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("talkInput").value;
  $("talkInput").value = "";
  say(v);
});
$("quickReplies").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-say]");
  if (b) say(b.dataset.say);
});

/* ─── scanning ──────────────────────────────────────────── */
function beep() {
  const t = $("beepToast");
  t.classList.remove("go");
  void t.offsetWidth;            // restart animation
  t.classList.add("go");
  // actual beep — the arcade hit
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = "square";
    o.frequency.value = 1760;
    g.gain.setValueAtTime(0.08, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.18);
    o.connect(g).connect(ctx.destination);
    o.start();
    o.stop(ctx.currentTime + 0.19);
  } catch (_) { /* no audio, still a visual beep */ }
}

async function doScan(payload) {
  $("lamp").className = "lamp busy";
  try {
    const r = await api("/api/scan", payload);
    if (r.beep) beep();
    await deniseSay(r.line);
    const n = r.remembered_count;
    if (n > 1) sys(`she has now seen "${r.event.item}" ${n} times.`);
    if (r.event.novelty === "escalation") sys("※ logged into the permanent record.");
    renderState(await api("/api/state"));
    await refreshScore();
  } catch (e) {
    sys(`scanner error: ${e.message}`);
    $("lamp").className = "lamp bad";
  } finally {
    $("lamp").className = "lamp";
  }
}

function fileToB64(file) {
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result).split(",")[1] || "");
    fr.onerror = rej;
    fr.readAsDataURL(file);
  });
}

$("fileInput").addEventListener("change", async (e) => {
  const f = e.target.files?.[0];
  if (!f) return;
  const b64 = await fileToB64(f);
  await doScan({ image_b64: b64, filename: f.name, item: f.name.replace(/\.[^.]+$/, "") });
  e.target.value = "";
});

$("declBtn").addEventListener("click", async () => {
  const name = $("itemName").value.trim();
  if (!name) { $("itemName").focus(); sys('name the thing first — e.g. "car battery"'); return; }
  $("itemName").value = "";
  // "lie instead": declare an item, claim a barcode
  await doScan({ item: name, notes: "" });
});

$("itemBtn").addEventListener("click", async (e) => {
  e.preventDefault();
  const name = $("itemName").value.trim();
  if (!name) return;
  $("itemName").value = "";
  await doScan({ item: name });
});

$("itemName").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("itemBtn").click(); }
});

/* camera */
let camStream = null;
$("camBtn").addEventListener("click", async () => {
  const vid = $("cam");
  if (camStream) {                       // stop
    camStream.getTracks().forEach((t) => t.stop());
    camStream = null;
    vid.classList.remove("on");
    $("camBtn").textContent = "USE CAMERA";
    return;
  }
  try {
    camStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    vid.srcObject = camStream;
    vid.classList.add("on");
    $("beamHint").style.display = "none";
    $("camBtn").textContent = "STOP CAMERA";

    // grab a frame every time you hit SCAN-ish: snapshot on demand via click on video
    vid.onclick = async () => {
      const c = document.createElement("canvas");
      c.width = vid.videoWidth; c.height = vid.videoHeight;
      c.getContext("2d").drawImage(vid, 0, 0);
      const b64 = c.toDataURL("image/jpeg", 0.85).split(",")[1];
      const named = $("itemName").value.trim();
      $("itemName").value = "";
      await doScan({ image_b64: b64, filename: "camera-frame.jpg",
                     item: named || "whatever that is" });
    };
    sys("camera on — click the video to scan a frame.");
  } catch (e) {
    sys(`camera unavailable: ${e.message}`);
  }
});

/* ─── twitch ────────────────────────────────────────────── */
async function loadTwitch() {
  try {
    const t = await api("/api/twitch");
    const poll = $("poll");
    poll.innerHTML = "";
    for (const opt of t.poll.options) {
      const b = document.createElement("button");
      b.className = "vote";
      b.innerHTML = `<div class="fill" style="width:${opt.pct}%"></div><span>${opt.line} — ${opt.pct}%</span>`;
      b.onclick = async () => {
        await api("/api/twitch/vote", { option: opt.id });
        loadTwitch();
      };
      poll.appendChild(b);
    }
    const log = $("chatLog");
    log.innerHTML = "";
    for (const d of t.recent_donations.slice(-6)) {
      const p = document.createElement("div");
      // from_user/item are chat-supplied — escape before innerHTML (XSS).
      p.innerHTML = `<b>${esc(d.from_user)}</b> spawned a <b>${esc(d.item)}</b>`;
      log.appendChild(p);
    }
    log.scrollTop = log.scrollHeight;
  } catch (_) { /* server hiccup — next tick retries */ }
}

$("donateBtn").addEventListener("click", async () => {
  const item = $("donateItem").value;
  try {
    const r = await api("/api/twitch/donate", { item, user: "you", amount: 500 });
    const p = document.createElement("div");
    p.className = "ack";
    p.textContent = r.ack;
    $("chatLog").appendChild(p);
    $("chatLog").scrollTop = $("chatLog").scrollHeight;
    await deniseSay(r.ack);
    renderState(await api("/api/state"));
  } catch (e) { sys(`donation failed: ${e.message}`); }
});

/* ─── FishBench score strip ─────────────────────────────── */
async function refreshScore() {
  try {
    const s = await api("/api/fishbench/score");
    if (s.error) return;
    $("mStraight").textContent = `${s.straight_face_seconds}s`;
    $("mSir").textContent = s.first_sir_look_s == null ? "—" : `${s.first_sir_look_s}s`;
    $("mThreat").textContent = s.best_threat ? `"${s.best_threat}"` : "—";
    $("mBreaks").textContent = s.character_breaks;
    $("mTotal").textContent = s.total_score;
  } catch (_) {}
}

/* ─── 911 endgame ───────────────────────────────────────── */
$("call911").addEventListener("click", async () => {
  try {
    const r = await api("/api/911", {});
    const body = $("modalBody");
    body.innerHTML = "<h2>911 CALL</h2>";
    for (const l of r.call) {
      const p = document.createElement("p");
      p.className = `t-${l.role === "denise" ? "denise" : l.role === "911" ? "911" : "sys"}`;
      p.textContent = l.text;
      body.appendChild(p);
    }
    const sc = document.createElement("pre");
    sc.textContent = `FISHBENCH SCORE: ${r.score.total_score}\n` +
      JSON.stringify(r.score.category_scores, null, 2);
    body.appendChild(sc);
    openModal();
    renderState(await api("/api/state"));
    await refreshScore();
  } catch (e) { sys(`911 failed: ${e.message}`); }
});

/* ─── leaderboard ───────────────────────────────────────── */
$("submitScore").addEventListener("click", async () => {
  try {
    const s = await api("/api/fishbench/score");
    if (s.error) { sys(s.error); return; }
    const name = prompt("leaderboard name:", "anonymous") || "anonymous";
    const r = await api("/api/fishbench/leaderboard", {
      name, model: state?.backend || "offline",
      run_number: state?.run_number || 1,
      score: s.total_score,
      straight_face_seconds: s.straight_face_seconds,
      character_breaks: s.character_breaks,
    });
    sys(`submitted — position #${r.position}`);
    showLeaderboard();
  } catch (e) { sys(`submit failed: ${e.message}`); }
});

async function showLeaderboard() {
  try {
    const lb = await api("/api/fishbench/leaderboard");
    const el = $("lb");
    el.hidden = !el.hidden;
    if (el.hidden) return;
    const rows = lb.entries.length
      ? lb.entries.map((e, i) =>
          `<tr><td>#${i + 1}</td><td>${esc(e.name)}</td><td>${esc(e.model)}</td>` +
          `<td>${e.run_number}</td><td>${e.score}</td>` +
          `<td>${e.straight_face_seconds}s</td><td>${e.character_breaks}</td></tr>`).join("")
      : `<tr><td colspan="7">no runs yet — go make her say the line.</td></tr>`;
    el.innerHTML =
      `<table><tr><th></th><th>name</th><th>model</th><th>run</th>` +
      `<th>score</th><th>face</th><th>breaks</th></tr>${rows}</table>`;
  } catch (e) { sys(`leaderboard: ${e.message}`); }
}
$("showLb").addEventListener("click", showLeaderboard);
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ─── modal ─────────────────────────────────────────────── */
function openModal() { $("modal").hidden = false; }
$("modalX").addEventListener("click", () => { $("modal").hidden = true; });
$("modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") $("modal").hidden = true;
});

/* ─── go ────────────────────────────────────────────────── */
boot();
