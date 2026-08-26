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
from typing import Any, Literal
from xml.etree import ElementTree

import yaml
from pydantic import Field

from legacy_migration_agent.contracts import Sha256Digest, StrictModel
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.workspace import SnapshotEntry, TreeSnapshot, snapshot_tree

CORE = "http://www.mulesoft.org/schema/mule/core"
DW1 = "http://www.mulesoft.org/schema/mule/ee/dw"
EE = "http://www.mulesoft.org/schema/mule/ee/core"
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
# parsers below. They expose no oracle bytes and do not claim runtime success.
MULESOFT_IMPLEMENTATION_CONTRACT = (
    (
        "Create an additive Mule 4 application and preserve all three Mule 3 files byte-for-byte. "
        "The POM must package as mule-application and define app.runtime 4.9.20, "
        "mule.maven.plugin.version 4.10.1, munit.version 3.7.3 and http.connector.version 1.12.0. "
        "Use exactly mule-maven-plugin and munit-maven-plugin; bind the MUnit goal test to phase "
        "test; and configure the Mule plugin runtimeVersion from `${app.runtime}`."
    ),
    (
        "The POM dependency set must be exactly org.mule.connectors:mule-http-connector with the "
        "HTTP version and mule-plugin classifier, plus test-scoped "
        "com.mulesoft.munit:munit-runner and munit-tools with the MUnit version and mule-plugin "
        "classifier. Repository and pluginRepository URLs may only be "
        "https://repository.mulesoft.org/releases/. Include no distributionManagement, servers, "
        "credentials or extra plugins/dependencies."
    ),
    (
        "application.yaml must decode exactly to http.host 127.0.0.1 and the string http.port "
        "8081. mule-artifact.json must contain exactly minMuleVersion 4.9.20, "
        'javaSpecificationVersions ["17"] and requiredProduct MULE_EE, with no extra key.'
    ),
    (
        "The Mule XML must load exactly application.yaml, define listener config "
        "customer-status-http-listener with basePath /api and a nested listener-connection using "
        "${http.host} and ${http.port}, and preserve allowedMethods GET at "
        "/customers/{customerId}/status. Define exactly flow customer-status-api-flow and "
        "sub-flow build-customer-status-response."
    ),
    (
        "In the main flow set variable customerId to "
        "`#[attributes.uriParams.customerId as String]`, then flow-ref only "
        "build-customer-status-response. In that sub-flow use ee:set-payload with exactly resource "
        "dw/customer-status-response.dwl. Include no inboundProperties, flowVars, outbound HTTP, "
        "DB, email, file, FTP, JMS, object-store, SFTP, sockets or VM connector."
    ),
    (
        "The DataWeave file must begin exactly `%dw 2.0`, `output application/json`, `---` on "
        "successive lines and emit the arbitrary `vars.customerId`, fixed status `ACTIVE`, and "
        "source `synthetic-fixture`; do not hard-code CUST-100 or use Mule 3 syntax."
    ),
    (
        "Create only MUnit config customer-status-api-test-suite and one test named "
        "build-customer-status-response-test with description `Builds a synthetic ACTIVE customer "
        "response`, in exact behavior/execution/validation order. Behavior must set-event with "
        'cloneOriginalEvent=false and one customerId variable value `#["CUST-100"]`, mediaType '
        "text/plain and encoding UTF-8; execution must flow-ref the real "
        "build-customer-status-response sub-flow."
    ),
    (
        "The MUnit validation phase must contain exactly three munit-tools:assert-that elements: "
        "payload.customerId equals CUST-100 with message `The customer ID must be preserved`; "
        "payload.status equals ACTIVE with message `The synthetic status must remain ACTIVE`; and "
        "payload.source equals synthetic-fixture with message `The fixture provenance must be "
        "explicit`. This test proves only the response sub-flow; do not claim listener, Maven, "
        "deployment or live-runtime success."
    ),
)

