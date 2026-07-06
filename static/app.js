/**
 * RAVEN UI — Frontend
 * Single-page app that talks to the Python HTTP server.
 */

"use strict";

// ──────────────────────────────────────────────────────────────────────────────
// Pipeline stage definitions (must match server.py PIPELINE_STAGES)
// ──────────────────────────────────────────────────────────────────────────────

const STAGES = [
  { key: "intake",   label: "Fetch Issue",     icon: "I" },
  { key: "repo",     label: "Clone Repo",      icon: "R" },
  { key: "media",    label: "Frames",          icon: "M" },
  { key: "emulator", label: "Install APK",     icon: "E" },
  { key: "agent1",   label: "Action Seq",      icon: "A1" },
  { key: "agent2",   label: "Reproduce",       icon: "A2" },
  { key: "fallback", label: "TF-IDF Fallback", icon: "FB" },
  { key: "agent3",   label: "HDG Build",       icon: "A3" },
  { key: "agent4",   label: "Root Cause",      icon: "A4" },
  { key: "done",     label: "Complete",        icon: "✓" },
];

// ──────────────────────────────────────────────────────────────────────────────
// State
// ──────────────────────────────────────────────────────────────────────────────

let currentRunId = null;
let currentSSE   = null;
let appPresets   = {};

// ──────────────────────────────────────────────────────────────────────────────
// DOM helpers
// ──────────────────────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
};

// ──────────────────────────────────────────────────────────────────────────────
// Tabs
// ──────────────────────────────────────────────────────────────────────────────

document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(s => s.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "history") loadHistory();
  });
});

// ──────────────────────────────────────────────────────────────────────────────
// Stage grid
// ──────────────────────────────────────────────────────────────────────────────

function buildStageGrid() {
  const grid = $("stage-grid");
  grid.innerHTML = "";
  STAGES.forEach(s => {
    const card = el("div", "stage-card", `
      <div class="stage-icon" id="icon-${s.key}">${s.icon}</div>
      <div class="stage-name">${s.label}</div>
      <div class="stage-msg"  id="msg-${s.key}">—</div>
    `);
    card.id = "stage-" + s.key;
    grid.appendChild(card);
  });
}

function updateStage(key, status, message) {
  const card = $("stage-" + key);
  if (!card) return;
  card.className = "stage-card " + (status || "idle");
  const msg = $("msg-" + key);
  if (msg && message) {
    // Truncate long messages for the card
    msg.textContent = message.length > 60 ? message.slice(0, 57) + "…" : message;
    msg.title = message;
  }
}

function resetStages() {
  STAGES.forEach(s => updateStage(s.key, "idle", "—"));
}

// ──────────────────────────────────────────────────────────────────────────────
// Event log
// ──────────────────────────────────────────────────────────────────────────────

function appendLog(ev) {
  const pre = $("log-pre");
  const cls = `log-line-${ev.status}`;
  const agent = ev.agent.padEnd(9);
  const line = `[${ev.ts}] ${agent} ${ev.status.padEnd(9)} ${ev.message}\n`;
  const span = el("span", cls, escHtml(line));
  pre.appendChild(span);

  // Auto-scroll
  const scroll = $("log-scroll");
  scroll.scrollTop = scroll.scrollHeight;
}

$("clear-log-btn").addEventListener("click", () => {
  $("log-pre").innerHTML = "";
});

function escHtml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// ──────────────────────────────────────────────────────────────────────────────
// App presets
// ──────────────────────────────────────────────────────────────────────────────

async function loadAppPresets() {
  try {
    const r = await fetch("/api/apps");
    const data = await r.json();
    appPresets = data.apps || {};
    const sel = $("app-preset");
    Object.entries(appPresets).forEach(([key, app]) => {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = app.label + (app.apk_found ? "" : " (no APK)");
      sel.appendChild(opt);
    });
  } catch (e) {
    console.warn("Could not load app presets:", e);
  }
}

$("app-preset").addEventListener("change", () => {
  const key = $("app-preset").value;
  const preset = appPresets[key];
  if (!preset) return;
  if (!$("repo-url").value) $("repo-url").value = preset.repo_url;
  if (preset.apk_path) {
    $("apk-path").value = preset.apk_path;
    showApkStatus(true, preset.apk_path.split(/[\\/]/).pop());
  } else {
    $("apk-path").value = "";
    showApkStatus(false);
  }
});

