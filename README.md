# Bridging the Visual-to-Code Gap in Android Fault Localization via Polyglot Code Graphs

Functional UI bugs in Android apps are difficult to localize because they manifest as incorrect GUI behavior rather than crashes or exceptions, and their root causes often span UI interactions, XML layouts, Kotlin/Java code, resource bindings, and framework callbacks. We present RAVEN, a framework for localizing such bugs from issue reports. It uses a vision-language model to reconstruct UI interaction sequences from reproduction steps, screenshots, or videos, and adaptively replays them on an Android emulator to ground GUI-level symptoms in runtime evidence. It then builds an execution-guided Polyglot Heterogeneous Dependency Graph (HDG) by combining parser-derived candidate dependencies, emulator execution, and validated LLM-assisted cross-artifact links among UI actions, widgets, XML layouts, resources, Kotlin/Java code, generated
bindings, and lifecycle callbacks. Finally, RAVEN applies graph-grounded LLM reasoning to rank suspicious methods and files by tracing cross-language dependencies from the observed UI symptom to source-level entities. Our empirical evaluation shows that RAVEN improves over the strong coding agents in fault localization for Android UI functional bugs.

# RAVEN — Reproduction Package

**RAVEN** is a four-agent LLM pipeline that automatically locates the root cause of an Android bug given only a GitHub issue URL. It combines dynamic emulator-based bug reproduction, ADB logcat tracing, static program analysis (HDG), and chain-of-thought LLM reasoning to rank the exact source file and code statement responsible for a reported defect.

This package is **self-contained**: all pipeline source code is bundled in the `raven/` subdirectory. No external checkout of the main Raven repository is required.

---

## Table of Contents

