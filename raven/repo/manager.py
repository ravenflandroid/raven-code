from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from git import Repo

from raven.models import IssueReport
from raven.repo.github_client import repo_url_to_slug


class RepositoryManager:
    def __init__(self, workspace_dir: Path):
        self.workspace_dir = workspace_dir

    def clone_and_checkout_prefix(self, repo_url: str, issue: IssueReport) -> tuple[Path, str]:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = self.workspace_dir / "repos" / repo_url_to_slug(repo_url)
        if repo_dir.exists():
            repo = Repo(repo_dir)
            repo.git.fetch("--all", "--tags")
        else:
            repo = Repo.clone_from(repo_url, repo_dir)
            repo.git.fetch("--all", "--tags")

        checkout_ref = self._choose_prefix_ref(repo, issue)
        # --force handles repos with deeply-nested paths that exceed the Windows MAX_PATH
        # limit — git refuses the checkout otherwise; -f also clears untracked files that
        # would otherwise block the switch.  git clean -fd removes any leftovers from HEAD
        # that don't exist at the target ref.
        repo.git.clean("-fd")
        repo.git.checkout("--force", checkout_ref)
        return repo_dir, checkout_ref

    def _choose_prefix_ref(self, repo: Repo, issue: IssueReport) -> str:
        for commit in issue.fix_commits:
            try:
                repo.git.rev_parse("--verify", commit)
                parent = repo.git.rev_parse(f"{commit}^")
                return parent
            except Exception:
                continue

        stable_ref = self._stable_ref_before_issue_close(repo, issue)
        if stable_ref:
            return stable_ref

        default_branch = _default_branch(repo.working_tree_dir or ".")
        try:
            repo.git.checkout(default_branch)
            return default_branch
        except Exception:
            return "HEAD"

    def _stable_ref_before_issue_close(self, repo: Repo, issue: IssueReport) -> str | None:
        cutoff = _parse_github_time(issue.closed_at or issue.created_at)
        if not cutoff:
            return None
        candidates: list[tuple[datetime, str]] = []
        for tag in repo.tags:
            try:
                commit = tag.commit
                when = commit.committed_datetime.astimezone(timezone.utc)
                if when <= cutoff:
                    candidates.append((when, str(tag)))
            except Exception:
                continue
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]

        branch_names = ["main", "master", "develop"]
        for branch in branch_names:
            ref = f"origin/{branch}"
            try:
                commit = repo.commit(ref)
                when = commit.committed_datetime.astimezone(timezone.utc)
                if when <= cutoff:
                    return ref
            except Exception:
                continue
        return None


def _default_branch(path: str) -> str:
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=path,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.strip().split("/")[-1]
    except Exception:
        return "main"


def _parse_github_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
