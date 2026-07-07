from __future__ import annotations

from pathlib import Path

from raven.hdg.builder import HDGBuilder
from raven.llm.azure_client import AzureGPT4OClient
from raven.models import ActionSequence, ReplayResult


HDG_SYSTEM = """You are Agent #3, the HDG orchestration agent.
You receive a failure-specific Polyglot HDG for Android:
G_tau=(V,E,phi,psi,omega). Preserve the node taxonomy
{UIACTION,WIDGET,UISTATE,XMLNODE,RESOURCE,KOTLINSTMT,KOTLINMETHOD,JAVASTMT,JAVAMETHOD,FRAMEWORKCALLBACK}
and edge taxonomy {CALLS,CONTROLS,DEFINES,USES,ACTSON,DECLAREDIN,RESOLVESTO,BINDSTO,TRIGGERS,UPDATES,OVERWRITES}.
Only add evidence-grounded inferred edges/nodes that improve RCA traceability from GUI trigger to source statement.
Return JSON with optional keys: nodes, edges, notes. Every node/edge must include evidence."""


class HDGGenerationAgent:
    def __init__(self, llm: AzureGPT4OClient):
        self.llm = llm

    def build(
        self,
        repo_path: Path,
        covered_files: list[Path],
        output_path: Path,
        *,
        action_sequence: ActionSequence | None = None,
        replay: ReplayResult | None = None,
        max_files: int = 120,
        expansion_bound: int = 2,
        compact_json: bool = True,
        joern_parse: str = "joern-parse",
        joern_export: str = "joern-export",
    ) -> Path:
        graph = HDGBuilder(
            repo_path,
            expansion_bound=expansion_bound,
            max_files=max_files,
            joern_parse=joern_parse,
            joern_export=joern_export,
        ).build(
            covered_files[:max_files],
            action_sequence=action_sequence,
            replay=replay,
        )
        prompt = graph.to_prompt(max_nodes=200, max_edges=300)
        try:
            enrichment = self.llm.complete_json(HDG_SYSTEM, prompt)
            graph.apply_enrichment(enrichment)
        except Exception as exc:
            graph.notes.append(f"LLM enrichment skipped: {exc}")
        output_path.write_text(graph.model_dump_json(indent=None if compact_json else 2), encoding="utf-8")
        graph.export_sqlite(output_path.with_suffix(".sqlite"))
        return output_path
