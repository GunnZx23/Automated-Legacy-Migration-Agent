"""Read-only static validation for the bounded Mule 3-to-4 candidate.

The validator accepts two explicit roots: an immutable Mule 3 source tree and
the disposable candidate produced by the Engineer.  It reads neither fixture
metadata nor reviewed target/oracle output.  All parsing starts from one safe
filesystem snapshot per root, and every failure is reduced to a controlled
code plus a repository-relative artifact identifier.

These checks prove inventory, source preservation, and domain-specific static
contracts only.  They do not invoke Maven, execute MUnit, start Mule, contact
Anypoint, or support a deployment/runtime-success claim.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast
from xml.etree import ElementTree

import yaml
from pydantic import Field
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from legacy_migration_agent.contracts import Sha256Digest, StrictModel
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.workspace import SnapshotEntry, TreeSnapshot, snapshot_tree

CORE = "http://www.mulesoft.org/schema/mule/core"
DW1 = "http://www.mulesoft.org/schema/mule/ee/dw"
HTTP = "http://www.mulesoft.org/schema/mule/http"
MUNIT = "http://www.mulesoft.org/schema/mule/munit"
MUNIT_TOOLS = "http://www.mulesoft.org/schema/mule/munit-tools"
MAVEN = "http://maven.apache.org/POM/4.0.0"

MULE3_APP = "legacy-mule3/customer-status-api/src/main/app/customer-status-api.xml"
MULE3_PROPERTIES = "legacy-mule3/customer-status-api/src/main/app/mule-app.properties"
MULE3_TEST = "legacy-mule3/customer-status-api/src/test/munit/customer-status-api-test.xml"
MULE4_PROJECT = "mule4/customer-status-api"
MULE4_POM = f"{MULE4_PROJECT}/pom.xml"
MULE4_ARTIFACT = f"{MULE4_PROJECT}/mule-artifact.json"
MULE4_APP = f"{MULE4_PROJECT}/src/main/mule/customer-status-api.xml"
MULE4_PROPERTIES = f"{MULE4_PROJECT}/src/main/resources/application.yaml"
MULE4_DATAWEAVE = f"{MULE4_PROJECT}/src/main/resources/dw/customer-status-response.dwl"
MULE4_TEST = f"{MULE4_PROJECT}/src/test/munit/customer-status-api-test.xml"

SOURCE_FILES = (MULE3_APP, MULE3_PROPERTIES, MULE3_TEST)
TARGET_FILES = (
    MULE4_ARTIFACT,
    MULE4_POM,
    MULE4_APP,
    MULE4_PROPERTIES,
    MULE4_DATAWEAVE,
    MULE4_TEST,
)
CANDIDATE_FILES = tuple(sorted((*SOURCE_FILES, *TARGET_FILES)))

# Human-reviewable acceptance requirements derived from the deterministic
# parsers below. They describe observable behavior and safety boundaries, not
# one controller-authored implementation or candidate-test oracle.
MULESOFT_IMPLEMENTATION_CONTRACT = (
    (
        "Create the additive Mule 4 application only in the approved target paths and preserve "
        "all three Mule 3 source files byte-for-byte. Every artifact must remain bounded UTF-8 "
        "input; parse XML, YAML and JSON with safe parsers and reject external entity expansion "
        "or parsing that needs filesystem/network access."
    ),
    (
        "Preserve the public read-only behavior: GET /api/customers/{customerId}/status returns "
        "application/json whose customerId echoes the path value, whose status is ACTIVE, and "
        "whose source is synthetic-fixture. Those three public response values are the immutable "
        "controller-test contract. Internal flow, sub-flow, variable and processor names, topology "
        "and expression spelling are implementation choices."
    ),
    (
        "Use Mule 4 configuration and DataWeave 2.0 syntax throughout the target. The response "
        "DataWeave must declare JSON output, but its formatting, field order, local variables and "
        "other equivalent implementation details are not prescribed."
    ),
    (
        "Author candidate-owned MUnit tests that are well-formed and nonempty, execute at least "
        "one flow or sub-flow defined by the candidate application either directly or through "
        "the fixed loopback public route, and make at least one MUnit assertion. Suite names, "
        "test names and descriptions, setup data, assertion values, messages, order and count "
        "are candidate choices and are not an acceptance oracle. Do not reuse runtime-reserved "
        "evidence identities "
        "`controller-customer-status-behavior-test-suite` or "
        "`controller-build-customer-status-response-contract`."
    ),
    (
        "Package as mule-application and pin Mule runtime 4.9.20, Java 17, Mule Maven plugin "
        "4.10.1, MUnit 3.7.3 and HTTP connector 1.12.0. Allow only the Mule Maven and MUnit Maven "
        "plugins and only the HTTP connector, MUnit runner and MUnit tools dependencies at their "
        "approved coordinates, classifiers and scopes."
    ),
    (
        "Repository and pluginRepository URLs may only use the MuleSoft releases repository. "
        "Include no distributionManagement, Maven servers, embedded credentials, extra plugins "
        "or extra dependencies. mule-artifact.json must retain the pinned runtime, Java and "
        "MULE_EE requirements while allowing unrelated metadata keys."
    ),
    (
        "Keep listener configuration on 127.0.0.1:8081 and expose no additional method or route. "
        "Include no secrets, credential-bearing URLs, outbound HTTP, DB, email, file, FTP, JMS, "
        "object-store, SFTP, sockets or VM connector. Static acceptance proves constraints only; "
        "runtime-owned tests provide behavioral evidence, and no deployment claim is permitted."
    ),
)

MULE4_RUNTIME = "4.9.20"
JAVA_VERSION = "17"
DATAWEAVE_VERSION = "2.0"
MUNIT_VERSION = "3.7.3"
MULE_MAVEN_PLUGIN_VERSION = "4.10.1"
HTTP_CONNECTOR_VERSION = "1.12.0"
MULESOFT_PUBLIC_ROUTE = "/api/customers/{customerId}/status"
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_YAML_DEPTH = 32
MAX_YAML_ENTRIES = 1024
MAX_YAML_KEY_LENGTH = 256
MAX_YAML_PATH_LENGTH = 1024
MAX_MUNIT_RESPONSE_TIMEOUT_MS = 300_000

STATIC_CHECKS = (
    "exact-inventory",
    "legacy-byte-preservation",
    "safe-structured-parsing",
    "mule4-public-interface",
    "dataweave-2-compatibility",
    "candidate-munit-structure",
    "pinned-toolchain-allowlist",
    "pom-artifact-version-consistency",
    "loopback-only-config",
    "no-secrets",
    "no-outbound-connectors",
)

_XML_GUARD = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|passwd|client[_-]?secret|access[_-]?token|api[_-]?key|"
    r"authorization)\b\s*[:=]\s*[\"']?[^\s\"'<>]+"
)
_CREDENTIAL_URI = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@")
_PRIVATE_KEY = re.compile(r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----")
_PROPERTY_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}")
_PROPERTY_EXPRESSION = re.compile(r"#\[\s*(?:Mule::)?p\(\s*(['\"])([A-Za-z0-9_.-]+)\1\s*\)\s*\]")
_DATAWEAVE_VERSION = re.compile(r"(?m)^[ \t]*%dw[ \t]+2\.0[ \t]*(?://[^\r\n]*)?$")
_DATAWEAVE_OUTPUT_JSON = re.compile(
    r"(?mi)^[ \t]*output[ \t]+application/json(?:[ \t]+[^\r\n]+)?[ \t]*$"
)
_DATAWEAVE_BODY = re.compile(r"(?m)^[ \t]*---[ \t]*$")
_DATAWEAVE_RESPONSE_KEY = re.compile(r"(?m)(?:^|[{,])\s*['\"]?(customerId|status|source)['\"]?\s*:")
_DATAWEAVE_RUNTIME_VALUE = re.compile(r"\bvars\s*(?:\.|\[)")
_MULE3_EXPRESSION = re.compile(r"\b(?:flowVars|sessionVars|inboundProperties|outboundProperties)\b")
_MUNIT_LOOPBACK_REQUEST_PATH = re.compile(r"/api/customers/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/status")
_MUNIT_COMPONENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")
_MULE_TARGET_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_SAFE_MUNIT_TARGET_VALUES = frozenset({"#[payload]", "#[attributes]", "#[message]"})
_MUNIT_RUNTIME_OBSERVABLE = re.compile(r"\b(?:payload|attributes|vars|error)\b")
_MUNIT_TRIVIAL_EXPRESSION = re.compile(
    r"^#\[\s*(?:true|false|null|[-+]?(?:\d+(?:\.\d+)?)|['\"][^'\"]*['\"])\s*\]$",
    re.IGNORECASE,
)
_YAML_MERGE_TAG = "tag:yaml.org,2002:merge"
_YAML_KEY = re.compile(r"[A-Za-z0-9_.-]+")
_RESERVED_RUNTIME_MUNIT_NAMES = frozenset(
    {
        "controller-customer-status-behavior-test-suite",
        "controller-build-customer-status-response-contract",
    }
)
_FORBIDDEN_CONNECTOR_NAMESPACES = (
    "/schema/mule/db",
    "/schema/mule/email",
    "/schema/mule/file",
    "/schema/mule/ftp",
    "/schema/mule/jms",
    "/schema/mule/objectstore",
    "/schema/mule/sftp",
    "/schema/mule/sockets",
    "/schema/mule/vm",
)


class MuleSoftLocalCheckCode(StrEnum):
    """Stable failure categories safe to expose in command output."""

    UNSAFE_TREE = "unsafe_tree"
    INVENTORY_MISMATCH = "inventory_mismatch"
    SOURCE_DRIFT = "source_drift"
    UNSAFE_TEXT = "unsafe_text"
    UNSAFE_XML = "unsafe_xml"
    MALFORMED_XML = "malformed_xml"
    MALFORMED_YAML = "malformed_yaml"
    MALFORMED_JSON = "malformed_json"
    MULE3_CONTRACT = "mule3_contract"
    MULE4_CONTRACT = "mule4_contract"
    DATAWEAVE_CONTRACT = "dataweave_contract"
    MUNIT_CONTRACT = "munit_contract"
    POM_CONTRACT = "pom_contract"
    ARTIFACT_CONTRACT = "artifact_contract"
    VERSION_MISMATCH = "version_mismatch"
    SECRET_MATERIAL = "secret_material"
    OUTBOUND_CONNECTOR = "outbound_connector"


class MuleSoftLocalCheckFailure(RuntimeError):
    """Controlled, path-sanitized candidate validation failure."""

    def __init__(
        self,
        code: MuleSoftLocalCheckCode,
        artifact: str = "candidate",
    ) -> None:
        self.code = code
        self.artifact = artifact
        super().__init__(f"{code.value}:{artifact}")


class MuleSoftCandidateValidationSummary(StrictModel):
    """Bounded static result; runtime and deployment claims are impossible."""

    schema_version: Literal["1.0"] = "1.0"
    check: Literal["mulesoft-candidate-static-contract"] = "mulesoft-candidate-static-contract"
    passed: Literal[True] = True
    source_revision: Sha256Digest
    candidate_revision: Sha256Digest
    inventory_files: int = Field(ge=1, le=32)
    preserved_source_files: int = Field(ge=1, le=16)
    mule3_runtime: Literal["3.9.5"] = "3.9.5"
    mule4_runtime: Literal["4.9.20"] = "4.9.20"
    java: Literal["17"] = "17"
    dataweave: Literal["2.0"] = "2.0"
    munit: Literal["3.7.3"] = "3.7.3"
    static_checks: tuple[str, ...]
    maven_executed: Literal[False] = False
    munit_executed: Literal[False] = False
    deployment_claim: Literal[False] = False


class _BoundedConfigurationLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects graph features and bounds composition."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._configuration_depth = 0
        self._configuration_entries = 0

    def compose_node(
        self,
        parent: Node | None,
        index: Any,
    ) -> Node:
        if self.check_event(AliasEvent):
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise ComposerError(
                "while composing application configuration",
                None,
                "aliases are not allowed",
                event.start_mark,
            )
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if getattr(event, "anchor", None) is not None:
            raise ComposerError(
                "while composing application configuration",
                None,
                "anchors are not allowed",
                event.start_mark,
            )

        if isinstance(parent, SequenceNode) or (isinstance(parent, MappingNode) and index is None):
            self._configuration_entries += 1
            if self._configuration_entries > MAX_YAML_ENTRIES:
                raise ComposerError(
                    "while composing application configuration",
                    None,
                    "configuration contains too many entries",
                    event.start_mark,
                )

        self._configuration_depth += 1
        try:
            if self._configuration_depth > MAX_YAML_DEPTH:
                raise ComposerError(
                    "while composing application configuration",
                    None,
                    "configuration nesting is too deep",
                    event.start_mark,
                )
            return cast(Node, super().compose_node(parent, index))
        except RecursionError as exc:
            raise ComposerError(
                "while composing application configuration",
                None,
                "configuration nesting is too deep",
                event.start_mark,
            ) from exc
        finally:
            self._configuration_depth -= 1

    def flatten_mapping(self, node: MappingNode) -> None:
        if any(key_node.tag == _YAML_MERGE_TAG for key_node, _ in node.value):
            raise ConstructorError(
                "while constructing application configuration",
                node.start_mark,
                "merge keys are not allowed",
                node.start_mark,
            )
        super().flatten_mapping(node)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if isinstance(key_node, ScalarNode) and len(key_node.value) > MAX_YAML_KEY_LENGTH:
                raise ConstructorError(
                    "while constructing application configuration",
                    node.start_mark,
                    "configuration key is too long",
                    key_node.start_mark,
                )
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or not key or _YAML_KEY.fullmatch(key) is None:
                raise ConstructorError(
                    "while constructing application configuration",
                    node.start_mark,
                    "configuration keys must be bounded property names",
                    key_node.start_mark,
                )
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError as exc:
                raise ConstructorError(
                    "while constructing application configuration",
                    node.start_mark,
                    "configuration keys must be hashable",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise ConstructorError(
                    "while constructing application configuration",
                    node.start_mark,
                    "duplicate configuration key",
                    key_node.start_mark,
                )
        return super().construct_mapping(node, deep=deep)


def check_mulesoft_candidate(
    candidate_root: Path,
    source_root: Path,
) -> MuleSoftCandidateValidationSummary:
    """Validate the exact synthetic Mule candidate without consulting an oracle."""

    source = _capture(source_root, "source")
    candidate = _capture(candidate_root, "candidate")
    source_entries = source.by_path()
    candidate_entries = candidate.by_path()
    _require_inventory(source_entries, candidate_entries)
    _require_source_preservation(source_entries, candidate_entries)

    texts = {path: _text(entry, path) for path, entry in candidate_entries.items()}
    xml_roots = {
        path: _xml(texts[path], path)
        for path in (MULE3_APP, MULE3_TEST, MULE4_POM, MULE4_APP, MULE4_TEST)
    }

    properties = parse_mule_application_properties(texts[MULE4_PROPERTIES])
    candidate_callables, public_listener_flow = _validate_mule4(
        xml_roots[MULE4_APP],
        properties,
    )
    _validate_dataweave(texts[MULE4_DATAWEAVE])
    allowed_munit_http_elements = _validate_munit(
        xml_roots[MULE4_TEST],
        candidate_callables,
        public_listener_flow,
        properties,
    )
    versions = _validate_pom(xml_roots[MULE4_POM])
    artifact = _parse_json(texts[MULE4_ARTIFACT], MULE4_ARTIFACT)
    _validate_artifact(artifact, versions)
    _reject_secrets(texts)
    _reject_outbound_connectors(xml_roots, allowed_munit_http_elements)

    return MuleSoftCandidateValidationSummary(
        source_revision=source.revision,
        candidate_revision=candidate.revision,
        inventory_files=len(candidate_entries),
        preserved_source_files=len(source_entries),
        static_checks=STATIC_CHECKS,
    )


def _capture(root: Path, role: str) -> TreeSnapshot:
    try:
        return snapshot_tree(root)
    except (OSError, ValueError, PolicyViolation) as exc:
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.UNSAFE_TREE,
            role,
        ) from exc


def _require_inventory(
    source: Mapping[str, SnapshotEntry],
    candidate: Mapping[str, SnapshotEntry],
) -> None:
    if tuple(sorted(source)) != SOURCE_FILES:
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.INVENTORY_MISMATCH,
            "source",
        )
    if tuple(sorted(candidate)) != CANDIDATE_FILES:
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.INVENTORY_MISMATCH,
            "candidate",
        )


def _require_source_preservation(
    source: Mapping[str, SnapshotEntry],
    candidate: Mapping[str, SnapshotEntry],
) -> None:
    if any(
        candidate[path].content != source[path].content or candidate[path].mode != source[path].mode
        for path in SOURCE_FILES
    ):
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.SOURCE_DRIFT,
            "legacy-mule3",
        )


def _text(entry: SnapshotEntry, artifact: str) -> str:
    if len(entry.content) > MAX_ARTIFACT_BYTES:
        raise MuleSoftLocalCheckFailure(MuleSoftLocalCheckCode.UNSAFE_TEXT, artifact)
    try:
        text = entry.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.UNSAFE_TEXT,
            artifact,
        ) from exc
    if "\x00" in text:
        raise MuleSoftLocalCheckFailure(MuleSoftLocalCheckCode.UNSAFE_TEXT, artifact)
    return text


def _xml(text: str, artifact: str) -> ElementTree.Element:
    if _XML_GUARD.search(text):
        raise MuleSoftLocalCheckFailure(MuleSoftLocalCheckCode.UNSAFE_XML, artifact)
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.MALFORMED_XML,
            artifact,
        ) from exc


def _validate_mule4(
    app: ElementTree.Element,
    properties: Mapping[str, str],
) -> tuple[frozenset[str], str]:
    _require(app.tag == _tag(CORE, "mule"), MuleSoftLocalCheckCode.MULE4_CONTRACT, MULE4_APP)
    property_loaders = app.findall(_tag(CORE, "configuration-properties"))
    _require(
        any(loader.attrib.get("file") == "application.yaml" for loader in property_loaders),
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )
    listener_configs = app.findall(_tag(HTTP, "listener-config"))
    _require(bool(listener_configs), MuleSoftLocalCheckCode.MULE4_CONTRACT, MULE4_APP)
    configs_by_name: dict[str, str | None] = {}
    for config in listener_configs:
        name = config.attrib.get("name", "").strip()
        raw_base_path = config.attrib.get("basePath")
        base_path = resolve_mule_property_value(raw_base_path, properties)
        connection = config.find(_tag(HTTP, "listener-connection"))
        _require(
            bool(name)
            and name not in configs_by_name
            and (raw_base_path is None or base_path is not None)
            and normalize_http_route(base_path, "/") is not None
            and connection is not None,
            MuleSoftLocalCheckCode.MULE4_CONTRACT,
            MULE4_APP,
        )
        assert connection is not None
        _require(
            resolve_mule_property_value(connection.attrib.get("host"), properties) == "127.0.0.1"
            and resolve_mule_property_value(connection.attrib.get("port"), properties) == "8081",
            MuleSoftLocalCheckCode.MULE4_CONTRACT,
            MULE4_APP,
        )
        configs_by_name[name] = base_path

    flow_elements = tuple(
        element for local in ("flow", "sub-flow") for element in app.findall(_tag(CORE, local))
    )
    flow_names = tuple(element.attrib.get("name", "").strip() for element in flow_elements)
    _require(
        bool(flow_names) and all(flow_names) and len(set(flow_names)) == len(flow_names),
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )
    listeners = tuple(
        (flow.attrib.get("name", "").strip(), listener)
        for flow in app.findall(_tag(CORE, "flow"))
        for listener in flow.iter(_tag(HTTP, "listener"))
    )
    public_listener_flow = listeners[0][0] if len(listeners) == 1 else ""
    public_listener = listeners[0][1] if len(listeners) == 1 else None
    config_ref = public_listener.attrib.get("config-ref") if public_listener is not None else None
    base_path = configs_by_name.get(config_ref) if config_ref is not None else None
    listener_path = (
        resolve_mule_property_value(public_listener.attrib.get("path"), properties)
        if public_listener is not None
        else None
    )
    allowed_methods = (
        _http_methods(public_listener.attrib.get("allowedMethods"))
        if public_listener is not None
        else set()
    )
    _require(
        len(listeners) == 1
        and config_ref in configs_by_name
        and listener_path is not None
        and bool(listener_path.strip())
        and normalize_http_route(base_path, listener_path) == MULESOFT_PUBLIC_ROUTE
        and allowed_methods == {"GET"}
        and not any(_namespace(element.tag) == DW1 for element in app.iter())
        and not _contains_mule3_expression(app),
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )
    return frozenset(flow_names), public_listener_flow


def _validate_dataweave(text: str) -> None:
    normalized = text.replace("\r\n", "\n")
    version_headers = re.findall(
        r"(?m)^[ \t]*%dw[ \t]+([0-9]+(?:\.[0-9]+)?)[^\r\n]*$",
        normalized,
    )
    body_marker = _DATAWEAVE_BODY.search(normalized)
    body = normalized[body_marker.end() :].strip() if body_marker is not None else ""
    response_keys = set(_DATAWEAVE_RESPONSE_KEY.findall(body))
    _require(
        version_headers == [DATAWEAVE_VERSION]
        and _DATAWEAVE_VERSION.search(normalized) is not None
        and _DATAWEAVE_OUTPUT_JSON.search(normalized) is not None
        and body_marker is not None
        and body.casefold() not in {"", "null", "{}"}
        and response_keys == {"customerId", "status", "source"}
        and _DATAWEAVE_RUNTIME_VALUE.search(normalized) is not None,
        MuleSoftLocalCheckCode.DATAWEAVE_CONTRACT,
        MULE4_DATAWEAVE,
    )


def _assertion_observes_candidate_value(assertion: ElementTree.Element) -> bool:
    local = _local_name(assertion.tag)
    attribute = "actual" if local in {"assert-equals", "assert-not-equals"} else "expression"
    observed = assertion.attrib.get(attribute, "").strip()
    if (
        not observed
        or _MUNIT_TRIVIAL_EXPRESSION.fullmatch(observed) is not None
        or _MUNIT_RUNTIME_OBSERVABLE.search(observed) is None
    ):
        return False
    comparison = (
        assertion.attrib.get("expected", "").strip()
        if local in {"assert-equals", "assert-not-equals"}
        else assertion.attrib.get("is", "").strip()
    )
    if local in {"assert-that", "assert-equals", "assert-not-equals"} and not comparison:
        return False
    # A self-comparison is syntactically an assertion but supplies no evidence.
    compact_observed = re.sub(r"\s+", "", observed).removeprefix("#[").removesuffix("]")
    compact_comparison = re.sub(r"\s+", "", comparison)
    compact_expected = compact_comparison.removeprefix("#[").removesuffix("]")
    if compact_expected == compact_observed or (
        compact_comparison and f"equalTo({compact_observed})" in compact_comparison
    ):
        return False
    return True


def _validate_munit(
    root: ElementTree.Element,
    candidate_callables: frozenset[str],
    public_listener_flow: str,
    properties: Mapping[str, str],
) -> frozenset[int]:
    _require(root.tag == _tag(CORE, "mule"), MuleSoftLocalCheckCode.MUNIT_CONTRACT, MULE4_TEST)
    configs = root.findall(_tag(MUNIT, "config"))
    tests = root.findall(_tag(MUNIT, "test"))
    candidate_evidence_names = tuple(
        element.attrib.get("name", "").strip() for element in (*configs, *tests)
    )
    _require(
        bool(configs)
        and all(config.attrib.get("name", "").strip() for config in configs)
        and bool(tests)
        and all(test.attrib.get("name", "").strip() for test in tests)
        and len(set(candidate_evidence_names)) == len(candidate_evidence_names)
        and set(candidate_evidence_names).isdisjoint(_RESERVED_RUNTIME_MUNIT_NAMES),
        MuleSoftLocalCheckCode.MUNIT_CONTRACT,
        MULE4_TEST,
    )
    flow_refs = tuple(element for test in tests for element in test.iter(_tag(CORE, "flow-ref")))
    assertions = tuple(
        element
        for test in tests
        for element in test.iter()
        if _namespace(element.tag) == MUNIT_TOOLS and _local_name(element.tag).startswith("assert-")
    )
    _require(
        all(flow_ref.attrib.get("name") in candidate_callables for flow_ref in flow_refs),
        MuleSoftLocalCheckCode.MUNIT_CONTRACT,
        MULE4_TEST,
    )
    allowed_http_elements = _validate_munit_loopback_http(
        root,
        tests,
        public_listener_flow,
        properties,
    )
    evidence_bearing_test = any(
        (
            any(
                flow_ref.attrib.get("name") in candidate_callables
                for flow_ref in test.iter(_tag(CORE, "flow-ref"))
            )
            or any(
                id(element) in allowed_http_elements for element in test.iter(_tag(HTTP, "request"))
            )
        )
        and any(
            _assertion_observes_candidate_value(element)
            for element in test.iter()
            if _namespace(element.tag) == MUNIT_TOOLS
            and _local_name(element.tag).startswith("assert-")
        )
        for test in tests
    )
    _require(
        (bool(flow_refs) or bool(allowed_http_elements))
        and bool(assertions)
        and evidence_bearing_test,
        MuleSoftLocalCheckCode.MUNIT_CONTRACT,
        MULE4_TEST,
    )
    return allowed_http_elements


def _validate_munit_loopback_http(
    root: ElementTree.Element,
    tests: Sequence[ElementTree.Element],
    public_listener_flow: str,
    properties: Mapping[str, str],
) -> frozenset[int]:
    http_elements = tuple(element for element in root.iter() if _namespace(element.tag) == HTTP)
    if not http_elements:
        return frozenset()

    allowed_names = {"request", "request-config", "request-connection"}
    _require(
        all(_local_name(element.tag) in allowed_names for element in http_elements),
        MuleSoftLocalCheckCode.OUTBOUND_CONNECTOR,
        MULE4_TEST,
    )
    request_configs = tuple(root.findall(_tag(HTTP, "request-config")))
    request_configs_by_name: dict[str, str | None] = {}
    for config in request_configs:
        name = config.attrib.get("name", "").strip()
        children = tuple(config)
        raw_base_path = config.attrib.get("basePath")
        base_path = resolve_mule_property_value(raw_base_path, properties)
        _require(
            _MUNIT_COMPONENT_NAME.fullmatch(name) is not None
            and name not in request_configs_by_name
            and set(config.attrib) <= {"name", "basePath", "responseTimeout"}
            and (raw_base_path is None or base_path is not None)
            and (base_path is None or normalize_http_route(base_path, "/") is not None)
            and _bounded_munit_response_timeout(
                config.attrib.get("responseTimeout"),
                properties,
            )
            and len(children) == 1
            and children[0].tag == _tag(HTTP, "request-connection"),
            MuleSoftLocalCheckCode.OUTBOUND_CONNECTOR,
            MULE4_TEST,
        )
        connection = children[0]
        protocol = resolve_mule_property_value(connection.attrib.get("protocol"), properties)
        _require(
            not tuple(connection)
            and set(connection.attrib) <= {"host", "port", "protocol"}
            and resolve_mule_property_value(connection.attrib.get("host"), properties)
            == "127.0.0.1"
            and resolve_mule_property_value(connection.attrib.get("port"), properties) == "8081"
            and (protocol is None or protocol.upper() == "HTTP"),
            MuleSoftLocalCheckCode.OUTBOUND_CONNECTOR,
            MULE4_TEST,
        )
        request_configs_by_name[name] = base_path

    requests = tuple(request for test in tests for request in test.iter(_tag(HTTP, "request")))
    all_requests = tuple(root.iter(_tag(HTTP, "request")))
    enabled_sources = tuple(
        source for test in tests for source in test.iter(_tag(MUNIT, "enable-flow-source"))
    )
    _require(
        bool(request_configs)
        and bool(requests)
        and len(requests) == len(all_requests)
        and bool(enabled_sources)
        and all(source.attrib.get("value") == public_listener_flow for source in enabled_sources),
        MuleSoftLocalCheckCode.MUNIT_CONTRACT,
        MULE4_TEST,
    )
    for request in requests:
        config_ref = request.attrib.get("config-ref")
        request_path = resolve_mule_property_value(request.attrib.get("path"), properties)
        target = request.attrib.get("target")
        target_value = request.attrib.get("targetValue")
        effective_route = (
            normalize_http_route(request_configs_by_name[config_ref], request_path)
            if config_ref in request_configs_by_name
            else None
        )
        _require(
            not tuple(request)
            and set(request.attrib)
            <= {
                "config-ref",
                "method",
                "path",
                "responseTimeout",
                "target",
                "targetValue",
            }
            and config_ref in request_configs_by_name
            and request.attrib.get("method", "").strip().upper() == "GET"
            and request_path is not None
            and effective_route is not None
            and _MUNIT_LOOPBACK_REQUEST_PATH.fullmatch(effective_route) is not None
            and _bounded_munit_response_timeout(
                request.attrib.get("responseTimeout"),
                properties,
            )
            and _safe_munit_target(target, target_value),
            MuleSoftLocalCheckCode.OUTBOUND_CONNECTOR,
            MULE4_TEST,
        )
    validated_http_elements = frozenset(
        id(element)
        for element in (
            *request_configs,
            *(config[0] for config in request_configs),
            *requests,
        )
    )
    _require(
        validated_http_elements == frozenset(id(element) for element in http_elements),
        MuleSoftLocalCheckCode.OUTBOUND_CONNECTOR,
        MULE4_TEST,
    )
    return validated_http_elements


def _validate_pom(root: ElementTree.Element) -> dict[str, str]:
    ns = {"m": MAVEN}
    _require(root.tag == _tag(MAVEN, "project"), MuleSoftLocalCheckCode.POM_CONTRACT, MULE4_POM)
    _require(
        root.findtext("m:packaging", namespaces=ns) == "mule-application",
        MuleSoftLocalCheckCode.POM_CONTRACT,
        MULE4_POM,
    )
    properties = root.find("m:properties", ns)
    _require(properties is not None, MuleSoftLocalCheckCode.POM_CONTRACT, MULE4_POM)
    assert properties is not None
    values = {_local_name(child.tag): child.text or "" for child in properties}
    plugin_elements = root.findall(".//m:plugin", ns)
    plugins = {plugin.findtext("m:artifactId", namespaces=ns): plugin for plugin in plugin_elements}
    _require(
        len(plugin_elements) == 2
        and len(plugins) == 2
        and set(plugins) == {"mule-maven-plugin", "munit-maven-plugin"},
        MuleSoftLocalCheckCode.POM_CONTRACT,
        MULE4_POM,
    )
    mule_plugin = plugins["mule-maven-plugin"]
    munit_plugin = plugins["munit-maven-plugin"]
    _require(
        mule_plugin.findtext("m:groupId", namespaces=ns) == "org.mule.tools.maven"
        and mule_plugin.findtext("m:extensions", namespaces=ns) == "true"
        and munit_plugin.findtext("m:groupId", namespaces=ns) == "com.mulesoft.munit.tools"
        and any(
            execution.findtext("m:phase", namespaces=ns) == "test"
            and "test"
            in {
                goal.text
                for goal in execution.findall("m:goals/m:goal", ns)
                if goal.text is not None
            }
            for execution in munit_plugin.findall("m:executions/m:execution", ns)
        ),
        MuleSoftLocalCheckCode.POM_CONTRACT,
        MULE4_POM,
    )
    _require(
        _maven_value(mule_plugin.findtext("m:version", namespaces=ns), values)
        == MULE_MAVEN_PLUGIN_VERSION
        and _maven_value(
            mule_plugin.findtext("m:configuration/m:runtimeVersion", namespaces=ns),
            values,
        )
        == MULE4_RUNTIME
        and _maven_value(munit_plugin.findtext("m:version", namespaces=ns), values)
        == MUNIT_VERSION,
        MuleSoftLocalCheckCode.VERSION_MISMATCH,
        MULE4_POM,
    )
    dependency_elements = root.findall(".//m:dependency", ns)
    dependencies = {
        dependency.findtext("m:artifactId", namespaces=ns): dependency
        for dependency in dependency_elements
    }
    _require(
        len(dependency_elements) == 3
        and len(dependencies) == 3
        and set(dependencies) == {"mule-http-connector", "munit-runner", "munit-tools"},
        MuleSoftLocalCheckCode.POM_CONTRACT,
        MULE4_POM,
    )
    expected_dependencies: dict[str, tuple[str, str, frozenset[str | None]]] = {
        "mule-http-connector": (
            "org.mule.connectors",
            HTTP_CONNECTOR_VERSION,
            frozenset((None, "compile")),
        ),
        "munit-runner": ("com.mulesoft.munit", MUNIT_VERSION, frozenset(("test",))),
        "munit-tools": ("com.mulesoft.munit", MUNIT_VERSION, frozenset(("test",))),
    }
    for artifact_id, (group_id, version, scopes) in expected_dependencies.items():
        dependency = dependencies[artifact_id]
        _require(
            dependency.findtext("m:groupId", namespaces=ns) == group_id
            and dependency.findtext("m:classifier", namespaces=ns) == "mule-plugin"
            and dependency.findtext("m:scope", namespaces=ns) in scopes,
            MuleSoftLocalCheckCode.POM_CONTRACT,
            MULE4_POM,
        )
        _require(
            _maven_value(dependency.findtext("m:version", namespaces=ns), values) == version,
            MuleSoftLocalCheckCode.VERSION_MISMATCH,
            MULE4_POM,
        )
    repository_urls = root.findall(".//m:repository/m:url", ns)
    plugin_repository_urls = root.findall(".//m:pluginRepository/m:url", ns)
    _require(
        bool(repository_urls)
        and bool(plugin_repository_urls)
        and all(
            _allowed_repository_url(element.text)
            for element in (*repository_urls, *plugin_repository_urls)
        )
        and root.find(".//m:distributionManagement", ns) is None
        and root.find(".//m:servers", ns) is None,
        MuleSoftLocalCheckCode.POM_CONTRACT,
        MULE4_POM,
    )
    return {"app.runtime": MULE4_RUNTIME}


def _validate_artifact(value: Any, versions: Mapping[str, str]) -> None:
    _require(
        isinstance(value, dict)
        and value.get("minMuleVersion") == MULE4_RUNTIME
        and value.get("javaSpecificationVersions") == [JAVA_VERSION]
        and value.get("requiredProduct") == "MULE_EE",
        MuleSoftLocalCheckCode.ARTIFACT_CONTRACT,
        MULE4_ARTIFACT,
    )
    _require(
        value["minMuleVersion"] == versions["app.runtime"],
        MuleSoftLocalCheckCode.VERSION_MISMATCH,
        MULE4_ARTIFACT,
    )


def parse_mule_application_properties(text: str) -> dict[str, str]:
    """Parse and flatten bounded Mule application YAML without graph features."""

    try:
        value = yaml.load(text, Loader=_BoundedConfigurationLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.MALFORMED_YAML,
            MULE4_PROPERTIES,
        ) from exc
    _require(
        isinstance(value, Mapping),
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_PROPERTIES,
    )
    return _flatten_configuration(value)


def _parse_json(text: str, artifact: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.MALFORMED_JSON,
            artifact,
        ) from exc


def _flatten_configuration(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    flattened: dict[str, str] = {}
    active_mappings: set[int] = set()
    entry_count = 0

    def reject_configuration() -> None:
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.MALFORMED_YAML,
            MULE4_PROPERTIES,
        )

    def visit(mapping: Mapping[Any, Any], prefix: str = "", depth: int = 1) -> None:
        nonlocal entry_count
        identity = id(mapping)
        if identity in active_mappings or depth > MAX_YAML_DEPTH:
            reject_configuration()
        active_mappings.add(identity)
        try:
            for key, child in mapping.items():
                entry_count += 1
                if entry_count > MAX_YAML_ENTRIES:
                    reject_configuration()
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > MAX_YAML_KEY_LENGTH
                    or _YAML_KEY.fullmatch(key) is None
                ):
                    reject_configuration()
                path = f"{prefix}.{key}" if prefix else key
                if len(path) > MAX_YAML_PATH_LENGTH:
                    reject_configuration()
                if isinstance(child, Mapping):
                    visit(child, path, depth + 1)
                    continue
                if isinstance(child, str):
                    resolved = child.strip()
                elif isinstance(child, int) and not isinstance(child, bool):
                    resolved = str(child)
                else:
                    continue
                if path in flattened:
                    reject_configuration()
                flattened[path] = resolved
        finally:
            active_mappings.remove(identity)

    try:
        visit(value)
    except MuleSoftLocalCheckFailure:
        raise
    except RecursionError as exc:
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.MALFORMED_YAML,
            MULE4_PROPERTIES,
        ) from exc
    return flattened


def resolve_mule_property_value(
    raw: str | None,
    properties: Mapping[str, str],
) -> str | None:
    """Resolve one full Mule property placeholder through a bounded property chain."""

    if raw is None:
        return None
    value = raw.strip()
    for _ in range(4):
        placeholder = _PROPERTY_PLACEHOLDER.fullmatch(value)
        expression = _PROPERTY_EXPRESSION.fullmatch(value)
        property_name = placeholder.group(1) if placeholder else None
        if expression is not None:
            property_name = expression.group(2)
        if property_name is None:
            return value
        resolved = properties.get(property_name)
        if resolved is None or resolved == value:
            return resolved
        value = resolved
    return value


def _bounded_munit_response_timeout(
    raw: str | None,
    properties: Mapping[str, str],
) -> bool:
    if raw is None:
        return True
    resolved = resolve_mule_property_value(raw, properties)
    if resolved is None or re.fullmatch(r"[0-9]{1,6}", resolved) is None:
        return False
    return 1 <= int(resolved) <= MAX_MUNIT_RESPONSE_TIMEOUT_MS


def _safe_munit_target(target: str | None, target_value: str | None) -> bool:
    if target is None:
        return target_value is None
    if _MULE_TARGET_VARIABLE.fullmatch(target.strip()) is None:
        return False
    return target_value is None or target_value.strip() in _SAFE_MUNIT_TARGET_VALUES


def _http_methods(raw: str | None) -> set[str]:
    if raw is None:
        return set()
    return {method.upper() for method in re.split(r"[\s,]+", raw.strip()) if method}


def normalize_http_route(base_path: str | None, listener_path: str | None) -> str | None:
    """Return one absolute effective route or ``None`` for an unsafe path shape."""

    segments: list[str] = []
    for raw in (base_path, listener_path):
        if raw is None:
            continue
        value = raw.strip()
        if not value:
            continue
        if not value.startswith("/") or any(marker in value for marker in ("\\", "?", "#")):
            return None
        path_segments = value.split("/")
        if any(
            segment in {".", ".."} or (not segment and index not in {0, len(path_segments) - 1})
            for index, segment in enumerate(path_segments)
        ):
            return None
        segments.extend(segment for segment in path_segments if segment)
    return "/" + "/".join(segments)


def _contains_mule3_expression(root: ElementTree.Element) -> bool:
    return any(
        _MULE3_EXPRESSION.search(value) is not None
        for element in root.iter()
        for value in element.attrib.values()
        if "#[" in value
    )


def _maven_value(raw: str | None, properties: Mapping[str, str]) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    for _ in range(4):
        placeholder = _PROPERTY_PLACEHOLDER.fullmatch(value)
        if placeholder is None:
            return value
        resolved = properties.get(placeholder.group(1))
        if resolved is None or resolved.strip() == value:
            return resolved
        value = resolved.strip()
    return value


def _allowed_repository_url(raw: str | None) -> bool:
    return raw is not None and raw.strip().rstrip("/") == (
        "https://repository.mulesoft.org/releases"
    )


def _reject_secrets(texts: Mapping[str, str]) -> None:
    for artifact, text in texts.items():
        if (
            _SECRET_ASSIGNMENT.search(text)
            or _CREDENTIAL_URI.search(text)
            or _PRIVATE_KEY.search(text)
        ):
            raise MuleSoftLocalCheckFailure(
                MuleSoftLocalCheckCode.SECRET_MATERIAL,
                artifact,
            )


def _reject_outbound_connectors(
    roots: Mapping[str, ElementTree.Element],
    allowed_munit_http_elements: frozenset[int],
) -> None:
    for artifact, root in roots.items():
        if artifact == MULE4_POM:
            continue
        for element in root.iter():
            namespace = _namespace(element.tag).casefold()
            local_name = _local_name(element.tag).casefold()
            forbidden_namespace = any(
                marker in namespace for marker in _FORBIDDEN_CONNECTOR_NAMESPACES
            )
            outbound_http = namespace == HTTP and local_name in {
                "request",
                "request-config",
                "request-connection",
            }
            allowed_loopback_http = (
                artifact == MULE4_TEST and id(element) in allowed_munit_http_elements
            )
            if forbidden_namespace or (outbound_http and not allowed_loopback_http):
                raise MuleSoftLocalCheckFailure(
                    MuleSoftLocalCheckCode.OUTBOUND_CONNECTOR,
                    artifact,
                )


def _require(
    condition: bool,
    code: MuleSoftLocalCheckCode,
    artifact: str,
) -> None:
    if not condition:
        raise MuleSoftLocalCheckFailure(code, artifact)


def _tag(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="legacy-migration-mulesoft-check")
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="immutable Mule 3 source root; candidate root is the current directory",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = check_mulesoft_candidate(Path.cwd(), args.source_root)
    except MuleSoftLocalCheckFailure as exc:
        print(
            json.dumps(
                {
                    "check": "mulesoft-candidate-static-contract",
                    "passed": False,
                    "code": exc.code.value,
                    "artifact": exc.artifact,
                    "maven_executed": False,
                    "munit_executed": False,
                    "deployment_claim": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(summary.model_dump_json())
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the function boundary
    raise SystemExit(main())


__all__ = [
    "CANDIDATE_FILES",
    "MULE3_APP",
    "MULE4_APP",
    "MULE4_ARTIFACT",
    "MULE4_DATAWEAVE",
    "MULE4_POM",
    "MULE4_PROPERTIES",
    "MULE4_TEST",
    "MAX_ARTIFACT_BYTES",
    "MAX_MUNIT_RESPONSE_TIMEOUT_MS",
    "MuleSoftCandidateValidationSummary",
    "MuleSoftLocalCheckCode",
    "MuleSoftLocalCheckFailure",
    "SOURCE_FILES",
    "TARGET_FILES",
    "check_mulesoft_candidate",
    "main",
    "normalize_http_route",
    "parse_mule_application_properties",
    "resolve_mule_property_value",
]
