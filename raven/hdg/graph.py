from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


NODE_TYPES = {
    "UIACTION",
    "WIDGET",
    "UISTATE",
    "XMLNODE",
    "RESOURCE",
    "KOTLINSTMT",
    "KOTLINMETHOD",
    "JAVASTMT",
    "JAVAMETHOD",
    "FRAMEWORKCALLBACK",
}

INTRA_EDGE_TYPES = {"CALLS", "CONTROLS", "DEFINES", "USES"}
CROSS_EDGE_TYPES = {"ACTSON", "DECLAREDIN", "RESOLVESTO", "BINDSTO", "TRIGGERS", "UPDATES", "OVERWRITES"}
EDGE_TYPES = INTRA_EDGE_TYPES | CROSS_EDGE_TYPES
SAFE_EXPANSION_EDGES = {"CALLS", "CONTROLS", "DEFINES", "USES", "DECLAREDIN", "RESOLVESTO", "BINDSTO", "TRIGGERS", "UPDATES"}


class HDGEvidence(BaseModel):
    source: str
    detail: str
    file: str | None = None
    line: int | None = None
    confidence: float = 1.0


class HDGNode(BaseModel):
    id: str
    type: str
    label: str
    file: str | None = None
    line: int | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    evidence: list[HDGEvidence] = Field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.type


class HDGEdge(BaseModel):
    source: str
    target: str
    type: str
    properties: dict[str, Any] = Field(default_factory=dict)
    evidence: list[HDGEvidence] = Field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.type


