from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


class AndroidActionType(str, Enum):
    tap = "tap"
    long_press = "long_press"
    swipe = "swipe"
    input_text = "input_text"
    keyevent = "keyevent"
    wait = "wait"
    launch_activity = "launch_activity"
    shell = "shell"


class AndroidAction(BaseModel):
    type: AndroidActionType
    x: int | None = None
    y: int | None = None
    x2: int | None = None
    y2: int | None = None
    duration_ms: int = 300
    text: str | None = None
    keycode: str | int | None = None
    activity: str | None = None
    command: str | None = None
    rationale: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # LLM may return "action" instead of "type"
        if "action" in data and "type" not in data:
            data = dict(data)
            data["type"] = data.pop("action")
        # LLM may nest coordinates/text inside "params" dict
        if "params" in data and isinstance(data["params"], dict):
            data = dict(data)
            params = data.pop("params")
            for key in ("x", "y", "x2", "y2", "duration_ms", "text", "keycode", "activity", "command"):
                if key in params and key not in data:
                    data[key] = params[key]
        # LLM may return numeric duration as text (e.g. wait action with text=3000)
        if "text" in data and data["text"] is not None and not isinstance(data["text"], str):
            data = dict(data)
            data["text"] = str(data["text"])
        return data


class ActionSequence(BaseModel):
    actions: list[AndroidAction] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    target_final_state: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        tfs = data.get("target_final_state")
        # LLM may return target_final_state as a dict — flatten to a JSON string
        if isinstance(tfs, dict):
            import json as _json
            data = dict(data)
            data["target_final_state"] = _json.dumps(tfs)
        return data


class IssueReport(BaseModel):
    url: str
    number: int
    title: str
    body: str = ""
    state: str = ""
    created_at: str = ""
    closed_at: str | None = None
    labels: list[str] = Field(default_factory=list)
    media_urls: list[str] = Field(default_factory=list)
    linked_pull_requests: list[str] = Field(default_factory=list)
    fix_commits: list[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        labels = ", ".join(self.labels)
        return f"{self.title}\n\n{self.body}\n\nLabels: {labels}"


class ReplayResult(BaseModel):
    verified: bool
    attempts: int
    logcat_path: Path
    screenshot_path: Path | None = None
    ui_hierarchy_paths: list[Path] = Field(default_factory=list)
    covered_files: list[Path] = Field(default_factory=list)
    verification_reason: str = ""
    action_history: list[dict[str, Any]] = Field(default_factory=list)


class LocalizationCandidate(BaseModel):
    rank: int
    file: str
    symbol: str = ""
    line: int | None = None
    snippet: str = ""
    suspicion: float = 0.0
    reasoning: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # LLM may return reasoning as a list of strings
        if "reasoning" in data and isinstance(data["reasoning"], list):
            data = dict(data)
            data["reasoning"] = " ".join(str(s) for s in data["reasoning"])
        # LLM may return suspicion as a string
        if "suspicion" in data and isinstance(data["suspicion"], str):
            data = dict(data)
            try:
                data["suspicion"] = float(data["suspicion"].strip().rstrip("%")) / (
                    100.0 if "%" in str(data["suspicion"]) else 1.0
                )
            except ValueError:
                data["suspicion"] = 0.0
        return data


class RavenRunResult(BaseModel):
    run_dir: Path
    issue: IssueReport
    repo_path: Path
    checkout_ref: str
    action_sequence: ActionSequence
    replay: ReplayResult
    hdg_path: Path
    localization_path: Path
    report_path: Path
