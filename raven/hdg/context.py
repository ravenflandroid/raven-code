from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def build_hdg_context_pack(hdg_path: Path, *, max_nodes: int = 80, max_edges: int = 140, max_snippets: int = 24) -> str:
    db_path = hdg_path.with_suffix(".sqlite")
    if not db_path.exists():
        return hdg_path.read_text(encoding="utf-8", errors="ignore")[:80000]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        observed = _observed(conn)
        source_nodes = _source_nodes(conn, max_snippets)
        edges = _edges_for_nodes(conn, [n["id"] for n in observed + source_nodes], max_edges)
        pack = {
            "purpose": "Token-friendly HDG context for ReACT RCA. Prefer observed runtime nodes, callback/resource paths, UI-state updates, and source snippets.",
            "observed_nodes": observed[:max_nodes],
            "source_candidate_nodes": source_nodes[:max_snippets],
            "relevant_edges": edges[:max_edges],
            "query_hints": [
                "Start from UIACTION/WIDGET/UISTATE observed_nodes.",
                "Follow ACTSON -> DECLAREDIN -> RESOLVESTO/BINDSTO -> TRIGGERS -> CONTROLS/USES/UPDATES.",
                "Rank KOTLINSTMT/JAVASTMT first, then KOTLINMETHOD/JAVAMETHOD if statement evidence is insufficient.",
            ],
        }
        return json.dumps(pack, separators=(",", ":"))
    finally:
        conn.close()


def _observed(conn: sqlite3.Connection) -> list[dict]:
    raw = conn.execute("select value from graph_meta where key='observed_node_ids'").fetchone()
    observed_ids = json.loads(raw["value"]) if raw else []
    if not observed_ids:
        return []
    placeholders = ",".join("?" for _ in observed_ids)
    rows = conn.execute(
        f"select id,type,label,file,line,properties_json,evidence_json from nodes where id in ({placeholders})",
        observed_ids,
    ).fetchall()
    return [_node(row) for row in rows]


def _source_nodes(conn: sqlite3.Connection, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        select id,type,label,file,line,properties_json,evidence_json
        from nodes
        where type in ('KOTLINSTMT','JAVASTMT','KOTLINMETHOD','JAVAMETHOD','FRAMEWORKCALLBACK','RESOURCE','XMLNODE')
        order by
          case type
            when 'KOTLINSTMT' then 0
            when 'JAVASTMT' then 1
            when 'KOTLINMETHOD' then 2
            when 'JAVAMETHOD' then 3
            when 'FRAMEWORKCALLBACK' then 4
            else 5
          end,
          file,
          line
        limit ?
        """,
        (limit,),
    ).fetchall()
    return [_node(row) for row in rows]


def _edges_for_nodes(conn: sqlite3.Connection, node_ids: list[str], limit: int) -> list[dict]:
    if not node_ids:
        return []
    placeholders = ",".join("?" for _ in node_ids)
    rows = conn.execute(
        f"""
        select source,target,type,properties_json,evidence_json from edges
        where source in ({placeholders}) or target in ({placeholders})
        order by
          case type
            when 'ACTSON' then 0
            when 'DECLAREDIN' then 1
            when 'TRIGGERS' then 2
            when 'UPDATES' then 3
            when 'OVERWRITES' then 4
            else 5
          end
        limit ?
        """,
        [*node_ids, *node_ids, limit],
    ).fetchall()
    return [
        {
            "source": row["source"],
            "target": row["target"],
            "type": row["type"],
            "evidence": _evidence(row["evidence_json"]),
        }
        for row in rows
    ]


def _node(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "label": row["label"],
        "file": row["file"],
        "line": row["line"],
        "properties": _loads(row["properties_json"]),
        "evidence": _evidence(row["evidence_json"]),
    }


def _evidence(raw: str) -> list[dict]:
    items = _loads(raw)
    return items[:3] if isinstance(items, list) else []


def _loads(raw: str):
    try:
        return json.loads(raw or "null")
    except json.JSONDecodeError:
        return None