1. [System Requirements](#1-system-requirements)
2. [One-Time Installation](#2-one-time-installation)
3. [Emulator Setup](#3-emulator-setup)
4. [Configuration File](#4-configuration-file)
5. [Running RAVEN — Single Bug (CLI)](#5-running-raven--single-bug-cli)
6. [Running RAVEN — Web UI](#6-running-raven--web-ui)
7. [Running the Full 50-Bug Evaluation](#7-running-the-full-50-bug-evaluation)
8. [Output Files Explained](#8-output-files-explained)
9. [Pipeline Architecture](#9-pipeline-architecture)
10. [Dataset Apps and APK Notes](#10-dataset-apps-and-apk-notes)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. System Requirements

All requirements below must be satisfied on the machine running RAVEN. The exact paths shown are for this installation on Windows 11; adjust if you are on Linux/macOS.

| Requirement | Version / Notes | Where to install |
|---|---|---|
| **Windows 11** | or Linux/macOS | — |
| **Python 3.11+** | Standard Python install | python.org or `winget install Python.Python.3.11` |
| **Android SDK** | API 29 system image, `platform-tools` | Android Studio SDK Manager |
| **ADB** | `platform-tools/adb.exe` | Bundled with Android SDK |
| **Android Emulator** | Pixel29 AVD (API 29, x86, Google Play) | Android Studio AVD Manager |
| **Java 11+** | Required by Joern | `winget install Microsoft.OpenJDK.11` |
| **ffmpeg** | Frame extraction from bug videos | `winget install Gyan.FFmpeg` or PATH |
| **Joern CLI** | `joern-parse` + `joern-export` | `raven install-tools joern` (see §2) |
| **Azure OpenAI GPT-4o** | Endpoint + API key | Azure Portal |
| **Git** | For repo cloning | `winget install Git.Git` |

All paths in this package are **relative to the `raven_reproduction/` folder**:

```
raven_reproduction/
├── raven/               pipeline source code (bundled — no separate install)
├── apks/                APK files for all 7 apps (bundled)
├── configs/             per-app YAML configs
├── static/              web UI (HTML/CSS/JS)
├── config.yaml          Azure OpenAI credentials + runtime settings
├── server.py            web UI server
├── run_ui.py            launcher
├── pyproject.toml       installable package definition
├── requirements.txt     Python dependencies
└── .raven_runs/         created at runtime — workspace for results
```

---

## 2. One-Time Installation

All commands in this section are run from inside the `raven_reproduction/` directory.

### 2a. Install Python dependencies

```powershell
cd raven_reproduction
pip install -e .
```

This installs the bundled `raven/` package and all Python dependencies, and registers the `raven` CLI command. Verify:

```powershell
raven --help
# Expected output:
# usage: raven [-h] {run,ui,doctor,install-tools} ...
```

If you prefer a virtual environment (recommended for isolation):

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\raven --help
```

### 2b. Install Joern (if not already installed)

Joern generates Code Property Graphs for the HDG. Install it via the bundled CLI tool:

```powershell
raven install-tools joern --target C:/Tools/joern
```

After installation, confirm:

```
C:\Tools\joern\joern-cli\joern-parse.bat --help
C:\Tools\joern\joern-cli\joern-export.bat --help
```

Then update `config.yaml` (or `configs/*.yaml`) to point to the installed paths:

```yaml
tools:
  joern_parse:  "C:/Tools/joern/joern-cli/joern-parse.bat"
  joern_export: "C:/Tools/joern/joern-cli/joern-export.bat"
```

### 2c. Install ffmpeg (if not already on PATH)

```powershell
winget install Gyan.FFmpeg
# OR add the existing ffmpeg binary to PATH
```

### 2d. Set your Azure OpenAI credentials

Edit `config.yaml`:

```yaml
azure_openai:
  endpoint: "https://<your-resource>.openai.azure.com/"
  api_key:  "<your-api-key>"
```

The `config.yaml` shipped with this package contains the author's credentials for reviewer convenience.

### 2e. Run the doctor check

This checks that all external tools resolve correctly and that the Azure OpenAI credentials are set:

```powershell
cd raven_reproduction
raven doctor --config config.yaml
```

Expected output (all OK):

```
OK adb: Android Debug Bridge version 1.0.41
OK java: openjdk version "11.0.x"
OK ffmpeg: ffmpeg version 7.x.x
OK joern-parse: ...
OK joern-export: ...
OK azure endpoint configured
OK azure api key configured
```

Fix any FAIL lines before proceeding.

---

## 3. Emulator Setup

RAVEN uses the emulator to replay the bug's UI actions and capture logcat output. All APKs for the 7 evaluation apps are bundled in `apks/`.

### 3a. Create the Pixel29 AVD (one time, already done on this machine)

```powershell
# Check existing AVDs
& "$env:LOCALAPPDATA\Android\Sdk\emulator\emulator.exe" -list-avds
# Expected: Pixel29
```

If `Pixel29` is missing, recreate it:

```powershell
& "$env:LOCALAPPDATA\Android\Sdk\tools\bin\avdmanager.bat" create avd `
    --name "Pixel29" `
    --package "system-images;android-29;google_apis_playstore;x86" `
    --device "pixel" `
    --force
```

> **Important:** The system image must be `google_apis_playstore;x86` (not `x86_64`). This ensures compatibility with the downloaded APKs and allows Google Play Services. Install the image via Android Studio SDK Manager → SDK Platforms → Android 10 (API 29) → Google Play Intel x86 Atom System Image.

### 3b. Start the emulator (required before every RAVEN run)

```powershell
& "$env:LOCALAPPDATA\Android\Sdk\emulator\emulator.exe" `
    -avd Pixel29 `
    -no-snapshot-load `
    -port 5554
```

Wait approximately 60 seconds for full boot. Confirm in a second terminal:

```powershell
& "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe" devices
# Must show:
# emulator-5554   device
```

Do not proceed until `device` (not `offline`) appears.

### 3c. Install all APKs on the emulator (first session only)

With the emulator running, install all 7 APKs via ADB:

```powershell
# Run from raven_reproduction/
adb install -g apks\simplenote\simplenote-2.14.apk
adb install -g apks\ankidroid\variant-abi-AnkiDroid-2.24.0-x86.apk
adb install -g apks\wordpress\wpandroid-26.8.apk
adb install -g apks\amaze\app-fdroid-release.apk
adb install -g apks\k9mail\k9mail-20.0.apk
adb install -g apks\newpipe\NewPipe_v0.28.8.apk
adb install -g apks\antennapod\de.danoeh.antennapod-3.11.4.apk
```

The `-g` flag grants all runtime permissions automatically. Each `adb install` prints `Success` when done.

Once installed, subsequent runs pass `--skip-install` to avoid reinstalling on every run.

---

## 4. Configuration File

The config file is `raven_reproduction/config.yaml`. It is pre-filled with working Azure credentials for reviewer convenience. To adapt for a new Azure deployment, edit these fields:

```yaml
azure_openai:
  endpoint: "https://<your-resource>.openai.azure.com/"
  api_key:  "<your-api-key>"
  api_version: "2025-04-01-preview"
  deployment:  "gpt-4o"

github:
  token: ""   # optional — add a PAT to avoid 60 req/hr rate limiting on public repos

android:
  adb_path:        "adb"            # full path if adb is not on PATH
  emulator_serial: "emulator-5554"
  # package_name and launch_activity are overridden per-run by the UI/CLI preset

tools:
  ffmpeg_path:  "ffmpeg"
  joern_parse:  "joern-parse"       # or full path: C:/Tools/joern/joern-cli/joern-parse.bat
  joern_export: "joern-export"

runtime:
  workspace_dir: ".raven_runs"      # relative to config.yaml → raven_reproduction/.raven_runs/
  max_replay_attempts: 3
  static_top_k: 30
```

Per-app configs (pre-filled package_name, launch_activity, apk_path) are in `configs/`.

Per-app configs with APK paths already filled in are in `raven_reproduction/configs/`:

```
configs/simplenote.yaml
configs/ankidroid.yaml
configs/wordpress.yaml
configs/amaze.yaml
configs/k9mail.yaml
configs/newpipe.yaml
configs/antennapod.yaml
```

---

## 5. Running RAVEN — Single Bug (CLI)

All CLI commands below are run from inside the `raven_reproduction/` directory.

### Step 1: Ensure the emulator is running

```powershell
adb devices
# emulator-5554   device
```

### Step 2: Run RAVEN on a GitHub issue

**Using a per-app config (recommended — package/activity/APK pre-filled):**

```powershell
cd raven_reproduction

raven run `
  --config    configs\simplenote.yaml `
  --issue-url https://github.com/Automattic/simplenote-android/issues/1765 `
  --skip-install
```

**Full syntax (custom app, all flags explicit):**

```powershell
raven run `
  --config          config.yaml `
  --repo-url        https://github.com/Automattic/simplenote-android `
  --issue-url       https://github.com/Automattic/simplenote-android/issues/1765 `
  --package-name    com.automattic.simplenote `
  --launch-activity com.automattic.simplenote/.Simplenote `
  --skip-install
```

**With a bug video (improves Agent 1 accuracy):**

```powershell
raven run `
  --config    configs\simplenote.yaml `
  --issue-url https://github.com/Automattic/simplenote-android/issues/1765 `
  --media     C:\path\to\bug_video.mp4 `
  --skip-install
```

### CLI flags reference

| Flag | Default | Description |
|---|---|---|
| `--config` | `config.yaml` | Path to the RAVEN YAML config |
| `--repo-url` | *(required)* | Git URL of the Android app repository |
| `--issue-url` | *(required)* | Full GitHub issue URL |
| `--apk` | from config | Explicit APK path to install before the run |
| `--skip-install` | false | Skip APK installation (app already on emulator) |
| `--media` | none | Local bug video (.mp4) or screenshot (.png/.jpg) |
| `--package-name` | from config | Android package name (e.g. `com.automattic.simplenote`) |
| `--launch-activity` | from config | ADB activity string (e.g. `com.app/.MainActivity`) |
| `--emulator-serial` | from config | Override ADB serial (default: `emulator-5554`) |

### Expected terminal output

```
[2026-07-06 14:32:01] INTAKE    running  Fetching GitHub issue ...
[2026-07-06 14:32:02] INTAKE    complete Loaded issue #1765: Dark mode inconsistency
[2026-07-06 14:32:02] REPO      running  Cloning/updating repository ...
[2026-07-06 14:32:45] REPO      complete Checked out pre-fix ref v2.18
[2026-07-06 14:32:45] EMULATOR  skipped  APK install skipped — using app already on emulator
[2026-07-06 14:32:45] AGENT1    running  Generating executable Android action sequence
[2026-07-06 14:32:58] AGENT1    complete Generated 9 action(s)
[2026-07-06 14:32:58] AGENT2    running  Replay attempt 1 of 3
[2026-07-06 14:33:41] AGENT2    complete Replay verified=True; attempts=1
[2026-07-06 14:33:41] AGENT3    running  Building heterogeneous Android data-flow graph
[2026-07-06 14:35:10] AGENT3    complete HDG written to .raven_runs/...
[2026-07-06 14:35:10] AGENT4    running  Running root-cause localization
[2026-07-06 14:36:28] AGENT4    complete Ranked 3 candidate(s)
[2026-07-06 14:36:28] DONE      complete Report written to .raven_runs/Automattic_simplenote_android_1765/report.md
RAVEN run complete: .raven_runs/Automattic_simplenote_android_1765
```

If Agent 2 fails all 3 replay attempts, RAVEN automatically falls back to TF-IDF static file selection and continues with Agents 3 and 4.

---

## 6. Running RAVEN — Web UI

The web UI provides a browser-based interface for submitting single-bug runs, watching pipeline progress in real time, and browsing all completed workspace results.

### Step 1: Start the emulator (see §3b)

### Step 2: Launch the UI server

```powershell
cd raven_reproduction
python run_ui.py --open
```

`--open` automatically opens your browser to `http://127.0.0.1:8765`. To use a different port:

```powershell
python run_ui.py --port 9000 --open
```

### Step 3: Submit a run

1. **Select an App preset** from the dropdown — this auto-fills the Repo URL and APK path.
2. **Paste the GitHub issue URL** in the "GitHub issue URL" field.
3. **Tick "Skip install"** if the app is already installed on the emulator (normal case after first `--setup-emulator` run).
4. Click **Run RAVEN**.

### UI panels

**Pipeline tab** (visible during a run):

Ten stage cards show status in real time:

```
Fetch Issue → Clone Repo → Frames → Install APK
→ Action Seq [A1] → Reproduce [A2] → [TF-IDF Fallback]
→ HDG Build [A3] → Root Cause [A4] → Complete
```

- Blue pulsing = currently running
- Green = completed successfully
- Yellow = warning (e.g. fallback triggered)
- Red = failed

The event log below the cards streams every pipeline message as it occurs.

**Results tab** (switches automatically when the run completes):

- **Fault Candidates** — ranked list with file path, method name, suspicion score bar (0–100%), reasoning, and code snippet.
- **Covered Files** — lists which files fed the HDG, with a badge indicating whether runtime logcat coverage or TF-IDF static fallback was used.

**History tab** — browse all completed runs from the workspace directory, showing top candidate, score, and coverage mode per run.

### Quick-pick panel (bottom of sidebar)

14 of the 50 evaluation bugs are pre-listed. Clicking one fills the issue URL and selects the correct app preset automatically.

---

## 7. Running the Full 50-Bug Evaluation

The batch evaluation covers all 50 bugs across 7 apps. Each bug is run via the same `raven run` CLI path used in §5.

### Prerequisites

- Emulator running with all 7 apps installed (§3b–§3c).
- Azure OpenAI credentials in `config.yaml`.

### Run all 50 bugs with a shell loop

```powershell
# Run from raven_reproduction/
# Example: iterate a list of issue URLs and configs
# (see the dataset table in §10 for the full bug list)

raven run --config configs\simplenote.yaml --issue-url https://github.com/Automattic/simplenote-android/issues/1727 --skip-install
raven run --config configs\simplenote.yaml --issue-url https://github.com/Automattic/simplenote-android/issues/1736 --skip-install
# ... repeat for all 50 bugs
```

RAVEN skips a bug automatically if `localization.json` already exists in `.raven_runs/`. Re-run with a fresh workspace to force re-evaluation.

### Expected per-bug duration

| Phase | Time |
|---|---|
| GitHub issue fetch + repo clone | 1–3 min |
| Agent 1 (action sequence) | ~30 s |
| Agent 2 (emulator replay, up to 3×) | 1–5 min |
| Agent 3 (HDG generation) | 3–8 min |
| Agent 4 (root-cause localization) | 1–3 min |
| **Total per bug** | **~8–18 min** |

### Where results are written

```
raven_reproduction/.raven_runs/
└── Automattic_simplenote_android_1765/
    ├── issue.json                  GitHub issue details
    ├── actions.json                LLM-generated action sequence
    ├── replay/
    │   └── attempt_1/
    │       ├── logcat.txt          ADB logcat from replay
    │       ├── final.png           Emulator screenshot at replay end
    │       └── ui_001.xml          UI hierarchy snapshots
    ├── static_fallback_files.json  (only if all 3 replays failed)
    ├── hdg.json                    Heterogeneous Data-flow Graph
    ├── hdg.sqlite                  HDG in queryable SQLite form
    ├── localization.json           Ranked fault candidates
    └── report.md                   Human-readable summary
```

---

## 8. Output Files Explained

### `localization.json`

The primary output of RAVEN. Contains ranked fault candidates from Agent 4.

```json
{
  "candidates": [
    {
      "rank": 1,
      "file": "app/src/main/java/com/automattic/simplenote/NoteEditorFragment.kt",
      "symbol": "NoteEditorFragment.onDarkModeChanged",
      "line": 312,
      "snippet": "binding.editorContainer.setBackgroundColor(Color.WHITE)",
      "suspicion": 0.95,
      "reasoning": "The background is hardcoded to WHITE regardless of the current theme ..."
    }
  ]
}
```

- **`suspicion`** — confidence score from 0.0 to 1.0 (higher = more likely root cause).
- **`snippet`** — the exact code statement identified as the fault.
- **`reasoning`** — the ReAct-style step-by-step rationale Agent 4 used.

### `hdg.json` / `hdg.sqlite`

The Heterogeneous Data-flow Graph built by Agent 3. Nodes represent UI elements, callbacks, code statements, and data flows. Edges encode relationships (`TRIGGERS`, `CONTROLS`, `USES`, `UPDATES`, etc.). The SQLite file allows graph traversal queries.

### `static_fallback_files.json`

Present only when all 3 emulator replay attempts fail. Contains the top-30 files selected by TF-IDF cosine similarity between the issue text and source file content. Runs that use this file have no runtime coverage; the HDG and localization are based on text similarity only.

### `report.md`

Human-readable summary including issue URL, checked-out git ref, reproduction result, covered files list, and the ranked fault candidates.

---

## 9. Pipeline Architecture

```
GitHub Issue URL
      │
      ▼
┌──────────────────────────────────────────────┐
│  Intake                                      │
│  • Fetch issue text + media from GitHub API  │
│  • Clone repo, checkout pre-fix commit       │
│  • Extract N frames from bug video (ffmpeg)  │
└─────────────────────┬────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────┐
│  Agent #1 — Action Sequence Generation       │
│  • Input:  issue text + video frames         │
│  • GPT-4o predicts tap/swipe/input/keyevent  │
│  • Output: actions.json                      │
└─────────────────────┬────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────┐
│  Agent #2 — Bug Reproduction (Emulator)      │
│  • Replay action sequence on emulator-5554   │
│  • Capture ADB logcat + UI hierarchy         │
│  • Screenshot final emulator state           │
│  • GPT-4o verifies: screenshot ≈ issue state │
│  • If NO → repair sequence → retry (max 3×)  │
│                                              │
│  All 3 attempts fail ──► STATIC FALLBACK     │
│   TF-IDF top-30 files by issue text match    │
└─────────────────────┬────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────┐
│  Agent #3 — HDG Generation                   │
│  • Joern: CPG for XML + Java files           │
│  • tree-sitter: CPG for Kotlin files         │
│  • Merges CPGs into Heterogeneous DFG        │
│  • Node types: UIACTION, WIDGET, KOTLINSTMT  │
│    JAVASTMT, FRAMEWORKCALLBACK, …            │
│  • Edge types: TRIGGERS, CONTROLS, USES, …  │
│  • Output: hdg.json + hdg.sqlite             │
└─────────────────────┬────────────────────────┘
                      │
                      ▼
┌──────────────────────────────────────────────┐
│  Agent #4 — Root Cause Localization (ReAct)  │
│  • Input: HDG + issue text + logcat          │
│  • Identifies GUI trigger node from logcat   │
│  • Traces backward through HDG edges         │
│  • ReAct: Thought → Action → Observation     │
│  • Output: localization.json (ranked list)   │
└─────────────────────┬────────────────────────┘
                      │
                      ▼
              report.md + localization.json
```

---

## 10. Dataset Apps and APK Notes

### 50-Bug Evaluation Dataset (this package)

| # | App | Bug count | Issues |
|---|---|---|---|
| 1–5 | Simplenote | 5 | 1727, 1736, 1746, 1751, 1765 |
| 6–12 | AnkiDroid | 7 | 17967, 17982, 18554, 19426, 21153, 21155, 21242 |
| 13–16 | WordPress | 4 | 22878, 22879, 22905, 23014 |
| 17–24 | Amaze File Manager | 8 | 4391, 4560, 4562, 4564, 4580, 4581, 4585, 4619 |
| 25–34 | K-9 Mail | 10 | 11076, 11115, 11127, 11128, 11130, 11134, 11137, 11156, 11162, 11164 |
| 35–42 | NewPipe | 8 | 13414, 13436, 13439, 13506, 13548, 13549, 13592, 13612 |
| 43–50 | AntennaPod | 8 | 8284, 8424, 8425, 8446, 8447, 8454, 8528, 8530 |

### Installed APKs (on Pixel29, API 29)

| App | APK file | Size |
|---|---|---|
| Simplenote | `simplenote-2.14.apk` | 5 MB |
| AnkiDroid | `variant-abi-AnkiDroid-2.24.0-x86.apk` | 38 MB |
| WordPress | `wpandroid-26.8.apk` | 321 MB |
| Amaze | `app-fdroid-release.apk` | 11 MB |
| K-9 Mail | `k9mail-20.0.apk` | 10 MB |
| NewPipe | `NewPipe_v0.28.8.apk` | 10 MB |
| AntennaPod | `de.danoeh.antennapod-3.11.4.apk` | 11 MB |

### Emulator replay expectations per app

| App | Expected replay outcome | Reason |
|---|---|---|
| Simplenote | **Likely succeeds** | Works offline; no mandatory login for note-taking |
| AnkiDroid | **Likely succeeds** | Works offline; uses x86-specific APK (important) |
| Amaze | **Likely succeeds** | File manager; no login; straightforward UI navigation |
| NewPipe | **Likely succeeds** | YouTube frontend; no login; navigation is deterministic |
| AntennaPod | **Likely succeeds** | Podcast player; can be used without account |
| K-9 Mail | **May fall to TF-IDF** | Requires email account setup before most bug flows are reachable |
| WordPress | **May fall to TF-IDF** | Requires WordPress.com login; app shows login screen on cold start |

When replay falls to TF-IDF, RAVEN still produces `localization.json` via static analysis — quality is lower but the pipeline completes.

### Original 399-bug dataset (for reference)

The full research dataset at [Android-Functional-bugs-study/home](https://github.com/Android-Functional-bugs-study/home/tree/main/Dataset) has 399 bugs across 8 apps (adding Firefox Focus). Key limitations for that dataset:

- **Firefox Focus** — GitHub releases only ship ARM APKs (`Focus-arm.apk`), which are incompatible with x86 emulators. Emulator replay will always fail; static TF-IDF fallback runs instead.
- **WordPress (old versions 9.5–17.8)** — Many releases in that range have no APK attached to GitHub releases. Additionally, building from source fails with modern Gradle/JDK (deprecated dependencies). Use the closest available APK version or static fallback.
- **Simplenote (old versions 1.5.7–2.18)** — GitHub releases carry no APK attachments; APKs were distributed via Google Play only. Obtain from third-party mirrors or use static fallback.
- **K-9 Mail (old versions 5.x)** — Repository migrated from `k9mail/k-9` to `thunderbird/thunderbird-android`. RAVEN clones from the new URL but the old git history is preserved; pre-fix checkouts work correctly.

---

## 11. Troubleshooting

### Emulator not detected

```
WARNING: emulator-5554 is not running — RAVEN will use static fallback for all bugs.
```

**Fix:** Start the emulator first and wait for full boot (§3b). Then re-run the affected bugs — RAVEN will overwrite the static-only results because the emulator is now reachable.

### APK install fails — `INSTALL_FAILED_NO_MATCHING_ABIS`

The wrong APK architecture is installed on an x86 emulator.

**Fix for AnkiDroid:** Ensure `variant-abi-AnkiDroid-X.X-x86.apk` is used, not `x86_64`.

```powershell
ls apks\ankidroid\
# Must show: variant-abi-AnkiDroid-2.24.0-x86.apk  (NOT x86_64)
```

The correct x86 APK is already bundled in `apks/ankidroid/`.

### APK install fails — `INSTALL_FAILED_OLDER_SDK`

The emulator API level is too low for the APK.

**Fix:** Ensure the emulator is running `Pixel29` (API 29), not an older AVD:

```powershell
& "$env:LOCALAPPDATA\Android\Sdk\emulator\emulator.exe" -list-avds
# Must include: Pixel29
```

### ADB install times out on a small APK

Usually a sign the emulator is still booting or is in a bad state.

**Fix:** Wait for the emulator to fully boot (check `adb shell getprop sys.boot_completed` returns `1`), then retry. If it persists, cold-boot the emulator with `-no-snapshot-load`.

### `UnicodeEncodeError` on Windows terminal

```
'charmap' codec can't encode character '→'
```

**Fix:** Set the terminal to UTF-8:

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
```

Or run inside Windows Terminal (not the legacy cmd prompt) which uses UTF-8 by default.

### Agent 3 produces an empty HDG

Joern failed silently during CPG generation.

**Fix:** Confirm Joern is runnable:

```powershell
C:\Tools\joern\joern-cli\joern-parse.bat --help
```

Check Java is installed:

```powershell
java -version
# Must show Java 11 or higher
```

### All bugs show `static_fallback_files.json` (no emulator coverage)

This means the emulator was not running when RAVEN executed. Every run used TF-IDF fallback.

**Fix:** Start the emulator (§3b), then re-run the affected bugs from scratch — delete their subdirectory from `.raven_runs/` first so RAVEN doesn't skip them:

```powershell
# Start emulator
& "$env:LOCALAPPDATA\Android\Sdk\emulator\emulator.exe" -avd Pixel29 -no-snapshot-load -port 5554
# Wait ~60s, then re-run each bug
```

### Checking whether a specific run used emulator replay or fallback

```powershell
Get-ChildItem ".raven_runs" -Recurse -Filter "static_fallback_files.json" |
    ForEach-Object { $_.DirectoryName.Split('\')[-1] }
# Each line printed = a run that used TF-IDF fallback (no emulator coverage)
```

---

*RAVEN — ICSE 2027 Submission*

