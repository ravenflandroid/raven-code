"""Program analysis tools for HDG construction.

Primary:  tree-sitter (pip-installable, no JVM) — parses Java/Kotlin AST,
          extracts methods and call edges.
Optional: Joern (external JVM tool) — full CPG; used if configured and present.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CPGMethod:
    name: str
    file: str
    line: int
    end_line: int = 0


@dataclass
class CPGCall:
    caller: str
    caller_file: str
    caller_line: int
    callee: str


@dataclass
class CPGResult:
    tool: str
    available: bool
    methods: list[CPGMethod] = field(default_factory=list)
    calls: list[CPGCall] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "available": self.available,
            "method_count": len(self.methods),
            "call_count": len(self.calls),
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# tree-sitter analyzer
# ---------------------------------------------------------------------------

_JAVA_METHOD_TYPES = frozenset({"method_declaration", "constructor_declaration"})
_JAVA_CALL_TYPES = frozenset({"method_invocation"})
_KOTLIN_FUNC_TYPES = frozenset({"function_declaration", "secondary_constructor", "anonymous_function"})
_KOTLIN_CALL_TYPES = frozenset({"call_expression"})
_NAME_TYPES = frozenset({"identifier", "simple_identifier"})


class TreeSitterAnalyzer:
    """Extracts CPG-like information from Java/Kotlin source using tree-sitter AST.

    Requires: pip install tree-sitter tree-sitter-java
    Optional: pip install tree-sitter-kotlin
    """

    def __init__(self) -> None:
        self._java_parser: Any = None
        self._kotlin_parser: Any = None
        self.available = False
        self.error: str | None = None
        self._init()

    def _init(self) -> None:
        try:
            from tree_sitter import Language, Parser as _Parser  # noqa: PLC0415
            import tree_sitter_java as _tsjava  # noqa: PLC0415

            lang = Language(_tsjava.language())
            self._java_parser = self._make_parser(lang)
        except Exception as exc:
            self.error = f"tree-sitter-java unavailable: {exc}"
            return

        try:
            from tree_sitter import Language, Parser as _Parser  # noqa: PLC0415
            import tree_sitter_kotlin as _tskotlin  # noqa: PLC0415

            lang = Language(_tskotlin.language())
            self._kotlin_parser = self._make_parser(lang)
        except Exception:
            pass  # Kotlin grammar is optional; Java analysis still works

        self.available = True

    @staticmethod
    def _make_parser(language: Any) -> Any:
        from tree_sitter import Parser as _Parser  # noqa: PLC0415

        try:
            return _Parser(language)
        except TypeError:
            # tree-sitter 0.21 uses set_language instead of constructor arg
            p = _Parser()
            p.set_language(language)
            return p

    # ------------------------------------------------------------------
    def analyze_files(self, source_files: list[Path]) -> CPGResult:
        started = time.perf_counter()
        if not self.available:
            return CPGResult(tool="tree-sitter", available=False, error=self.error)

        methods: list[CPGMethod] = []
        calls: list[CPGCall] = []

        for path in source_files:
            suffix = path.suffix.lower()
            if suffix == ".java" and self._java_parser:
                m, c = self._parse_file(path, self._java_parser, "java")
                methods.extend(m)
                calls.extend(c)
            elif suffix == ".kt" and self._kotlin_parser:
                m, c = self._parse_file(path, self._kotlin_parser, "kotlin")
                methods.extend(m)
                calls.extend(c)

        return CPGResult(
            tool="tree-sitter",
            available=True,
            methods=methods,
            calls=calls,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    # ------------------------------------------------------------------
    def _parse_file(self, path: Path, parser: Any, lang: str) -> tuple[list[CPGMethod], list[CPGCall]]:
        try:
            source = path.read_bytes()
            tree = parser.parse(source)
        except Exception:
            return [], []

        method_types = _JAVA_METHOD_TYPES if lang == "java" else _KOTLIN_FUNC_TYPES
        call_types = _JAVA_CALL_TYPES if lang == "java" else _KOTLIN_CALL_TYPES
        path_str = str(path)

        methods: list[CPGMethod] = []
        calls: list[CPGCall] = []
        method_stack: list[str] = []

        def _first_name(node: Any) -> str | None:
            for child in node.children:
                if child.type in _NAME_TYPES:
                    return source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
            return None

        def _call_name(node: Any) -> str | None:
            if lang == "java":
                # method_invocation: [..., identifier, argument_list]
                # The method name is the last identifier before the argument list
                for child in reversed(node.children):
                    if child.type == "identifier":
                        return source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
            else:
                # kotlin call_expression: navigation_expression(...) or simple_identifier(...)
                if not node.children:
                    return None
                first = node.children[0]
                if first.type == "navigation_expression":
                    for child in reversed(first.children):
                        if child.type in _NAME_TYPES:
                            return source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                elif first.type in _NAME_TYPES:
                    return source[first.start_byte:first.end_byte].decode("utf-8", errors="replace")
            return None

        _SKIP_KEYWORDS = frozenset({"if", "for", "while", "when", "switch", "catch", "try", "return"})

        def walk(node: Any) -> None:
            if node.type in method_types:
                name = _first_name(node)
                if name and name not in _SKIP_KEYWORDS:
                    line = node.start_point[0] + 1
                    methods.append(CPGMethod(
                        name=name,
                        file=path_str,
                        line=line,
                        end_line=node.end_point[0] + 1,
                    ))
                    method_stack.append(name)
                    for child in node.children:
                        walk(child)
                    method_stack.pop()
                    return

            if node.type in call_types and method_stack:
                callee = _call_name(node)
                if callee and callee not in _SKIP_KEYWORDS:
                    calls.append(CPGCall(
                        caller=method_stack[-1],
                        caller_file=path_str,
                        caller_line=node.start_point[0] + 1,
                        callee=callee,
                    ))

            for child in node.children:
                walk(child)

        walk(tree.root_node)
        return methods, calls


# ---------------------------------------------------------------------------
# Joern analyzer (optional, JVM-based)
# ---------------------------------------------------------------------------

class JoernAnalyzer:
    """Wraps joern-parse + joern-export to produce a CPG binary.

    Joern is not required; if the executables are missing the result reflects
    that and HDG construction continues with tree-sitter data only.
    """

    def __init__(self, joern_parse: str, joern_export: str) -> None:
        self.joern_parse = joern_parse
        self.joern_export = joern_export

    def run(self, repo_path: Path, output_dir: Path) -> dict:
        output_dir.mkdir(parents=True, exist_ok=True)
        cpg_path = output_dir / "joern_cpg.bin.zip"
        export_dir = output_dir / "joern_export"
        started = time.perf_counter()
        result: dict[str, Any] = {
            "available": False,
            "cpg_path": None,
            "export_dir": None,
            "duration_ms": 0,
            "error": None,
        }
        try:
            subprocess.run(
                [self.joern_parse, str(repo_path), "--output", str(cpg_path)],
                cwd=repo_path,
                text=True,
                capture_output=True,
                check=True,
                timeout=900,
            )
            subprocess.run(
                [self.joern_export, str(cpg_path), "--repr", "cpg14", "--out", str(export_dir)],
                cwd=repo_path,
                text=True,
                capture_output=True,
                check=True,
                timeout=900,
            )
            result.update({
                "available": True,
                "cpg_path": str(cpg_path),
                "export_dir": str(export_dir),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            })
        except FileNotFoundError:
            result["error"] = f"joern-parse not found at {self.joern_parse!r}"
            result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        except Exception as exc:
            result["error"] = str(exc)
            result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class PAToolRunner:
    """Runs all available PA tools and writes pa_tools.json."""

    def __init__(
        self,
        repo_path: Path,
        output_dir: Path,
        joern_parse: str = "joern-parse",
        joern_export: str = "joern-export",
    ) -> None:
        self.repo_path = repo_path
        self.output_dir = output_dir
        self._ts = TreeSitterAnalyzer()
        self._joern = JoernAnalyzer(joern_parse, joern_export)

    def run(self, source_files: list[Path]) -> dict:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        code_files = [p for p in source_files if p.suffix.lower() in {".kt", ".java"}]

        ts_result = self._ts.analyze_files(code_files) if code_files else CPGResult(
            tool="tree-sitter", available=False, error="No Kotlin/Java files selected."
        )

        joern_result = self._joern.run(self.repo_path, self.output_dir) if code_files else {
            "available": False,
            "error": "No Kotlin/Java files selected.",
        }

        combined = {
            "tree_sitter": ts_result.to_dict(),
            "joern": joern_result,
            # Serialise full method/call lists for builder.py to consume
            "_tree_sitter_methods": [asdict(m) for m in ts_result.methods],
            "_tree_sitter_calls": [asdict(c) for c in ts_result.calls],
        }
        self._write_result(combined)
        return combined

    def _write_result(self, result: dict) -> None:
        (self.output_dir / "pa_tools.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
