from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from raven.config import AndroidConfig
from raven.models import AndroidAction, AndroidActionType


class ADBController:
    def __init__(self, config: AndroidConfig):
        self.config = config

    def install_apk(self, apk_path: Path) -> None:
        self._run(["install", "-r", str(apk_path)], timeout=self.config.install_timeout_seconds)

    def execute(self, action: AndroidAction) -> dict[str, Any]:
        started = time.time()
        if action.type == AndroidActionType.tap:
            self._run(["shell", "input", "tap", str(action.x), str(action.y)])
        elif action.type == AndroidActionType.long_press:
            self._run(
                [
                    "shell",
                    "input",
                    "swipe",
                    str(action.x),
                    str(action.y),
                    str(action.x),
                    str(action.y),
                    str(action.duration_ms),
                ]
            )
        elif action.type == AndroidActionType.swipe:
            self._run(
                [
                    "shell",
                    "input",
                    "swipe",
                    str(action.x),
                    str(action.y),
                    str(action.x2),
                    str(action.y2),
                    str(action.duration_ms),
                ]
            )
        elif action.type == AndroidActionType.input_text:
            text = (action.text or "").replace(" ", "%s")
            self._run(["shell", "input", "text", text])
        elif action.type == AndroidActionType.keyevent:
            self._run(["shell", "input", "keyevent", str(action.keycode)])
        elif action.type == AndroidActionType.wait:
            time.sleep(max(action.duration_ms, 0) / 1000)
        elif action.type == AndroidActionType.launch_activity:
            activity = action.activity or self.config.launch_activity
            if not activity:
                raise ValueError("launch_activity action requires an activity")
            self._run(["shell", "am", "start", "-n", activity])
        elif action.type == AndroidActionType.shell:
            if not action.command:
                raise ValueError("shell action requires command")
            self._run(["shell", action.command])
        else:
            raise ValueError(f"Unsupported action type: {action.type}")
        return {
            "type": action.type.value,
            "action": action.model_dump(mode="json"),
            "elapsed_ms": int((time.time() - started) * 1000),
            "rationale": action.rationale,
        }

    def start_logcat(self, output_path: Path) -> subprocess.Popen[str]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        handle = output_path.open("w", encoding="utf-8")
        return subprocess.Popen(
            [self.config.adb_path, "-s", self.config.emulator_serial, "logcat", "-v", "threadtime"],
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def stop_process(self, proc: subprocess.Popen[str]) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def screenshot(self, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        raw = subprocess.run(
            [self.config.adb_path, "-s", self.config.emulator_serial, "exec-out", "screencap", "-p"],
            capture_output=True,
            check=True,
        ).stdout
        output_path.write_bytes(raw)
        return output_path

    def dump_ui_hierarchy(self, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        remote = "/sdcard/raven-window.xml"
        self._run(["shell", "uiautomator", "dump", remote], timeout=15)
        raw = subprocess.run(
            [self.config.adb_path, "-s", self.config.emulator_serial, "exec-out", "cat", remote],
            capture_output=True,
            check=True,
            timeout=15,
        ).stdout
        output_path.write_bytes(raw)
        return output_path

    def _run(self, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.config.adb_path, "-s", self.config.emulator_serial, *args],
            check=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )


def files_from_logcat(logcat_path: Path, repo_path: Path) -> list[Path]:
    if not logcat_path.exists():
        return []
    text = logcat_path.read_text(encoding="utf-8", errors="ignore")
    names = set(re.findall(r"\b([A-Z][A-Za-z0-9_]*(?:Activity|Fragment|ViewModel|Adapter|Service|Repository|Presenter)?)\b", text))
    package_classes = set(re.findall(r"\b(?:[a-z_]\w*\.)+([A-Z][A-Za-z0-9_]+)\b", text))
    names.update(package_classes)
    out: list[Path] = []
    for source in repo_path.rglob("*"):
        if source.suffix.lower() not in {".kt", ".java", ".xml"}:
            continue
        stem = source.stem
        if stem in names or any(name in stem for name in names):
            out.append(source)
    return sorted(set(out))