class HeterogeneousDataFlowGraph(BaseModel):
    repo_root: str
    trace_id: str = "tau"
    principle: str = (
        "Failure-specific Polyglot HDG G_tau=(V,E,phi,psi,omega), combining static "
        "candidate graph completeness with execution-guided relevance from logs, UI "
        "hierarchy snapshots, callback traces, and runtime coverage."
    )
    nodes: list[HDGNode] = Field(default_factory=list)
    edges: list[HDGEdge] = Field(default_factory=list)
    observed_node_ids: list[str] = Field(default_factory=list)
    expansion_bound: int = 2
    notes: list[str] = Field(default_factory=list)

    def add_node(self, node: HDGNode) -> None:
        if node.type not in NODE_TYPES:
            raise ValueError(f"Unsupported HDG node type: {node.type}")
        for existing in self.nodes:
            if existing.id == node.id:
                existing.evidence.extend(node.evidence)
                existing.properties.update(node.properties)
                return
        self.nodes.append(node)

    def add_edge(self, edge: HDGEdge) -> None:
        if edge.type not in EDGE_TYPES:
            raise ValueError(f"Unsupported HDG edge type: {edge.type}")
        for existing in self.edges:
            if (existing.source, existing.target, existing.type) == (edge.source, edge.target, edge.type):
                existing.evidence.extend(edge.evidence)
                existing.properties.update(edge.properties)
                return
        self.edges.append(edge)

    def mark_observed(self, node_id: str) -> None:
        if node_id not in self.observed_node_ids:
            self.observed_node_ids.append(node_id)

    def apply_enrichment(self, enrichment: dict[str, Any]) -> None:
        for raw in enrichment.get("nodes", []):
            if "kind" in raw and "type" not in raw:
                raw["type"] = raw.pop("kind")
            self.add_node(HDGNode.model_validate(raw))
        for raw in enrichment.get("edges", []):
            if "kind" in raw and "type" not in raw:
                raw["type"] = raw.pop("kind")
            self.add_edge(HDGEdge.model_validate(raw))
        for note in enrichment.get("notes", []):
            self.notes.append(str(note))

    def induced_by_coverage_expansion(self, bound: int = 2) -> "HeterogeneousDataFlowGraph":
        if not self.observed_node_ids:
            self.notes.append("No observed nodes found; retained conservative candidate graph.")
            return self
        node_types = {node.id: node.type for node in self.nodes}
        if not any(node_types.get(node_id) != "UIACTION" for node_id in self.observed_node_ids):
            self.notes.append("Only UIACTION nodes were observed; retained conservative candidate graph to avoid dropping source context.")
            return self

        adjacency: dict[str, set[str]] = {}
        for edge in self.edges:
            if edge.type not in SAFE_EXPANSION_EDGES:
                continue
            adjacency.setdefault(edge.source, set()).add(edge.target)
            adjacency.setdefault(edge.target, set()).add(edge.source)

        keep = set(self.observed_node_ids)
        frontier = set(self.observed_node_ids)
        for _ in range(bound):
            next_frontier: set[str] = set()
            for node_id in frontier:
                next_frontier.update(adjacency.get(node_id, set()) - keep)
            keep.update(next_frontier)
            frontier = next_frontier

        filtered = HeterogeneousDataFlowGraph(
            repo_root=self.repo_root,
            trace_id=self.trace_id,
            principle=self.principle,
            observed_node_ids=[node_id for node_id in self.observed_node_ids if node_id in keep],
            expansion_bound=bound,
            notes=[*self.notes, f"Coverage-expanded filtering retained {len(keep)} node(s) with d={bound}."],
        )
        for node in self.nodes:
            if node.id in keep:
                filtered.add_node(node)
        for edge in self.edges:
            if edge.source in keep and edge.target in keep:
                filtered.add_edge(edge)
        return filtered

    def to_prompt(self, max_nodes: int = 200, max_edges: int = 300) -> str:
        nodes = [n.model_dump() for n in self.nodes[:max_nodes]]
        edges = [e.model_dump() for e in self.edges[:max_edges]]
        return f"""HDG formulation:
{self.principle}

Node types TV: {sorted(NODE_TYPES)}
Edge types TE: {sorted(EDGE_TYPES)}
Safe expansion edges: {sorted(SAFE_EXPANSION_EDGES)}
Observed nodes: {self.observed_node_ids[:100]}

Filtered HDG nodes:
{nodes}

Filtered HDG edges:
{edges}

Add only evidence-grounded nodes/edges. Preserve uppercase node and edge types.
Every new node or edge must include omega evidence."""

    def export_sqlite(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "create table graph_meta (key text primary key, value text not null)"
            )
            conn.execute(
                "create table nodes (id text primary key, type text, label text, file text, line integer, properties_json text, evidence_json text)"
            )
            conn.execute(
                "create table edges (id integer primary key autoincrement, source text, target text, type text, properties_json text, evidence_json text)"
            )
            conn.execute("create index idx_nodes_type on nodes(type)")
            conn.execute("create index idx_edges_source on edges(source)")
            conn.execute("create index idx_edges_target on edges(target)")
            conn.execute("insert into graph_meta values (?, ?)", ("trace_id", self.trace_id))
            conn.execute("insert into graph_meta values (?, ?)", ("principle", self.principle))
            conn.execute("insert into graph_meta values (?, ?)", ("observed_node_ids", json.dumps(self.observed_node_ids)))
            for node in self.nodes:
                conn.execute(
                    "insert into nodes values (?, ?, ?, ?, ?, ?, ?)",
                    (
                        node.id,
                        node.type,
                        node.label,
                        node.file,
                        node.line,
                        json.dumps(node.properties),
                        json.dumps([e.model_dump() for e in node.evidence]),
                    ),
                )
            for edge in self.edges:
                conn.execute(
                    "insert into edges(source, target, type, properties_json, evidence_json) values (?, ?, ?, ?, ?)",
                    (
                        edge.source,
                        edge.target,
                        edge.type,
                        json.dumps(edge.properties),
                        json.dumps([e.model_dump() for e in edge.evidence]),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return path


def evidence(source: str, detail: str, file: str | None = None, line: int | None = None, confidence: float = 1.0) -> HDGEvidence:
    return HDGEvidence(source=source, detail=detail, file=file, line=line, confidence=confidence)


def stable_id(*parts: object) -> str:
    raw = "::".join(str(part) for part in parts)
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", raw)


def rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
