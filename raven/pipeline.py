from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Callable

from raven.agents.action_sequence import ActionSequenceAgent
from raven.agents.hdg_generation import HDGGenerationAgent
from raven.agents.localization import RootCauseLocalizationAgent
from raven.agents.reproduction import BugReproductionAgent
from raven.android.adb import ADBController
from raven.config import RavenConfig
from raven.llm.azure_client import AzureGPT4OClient
from raven.media import extract_frames
from raven.models import ActionSequence, RavenRunResult, ReplayResult
from raven.repo.github_client import GitHubClient, parse_issue_url
from raven.repo.manager import RepositoryManager
from raven.run_logger import RavenRunLogger, log_filename
from raven.static_similarity import rank_similar_files

ProgressCallback = Callable[[str, str, str], None]


class RavenPipeline:
    def __init__(self, config: RavenConfig, progress: ProgressCallback | None = None):
        self.config = config
        self.progress = progress or (lambda agent, status, message: None)
        self.run_logger: RavenRunLogger | None = None
        self.llm = AzureGPT4OClient(config.azure_openai)
        self.github = GitHubClient(config.github.token)
        self.repo_manager = RepositoryManager(config.runtime.workspace_dir)
        self.adb = ADBController(config.android)

    def run(
        self,
        *,
        repo_url: str,
        issue_url: str,
        apk_path: Path | None = None,
        skip_install: bool = False,
        media_path: Path | None = None,
        package_name: str | None = None,
        launch_activity: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RavenRunResult:
        issue_ref = parse_issue_url(issue_url)
        run_dir = self.config.runtime.workspace_dir / issue_ref.slug
        run_dir.mkdir(parents=True, exist_ok=True)
        self.run_logger = RavenRunLogger(
            run_dir / log_filename(issue_ref.repo, issue_ref.number),
            app_name=issue_ref.repo,
            issue_id=issue_ref.number,
            issue_url=issue_url,
            repo_url=repo_url,
            compact_json=self.config.runtime.compact_json,
        )
        self.llm.log_callback = self.run_logger.llm_call
        try:
            result = self._run_impl(
                repo_url=repo_url,
                issue_url=issue_url,
                apk_path=apk_path,
                skip_install=skip_install,
                media_path=media_path,
                package_name=package_name,
                launch_activity=launch_activity,
                cancel_event=cancel_event,
            )
            self.run_logger.finalize(
                "complete",
                {
                    "run_dir": result.run_dir,
                    "repo_path": result.repo_path,
                    "checkout_ref": result.checkout_ref,
                    "issue": result.issue,
                    "action_sequence": result.action_sequence,
                    "replay": result.replay,
                    "hdg_path": result.hdg_path,
                    "hdg_sqlite_path": result.hdg_path.with_suffix(".sqlite"),
                    "localization_path": result.localization_path,
                    "report_path": result.report_path,
                    "localization": _load_json_if_exists(result.localization_path),
                },
            )
            return result
        except Exception as exc:
            self._progress("system", "failed", str(exc))
            if self.run_logger:
                self.run_logger.finalize("failed", error=str(exc))
            raise

    def _check_cancelled(self, cancel_event: threading.Event | None) -> None:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Run cancelled by user")

    def _run_impl(
        self,
        *,
        repo_url: str,
        issue_url: str,
        apk_path: Path | None = None,
        skip_install: bool = False,
        media_path: Path | None = None,
        package_name: str | None = None,
        launch_activity: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RavenRunResult:
        issue_ref = parse_issue_url(issue_url)
        run_dir = self.config.runtime.workspace_dir / issue_ref.slug
        run_dir.mkdir(parents=True, exist_ok=True)
        self._progress("intake", "running", f"Created run directory {run_dir}")

        if package_name:
            self.config.android.package_name = package_name
        if launch_activity:
            self.config.android.launch_activity = launch_activity

        self._progress("intake", "running", f"Fetching GitHub issue {issue_url}")
        issue = self.github.get_issue(issue_url)
        (run_dir / "issue.json").write_text(issue.model_dump_json(indent=2), encoding="utf-8")
        self._progress("intake", "complete", f"Loaded issue #{issue.number}: {issue.title}")

        self._progress("repo", "running", f"Cloning/updating repository {repo_url}")
        repo_path, checkout_ref = self.repo_manager.clone_and_checkout_prefix(repo_url, issue)
        self._progress("repo", "complete", f"Checked out pre-fix ref {checkout_ref}")

        self._progress("media", "running", "Preparing issue media frames")
        frames = self._prepare_frames(media_path, run_dir)
        self._progress("media", "complete", f"Prepared {len(frames)} reference frame(s)")

        if skip_install or apk_path is None:
            self._progress("emulator", "skipped", "APK install skipped — using app already on emulator")
        else:
            self._progress("emulator", "running", f"Installing APK on {self.config.android.emulator_serial}")
            self.adb.install_apk(apk_path)
            self._progress("emulator", "complete", "APK installed")

        self._check_cancelled(cancel_event)
        action_agent = ActionSequenceAgent(self.llm)
        self._progress("agent1", "running", "Generating executable Android action sequence")
        sequence = action_agent.generate(issue.text, frames, run_dir / "actions.json")
        self._progress("agent1", "complete", f"Generated {len(sequence.actions)} action(s)")

        self._check_cancelled(cancel_event)
        reproduction_agent = BugReproductionAgent(
            self.adb,
            self.llm,
            max_logcat_bytes=self.config.runtime.max_logcat_bytes,
            max_ui_hierarchy_snapshots=self.config.runtime.max_ui_hierarchy_snapshots,
            compact_json=self.config.runtime.compact_json,
        )
        self._progress("agent2", "running", "Replaying action sequence with logcat capture")
        replay = self._replay_with_repair(
            reproduction_agent=reproduction_agent,
            action_agent=action_agent,
            issue_text=issue.text,
            sequence=sequence,
            repo_path=repo_path,
            run_dir=run_dir,
            frames=frames,
        )
        self._progress(
            "agent2",
            "complete" if replay.verified else "warning",
            f"Replay verified={replay.verified}; attempts={replay.attempts}",
        )

        covered_files = replay.covered_files
        if not replay.verified or not covered_files:
            reason = (
                f"bug reproduction failed after {replay.attempts} attempt(s)"
                if not replay.verified
                else "logcat produced no file hits"
            )
            self._progress(
                "fallback",
                "running",
                f"Static text-search fallback triggered ({reason}) — ranking top-{self.config.runtime.static_top_k} files by TF-IDF cosine similarity to issue text",
            )
            covered_files = rank_similar_files(
                repo_path,
                issue.text,
                min(self.config.runtime.static_top_k, self.config.runtime.max_hdg_files),
            )
            replay.covered_files = covered_files
            (run_dir / "static_fallback_files.json").write_text(
                json.dumps([str(path) for path in covered_files], indent=2),
                encoding="utf-8",
            )
            self._progress("fallback", "complete", f"Selected {len(covered_files)} file(s) via static fallback")

        self._check_cancelled(cancel_event)
        hdg_path = run_dir / "hdg.json"
        self._progress("agent3", "running", "Building heterogeneous Android data-flow graph")
        HDGGenerationAgent(self.llm).build(
            repo_path,
            covered_files,
            hdg_path,
            action_sequence=sequence,
            replay=replay,
            max_files=self.config.runtime.max_hdg_files,
            expansion_bound=self.config.runtime.hdg_expansion_bound,
            compact_json=self.config.runtime.compact_json,
            joern_parse=self.config.tools.joern_parse,
            joern_export=self.config.tools.joern_export,
        )
        self._progress("agent3", "complete", f"HDG written to {hdg_path} and {hdg_path.with_suffix('.sqlite')}")

        self._check_cancelled(cancel_event)
        localization_path = run_dir / "localization.json"
        self._progress("agent4", "running", "Running root-cause localization")
        candidates = RootCauseLocalizationAgent(self.llm).localize(
            issue=issue,
            hdg_path=hdg_path,
            logcat_path=replay.logcat_path,
            repo_path=repo_path,
            output_path=localization_path,
        )
        self._progress("agent4", "complete", f"Ranked {len(candidates)} candidate(s)")

        report_path = self._write_report(
            run_dir=run_dir,
            issue_url=issue_url,
            repo_url=repo_url,
            checkout_ref=checkout_ref,
            replay=replay,
            covered_files=covered_files,
            candidates=candidates,
        )
        self._progress("done", "complete", f"Report written to {report_path}")

        return RavenRunResult(
            run_dir=run_dir,
            issue=issue,
            repo_path=repo_path,
            checkout_ref=checkout_ref,
            action_sequence=sequence,
            replay=replay,
            hdg_path=hdg_path,
            localization_path=localization_path,
            report_path=report_path,
        )

    def _prepare_frames(self, media_path: Path | None, run_dir: Path) -> list[Path]:
        if not media_path:
            return []
        media_path = media_path.resolve()
        frames_dir = run_dir / "frames"
        if media_path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            frames_dir.mkdir(parents=True, exist_ok=True)
            copied = frames_dir / media_path.name
            shutil.copy2(media_path, copied)
            return [copied]
        return extract_frames(
            media_path,
            frames_dir,
            sample_count=self.config.runtime.frame_sample_count,
            ffmpeg_path=self.config.tools.ffmpeg_path,
        )

    def _replay_with_repair(
        self,
        *,
        reproduction_agent: BugReproductionAgent,
        action_agent: ActionSequenceAgent,
        issue_text: str,
        sequence: ActionSequence,
        repo_path: Path,
        run_dir: Path,
        frames: list[Path],
    ) -> ReplayResult:
        latest = sequence
        result: ReplayResult | None = None
        all_covered: set[Path] = set()
        max_attempts = self.config.runtime.max_replay_attempts
        for attempt in range(1, max_attempts + 1):
            self._progress("agent2", "running", f"Replay attempt {attempt} of {max_attempts}")
            try:
                result = reproduction_agent.replay_once(
                    sequence=latest,
                    repo_path=repo_path,
                    run_dir=run_dir,
                    attempt=attempt,
                    target_frames=frames,
                )
            except Exception as _exc:
                # adb unavailable, app not installed, or emulator not responding —
                # return a failed ReplayResult so the static TF-IDF fallback triggers
                self._progress(
                    "agent2",
                    "warning",
                    f"Replay attempt {attempt} aborted ({type(_exc).__name__}: {_exc}) "
                    f"— triggering static text-search fallback",
                )
                dummy_logcat = run_dir / "replay" / f"attempt_{attempt}" / "logcat.txt"
                dummy_logcat.parent.mkdir(parents=True, exist_ok=True)
                dummy_logcat.touch()
                return ReplayResult(
                    verified=False,
                    attempts=attempt,
                    logcat_path=dummy_logcat,
                    verification_reason=f"adb error: {_exc}",
                )
            all_covered.update(result.covered_files)
            if result.verified:
                result.covered_files = sorted(all_covered)
                self._progress("agent2", "complete", f"Replay attempt {attempt} matched final state")
                return result
            if attempt < max_attempts:
                self._progress("agent1", "running", f"Repairing action sequence after attempt {attempt}")
                latest = action_agent.repair(
                    issue_text,
                    latest,
                    result.verification_reason,
                    frames,
                    run_dir / f"actions_repaired_attempt_{attempt + 1}.json",
                )
                self._progress("agent1", "complete", f"Prepared repaired sequence for attempt {attempt + 1}")
        if result is None:
            raise RuntimeError("Replay loop did not run")
        result.covered_files = sorted(all_covered)
        self._progress(
            "agent2",
            "warning",
            f"All {max_attempts} replay attempts exhausted without verification — triggering static text-search fallback",
        )
        return result

    def _progress(self, agent: str, status: str, message: str) -> None:
        self.progress(agent, status, message)
        if self.run_logger:
            self.run_logger.event(agent, status, message)

    def _write_report(
        self,
        *,
        run_dir: Path,
        issue_url: str,
        repo_url: str,
        checkout_ref: str,
        replay: ReplayResult,
        covered_files: list[Path],
        candidates: list,
    ) -> Path:
        report = run_dir / "report.md"
        lines = [
            "# RAVEN Report",
            "",
            f"- Issue: {issue_url}",
            f"- Repository: {repo_url}",
            f"- Checkout: `{checkout_ref}`",
            f"- Reproduced: `{replay.verified}` after {replay.attempts} attempt(s)",
            f"- Verification: {replay.verification_reason}",
            "",
            "## Runtime / Static Files",
            *[f"- `{path}`" for path in covered_files[:50]],
            "",
            "## Ranked Fault Candidates",
        ]
        for candidate in candidates:
            loc = f"{candidate.file}:{candidate.line}" if candidate.line else candidate.file
            lines.append(f"{candidate.rank}. `{loc}` `{candidate.symbol}` score={candidate.suspicion}")
            lines.append(f"   {candidate.reasoning}")
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report


def _load_json_if_exists(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError:
        return {"path": str(path), "parse_error": True}
