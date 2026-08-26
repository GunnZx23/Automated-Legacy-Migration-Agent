"""Deterministic, evidence-bearing Salesforce dependency discovery.

This module deliberately implements a conservative static inventory rather
than pretending to be a complete Apex compiler.  Every discovered relationship
has source provenance.  Constructs that cannot be resolved statically (for
example ``Database.query`` and ``Type.forName``) become unresolved graph
evidence and parser warnings; they are never silently discarded.

The public entry point is :func:`build_salesforce_dependency_graph`. Reads are
limited to one immutable snapshot of the explicitly supplied repository root;
symlinks and special files fail closed before parsing.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

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

SALESFORCE_ANALYZER_VERSION = "salesforce-static-v1"


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


@dataclass(frozen=True)
class _SoqlToken:
    value: str
    start: int
    end: int
    parents: tuple[int, ...]


@dataclass(frozen=True)
class _SoqlIssue:
    message: str
    start: int
    end: int
    code: WarningCode = WarningCode.MALFORMED_SOURCE


@dataclass(frozen=True)
class _SoqlScope:
    select: _SoqlToken
    end: int
    from_token: _SoqlToken | None
    object_token: _SoqlToken | None
    ambiguous: bool = False


# This is intentionally a small platform vocabulary for the capstone fixture,
# not a claim to be a substitute for an org describe.  Repository custom
# metadata extends it.  Unknown explicit object/field references are surfaced.
_STANDARD_SCHEMA: dict[str, tuple[str, ...]] = {
    "Account": (
        "Id",
        "Name",
        "AccountNumber",
        "BillingCity",
        "BillingCountry",
        "BillingState",
        "Industry",
        "OwnerId",
        "Phone",
        "Type",
        "Website",
    ),
    "Contact": (
        "Id",
        "AccountId",
        "Email",
        "FirstName",
        "LastName",
        "Name",
        "OwnerId",
        "Phone",
        "Title",
    ),
}

_RELATIONSHIP_TO_OBJECT = {
    "accounts": "Account",
    "contacts": "Contact",
}

_PLATFORM_APEX_TYPES = {
    "apexpages.standardcontroller",
    "apexpages.standardsetcontroller",
    "apexpages.message",
    "aurahandledexception",
    "blob",
    "boolean",
    "date",
    "datetime",
    "decimal",
    "exception",
    "http",
    "httprequest",
    "httpresponse",
    "id",
    "integer",
    "jsonparser",
    "list",
    "long",
    "map",
    "pageReference".casefold(),
    "set",
    "selectoption",
    "string",
    "test",
}

_SOQL_KEYWORDS = {
    "and",
    "asc",
    "by",
    "desc",
    "false",
    "first",
    "for",
    "from",
    "group",
    "having",
    "in",
    "last",
    "limit",
    "not",
    "null",
    "nulls",
    "offset",
    "or",
    "order",
    "select",
    "true",
    "where",
    "with",
}


def _identifier(prefix: str, name: str) -> str:
    return f"sf:{prefix}:{name.casefold()}"


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _excerpt(text: str, start: int, end: int) -> str:
    value = " ".join(text[start:end].strip().split())
    if not value:
        value = "<empty source>"
    return value[:500]


def _provenance(file: _File, start: int, end: int, parser: str) -> SourceProvenance:
    return SourceProvenance(
        path=file.relative_path,
        line=_line_number(file.text, start),
        excerpt=_excerpt(file.text, start, end),
        parser=parser,
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _is_salesforce_metadata_path(relative_path: str) -> bool:
    parts = relative_path.split("/")
    name = parts[-1]
    part_set = set(parts)
    if name == "sfdx-project.json" or name == "package.xml":
        return True
    if "classes" in part_set and (name.endswith(".cls") or name.endswith(".cls-meta.xml")):
        return True
    if "pages" in part_set and (name.endswith(".page") or name.endswith(".page-meta.xml")):
        return True
    if "permissionsets" in part_set and name.endswith(".permissionset-meta.xml"):
        return True
    if "objects" in part_set and (
        name.endswith(".object-meta.xml") or name.endswith(".field-meta.xml")
    ):
        return True
    if "lwc" in part_set:
        lwc_index = parts.index("lwc")
        return len(parts) > lwc_index + 2
    return False


def _file_from_snapshot(relative_path: str, payload: bytes) -> _File:
    """Decode one already-captured snapshot entry without touching the live tree."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Salesforce metadata must be UTF-8: {relative_path}") from exc
    return _File(
        relative_path=relative_path,
        text=text,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


class _SalesforceGraphBuilder:
    def __init__(self, source_snapshot: TreeSnapshot, base_revision: str):
        self.source_snapshot = source_snapshot
        self.snapshot_paths = frozenset(entry.path for entry in source_snapshot.entries)
        self.snapshot_directories = frozenset(source_snapshot.directories)
        if base_revision != source_snapshot.revision:
            raise PolicyViolation(
                "Salesforce dependency graph base_revision is stale for the captured source"
            )
        self.base_revision = source_snapshot.revision
        self.files: dict[str, _File] = {}
        self.nodes: dict[str, _NodeRecord] = {}
        self.path_nodes: dict[str, set[str]] = {}
        self.edges: dict[tuple[str, str, EdgeKind, str | None, bool], _EdgeRecord] = {}
        self.warnings: dict[tuple[WarningCode, str, int, str], ParserWarning] = {}
        self.apex: dict[str, str] = {}
        self.apex_methods: dict[str, set[str]] = {}
        self.pages: dict[str, str] = {}
        self.objects: dict[str, str] = {}
        self.fields: dict[tuple[str, str], str] = {}

    def build(self, entry_paths: Iterable[str]) -> DependencyGraph:
        entries = tuple(dict.fromkeys(validate_relative_path(value) for value in entry_paths))
        if not entries:
            raise ValueError("at least one Salesforce entry path is required")
        self._inventory()
        self._parse_all()
        seeds = self._entry_nodes(entries)
        selected = self._scope_nodes(seeds)
        return self._freeze(entries, selected)

    def _inventory(self) -> None:
        for entry in self.source_snapshot.entries:
            if not _is_salesforce_metadata_path(entry.path):
                continue
            file = _file_from_snapshot(entry.path, entry.content)
            self.files[file.relative_path] = file

        self._index_apex()
        self._index_pages()
        self._index_lwc()
        self._index_permission_sets()
        self._index_schema_metadata()
        self._index_generic_metadata()

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
        paths_set = set(paths)
        if node_id in self.nodes:
            existing = self.nodes[node_id]
            existing.metadata_paths.update(paths_set)
            existing.resolved = existing.resolved or resolved
            if existing.kind is NodeKind.APEX_CLASS and kind is NodeKind.APEX_TEST:
                existing.kind = NodeKind.APEX_TEST
            for path in paths_set:
                self.path_nodes.setdefault(path, set()).add(node_id)
            return node_id
        self.nodes[node_id] = _NodeRecord(
            node_id=node_id,
            kind=kind,
            name=name,
            metadata_paths=paths_set,
            resolved=resolved,
            external=external,
        )
        for path in paths_set:
            self.path_nodes.setdefault(path, set()).add(node_id)
        return node_id

    def _index_apex(self) -> None:
        source_files = {path: file for path, file in self.files.items() if path.endswith(".cls")}
        for path, file in sorted(source_files.items()):
            name = Path(path).name.removesuffix(".cls")
            directory = path.rsplit("/", 1)[0]
            meta_path = f"{directory}/{name}.cls-meta.xml"
            paths = [path]
            if meta_path in self.files:
                paths.append(meta_path)
            kind = (
                NodeKind.APEX_TEST
                if re.search(r"(?i)@isTest\b", file.text)
                else NodeKind.APEX_CLASS
            )
            node_id = self._add_node(_identifier("apex", name), kind, name, paths)
            self.apex[name.casefold()] = node_id
            self.apex_methods[name.casefold()] = self._apex_method_names(file.text)

        for path in sorted(self.files):
            if not path.endswith(".cls-meta.xml"):
                continue
            name = Path(path).name.removesuffix(".cls-meta.xml")
            if name.casefold() in self.apex:
                continue
            node_id = self._add_node(
                _identifier("apex", name),
                NodeKind.APEX_CLASS,
                name,
                (path,),
                resolved=False,
            )
            self.apex[name.casefold()] = node_id

    @staticmethod
    def _apex_method_names(text: str) -> set[str]:
        pattern = re.compile(
            r"(?im)^\s*(?:public|global|private|protected)\s+"
            r"(?:static\s+|virtual\s+|override\s+|testMethod\s+)*"
            r"[A-Za-z_][\w.<>,\[\]?\s]*?\s+([A-Za-z_]\w*)\s*\("
        )
        return {match.group(1).casefold() for match in pattern.finditer(text)}

    def _index_pages(self) -> None:
        for path in sorted(self.files):
            if not path.endswith(".page"):
                continue
            name = Path(path).name.removesuffix(".page")
            directory = path.rsplit("/", 1)[0]
            meta_path = f"{directory}/{name}.page-meta.xml"
            paths = [path]
            if meta_path in self.files:
                paths.append(meta_path)
            node_id = self._add_node(
                _identifier("page", name), NodeKind.VISUALFORCE_PAGE, name, paths
            )
            self.pages[name.casefold()] = node_id
        for path in sorted(self.files):
            if not path.endswith(".page-meta.xml"):
                continue
            name = Path(path).name.removesuffix(".page-meta.xml")
            if name.casefold() in self.pages:
                continue
            node_id = self._add_node(
                _identifier("page", name),
                NodeKind.VISUALFORCE_PAGE,
                name,
                (path,),
                resolved=False,
            )
            self.pages[name.casefold()] = node_id

    def _index_lwc(self) -> None:
        bundles: dict[str, list[str]] = {}
        for path in sorted(self.files):
            parts = path.split("/")
            if "lwc" not in parts:
                continue
            index = parts.index("lwc")
            if len(parts) <= index + 2:
                continue
            bundle = parts[index + 1]
            bundles.setdefault(bundle, []).append(path)
        for bundle, paths in sorted(bundles.items()):
            has_controller = any(path.endswith(f"/{bundle}.js") for path in paths)
            self._add_node(
                _identifier("lwc", bundle),
                NodeKind.LWC_COMPONENT,
                bundle,
                paths,
                resolved=has_controller,
            )

    def _index_permission_sets(self) -> None:
        for path in sorted(self.files):
            if not path.endswith(".permissionset-meta.xml"):
                continue
            name = Path(path).name.removesuffix(".permissionset-meta.xml")
            self._add_node(
                _identifier("permission_set", name),
                NodeKind.PERMISSION_SET,
                name,
                (path,),
            )

    def _index_schema_metadata(self) -> None:
        for path in sorted(self.files):
            parts = path.split("/")
            if "objects" not in parts:
                continue
            index = parts.index("objects")
            if len(parts) <= index + 2:
                continue
            object_name = parts[index + 1]
            if path.endswith(".object-meta.xml"):
                node_id = self._add_node(
                    _identifier("sobject", object_name),
                    NodeKind.SCHEMA_OBJECT,
                    object_name,
                    (path,),
                )
                self.objects[object_name.casefold()] = node_id
            elif path.endswith(".field-meta.xml"):
                field_name = Path(path).name.removesuffix(".field-meta.xml")
                object_id = self.objects.get(object_name.casefold())
                if object_id is None:
                    object_id = self._add_node(
                        _identifier("sobject", object_name),
                        NodeKind.SCHEMA_OBJECT,
                        object_name,
                    )
                    self.objects[object_name.casefold()] = object_id
                field_id = self._add_node(
                    _identifier("field", f"{object_name}.{field_name}"),
                    NodeKind.SCHEMA_FIELD,
                    f"{object_name}.{field_name}",
                    (path,),
                )
                self.fields[(object_name.casefold(), field_name.casefold())] = field_id

    def _index_generic_metadata(self) -> None:
        for path in sorted(self.files):
            if path in self.path_nodes:
                continue
            name = Path(path).name
            if name == "sfdx-project.json" or name == "package.xml":
                self._add_node(
                    _identifier("metadata", path),
                    NodeKind.METADATA_FILE,
                    name,
                    (path,),
                )

    def _parse_all(self) -> None:
        for _, node_id in sorted(self.pages.items()):
            node = self.nodes[node_id]
            page_path = next((path for path in node.metadata_paths if path.endswith(".page")), None)
            if page_path is not None:
                self._parse_page(node_id, self.files[page_path])
        for _, node_id in sorted(self.apex.items()):
            node = self.nodes[node_id]
            class_path = next((path for path in node.metadata_paths if path.endswith(".cls")), None)
            if class_path is not None:
                self._parse_apex(node_id, self.files[class_path])
        for node in sorted(self.nodes.values(), key=lambda value: value.node_id):
            if node.kind is NodeKind.LWC_COMPONENT:
                self._parse_lwc(node.node_id, node)
            elif node.kind is NodeKind.PERMISSION_SET:
                path = next(iter(sorted(node.metadata_paths)))
                self._parse_permission_set(node.node_id, self.files[path])

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
        key = (code, provenance.path, provenance.line, message)
        self.warnings[key] = ParserWarning(
            code=code,
            message=message,
            provenance=provenance,
            unresolved=True,
        )

    def _unresolved_node(self, category: str, name: str) -> str:
        node_id = _identifier(f"unresolved:{category}", name)
        return self._add_node(
            node_id,
            NodeKind.UNRESOLVED,
            name,
            resolved=False,
        )

    def _resolve_apex(self, name: str, provenance: SourceProvenance) -> tuple[str, bool]:
        target = self.apex.get(name.casefold())
        if target is not None and self.nodes[target].resolved:
            return target, True
        target = target or self._unresolved_node("apex", name)
        self._warn(
            WarningCode.UNRESOLVED_REFERENCE,
            f"Apex class reference could not be resolved in repository metadata: {name}",
            provenance,
        )
        return target, False

    def _resolve_page(self, name: str, provenance: SourceProvenance) -> tuple[str, bool]:
        target = self.pages.get(name.casefold())
        if target is not None and self.nodes[target].resolved:
            return target, True
        target = target or self._unresolved_node("page", name)
        self._warn(
            WarningCode.UNRESOLVED_REFERENCE,
            f"Visualforce page reference could not be resolved in repository metadata: {name}",
            provenance,
        )
        return target, False

    def _resolve_object(self, name: str, provenance: SourceProvenance) -> tuple[str, str, bool]:
        canonical = _RELATIONSHIP_TO_OBJECT.get(name.casefold(), name)
        target = self.objects.get(canonical.casefold())
        if target is not None and self.nodes[target].resolved:
            return target, self.nodes[target].name, True
        standard_name = next(
            (value for value in _STANDARD_SCHEMA if value.casefold() == canonical.casefold()),
            None,
        )
        if standard_name is not None:
            target = self._add_node(
                _identifier("sobject", standard_name),
                NodeKind.SCHEMA_OBJECT,
                standard_name,
                resolved=True,
                external=True,
            )
            self.objects[standard_name.casefold()] = target
            return target, standard_name, True
        target = target or self._unresolved_node("sobject", canonical)
        self._warn(
            WarningCode.UNRESOLVED_REFERENCE,
            f"schema object reference could not be resolved: {name}",
            provenance,
        )
        return target, canonical, False

    def _resolve_field(
        self,
        object_name: str,
        field_name: str,
        provenance: SourceProvenance,
        *,
        warn_if_missing: bool,
    ) -> tuple[str | None, bool]:
        key = (object_name.casefold(), field_name.casefold())
        target = self.fields.get(key)
        if target is not None and self.nodes[target].resolved:
            return target, True
        standard_name = next(
            (value for value in _STANDARD_SCHEMA if value.casefold() == object_name.casefold()),
            None,
        )
        if standard_name is not None:
            canonical_field = next(
                (
                    value
                    for value in _STANDARD_SCHEMA[standard_name]
                    if value.casefold() == field_name.casefold()
                ),
                None,
            )
            if canonical_field is not None:
                field_id = self._add_node(
                    _identifier("field", f"{standard_name}.{canonical_field}"),
                    NodeKind.SCHEMA_FIELD,
                    f"{standard_name}.{canonical_field}",
                    resolved=True,
                    external=True,
                )
                self.fields[(standard_name.casefold(), canonical_field.casefold())] = field_id
                return field_id, True
        if not warn_if_missing:
            return None, False
        name = f"{object_name}.{field_name}"
        target = target or self._unresolved_node("field", name)
        self._warn(
            WarningCode.UNRESOLVED_REFERENCE,
            f"schema field reference could not be resolved: {name}",
            provenance,
        )
        return target, False

    def _parse_page(self, source_id: str, file: _File) -> None:
        opening = re.search(r"(?is)<apex:page\b(?P<attrs>[^>]*)>", file.text)
        if opening is None:
            provenance = _provenance(file, 0, min(len(file.text), 200), "visualforce")
            self._warn(
                WarningCode.MALFORMED_SOURCE,
                "Visualforce source has no apex:page opening element",
                provenance,
            )
            return
        attrs = opening.group("attrs")
        attrs_offset = opening.start("attrs")
        for attribute, kind in (
            ("controller", EdgeKind.VF_CONTROLLER),
            ("extensions", EdgeKind.VF_EXTENSION),
        ):
            match = re.search(rf"(?is)\b{attribute}\s*=\s*([\"'])(?P<value>.*?)\1", attrs)
            if match is None:
                continue
            for value in (part.strip() for part in match.group("value").split(",")):
                if not value:
                    continue
                start = attrs_offset + match.start("value")
                end = attrs_offset + match.end("value")
                provenance = _provenance(file, start, end, "visualforce")
                target, resolved = self._resolve_apex(value, provenance)
                self._add_edge(
                    source_id,
                    target,
                    kind,
                    provenance,
                    symbol=value,
                    resolved=resolved,
                )
        standard = re.search(r"(?is)\bstandardController\s*=\s*([\"'])(?P<value>.*?)\1", attrs)
        if standard is not None:
            start = attrs_offset + standard.start("value")
            end = attrs_offset + standard.end("value")
            provenance = _provenance(file, start, end, "visualforce")
            name = standard.group("value").strip()
            target, canonical, resolved = self._resolve_object(name, provenance)
            self._add_edge(
                source_id,
                target,
                EdgeKind.VF_STANDARD_CONTROLLER,
                provenance,
                symbol=canonical,
                resolved=resolved,
            )

    def _parse_apex(self, source_id: str, file: _File) -> None:
        self._parse_dynamic_apex(source_id, file)
        self._parse_static_soql(source_id, file)

        for match in re.finditer(
            r"\bnew\s+(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(",
            file.text,
        ):
            name = match.group("name")
            lowered = name.casefold()
            if (
                lowered in _PLATFORM_APEX_TYPES
                or lowered in self.objects
                or any(name.casefold() == standard.casefold() for standard in _STANDARD_SCHEMA)
            ):
                continue
            provenance = _provenance(file, match.start(), match.end(), "apex")
            target, resolved = self._resolve_apex(name, provenance)
            if target == source_id:
                continue
            self._add_edge(
                source_id,
                target,
                EdgeKind.APEX_CLASS_REFERENCE,
                provenance,
                symbol=name,
                resolved=resolved,
            )

        for match in re.finditer(
            r"\b(?:extends|implements)\s+(?P<names>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)",
            file.text,
        ):
            for name in (part.strip() for part in match.group("names").split(",")):
                if name.casefold() in _PLATFORM_APEX_TYPES:
                    continue
                provenance = _provenance(file, match.start(), match.end(), "apex")
                target, resolved = self._resolve_apex(name, provenance)
                if target == source_id:
                    continue
                self._add_edge(
                    source_id,
                    target,
                    EdgeKind.APEX_CLASS_REFERENCE,
                    provenance,
                    symbol=name,
                    resolved=resolved,
                )

        # Static Class.method calls are reliable class references only when the
        # class exists in the indexed repository.  Unknown uppercase qualifiers
        # may be namespaces or platform classes, so they are not guessed.
        for _, target in sorted(self.apex.items()):
            if target == source_id:
                continue
            display_name = self.nodes[target].name
            for match in re.finditer(
                rf"\b{re.escape(display_name)}\s*\.\s*[A-Za-z_]\w*\s*\(",
                file.text,
                flags=re.IGNORECASE,
            ):
                provenance = _provenance(file, match.start(), match.end(), "apex")
                self._add_edge(
                    source_id,
                    target,
                    EdgeKind.APEX_CLASS_REFERENCE,
                    provenance,
                    symbol=display_name,
                    resolved=self.nodes[target].resolved,
                )

        for match in re.finditer(r"\bPage\.(?P<name>[A-Za-z_]\w*)", file.text):
            name = match.group("name")
            provenance = _provenance(file, match.start(), match.end(), "apex")
            target, resolved = self._resolve_page(name, provenance)
            self._add_edge(
                source_id,
                target,
                EdgeKind.APEX_PAGE_REFERENCE,
                provenance,
                symbol=name,
                resolved=resolved,
            )

    def _parse_dynamic_apex(self, source_id: str, file: _File) -> None:
        patterns = (
            (
                re.compile(r"\bDatabase\s*\.\s*(?:query|countQuery)\s*\(", re.IGNORECASE),
                WarningCode.DYNAMIC_SOQL,
                "dynamic SOQL cannot be resolved statically",
                "dynamic_soql",
            ),
            (
                re.compile(r"\bType\s*\.\s*forName\s*\(", re.IGNORECASE),
                WarningCode.DYNAMIC_TYPE,
                "Type.forName dependency cannot be resolved statically",
                "dynamic_type",
            ),
        )
        for pattern, code, message, category in patterns:
            for match in pattern.finditer(file.text):
                provenance = _provenance(file, match.start(), match.end(), "apex")
                dynamic_name = f"{category}@{file.relative_path}:{provenance.line}"
                target = self._unresolved_node(category, dynamic_name)
                self._add_edge(
                    source_id,
                    target,
                    EdgeKind.DYNAMIC_REFERENCE,
                    provenance,
                    symbol=category,
                    resolved=False,
                )
                self._warn(code, message, provenance)

    @staticmethod
    def _scan_soql(
        body: str,
    ) -> tuple[tuple[_SoqlToken, ...], dict[int, int], tuple[_SoqlIssue, ...]]:
        """Tokenize enough SOQL structure to assign fields conservatively.

        Parenthesis ancestry is retained for every word token.  Quoted values
        and comments are deliberately opaque so keywords inside them cannot
        create false query scopes.
        """

        tokens: list[_SoqlToken] = []
        closures: dict[int, int] = {}
        issues: list[_SoqlIssue] = []
        parents: list[int] = []
        index = 0
        while index < len(body):
            character = body[index]
            if character in {"'", '"'}:
                quote = character
                quote_start = index
                index += 1
                while index < len(body):
                    if body[index] == "\\":
                        index += 2
                        continue
                    if body[index] == quote:
                        index += 1
                        break
                    index += 1
                else:
                    issues.append(
                        _SoqlIssue(
                            "static SOQL contains an unterminated quoted value",
                            quote_start,
                            len(body),
                        )
                    )
                continue
            if body.startswith("//", index):
                newline = body.find("\n", index + 2)
                index = len(body) if newline < 0 else newline + 1
                continue
            if body.startswith("/*", index):
                comment_start = index
                comment_end = body.find("*/", index + 2)
                if comment_end < 0:
                    issues.append(
                        _SoqlIssue(
                            "static SOQL contains an unterminated block comment",
                            comment_start,
                            len(body),
                        )
                    )
                    break
                index = comment_end + 2
                continue
            if character == "(":
                parents.append(index)
                index += 1
                continue
            if character == ")":
                if not parents:
                    issues.append(
                        _SoqlIssue(
                            "static SOQL contains an unmatched closing parenthesis",
                            index,
                            index + 1,
                        )
                    )
                else:
                    closures[parents.pop()] = index
                index += 1
                continue
            if character.isalpha() or character == "_":
                end = index + 1
                while end < len(body) and (body[end].isalnum() or body[end] == "_"):
                    end += 1
                tokens.append(
                    _SoqlToken(
                        value=body[index:end],
                        start=index,
                        end=end,
                        parents=tuple(parents),
                    )
                )
                index = end
                continue
            index += 1

        for opening in parents:
            issues.append(
                _SoqlIssue(
                    "static SOQL contains an unmatched opening parenthesis",
                    opening,
                    min(opening + 1, len(body)),
                )
            )
        return tuple(tokens), closures, tuple(issues)

    @staticmethod
    def _soql_scopes(
        body: str,
        tokens: tuple[_SoqlToken, ...],
        closures: dict[int, int],
    ) -> tuple[tuple[_SoqlScope, ...], tuple[_SoqlIssue, ...]]:
        """Pair each SELECT with its direct FROM and object token."""

        issues: list[_SoqlIssue] = []
        select_tokens = tuple(token for token in tokens if token.value.casefold() == "select")
        scopes: list[_SoqlScope] = []
        parent_counts: dict[tuple[int, ...], int] = {}
        for select in select_tokens:
            parent_counts[select.parents] = parent_counts.get(select.parents, 0) + 1

        for select in select_tokens:
            scope_end = closures.get(select.parents[-1], len(body)) if select.parents else len(body)
            direct_tokens = tuple(
                token
                for token in tokens
                if select.end <= token.start < scope_end and token.parents == select.parents
            )
            from_tokens = tuple(
                token for token in direct_tokens if token.value.casefold() == "from"
            )
            ambiguous = parent_counts[select.parents] > 1 or len(from_tokens) > 1
            if parent_counts[select.parents] > 1:
                issues.append(
                    _SoqlIssue(
                        "static SOQL has multiple SELECT clauses in one query scope",
                        select.start,
                        select.end,
                    )
                )
            if not from_tokens:
                issues.append(
                    _SoqlIssue(
                        "static SOQL SELECT has no direct FROM target",
                        select.start,
                        select.end,
                    )
                )
                scopes.append(_SoqlScope(select, scope_end, None, None, ambiguous=True))
                continue
            if len(from_tokens) > 1:
                issues.append(
                    _SoqlIssue(
                        "static SOQL has multiple FROM targets in one query scope",
                        from_tokens[1].start,
                        from_tokens[-1].end,
                    )
                )

            from_token = from_tokens[0]
            object_token = next(
                (token for token in direct_tokens if token.start >= from_token.end),
                None,
            )
            if object_token is None or object_token.value.casefold() in _SOQL_KEYWORDS:
                issues.append(
                    _SoqlIssue(
                        "static SOQL FROM has no resolvable object or child relationship target",
                        from_token.start,
                        from_token.end,
                    )
                )
                object_token = None
                ambiguous = True
            scopes.append(
                _SoqlScope(
                    select=select,
                    end=scope_end,
                    from_token=from_token,
                    object_token=object_token,
                    ambiguous=ambiguous,
                )
            )
        return tuple(scopes), tuple(issues)

    @staticmethod
    def _owning_soql_scope(
        token: _SoqlToken,
        scopes: tuple[_SoqlScope, ...],
    ) -> _SoqlScope | None:
        candidates = tuple(
            scope
            for scope in scopes
            if scope.select.start <= token.start < scope.end
            and token.parents[: len(scope.select.parents)] == scope.select.parents
        )
        if not candidates:
            return None
        return max(candidates, key=lambda scope: (len(scope.select.parents), scope.select.start))

    def _warn_soql_issue(
        self,
        file: _File,
        body_start: int,
        issue: _SoqlIssue,
    ) -> None:
        start = body_start + issue.start
        end = body_start + max(issue.end, issue.start + 1)
        provenance = _provenance(file, start, min(end, len(file.text)), "apex_soql")
        self._warn(issue.code, issue.message, provenance)

    def _parse_static_soql(self, source_id: str, file: _File) -> None:
        for query in re.finditer(r"(?is)\[(?P<body>\s*SELECT\b[^\]]+)\]", file.text):
            body = query.group("body")
            body_start = query.start("body")
            tokens, closures, scan_issues = self._scan_soql(body)
            scopes, scope_issues = self._soql_scopes(body, tokens, closures)
            for issue in (*scan_issues, *scope_issues):
                self._warn_soql_issue(file, body_start, issue)
            if not scopes:
                self._warn_soql_issue(
                    file,
                    body_start,
                    _SoqlIssue(
                        "static SOQL has no resolvable SELECT scope",
                        0,
                        len(body),
                    ),
                )
                continue

            opaque_field_scopes: set[_SoqlScope] = set()
            reported_opaque_scopes: set[tuple[_SoqlScope, str]] = set()
            for token in tokens:
                scope = self._owning_soql_scope(token, scopes)
                if scope is None:
                    continue
                construct: str | None = None
                message: str | None = None
                if token.value.casefold() == "typeof":
                    construct = "TYPEOF"
                    message = "static SOQL TYPEOF field ownership is not resolved by this analyzer"
                elif token.value.casefold() == "fields" and re.match(r"\s*\(", body[token.end :]):
                    construct = "FIELDS"
                    message = "static SOQL FIELDS() expansion cannot be enumerated statically"
                if construct is None or message is None:
                    continue
                opaque_field_scopes.add(scope)
                report_key = (scope, construct)
                if report_key in reported_opaque_scopes:
                    continue
                reported_opaque_scopes.add(report_key)
                self._warn_soql_issue(
                    file,
                    body_start,
                    _SoqlIssue(
                        message,
                        token.start,
                        token.end,
                        code=WarningCode.DYNAMIC_REFERENCE,
                    ),
                )

            resolved_scopes: dict[_SoqlScope, tuple[str, bool]] = {}
            object_positions = {
                scope.object_token.start for scope in scopes if scope.object_token is not None
            }
            for scope in scopes:
                if scope.ambiguous or scope.object_token is None:
                    continue
                object_name = scope.object_token.value
                start = body_start + scope.object_token.start
                end = body_start + scope.object_token.end
                provenance = _provenance(file, start, end, "apex_soql")
                target, canonical, resolved = self._resolve_object(object_name, provenance)
                resolved_scopes[scope] = (canonical, resolved)
                self._add_edge(
                    source_id,
                    target,
                    EdgeKind.SOQL_OBJECT,
                    provenance,
                    symbol=canonical,
                    resolved=resolved,
                )

            # Attribute bare fields to their nearest SELECT scope.  This keeps
            # a repeated name such as Id on Contact in a child relationship
            # query from leaking into the outer Account scope.  Parentheses
            # used only for functions remain in the owning query scope.
            for scope, (scope_object, scope_resolved) in resolved_scopes.items():
                if not scope_resolved or scope in opaque_field_scopes:
                    continue
                known_fields = {
                    field_name.casefold(): field_name
                    for object_name, fields in _STANDARD_SCHEMA.items()
                    if object_name.casefold() == scope_object.casefold()
                    for field_name in fields
                }
                known_fields.update(
                    {
                        field_name: self.nodes[node_id].name.split(".", 1)[1]
                        for (object_name, field_name), node_id in self.fields.items()
                        if object_name == scope_object.casefold()
                    }
                )
                for token in tokens:
                    if self._owning_soql_scope(token, scopes) != scope:
                        continue
                    if token.start in object_positions:
                        continue
                    canonical_field = known_fields.get(token.value.casefold())
                    if canonical_field is None or token.value.casefold() in _SOQL_KEYWORDS:
                        continue
                    start = body_start + token.start
                    end = body_start + token.end
                    provenance = _provenance(file, start, end, "apex_soql")
                    field_target, resolved = self._resolve_field(
                        scope_object,
                        canonical_field,
                        provenance,
                        warn_if_missing=False,
                    )
                    if field_target is not None:
                        self._add_edge(
                            source_id,
                            field_target,
                            EdgeKind.SOQL_FIELD,
                            provenance,
                            symbol=f"{scope_object}.{canonical_field}",
                            resolved=resolved,
                        )

            # Explicit Object.Field syntax is strong enough to surface an
            # unresolved field rather than treating it as an arbitrary alias.
            for dotted in re.finditer(
                r"\b(?P<object>[A-Za-z_]\w*)\.(?P<field>[A-Za-z_]\w*)\b",
                body,
            ):
                # The structural scanner omits quoted values and comments.
                # Requiring exact token starts prevents dotted text in either
                # from being reported as a dependency.
                object_token = next(
                    (token for token in tokens if token.start == dotted.start("object")),
                    None,
                )
                field_token = next(
                    (token for token in tokens if token.start == dotted.start("field")),
                    None,
                )
                if object_token is None or field_token is None:
                    continue
                object_name = dotted.group("object")
                if object_name.casefold() not in self.objects and not any(
                    value.casefold() == object_name.casefold() for value in _STANDARD_SCHEMA
                ):
                    continue
                start = body_start + dotted.start()
                end = body_start + dotted.end()
                provenance = _provenance(file, start, end, "apex_soql")
                field_target, resolved = self._resolve_field(
                    object_name,
                    dotted.group("field"),
                    provenance,
                    warn_if_missing=True,
                )
                if field_target is not None:
                    self._add_edge(
                        source_id,
                        field_target,
                        EdgeKind.SOQL_FIELD,
                        provenance,
                        symbol=f"{object_name}.{dotted.group('field')}",
                        resolved=resolved,
                    )

    def _parse_lwc(self, source_id: str, node: _NodeRecord) -> None:
        for path in sorted(node.metadata_paths):
            if not path.endswith(".js") or path.endswith(".test.js"):
                continue
            file = self.files[path]
            for match in re.finditer(
                r"@salesforce/apex/(?P<class>[A-Za-z_]\w*)\.(?P<method>[A-Za-z_]\w*)",
                file.text,
            ):
                class_name = match.group("class")
                method_name = match.group("method")
                provenance = _provenance(file, match.start(), match.end(), "lwc_import")
                target, class_resolved = self._resolve_apex(class_name, provenance)
                method_resolved = (
                    class_resolved
                    and method_name.casefold()
                    in self.apex_methods.get(class_name.casefold(), set())
                )
                if class_resolved and not method_resolved:
                    self._warn(
                        WarningCode.UNRESOLVED_REFERENCE,
                        f"LWC Apex import method was not found on {class_name}: {method_name}",
                        provenance,
                    )
                self._add_edge(
                    source_id,
                    target,
                    EdgeKind.LWC_APEX_IMPORT,
                    provenance,
                    symbol=f"{class_name}.{method_name}",
                    resolved=method_resolved,
                )

            for match in re.finditer(
                r"@salesforce/schema/(?P<object>[A-Za-z_]\w*)(?:\.(?P<field>[A-Za-z_]\w*))?",
                file.text,
            ):
                object_name = match.group("object")
                provenance = _provenance(file, match.start(), match.end(), "lwc_import")
                field_name = match.group("field")
                if field_name:
                    target_id, resolved = self._resolve_field(
                        object_name,
                        field_name,
                        provenance,
                        warn_if_missing=True,
                    )
                    symbol = f"{object_name}.{field_name}"
                else:
                    target_id, symbol, resolved = self._resolve_object(object_name, provenance)
                if target_id is not None:
                    self._add_edge(
                        source_id,
                        target_id,
                        EdgeKind.LWC_SCHEMA_IMPORT,
                        provenance,
                        symbol=symbol,
                        resolved=resolved,
                    )

    def _parse_permission_set(self, source_id: str, file: _File) -> None:
        try:
            root = ElementTree.fromstring(file.text)
        except ElementTree.ParseError as exc:
            offset = 0
            if exc.position:
                requested_line = exc.position[0]
                lines = file.text.splitlines(keepends=True)
                offset = sum(len(line) for line in lines[: max(0, requested_line - 1)])
            provenance = _provenance(
                file, offset, min(len(file.text), offset + 200), "permission_set"
            )
            self._warn(
                WarningCode.MALFORMED_SOURCE,
                f"permission set XML could not be parsed: {exc}",
                provenance,
            )
            return

        relationships = (
            (
                "classAccesses",
                "apexClass",
                EdgeKind.PERMISSION_CLASS_ACCESS,
                self._resolve_apex,
            ),
            (
                "pageAccesses",
                "apexPage",
                EdgeKind.PERMISSION_PAGE_ACCESS,
                self._resolve_page,
            ),
        )
        for container_name, value_name, edge_kind, resolver in relationships:
            for container in root.iter():
                if _local_name(container.tag) != container_name:
                    continue
                value = next(
                    (
                        (child.text or "").strip()
                        for child in container
                        if _local_name(child.tag) == value_name
                    ),
                    "",
                )
                if not value:
                    continue
                offset = file.text.find(value)
                provenance = _provenance(
                    file,
                    max(0, offset),
                    max(0, offset) + len(value),
                    "permission_set",
                )
                target, resolved = resolver(value, provenance)
                self._add_edge(
                    source_id,
                    target,
                    edge_kind,
                    provenance,
                    symbol=value,
                    resolved=resolved,
                )

        for container in root.iter():
            local = _local_name(container.tag)
            if local not in {"objectPermissions", "fieldPermissions"}:
                continue
            child_name = "object" if local == "objectPermissions" else "field"
            value = next(
                (
                    (child.text or "").strip()
                    for child in container
                    if _local_name(child.tag) == child_name
                ),
                "",
            )
            if not value:
                continue
            offset = file.text.find(value)
            provenance = _provenance(
                file,
                max(0, offset),
                max(0, offset) + len(value),
                "permission_set",
            )
            target_id: str | None
            if local == "objectPermissions":
                target_id, canonical, resolved = self._resolve_object(value, provenance)
                edge_kind = EdgeKind.PERMISSION_OBJECT_ACCESS
                symbol = canonical
            else:
                if "." not in value:
                    target_id = self._unresolved_node("field", value)
                    resolved = False
                    symbol = value
                    self._warn(
                        WarningCode.UNRESOLVED_REFERENCE,
                        f"permission set field reference is not Object.Field: {value}",
                        provenance,
                    )
                else:
                    object_name, field_name = value.split(".", 1)
                    target_id, resolved = self._resolve_field(
                        object_name,
                        field_name,
                        provenance,
                        warn_if_missing=True,
                    )
                    symbol = value
                    assert target_id is not None
                edge_kind = EdgeKind.PERMISSION_FIELD_ACCESS
            self._add_edge(
                source_id,
                target_id,
                edge_kind,
                provenance,
                symbol=symbol,
                resolved=resolved,
            )

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
                raise FileNotFoundError(f"Salesforce entry path does not exist: {entry}")
            if not matched:
                raise ValueError(f"unsupported Salesforce metadata entry path: {entry}")
            seeds.update(matched)
        if not seeds:
            raise ValueError("entry paths do not select any supported Salesforce metadata")
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

            # Tests and permission sets are relevant reverse evidence for a
            # selected production component even though dependency direction is
            # test/permission -> component.
            for edge in self.edges.values():
                source = self.nodes[edge.source_id]
                if (
                    edge.target_id in selected
                    and source.kind in {NodeKind.APEX_TEST, NodeKind.PERMISSION_SET}
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
        digests = tuple(
            SourceDigest(path=path, sha256=self.files[path].sha256)
            for path in sorted(selected_paths)
            if path in self.files
        )
        edge_records = [
            edge
            for edge in self.edges.values()
            if edge.source_id in selected and edge.target_id in selected
        ]
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
        selected_source_paths = {
            provenance.path for edge in edges for provenance in edge.provenance
        } | selected_paths
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
            platform=Platform.SALESFORCE,
            base_revision=self.base_revision,
            entry_paths=entries,
            source_digests=digests,
            nodes=nodes,
            edges=edges,
            warnings=warnings,
        )


def build_salesforce_dependency_graph(
    repository_root: Path | str,
    entry_paths: Iterable[str],
    base_revision: str,
) -> DependencyGraph:
    """Build a deterministic graph for one Salesforce repository revision.

    ``entry_paths`` may identify metadata files or directories.  The repository
    is captured exactly once, then the parser reads only those captured bytes.
    ``base_revision`` must equal that snapshot's deterministic content revision;
    arbitrary labels and stale revisions fail closed.  The returned graph
    includes the dependency closure plus Apex tests and permission sets that
    reference the selected production artifacts.
    """

    source_snapshot = snapshot_tree(repository_root)
    return _SalesforceGraphBuilder(source_snapshot, base_revision).build(entry_paths)


__all__ = [
    "DependencyEdge",
    "DependencyGraph",
    "DependencyNode",
    "EdgeKind",
    "GRAPH_SCHEMA_VERSION",
    "NodeKind",
    "ParserWarning",
    "SALESFORCE_ANALYZER_VERSION",
    "SourceDigest",
    "SourceProvenance",
    "WarningCode",
    "build_salesforce_dependency_graph",
]
