from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from raven.models import IssueReport


ISSUE_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)")


@dataclass(frozen=True)
class GitHubIssueRef:
    owner: str
    repo: str
    number: int

    @property
    def api_base(self) -> str:
        return f"https://api.github.com/repos/{self.owner}/{self.repo}"

    @property
    def slug(self) -> str:
        return f"{self.owner}_{self.repo}_{self.number}".replace("-", "_")


def parse_issue_url(url: str) -> GitHubIssueRef:
    match = ISSUE_RE.search(url)
    if not match:
        raise ValueError(f"Unsupported GitHub issue URL: {url}")
    return GitHubIssueRef(match["owner"], match["repo"], int(match["number"]))


class GitHubClient:
    def __init__(self, token: str = ""):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/vnd.github+json"})
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}"})

    def get_issue(self, issue_url: str) -> IssueReport:
        ref = parse_issue_url(issue_url)
        issue = self._get(f"{ref.api_base}/issues/{ref.number}")
        events = self._get_all(f"{ref.api_base}/issues/{ref.number}/events")
        timeline = self._get_all(
            f"{ref.api_base}/issues/{ref.number}/timeline",
            accept="application/vnd.github.mockingbird-preview+json",
        )
        body = issue.get("body") or ""
        media_urls = sorted(set(_extract_media_urls(body)))
        linked_prs = sorted(set(_extract_linked_prs(body, timeline)))
        fix_commits = sorted(set(_extract_fix_commits(events, timeline)))
        return IssueReport(
            url=issue_url,
            number=ref.number,
            title=issue.get("title") or "",
            body=body,
            state=issue.get("state") or "",
            created_at=issue.get("created_at") or "",
            closed_at=issue.get("closed_at"),
            labels=[label.get("name", "") for label in issue.get("labels", [])],
            media_urls=media_urls,
            linked_pull_requests=linked_prs,
            fix_commits=fix_commits,
        )

    def _get(self, url: str, accept: str | None = None) -> dict[str, Any]:
        headers = {"Accept": accept} if accept else None
        response = self.session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()

    def _get_all(self, url: str, accept: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            headers = {"Accept": accept} if accept else None
            response = self.session.get(
                url, params={"per_page": 100, "page": page}, headers=headers, timeout=30
            )
            if response.status_code in {403, 404, 410}:
                return out
            response.raise_for_status()
            items = response.json()
            if not items:
                return out
            out.extend(items)
            page += 1


def repo_url_to_slug(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    path = parsed.path.strip("/").removesuffix(".git")
    return path.replace("/", "_").replace("-", "_")


def _extract_media_urls(text: str) -> list[str]:
    url_re = re.compile(r"https?://\S+")
    media_ext = (".mp4", ".mov", ".webm", ".mkv", ".png", ".jpg", ".jpeg")
    urls = []
    for raw in url_re.findall(text):
        url = raw.rstrip(").,]")
        if "youtube.com" in url or "youtu.be" in url or url.lower().endswith(media_ext):
            urls.append(url)
    return urls


def _extract_linked_prs(body: str, timeline: list[dict[str, Any]]) -> list[str]:
    prs = re.findall(r"github\.com/[^/]+/[^/]+/pull/\d+", body)
    for event in timeline:
        source = event.get("source") or {}
        issue = source.get("issue") or {}
        html_url = issue.get("html_url") or ""
        if "/pull/" in html_url:
            prs.append(html_url)
    return prs


def _extract_fix_commits(events: list[dict[str, Any]], timeline: list[dict[str, Any]]) -> list[str]:
    commits = []
    for event in events + timeline:
        commit_id = event.get("commit_id")
        if commit_id:
            commits.append(commit_id)
        commit_url = event.get("commit_url") or ""
        if commit_url:
            commits.append(commit_url.rstrip("/").split("/")[-1])
    return commits

