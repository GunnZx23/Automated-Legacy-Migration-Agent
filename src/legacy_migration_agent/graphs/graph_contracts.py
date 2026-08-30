"""Platform-neutral contracts for deterministic dependency graphs.

The Salesforce analyzer and future platform analyzers share these immutable
models.  Platform parsers may extend the enumerations without changing the
serialized graph envelope; existing Salesforce JSON therefore remains
compatible with the original ``1.0`` graph shape.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.contracts import Platform, Revision, StrictModel, validate_relative_path

GRAPH_SCHEMA_VERSION: Literal["1.0"] = "1.0"


class NodeKind(StrEnum):
    """Kinds currently emitted by the deterministic graph analyzers."""

    VISUALFORCE_PAGE = "visualforce_page"
    APEX_CLASS = "apex_class"
    APEX_TEST = "apex_test"
    LWC_COMPONENT = "lwc_component"
    PERMISSION_SET = "permission_set"
    SCHEMA_OBJECT = "schema_object"
    SCHEMA_FIELD = "schema_field"
    METADATA_FILE = "metadata_file"
    MULE_FLOW = "mule_flow"
    MULE_SUBFLOW = "mule_subflow"
    MULE_CONFIGURATION = "mule_configuration"
    MULE_PROPERTY = "mule_property"
    MULE_VARIABLE = "mule_variable"
    DATAWEAVE_MODULE = "dataweave_module"
    MUNIT_SUITE = "munit_suite"
    MUNIT_TEST = "munit_test"
    MAVEN_PROJECT = "maven_project"
    MAVEN_PLUGIN = "maven_plugin"
    MAVEN_DEPENDENCY = "maven_dependency"
    UNRESOLVED = "unresolved"


class EdgeKind(StrEnum):
    """Relationships currently emitted by the deterministic graph analyzers."""

    VF_CONTROLLER = "visualforce_controller"
    VF_EXTENSION = "visualforce_extension"
    VF_STANDARD_CONTROLLER = "visualforce_standard_controller"
    APEX_CLASS_REFERENCE = "apex_class_reference"
    APEX_PAGE_REFERENCE = "apex_page_reference"
    SOQL_OBJECT = "soql_object"
    SOQL_FIELD = "soql_field"
    LWC_APEX_IMPORT = "lwc_apex_import"
    LWC_SCHEMA_IMPORT = "lwc_schema_import"
    PERMISSION_CLASS_ACCESS = "permission_class_access"
    PERMISSION_PAGE_ACCESS = "permission_page_access"
    PERMISSION_OBJECT_ACCESS = "permission_object_access"
    PERMISSION_FIELD_ACCESS = "permission_field_access"
    FLOW_REFERENCE = "flow_reference"
    HTTP_LISTENER_CONFIG_REFERENCE = "http_listener_config_reference"
    CONNECTOR_CONFIG_REFERENCE = "connector_config_reference"
    CONFIGURATION_PROPERTIES_REFERENCE = "configuration_properties_reference"
    PROPERTY_REFERENCE = "property_reference"
    MULE_ROUTE_PARAMETER_BINDING = "mule_route_parameter_binding"
    DATAWEAVE_VARIABLE_REFERENCE = "dataweave_variable_reference"
    DATAWEAVE_MODULE_REFERENCE = "dataweave_module_reference"
    MUNIT_FLOW_REFERENCE = "munit_flow_reference"
    MUNIT_VARIABLE_REFERENCE = "munit_variable_reference"
    MUNIT_SUITE_TEST = "munit_suite_test"
    MAVEN_PLUGIN = "maven_plugin"
    MAVEN_DEPENDENCY = "maven_dependency"
    DYNAMIC_REFERENCE = "dynamic_reference"


class WarningCode(StrEnum):
    MALFORMED_SOURCE = "malformed_source"
    DYNAMIC_SOQL = "dynamic_soql"
    DYNAMIC_TYPE = "dynamic_type"
    DYNAMIC_REFERENCE = "dynamic_reference"
    UNRESOLVED_REFERENCE = "unresolved_reference"


class SourceProvenance(StrictModel):
    path: str
    line: int = Field(ge=1)
    excerpt: str = Field(min_length=1, max_length=500)
    parser: str = Field(min_length=1, max_length=80)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class SourceDigest(StrictModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class DependencyNode(StrictModel):
    node_id: str = Field(min_length=1, max_length=500)
    kind: NodeKind
    name: str = Field(min_length=1, max_length=500)
    metadata_paths: tuple[str, ...] = ()
    resolved: bool
    external: bool = False

    @field_validator("metadata_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_relative_path(value) for value in values)

    @model_validator(mode="after")
    def validate_resolution(self) -> DependencyNode:
        if self.external and not self.resolved:
            raise ValueError("an external platform node must be resolved")
        if self.kind is NodeKind.UNRESOLVED and self.resolved:
            raise ValueError("unresolved nodes cannot claim resolution")
        return self


class DependencyEdge(StrictModel):
    source_id: str = Field(min_length=1, max_length=500)
    target_id: str = Field(min_length=1, max_length=500)
    kind: EdgeKind
    symbol: str | None = Field(default=None, max_length=500)
    resolved: bool
    provenance: tuple[SourceProvenance, ...] = Field(min_length=1)


class ParserWarning(StrictModel):
    code: WarningCode
    message: str = Field(min_length=1, max_length=2000)
    provenance: SourceProvenance
    unresolved: bool = True


class DependencyGraph(StrictModel):
    """A revision-bound dependency subgraph with reproducible source hashes."""

    schema_version: Literal["1.0"] = GRAPH_SCHEMA_VERSION
    # The default preserves compatibility with 1.0 Salesforce payloads that
    # relied on the original contract's implicit platform value. Other
    # analyzers must set their platform explicitly.
    platform: Platform = Platform.SALESFORCE
    base_revision: Revision
    entry_paths: tuple[str, ...] = Field(min_length=1)
    source_digests: tuple[SourceDigest, ...]
    nodes: tuple[DependencyNode, ...] = Field(min_length=1)
    edges: tuple[DependencyEdge, ...]
    warnings: tuple[ParserWarning, ...] = ()

    @field_validator("entry_paths")
    @classmethod
    def validate_entries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        # Entry selection is a set, not an execution order.  Canonicalizing it
        # here keeps the graph bytes aligned with GraphSnapshotKey and avoids
        # two equivalent requests colliding at one immutable cache location.
        return tuple(sorted({validate_relative_path(value) for value in values}))

    @model_validator(mode="after")
    def validate_references(self) -> DependencyGraph:
        node_ids = {node.node_id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("dependency graph node IDs must be unique")
        for edge in self.edges:
            if edge.source_id not in node_ids or edge.target_id not in node_ids:
                raise ValueError("every dependency edge must reference graph nodes")
        return self

    @property
    def has_unresolved(self) -> bool:
        return bool(
            any(not node.resolved for node in self.nodes)
            or any(not edge.resolved for edge in self.edges)
            or any(warning.unresolved for warning in self.warnings)
        )

    def node(self, kind: NodeKind, name: str) -> DependencyNode | None:
        lowered = name.casefold()
        return next(
            (node for node in self.nodes if node.kind is kind and node.name.casefold() == lowered),
            None,
        )


__all__ = [
    "DependencyEdge",
    "DependencyGraph",
    "DependencyNode",
    "EdgeKind",
    "GRAPH_SCHEMA_VERSION",
    "NodeKind",
    "ParserWarning",
    "SourceDigest",
    "SourceProvenance",
    "WarningCode",
]
