from __future__ import annotations

import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree

from raven.hdg.graph import HDGEdge, HDGNode, HeterogeneousDataFlowGraph, evidence, rel, stable_id
from raven.hdg.pa_tools import PAToolRunner
from raven.models import ActionSequence, ReplayResult


ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
SOURCE_EXTS = {".kt", ".java", ".xml"}
UPDATE_HINTS = ("setText", "setVisibility", "setEnabled", "setChecked", "show", "hide", "notifyDataSetChanged")
CALLBACK_HINTS = (
    "onCreate",
    "onStart",
    "onResume",
    "onPause",
    "onStop",
    "onDestroy",
    "onClick",
    "onLongClick",
    "onTextChanged",
    "afterTextChanged",
    "onConfigurationChanged",
)


class HDGBuilder:
    def __init__(
        self,
        repo_path: Path,
        expansion_bound: int = 2,
        max_files: int = 120,
        joern_parse: str = "joern-parse",
        joern_export: str = "joern-export",
    ):
        self.repo_path = repo_path
        self.expansion_bound = expansion_bound
        self.max_files = max_files
        self.joern_parse = joern_parse
        self.joern_export = joern_export

    def build(
        self,
        seed_files: list[Path],
        *,
        action_sequence: ActionSequence | None = None,
        replay: ReplayResult | None = None,
    ) -> HeterogeneousDataFlowGraph:
        candidate = HeterogeneousDataFlowGraph(repo_root=str(self.repo_path), expansion_bound=self.expansion_bound)
        files = self._expand_files(seed_files)
        pa_result = PAToolRunner(
            self.repo_path,
            self.repo_path / ".raven_pa",
            self.joern_parse,
            self.joern_export,
        ).run(files)
        ts_methods = pa_result.get("_tree_sitter_methods", [])
        ts_calls = pa_result.get("_tree_sitter_calls", [])
        ts_info = pa_result.get("tree_sitter", {})
        candidate.notes.append(
            f"tree-sitter: available={ts_info.get('available')}, "
            f"methods={ts_info.get('method_count', 0)}, "
            f"calls={ts_info.get('call_count', 0)}, "
            f"error={ts_info.get('error')}"
        )
        joern_info = pa_result.get("joern", {})
        candidate.notes.append(
            f"joern: available={joern_info.get('available')}, error={joern_info.get('error')}"
        )

        self._add_ui_actions(candidate, action_sequence, replay)
        self._add_runtime_ui(candidate, replay)

        for path in files:
            if path.name == "AndroidManifest.xml":
                self._add_manifest(candidate, path)
            elif path.suffix.lower() == ".xml":
                self._add_xml(candidate, path)
            elif path.suffix.lower() in {".kt", ".java"}:
                self._add_code(candidate, path)

        # Enrich with tree-sitter CPG: accurate method nodes + CALLS edges
        self._apply_tree_sitter_cpg(candidate, ts_methods, ts_calls)

        self._connect_android_layers(candidate)
        self._mark_observed_from_logs(candidate, replay)
        self._record_external_tool_status(candidate)
        filtered = candidate.induced_by_coverage_expansion(self.expansion_bound)
        return filtered

    def _expand_files(self, seed_files: list[Path]) -> list[Path]:
        if seed_files:
            normalized = [p if p.is_absolute() else self.repo_path / p for p in seed_files]
            existing = [p for p in normalized if p.exists()]
            extra = self._nearby_android_files(existing)
            return sorted(set(existing + extra))[: self.max_files]
        return [
            p
            for p in self.repo_path.rglob("*")
            if p.is_file() and p.suffix.lower() in SOURCE_EXTS and not _is_ignored(p, self.repo_path)
        ][: self.max_files]

    def _nearby_android_files(self, files: list[Path]) -> list[Path]:
        out: list[Path] = []
        manifests = list(self.repo_path.rglob("AndroidManifest.xml"))
        out.extend(manifests)
        res = self.repo_path / "app" / "src" / "main" / "res"
        if res.exists():
            out.extend([p for p in res.rglob("*.xml") if "layout" in p.parts or "values" in p.parts])
        return out[: max(20, self.max_files // 2)]

    def _add_manifest(self, graph: HeterogeneousDataFlowGraph, path: Path) -> None:
        manifest_file = rel(path, self.repo_path)
        try:
            root = ElementTree.fromstring(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception as exc:
            graph.notes.append(f"Manifest parse failed for {manifest_file}: {exc}")
            return

        package_name = root.attrib.get("package", "")
        manifest_id = stable_id("XMLNODE", manifest_file, "manifest")
        graph.add_node(
            HDGNode(
                id=manifest_id,
                type="XMLNODE",
                label="AndroidManifest",
                file=manifest_file,
                properties={"package": package_name, "tag": "manifest"},
                evidence=[evidence("manifest_parse", "Android manifest root", file=manifest_file)],
            )
        )
        graph.mark_observed(manifest_id)
        for element in root.iter():
            tag = element.tag.split("}")[-1]
            if tag not in {"activity", "service", "receiver", "provider", "application", "uses-permission"}:
                continue
            attrs = _attrs(element)
            name = attrs.get("name") or tag
            node_id = stable_id("XMLNODE", manifest_file, tag, name)
            graph.add_node(
                HDGNode(
                    id=node_id,
                    type="XMLNODE",
                    label=name,
                    file=manifest_file,
                    properties={"tag": tag, **attrs},
                    evidence=[evidence("manifest_parse", f"Manifest component <{tag}>", file=manifest_file)],
                )
            )
            graph.mark_observed(node_id)
            graph.add_edge(HDGEdge(source=manifest_id, target=node_id, type="DEFINES", evidence=[evidence("manifest_parse", "Manifest declares component", file=manifest_file)]))
            if tag in {"activity", "service", "receiver", "provider"}:
                cb_label = f"framework_launch:{tag}:{name}"
                cb_id = stable_id("FRAMEWORKCALLBACK", cb_label)
                graph.add_node(
                    HDGNode(
                        id=cb_id,
                        type="FRAMEWORKCALLBACK",
                        label=cb_label,
                        properties={"component": tag, "class": name, "package": package_name},
                        evidence=[evidence("manifest_parse", f"Framework can instantiate {tag} {name}", file=manifest_file)],
                    )
                )
                graph.add_edge(HDGEdge(source=node_id, target=cb_id, type="TRIGGERS", evidence=[evidence("manifest_parse", "Manifest component is framework entrypoint", file=manifest_file)]))
            for key, value in attrs.items():
                if value.startswith("@"):
                    res_id = stable_id("RESOURCE", _resource_label(value, key))
                    graph.add_node(
                        HDGNode(
                            id=res_id,
                            type="RESOURCE",
                            label=_resource_label(value, key),
                            file=manifest_file,
                            properties={"attribute": key, "raw_value": value},
                            evidence=[evidence("manifest_resource_resolution", f"{key}={value}", file=manifest_file)],
                        )
                    )
                    graph.add_edge(HDGEdge(source=node_id, target=res_id, type="RESOLVESTO", evidence=[evidence("manifest_resource_resolution", "Manifest attribute resolves to resource", file=manifest_file)]))

    def _add_ui_actions(
        self,
        graph: HeterogeneousDataFlowGraph,
        sequence: ActionSequence | None,
        replay: ReplayResult | None,
    ) -> None:
        actions = sequence.actions if sequence else []
        if not actions and replay:
            for entry in replay.action_history:
                raw = entry.get("action") or {"type": entry.get("type")}
                node_id = stable_id("UIACTION", len(graph.nodes), raw)
                graph.add_node(
                    HDGNode(
                        id=node_id,
                        type="UIACTION",
                        label=str(raw.get("type", "action")).upper(),
                        properties=raw,
                        evidence=[evidence("emulator_action_history", "Action executed during replay")],
                    )
                )
                graph.mark_observed(node_id)
            return

        for index, action in enumerate(actions, start=1):
            node_id = stable_id("UIACTION", index, action.type.value)
            graph.add_node(
                HDGNode(
                    id=node_id,
                    type="UIACTION",
                    label=action.type.value.upper(),
                    properties=action.model_dump(mode="json"),
                    evidence=[evidence("llm_action_sequence", action.rationale or "Predicted executable action")],
                )
            )
            graph.mark_observed(node_id)

    def _add_runtime_ui(self, graph: HeterogeneousDataFlowGraph, replay: ReplayResult | None) -> None:
        if not replay:
            return
        action_nodes = [node for node in graph.nodes if node.type == "UIACTION"]
        for snap_index, snapshot in enumerate(replay.ui_hierarchy_paths, start=1):
            if not snapshot.exists():
                continue
            try:
                root = ElementTree.fromstring(snapshot.read_text(encoding="utf-8", errors="ignore"))
            except Exception as exc:
                graph.notes.append(f"UI hierarchy parse failed for {snapshot}: {exc}")
                continue
            for node in root.iter("node"):
                attrs = dict(node.attrib)
                resource_id = attrs.get("resource-id") or attrs.get("content-desc") or attrs.get("text") or attrs.get("class", "widget")
                widget_id = stable_id("WIDGET", resource_id, attrs.get("bounds", ""), snap_index)
                graph.add_node(
                    HDGNode(
                        id=widget_id,
                        type="WIDGET",
                        label=resource_id,
                        properties=attrs,
                        evidence=[evidence("ui_hierarchy_snapshot", f"Observed in {snapshot.name}", file=str(snapshot))],
                    )
                )
                graph.mark_observed(widget_id)
                if action_nodes:
                    source = action_nodes[min(snap_index - 1, len(action_nodes) - 1)]
                    graph.add_edge(
                        HDGEdge(
                            source=source.id,
                            target=widget_id,
                            type="ACTSON",
                            evidence=[evidence("emulator_replay", f"Action {snap_index} followed by hierarchy snapshot {snapshot.name}")],
                        )
                    )
                for prop in ("text", "enabled", "checked", "selected", "focused", "visible-to-user"):
                    if prop in attrs and attrs[prop] not in {"", "false"}:
                        state_id = stable_id("UISTATE", widget_id, prop, attrs[prop])
                        graph.add_node(
                            HDGNode(
                                id=state_id,
                                type="UISTATE",
                                label=f"{prop}={attrs[prop]}",
                                properties={"property": prop, "value": attrs[prop]},
                                evidence=[evidence("ui_hierarchy_snapshot", f"Runtime property from {snapshot.name}", file=str(snapshot))],
                            )
                        )
                        graph.mark_observed(state_id)
                        graph.add_edge(HDGEdge(source=widget_id, target=state_id, type="DEFINES", evidence=[evidence("ui_hierarchy_snapshot", "Widget owns observed UI state")]))

    def _add_xml(self, graph: HeterogeneousDataFlowGraph, path: Path) -> None:
        xml_file = rel(path, self.repo_path)
        try:
            root = ElementTree.fromstring(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception as exc:
            graph.notes.append(f"XML parse failed for {xml_file}: {exc}")
            return
        for index, element in enumerate(root.iter(), start=1):
            attrs = _attrs(element)
            tag = element.tag.split("}")[-1]
            android_id = attrs.get("id")
            xml_id = stable_id("XMLNODE", xml_file, android_id or tag, index)
            graph.add_node(
                HDGNode(
                    id=xml_id,
                    type="XMLNODE",
                    label=android_id or tag,
                    file=xml_file,
                    properties={"tag": tag, **attrs},
                    evidence=[evidence("xml_parse", f"Layout node <{tag}>", file=xml_file)],
                )
            )
            for key, value in attrs.items():
                if value.startswith("@") or key in {"id", "text", "style"}:
                    res_id = stable_id("RESOURCE", _resource_label(value, key))
                    graph.add_node(
                        HDGNode(
                            id=res_id,
                            type="RESOURCE",
                            label=_resource_label(value, key),
                            file=xml_file,
                            properties={"attribute": key, "raw_value": value},
                            evidence=[evidence("android_resource_resolution", f"{key}={value}", file=xml_file)],
                        )
                    )
                    graph.add_edge(HDGEdge(source=xml_id, target=res_id, type="RESOLVESTO", evidence=[evidence("xml_parse", f"XML attribute {key} resolves to resource", file=xml_file)]))
            callback = attrs.get("onClick") or attrs.get("onLongClick")
            if callback:
                cb_id = stable_id("FRAMEWORKCALLBACK", callback)
                graph.add_node(HDGNode(id=cb_id, type="FRAMEWORKCALLBACK", label=callback, evidence=[evidence("xml_event_attribute", f"android:onClick={callback}", file=xml_file)]))
                graph.add_edge(HDGEdge(source=xml_id, target=cb_id, type="TRIGGERS", evidence=[evidence("xml_event_attribute", "XML event attribute triggers callback", file=xml_file)]))

    def _add_code(self, graph: HeterogeneousDataFlowGraph, path: Path) -> None:
        source_file = rel(path, self.repo_path)
        text = path.read_text(encoding="utf-8", errors="ignore")
        method_type = "KOTLINMETHOD" if path.suffix.lower() == ".kt" else "JAVAMETHOD"
        stmt_type = "KOTLINSTMT" if path.suffix.lower() == ".kt" else "JAVASTMT"
        methods: list[tuple[str, int, int]] = []

        method_re = r"\b(?:fun\s+|(?:public|private|protected|static|final|override|\s)+[\w<>\[\]?]+\s+)([A-Za-z_][A-Za-z0-9_]*)\s*\("
        for match in re.finditer(method_re, text):
            name = match.group(1)
            if name in {"if", "for", "while", "switch", "catch", "return"}:
                continue
            line = _line(text, match.start())
            method_id = stable_id(method_type, source_file, name, line)
            methods.append((method_id, line, match.start()))
            graph.add_node(
                HDGNode(
                    id=method_id,
                    type=method_type,
                    label=name,
                    file=source_file,
                    line=line,
                    evidence=[evidence("joern_or_source_parse", f"Method {name}", file=source_file, line=line)],
                )
            )
            if name in CALLBACK_HINTS or name.startswith("on"):
                cb_id = stable_id("FRAMEWORKCALLBACK", name)
                graph.add_node(HDGNode(id=cb_id, type="FRAMEWORKCALLBACK", label=name, evidence=[evidence("android_callback_convention", f"Callback-like method {name}", file=source_file, line=line)]))
                graph.add_edge(HDGEdge(source=cb_id, target=method_id, type="TRIGGERS", evidence=[evidence("android_callback_convention", "Framework callback invokes implementation", file=source_file, line=line)]))

        for line_no, line_text in enumerate(text.splitlines(), start=1):
            stripped = line_text.strip()
            if not stripped or stripped.startswith("//"):
                continue
            if "R." in stripped or any(hint in stripped for hint in UPDATE_HINTS):
                stmt_id = stable_id(stmt_type, source_file, line_no, stripped[:60])
                graph.add_node(
                    HDGNode(
                        id=stmt_id,
                        type=stmt_type,
                        label=stripped[:140],
                        file=source_file,
                        line=line_no,
                        evidence=[evidence("source_parse", "Relevant resource/UI statement", file=source_file, line=line_no)],
                    )
                )
                owner = _nearest_method(methods, line_no)
                if owner:
                    graph.add_edge(HDGEdge(source=owner, target=stmt_id, type="CONTROLS", evidence=[evidence("source_parse", "Statement inside method", file=source_file, line=line_no)]))
                for match in re.finditer(r"R\.(layout|id|string|drawable|menu|style)\.([A-Za-z0-9_]+)", stripped):
                    res_label = f"{match.group(1)}.{match.group(2)}"
                    res_id = stable_id("RESOURCE", res_label)
                    graph.add_node(HDGNode(id=res_id, type="RESOURCE", label=res_label, evidence=[evidence("source_parse", f"Resource reference {res_label}", file=source_file, line=line_no)]))
                    graph.add_edge(HDGEdge(source=stmt_id, target=res_id, type="USES", evidence=[evidence("source_parse", "Statement uses Android resource", file=source_file, line=line_no)]))
                if any(hint in stripped for hint in UPDATE_HINTS):
                    state_id = stable_id("UISTATE", source_file, line_no, "updated")
                    graph.add_node(HDGNode(id=state_id, type="UISTATE", label="updated_by_statement", file=source_file, line=line_no, evidence=[evidence("source_parse", "Statement likely mutates UI state", file=source_file, line=line_no, confidence=0.65)]))
                    graph.add_edge(HDGEdge(source=stmt_id, target=state_id, type="UPDATES", evidence=[evidence("source_parse", "UI setter/update method", file=source_file, line=line_no, confidence=0.65)]))

    def _apply_tree_sitter_cpg(
        self,
        graph: HeterogeneousDataFlowGraph,
        ts_methods: list[dict],
        ts_calls: list[dict],
    ) -> None:
        """Overlay tree-sitter AST results: upsert accurate method nodes and add CALLS edges."""
        if not ts_methods and not ts_calls:
            return

        method_id_map: dict[tuple[str, str], str] = {}

        for m in ts_methods:
            path = Path(m["file"])
            source_file = rel(path, self.repo_path)
            is_kotlin = path.suffix.lower() == ".kt"
            m_type = "KOTLINMETHOD" if is_kotlin else "JAVAMETHOD"
            method_id = stable_id(m_type, source_file, m["name"], m["line"])
            method_id_map[(m["name"], source_file)] = method_id
            graph.add_node(
                HDGNode(
                    id=method_id,
                    type=m_type,
                    label=m["name"],
                    file=source_file,
                    line=m["line"],
                    evidence=[evidence("tree_sitter_ast", f"AST method {m['name']}", file=source_file, line=m["line"])],
                )
            )
            if m["name"] in CALLBACK_HINTS or m["name"].startswith("on"):
                cb_id = stable_id("FRAMEWORKCALLBACK", m["name"])
                graph.add_node(HDGNode(
                    id=cb_id,
                    type="FRAMEWORKCALLBACK",
                    label=m["name"],
                    evidence=[evidence("android_callback_convention", f"Callback-like {m['name']}", file=source_file, line=m["line"])],
                ))
                graph.add_edge(HDGEdge(
                    source=cb_id,
                    target=method_id,
                    type="TRIGGERS",
                    evidence=[evidence("android_callback_convention", "Framework callback invokes implementation", file=source_file, line=m["line"])],
                ))

        # Build a name-only index for cross-file call resolution
        name_to_ids: dict[str, list[str]] = {}
        for (name, _), mid in method_id_map.items():
            name_to_ids.setdefault(name, []).append(mid)

        for c in ts_calls:
            caller_path = Path(c["caller_file"])
            caller_rel = rel(caller_path, self.repo_path)
            caller_id = method_id_map.get((c["caller"], caller_rel))
            if not caller_id:
                continue
            for callee_id in name_to_ids.get(c["callee"], []):
                graph.add_edge(HDGEdge(
                    source=caller_id,
                    target=callee_id,
                    type="CALLS",
                    evidence=[evidence(
                        "tree_sitter_ast",
                        f"Call {c['caller']} -> {c['callee']}",
                        file=caller_rel,
                        line=c["caller_line"],
                    )],
                ))

    def _connect_android_layers(self, graph: HeterogeneousDataFlowGraph) -> None:
        xml_nodes = [node for node in graph.nodes if node.type == "XMLNODE"]
        widgets = [node for node in graph.nodes if node.type == "WIDGET"]
        resources = [node for node in graph.nodes if node.type == "RESOURCE"]
        methods = {node.label: node for node in graph.nodes if node.type in {"KOTLINMETHOD", "JAVAMETHOD"}}
        callbacks = [node for node in graph.nodes if node.type == "FRAMEWORKCALLBACK"]

        for widget in widgets:
            wid = _last_resource_segment(str(widget.label))
            for xml in xml_nodes:
                xid = _last_resource_segment(str(xml.label))
                if wid and xid and wid == xid:
                    graph.add_edge(HDGEdge(source=widget.id, target=xml.id, type="DECLAREDIN", evidence=[evidence("runtime_static_join", "Runtime widget id matches XML id", confidence=0.8)]))

        for resource in resources:
            if resource.label.startswith("layout."):
                layout = resource.label.split(".", 1)[1]
                for xml in xml_nodes:
                    if xml.file and Path(xml.file).stem == layout:
                        graph.add_edge(HDGEdge(source=resource.id, target=xml.id, type="RESOLVESTO", evidence=[evidence("android_resource_resolution", "layout resource resolves to XML node")]))
            if resource.label.startswith("id."):
                rid = resource.label.split(".", 1)[1]
                for xml in xml_nodes:
                    if _last_resource_segment(str(xml.label)) == rid:
                        graph.add_edge(HDGEdge(source=resource.id, target=xml.id, type="BINDSTO", evidence=[evidence("android_resource_resolution", "id resource binds to XML node")]))

        for callback in callbacks:
            method = methods.get(callback.label)
            if method:
                graph.add_edge(HDGEdge(source=callback.id, target=method.id, type="TRIGGERS", evidence=[evidence("callback_resolution", "Callback name resolves to method")]))

    def _mark_observed_from_logs(self, graph: HeterogeneousDataFlowGraph, replay: ReplayResult | None) -> None:
        if not replay or not replay.logcat_path.exists():
            return
        text = replay.logcat_path.read_text(encoding="utf-8", errors="ignore")
        for node in graph.nodes:
            if node.type in {"KOTLINMETHOD", "JAVAMETHOD", "FRAMEWORKCALLBACK", "RESOURCE"} and node.label in text:
                graph.mark_observed(node.id)
                node.evidence.append(evidence("emulator_logcat", f"Label {node.label} appears in logcat", file=str(replay.logcat_path), confidence=0.85))
            if node.file and Path(node.file).stem in text:
                graph.mark_observed(node.id)
                node.evidence.append(evidence("emulator_logcat", f"File stem {Path(node.file).stem} appears in logcat", file=str(replay.logcat_path), confidence=0.75))

    def _record_external_tool_status(self, graph: HeterogeneousDataFlowGraph) -> None:
        for tool in [self.joern_parse, self.joern_export]:
            try:
                subprocess.run([tool, "--help"], capture_output=True, text=True, timeout=5)
                graph.notes.append(f"{tool} available for conservative candidate CPG enrichment.")
            except Exception:
                graph.notes.append(f"{tool} not available; used source-derived conservative candidate graph.")


def _attrs(element: ElementTree.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in element.attrib.items():
        clean = key.removeprefix(ANDROID_NS)
        out[clean] = value
    return out


def _resource_label(value: str, key: str) -> str:
    clean = value.removeprefix("@+").removeprefix("@")
    clean = clean.replace("/", ".")
    if clean == value and key == "id":
        clean = f"id.{_last_resource_segment(value)}"
    return clean


def _last_resource_segment(value: str) -> str:
    value = value.split("/")[-1].split(".")[-1]
    return value.replace("@+id/", "").replace("@id/", "").strip()


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _nearest_method(methods: list[tuple[str, int, int]], line_no: int) -> str | None:
    earlier = [(node_id, line) for node_id, line, _ in methods if line <= line_no]
    if not earlier:
        return None
    earlier.sort(key=lambda item: item[1], reverse=True)
    return earlier[0][0]


def _is_ignored(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts
    return any(part in {"build", ".gradle"} or part.startswith(".") for part in parts)
