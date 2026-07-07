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


UI_DIR = Path(__file__).with_name("static")


@dataclass
class RunState:
    id: str
    events: list[dict] = field(default_factory=list)
    terminal: list[str] = field(default_factory=list)
    status: str = "idle"
    result: dict | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


class RavenUIState:
    def __init__(self) -> None:
        self.runs: dict[str, RunState] = {}
        self.lock = threading.Lock()

    def create_run(self) -> RunState:
        run = RunState(id=uuid.uuid4().hex[:12], status="queued")
        with self.lock:
            self.runs[run.id] = run
        self.event(run.id, "system", "queued", "Run queued")
        return run

    def event(self, run_id: str, agent: str, status: str, message: str) -> None:
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "agent": agent,
            "status": status,
            "message": message,
        }
        with self.lock:
            run = self.runs.get(run_id)
            if not run:
                return
            run.events.append(payload)
            run.terminal.append(f"[{payload['ts']}] {agent.upper()} {status}: {message}")
            run.status = status if agent == "done" else run.status

    def snapshot(self, run_id: str) -> RunState | None:
        with self.lock:
            return self.runs.get(run_id)


STATE = RavenUIState()


class RavenUIHandler(BaseHTTPRequestHandler):
    server_version = "RAVENUI/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_file(UI_DIR / "index.html")
            return
        if parsed.path == "/events":
            self._serve_events(parse_qs(parsed.query).get("run_id", [""])[0])
            return
        if parsed.path == "/api/runs":
            self._json({"runs": list(STATE.runs)})
            return
        if parsed.path.startswith("/api/run/"):
            run_id = parsed.path.rsplit("/", 1)[-1]
            run = STATE.snapshot(run_id)
            if not run:
                self._json({"error": "run not found"}, status=404)
                return
            self._json(
                {
                    "id": run.id,
                    "status": run.status,
                    "events": run.events[-100:],
                    "terminal": run.terminal[-200:],
                    "result": run.result,
                }
            )
            return
        path = (UI_DIR / parsed.path.lstrip("/")).resolve()
        if UI_DIR.resolve() in path.parents and path.exists():
            self._serve_file(path)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/run":
            self._start_run()
            return
        if parsed.path == "/api/terminal":
            self._terminal_command()
            return
        # /api/run/<run_id>/stop
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "run"] and parts[3] == "stop":
            self._stop_run(parts[2])
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def _json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, path: Path) -> None:
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_events(self, run_id: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        sent = 0
        while True:
            run = STATE.snapshot(run_id)
            if not run:
                break
            items = run.events[sent:]
            for event in items:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                self.wfile.flush()
            sent += len(items)
            if run.status in {"complete", "failed"} and sent >= len(run.events):
                break
            time.sleep(0.4)

    def _start_run(self) -> None:
        payload = self._read_json()
        run = STATE.create_run()
        thread = threading.Thread(target=_run_pipeline, args=(run.id, payload), daemon=True)
        thread.start()
        self._json({"run_id": run.id})

    def _stop_run(self, run_id: str) -> None:
        run = STATE.snapshot(run_id)
        if not run:
            self._json({"error": "run not found"}, status=404)
            return
        run.cancel_event.set()
        STATE.event(run_id, "system", "cancelled", "Stop requested by user")
        with STATE.lock:
            if STATE.runs[run_id].status not in {"complete", "failed"}:
                STATE.runs[run_id].status = "cancelled"
        self._json({"cancelled": True})

    def _terminal_command(self) -> None:
        payload = self._read_json()
        run_id = payload.get("run_id") or ""
        command = payload.get("command") or ""
        if not run_id or not command:
            self._json({"error": "run_id and command are required"}, status=400)
            return
        threading.Thread(target=_run_terminal_command, args=(run_id, command), daemon=True).start()
        self._json({"accepted": True})


def _run_pipeline(run_id: str, payload: dict) -> None:
    try:
        STATE.event(run_id, "system", "running", "Starting RAVEN pipeline")
        config_path = Path(payload["config_path"]).resolve()
        config = RavenConfig.load(config_path)
        emulator = payload.get("emulator_serial")
        if emulator:
            config.android.emulator_serial = emulator
        run = STATE.snapshot(run_id)
        cancel_event = run.cancel_event if run else None
        pipeline = RavenPipeline(config, progress=lambda a, s, m: STATE.event(run_id, a, s, m))
        result = pipeline.run(
            repo_url=payload["repo_url"],
            issue_url=payload["issue_url"],
            apk_path=Path(payload["apk_path"]).resolve(),
            media_path=Path(payload["media_path"]).resolve() if payload.get("media_path") else None,
            package_name=payload.get("package_name") or None,
            launch_activity=payload.get("launch_activity") or None,
            cancel_event=cancel_event,
        )
        with STATE.lock:
            run = STATE.runs[run_id]
            run.status = "complete"
            run.result = {
                "run_dir": str(result.run_dir),
                "report": str(result.report_path),
                "hdg": str(result.hdg_path),
                "localization": str(result.localization_path),
            }
        STATE.event(run_id, "done", "complete", f"Run complete: {result.report_path}")
    except Exception as exc:
        run = STATE.snapshot(run_id)
        is_cancelled = run and run.cancel_event.is_set()
        final_status = "cancelled" if is_cancelled else "failed"
        with STATE.lock:
            STATE.runs[run_id].status = final_status
        STATE.event(run_id, "system", final_status, "Run cancelled by user" if is_cancelled else str(exc))


def _run_terminal_command(run_id: str, command: str) -> None:
    STATE.event(run_id, "terminal", "running", f"$ {command}")
    try:
        proc = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            STATE.event(run_id, "terminal", "output", line.rstrip())
        code = proc.wait()
        STATE.event(run_id, "terminal", "complete", f"Command exited with code {code}")
    except Exception as exc:
        STATE.event(run_id, "terminal", "failed", str(exc))


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), RavenUIHandler)
    print(f"RAVEN UI running at http://{host}:{port}")
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the RAVEN local web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