MULE3_RUNTIME = "3.9.5"
MULE4_RUNTIME = "4.9.20"
JAVA_VERSION = "17"
DATAWEAVE_VERSION = "2.0"
MUNIT_VERSION = "3.7.3"
MULE_MAVEN_PLUGIN_VERSION = "4.10.1"
HTTP_CONNECTOR_VERSION = "1.12.0"
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024

STATIC_CHECKS = (
    "exact-inventory",
    "legacy-byte-preservation",
    "safe-structured-parsing",
    "mule3-domain-contract",
    "mule4-domain-contract",
    "dataweave-version-mapping",
    "munit-version-mapping",
    "pom-artifact-version-consistency",
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

    _validate_mule3(xml_roots[MULE3_APP], xml_roots[MULE3_TEST], texts)
    _validate_mule4(xml_roots[MULE4_APP], texts)
    _validate_dataweave(texts[MULE4_DATAWEAVE])
    _validate_munit(xml_roots[MULE4_TEST])
    versions = _validate_pom(xml_roots[MULE4_POM])
    artifact = _parse_json(texts[MULE4_ARTIFACT], MULE4_ARTIFACT)
    _validate_artifact(artifact, versions)
    _validate_properties(texts[MULE4_PROPERTIES])
    _reject_secrets(texts)
    _reject_outbound_connectors(xml_roots)

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


def _validate_mule3(
    app: ElementTree.Element,
    test: ElementTree.Element,
    texts: Mapping[str, str],
) -> None:
    _require(app.tag == _tag(CORE, "mule"), MuleSoftLocalCheckCode.MULE3_CONTRACT, MULE3_APP)
    listener_config = app.find(_tag(HTTP, "listener-config"))
    _require(listener_config is not None, MuleSoftLocalCheckCode.MULE3_CONTRACT, MULE3_APP)
    assert listener_config is not None
    _require(
        listener_config.attrib
        == {
            "name": "customer-status-http-listener",
            "host": "${http.host}",
            "port": "${http.port}",
            "basePath": "/api",
        },
        MuleSoftLocalCheckCode.MULE3_CONTRACT,
        MULE3_APP,
    )
    _require(
        listener_config.find(_tag(HTTP, "listener-connection")) is None,
        MuleSoftLocalCheckCode.MULE3_CONTRACT,
        MULE3_APP,
    )
    listener = app.find(f".//{_tag(HTTP, 'listener')}")
    _require(listener is not None, MuleSoftLocalCheckCode.MULE3_CONTRACT, MULE3_APP)
    assert listener is not None
    _require(
        listener.attrib.get("allowedMethods") == "GET"
        and listener.attrib.get("path") == "/customers/{customerId}/status",
        MuleSoftLocalCheckCode.MULE3_CONTRACT,
        MULE3_APP,
    )
    app_text = texts[MULE3_APP]
    dataweave = app.find(f".//{_tag(DW1, 'set-payload')}")
    _require(
        dataweave is not None
        and dataweave.text is not None
        and "%dw 1.0" in dataweave.text
        and "%output application/json" in dataweave.text
        and "flowVars.customerId" in dataweave.text
        and "flowVars.responseStatus" in dataweave.text
        and "inboundProperties.'http.uri.params'.customerId" in app_text
        and "attributes.uriParams" not in app_text,
        MuleSoftLocalCheckCode.MULE3_CONTRACT,
        MULE3_APP,
    )
    _require(
        test.find(f".//{_tag(MUNIT, 'set')}") is not None
        and test.find(f".//{_tag(MUNIT, 'invocation-property')}") is not None
        and test.find(f".//{_tag(MUNIT, 'assert-on-equals')}") is not None,
        MuleSoftLocalCheckCode.MULE3_CONTRACT,
        MULE3_TEST,
    )
    _require(
        _parse_properties(texts[MULE3_PROPERTIES], MULE3_PROPERTIES)
        == {"http.host": "127.0.0.1", "http.port": "8081"},
        MuleSoftLocalCheckCode.MULE3_CONTRACT,
        MULE3_PROPERTIES,
    )


def _validate_mule4(app: ElementTree.Element, texts: Mapping[str, str]) -> None:
    _require(app.tag == _tag(CORE, "mule"), MuleSoftLocalCheckCode.MULE4_CONTRACT, MULE4_APP)
    properties = app.find(_tag(CORE, "configuration-properties"))
    _require(
        properties is not None and properties.attrib == {"file": "application.yaml"},
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )
    listener_config = app.find(_tag(HTTP, "listener-config"))
    _require(listener_config is not None, MuleSoftLocalCheckCode.MULE4_CONTRACT, MULE4_APP)
    assert listener_config is not None
    _require(
        listener_config.attrib == {"name": "customer-status-http-listener", "basePath": "/api"},
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )
    connection = listener_config.find(_tag(HTTP, "listener-connection"))
    _require(
        connection is not None
        and connection.attrib == {"host": "${http.host}", "port": "${http.port}"},
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )
    listener = app.find(f".//{_tag(HTTP, 'listener')}")
    _require(
        listener is not None
        and listener.attrib.get("allowedMethods") == "GET"
        and listener.attrib.get("path") == "/customers/{customerId}/status",
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )
    flows = {
        element.attrib.get("name")
        for local in ("flow", "sub-flow")
        for element in app.findall(_tag(CORE, local))
    }
    _require(
        flows == {"customer-status-api-flow", "build-customer-status-response"},
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )
    variable = app.find(f".//{_tag(CORE, 'set-variable')}")
    _require(
        variable is not None
        and variable.attrib.get("variableName") == "customerId"
        and variable.attrib.get("value") == "#[attributes.uriParams.customerId as String]",
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )
    flow_ref = app.find(f".//{_tag(CORE, 'flow-ref')}")
    _require(
        flow_ref is not None and flow_ref.attrib.get("name") == "build-customer-status-response",
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )
    payload = app.find(f".//{_tag(EE, 'set-payload')}")
    _require(
        payload is not None and payload.attrib == {"resource": "dw/customer-status-response.dwl"},
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )
    app_text = texts[MULE4_APP]
    _require(
        "attributes.uriParams.customerId" in app_text
        and "inboundProperties" not in app_text
        and "flowVars" not in app_text,
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_APP,
    )


def _validate_dataweave(text: str) -> None:
    normalized = text.replace("\r\n", "\n")
    _require(
        normalized.startswith("%dw 2.0\noutput application/json\n---\n")
        and "vars.customerId" in normalized
        and 'status: "ACTIVE"' in normalized
        and 'source: "synthetic-fixture"' in normalized
        and "%dw 1.0" not in normalized
        and "flowVars" not in normalized
        and "inboundProperties" not in normalized,
        MuleSoftLocalCheckCode.DATAWEAVE_CONTRACT,
        MULE4_DATAWEAVE,
    )


def _validate_munit(root: ElementTree.Element) -> None:
    _require(root.tag == _tag(CORE, "mule"), MuleSoftLocalCheckCode.MUNIT_CONTRACT, MULE4_TEST)
    config = root.find(_tag(MUNIT, "config"))
    tests = root.findall(_tag(MUNIT, "test"))
    _require(
        config is not None
        and config.attrib == {"name": "customer-status-api-test-suite"}
        and tuple(child.tag for child in root)
        == (
            _tag(MUNIT, "config"),
            _tag(MUNIT, "test"),
        )
        and len(tests) == 1,
        MuleSoftLocalCheckCode.MUNIT_CONTRACT,
        MULE4_TEST,
    )
    test = tests[0]
    behavior = test.find(_tag(MUNIT, "behavior"))
    execution = test.find(_tag(MUNIT, "execution"))
    validation = test.find(_tag(MUNIT, "validation"))
    _require(
        test.attrib
        == {
            "name": "build-customer-status-response-test",
            "description": "Builds a synthetic ACTIVE customer response",
        }
        and tuple(child.tag for child in test)
        == (
            _tag(MUNIT, "behavior"),
            _tag(MUNIT, "execution"),
            _tag(MUNIT, "validation"),
        )
        and behavior is not None
        and execution is not None
        and validation is not None,
        MuleSoftLocalCheckCode.MUNIT_CONTRACT,
        MULE4_TEST,
    )
    assert behavior is not None
    assert execution is not None
    assert validation is not None
    set_event = test.find(f".//{_tag(MUNIT, 'set-event')}")
    variable = test.find(f".//{_tag(MUNIT, 'variable')}")
    flow_ref = test.find(f".//{_tag(CORE, 'flow-ref')}")
    _require(
        set_event is not None
        and set_event.attrib == {"cloneOriginalEvent": "false"}
        and variable is not None
        and variable.attrib
        == {
            "key": "customerId",
            "value": '#["CUST-100"]',
            "mediaType": "text/plain",
            "encoding": "UTF-8",
        }
        and flow_ref is not None
        and flow_ref.attrib == {"name": "build-customer-status-response"}
        and tuple(child.tag for child in behavior) == (_tag(MUNIT, "set-event"),)
        and tuple(child.tag for child in execution) == (_tag(CORE, "flow-ref"),),
        MuleSoftLocalCheckCode.MUNIT_CONTRACT,
        MULE4_TEST,
    )
    assert set_event is not None
    variables = set_event.find(_tag(MUNIT, "variables"))
    _require(
        variables is not None
        and tuple(child.tag for child in set_event) == (_tag(MUNIT, "variables"),)
        and tuple(child.tag for child in variables) == (_tag(MUNIT, "variable"),),
        MuleSoftLocalCheckCode.MUNIT_CONTRACT,
        MULE4_TEST,
    )
    assertions = validation.findall(_tag(MUNIT_TOOLS, "assert-that"))
    expected_assertions = {
        "#[payload.customerId]": {
            "expression": "#[payload.customerId]",
            "is": '#[MunitTools::equalTo("CUST-100")]',
            "message": "The customer ID must be preserved",
        },
        "#[payload.status]": {
            "expression": "#[payload.status]",
            "is": '#[MunitTools::equalTo("ACTIVE")]',
            "message": "The synthetic status must remain ACTIVE",
        },
        "#[payload.source]": {
            "expression": "#[payload.source]",
            "is": '#[MunitTools::equalTo("synthetic-fixture")]',
            "message": "The fixture provenance must be explicit",
        },
    }
    _require(
        len(assertions) == 3
        and tuple(child.tag for child in validation) == (_tag(MUNIT_TOOLS, "assert-that"),) * 3
        and {assertion.attrib.get("expression"): assertion.attrib for assertion in assertions}
        == expected_assertions,
        MuleSoftLocalCheckCode.MUNIT_CONTRACT,
        MULE4_TEST,
    )


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
    required_versions = {
        "app.runtime": MULE4_RUNTIME,
        "mule.maven.plugin.version": MULE_MAVEN_PLUGIN_VERSION,
        "munit.version": MUNIT_VERSION,
        "http.connector.version": HTTP_CONNECTOR_VERSION,
    }
    _require(
        all(values.get(name) == value for name, value in required_versions.items()),
        MuleSoftLocalCheckCode.VERSION_MISMATCH,
        MULE4_POM,
    )
    plugins = {
        plugin.findtext("m:artifactId", namespaces=ns): plugin
        for plugin in root.findall("m:build/m:plugins/m:plugin", ns)
    }
    _require(
        set(plugins) == {"mule-maven-plugin", "munit-maven-plugin"},
        MuleSoftLocalCheckCode.POM_CONTRACT,
        MULE4_POM,
    )
    mule_plugin = plugins["mule-maven-plugin"]
    munit_plugin = plugins["munit-maven-plugin"]
    _require(
        mule_plugin.findtext("m:groupId", namespaces=ns) == "org.mule.tools.maven"
        and mule_plugin.findtext("m:version", namespaces=ns) == "${mule.maven.plugin.version}"
        and mule_plugin.findtext("m:extensions", namespaces=ns) == "true"
        and mule_plugin.findtext("m:configuration/m:runtimeVersion", namespaces=ns)
        == "${app.runtime}"
        and munit_plugin.findtext("m:groupId", namespaces=ns) == "com.mulesoft.munit.tools"
        and munit_plugin.findtext("m:version", namespaces=ns) == "${munit.version}"
        and munit_plugin.findtext("m:executions/m:execution/m:phase", namespaces=ns) == "test"
        and munit_plugin.findtext("m:executions/m:execution/m:goals/m:goal", namespaces=ns)
        == "test",
        MuleSoftLocalCheckCode.POM_CONTRACT,
        MULE4_POM,
    )
    dependencies = {
        dependency.findtext("m:artifactId", namespaces=ns): dependency
        for dependency in root.findall("m:dependencies/m:dependency", ns)
    }
    _require(
        set(dependencies) == {"mule-http-connector", "munit-runner", "munit-tools"},
        MuleSoftLocalCheckCode.POM_CONTRACT,
        MULE4_POM,
    )
    expected_dependencies = {
        "mule-http-connector": ("org.mule.connectors", "${http.connector.version}", None),
        "munit-runner": ("com.mulesoft.munit", "${munit.version}", "test"),
        "munit-tools": ("com.mulesoft.munit", "${munit.version}", "test"),
    }
    for artifact_id, (group_id, version, scope) in expected_dependencies.items():
        dependency = dependencies[artifact_id]
        _require(
            dependency.findtext("m:groupId", namespaces=ns) == group_id
            and dependency.findtext("m:version", namespaces=ns) == version
            and dependency.findtext("m:classifier", namespaces=ns) == "mule-plugin"
            and dependency.findtext("m:scope", namespaces=ns) == scope,
            MuleSoftLocalCheckCode.POM_CONTRACT,
            MULE4_POM,
        )
    repository_urls = {
        element.text
        for element in (
            *root.findall("m:repositories/m:repository/m:url", ns),
            *root.findall("m:pluginRepositories/m:pluginRepository/m:url", ns),
        )
    }
    _require(
        repository_urls == {"https://repository.mulesoft.org/releases/"}
        and root.find("m:distributionManagement", ns) is None
        and root.find("m:servers", ns) is None,
        MuleSoftLocalCheckCode.POM_CONTRACT,
        MULE4_POM,
    )
    return required_versions


def _validate_artifact(value: Any, versions: Mapping[str, str]) -> None:
    expected = {
        "minMuleVersion": MULE4_RUNTIME,
        "javaSpecificationVersions": [JAVA_VERSION],
        "requiredProduct": "MULE_EE",
    }
    _require(
        isinstance(value, dict) and value == expected,
        MuleSoftLocalCheckCode.ARTIFACT_CONTRACT,
        MULE4_ARTIFACT,
    )
    _require(
        value["minMuleVersion"] == versions["app.runtime"],
        MuleSoftLocalCheckCode.VERSION_MISMATCH,
        MULE4_ARTIFACT,
    )


def _validate_properties(text: str) -> None:
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.MALFORMED_YAML,
            MULE4_PROPERTIES,
        ) from exc
    _require(
        value == {"http": {"host": "127.0.0.1", "port": "8081"}},
        MuleSoftLocalCheckCode.MULE4_CONTRACT,
        MULE4_PROPERTIES,
    )


def _parse_json(text: str, artifact: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MuleSoftLocalCheckFailure(
            MuleSoftLocalCheckCode.MALFORMED_JSON,
            artifact,
        ) from exc


def _parse_properties(text: str, artifact: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if "=" not in line:
            raise MuleSoftLocalCheckFailure(
                MuleSoftLocalCheckCode.MULE3_CONTRACT,
                artifact,
            )
        key, value = (item.strip() for item in line.split("=", 1))
        if not key or key in values:
            raise MuleSoftLocalCheckFailure(
                MuleSoftLocalCheckCode.MULE3_CONTRACT,
                artifact,
            )
        values[key] = value
    return values


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
            if forbidden_namespace or outbound_http:
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
    "MuleSoftCandidateValidationSummary",
    "MuleSoftLocalCheckCode",
    "MuleSoftLocalCheckFailure",
    "SOURCE_FILES",
    "TARGET_FILES",
    "check_mulesoft_candidate",
    "main",
]
