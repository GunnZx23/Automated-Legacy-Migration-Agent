"""Controller-owned Mule behavior contracts and bounded evidence operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape

from pydantic import Field, model_validator

from legacy_migration_agent.contracts import ArtifactDigest, Sha256Digest, StrictModel
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.platforms import mulesoft_local_checks, mulesoft_validation

MuleSoftEvidenceError = mulesoft_validation.MuleSoftEvidenceError

_SOURCE_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
_CONTROLLER_BEHAVIOR_CONTRACT_PATH: Final = (
    _SOURCE_REPOSITORY_ROOT / "tooling/mulesoft-runtime/behavior-contract.json"
)
_CONTROLLER_BEHAVIOR_SUITE_PATH: Final = (
    _SOURCE_REPOSITORY_ROOT
    / "tooling/mulesoft-runtime/controller-tests/customer-status-behavior-test.xml"
)
_RELEASED_CONTROLLER_BEHAVIOR_CONTRACT_SHA256: Final = (
    "sha256:de3b1bd6151df82c94ffb3d41105544404bf4432ab0853d6544820f5deface11"
)
_RELEASED_CONTROLLER_BEHAVIOR_SUITE_SHA256: Final = (
    "sha256:1a429b0c94c46ccab23eadc229f1a04d52e739f6b93d88174bacac988a5b4dda"
)
_CONTROLLER_BEHAVIOR_MAX_BYTES: Final = 64 * 1024
_CONTROLLER_RUNTIME_TEST_PATH: Final = (
    "mule4/customer-status-api/src/test/munit/controller-customer-status-behavior-test.xml"
)
_CONTROLLER_BEHAVIOR_EXPECTATIONS: Final = (
    ("#[payload.customerId]", '#[MunitTools::equalTo("CTRL-CUST-9001")]'),
    ("#[payload.status]", '#[MunitTools::equalTo("ACTIVE")]'),
    ("#[payload.source]", '#[MunitTools::equalTo("synthetic-fixture")]'),
)
_CONTROLLER_XML_GUARD: Final = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class _ControllerBehaviorExpectation(StrictModel):
    expression: str = Field(min_length=1, max_length=256)
    matcher: str = Field(min_length=1, max_length=512)


class _ControllerBehaviorContract(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    contract_id: Literal["customer-status-api-behavior-v1"]
    runtime_relative_path: Literal[
        "mule4/customer-status-api/src/test/munit/controller-customer-status-behavior-test.xml"
    ]
    suite_name: Literal["controller-customer-status-behavior-test-suite"]
    test_name: Literal["controller-build-customer-status-response-contract"]
    flow_name_placeholder: Literal["__CONTROLLER_ENTRY_FLOW__"]
    request_method: Literal["GET"]
    request_path: Literal["/api/customers/CTRL-CUST-9001/status"]
    expectations: tuple[_ControllerBehaviorExpectation, ...] = Field(min_length=3, max_length=3)
    suite_sha256: Sha256Digest

    @model_validator(mode="after")
    def require_exact_behavior(self) -> _ControllerBehaviorContract:
        observed = tuple(
            (expectation.expression, expectation.matcher) for expectation in self.expectations
        )
        if observed != _CONTROLLER_BEHAVIOR_EXPECTATIONS:
            raise ValueError("controller behavior expectations differ from the released contract")
        if self.suite_sha256 != _RELEASED_CONTROLLER_BEHAVIOR_SUITE_SHA256:
            raise ValueError("controller behavior suite differs from the released identity")
        return self


class _ControllerBehaviorBinding(StrictModel):
    contract_id: Literal["customer-status-api-behavior-v1"]
    contract_sha256: Sha256Digest
    suite_sha256: Sha256Digest
    expectations_digest: Sha256Digest
    runtime_relative_path: str
    suite_name: str
    test_name: str


@dataclass(frozen=True)
class _ControllerBehaviorLoad:
    contract: _ControllerBehaviorContract | None
    binding: _ControllerBehaviorBinding | None
    suite_payload: bytes | None
    contract_digest: str
    suite_digest: str
    reason: str


class _ControllerBehaviorReportBinding(StrictModel):
    contract_binding_digest: Sha256Digest
    rendered_suite_sha256: Sha256Digest
    controller_report: ArtifactDigest
    controller_suite_name: str
    controller_test_name: str
    candidate_reports: tuple[ArtifactDigest, ...] = Field(min_length=1, max_length=127)
    candidate_suite_names: tuple[str, ...] = Field(min_length=1, max_length=127)
    candidate_test_count: int = Field(ge=1, le=10_000)


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("authority manifest contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"authority manifest contains unsupported JSON constant: {value}")


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _file_digest(path: Path) -> str:
    expected = path.lstat()
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
        raise PolicyViolation("digest input must be a regular non-symlink file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise PolicyViolation("digest input changed while being opened")
        while payload := os.read(descriptor, 64 * 1024):
            digest.update(payload)
        if _file_identity(os.fstat(descriptor)) != _file_identity(opened):
            raise PolicyViolation("digest input changed while being read")
    finally:
        os.close(descriptor)
    return f"sha256:{digest.hexdigest()}"


def _read_regular_file(path: Path, *, max_bytes: int, role: str) -> bytes:
    try:
        expected = path.lstat()
    except FileNotFoundError as exc:
        raise MuleSoftEvidenceError(f"{role} does not exist") from exc
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
        raise PolicyViolation(f"{role} must be a regular non-symlink file")
    if expected.st_size > max_bytes:
        raise MuleSoftEvidenceError(f"{role} exceeds its byte limit")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise PolicyViolation(f"{role} changed while being opened")
        payload = b""
        while chunk := os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(payload))):
            payload += chunk
            if len(payload) > max_bytes:
                raise MuleSoftEvidenceError(f"{role} exceeds its byte limit")
        if _file_identity(os.fstat(descriptor)) != _file_identity(opened):
            raise PolicyViolation(f"{role} changed while being read")
    finally:
        os.close(descriptor)
    return payload


def _safe_directory(path: Path, role: str) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PolicyViolation(f"{role} does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation(f"{role} must be a non-symlink directory")
    return path.resolve(strict=True)


def _safe_descendant_directory(root: Path, path: Path, role: str) -> Path:
    safe_root = _safe_directory(root, "containment root")
    safe_path = _safe_directory(path, role)
    try:
        safe_path.relative_to(safe_root)
    except ValueError as exc:
        raise PolicyViolation(f"{role} escapes its controller-owned root") from exc
    current = safe_root
    for part in safe_path.relative_to(safe_root).parts:
        current = current / part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PolicyViolation(f"{role} contains an unsafe path component")
    return safe_path


def _behavior_unavailable(
    state: str,
    *,
    contract_digest: str | None = None,
    suite_digest: str | None = None,
) -> _ControllerBehaviorLoad:
    return _ControllerBehaviorLoad(
        contract=None,
        binding=None,
        suite_payload=None,
        contract_digest=contract_digest or artifact_digest({"controller_behavior_contract": state}),
        suite_digest=suite_digest or artifact_digest({"controller_behavior_suite": state}),
        reason=f"controller-behavior-{state}",
    )


def load_controller_behavior_contract(
    contract_path: Path,
    suite_path: Path,
    *,
    released_contract_sha256: str,
    released_suite_sha256: str,
) -> _ControllerBehaviorLoad:
    """Load the independently authored suite only when both release pins match."""

    try:
        contract_payload = _read_regular_file(
            contract_path,
            max_bytes=_CONTROLLER_BEHAVIOR_MAX_BYTES,
            role="controller Mule behavior contract",
        )
    except (MuleSoftEvidenceError, OSError, PolicyViolation):
        return _behavior_unavailable("contract-unreadable")
    contract_digest = f"sha256:{hashlib.sha256(contract_payload).hexdigest()}"
    if contract_digest != released_contract_sha256:
        return _behavior_unavailable("contract-pin-mismatch", contract_digest=contract_digest)
    try:
        raw = json.loads(
            contract_payload.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
        contract = _ControllerBehaviorContract.model_validate(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return _behavior_unavailable("contract-invalid", contract_digest=contract_digest)
    try:
        suite_payload = _read_regular_file(
            suite_path,
            max_bytes=_CONTROLLER_BEHAVIOR_MAX_BYTES,
            role="controller Mule behavior suite",
        )
    except (MuleSoftEvidenceError, OSError, PolicyViolation):
        return _behavior_unavailable("suite-unreadable", contract_digest=contract_digest)
    suite_digest = f"sha256:{hashlib.sha256(suite_payload).hexdigest()}"
    if suite_digest != released_suite_sha256 or suite_digest != contract.suite_sha256:
        return _behavior_unavailable(
            "suite-pin-mismatch",
            contract_digest=contract_digest,
            suite_digest=suite_digest,
        )
    try:
        _validate_controller_behavior_suite(suite_payload, contract)
    except (ElementTree.ParseError, UnicodeError, ValueError):
        return _behavior_unavailable(
            "suite-invalid",
            contract_digest=contract_digest,
            suite_digest=suite_digest,
        )
    binding = _ControllerBehaviorBinding(
        contract_id=contract.contract_id,
        contract_sha256=contract_digest,
        suite_sha256=suite_digest,
        expectations_digest=artifact_digest(
            tuple(expectation.model_dump(mode="json") for expectation in contract.expectations)
        ),
        runtime_relative_path=contract.runtime_relative_path,
        suite_name=contract.suite_name,
        test_name=contract.test_name,
    )
    return _ControllerBehaviorLoad(
        contract=contract,
        binding=binding,
        suite_payload=suite_payload,
        contract_digest=contract_digest,
        suite_digest=suite_digest,
        reason="controller-behavior-verified",
    )


def _validate_controller_behavior_suite(
    payload: bytes,
    contract: _ControllerBehaviorContract,
) -> None:
    text = payload.decode("utf-8")
    if _CONTROLLER_XML_GUARD.search(text):
        raise ValueError("controller behavior suite contains an unsafe XML declaration")
    root = ElementTree.fromstring(text)

    def tag(namespace: str, name: str) -> str:
        return f"{{{namespace}}}{name}"

    if root.tag != tag(mulesoft_local_checks.CORE, "mule"):
        raise ValueError("controller behavior suite root is invalid")
    if tuple(child.tag for child in root) != (
        tag(mulesoft_local_checks.HTTP, "request-config"),
        tag(mulesoft_local_checks.MUNIT, "config"),
        tag(mulesoft_local_checks.MUNIT, "test"),
    ):
        raise ValueError("controller behavior suite has unexpected top-level elements")
    request_config, config, test = tuple(root)
    if (
        request_config.attrib != {"name": "controller-customer-status-request"}
        or tuple(child.tag for child in request_config)
        != (tag(mulesoft_local_checks.HTTP, "request-connection"),)
        or tuple(request_config)[0].attrib != {"host": "127.0.0.1", "port": "8081"}
    ):
        raise ValueError("controller behavior request boundary is invalid")
    if config.attrib != {"name": contract.suite_name}:
        raise ValueError("controller behavior suite identity is invalid")
    if test.attrib.get("name") != contract.test_name:
        raise ValueError("controller behavior test identity is invalid")
    if tuple(child.tag for child in test) != (
        tag(mulesoft_local_checks.MUNIT, "enable-flow-sources"),
        tag(mulesoft_local_checks.MUNIT, "execution"),
        tag(mulesoft_local_checks.MUNIT, "validation"),
    ):
        raise ValueError("controller behavior test phases are invalid")
    flow_sources, execution, validation = tuple(test)
    enabled_sources = flow_sources.findall(tag(mulesoft_local_checks.MUNIT, "enable-flow-source"))
    requests = execution.findall(tag(mulesoft_local_checks.HTTP, "request"))
    if (
        len(enabled_sources) != 1
        or enabled_sources[0].attrib != {"value": contract.flow_name_placeholder}
        or len(requests) != 1
        or requests[0].attrib
        != {
            "method": contract.request_method,
            "path": contract.request_path,
            "config-ref": "controller-customer-status-request",
        }
    ):
        raise ValueError("controller behavior setup or execution is invalid")
    assertions = validation.findall(tag(mulesoft_local_checks.MUNIT_TOOLS, "assert-that"))
    observed = tuple(
        (assertion.attrib.get("expression"), assertion.attrib.get("is")) for assertion in assertions
    )
    if (
        len(assertions) != 3
        or tuple(child.tag for child in validation)
        != (tag(mulesoft_local_checks.MUNIT_TOOLS, "assert-that"),) * 3
        or observed != _CONTROLLER_BEHAVIOR_EXPECTATIONS
    ):
        raise ValueError("controller behavior assertions are invalid")


def _install_controller_behavior_suite(
    candidate_root: Path,
    contract: _ControllerBehaviorContract,
    payload: bytes,
) -> str:
    root = _safe_directory(candidate_root, "controller behavior candidate root")
    _reject_reserved_controller_test_identity(root, contract)
    if contract.runtime_relative_path != _CONTROLLER_RUNTIME_TEST_PATH:
        raise PolicyViolation("controller behavior runtime path drifted")
    destination = root / contract.runtime_relative_path
    _safe_descendant_directory(root, destination.parent, "controller behavior suite parent")
    try:
        destination.lstat()
    except FileNotFoundError:
        pass
    else:
        raise PolicyViolation("candidate attempted to supply the controller behavior suite")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("controller behavior suite write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    rendered_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if _file_digest(destination) != rendered_digest:
        raise PolicyViolation("installed controller behavior suite digest mismatch")
    return rendered_digest


def _discover_public_listener_flow(candidate_root: Path) -> str:
    properties_payload = _read_regular_file(
        candidate_root / mulesoft_local_checks.MULE4_PROPERTIES,
        max_bytes=mulesoft_local_checks.MAX_ARTIFACT_BYTES,
        role="candidate Mule application properties",
    )
    payload = _read_regular_file(
        candidate_root / mulesoft_local_checks.MULE4_APP,
        max_bytes=mulesoft_validation.MAX_XML_REPORT_BYTES,
        role="candidate Mule application",
    )
    try:
        properties = mulesoft_local_checks.parse_mule_application_properties(
            properties_payload.decode("utf-8")
        )
    except (mulesoft_local_checks.MuleSoftLocalCheckFailure, UnicodeError) as exc:
        raise PolicyViolation("candidate Mule application properties are not safe YAML") from exc
    try:
        text = payload.decode("utf-8")
        if _CONTROLLER_XML_GUARD.search(text):
            raise PolicyViolation("candidate Mule application has an unsafe declaration")
        root = ElementTree.fromstring(text)
    except (ElementTree.ParseError, UnicodeError) as exc:
        raise PolicyViolation("candidate Mule application is not safe XML") from exc
    flow_tag = f"{{{mulesoft_local_checks.CORE}}}flow"
    listener_tag = f"{{{mulesoft_local_checks.HTTP}}}listener"
    listener_config_tag = f"{{{mulesoft_local_checks.HTTP}}}listener-config"
    listener_configs: dict[str, str | None] = {}
    for config in root.findall(listener_config_tag):
        name = config.attrib.get("name", "").strip()
        raw_base_path = config.attrib.get("basePath")
        base_path = mulesoft_local_checks.resolve_mule_property_value(raw_base_path, properties)
        if (
            not name
            or len(name) > 500
            or name in listener_configs
            or (raw_base_path is not None and base_path is None)
            or mulesoft_local_checks.normalize_http_route(base_path, "/") is None
        ):
            raise PolicyViolation("candidate listener configuration is not safely bounded")
        listener_configs[name] = base_path
    matches: list[str] = []
    for flow in root.findall(flow_tag):
        flow_name = flow.attrib.get("name", "").strip()
        for listener in flow.iter(listener_tag):
            methods = {
                method.strip().upper()
                for method in re.split(r"[\s,]+", listener.attrib.get("allowedMethods", ""))
                if method.strip()
            }
            config_ref = listener.attrib.get("config-ref")
            if config_ref is None or config_ref not in listener_configs:
                continue
            listener_path = mulesoft_local_checks.resolve_mule_property_value(
                listener.attrib.get("path"), properties
            )
            if listener_path is None:
                continue
            effective_route = mulesoft_local_checks.normalize_http_route(
                listener_configs[config_ref], listener_path
            )
            if effective_route == mulesoft_local_checks.MULESOFT_PUBLIC_ROUTE and methods == {
                "GET"
            }:
                matches.append(flow_name)
    if len(matches) != 1 or not matches[0] or len(matches[0]) > 500:
        raise PolicyViolation("candidate must expose exactly one bounded public listener flow")
    return matches[0]


def _render_controller_behavior_suite(
    template: bytes,
    contract: _ControllerBehaviorContract,
    flow_name: str,
) -> bytes:
    if f"sha256:{hashlib.sha256(template).hexdigest()}" != contract.suite_sha256:
        raise PolicyViolation("controller behavior template digest mismatch")
    placeholder = contract.flow_name_placeholder.encode("utf-8")
    if template.count(placeholder) != 1:
        raise PolicyViolation("controller behavior template placeholder is invalid")
    escaped_flow_name = xml_escape(flow_name, {'"': "&quot;"}).encode("utf-8")
    rendered = template.replace(placeholder, escaped_flow_name)
    try:
        root = ElementTree.fromstring(rendered.decode("utf-8"))
    except (ElementTree.ParseError, UnicodeError) as exc:
        raise PolicyViolation("rendered controller behavior suite is invalid") from exc
    enabled = root.findall(f".//{{{mulesoft_local_checks.MUNIT}}}enable-flow-source")
    if len(enabled) != 1 or enabled[0].attrib != {"value": flow_name}:
        raise PolicyViolation("rendered controller suite did not bind the public listener flow")
    return rendered


def _reject_reserved_controller_test_identity(
    candidate_root: Path,
    contract: _ControllerBehaviorContract,
) -> None:
    reserved = {contract.suite_name, contract.test_name}
    for relative_path in mulesoft_local_checks.TARGET_FILES:
        if "/src/test/munit/" not in relative_path or not relative_path.endswith(".xml"):
            continue
        path = candidate_root / relative_path
        payload = _read_regular_file(
            path,
            max_bytes=mulesoft_validation.MAX_XML_REPORT_BYTES,
            role="candidate-authored MUnit suite",
        )
        try:
            text = payload.decode("utf-8")
            if _CONTROLLER_XML_GUARD.search(text):
                raise PolicyViolation("candidate-authored MUnit suite has an unsafe declaration")
            root = ElementTree.fromstring(text)
        except (ElementTree.ParseError, UnicodeError) as exc:
            raise PolicyViolation("candidate-authored MUnit suite is not safe XML") from exc
        identities = (
            *root.findall(f"{{{mulesoft_local_checks.MUNIT}}}config"),
            *root.findall(f"{{{mulesoft_local_checks.MUNIT}}}test"),
        )
        if any(element.attrib.get("name") in reserved for element in identities):
            raise PolicyViolation("candidate-authored MUnit suite reused a reserved controller ID")


def _validate_controller_behavior_reports(
    reports: tuple[bytes, ...],
    artifacts: tuple[ArtifactDigest, ...],
    contract: _ControllerBehaviorContract,
    contract_binding_digest: str,
    rendered_suite_sha256: str,
) -> _ControllerBehaviorReportBinding:
    if len(reports) != len(artifacts):
        raise MuleSoftEvidenceError("MUnit report payload and artifact counts differ")
    controller_report: ArtifactDigest | None = None
    candidate_reports: list[ArtifactDigest] = []
    candidate_suite_names: list[str] = []
    candidate_test_count = 0
    observed_suite_names: set[str] = set()
    for payload, artifact in zip(reports, artifacts, strict=True):
        if f"sha256:{hashlib.sha256(payload).hexdigest()}" != artifact.sha256:
            raise MuleSoftEvidenceError("MUnit report artifact digest does not match its payload")
        try:
            text = payload.decode("utf-8")
            if _CONTROLLER_XML_GUARD.search(text):
                raise MuleSoftEvidenceError("MUnit report contains an unsafe XML declaration")
            root = ElementTree.fromstring(text)
        except (ElementTree.ParseError, UnicodeError) as exc:
            raise MuleSoftEvidenceError("MUnit report identity XML is invalid") from exc
        if root.tag.rsplit("}", 1)[-1] != "testsuite":
            raise MuleSoftEvidenceError("MUnit identity report must contain one direct suite")
        testcases = [child for child in root if child.tag.rsplit("}", 1)[-1] == "testcase"]
        suite_name = root.attrib.get("name", "")
        test_names = tuple(testcase.attrib.get("name", "") for testcase in testcases)
        if (
            not suite_name
            or len(suite_name) > 512
            or suite_name in observed_suite_names
            or not test_names
            or any(not name or len(name) > 512 for name in test_names)
        ):
            raise MuleSoftEvidenceError("MUnit suite or test identity is missing or duplicated")
        observed_suite_names.add(suite_name)
        if suite_name == contract.suite_name:
            if controller_report is not None or test_names != (contract.test_name,):
                raise MuleSoftEvidenceError("controller-owned MUnit report identity is invalid")
            controller_report = artifact
            continue
        if contract.test_name in test_names:
            raise MuleSoftEvidenceError("candidate MUnit report reused a reserved controller ID")
        candidate_reports.append(artifact)
        candidate_suite_names.append(suite_name)
        candidate_test_count += len(test_names)
    if controller_report is None:
        raise MuleSoftEvidenceError("MUnit evidence omitted the controller behavior suite")
    if not candidate_reports or candidate_test_count < 1:
        raise MuleSoftEvidenceError("MUnit evidence omitted candidate-authored tests")
    return _ControllerBehaviorReportBinding(
        contract_binding_digest=contract_binding_digest,
        rendered_suite_sha256=rendered_suite_sha256,
        controller_report=controller_report,
        controller_suite_name=contract.suite_name,
        controller_test_name=contract.test_name,
        candidate_reports=tuple(candidate_reports),
        candidate_suite_names=tuple(candidate_suite_names),
        candidate_test_count=candidate_test_count,
    )
