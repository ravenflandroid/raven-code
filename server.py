"""
RAVEN Reproduction Package — UI Server
=======================================
Standalone HTTP server that exposes the RAVEN pipeline through a browser UI.
Imports from the raven package installed via `pip install -e ../model/`.

Start:
    python server.py                      # default http://127.0.0.1:8765
    python server.py --port 9000
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from raven.config import RavenConfig
from raven.pipeline import RavenPipeline

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
CONFIGS_DIR = Path(__file__).parent / "configs"
RAVEN_ROOT = Path(__file__).parent.parent          # Raven/
MODEL_DIR  = RAVEN_ROOT / "model"
APK_DIR    = RAVEN_ROOT / "dataset" / "apks"
WORKSPACE  = MODEL_DIR / ".raven_runs"

# ---------------------------------------------------------------------------
# App presets (mirrors APP_CONFIG in run_eval_new_bugs.py)
# ---------------------------------------------------------------------------

APP_PRESETS: dict[str, dict] = {
    "simplenote": {
        "label":           "Simplenote",
        "repo_url":        "https://github.com/Automattic/simplenote-android.git",
        "package_name":    "com.automattic.simplenote",
        "launch_activity": "com.automattic.simplenote/.Simplenote",
        "apk_subdir":      "simplenote",
    },
    "ankidroid": {
        "label":           "AnkiDroid",
        "repo_url":        "https://github.com/ankidroid/Anki-Android.git",
        "package_name":    "com.ichi2.anki",
        "launch_activity": "com.ichi2.anki/.IntentBasedController",
        "apk_subdir":      "ankidroid",
    },
    "wordpress": {
        "label":           "WordPress",
        "repo_url":        "https://github.com/wordpress-mobile/WordPress-Android.git",
        "package_name":    "org.wordpress.android",
        "launch_activity": "org.wordpress.android/.ui.WPLaunchActivity",
        "apk_subdir":      "wordpress",
    },
    "amaze": {
        "label":           "Amaze File Manager",
        "repo_url":        "https://github.com/TeamAmaze/AmazeFileManager.git",
        "package_name":    "com.amaze.filemanager",
        "launch_activity": "com.amaze.filemanager/.ui.activities.MainActivity",
        "apk_subdir":      "amaze",
    },
    "k9mail": {
        "label":           "K-9 Mail",
        "repo_url":        "https://github.com/thunderbird/thunderbird-android.git",
        "package_name":    "com.fsck.k9",
        "launch_activity": "com.fsck.k9/.ui.LauncherShortcuts",
        "apk_subdir":      "k9mail",
    },
    "newpipe": {
        "label":           "NewPipe",
        "repo_url":        "https://github.com/TeamNewPipe/NewPipe.git",
        "package_name":    "org.schabi.newpipe",
        "launch_activity": "org.schabi.newpipe/.MainActivity",
        "apk_subdir":      "newpipe",
    },
    "antennapod": {
        "label":           "AntennaPod",
        "repo_url":        "https://github.com/AntennaPod/AntennaPod.git",
        "package_name":    "de.danoeh.antennapod",
        "launch_activity": "de.danoeh.antennapod/.activity.MainActivity",
        "apk_subdir":      "antennapod",
    },
}


def _find_apk(apk_subdir: str) -> str | None:
    subdir = APK_DIR / apk_subdir
    if not subdir.is_dir():
        return None
    apks = sorted(subdir.glob("*.apk"))
    return str(apks[-1]) if apks else None


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

PIPELINE_STAGES = [
    ("intake",   "Fetch Issue"),
    ("repo",     "Clone Repo"),
    ("media",    "Extract Frames"),
    ("emulator", "Install APK"),
    ("agent1",   "Action Sequence"),
    ("agent2",   "Bug Reproduction"),
    ("fallback", "Static Fallback"),
    ("agent3",   "HDG Generation"),
    ("agent4",   "Root Cause"),
    ("done",     "Complete"),
]


@dataclass
class RunState:
    id: str
    issue_url: str = ""
    app_preset: str = ""
    events: list[dict] = field(default_factory=list)
    stage_status: dict[str, str] = field(default_factory=dict)
    status: str = "idle"
    result: dict | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    started_at: str = ""

    def __post_init__(self) -> None:
        self.stage_status = {s: "idle" for s, _ in PIPELINE_STAGES}
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()

    def create(self, issue_url: str, app_preset: str) -> RunState:
        run = RunState(id=uuid.uuid4().hex[:12], issue_url=issue_url, app_preset=app_preset, status="queued")
        with self._lock:
            self._runs[run.id] = run
        return run

    def event(self, run_id: str, agent: str, status: str, message: str) -> None:
        payload = {"ts": time.strftime("%H:%M:%S"), "agent": agent, "status": status, "message": message}
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return
            run.events.append(payload)
            if agent in run.stage_status:
                run.stage_status[agent] = status
            if agent == "done":
                run.status = status
            elif status == "failed":
                run.status = "failed"

    def get(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._runs.get(run_id)

    def all_ids(self) -> list[str]:
        with self._lock:
            return list(self._runs)


REGISTRY = RunRegistry()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class UIHandler(BaseHTTPRequestHandler):
    server_version = "RAVEN-UI/1.0"

    def log_message(self, fmt: str, *args: object) -> None:  # silence default logs
        return

    # ── routing ──────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        p = urlparse(self.path)
        path, qs = p.path, parse_qs(p.query)

        if path == "/":
            return self._file(STATIC_DIR / "index.html")
        if path.startswith("/static/"):
            f = (STATIC_DIR / path[len("/static/"):]).resolve()
            if STATIC_DIR.resolve() in f.parents and f.exists():
                return self._file(f)
            return self._err(404)
        if path == "/api/apps":
            return self._json(_api_apps())
        if path == "/api/runs":
            return self._json(_api_runs())
        if path.startswith("/api/run/"):
            run_id = path.rsplit("/", 1)[-1]
            run = REGISTRY.get(run_id)
            if not run:
                return self._err(404)
            return self._json(_run_to_dict(run))
        if path == "/events":
            return self._sse(qs.get("run_id", [""])[0])
        if path == "/api/workspace":
            return self._json(_api_workspace())
        return self._err(404)

    def do_POST(self) -> None:
        p = urlparse(self.path)
        path = p.path
        body = self._body()

        if path == "/api/run":
            return self._start_run(body)
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[1] == "run" and parts[3] == "stop":
            return self._stop_run(parts[2])
        return self._err(404)

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}

    def _json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code: int) -> None:
        self.send_error(code)

    def _file(self, path: Path) -> None:
        data = path.read_bytes()
        ct = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, run_id: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        sent = 0
        while True:
            run = REGISTRY.get(run_id)
            if not run:
                break
            for ev in run.events[sent:]:
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
                sent += 1
            if run.status in {"complete", "failed", "cancelled"} and sent >= len(run.events):
                break
            time.sleep(0.3)

    def _start_run(self, body: dict) -> None:
        issue_url  = (body.get("issue_url") or "").strip()
        app_preset = (body.get("app_preset") or "custom").strip()
        if not issue_url:
            return self._json({"error": "issue_url is required"}, 400)
        run = REGISTRY.create(issue_url=issue_url, app_preset=app_preset)
        threading.Thread(target=_exec_run, args=(run.id, body), daemon=True).start()
        self._json({"run_id": run.id})

    def _stop_run(self, run_id: str) -> None:
        run = REGISTRY.get(run_id)
        if not run:
            return self._json({"error": "not found"}, 404)
        run.cancel_event.set()
        REGISTRY.event(run_id, "system", "cancelled", "Cancelled by user")
        with REGISTRY._lock:
            if REGISTRY._runs[run_id].status not in {"complete", "failed"}:
                REGISTRY._runs[run_id].status = "cancelled"
        self._json({"cancelled": True})


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _api_apps() -> dict:
    out = {}
    for key, preset in APP_PRESETS.items():
        apk = _find_apk(preset["apk_subdir"])
        out[key] = {
            "label":           preset["label"],
            "repo_url":        preset["repo_url"],
            "package_name":    preset["package_name"],
            "launch_activity": preset["launch_activity"],
            "apk_path":        apk,
            "apk_found":       apk is not None,
        }
    return {"apps": out}


def _api_runs() -> dict:
    runs = []
    for rid in REGISTRY.all_ids():
        run = REGISTRY.get(rid)
        if run:
            runs.append({
                "id":         run.id,
                "issue_url":  run.issue_url,
                "app_preset": run.app_preset,
                "status":     run.status,
                "started_at": run.started_at,
            })
    return {"runs": runs}


def _api_workspace() -> dict:
    """Return list of completed RAVEN runs from the workspace directory."""
    runs = []
    if not WORKSPACE.exists():
        return {"runs": runs}
    for subdir in sorted(WORKSPACE.iterdir()):
        if not subdir.is_dir() or subdir.name == "repos":
            continue
        loc = subdir / "localization.json"
        issue_f = subdir / "issue.json"
        sf = subdir / "static_fallback_files.json"
        if not loc.exists():
            continue
        try:
            issue_data = json.loads(issue_f.read_text(encoding="utf-8")) if issue_f.exists() else {}
            loc_data   = json.loads(loc.read_text(encoding="utf-8"))
            candidates = loc_data.get("candidates", [])
            top = candidates[0] if candidates else {}
            runs.append({
                "slug":         subdir.name,
                "issue_title":  issue_data.get("title", ""),
                "issue_url":    issue_data.get("url", ""),
                "used_fallback": sf.exists(),
                "top_file":     top.get("file", ""),
                "top_score":    top.get("suspicion", 0),
                "n_candidates": len(candidates),
            })
        except Exception:
            pass
    return {"runs": runs}


def _run_to_dict(run: RunState) -> dict:
    result = None
    if run.result:
        result = dict(run.result)
    return {
        "id":           run.id,
        "issue_url":    run.issue_url,
        "app_preset":   run.app_preset,
        "status":       run.status,
        "started_at":   run.started_at,
        "stage_status": run.stage_status,
        "events":       run.events[-200:],
        "result":       result,
    }


# ---------------------------------------------------------------------------
# Pipeline execution
# ---------------------------------------------------------------------------

def _exec_run(run_id: str, payload: dict) -> None:
    def progress(agent: str, status: str, message: str) -> None:
        REGISTRY.event(run_id, agent, status, message)

    try:
        REGISTRY.event(run_id, "intake", "running", "Initialising pipeline ...")

        config_path = Path(payload.get("config_path") or MODEL_DIR / "config.yaml").resolve()
        config = RavenConfig.load(config_path)

        app_preset = payload.get("app_preset") or ""
        preset = APP_PRESETS.get(app_preset, {})

        repo_url        = payload.get("repo_url")        or preset.get("repo_url") or ""
        package_name    = payload.get("package_name")    or preset.get("package_name") or None
        launch_activity = payload.get("launch_activity") or preset.get("launch_activity") or None
        apk_path_str    = payload.get("apk_path")        or _find_apk(preset.get("apk_subdir", "")) or ""
        skip_install    = bool(payload.get("skip_install", False))
        issue_url       = payload["issue_url"]

        if payload.get("emulator_serial"):
            config.android.emulator_serial = payload["emulator_serial"]

        apk_path = Path(apk_path_str) if apk_path_str and not skip_install else None

        run = REGISTRY.get(run_id)
        pipeline = RavenPipeline(config, progress=progress)
        result = pipeline.run(
            repo_url=repo_url,
            issue_url=issue_url,
            apk_path=apk_path,
            skip_install=skip_install,
            media_path=Path(payload["media_path"]) if payload.get("media_path") else None,
            package_name=package_name,
            launch_activity=launch_activity,
            cancel_event=run.cancel_event if run else None,
        )
        with REGISTRY._lock:
            r = REGISTRY._runs[run_id]
            r.status = "complete"
            r.result = {
                "run_dir":          str(result.run_dir),
                "report_path":      str(result.report_path),
                "hdg_path":         str(result.hdg_path),
                "localization_path": str(result.localization_path),
                "checkout_ref":     result.checkout_ref,
                "reproduced":       result.replay.verified,
                "attempts":         result.replay.attempts,
                "candidates":       [c.model_dump() for c in
                                     _load_candidates(result.localization_path)],
                "covered_files":    [str(f) for f in result.replay.covered_files[:50]],
                "used_fallback":    (result.run_dir / "static_fallback_files.json").exists(),
            }
        REGISTRY.event(run_id, "done", "complete", f"Done — {len(result.replay.covered_files)} files covered")

    except Exception as exc:
        run = REGISTRY.get(run_id)
        cancelled = run and run.cancel_event.is_set()
        final = "cancelled" if cancelled else "failed"
        with REGISTRY._lock:
            if run_id in REGISTRY._runs:
                REGISTRY._runs[run_id].status = final
        REGISTRY.event(run_id, "done", final, "Cancelled by user" if cancelled else str(exc))


def _load_candidates(path: Path) -> list:
    try:
        from raven.models import LocalizationCandidate
        data = json.loads(path.read_text(encoding="utf-8"))
        return [LocalizationCandidate.model_validate(c) for c in data.get("candidates", [])]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAVEN Reproduction UI Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open browser automatically")
    args = parser.parse_args(argv)

    url = f"http://{args.host}:{args.port}"
    print(f"RAVEN UI  ->  {url}")
    print(f"Workspace ->  {WORKSPACE}")
    print(f"APKs      ->  {APK_DIR}")
    print("Press Ctrl+C to stop.\n")

    if args.open:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    server = ThreadingHTTPServer((args.host, args.port), UIHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
