from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path


SOURCE_EXTENSIONS = {".kt", ".java", ".xml", ".gradle", ".kts"}


def rank_similar_files(repo_path: Path, query: str, top_k: int = 30) -> list[Path]:
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    q_counter = Counter(query_tokens)
    candidates: list[tuple[float, Path]] = []
    for path in repo_path.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        rel_parts = path.relative_to(repo_path).parts
        if any(part.startswith(".") or part in {"build", ".gradle"} for part in rel_parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        score = _cosine(q_counter, Counter(_tokens(text)))
        if score > 0:
            candidates.append((score, path))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates[:top_k]]


def _tokens(text: str) -> list[str]:
    return [tok.lower() for tok in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text)]


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    common = set(a) & set(b)
    numerator = sum(a[t] * b[t] for t in common)
    denom_a = math.sqrt(sum(v * v for v in a.values()))
    denom_b = math.sqrt(sum(v * v for v in b.values()))
    if denom_a == 0 or denom_b == 0:
        return 0.0
    return numerator / (denom_a * denom_b)
