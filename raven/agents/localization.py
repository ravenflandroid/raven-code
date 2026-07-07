from __future__ import annotations

import json
from pathlib import Path

from raven.hdg.context import build_hdg_context_pack
from raven.llm.azure_client import AzureGPT4OClient
from raven.models import IssueReport, LocalizationCandidate


LOCALIZE_SYSTEM = """You are Agent #4 in RAVEN.
Perform root-cause localization using ReAct-style step-by-step reasoning over a failure-specific
Polyglot HDG G_tau=(V,E,phi,psi,omega). Start from observed UIACTION/WIDGET/UISTATE nodes,
follow ACTSON, DECLAREDIN, RESOLVESTO, BINDSTO, TRIGGERS, CONTROLS, USES, UPDATES, and
OVERWRITES evidence, and trace back to exact KOTLINSTMT/JAVASTMT or method nodes.
Prefer candidates with runtime evidence omega, callback reachability, and UI-state update edges.
Return JSON with key candidates: list of ranked objects containing rank, file, symbol, line,
snippet, suspicion, reasoning."""


class RootCauseLocalizationAgent:
    def __init__(self, llm: AzureGPT4OClient):
        self.llm = llm

    def localize(
        self,
        *,
        issue: IssueReport,
        hdg_path: Path,
        logcat_path: Path,
        repo_path: Path,
        output_path: Path,
    ) -> list[LocalizationCandidate]:
        hdg_context = build_hdg_context_pack(hdg_path)
        hdg_db = hdg_path.with_suffix(".sqlite")
        log_text = logcat_path.read_text(encoding="utf-8", errors="ignore") if logcat_path.exists() else ""
        prompt = f"""GitHub issue:
{issue.text}

Token-friendly HDG context pack:
{hdg_context}

HDG SQLite database path:
{hdg_db if hdg_db.exists() else "not available"}

Runtime logcat:
{log_text[-60000:]}

Repository root: {repo_path}
Use ReAct-style reasoning internally:
1. identify observed GUI trigger node(s);
2. follow HDG evidence edges to runtime widget/XML/resource/callback nodes;
3. follow callback/control/use/update/overwrite edges into source;
4. rank exact faulty statements or methods.
Include snippets where possible and cite the HDG evidence in reasoning.
"""
        data = self.llm.complete_json(LOCALIZE_SYSTEM, prompt)
        candidates = [LocalizationCandidate.model_validate(item) for item in data.get("candidates", [])]
        output_path.write_text(json.dumps({"candidates": [c.model_dump() for c in candidates]}, indent=2), encoding="utf-8")
        return candidates
