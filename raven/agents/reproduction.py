from __future__ import annotations

import json
import os
import time
from pathlib import Path

from raven.android.adb import ADBController, files_from_logcat
from raven.llm.azure_client import AzureGPT4OClient
from raven.models import ActionSequence, ReplayResult


VERIFY_SYSTEM = """You are Agent #2 verifier in RAVEN.
Compare the emulator final screenshot with the target issue/video final state.
Return JSON: {"verified": boolean, "reason": string}."""


class BugReproductionAgent:
    def __init__(
        self,
        adb: ADBController,
        llm: AzureGPT4OClient,
        *,
        max_logcat_bytes: int = 2_000_000,
        max_ui_hierarchy_snapshots: int = 8,
        compact_json: bool = True,
    ):
        self.adb = adb
        self.llm = llm
        self.max_logcat_bytes = max_logcat_bytes
        self.max_ui_hierarchy_snapshots = max_ui_hierarchy_snapshots
        self.compact_json = compact_json

    def replay_once(
        self,
        *,
        sequence: ActionSequence,
        repo_path: Path,
        run_dir: Path,
        attempt: int,
        target_frames: list[Path],
    ) -> ReplayResult:
        replay_dir = run_dir / "replay" / f"attempt_{attempt}"
        replay_dir.mkdir(parents=True, exist_ok=True)
        logcat_path = replay_dir / "logcat.txt"
        screenshot_path = replay_dir / "final.png"
        hierarchy_paths: list[Path] = []
        history: list[dict] = []
        proc = self.adb.start_logcat(logcat_path)
        try:
            capture_every = max(1, len(sequence.actions) // max(1, self.max_ui_hierarchy_snapshots - 1))
            for index, action in enumerate(sequence.actions, start=1):
                entry = self.adb.execute(action)
                should_capture = index == 1 or index == len(sequence.actions) or index % capture_every == 0
                if should_capture and len(hierarchy_paths) < self.max_ui_hierarchy_snapshots:
                    try:
                        hierarchy = self.adb.dump_ui_hierarchy(replay_dir / f"ui_{index:03d}.xml")
                        hierarchy_paths.append(hierarchy)
                        entry["ui_hierarchy"] = str(hierarchy)
                    except Exception as exc:
                        entry["ui_hierarchy_error"] = str(exc)
                history.append(entry)
                time.sleep(0.25)
            self.adb.screenshot(screenshot_path)
            if len(hierarchy_paths) < self.max_ui_hierarchy_snapshots:
                hierarchy_paths.append(self.adb.dump_ui_hierarchy(replay_dir / "ui_final.xml"))
        finally:
            self.adb.stop_process(proc)

        _trim_file_tail(logcat_path, self.max_logcat_bytes)
        verification = self._verify(sequence, screenshot_path, target_frames)
        covered_files = files_from_logcat(logcat_path, repo_path)
        result = ReplayResult(
            verified=bool(verification.get("verified")),
            attempts=attempt,
            logcat_path=logcat_path,
            screenshot_path=screenshot_path,
            ui_hierarchy_paths=hierarchy_paths,
            covered_files=covered_files,
            verification_reason=str(verification.get("reason", "")),
            action_history=history,
        )
        indent = None if self.compact_json else 2
        (replay_dir / "result.json").write_text(result.model_dump_json(indent=indent), encoding="utf-8")
        return result

    def _verify(self, sequence: ActionSequence, screenshot: Path, target_frames: list[Path]) -> dict:
        prompt = f"""Target final state:
{sequence.target_final_state}

The first image is the emulator final screenshot. Remaining images are reference frames.
Decide whether the bug was reproduced and the final UI state matches the issue evidence.
"""
        images = [screenshot, *target_frames[-2:]]
        return self.llm.complete_json(VERIFY_SYSTEM, prompt, images=images)


def _trim_file_tail(path: Path, max_bytes: int) -> None:
    if max_bytes <= 0 or not path.exists() or path.stat().st_size <= max_bytes:
        return
    with path.open("rb") as handle:
        handle.seek(-max_bytes, os.SEEK_END)
        data = handle.read()
    marker = b"\n[RAVEN] logcat truncated to last configured bytes\n"
    path.write_bytes(marker + data)
