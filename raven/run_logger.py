from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RavenRunLogger:
    def __init__(
        self,
        path: Path,
        *,
        app_name: str,
        issue_id: int,
        issue_url: str,
        repo_url: str,
        compact_json: bool = True,
    ):
        self.path = path
        self.compact_json = compact_json
        self.started_monotonic = time.perf_counter()
        self.data: dict[str, Any] = {
            "schema_version": "1.0",
            "app_name": app_name,
            "issue_id": issue_id,
            "issue_url": issue_url,
            "repo_url": repo_url,
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "total_duration_ms": None,
            "token_usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
            },
            "events": [],
            "llm_calls": [],
            "final_results": None,
        }
        self.flush()

    def event(self, agent: str, status: str, message: str, **extra: Any) -> None:
        item = {
            "timestamp": _now(),
            "elapsed_ms": self.elapsed_ms(),
            "agent": agent,
            "status": status,
            "message": message,
        }
        item.update(_jsonable(extra))
        self.data["events"].append(item)
        self.flush()

    def llm_call(
        self,
        *,
        name: str,
        model: str,
        started_at: str,
        duration_ms: int,
        prompt_chars: int,
        image_count: int,
        response_chars: int,
        usage: dict[str, int],
    ) -> None:
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        self.data["token_usage"]["prompt_tokens"] += prompt_tokens
        self.data["token_usage"]["completion_tokens"] += completion_tokens
        self.data["token_usage"]["total_tokens"] += total_tokens
        self.data["token_usage"]["calls"] += 1
        self.data["llm_calls"].append(
            {
                "timestamp": started_at,
                "elapsed_ms": self.elapsed_ms(),
                "name": name,
                "model": model,
                "duration_ms": duration_ms,
                "prompt_chars": prompt_chars,
                "image_count": image_count,
                "response_chars": response_chars,
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            }
        )
        self.flush()

    def finalize(self, status: str, final_results: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.data["status"] = status
        self.data["finished_at"] = _now()
        self.data["total_duration_ms"] = self.elapsed_ms()
        if final_results is not None:
            self.data["final_results"] = _jsonable(final_results)
        if error:
            self.data["error"] = error
        self.flush()

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started_monotonic) * 1000)

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.compact_json:
            payload = json.dumps(self.data, separators=(",", ":"))
        else:
            payload = json.dumps(self.data, indent=2)
        self.path.write_text(payload, encoding="utf-8")


def log_filename(app_name: str, issue_id: int) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in app_name).strip("_")
    return f"{safe}-{issue_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    return value