$("apk-path").addEventListener("input", () => {
  const v = $("apk-path").value.trim();
  if (v) showApkStatus(null, v.split(/[\\/]/).pop());
  else showApkStatus(null);
});

function showApkStatus(found, name) {
  const el = $("apk-status");
  if (found === true)  { el.className = "apk-status found";   el.textContent = "✓ " + name; }
  else if (found === false) { el.className = "apk-status missing"; el.textContent = "APK not found for this app — install manually or check skip-install"; }
  else { el.className = "apk-status"; el.textContent = name ? name : ""; }
}

// ──────────────────────────────────────────────────────────────────────────────
// Form submission — start run
// ──────────────────────────────────────────────────────────────────────────────

$("run-form").addEventListener("submit", async e => {
  e.preventDefault();
  await startRun();
});

async function startRun() {
  const issueUrl = $("issue-url").value.trim();
  if (!issueUrl) return;

  // Reset UI
  resetStages();
  $("log-pre").innerHTML = "";
  $("results-content").style.display = "none";
  $("results-empty").style.display = "flex";
  $("run-btn").classList.add("hidden");
  $("stop-btn").classList.remove("hidden");

  // Switch to pipeline tab
  document.querySelector('[data-tab="pipeline"]').click();

  const payload = {
    issue_url:       issueUrl,
    repo_url:        $("repo-url").value.trim() || undefined,
    app_preset:      $("app-preset").value || undefined,
    apk_path:        $("apk-path").value.trim() || undefined,
    skip_install:    $("skip-install").checked,
    config_path:     $("config-path").value.trim() || undefined,
    media_path:      $("media-path").value.trim() || undefined,
    emulator_serial: $("emulator-serial").value.trim() || undefined,
  };

  try {
    const r = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const { run_id, error } = await r.json();
    if (error) throw new Error(error);
    currentRunId = run_id;
    subscribeSSE(run_id);
  } catch (err) {
    alert("Failed to start run: " + err.message);
    $("run-btn").classList.remove("hidden");
    $("stop-btn").classList.add("hidden");
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Stop button
// ──────────────────────────────────────────────────────────────────────────────

$("stop-btn").addEventListener("click", async () => {
  if (!currentRunId) return;
  await fetch(`/api/run/${currentRunId}/stop`, { method: "POST" });
});

// ──────────────────────────────────────────────────────────────────────────────
// SSE subscription
// ──────────────────────────────────────────────────────────────────────────────

function subscribeSSE(runId) {
  if (currentSSE) currentSSE.close();
  const source = new EventSource(`/events?run_id=${runId}`);
  currentSSE = source;

  source.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    appendLog(ev);
    updateStage(ev.agent, ev.status, ev.message);

    if (ev.agent === "done") {
      source.close();
      currentSSE = null;
      $("run-btn").classList.remove("hidden");
      $("stop-btn").classList.add("hidden");
      loadRunResult(runId);
    }
  };

  source.onerror = () => {
    source.close();
    currentSSE = null;
    $("run-btn").classList.remove("hidden");
    $("stop-btn").classList.add("hidden");
  };
}

// ──────────────────────────────────────────────────────────────────────────────
// Load and display results
// ──────────────────────────────────────────────────────────────────────────────

async function loadRunResult(runId) {
  try {
    const r = await fetch(`/api/run/${runId}`);
    const data = await r.json();
    if (data.result) {
      renderResults(data);
      document.querySelector('[data-tab="results"]').click();
    }
  } catch (e) {
    console.warn("Failed to load result:", e);
  }
}

function renderResults(data) {
  const result = data.result;
  if (!result) return;

  // Meta bar
  const reproduced  = result.reproduced;
  const usedFallback = result.used_fallback;
  const modeLabel = usedFallback ? "Static TF-IDF" : "Emulator Replay";
  const modeBadge = usedFallback
    ? `<span class="badge badge-yellow">Static TF-IDF fallback</span>`
    : `<span class="badge badge-green">Emulator replay</span>`;
  const reprBadge = reproduced
    ? `<span class="badge badge-green">Reproduced</span>`
    : `<span class="badge badge-red">Not reproduced</span>`;

  $("results-meta").innerHTML = `
    <div class="meta-item"><span class="meta-label">Issue</span>
      <a class="meta-value" href="${escHtml(data.issue_url)}" target="_blank">${escHtml(data.issue_url)}</a></div>
    <div class="meta-item"><span class="meta-label">Checkout</span>
      <span class="meta-value">${escHtml(result.checkout_ref || "—")}</span></div>
    <div class="meta-item"><span class="meta-label">Reproduction</span>${reprBadge}</div>
    <div class="meta-item"><span class="meta-label">Coverage mode</span>${modeBadge}</div>
    <div class="meta-item"><span class="meta-label">Attempts</span>
      <span class="meta-value">${result.attempts}</span></div>
  `;

  // Candidates
  const list = $("candidates-list");
  list.innerHTML = "";
  const candidates = result.candidates || [];
  if (candidates.length === 0) {
    list.innerHTML = '<li style="color:var(--muted);font-size:12px">No candidates.</li>';
  } else {
    candidates.forEach(c => {
      const score = parseFloat(c.suspicion) || 0;
      const pct   = Math.round(score * 100);
      const fillCls = score >= 0.7 ? "" : score >= 0.4 ? "mid" : "low";
      const li = document.createElement("li");
      li.className = "candidate-card";
      li.innerHTML = `
        <div class="cand-header">
          <span class="cand-rank">#${c.rank}</span>
          <span class="cand-file">${escHtml(c.file)}${c.line ? ":" + c.line : ""}</span>
          <span class="cand-score">${pct}%</span>
        </div>
        <div class="score-bar"><div class="score-fill ${fillCls}" style="width:${pct}%"></div></div>
        ${c.symbol ? `<div class="cand-symbol">${escHtml(c.symbol)}</div>` : ""}
        ${c.reasoning ? `<div class="cand-reason">${escHtml(truncate(c.reasoning, 300))}</div>` : ""}
        ${c.snippet ? `<pre class="cand-snippet">${escHtml(c.snippet)}</pre>` : ""}
      `;
      list.appendChild(li);
    });
  }

  // Files
  $("coverage-mode").innerHTML = usedFallback
    ? `<span class="badge badge-yellow">TF-IDF (no runtime coverage)</span>`
    : `<span class="badge badge-blue">Runtime logcat coverage</span>`;
  const filesList = $("files-list");
  filesList.innerHTML = "";
  (result.covered_files || []).forEach(f => {
    const li = document.createElement("li");
    li.title = f;
    // Show just last 2-3 path segments
    li.textContent = f.split(/[\\/]/).slice(-3).join("/");
    filesList.appendChild(li);
  });

  $("results-empty").style.display = "none";
  $("results-content").style.display = "flex";
}

function truncate(s, n) {
  return s.length > n ? s.slice(0, n) + " …" : s;
}

// ──────────────────────────────────────────────────────────────────────────────
// History tab
// ──────────────────────────────────────────────────────────────────────────────

$("refresh-history-btn").addEventListener("click", loadHistory);

async function loadHistory() {
  $("history-loading").style.display = "flex";
  $("history-table").classList.add("hidden");
  try {
    const r = await fetch("/api/workspace");
    const { runs } = await r.json();
    const tbody = $("history-tbody");
    tbody.innerHTML = "";
    runs.forEach(run => {
      const score = parseFloat(run.top_score) || 0;
      const pct   = Math.round(score * 100);
      const chipCls = score >= 0.7 ? "badge-green" : score >= 0.4 ? "badge-yellow" : "badge-red";
      const modeCell = run.used_fallback
        ? `<span class="badge badge-yellow">TF-IDF</span>`
        : `<span class="badge badge-green">Emulator</span>`;
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono">${escHtml(run.slug)}</td>
        <td><a href="${escHtml(run.issue_url)}" target="_blank">${escHtml(run.issue_title || run.issue_url)}</a></td>
        <td>${modeCell}</td>
        <td class="mono" title="${escHtml(run.top_file)}">${escHtml(run.top_file.split(/[\\/]/).slice(-2).join("/"))}</td>
        <td><span class="score-chip ${chipCls}">${pct}%</span></td>
      `;
      tbody.appendChild(tr);
    });
    $("history-loading").style.display = "none";
    $("history-table").classList.remove("hidden");
    if (runs.length === 0) {
      $("history-loading").textContent = "No completed runs in workspace.";
      $("history-loading").style.display = "flex";
    }
  } catch (e) {
    $("history-loading").textContent = "Failed to load history.";
    $("history-loading").style.display = "flex";
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Emulator badge (poll emulator status via a quick heuristic)
// We just check if the server is responding; real emulator check is server-side.
// ──────────────────────────────────────────────────────────────────────────────

async function pollEmulator() {
  try {
    // Ping the API — if server is up, emulator status is shown in the run events
    await fetch("/api/apps");
    $("emulator-badge").querySelector(".dot").className = "dot dot-ok";
    $("emulator-label").textContent = "Server ready";
  } catch {
    $("emulator-badge").querySelector(".dot").className = "dot dot-err";
    $("emulator-label").textContent = "Server offline";
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Quick-pick: populate from BUGS list if available (hardcoded for the 50 bugs)
// ──────────────────────────────────────────────────────────────────────────────

const QUICK_ISSUES = [
  // Simplenote
  { label: "SN #1765", url: "https://github.com/Automattic/simplenote-android/issues/1765" },
  { label: "SN #1751", url: "https://github.com/Automattic/simplenote-android/issues/1751" },
  // AnkiDroid
  { label: "AD #19426", url: "https://github.com/ankidroid/Anki-Android/issues/19426" },
  { label: "AD #21242", url: "https://github.com/ankidroid/Anki-Android/issues/21242" },
  // Amaze
  { label: "AM #4619", url: "https://github.com/TeamAmaze/AmazeFileManager/issues/4619" },
  { label: "AM #4585", url: "https://github.com/TeamAmaze/AmazeFileManager/issues/4585" },
  // K9Mail
  { label: "K9 #11164", url: "https://github.com/thunderbird/thunderbird-android/issues/11164" },
  { label: "K9 #11076", url: "https://github.com/thunderbird/thunderbird-android/issues/11076" },
  // NewPipe
  { label: "NP #13506", url: "https://github.com/TeamNewPipe/NewPipe/issues/13506" },
  { label: "NP #13612", url: "https://github.com/TeamNewPipe/NewPipe/issues/13612" },
  // AntennaPod
  { label: "AP #8528",  url: "https://github.com/AntennaPod/AntennaPod/issues/8528" },
  { label: "AP #8284",  url: "https://github.com/AntennaPod/AntennaPod/issues/8284" },
  // WordPress
  { label: "WP #22878", url: "https://github.com/wordpress-mobile/WordPress-Android/issues/22878" },
  { label: "WP #23014", url: "https://github.com/wordpress-mobile/WordPress-Android/issues/23014" },
];

function buildQuickPick() {
  const ul = $("quick-list");
  QUICK_ISSUES.forEach(item => {
    const li = document.createElement("li");
    const a  = document.createElement("a");
    a.href        = "#";
    a.textContent = item.label + " — " + item.url.split("/issues/")[0].split("/").pop();
    a.title       = item.url;
    a.addEventListener("click", e => {
      e.preventDefault();
      $("issue-url").value = item.url;
      // Auto-fill preset
      const app = inferAppPreset(item.url);
      if (app) {
        $("app-preset").value = app;
        $("app-preset").dispatchEvent(new Event("change"));
      }
    });
    li.appendChild(a);
    ul.appendChild(li);
  });
  $("quick-pick").style.display = "block";
}

function inferAppPreset(url) {
  if (url.includes("simplenote-android")) return "simplenote";
  if (url.includes("Anki-Android"))        return "ankidroid";
  if (url.includes("WordPress-Android"))   return "wordpress";
  if (url.includes("AmazeFileManager"))    return "amaze";
  if (url.includes("thunderbird-android")) return "k9mail";
  if (url.includes("NewPipe"))             return "newpipe";
  if (url.includes("AntennaPod"))          return "antennapod";
  return "";
}

// ──────────────────────────────────────────────────────────────────────────────
// Init
// ──────────────────────────────────────────────────────────────────────────────

(async () => {
  buildStageGrid();
  await loadAppPresets();
  buildQuickPick();
  pollEmulator();
  setInterval(pollEmulator, 30_000);
})();
