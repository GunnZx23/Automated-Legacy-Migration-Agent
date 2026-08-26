"""Deterministic, evidence-bearing MuleSoft dependency discovery.

The analyzer reads one immutable :class:`~legacy_migration_agent.core.workspace.TreeSnapshot`
and never follows references back to the live filesystem.  It models the Mule
configuration relationships that constrain a migration plan: flows and
subflows, connector configurations, application and Maven properties,
DataWeave modules and variables, MUnit tests, and Maven plugins/dependencies.
Missing and expression-driven targets remain explicit unresolved evidence.

This is deliberately a conservative source analyzer, not a replacement for a
Mule runtime.  XML declarations containing DTDs or entities fail closed before
``ElementTree`` is invoked.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml

from legacy_migration_agent.contracts import Platform, validate_relative_path
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.workspace import TreeSnapshot, snapshot_tree
from legacy_migration_agent.graphs.graph_contracts import (
    GRAPH_SCHEMA_VERSION,
    DependencyEdge,
    DependencyGraph,
    DependencyNode,
    EdgeKind,
    NodeKind,
    ParserWarning,
    SourceDigest,
    SourceProvenance,
    WarningCode,
)

MULESOFT_ANALYZER_VERSION = "mulesoft-static-v1"

_XML_GUARD = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_PROPERTY_REFERENCE = re.compile(r"\$\{\s*([^{}]+?)\s*\}")
_VARIABLE_REFERENCE = re.compile(
    r"\b(?:flowVars|vars)\s*(?:\.\s*([A-Za-z_]\w*)|\[\s*['\"]([^'\"]+)['\"]\s*\])"
)
_DATAWEAVE_IMPORT = re.compile(r"(?m)^\s*import\b[^\n]*?\bfrom\s+([A-Za-z_]\w*(?:::[A-Za-z_]\w*)+)")
_DYNAMIC_EXPRESSION = re.compile(r"#\[|\$\{")
_MAX_TEXT_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class _File:
    relative_path: str
    text: str
    sha256: str


@dataclass
class _NodeRecord:
    node_id: str
    kind: NodeKind
    name: str
    metadata_paths: set[str] = field(default_factory=set)
    resolved: bool = True
    external: bool = False


@dataclass
class _EdgeRecord:
    source_id: str
    target_id: str
    kind: EdgeKind
    symbol: str | None
    resolved: bool
    provenance: dict[tuple[str, int, str, str], SourceProvenance] = field(default_factory=dict)


def _identifier(prefix: str, name: str) -> str:
    return f"mule:{prefix}:{name.casefold()}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _excerpt(text: str, start: int, end: int) -> str:
    compact = " ".join(text[max(0, start) : max(start + 1, end)].strip().split())
    return (compact or "<empty source>")[:500]


def _provenance(
    file: _File,
    needle: str,
    parser: str,
    *,
    fallback_offset: int = 0,
) -> SourceProvenance:
    offset = file.text.find(needle)
    if offset < 0:
        offset = fallback_offset
    return SourceProvenance(
        path=file.relative_path,
        line=_line_number(file.text, offset),
        excerpt=_excerpt(file.text, offset, offset + max(1, len(needle))),
        parser=parser,
    )


def _is_supported_path(relative_path: str) -> bool:
    name = relative_path.rsplit("/", 1)[-1].casefold()
    return name == "pom.xml" or name.endswith(
        (".xml", ".dwl", ".properties", ".yaml", ".yml", ".json")
    )


def _resource_suffix(relative_path: str) -> str | None:
    marker = "src/main/resources/"
    lowered = relative_path.casefold()
    marker_index = lowered.find(marker)
    if marker_index < 0:
        return None
    return relative_path[marker_index + len(marker) :]


def _file_from_snapshot(relative_path: str, payload: bytes) -> _File:
    if len(payload) > _MAX_TEXT_BYTES:
        raise PolicyViolation(f"MuleSoft source exceeds the parser size limit: {relative_path}")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyViolation(f"MuleSoft source must be UTF-8: {relative_path}") from exc
    return _File(
        relative_path=relative_path,
        text=text,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _flatten_yaml(value: Any, prefix: str = "") -> tuple[str, ...]:
    """Return deterministic dotted scalar keys without interpreting values."""

    if isinstance(value, dict):
        flattened: list[str] = []
        for raw_key in sorted(value, key=lambda item: str(item)):
            key = str(raw_key).strip()
            if not key:
                continue
            child_prefix = f"{prefix}.{key}" if prefix else key
            flattened.extend(_flatten_yaml(value[raw_key], child_prefix))
        return tuple(flattened)
    if isinstance(value, list):
        return (prefix,) if prefix else ()
    return (prefix,) if prefix else ()


class _MuleSoftGraphBuilder:
    def __init__(self, source_snapshot: TreeSnapshot, base_revision: str):
        self.source_snapshot = source_snapshot
        self.snapshot_paths = frozenset(entry.path for entry in source_snapshot.entries)
        self.snapshot_directories = frozenset(source_snapshot.directories)
        if base_revision != source_snapshot.revision:
            raise PolicyViolation(
                "MuleSoft dependency graph base_revision is stale for the captured source"
            )
        self.base_revision = source_snapshot.revision
        self.files: dict[str, _File] = {}
        self.nodes: dict[str, _NodeRecord] = {}
        self.path_nodes: dict[str, set[str]] = {}
        self.edges: dict[tuple[str, str, EdgeKind, str | None, bool], _EdgeRecord] = {}
        self.warnings: dict[tuple[WarningCode, str, int, str], ParserWarning] = {}
        self.xml_roots: dict[str, ElementTree.Element] = {}
        self.flows: dict[str, str] = {}
        self.configs: dict[str, str] = {}
        self.properties: dict[str, str] = {}
        self.variables: dict[str, str] = {}
        self.dataweave_resources: dict[str, str] = {}
        self.resources: dict[str, str] = {}
        self.flow_elements: dict[str, tuple[_File, ElementTree.Element]] = {}
        self.config_elements: dict[str, tuple[_File, ElementTree.Element]] = {}
        self.test_elements: dict[str, tuple[_File, ElementTree.Element]] = {}
        self.suites_by_path: dict[str, tuple[str, ...]] = {}

    def build(self, entry_paths: Iterable[str]) -> DependencyGraph:
        entries = tuple(sorted({validate_relative_path(value) for value in entry_paths}))
        if not entries:
            raise ValueError("at least one MuleSoft entry path is required")
        self._inventory()
        self._parse_all()
        selected = self._scope_nodes(self._entry_nodes(entries))
        return self._freeze(entries, selected)

    def _inventory(self) -> None:
        for entry in self.source_snapshot.entries:
            if _is_supported_path(entry.path):
                self.files[entry.path] = _file_from_snapshot(entry.path, entry.content)
        self._index_resources()
        self._index_property_files()
        self._index_dataweave()
        self._index_xml()
        self._index_maven()

    def _add_node(
        self,
        node_id: str,
        kind: NodeKind,
        name: str,
        paths: Iterable[str] = (),
        *,
        resolved: bool = True,
        external: bool = False,
    ) -> str:
        path_set = set(paths)
        existing = self.nodes.get(node_id)
        if existing is not None:
            existing.metadata_paths.update(path_set)
            existing.resolved = existing.resolved or resolved
            existing.external = existing.external or external
        else:
            self.nodes[node_id] = _NodeRecord(
                node_id=node_id,
                kind=kind,
                name=name,
                metadata_paths=path_set,
                resolved=resolved,
                external=external,
            )
        for path in path_set:
            self.path_nodes.setdefault(path, set()).add(node_id)
        return node_id

    def _add_edge(
        self,
        source_id: str,
        target_id: str,
        kind: EdgeKind,
        provenance: SourceProvenance,
        *,
        symbol: str | None = None,
        resolved: bool | None = None,
    ) -> None:
        relation_resolved = self.nodes[target_id].resolved if resolved is None else resolved
        key = (source_id, target_id, kind, symbol, relation_resolved)
        record = self.edges.get(key)
        if record is None:
            record = _EdgeRecord(
                source_id=source_id,
                target_id=target_id,
                kind=kind,
                symbol=symbol,
                resolved=relation_resolved,
            )
            self.edges[key] = record
        provenance_key = (
            provenance.path,
            provenance.line,
            provenance.excerpt,
            provenance.parser,
        )
        record.provenance[provenance_key] = provenance

    def _warn(
        self,
        code: WarningCode,
        message: str,
        provenance: SourceProvenance,
    ) -> None:
        self.warnings[(code, provenance.path, provenance.line, message)] = ParserWarning(
            code=code,
            message=message,
            provenance=provenance,
            unresolved=True,
        )

    def _unresolved_node(self, category: str, name: str) -> str:
        return self._add_node(
            _identifier(f"unresolved:{category}", name),
            NodeKind.UNRESOLVED,
            name,
            resolved=False,
        )

    def _index_resources(self) -> None:
        for path in sorted(self.files):
            lowered = path.casefold()
            resource_suffix = _resource_suffix(path)
            if resource_suffix is not None:
                resource_name = resource_suffix
                node_id = self._add_node(
                    _identifier("metadata", path),
                    NodeKind.METADATA_FILE,
                    resource_name,
                    (path,),
                )
                self.resources.setdefault(resource_name.casefold(), node_id)
            elif "/src/main/app/" in lowered and path.endswith(".properties"):
                resource_name = path.rsplit("/", 1)[-1]
                node_id = self._add_node(
                    _identifier("metadata", path),
                    NodeKind.METADATA_FILE,
                    resource_name,
                    (path,),
                )
                self.resources.setdefault(resource_name.casefold(), node_id)

    def _index_property_files(self) -> None:
        for path, file in sorted(self.files.items()):
            lowered = path.casefold()
            keys: tuple[str, ...]
            if lowered.endswith(".properties"):
                parsed: list[str] = []
                for raw_line in file.text.splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith(("#", "!")):
                        continue
                    match = re.match(r"([^:=\s]+)\s*(?:[:=]|\s)\s*", line)
                    if match is not None:
                        parsed.append(match.group(1))
                keys = tuple(parsed)
            elif lowered.endswith((".yaml", ".yml")):
                try:
                    document = yaml.safe_load(file.text)
                except yaml.YAMLError as exc:
                    provenance = _provenance(file, file.text[:200], "mule_properties")
                    self._warn(
                        WarningCode.MALFORMED_SOURCE,
                        f"Mule property YAML could not be parsed: {exc}",
                        provenance,
                    )
                    continue
                keys = _flatten_yaml(document)
            else:
                continue
            for key in sorted(set(keys), key=str.casefold):
                node_id = self._add_node(
                    _identifier("property", key),
                    NodeKind.MULE_PROPERTY,
                    key,
                    (path,),
                )
                self.properties[key.casefold()] = node_id

    def _index_dataweave(self) -> None:
        for path in sorted(self.files):
            if not path.casefold().endswith(".dwl"):
                continue
            resource_name = _resource_suffix(path) or path
            node_id = self._add_node(
                _identifier("dataweave", resource_name),
                NodeKind.DATAWEAVE_MODULE,
                resource_name,
                (path,),
            )
            aliases = {
                resource_name.casefold(),
                resource_name.removesuffix(".dwl").replace("/", "::").casefold(),
            }
            for alias in aliases:
                self.dataweave_resources.setdefault(alias, node_id)

    def _secure_xml(self, file: _File) -> ElementTree.Element | None:
        forbidden = _XML_GUARD.search(file.text)
        if forbidden is not None:
            provenance = _provenance(file, forbidden.group(0), "secure_xml")
            raise PolicyViolation(
                "MuleSoft XML DTD/entity declarations are forbidden: "
                f"{provenance.path}:{provenance.line}"
            )
        try:
            return ElementTree.fromstring(file.text)
        except ElementTree.ParseError as exc:
            offset = 0
            if exc.position:
                lines = file.text.splitlines(keepends=True)
                offset = sum(len(line) for line in lines[: max(0, exc.position[0] - 1)])
            provenance = _provenance(
                file,
                file.text[offset : offset + 200],
                "mule_xml",
                fallback_offset=offset,
            )
            self._warn(
                WarningCode.MALFORMED_SOURCE,
                f"MuleSoft XML could not be parsed: {exc}",
                provenance,
            )
            return None

    def _index_xml(self) -> None:
        for path, file in sorted(self.files.items()):
            if not path.casefold().endswith(".xml") or path.casefold().endswith("pom.xml"):
                continue
            root = self._secure_xml(file)
            if root is None:
                self._add_node(
                    _identifier("metadata", path),
                    NodeKind.METADATA_FILE,
                    Path(path).name,
                    (path,),
                    resolved=False,
                )
                continue
            self.xml_roots[path] = root
            suites: list[str] = []
            for child in root:
                local = _local_name(child.tag)
                namespace = _namespace(child.tag).casefold()
                name = child.attrib.get("name", "").strip()
                if local in {"flow", "sub-flow"} and name:
                    kind = NodeKind.MULE_FLOW if local == "flow" else NodeKind.MULE_SUBFLOW
                    node_id = self._add_node(_identifier("flow", name), kind, name, (path,))
                    self.flows[name.casefold()] = node_id
                    self.flow_elements[node_id] = (file, child)
                elif local == "test" and "munit" in namespace and name:
                    node_id = self._add_node(
                        _identifier("munit-test", name),
                        NodeKind.MUNIT_TEST,
                        name,
                        (path,),
                    )
                    self.test_elements[node_id] = (file, child)
                elif local == "config" and "munit" in namespace and name:
                    node_id = self._add_node(
                        _identifier("munit-suite", name),
                        NodeKind.MUNIT_SUITE,
                        name,
                        (path,),
                    )
                    suites.append(node_id)
                elif (local.endswith("-config") or local == "configuration") and name:
                    node_id = self._add_node(
                        _identifier("config", name),
                        NodeKind.MULE_CONFIGURATION,
                        name,
                        (path,),
                    )
                    self.configs[name.casefold()] = node_id
                    self.config_elements[node_id] = (file, child)
                elif local == "configuration-properties":
                    resource = child.attrib.get("file", "").strip()
                    synthetic_name = f"configuration-properties:{resource or path}"
                    node_id = self._add_node(
                        _identifier("config", synthetic_name),
                        NodeKind.MULE_CONFIGURATION,
                        synthetic_name,
                        (path,),
                    )
                    self.config_elements[node_id] = (file, child)
            self.suites_by_path[path] = tuple(sorted(suites))
            self._index_variable_definitions(file, root)

    def _index_variable_definitions(self, file: _File, root: ElementTree.Element) -> None:
        for element in root.iter():
            local = _local_name(element.tag)
            name = ""
            if local in {"set-variable", "set-session-variable"}:
                name = element.attrib.get("variableName", "").strip()
            elif (
                local in {"variable", "invocation-property"}
                and "munit" in _namespace(element.tag).casefold()
            ):
                name = element.attrib.get("key", "").strip()
            if not name or _DYNAMIC_EXPRESSION.search(name):
                continue
            node_id = self._add_node(
                _identifier("variable", name),
                NodeKind.MULE_VARIABLE,
                name,
                (file.relative_path,),
            )
            self.variables[name.casefold()] = node_id

    def _index_maven(self) -> None:
        for path, file in sorted(self.files.items()):
            if path.rsplit("/", 1)[-1].casefold() != "pom.xml":
                continue
            root = self._secure_xml(file)
            if root is None:
                self._add_node(
                    _identifier("maven-project", path),
                    NodeKind.MAVEN_PROJECT,
                    Path(path).parent.name or "Maven project",
                    (path,),
                    resolved=False,
                )
                continue
            self.xml_roots[path] = root
            artifact_id = self._direct_text(root, "artifactId") or Path(path).parent.name
            project_id = self._add_node(
                _identifier("maven-project", path),
                NodeKind.MAVEN_PROJECT,
                artifact_id,
                (path,),
            )
            properties = next(
                (child for child in root if _local_name(child.tag) == "properties"), None
            )
            if properties is not None:
                for child in properties:
                    key = _local_name(child.tag)
                    node_id = self._add_node(
                        _identifier("property", key),
                        NodeKind.MULE_PROPERTY,
                        key,
                        (path,),
                    )
                    self.properties[key.casefold()] = node_id
            for container_kind, edge_kind, node_kind, prefix in (
                ("plugins", EdgeKind.MAVEN_PLUGIN, NodeKind.MAVEN_PLUGIN, "maven-plugin"),
                (
                    "dependencies",
                    EdgeKind.MAVEN_DEPENDENCY,
                    NodeKind.MAVEN_DEPENDENCY,
                    "maven-dependency",
                ),
            ):
                item_name = "plugin" if container_kind == "plugins" else "dependency"
                for container in (
                    element for element in root.iter() if _local_name(element.tag) == container_kind
                ):
                    for item in container:
                        if _local_name(item.tag) != item_name:
                            continue
                        group = self._direct_text(item, "groupId") or "<inherited>"
                        artifact = self._direct_text(item, "artifactId")
                        if not artifact:
                            continue
                        coordinate = f"{group}:{artifact}"
                        target_id = self._add_node(
                            _identifier(prefix, coordinate),
                            node_kind,
                            coordinate,
                            (path,),
                            external=True,
                        )
                        provenance = _provenance(file, artifact, "maven")
                        self._add_edge(
                            project_id,
                            target_id,
                            edge_kind,
                            provenance,
                            symbol=coordinate,
                        )
                        self._parse_property_references(target_id, file, self._element_text(item))

    @staticmethod
    def _direct_text(element: ElementTree.Element, local_name: str) -> str:
        for child in element:
            if _local_name(child.tag) == local_name:
                return (child.text or "").strip()
        return ""

    @staticmethod
    def _element_text(element: ElementTree.Element) -> str:
        chunks: list[str] = []
        for descendant in element.iter():
            chunks.extend(descendant.attrib.values())
            if descendant.text:
                chunks.append(descendant.text)
        return "\n".join(chunks)

    def _parse_all(self) -> None:
        for source_id, (file, element) in sorted(self.config_elements.items()):
            self._parse_config(source_id, file, element)
        for source_id, (file, element) in sorted(self.flow_elements.items()):
            self._parse_flow_like(source_id, file, element, munit=False)
        for source_id, (file, element) in sorted(self.test_elements.items()):
            self._parse_flow_like(source_id, file, element, munit=True)
            for suite_id in self.suites_by_path.get(file.relative_path, ()):
                provenance = _provenance(file, self.nodes[source_id].name, "munit")
                self._add_edge(
                    suite_id,
                    source_id,
                    EdgeKind.MUNIT_SUITE_TEST,
                    provenance,
                    symbol=self.nodes[source_id].name,
                )
        for node_id, node in sorted(self.nodes.items()):
            if node.kind is not NodeKind.DATAWEAVE_MODULE:
                continue
            path = next(iter(sorted(node.metadata_paths)))
            self._parse_dataweave(node_id, self.files[path], self.files[path].text)

    def _parse_config(
        self,
        source_id: str,
        file: _File,
        element: ElementTree.Element,
    ) -> None:
        self._parse_property_references(source_id, file, self._element_text(element))
        if _local_name(element.tag) == "configuration-properties":
            resource = element.attrib.get("file", "").strip()
            if not resource:
                return
            provenance = _provenance(file, resource, "mule_xml")
            target_id, resolved = self._resolve_resource(resource, provenance)
            self._add_edge(
                source_id,
                target_id,
                EdgeKind.CONFIGURATION_PROPERTIES_REFERENCE,
                provenance,
                symbol=resource,
                resolved=resolved,
            )

    def _parse_flow_like(
        self,
        source_id: str,
        file: _File,
        element: ElementTree.Element,
        *,
        munit: bool,
    ) -> None:
        for child in element.iter():
            local = _local_name(child.tag)
            if local == "flow-ref":
                name = child.attrib.get("name", "").strip()
                if name:
                    provenance = _provenance(file, name, "munit" if munit else "mule_xml")
                    target_id, resolved = self._resolve_named(self.flows, "flow", name, provenance)
                    edge_kind = EdgeKind.MUNIT_FLOW_REFERENCE if munit else EdgeKind.FLOW_REFERENCE
                    self._add_edge(
                        source_id,
                        target_id,
                        edge_kind
                        if resolved or not _DYNAMIC_EXPRESSION.search(name)
                        else EdgeKind.DYNAMIC_REFERENCE,
                        provenance,
                        symbol=name,
                        resolved=resolved,
                    )
            config_name = child.attrib.get("config-ref", "").strip()
            if config_name:
                provenance = _provenance(file, config_name, "mule_xml")
                target_id, resolved = self._resolve_named(
                    self.configs, "configuration", config_name, provenance
                )
                edge_kind = (
                    EdgeKind.HTTP_LISTENER_CONFIG_REFERENCE
                    if _namespace(child.tag).casefold().endswith("/http") and local == "listener"
                    else EdgeKind.CONNECTOR_CONFIG_REFERENCE
                )
                if not resolved and _DYNAMIC_EXPRESSION.search(config_name):
                    edge_kind = EdgeKind.DYNAMIC_REFERENCE
                self._add_edge(
                    source_id,
                    target_id,
                    edge_kind,
                    provenance,
                    symbol=config_name,
                    resolved=resolved,
                )
            resource = child.attrib.get("resource", "").strip()
            if resource and (resource.casefold().endswith(".dwl") or "dwl" in resource.casefold()):
                provenance = _provenance(file, resource, "mule_xml")
                target_id, resolved = self._resolve_dataweave(resource, provenance)
                self._add_edge(
                    source_id,
                    target_id,
                    EdgeKind.DATAWEAVE_MODULE_REFERENCE
                    if resolved or not _DYNAMIC_EXPRESSION.search(resource)
                    else EdgeKind.DYNAMIC_REFERENCE,
                    provenance,
                    symbol=resource,
                    resolved=resolved,
                )
            text = self._element_text(child)
            self._parse_property_references(source_id, file, text)
            self._parse_dataweave(source_id, file, text)

    def _parse_property_references(self, source_id: str, file: _File, text: str) -> None:
        for match in _PROPERTY_REFERENCE.finditer(text):
            symbol = match.group(0)
            key = match.group(1).strip()
            provenance = _provenance(file, symbol, "mule_property")
            target_id = self.properties.get(key.casefold())
            resolved = target_id is not None and self.nodes[target_id].resolved
            if target_id is None:
                target_id = self._unresolved_node("property", key)
                self._warn(
                    WarningCode.UNRESOLVED_REFERENCE,
                    f"Mule property reference could not be resolved: {key}",
                    provenance,
                )
            self._add_edge(
                source_id,
                target_id,
                EdgeKind.PROPERTY_REFERENCE,
                provenance,
                symbol=symbol,
                resolved=resolved,
            )

    def _parse_dataweave(self, source_id: str, file: _File, text: str) -> None:
        for match in _VARIABLE_REFERENCE.finditer(text):
            name = match.group(1) or match.group(2)
            provenance = _provenance(file, match.group(0), "dataweave")
            target_id = self.variables.get(name.casefold())
            resolved = target_id is not None and self.nodes[target_id].resolved
            if target_id is None:
                target_id = self._unresolved_node("variable", name)
                self._warn(
                    WarningCode.UNRESOLVED_REFERENCE,
                    f"DataWeave variable reference could not be resolved: {name}",
                    provenance,
                )
            self._add_edge(
                source_id,
                target_id,
                EdgeKind.DATAWEAVE_VARIABLE_REFERENCE,
                provenance,
                symbol=match.group(0),
                resolved=resolved,
            )
        for match in _DATAWEAVE_IMPORT.finditer(text):
            module = match.group(1)
            provenance = _provenance(file, module, "dataweave")
            target_id, resolved = self._resolve_dataweave(module, provenance)
            self._add_edge(
                source_id,
                target_id,
                EdgeKind.DATAWEAVE_MODULE_REFERENCE,
                provenance,
                symbol=module,
                resolved=resolved,
            )
        for dynamic in re.finditer(r"\breadUrl\s*\(\s*(?!['\"])", text):
            symbol = dynamic.group(0).strip()
            provenance = _provenance(file, dynamic.group(0), "dataweave")
            target_id = self._unresolved_node("dataweave-resource", symbol)
            self._warn(
                WarningCode.DYNAMIC_REFERENCE,
                "dynamic DataWeave readUrl target cannot be resolved statically",
                provenance,
            )
            self._add_edge(
                source_id,
                target_id,
                EdgeKind.DYNAMIC_REFERENCE,
                provenance,
                symbol=symbol,
                resolved=False,
            )

    def _resolve_named(
        self,
        index: dict[str, str],
        category: str,
        name: str,
        provenance: SourceProvenance,
    ) -> tuple[str, bool]:
        if _DYNAMIC_EXPRESSION.search(name):
            target_id = self._unresolved_node(category, name)
            self._warn(
                WarningCode.DYNAMIC_REFERENCE,
                f"dynamic Mule {category} reference cannot be resolved statically: {name}",
                provenance,
            )
            return target_id, False
        resolved_id: str | None = index.get(name.casefold())
        if resolved_id is not None and self.nodes[resolved_id].resolved:
            return resolved_id, True
        target_id = resolved_id or self._unresolved_node(category, name)
        self._warn(
            WarningCode.UNRESOLVED_REFERENCE,
            f"Mule {category} reference could not be resolved: {name}",
            provenance,
        )
        return target_id, False

    def _resolve_dataweave(self, resource: str, provenance: SourceProvenance) -> tuple[str, bool]:
        if _DYNAMIC_EXPRESSION.search(resource):
            return self._resolve_named({}, "DataWeave module", resource, provenance)
        normalized = resource.removeprefix("classpath://").lstrip("/")
        try:
            normalized = validate_relative_path(normalized)
        except ValueError:
            target_id = self._unresolved_node("dataweave-module", resource)
            self._warn(
                WarningCode.UNRESOLVED_REFERENCE,
                f"DataWeave module reference is not a safe relative resource: {resource}",
                provenance,
            )
            return target_id, False
        aliases = (
            normalized.casefold(),
            normalized.removesuffix(".dwl").replace("/", "::").casefold(),
        )
        resolved_id: str | None = next(
            (
                self.dataweave_resources[alias]
                for alias in aliases
                if alias in self.dataweave_resources
            ),
            None,
        )
        if resolved_id is not None:
            return resolved_id, True
        target_id = self._unresolved_node("dataweave-module", resource)
        self._warn(
            WarningCode.UNRESOLVED_REFERENCE,
            f"DataWeave module reference could not be resolved: {resource}",
            provenance,
        )
        return target_id, False

    def _resolve_resource(self, resource: str, provenance: SourceProvenance) -> tuple[str, bool]:
        if _DYNAMIC_EXPRESSION.search(resource):
            return self._resolve_named({}, "resource", resource, provenance)
        normalized = resource.removeprefix("classpath://").lstrip("/")
        try:
            normalized = validate_relative_path(normalized)
        except ValueError:
            target_id = self._unresolved_node("resource", resource)
            self._warn(
                WarningCode.UNRESOLVED_REFERENCE,
                f"Mule resource reference is not a safe relative path: {resource}",
                provenance,
            )
            return target_id, False
        resolved_id: str | None = self.resources.get(normalized.casefold())
        if resolved_id is not None:
            return resolved_id, True
        target_id = self._unresolved_node("resource", resource)
        self._warn(
            WarningCode.UNRESOLVED_REFERENCE,
            f"Mule resource reference could not be resolved: {resource}",
            provenance,
        )
        return target_id, False

    def _entry_nodes(self, entries: tuple[str, ...]) -> set[str]:
        seeds: set[str] = set()
        for entry in entries:
            matched: set[str] = set()
            if entry in self.snapshot_paths:
                matched.update(self.path_nodes.get(entry, set()))
            elif entry in self.snapshot_directories:
                prefix = f"{entry}/"
                for path, node_ids in self.path_nodes.items():
                    if path.startswith(prefix):
                        matched.update(node_ids)
            else:
                raise FileNotFoundError(f"MuleSoft entry path does not exist: {entry}")
            if not matched:
                raise ValueError(f"unsupported MuleSoft source entry path: {entry}")
            seeds.update(matched)
        if not seeds:
            raise ValueError("entry paths do not select any supported MuleSoft source")
        return seeds

    def _scope_nodes(self, seeds: set[str]) -> set[str]:
        selected = set(seeds)
        changed = True
        while changed:
            changed = False
            for edge in self.edges.values():
                if edge.source_id in selected and edge.target_id not in selected:
                    selected.add(edge.target_id)
                    changed = True
            for edge in self.edges.values():
                source = self.nodes[edge.source_id]
                if (
                    edge.target_id in selected
                    and source.kind in {NodeKind.MUNIT_TEST, NodeKind.MUNIT_SUITE}
                    and edge.source_id not in selected
                ):
                    selected.add(edge.source_id)
                    changed = True
        return selected

    def _freeze(self, entries: tuple[str, ...], selected: set[str]) -> DependencyGraph:
        nodes = tuple(
            DependencyNode(
                node_id=record.node_id,
                kind=record.kind,
                name=record.name,
                metadata_paths=tuple(sorted(record.metadata_paths)),
                resolved=record.resolved,
                external=record.external,
            )
            for record in sorted(
                (self.nodes[node_id] for node_id in selected),
                key=lambda value: (value.kind.value, value.name.casefold(), value.node_id),
            )
        )
        selected_paths = {path for node in nodes for path in node.metadata_paths}
        source_digests = tuple(
            SourceDigest(path=path, sha256=self.files[path].sha256)
            for path in sorted(selected_paths)
            if path in self.files
        )
        edge_records = tuple(
            edge
            for edge in self.edges.values()
            if edge.source_id in selected and edge.target_id in selected
        )
        edges = tuple(
            DependencyEdge(
                source_id=edge.source_id,
                target_id=edge.target_id,
                kind=edge.kind,
                symbol=edge.symbol,
                resolved=edge.resolved,
                provenance=tuple(
                    sorted(
                        edge.provenance.values(),
                        key=lambda value: (
                            value.path,
                            value.line,
                            value.excerpt,
                            value.parser,
                        ),
                    )
                ),
            )
            for edge in sorted(
                edge_records,
                key=lambda value: (
                    value.source_id,
                    value.target_id,
                    value.kind.value,
                    value.symbol or "",
                    value.resolved,
                ),
            )
        )
        selected_source_paths = selected_paths | {
            provenance.path for edge in edges for provenance in edge.provenance
        }
        warnings = tuple(
            warning
            for warning in sorted(
                self.warnings.values(),
                key=lambda value: (
                    value.provenance.path,
                    value.provenance.line,
                    value.code.value,
                    value.message,
                ),
            )
            if warning.provenance.path in selected_source_paths
        )
        return DependencyGraph(
            platform=Platform.MULESOFT,
            base_revision=self.base_revision,
            entry_paths=entries,
            source_digests=source_digests,
            nodes=nodes,
            edges=edges,
            warnings=warnings,
        )


def build_mulesoft_dependency_graph(
    repository_root: Path | str,
    entry_paths: Iterable[str],
    base_revision: str,
) -> DependencyGraph:
    """Build a deterministic MuleSoft graph for one captured source revision."""

    source_snapshot = snapshot_tree(repository_root)
    return _MuleSoftGraphBuilder(source_snapshot, base_revision).build(entry_paths)


__all__ = [
    "GRAPH_SCHEMA_VERSION",
    "MULESOFT_ANALYZER_VERSION",
    "build_mulesoft_dependency_graph",
]
