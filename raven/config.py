from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class AzureOpenAIConfig(BaseModel):
    endpoint: str
    api_key: str
    api_version: str = "2024-02-15-preview"
    deployment: str = "gpt-4o"


class GitHubConfig(BaseModel):
    token: str = ""


class AndroidConfig(BaseModel):
    adb_path: str = "adb"
    emulator_serial: str = "emulator-5554"
    package_name: str = ""
    launch_activity: str = ""
    install_timeout_seconds: int = 120
    apk_path: str = ""  # default APK; overridden by --apk on the CLI
    apk_paths: list[str] = Field(default_factory=list)  # all available APKs for this app


class ToolConfig(BaseModel):
    ffmpeg_path: str = "ffmpeg"
    joern_parse: str = "joern-parse"
    joern_export: str = "joern-export"
    soot_jar: str = ""


class RuntimeConfig(BaseModel):
    workspace_dir: Path = Path(".raven_runs")
    max_replay_attempts: int = 3
    frame_sample_count: int = 12
    static_top_k: int = 30
    max_logcat_bytes: int = 2_000_000
    max_ui_hierarchy_snapshots: int = 8
    max_hdg_files: int = 120
    hdg_expansion_bound: int = 2
    compact_json: bool = True


class RavenConfig(BaseModel):
    azure_openai: AzureOpenAIConfig
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    android: AndroidConfig = Field(default_factory=AndroidConfig)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @classmethod
    def load(cls, path: str | Path) -> "RavenConfig":
        config_path = Path(path)
        with config_path.open("r", encoding="utf-8") as handle:
            data: dict[str, Any] = yaml.safe_load(handle) or {}
        cfg = cls.model_validate(data)
        if not cfg.runtime.workspace_dir.is_absolute():
            cfg.runtime.workspace_dir = (config_path.parent / cfg.runtime.workspace_dir).resolve()
        return cfg
