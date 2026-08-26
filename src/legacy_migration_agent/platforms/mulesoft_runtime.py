"""Session-bound Mule 3.9.5 to Mule 4.9.20 validation runtime.

Model-authored Mule XML, DataWeave, and Maven configuration are untrusted code.
Static checks therefore parse bounded snapshots in-process. Candidate Maven and
MUnit are never delegated to a caller-supplied runner. This module defines a
container authority schema and fixed isolation contract, but activates them only
when a separately source-pinned, code-reviewed manifest contains identities from
a real built artifact. The checked-in manifest is deliberately disabled.

If the pinned runtime, Java 17/Mule image, or offline dependencies are absent,
the required MUnit check is unavailable and candidate code is never executed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol
from xml.etree import ElementTree

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.contracts import (
    ApprovalAction,
    ArtifactDigest,
    ChangeSet,
    CheckResult,
    CheckStatus,
    EnvironmentKind,
    MigrationManifest,
    MigrationRequest,
    Platform,
    Sha256Digest,
    StrictModel,
    ToolReceipt,
    ValidationCommand,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.execution import TIMEOUT_EXIT_CODE, execution_binding
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import (
    PolicyViolation,
    validate_change_set,
    validate_manifest_for_request,
    validate_report,
)
from legacy_migration_agent.core.run_session import AgentRunSession
from legacy_migration_agent.core.scope_policy import (
    MigrationScopePolicy,
    PlatformAdapter,
    validate_manifest_transformation_scope,
)
from legacy_migration_agent.core.workspace import IsolatedWorkspace, WorkspaceChanges, snapshot_tree
from legacy_migration_agent.graphs.graph_contracts import NodeKind
from legacy_migration_agent.graphs.mulesoft_dependency_graph import (
    MULESOFT_ANALYZER_VERSION,
    build_mulesoft_dependency_graph,
)
from legacy_migration_agent.platforms.local_checks import tree_fingerprint
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    CORE,
    JAVA_VERSION,
    MULE3_APP,
    MULE4_APP,
    MULE4_RUNTIME,
    MULESOFT_IMPLEMENTATION_CONTRACT,
    MUNIT,
    MUNIT_TOOLS,
    SOURCE_FILES,
    TARGET_FILES,
    MuleSoftLocalCheckFailure,
    check_mulesoft_candidate,
)
from legacy_migration_agent.platforms.mulesoft_validation import (
    MAX_XML_REPORT_BYTES,
    MAX_XML_REPORTS,
    MuleSoftEvidenceError,
    MuleSoftValidationContext,
    MuleSoftValidationStatus,
    parse_munit_surefire_xml,
)
from legacy_migration_agent.platforms.platform_runtime import PlatformRuntimeConfig

MULESOFT_CANDIDATE_CONTRACT_COMMAND_ID: Final = "mulesoft-candidate-contract"
MULESOFT_DEPENDENCY_CLOSURE_COMMAND_ID: Final = "mulesoft-dependency-closure"
MULESOFT_TOOLCHAIN_CONTRACT_COMMAND_ID: Final = "mulesoft-toolchain-contract"
MULESOFT_MUNIT_COMMAND_ID: Final = "mulesoft-munit"
MULESOFT_WORKSPACE_FINGERPRINT_COMMAND_ID: Final = "mulesoft-workspace-fingerprint"
MULESOFT_RUNTIME_AUTHORITY_ANCHOR_KIND: Final = "mulesoft-munit-authority-v1"

MULESOFT_VALIDATION_COMMAND_IDS: Final = (
    MULESOFT_CANDIDATE_CONTRACT_COMMAND_ID,
    MULESOFT_DEPENDENCY_CLOSURE_COMMAND_ID,
    MULESOFT_TOOLCHAIN_CONTRACT_COMMAND_ID,
    MULESOFT_MUNIT_COMMAND_ID,
    MULESOFT_WORKSPACE_FINGERPRINT_COMMAND_ID,
)

MULESOFT_TARGET_RUNTIME: Final = "Mule 4.9.20 with Java 17"
MULESOFT_SOURCE_VERSION: Final = "Mule 3.9.5"
MULESOFT_TARGET_VERSION: Final = "Mule 4.9.20"

MULESOFT_RUNTIME_CONFIG: Final = PlatformRuntimeConfig(
    platform=Platform.MULESOFT,
    analyzer_version=MULESOFT_ANALYZER_VERSION,
    graph_builder=build_mulesoft_dependency_graph,
)

MULESOFT_SCOPE_POLICY: Final = MigrationScopePolicy(
    policy_id="mulesoft-mule3-to-mule4-v2",
    platform=Platform.MULESOFT,
    required_source_input_paths=SOURCE_FILES,
    approved_output_paths=TARGET_FILES,
    forbidden_paths=SOURCE_FILES,
    allowed_validation_command_ids=MULESOFT_VALIDATION_COMMAND_IDS,
    required_validation_command_ids=MULESOFT_VALIDATION_COMMAND_IDS,
    required_implementation_contract=MULESOFT_IMPLEMENTATION_CONTRACT,
    max_changed_files=len(TARGET_FILES),
    required_approval_actions=(ApprovalAction.APPROVE_MANIFEST,),
)

MULESOFT_PLATFORM_ADAPTER: Final = PlatformAdapter.bind(
    adapter_id="mulesoft-mule3-to-mule4-v2",
    policy=MULESOFT_SCOPE_POLICY,
)

# Container-visible command and routing are runtime-owned constants bound to the
# immutable image contract; neither callers nor model output can replace them.
MULESOFT_MUNIT_ARGV: Final = (
    "/opt/maven/bin/mvn",
    "--batch-mode",
    "--no-transfer-progress",
    "--offline",
    "--errors",
    "--settings",
    "/opt/maven/settings.xml",
    "-Dmaven.repo.local=/opt/maven/.m2/repository",
    "-DskipTests=false",
    "test",
)
MULESOFT_MUNIT_ENVIRONMENT: Final = (
    ("CI", "true"),
    ("HOME", "/scratch/home"),
    ("JAVA_HOME", "/opt/java"),
    ("MAVEN_OPTS", "-Djava.awt.headless=true -Dfile.encoding=UTF-8"),
    ("NO_COLOR", "1"),
    ("PATH", "/opt/maven/bin:/opt/java/bin:/usr/bin:/bin"),
    ("TMPDIR", "/scratch/tmp"),
)
MULESOFT_CONTAINER_WORKDIR: Final = "/work/mule4/customer-status-api"
MULESOFT_CONTAINER_REPORT_ROOT: Final = "/output/surefire-reports"

_SOURCE_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[3]
_AUTHORITY_MANIFEST_PATH: Final = (
    _SOURCE_REPOSITORY_ROOT / "tooling/mulesoft-runtime/authority.json"
)
# Activation is a source-code change as well as a manifest change. This remains
# None until a real built authority has checked-in provenance and review.
_RELEASED_AUTHORITY_MANIFEST_SHA256: Final[str | None] = None
_AUTHORITY_MANIFEST_MAX_BYTES: Final = 64 * 1024
_AUTHORITY_LABEL_KEYS: Final = frozenset(
    {
        "com.salesforce.legacy-migration.contract",
        "com.salesforce.legacy-migration.java",
        "com.salesforce.legacy-migration.maven",
        "com.salesforce.legacy-migration.mule",
        "com.salesforce.legacy-migration.mule-maven-plugin",
        "com.salesforce.legacy-migration.munit",
        "com.salesforce.legacy-migration.network-installer",
        "com.salesforce.legacy-migration.output-mode",
        "com.salesforce.legacy-migration.input-root",
        "com.salesforce.legacy-migration.work-root",
        "com.salesforce.legacy-migration.report-root",
        "com.salesforce.legacy-migration.toolchain-cache",
        "com.salesforce.legacy-migration.argv",
    }
)
_CONTAINER_DAEMON_ARGV_SUFFIX: Final = (
    "version",
    "--format",
    "{{json .Server}}",
)
_CONTAINER_HOST_ENVIRONMENT: Final = {
    "HOME": "/nonexistent",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}
_CONTAINER_COMMAND_TIMEOUT_SECONDS: Final = 15.0
_CONTAINER_VALIDATION_TIMEOUT_SECONDS: Final = 300.0
_CONTAINER_MAX_OUTPUT_BYTES: Final = 64 * 1024
_CONTAINER_MAX_REPORT_TOTAL_BYTES: Final = 20 * 1024 * 1024
_CONTAINER_MAX_REPORT_DIRECTORY_ENTRIES: Final = 128
_CONTAINER_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_CONTAINER_ID = re.compile(r"^[a-f0-9]{64}$")

_ORACLE_SEGMENTS: Final = frozenset({"expected", "golden", "oracle"})

_CONTROLLER_BEHAVIOR_CONTRACT_PATH: Final = (
    _SOURCE_REPOSITORY_ROOT / "tooling/mulesoft-runtime/behavior-contract.json"
)
_CONTROLLER_BEHAVIOR_SUITE_PATH: Final = (
    _SOURCE_REPOSITORY_ROOT
    / "tooling/mulesoft-runtime/controller-tests/customer-status-behavior-test.xml"
)
_RELEASED_CONTROLLER_BEHAVIOR_CONTRACT_SHA256: Final = (
    "sha256:28418b81effed1169b6584efde7467babf358b1f5e45426b2d6f19c8d5c60454"
)
_RELEASED_CONTROLLER_BEHAVIOR_SUITE_SHA256: Final = (
    "sha256:1c7ba92ae284754626d4790f3874cd61970731bb7f6317b0e6d8193d05add8a0"
)
_CONTROLLER_BEHAVIOR_MAX_BYTES: Final = 64 * 1024
_CONTROLLER_RUNTIME_TEST_PATH: Final = (
    "mule4/customer-status-api/src/test/munit/controller-customer-status-behavior-test.xml"
)
_GENERATED_MUNIT_SUITE_NAME: Final = "customer-status-api-test-suite"
_GENERATED_MUNIT_TEST_NAME: Final = "build-customer-status-response-test"
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
    """Source-pinned controller oracle that is never supplied by a candidate."""

    schema_version: Literal["1.0"] = "1.0"
    contract_id: Literal["customer-status-api-behavior-v1"]
    runtime_relative_path: Literal[
        "mule4/customer-status-api/src/test/munit/controller-customer-status-behavior-test.xml"
    ]
    suite_name: Literal["controller-customer-status-behavior-test-suite"]
    test_name: Literal["controller-build-customer-status-response-contract"]
    flow_name: Literal["build-customer-status-response"]
    expectations: tuple[_ControllerBehaviorExpectation, ...] = Field(
        min_length=3,
        max_length=3,
    )
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
    controller_report: ArtifactDigest
    controller_suite_name: str
    controller_test_name: str
    supplemental_report: ArtifactDigest
    supplemental_suite_name: str
    supplemental_test_name: str


def _container_execution_contract_digest() -> str:
    return artifact_digest(
        {
            "munit_argv": MULESOFT_MUNIT_ARGV,
            "munit_environment": MULESOFT_MUNIT_ENVIRONMENT,
            "working_directory": MULESOFT_CONTAINER_WORKDIR,
            "report_root": MULESOFT_CONTAINER_REPORT_ROOT,
            "pull": "never",
            "network": "none",
            "read_only_rootfs": True,
            "cap_drop": "ALL",
            "no_new_privileges": True,
            "pids": 128,
            "pid_namespace": "private",
            "ipc_namespace": "private",
            "cgroup_namespace": "private",
            "memory": "1024m",
            "cpus": "1.0",
            "temp_tmpfs": "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "work_tmpfs": "/work:rw,nosuid,nodev,size=768m",
            "log_driver": "none",
            "lifecycle": ("create", "inspect", "start", "wait", "kill", "remove"),
        }
    )


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


def _load_controller_behavior_contract(
    contract_path: Path | None = None,
    suite_path: Path | None = None,
) -> _ControllerBehaviorLoad:
    """Load the independently authored suite only when both release pins match."""

    contract_path = contract_path or _CONTROLLER_BEHAVIOR_CONTRACT_PATH
    suite_path = suite_path or _CONTROLLER_BEHAVIOR_SUITE_PATH
    try:
        contract_payload = _read_regular_file(
            contract_path,
            max_bytes=_CONTROLLER_BEHAVIOR_MAX_BYTES,
            role="controller Mule behavior contract",
        )
    except (MuleSoftEvidenceError, OSError, PolicyViolation):
        return _behavior_unavailable("contract-unreadable")
    contract_digest = f"sha256:{hashlib.sha256(contract_payload).hexdigest()}"
    if contract_digest != _RELEASED_CONTROLLER_BEHAVIOR_CONTRACT_SHA256:
        return _behavior_unavailable(
            "contract-pin-mismatch",
            contract_digest=contract_digest,
        )
    try:
        raw = json.loads(
            contract_payload.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
        contract = _ControllerBehaviorContract.model_validate(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return _behavior_unavailable(
            "contract-invalid",
            contract_digest=contract_digest,
        )
    try:
        suite_payload = _read_regular_file(
            suite_path,
            max_bytes=_CONTROLLER_BEHAVIOR_MAX_BYTES,
            role="controller Mule behavior suite",
        )
    except (MuleSoftEvidenceError, OSError, PolicyViolation):
        return _behavior_unavailable(
            "suite-unreadable",
            contract_digest=contract_digest,
        )
    suite_digest = f"sha256:{hashlib.sha256(suite_payload).hexdigest()}"
    if (
        suite_digest != _RELEASED_CONTROLLER_BEHAVIOR_SUITE_SHA256
        or suite_digest != contract.suite_sha256
    ):
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

    if root.tag != tag(CORE, "mule"):
        raise ValueError("controller behavior suite root is invalid")
    if tuple(child.tag for child in root) != (tag(MUNIT, "config"), tag(MUNIT, "test")):
        raise ValueError("controller behavior suite has unexpected top-level elements")
    config, test = tuple(root)
    if config.attrib != {"name": contract.suite_name}:
        raise ValueError("controller behavior suite identity is invalid")
    if test.attrib.get("name") != contract.test_name:
        raise ValueError("controller behavior test identity is invalid")
    if tuple(child.tag for child in test) != (
        tag(MUNIT, "behavior"),
        tag(MUNIT, "execution"),
        tag(MUNIT, "validation"),
    ):
        raise ValueError("controller behavior test phases are invalid")
    behavior, execution, validation = tuple(test)
    set_events = behavior.findall(tag(MUNIT, "set-event"))
    variables = behavior.findall(f".//{tag(MUNIT, 'variable')}")
    flow_refs = execution.findall(tag(CORE, "flow-ref"))
    if (
        len(set_events) != 1
        or set_events[0].attrib != {"cloneOriginalEvent": "false"}
        or len(variables) != 1
        or variables[0].attrib
        != {
            "key": "customerId",
            "value": '#["CTRL-CUST-9001"]',
            "mediaType": "text/plain",
            "encoding": "UTF-8",
        }
        or len(flow_refs) != 1
        or flow_refs[0].attrib != {"name": contract.flow_name}
    ):
        raise ValueError("controller behavior setup or execution is invalid")
    assertions = validation.findall(tag(MUNIT_TOOLS, "assert-that"))
    observed = tuple(
        (assertion.attrib.get("expression"), assertion.attrib.get("is")) for assertion in assertions
    )
    if (
        len(assertions) != 3
        or tuple(child.tag for child in validation) != (tag(MUNIT_TOOLS, "assert-that"),) * 3
        or observed != _CONTROLLER_BEHAVIOR_EXPECTATIONS
    ):
        raise ValueError("controller behavior assertions are invalid")


class _DisabledContainerAuthorityManifest(StrictModel):
    """Checked-in declaration that no executable Mule authority exists."""

    schema_version: Literal["1.0"] = "1.0"
    enabled: Literal[False]
    disabled_reason: str = Field(min_length=16, max_length=512)


class _ContainerToolchainProbeContract(StrictModel):
    entrypoint_sha256: Sha256Digest
    maven_settings_sha256: Sha256Digest
    offline_repository_tree_sha256: Sha256Digest
    license_artifact_sha256: Sha256Digest
    java_version: str = Field(min_length=1, max_length=128)
    maven_version: str = Field(min_length=1, max_length=128)
    mule_runtime_version: str = Field(min_length=1, max_length=128)
    mule_maven_plugin_version: str = Field(min_length=1, max_length=128)
    munit_version: str = Field(min_length=1, max_length=128)


class _EnabledContainerAuthorityManifest(StrictModel):
    """Code-reviewed identities required before container execution is possible."""

    schema_version: Literal["1.0"] = "1.0"
    enabled: Literal[True]
    execution_contract_sha256: Sha256Digest
    cli_path: str = Field(min_length=1, max_length=4096)
    cli_sha256: Sha256Digest
    engine_version: str = Field(min_length=1, max_length=64)
    engine_api_version: str = Field(min_length=1, max_length=64)
    engine_os: Literal["linux"]
    engine_architecture: Literal["amd64"]
    image_ref: str = Field(min_length=1, max_length=4096)
    image_digest: Sha256Digest
    image_config_digest: Sha256Digest
    image_os: Literal["linux"]
    image_architecture: Literal["amd64"]
    rootfs_diff_ids: tuple[Sha256Digest, ...] = Field(min_length=1, max_length=128)
    entrypoint: tuple[str, ...] = Field(min_length=1, max_length=16)
    default_command: tuple[str, ...] = Field(min_length=1, max_length=16)
    image_environment: tuple[str, ...] = Field(max_length=128)
    working_directory: str = Field(min_length=1, max_length=4096)
    user: str = Field(min_length=3, max_length=64)
    labels: tuple[tuple[str, str], ...] = Field(min_length=1, max_length=64)
    toolchain_probe: _ContainerToolchainProbeContract

    @model_validator(mode="after")
    def require_complete_immutable_authority(self) -> _EnabledContainerAuthorityManifest:
        if self.execution_contract_sha256 != _container_execution_contract_digest():
            raise ValueError("authority manifest does not bind the fixed execution contract")
        cli_path = Path(self.cli_path)
        if not cli_path.is_absolute() or any(part in {"", ".", ".."} for part in cli_path.parts):
            raise ValueError("authority CLI path must be absolute and normalized")
        image_name, separator, digest = self.image_ref.rpartition("@")
        if (
            separator != "@"
            or digest != self.image_digest
            or not image_name
            or ":" in image_name.rsplit("/", 1)[-1]
        ):
            raise ValueError("authority image must use only its immutable manifest digest")
        if len(set(self.rootfs_diff_ids)) != len(self.rootfs_diff_ids):
            raise ValueError("authority image RootFS DiffIDs must be unique and ordered")
        if any(
            not value or len(value) > 4096 for value in (*self.entrypoint, *self.default_command)
        ):
            raise ValueError("authority image command fields are invalid")
        if self.working_directory != MULESOFT_CONTAINER_WORKDIR:
            raise ValueError("authority working directory does not match the fixed contract")
        user_parts = self.user.split(":")
        if (
            len(user_parts) != 2
            or any(not part.isascii() or not part.isdigit() for part in user_parts)
            or any(int(part) <= 0 for part in user_parts)
        ):
            raise ValueError("authority image must use an explicit nonroot uid and gid")
        environment_names: set[str] = set()
        for value in self.image_environment:
            name, separator, _ = value.partition("=")
            if separator != "=" or not name or name in environment_names:
                raise ValueError("authority image environment is malformed or duplicated")
            environment_names.add(name)
        labels = dict(self.labels)
        if len(labels) != len(self.labels) or set(labels) != _AUTHORITY_LABEL_KEYS:
            raise ValueError("authority image labels are incomplete or duplicated")
        required_labels = {
            "com.salesforce.legacy-migration.contract": "mulesoft-munit-container-v1",
            "com.salesforce.legacy-migration.java": self.toolchain_probe.java_version,
            "com.salesforce.legacy-migration.maven": self.toolchain_probe.maven_version,
            "com.salesforce.legacy-migration.mule": self.toolchain_probe.mule_runtime_version,
            "com.salesforce.legacy-migration.mule-maven-plugin": (
                self.toolchain_probe.mule_maven_plugin_version
            ),
            "com.salesforce.legacy-migration.munit": self.toolchain_probe.munit_version,
            "com.salesforce.legacy-migration.network-installer": "none",
            "com.salesforce.legacy-migration.output-mode": "0644",
            "com.salesforce.legacy-migration.input-root": "/input",
            "com.salesforce.legacy-migration.work-root": "/work",
            "com.salesforce.legacy-migration.report-root": MULESOFT_CONTAINER_REPORT_ROOT,
            "com.salesforce.legacy-migration.toolchain-cache": "embedded-read-only",
            "com.salesforce.legacy-migration.argv": artifact_digest(MULESOFT_MUNIT_ARGV),
        }
        if any(labels.get(key) != value for key, value in required_labels.items()):
            raise ValueError("authority image labels do not implement the fixed runtime contract")
        if (
            self.toolchain_probe.java_version != JAVA_VERSION
            or self.toolchain_probe.mule_runtime_version != MULE4_RUNTIME
        ):
            raise ValueError("authority toolchain versions do not match the migration target")
        return self


@dataclass(frozen=True)
class _AuthorityManifestLoad:
    path: Path
    manifest: _DisabledContainerAuthorityManifest | _EnabledContainerAuthorityManifest | None
    digest: str
    identity_digest: str
    reason: str


class _ContainerCommandResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    argv: tuple[str, ...]
    exit_code: int = Field(ge=-255, le=255)
    stdout: str = Field(max_length=_CONTAINER_MAX_OUTPUT_BYTES)
    stderr: str = Field(max_length=_CONTAINER_MAX_OUTPUT_BYTES)
    timed_out: bool = False

    @field_validator("timed_out", mode="before")
    @classmethod
    def require_strict_boolean(cls, value: object) -> object:
        if not isinstance(value, bool):
            raise ValueError("container timed_out must be a boolean")
        return value


class _ContainerCommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> _ContainerCommandResult: ...


class _BoundedSubprocessContainerRunner:
    """Runtime-owned no-shell runner with bounded output and process-group kill."""

    def __init__(self, executable: Path) -> None:
        self._executable = executable

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> _ContainerCommandResult:
        if not argv or Path(argv[0]) != self._executable:
            raise PolicyViolation("container command does not use the pinned executable")
        if timeout_seconds <= 0 or timeout_seconds > _CONTAINER_VALIDATION_TIMEOUT_SECONDS:
            raise PolicyViolation("container command timeout is outside the fixed policy")
        process = subprocess.Popen(  # noqa: S603 - executable digest is verified by runtime
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
            env=_CONTAINER_HOST_ENVIRONMENT,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        lengths = {"stdout": 0, "stderr": 0}
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        output_exceeded = False
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    _kill_process_group(process)
                    break
                for key, _ in selector.select(min(remaining, 0.25)):
                    payload = os.read(key.fd, 8192)
                    stream = str(key.data)
                    if not payload:
                        selector.unregister(key.fileobj)
                        continue
                    lengths[stream] += len(payload)
                    if lengths[stream] > _CONTAINER_MAX_OUTPUT_BYTES:
                        output_exceeded = True
                        _kill_process_group(process)
                        break
                    chunks[stream].append(payload)
                if output_exceeded:
                    break
            if timed_out or output_exceeded:
                process.wait(timeout=5)
                exit_code = TIMEOUT_EXIT_CODE if timed_out else 125
            else:
                exit_code = process.wait(timeout=5)
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
        stdout = b"".join(chunks["stdout"]).decode("utf-8", errors="replace")
        stderr = b"".join(chunks["stderr"]).decode("utf-8", errors="replace")
        if output_exceeded:
            stderr = "container command exceeded the controller output bound"
        return _ContainerCommandResult(
            argv=argv,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )


class _ContainerAuthorityRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    authority_manifest_digest: Sha256Digest
    cli_sha256: Sha256Digest
    engine_version: str
    daemon_os: Literal["linux"]
    daemon_arch: Literal["amd64"]
    daemon_security_options: tuple[str, ...]
    daemon_identity_digest: Sha256Digest
    image_ref: str
    image_digest: Sha256Digest
    image_config_digest: Sha256Digest
    rootfs_diff_ids: tuple[Sha256Digest, ...]
    config_digest: Sha256Digest
    labels_digest: Sha256Digest
    execution_policy_digest: Sha256Digest


@dataclass(frozen=True)
class _RuntimeOwnedContainerBackend:
    executable: Path
    runner: _ContainerCommandRunner
    manifest: _EnabledContainerAuthorityManifest
    authority: _ContainerAuthorityRecord


@dataclass(frozen=True)
class _ContainerDiscovery:
    backend: _RuntimeOwnedContainerBackend | None
    reason: str


class _MuleRuntimeAuthorityAnchor(StrictModel):
    """Independent session binding for the runtime-owned container authority."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    request_digest: Sha256Digest
    source_revision: Sha256Digest
    authority_manifest_digest: Sha256Digest
    authority_manifest_identity_digest: Sha256Digest
    authority_manifest_path_digest: Sha256Digest
    authority_state: Literal["available", "unavailable"]
    authority_record_digest: Sha256Digest
    execution_contract_digest: Sha256Digest
    behavior_contract_state: Literal["available", "unavailable"]
    behavior_contract_digest: Sha256Digest
    behavior_suite_digest: Sha256Digest
    behavior_binding_digest: Sha256Digest


@dataclass(frozen=True)
class _ContainerValidationOutcome:
    exit_code: int
    timed_out: bool
    execution_digest: str
    started_at: datetime
    ended_at: datetime


class MuleSoftLocalValidator:
    """Session-bound static validator with fail-closed MUnit availability."""

    def __init__(
        self,
        session: AgentRunSession,
    ) -> None:
        self._session = session
        self._project_root = _safe_directory(session.project_root, "project root")
        self._source_root = _safe_descendant_directory(
            self._project_root, session.source_root, "session source root"
        )
        self._workspaces_root = _safe_descendant_directory(
            self._project_root, session.workspaces_dir, "session workspaces directory"
        )
        self._scratch_root = _safe_descendant_directory(
            self._project_root, session.scratch_dir, "session scratch directory"
        )
        self._context_digest = artifact_digest(session.context)
        self._agent_definition_digests = session.context.agent_definition_digests
        self._consumed_attempts: set[int] = set()
        self._attempt_lock = threading.Lock()
        self._validator_instance_id = secrets.token_hex(16)
        self._behavior_contract_load = _load_controller_behavior_contract()
        self._behavior_binding_digest = (
            artifact_digest(self._behavior_contract_load.binding)
            if self._behavior_contract_load.binding is not None
            else artifact_digest(
                {
                    "contract_digest": self._behavior_contract_load.contract_digest,
                    "suite_digest": self._behavior_contract_load.suite_digest,
                    "state": "unavailable",
                }
            )
        )
        manifest_load = _load_runtime_authority_manifest(_AUTHORITY_MANIFEST_PATH)
        self._authority_manifest_path = manifest_load.path
        self._authority_manifest_digest = manifest_load.digest
        self._authority_manifest_identity_digest = manifest_load.identity_digest
        discovery = _discover_runtime_owned_container(manifest_load)
        self._container_backend = discovery.backend
        self._container_unavailable_reason = discovery.reason
        authority_record_digest = (
            artifact_digest(discovery.backend.authority)
            if discovery.backend is not None
            else artifact_digest(
                {
                    "authority_manifest_digest": manifest_load.digest,
                    "state": "unavailable",
                }
            )
        )
        self._runtime_authority_anchor = _MuleRuntimeAuthorityAnchor(
            run_id=session.context.run_id,
            request_digest=session.context.request_digest,
            source_revision=session.context.source_revision,
            authority_manifest_digest=manifest_load.digest,
            authority_manifest_identity_digest=manifest_load.identity_digest,
            authority_manifest_path_digest=artifact_digest(str(manifest_load.path)),
            authority_state=("available" if discovery.backend is not None else "unavailable"),
            authority_record_digest=authority_record_digest,
            execution_contract_digest=_container_execution_contract_digest(),
            behavior_contract_state=(
                "available" if self._behavior_contract_load.binding is not None else "unavailable"
            ),
            behavior_contract_digest=self._behavior_contract_load.contract_digest,
            behavior_suite_digest=self._behavior_contract_load.suite_digest,
            behavior_binding_digest=self._behavior_binding_digest,
        )
        session.bind_runtime_anchor(
            MULESOFT_RUNTIME_AUTHORITY_ANCHOR_KIND,
            self._runtime_authority_anchor,
        )
        self._verify_session_state()

    def __call__(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        workspace: IsolatedWorkspace,
        attempt: int,
    ) -> ValidationReport:
        """Run all exact checks; never claim deployment or Anypoint validation."""

        self._preflight(request, manifest, change_set, workspace, attempt)
        self._consume_attempt(request, change_set, attempt)

        evidence_fingerprint = tree_fingerprint(self._session.evidence_dir)
        initial_source_fingerprint = tree_fingerprint(self._source_root)
        initial_candidate_fingerprint = tree_fingerprint(workspace.root)
        initial_candidate_revision = snapshot_tree(workspace.root).revision
        initial_changes = workspace.audit_changes()
        _require_changes_match(initial_changes, change_set)
        workspace.assert_source_unchanged()

        planned = {check.command_id: check for check in manifest.validation_plan}
        change_digest = artifact_digest(change_set)
        results: dict[str, CheckResult] = {}
        try:
            results[MULESOFT_CANDIDATE_CONTRACT_COMMAND_ID] = self._candidate_contract_check(
                planned[MULESOFT_CANDIDATE_CONTRACT_COMMAND_ID],
                workspace.root,
                change_digest,
                request,
                manifest,
                attempt,
            )
            results[MULESOFT_DEPENDENCY_CLOSURE_COMMAND_ID] = self._dependency_closure_check(
                planned[MULESOFT_DEPENDENCY_CLOSURE_COMMAND_ID],
                workspace.root,
                initial_candidate_revision,
                change_digest,
                request,
                manifest,
                attempt,
            )
            results[MULESOFT_TOOLCHAIN_CONTRACT_COMMAND_ID] = self._toolchain_check(
                planned[MULESOFT_TOOLCHAIN_CONTRACT_COMMAND_ID],
                change_digest,
                request,
                manifest,
                attempt,
            )
            prerequisites_passed = all(
                results[command_id].status is CheckStatus.PASSED
                for command_id in (
                    MULESOFT_CANDIDATE_CONTRACT_COMMAND_ID,
                    MULESOFT_DEPENDENCY_CLOSURE_COMMAND_ID,
                    MULESOFT_TOOLCHAIN_CONTRACT_COMMAND_ID,
                )
            )
            results[MULESOFT_MUNIT_COMMAND_ID] = self._munit_check(
                planned[MULESOFT_MUNIT_COMMAND_ID],
                workspace,
                prerequisites_passed,
                initial_source_fingerprint,
                initial_candidate_fingerprint,
                change_digest,
                request,
                manifest,
                attempt,
            )
        finally:
            # Integrity is a controller invariant, not merely a success-path
            # check. Future runtime-owned execution cannot bypass it by raising.
            self._verify_postconditions(
                workspace,
                change_set,
                initial_source_fingerprint,
                initial_candidate_fingerprint,
                evidence_fingerprint,
            )
        results[MULESOFT_WORKSPACE_FINGERPRINT_COMMAND_ID] = _passed_result(
            planned[MULESOFT_WORKSPACE_FINGERPRINT_COMMAND_ID],
            _controller_receipt(
                planned[MULESOFT_WORKSPACE_FINGERPRINT_COMMAND_ID],
                self._session,
                request,
                manifest,
                change_digest,
                attempt,
                exit_code=0,
                operation="controller-read-only-fingerprint",
            ),
            "candidate, source, session, and runtime authority remained unchanged",
        )

        ordered = tuple(results[command_id] for command_id in MULESOFT_VALIDATION_COMMAND_IDS)
        report = ValidationReport(
            report_id=_report_id(self._session.context.run_id, request.request_id, attempt),
            request_id=request.request_id,
            manifest_id=manifest.manifest_id,
            change_set_id=change_set.change_set_id,
            base_revision=manifest.base_revision,
            results=ordered,
            disposition=_disposition(ordered),
            attempt=attempt,
        )
        validate_report(report, manifest, change_set)
        self._session.validate_portable_evidence(report)
        return report

    def _preflight(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        workspace: IsolatedWorkspace,
        attempt: int,
    ) -> None:
        if attempt not in {1, 2}:
            raise PolicyViolation("MuleSoft local validation supports attempts 1 and 2 only")
        self._verify_session_state()
        if artifact_digest(request) != self._session.context.request_digest:
            raise PolicyViolation("migration request does not match the bound run session")
        if request.platform is not Platform.MULESOFT:
            raise PolicyViolation("MuleSoft runtime does not match the migration request")
        if request.repository != self._session.context.source_root:
            raise PolicyViolation("MuleSoft request repository differs from the bound source root")
        if request.base_revision != self._session.context.source_revision:
            raise PolicyViolation("migration request revision does not match the run session")
        if request.allowed_environment is not EnvironmentKind.LOCAL:
            raise PolicyViolation("MuleSoft preset permits only local validation")
        expected_target = (
            MULE3_APP,
            MULESOFT_TARGET_RUNTIME,
            MULESOFT_SOURCE_VERSION,
            MULESOFT_TARGET_VERSION,
        )
        observed_target = (
            request.target.entry_path,
            request.target.target_runtime,
            request.target.source_version,
            request.target.target_version,
        )
        if observed_target != expected_target:
            raise PolicyViolation("MuleSoft request target or version contract drifted")

        validate_manifest_for_request(manifest, request)
        MULESOFT_PLATFORM_ADAPTER.validate_manifest(manifest, request)
        validate_change_set(change_set, manifest)
        if manifest.approved_paths != TARGET_FILES:
            raise PolicyViolation("MuleSoft manifest must declare the exact six target outputs")
        if manifest.required_approvals != (ApprovalAction.APPROVE_MANIFEST,):
            raise PolicyViolation("MuleSoft manifest approval contract drifted")
        validate_manifest_transformation_scope(
            manifest,
            required_source_input_paths=SOURCE_FILES,
            approved_output_paths=TARGET_FILES,
        )
        commands = tuple(check.command_id for check in manifest.validation_plan)
        if commands != MULESOFT_VALIDATION_COMMAND_IDS:
            raise PolicyViolation("MuleSoft validation plan has command drift or reordering")
        if any(not check.required for check in manifest.validation_plan):
            raise PolicyViolation("every supported MuleSoft local check must be required")
        if any(
            check.environment is not EnvironmentKind.LOCAL for check in manifest.validation_plan
        ):
            raise PolicyViolation("MuleSoft validation commands must use the local environment")

        if workspace.closed:
            raise PolicyViolation("MuleSoft candidate workspace is already closed")
        if workspace.source_root != self._source_root:
            raise PolicyViolation("MuleSoft workspace belongs to a different source tree")
        if workspace.base_revision != request.base_revision:
            raise PolicyViolation("MuleSoft workspace is stale for the migration request")
        if set(workspace.approved_paths) != set(TARGET_FILES):
            raise PolicyViolation("MuleSoft workspace does not have the exact target scope")
        candidate = _safe_descendant_directory(
            self._workspaces_root, workspace.root, "candidate workspace"
        )
        try:
            relative = candidate.relative_to(self._workspaces_root)
        except ValueError as exc:  # pragma: no cover - helper already enforces this
            raise PolicyViolation("candidate workspace belongs to another run") from exc
        if len(relative.parts) != 2 or relative.parts[-1] != "repository":
            raise PolicyViolation("candidate workspace is not owned by this run session")

        changes = workspace.audit_changes()
        _require_changes_match(changes, change_set)
        if changes.added_paths != TARGET_FILES or changes.modified_paths or changes.deleted_paths:
            raise PolicyViolation("MuleSoft change set must add exactly six target files")

        _reject_oracle_path(self._project_root, "project root")
        _reject_oracle_path(self._source_root, "source root")
        _reject_oracle_path(candidate, "candidate workspace")

    def _consume_attempt(
        self,
        request: MigrationRequest,
        change_set: ChangeSet,
        attempt: int,
    ) -> None:
        with self._attempt_lock:
            if attempt in self._consumed_attempts:
                raise PolicyViolation("MuleSoft validation attempt has already been consumed")
            self._session.bind_runtime_anchor(
                f"mulesoft-validation-attempt-{attempt}",
                {
                    "run_id": self._session.context.run_id,
                    "request_id": request.request_id,
                    "base_revision": request.base_revision,
                    "change_set_digest": artifact_digest(change_set),
                    "validator_instance_id": self._validator_instance_id,
                },
            )
            self._consumed_attempts.add(attempt)

    def _candidate_contract_check(
        self,
        check: ValidationCommand,
        candidate_root: Path,
        change_digest: str,
        request: MigrationRequest,
        manifest: MigrationManifest,
        attempt: int,
    ) -> CheckResult:
        try:
            summary = check_mulesoft_candidate(candidate_root, self._source_root)
        except MuleSoftLocalCheckFailure as exc:
            receipt = _controller_receipt(
                check,
                self._session,
                request,
                manifest,
                change_digest,
                attempt,
                exit_code=1,
                operation="controller-static-candidate-contract",
            )
            return _failed_result(
                check,
                receipt,
                f"candidate static contract failed: {exc.code.value}:{exc.artifact}",
            )
        receipt = _controller_receipt(
            check,
            self._session,
            request,
            manifest,
            change_digest,
            attempt,
            exit_code=0,
            operation="controller-static-candidate-contract",
        )
        return _passed_result(
            check,
            receipt,
            (
                f"exact candidate contract passed; files={summary.inventory_files} "
                f"preserved-source={summary.preserved_source_files} java={summary.java}"
            ),
        )

    def _dependency_closure_check(
        self,
        check: ValidationCommand,
        candidate_root: Path,
        candidate_revision: str,
        change_digest: str,
        request: MigrationRequest,
        manifest: MigrationManifest,
        attempt: int,
    ) -> CheckResult:
        receipt = _controller_receipt(
            check,
            self._session,
            request,
            manifest,
            change_digest,
            attempt,
            exit_code=0,
            operation="controller-static-dependency-closure",
        )
        try:
            graph = build_mulesoft_dependency_graph(
                candidate_root,
                (MULE4_APP,),
                candidate_revision,
            )
            required = (
                graph.node(NodeKind.MULE_FLOW, "customer-status-api-flow"),
                graph.node(NodeKind.MULE_SUBFLOW, "build-customer-status-response"),
                graph.node(NodeKind.MULE_CONFIGURATION, "customer-status-http-listener"),
                graph.node(NodeKind.DATAWEAVE_MODULE, "dw/customer-status-response.dwl"),
                graph.node(NodeKind.MUNIT_TEST, "build-customer-status-response-test"),
            )
            if graph.has_unresolved or any(node is None or not node.resolved for node in required):
                raise ValueError("required target dependency closure is unresolved")
        except (OSError, ValueError, PolicyViolation):
            failed_receipt = receipt.model_copy(update={"exit_code": 1})
            return _failed_result(
                check,
                failed_receipt,
                "target Mule dependency closure is incomplete or unresolved",
            )
        return _passed_result(
            check,
            receipt,
            f"resolved Mule target dependency closure; nodes={len(graph.nodes)} edges={len(graph.edges)}",
        )

    def _toolchain_check(
        self,
        check: ValidationCommand,
        change_digest: str,
        request: MigrationRequest,
        manifest: MigrationManifest,
        attempt: int,
    ) -> CheckResult:
        self._verify_runtime_authority_anchor()
        if self._container_backend is None:
            return _unavailable_result(
                check,
                (
                    "runtime-owned digest-pinned Mule container, Java 17, Maven, "
                    "Mule dependencies, or license are unavailable: "
                    f"{self._container_unavailable_reason}"
                ),
            )
        current = _inspect_runtime_owned_container(
            self._container_backend.executable,
            self._container_backend.runner,
            self._container_backend.manifest,
            self._authority_manifest_digest,
        )
        if current != self._container_backend.authority:
            raise PolicyViolation("Mule container authority drifted after session binding")
        receipt = _controller_receipt(
            check,
            self._session,
            request,
            manifest,
            change_digest,
            attempt,
            exit_code=0,
            operation=(f"controller-container-authority;authority:{artifact_digest(current)}"),
        )
        return _passed_result(
            check,
            receipt,
            (
                "runtime-owned container authority passed; "
                f"engine={current.engine_version} image={current.image_digest} "
                f"java={JAVA_VERSION} mule={MULE4_RUNTIME}"
            ),
        )

    def _munit_check(
        self,
        check: ValidationCommand,
        workspace: IsolatedWorkspace,
        prerequisites_passed: bool,
        source_fingerprint: str,
        candidate_fingerprint: str,
        change_digest: str,
        request: MigrationRequest,
        manifest: MigrationManifest,
        attempt: int,
    ) -> CheckResult:
        self._verify_runtime_authority_anchor()
        behavior_contract = self._behavior_contract_load.contract
        behavior_binding = self._behavior_contract_load.binding
        behavior_suite = self._behavior_contract_load.suite_payload
        if behavior_contract is None or behavior_binding is None or behavior_suite is None:
            return _unavailable_result(
                check,
                (
                    "candidate Maven/MUnit was not executed because the controller-owned "
                    f"behavior contract is unavailable: {self._behavior_contract_load.reason}"
                ),
            )
        if not prerequisites_passed or self._container_backend is None:
            return _unavailable_result(
                check,
                (
                    "candidate Maven/MUnit was not executed because no verified "
                    "runtime-owned isolation authority is available"
                ),
            )
        backend = self._container_backend
        attempt_root = Path(
            tempfile.mkdtemp(prefix=f"mulesoft-container-{attempt}-", dir=self._scratch_root)
        )
        os.chmod(attempt_root, 0o700)
        probe_output = attempt_root / "probe-output"
        probe_scratch = attempt_root / "probe-scratch"
        validation_output = attempt_root / "validation-output"
        validation_scratch = attempt_root / "validation-scratch"
        for path in (probe_output, probe_scratch, validation_output, validation_scratch):
            path.mkdir(mode=0o733)
        canary = attempt_root / "controller-canary"
        canary.write_bytes(b"controller-owned-no-outside-effects\n")
        canary_digest = _file_digest(canary)
        validation_workspace = IsolatedWorkspace(
            workspace.root,
            TARGET_FILES,
            temp_parent=attempt_root,
            expected_revision=snapshot_tree(workspace.root).revision,
        )
        try:
            _install_controller_behavior_suite(
                validation_workspace.root,
                behavior_contract,
                behavior_suite,
            )
            _make_tree_container_readable(validation_workspace.root)
            validation_fingerprint = tree_fingerprint(validation_workspace.root)
            probe_name = _container_name(self._session.context.run_id, attempt, "probe")
            probe_argv = _container_run_argv(
                backend.executable,
                backend.manifest,
                probe_name,
                output_root=probe_output,
                scratch_root=probe_scratch,
                candidate_root=None,
                mode="probe",
            )
            probe_outcome = _run_container_lifecycle(
                backend,
                probe_argv,
                timeout_seconds=_CONTAINER_COMMAND_TIMEOUT_SECONDS,
            )
            if probe_outcome.timed_out or probe_outcome.exit_code != 0:
                return _unavailable_result(
                    check,
                    "runtime-owned container isolation probe did not complete successfully",
                )
            _verify_probe_output(probe_output, backend.manifest)
            if _file_digest(canary) != canary_digest:
                raise PolicyViolation("Mule isolation probe changed its outside-effect canary")

            validation_name = _container_name(self._session.context.run_id, attempt, "validate")
            validation_argv = _container_run_argv(
                backend.executable,
                backend.manifest,
                validation_name,
                output_root=validation_output,
                scratch_root=validation_scratch,
                candidate_root=validation_workspace.root,
                mode="validate",
            )
            outcome = _run_container_lifecycle(
                backend,
                validation_argv,
                timeout_seconds=_CONTAINER_VALIDATION_TIMEOUT_SECONDS,
            )
            if outcome.timed_out:
                receipt = _container_receipt(
                    check,
                    self._session,
                    request,
                    manifest,
                    change_digest,
                    attempt,
                    outcome,
                    backend.authority,
                    probe_outcome.execution_digest,
                    artifacts=(),
                    report_set_digest=None,
                    evidence_digest=None,
                    behavior_contract_digest=self._behavior_binding_digest,
                    behavior_report_digest=None,
                )
                return _unavailable_result(
                    check,
                    "runtime-owned Mule container timed out without terminal reports",
                    receipt=receipt,
                )
            if tree_fingerprint(validation_workspace.root) != validation_fingerprint:
                raise PolicyViolation("Mule container mutated its read-only candidate mount")
            validation_workspace.assert_source_unchanged()
            if _file_digest(canary) != canary_digest:
                raise PolicyViolation("Mule validation changed its outside-effect canary")
            reports, artifacts, report_set_digest = _read_surefire_reports(
                validation_output / "surefire-reports"
            )
            evidence = parse_munit_surefire_xml(
                reports,
                command_exit_code=outcome.exit_code,
                context=MuleSoftValidationContext(
                    request_id=request.request_id,
                    run_id=self._session.context.run_id,
                    base_revision=manifest.base_revision,
                    artifact_digest=report_set_digest,
                ),
            )
            behavior_reports = _validate_controller_behavior_reports(
                reports,
                artifacts,
                behavior_contract,
                self._behavior_binding_digest,
            )
            combined_evidence_digest = artifact_digest(
                {
                    "aggregate": evidence.model_dump(mode="json"),
                    "behavior_reports": behavior_reports.model_dump(mode="json"),
                }
            )
            receipt = _container_receipt(
                check,
                self._session,
                request,
                manifest,
                change_digest,
                attempt,
                outcome,
                backend.authority,
                probe_outcome.execution_digest,
                artifacts=artifacts,
                report_set_digest=report_set_digest,
                evidence_digest=combined_evidence_digest,
                behavior_contract_digest=self._behavior_binding_digest,
                behavior_report_digest=artifact_digest(behavior_reports),
            )
            complete = (
                evidence.report_count == 2
                and evidence.suites == 2
                and evidence.tests == 2
                and evidence.skipped == 0
            )
            if not complete:
                return _unavailable_result(
                    check,
                    "runtime-owned MUnit reports were incomplete or contained skipped tests",
                    receipt=receipt,
                )
            if evidence.status is MuleSoftValidationStatus.PASSED:
                return _passed_result(
                    check,
                    receipt,
                    (
                        "runtime-owned controller behavior and supplemental candidate MUnit "
                        "passed; suites=2 tests=2 deployment=false"
                    ),
                )
            if outcome.exit_code == 0:
                return _unavailable_result(
                    check,
                    "MUnit report failure disagreed with the container exit code",
                    receipt=receipt,
                )
            return _failed_result(
                check,
                receipt,
                "runtime-owned isolated MUnit completed with a candidate test failure",
            )
        except (MuleSoftEvidenceError, OSError, UnicodeError, ValueError) as exc:
            return _unavailable_result(
                check,
                f"runtime-owned container evidence was unavailable: {type(exc).__name__}",
            )
        finally:
            _make_tree_controller_writable(validation_workspace.root)
            validation_workspace.cleanup()
            _remove_attempt_root(attempt_root, self._scratch_root)

    def _verify_postconditions(
        self,
        workspace: IsolatedWorkspace,
        change_set: ChangeSet,
        source_fingerprint: str,
        candidate_fingerprint: str,
        evidence_fingerprint: str,
    ) -> None:
        self._verify_session_state()
        workspace.assert_source_unchanged()
        if tree_fingerprint(self._source_root) != source_fingerprint:
            raise PolicyViolation("MuleSoft validation mutated the immutable source tree")
        if tree_fingerprint(workspace.root) != candidate_fingerprint:
            raise PolicyViolation("MuleSoft validation mutated the candidate workspace")
        if tree_fingerprint(self._session.evidence_dir) != evidence_fingerprint:
            raise PolicyViolation("MuleSoft validation mutated lifecycle evidence")
        _require_changes_match(workspace.audit_changes(), change_set)
        if self._container_backend is not None:
            current = _inspect_runtime_owned_container(
                self._container_backend.executable,
                self._container_backend.runner,
                self._container_backend.manifest,
                self._authority_manifest_digest,
            )
            if current != self._container_backend.authority:
                raise PolicyViolation("Mule container authority changed after validation")

    def _verify_runtime_authority_anchor(self) -> None:
        self._session.verify_runtime_anchor(
            MULESOFT_RUNTIME_AUTHORITY_ANCHOR_KIND,
            self._runtime_authority_anchor,
        )
        current_manifest = _load_runtime_authority_manifest(self._authority_manifest_path)
        if (
            current_manifest.digest != self._authority_manifest_digest
            or current_manifest.identity_digest != self._authority_manifest_identity_digest
        ):
            raise PolicyViolation("Mule runtime authority manifest changed after session binding")
        current_behavior = _load_controller_behavior_contract()
        if (
            current_behavior.contract_digest != self._behavior_contract_load.contract_digest
            or current_behavior.suite_digest != self._behavior_contract_load.suite_digest
            or current_behavior.binding != self._behavior_contract_load.binding
            or current_behavior.reason != self._behavior_contract_load.reason
        ):
            raise PolicyViolation("controller Mule behavior contract changed after session binding")

    def _verify_session_state(self) -> None:
        self._verify_runtime_authority_anchor()
        self._session.verify_index("initialized", exact=False)
        self._session.verify_source_revision()
        if artifact_digest(self._session.context) != self._context_digest:
            raise PolicyViolation("MuleSoft validator session context drifted")
        if self._session.context.agent_definition_digests != self._agent_definition_digests:
            raise PolicyViolation("MuleSoft validator agent-definition digests drifted")
        loaded = AgentRunSession.load(self._project_root, self._session.run_dir)
        if loaded.context != self._session.context or loaded.run_dir != self._session.run_dir:
            raise PolicyViolation("MuleSoft validator is bound to a foreign run session")
        evidence = snapshot_tree(self._session.evidence_dir)
        index_names = tuple(
            Path(entry.path).stem
            for entry in evidence.entries
            if entry.path.startswith("indexes/") and entry.path.endswith(".json")
        )
        if "initialized" not in index_names:
            raise PolicyViolation("MuleSoft run has no initialized lifecycle index")
        for kind in index_names:
            self._session.verify_index(kind, exact=False)


def _new_container_runner(executable: Path) -> _ContainerCommandRunner:
    return _BoundedSubprocessContainerRunner(executable)


def _manifest_unavailable(path: Path, state: str) -> _AuthorityManifestLoad:
    return _AuthorityManifestLoad(
        path=path,
        manifest=None,
        digest=artifact_digest({"authority_manifest": state}),
        identity_digest=artifact_digest({"authority_manifest_identity": state}),
        reason=f"authority-manifest-{state}",
    )


def _trusted_parent_records(path: Path, role: str) -> tuple[tuple[str, int, int, int, int], ...]:
    records: list[tuple[str, int, int, int, int]] = []
    current = path.parent
    while True:
        metadata = current.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mode & 0o022
            or metadata.st_uid not in {0, os.geteuid()}
        ):
            raise PolicyViolation(f"{role} parent is unsafe")
        records.append(
            (
                str(current),
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_uid,
                stat.S_IMODE(metadata.st_mode),
            )
        )
        if current.parent == current:
            break
        current = current.parent
    return tuple(records)


def _manifest_file_identity(path: Path, metadata: os.stat_result) -> str:
    if metadata.st_nlink != 1:
        raise PolicyViolation("Mule runtime authority manifest must have one link")
    parent_records = _trusted_parent_records(path, "Mule runtime authority manifest")
    return artifact_digest(
        {
            "path": str(path),
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "owner": metadata.st_uid,
            "group": metadata.st_gid,
            "mode": stat.S_IMODE(metadata.st_mode),
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
            "parents": parent_records,
        }
    )


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("authority manifest contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"authority manifest contains unsupported JSON constant: {value}")


def _load_runtime_authority_manifest(path: Path) -> _AuthorityManifestLoad:
    """Read one controller-owned manifest without treating invalid data as authority."""

    if not path.is_absolute():
        return _manifest_unavailable(path, "path-invalid")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _manifest_unavailable(path, "missing")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
        or metadata.st_uid not in {0, os.geteuid()}
    ):
        return _manifest_unavailable(path, "file-unsafe")
    try:
        identity_digest = _manifest_file_identity(path, metadata)
        payload = _read_regular_file(
            path,
            max_bytes=_AUTHORITY_MANIFEST_MAX_BYTES,
            role="Mule runtime authority manifest",
        )
        after = path.lstat()
        if _manifest_file_identity(path, after) != identity_digest:
            raise PolicyViolation("Mule runtime authority manifest changed while loading")
    except (MuleSoftEvidenceError, OSError, PolicyViolation):
        return _manifest_unavailable(path, "unreadable")
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    try:
        raw = _mapping(
            json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            ),
            "Mule runtime authority manifest",
        )
        if raw.get("enabled") is True:
            manifest: _DisabledContainerAuthorityManifest | _EnabledContainerAuthorityManifest = (
                _EnabledContainerAuthorityManifest.model_validate(raw)
            )
            reason = "authority-manifest-enabled"
        elif raw.get("enabled") is False:
            manifest = _DisabledContainerAuthorityManifest.model_validate(raw)
            reason = "authority-manifest-disabled"
        else:
            raise ValueError("authority manifest enabled flag must be a boolean")
    except (UnicodeError, ValueError, json.JSONDecodeError, PolicyViolation):
        return _AuthorityManifestLoad(
            path,
            None,
            digest,
            identity_digest,
            "authority-manifest-invalid",
        )
    return _AuthorityManifestLoad(path, manifest, digest, identity_digest, reason)


def _discover_runtime_owned_container(
    manifest_load: _AuthorityManifestLoad,
) -> _ContainerDiscovery:
    manifest = manifest_load.manifest
    if not isinstance(manifest, _EnabledContainerAuthorityManifest):
        return _ContainerDiscovery(None, manifest_load.reason)
    if _RELEASED_AUTHORITY_MANIFEST_SHA256 is None:
        return _ContainerDiscovery(None, "authority-manifest-release-pin-missing")
    if manifest_load.digest != _RELEASED_AUTHORITY_MANIFEST_SHA256:
        return _ContainerDiscovery(None, "authority-manifest-release-pin-mismatch")
    try:
        executable = _safe_controller_executable(Path(manifest.cli_path))
        if _file_digest(executable) != manifest.cli_sha256:
            raise PolicyViolation("container CLI digest does not match the runtime pin")
        runner = _new_container_runner(executable)
        authority = _inspect_runtime_owned_container(
            executable,
            runner,
            manifest,
            manifest_load.digest,
        )
    except (
        OSError,
        PolicyViolation,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        return _ContainerDiscovery(None, type(exc).__name__)
    return _ContainerDiscovery(
        _RuntimeOwnedContainerBackend(
            executable=executable,
            runner=runner,
            manifest=manifest,
            authority=authority,
        ),
        "verified",
    )


def _inspect_runtime_owned_container(
    executable: Path,
    runner: _ContainerCommandRunner,
    manifest: _EnabledContainerAuthorityManifest,
    manifest_digest: str,
) -> _ContainerAuthorityRecord:
    current_executable = _safe_controller_executable(Path(manifest.cli_path))
    if executable != current_executable:
        raise PolicyViolation("container CLI path changed after discovery")
    cli_digest = _file_digest(executable)
    if cli_digest != manifest.cli_sha256:
        raise PolicyViolation("container CLI digest changed after discovery")

    daemon_argv = (str(executable), *_CONTAINER_DAEMON_ARGV_SUFFIX)
    daemon_result = runner.run(
        daemon_argv,
        timeout_seconds=_CONTAINER_COMMAND_TIMEOUT_SECONDS,
    )
    _require_command_result(daemon_result, daemon_argv, "container daemon inspection")
    daemon = _json_mapping(daemon_result.stdout)
    if (
        daemon.get("Version") != manifest.engine_version
        or daemon.get("ApiVersion") != manifest.engine_api_version
        or daemon.get("Os") != manifest.engine_os
        or daemon.get("Arch") != manifest.engine_architecture
        or daemon.get("Experimental") is not False
    ):
        raise PolicyViolation("container daemon identity is unsupported")
    security_options = _string_tuple(daemon.get("SecurityOptions"), "daemon security")
    if "name=seccomp,profile=default" not in security_options:
        raise PolicyViolation("container daemon does not expose the required seccomp profile")

    inspect_argv = (str(executable), "image", "inspect", manifest.image_ref)
    inspect_result = runner.run(
        inspect_argv,
        timeout_seconds=_CONTAINER_COMMAND_TIMEOUT_SECONDS,
    )
    _require_command_result(inspect_result, inspect_argv, "container image inspection")
    raw_inspect = json.loads(inspect_result.stdout)
    if not isinstance(raw_inspect, list) or len(raw_inspect) != 1:
        raise PolicyViolation("container image inspection must contain exactly one image")
    image = _mapping(raw_inspect[0], "container image inspection")
    if (
        image.get("Id") != manifest.image_config_digest
        or image.get("Os") != manifest.image_os
        or image.get("Architecture") != manifest.image_architecture
    ):
        raise PolicyViolation("container image config or platform does not match the runtime pin")
    repo_digests = _string_tuple(image.get("RepoDigests"), "image RepoDigests")
    if repo_digests != (manifest.image_ref,):
        raise PolicyViolation("container image RepoDigest is missing or mutable")
    repo_tags = image.get("RepoTags")
    if repo_tags not in (None, []):
        raise PolicyViolation("container validation image must not expose mutable tags")

    rootfs = _mapping(image.get("RootFS"), "container RootFS")
    if rootfs.get("Type") != "layers":
        raise PolicyViolation("container image RootFS is not layer-backed")
    diff_ids = _string_tuple(rootfs.get("Layers"), "container RootFS DiffIDs")
    if diff_ids != manifest.rootfs_diff_ids:
        raise PolicyViolation("container image RootFS does not match the pinned DiffIDs")

    config = _mapping(image.get("Config"), "container image Config")
    if config.get("User") != manifest.user:
        raise PolicyViolation("container image does not use the fixed nonroot user")
    if _string_tuple(config.get("Entrypoint"), "image entrypoint") != manifest.entrypoint:
        raise PolicyViolation("container image entrypoint drifted")
    if _string_tuple(config.get("Cmd"), "image command") != manifest.default_command:
        raise PolicyViolation("container image command drifted")
    if _string_tuple(config.get("Env"), "image environment") != manifest.image_environment:
        raise PolicyViolation("container image environment drifted")
    if config.get("WorkingDir") != manifest.working_directory:
        raise PolicyViolation("container image working directory drifted")
    if config.get("ExposedPorts") not in (None, {}):
        raise PolicyViolation("container validation image cannot expose ports")
    if config.get("Volumes") not in (None, {}):
        raise PolicyViolation("container validation image cannot declare anonymous volumes")
    labels = _mapping(config.get("Labels"), "container image labels")
    expected_labels = dict(manifest.labels)
    if labels != expected_labels:
        raise PolicyViolation("container image labels do not match the pinned toolchain")

    config_material = {
        "user": config.get("User"),
        "entrypoint": manifest.entrypoint,
        "command": manifest.default_command,
        "environment": manifest.image_environment,
        "working_directory": manifest.working_directory,
        "labels": manifest.labels,
        "rootfs_diff_ids": manifest.rootfs_diff_ids,
        "image_os": manifest.image_os,
        "image_architecture": manifest.image_architecture,
    }
    return _ContainerAuthorityRecord(
        authority_manifest_digest=manifest_digest,
        cli_sha256=cli_digest,
        engine_version=manifest.engine_version,
        daemon_os=manifest.engine_os,
        daemon_arch=manifest.engine_architecture,
        daemon_security_options=tuple(sorted(security_options)),
        daemon_identity_digest=artifact_digest(daemon),
        image_ref=manifest.image_ref,
        image_digest=manifest.image_digest,
        image_config_digest=manifest.image_config_digest,
        rootfs_diff_ids=manifest.rootfs_diff_ids,
        config_digest=artifact_digest(config_material),
        labels_digest=artifact_digest(manifest.labels),
        execution_policy_digest=_container_execution_contract_digest(),
    )


def _container_name(run_id: str, attempt: int, mode: Literal["probe", "validate"]) -> str:
    suffix = hashlib.sha256(f"{run_id}\x00{attempt}\x00{mode}".encode()).hexdigest()[:20]
    name = f"lma-mule-{attempt}-{mode}-{suffix}"
    if _CONTAINER_NAME.fullmatch(name) is None:
        raise PolicyViolation("controller generated an invalid container name")
    return name


def _container_run_argv(
    executable: Path,
    manifest: _EnabledContainerAuthorityManifest,
    name: str,
    *,
    output_root: Path,
    scratch_root: Path,
    candidate_root: Path | None,
    mode: Literal["probe", "validate"],
) -> tuple[str, ...]:
    if _CONTAINER_NAME.fullmatch(name) is None:
        raise PolicyViolation("container name is outside the controller contract")
    output = _mount_spec(output_root, "/output", read_only=False)
    scratch = _mount_spec(scratch_root, "/scratch", read_only=False)
    mounts: tuple[str, ...] = ("--mount", output, "--mount", scratch)
    if mode == "validate":
        if candidate_root is None:
            raise PolicyViolation("validation container requires the candidate input mount")
        candidate = _mount_spec(candidate_root, "/input", read_only=True)
        mounts = (*mounts, "--mount", candidate)
    elif candidate_root is not None:
        raise PolicyViolation("isolation probe cannot receive candidate content")
    argv = (
        str(executable),
        "create",
        "--pull",
        "never",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "128",
        "--pid",
        "private",
        "--ipc",
        "private",
        "--cgroupns",
        "private",
        "--memory",
        "1024m",
        "--cpus",
        "1.0",
        "--user",
        manifest.user,
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs",
        "/work:rw,nosuid,nodev,size=768m",
        "--log-driver",
        "none",
        *mounts,
        manifest.image_ref,
        mode,
    )
    _require_container_run_contract(argv, manifest, mode=mode)
    return argv


def _require_container_run_contract(
    argv: tuple[str, ...],
    manifest: _EnabledContainerAuthorityManifest,
    *,
    mode: Literal["probe", "validate"],
) -> None:
    required_pairs = {
        "--pull": "never",
        "--network": "none",
        "--cap-drop": "ALL",
        "--security-opt": "no-new-privileges:true",
        "--pids-limit": "128",
        "--pid": "private",
        "--ipc": "private",
        "--cgroupns": "private",
        "--memory": "1024m",
        "--cpus": "1.0",
        "--user": manifest.user,
        "--log-driver": "none",
    }
    if len(argv) < 10 or argv[1] != "create":
        raise PolicyViolation("container execution is not the fixed create command")
    if argv[-2:] != (manifest.image_ref, mode):
        raise PolicyViolation("container execution does not use the immutable image and mode")
    if argv.count("--read-only") != 1 or argv.count("--name") != 1:
        raise PolicyViolation("container rootfs/name restrictions drifted")
    for flag, expected in required_pairs.items():
        if argv.count(flag) != 1:
            raise PolicyViolation(f"container restriction is missing or duplicated: {flag}")
        index = argv.index(flag)
        if index + 1 >= len(argv) or argv[index + 1] != expected:
            raise PolicyViolation(f"container restriction drifted: {flag}")
    tmpfs_values = tuple(
        argv[index + 1] for index, token in enumerate(argv[:-1]) if token == "--tmpfs"
    )
    if tmpfs_values != (
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "/work:rw,nosuid,nodev,size=768m",
    ):
        raise PolicyViolation("container tmpfs work/temporary limits drifted")
    forbidden = {
        "--privileged",
        "--network=host",
        "--cap-add",
        "--device",
        "--env",
        "--env-file",
        "--entrypoint",
        "-e",
        "-v",
        "--volume",
    }
    if any(token in forbidden for token in argv):
        raise PolicyViolation("container execution contains a forbidden authority relaxation")
    mount_values = tuple(
        argv[index + 1] for index, token in enumerate(argv[:-1]) if token == "--mount"
    )
    expected_mounts = 3 if mode == "validate" else 2
    if len(mount_values) != expected_mounts:
        raise PolicyViolation("container execution has unexpected mount cardinality")
    destinations: set[str] = set()
    for mount in mount_values:
        parts = _mount_mapping(mount)
        if parts.get("type") != "bind":
            raise PolicyViolation("container execution permits only bind mounts")
        source = parts.get("src")
        destination = parts.get("dst")
        if source is None or destination not in {"/input", "/output", "/scratch"}:
            raise PolicyViolation("container mount source or destination drifted")
        source_path = Path(source)
        if not source_path.is_absolute() or source_path == Path("/"):
            raise PolicyViolation("container mount source is not a bounded absolute path")
        if any(part.casefold() in _ORACLE_SEGMENTS for part in source_path.parts):
            raise PolicyViolation("container mount cannot expose oracle content")
        if "docker.sock" in mount:
            raise PolicyViolation("container execution cannot mount a daemon socket")
        if destination in destinations:
            raise PolicyViolation("container mount destination is duplicated")
        destinations.add(destination)
        if destination == "/input" and parts.get("readonly") != "true":
            raise PolicyViolation("candidate container mount must be read-only")
        if destination != "/input" and "readonly" in parts:
            raise PolicyViolation("only output and scratch mounts may be writable")
    expected_destinations = (
        {"/input", "/output", "/scratch"} if mode == "validate" else {"/output", "/scratch"}
    )
    if destinations != expected_destinations:
        raise PolicyViolation("container mount destinations drifted")
    mount_tokens = tuple(token for mount in mount_values for token in ("--mount", mount))
    name = argv[argv.index("--name") + 1]
    expected_argv = (
        argv[0],
        "create",
        "--pull",
        "never",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "128",
        "--pid",
        "private",
        "--ipc",
        "private",
        "--cgroupns",
        "private",
        "--memory",
        "1024m",
        "--cpus",
        "1.0",
        "--user",
        manifest.user,
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs",
        "/work:rw,nosuid,nodev,size=768m",
        "--log-driver",
        "none",
        *mount_tokens,
        manifest.image_ref,
        mode,
    )
    if argv != expected_argv:
        raise PolicyViolation("container argv has non-controller arguments or ordering")


def _run_container_lifecycle(
    backend: _RuntimeOwnedContainerBackend,
    create_argv: tuple[str, ...],
    *,
    timeout_seconds: float,
) -> _ContainerValidationOutcome:
    raw_mode = create_argv[-1]
    if raw_mode == "probe":
        mode: Literal["probe", "validate"] = "probe"
    elif raw_mode == "validate":
        mode = "validate"
    else:
        raise PolicyViolation("container lifecycle mode is not controller-owned")
    _require_container_run_contract(create_argv, backend.manifest, mode=mode)
    name = create_argv[create_argv.index("--name") + 1]
    started = datetime.now(UTC)
    execution_material: dict[str, object] = {
        "create_argv": create_argv,
        "authority_digest": artifact_digest(backend.authority),
    }
    create_result = backend.runner.run(
        create_argv,
        timeout_seconds=_CONTAINER_COMMAND_TIMEOUT_SECONDS,
    )
    _require_result_binding(create_result, create_argv)
    execution_material["create_result"] = _sanitized_command_result(create_result)
    created = create_result.exit_code == 0 and not create_result.timed_out
    exit_code = create_result.exit_code
    timed_out = create_result.timed_out
    try:
        if created:
            container_id = create_result.stdout.strip()
            if _CONTAINER_ID.fullmatch(container_id) is None:
                raise PolicyViolation("container daemon returned an invalid container ID")
            inspect_argv = (str(backend.executable), "container", "inspect", name)
            inspect_result = backend.runner.run(
                inspect_argv,
                timeout_seconds=_CONTAINER_COMMAND_TIMEOUT_SECONDS,
            )
            _require_command_result(
                inspect_result,
                inspect_argv,
                "created container inspection",
            )
            _verify_created_container(
                inspect_result.stdout,
                create_argv,
                container_id,
                backend.manifest,
            )
            execution_material["container_inspect"] = _sanitized_command_result(inspect_result)

            start_argv = (str(backend.executable), "start", name)
            start_result = backend.runner.run(
                start_argv,
                timeout_seconds=_CONTAINER_COMMAND_TIMEOUT_SECONDS,
            )
            _require_command_result(start_result, start_argv, "container start")
            execution_material["start_result"] = _sanitized_command_result(start_result)

            wait_argv = (str(backend.executable), "wait", name)
            wait_result = backend.runner.run(wait_argv, timeout_seconds=timeout_seconds)
            _require_result_binding(wait_result, wait_argv)
            execution_material["wait_result"] = _sanitized_command_result(wait_result)
            timed_out = wait_result.timed_out
            if timed_out:
                exit_code = TIMEOUT_EXIT_CODE
                kill_argv = (str(backend.executable), "kill", "--signal", "KILL", name)
                kill_result = backend.runner.run(
                    kill_argv,
                    timeout_seconds=_CONTAINER_COMMAND_TIMEOUT_SECONDS,
                )
                _require_command_result(
                    kill_result,
                    kill_argv,
                    "container timeout kill",
                )
                execution_material["kill_result"] = _sanitized_command_result(kill_result)
            else:
                if wait_result.exit_code != 0:
                    raise PolicyViolation("container wait command failed")
                exit_code = _parse_container_exit(wait_result.stdout)
    finally:
        if created:
            remove_argv = (str(backend.executable), "rm", "--force", name)
            remove_result = backend.runner.run(
                remove_argv,
                timeout_seconds=_CONTAINER_COMMAND_TIMEOUT_SECONDS,
            )
            _require_command_result(remove_result, remove_argv, "container cleanup")
            execution_material["remove_result"] = _sanitized_command_result(remove_result)
    ended = datetime.now(UTC)
    return _ContainerValidationOutcome(
        exit_code=exit_code,
        timed_out=timed_out,
        execution_digest=artifact_digest(execution_material),
        started_at=started,
        ended_at=ended,
    )


def _verify_created_container(
    payload: str,
    create_argv: tuple[str, ...],
    container_id: str,
    manifest: _EnabledContainerAuthorityManifest,
) -> None:
    raw = json.loads(payload)
    if not isinstance(raw, list) or len(raw) != 1:
        raise PolicyViolation("created container inspection must contain one object")
    value = _mapping(raw[0], "created container inspection")
    name = create_argv[create_argv.index("--name") + 1]
    mode = create_argv[-1]
    if (
        value.get("Id") != container_id
        or value.get("Name") != f"/{name}"
        or value.get("Image") != manifest.image_config_digest
    ):
        raise PolicyViolation("created container identity drifted")
    config = _mapping(value.get("Config"), "created container config")
    if (
        config.get("Image") != manifest.image_ref
        or config.get("User") != manifest.user
        or _string_tuple(config.get("Entrypoint"), "created entrypoint") != manifest.entrypoint
        or _string_tuple(config.get("Cmd"), "created command") != (mode,)
        or _string_tuple(config.get("Env"), "created environment") != manifest.image_environment
        or config.get("WorkingDir") != manifest.working_directory
        or _mapping(config.get("Labels"), "created labels") != dict(manifest.labels)
    ):
        raise PolicyViolation("created container config drifted")
    host = _mapping(value.get("HostConfig"), "created container HostConfig")
    expected_host = {
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "CapAdd": [],
        "SecurityOpt": ["no-new-privileges:true"],
        "PidsLimit": 128,
        "Memory": 1024 * 1024 * 1024,
        "NanoCpus": 1_000_000_000,
        "Privileged": False,
        "PidMode": "private",
        "IpcMode": "private",
        "CgroupnsMode": "private",
        "LogConfig": {"Type": "none", "Config": {}},
        "Tmpfs": {
            "/tmp": "rw,noexec,nosuid,nodev,size=64m",
            "/work": "rw,nosuid,nodev,size=768m",
        },
    }
    for key, expected in expected_host.items():
        if host.get(key) != expected:
            raise PolicyViolation(f"created container HostConfig drifted: {key}")
    mounts = value.get("Mounts")
    if not isinstance(mounts, list):
        raise PolicyViolation("created container mount inspection is missing")
    expected_mounts = {
        parts["dst"]: (parts["src"], parts.get("readonly") != "true")
        for parts in (
            _mount_mapping(create_argv[index + 1])
            for index, token in enumerate(create_argv[:-1])
            if token == "--mount"
        )
    }
    observed_mounts: dict[str, tuple[str, bool]] = {}
    for raw_mount in mounts:
        mount = _mapping(raw_mount, "created container mount")
        if mount.get("Type") != "bind":
            raise PolicyViolation("created container includes a non-bind mount")
        source = mount.get("Source")
        destination = mount.get("Destination")
        writable = mount.get("RW")
        if (
            not isinstance(source, str)
            or not isinstance(destination, str)
            or not isinstance(writable, bool)
        ):
            raise PolicyViolation("created container mount evidence is malformed")
        observed_mounts[destination] = (source, writable)
    if observed_mounts != expected_mounts:
        raise PolicyViolation("created container effective mounts drifted")


def _verify_probe_output(
    output_root: Path,
    manifest: _EnabledContainerAuthorityManifest,
) -> None:
    root = _safe_directory(output_root, "container probe output")
    entries = tuple(sorted(root.iterdir(), key=lambda path: path.name))
    if tuple(path.name for path in entries) != ("probe.json",):
        raise PolicyViolation("container probe output is incomplete or contains extras")
    probe = _read_bounded_json(entries[0], max_bytes=16 * 1024)
    expected = {
        "schema_version": "1.0",
        "contract": "mulesoft-munit-container-v1",
        "uid": int(manifest.user.split(":", 1)[0]),
        "network": "none",
        "rootfs_read_only": True,
        "capabilities": [],
        "no_new_privileges": True,
        "candidate_mounted": False,
        "docker_socket_present": False,
        "execution_contract_sha256": manifest.execution_contract_sha256,
        "toolchain": manifest.toolchain_probe.model_dump(mode="json"),
    }
    if probe != expected:
        raise PolicyViolation("container isolation probe did not prove the fixed profile")


def build_mulesoft_local_validator(
    session: AgentRunSession,
) -> MuleSoftLocalValidator:
    """Bind the fail-closed MuleSoft validator to one immutable run session."""

    return MuleSoftLocalValidator(session)


def _passed_result(
    check: ValidationCommand,
    receipt: ToolReceipt,
    summary: str,
) -> CheckResult:
    return CheckResult(
        check_id=check.check_id,
        command_id=check.command_id,
        required=check.required,
        status=CheckStatus.PASSED,
        receipt=receipt,
        summary=summary,
    )


def _failed_result(
    check: ValidationCommand,
    receipt: ToolReceipt,
    summary: str,
) -> CheckResult:
    return CheckResult(
        check_id=check.check_id,
        command_id=check.command_id,
        required=check.required,
        status=CheckStatus.FAILED,
        receipt=receipt,
        summary=summary,
    )


def _unavailable_result(
    check: ValidationCommand,
    summary: str,
    *,
    receipt: ToolReceipt | None = None,
) -> CheckResult:
    return CheckResult(
        check_id=check.check_id,
        command_id=check.command_id,
        required=check.required,
        status=CheckStatus.UNAVAILABLE,
        receipt=receipt,
        summary=summary,
    )


def _controller_receipt(
    check: ValidationCommand,
    session: AgentRunSession,
    request: MigrationRequest,
    manifest: MigrationManifest,
    change_digest: str,
    attempt: int,
    *,
    exit_code: int,
    operation: str,
) -> ToolReceipt:
    now = datetime.now(UTC)
    binding = execution_binding(
        request.request_id,
        session.context.run_id,
        check.command_id,
        attempt,
        manifest.base_revision,
        EnvironmentKind.LOCAL,
        change_digest,
    )
    return ToolReceipt(
        receipt_id=f"receipt-{binding[:24]}",
        tool_id=check.command_id,
        request_id=request.request_id,
        run_id=session.context.run_id,
        attempt=attempt,
        base_revision=manifest.base_revision,
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=change_digest,
        operation=f"{operation};binding:{binding}",
        working_directory=session.context.run_directory,
        started_at=now,
        ended_at=now,
        exit_code=exit_code,
        terminal=True,
    )


def _container_receipt(
    check: ValidationCommand,
    session: AgentRunSession,
    request: MigrationRequest,
    manifest: MigrationManifest,
    change_digest: str,
    attempt: int,
    outcome: _ContainerValidationOutcome,
    authority: _ContainerAuthorityRecord,
    probe_execution_digest: str,
    *,
    artifacts: tuple[ArtifactDigest, ...],
    report_set_digest: str | None,
    evidence_digest: str | None,
    behavior_contract_digest: str,
    behavior_report_digest: str | None,
) -> ToolReceipt:
    binding = execution_binding(
        request.request_id,
        session.context.run_id,
        check.command_id,
        attempt,
        manifest.base_revision,
        EnvironmentKind.LOCAL,
        change_digest,
    )
    return ToolReceipt(
        receipt_id=f"receipt-{binding[:24]}",
        tool_id=check.command_id,
        request_id=request.request_id,
        run_id=session.context.run_id,
        attempt=attempt,
        base_revision=manifest.base_revision,
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=change_digest,
        operation=(
            f"runtime-owned-container:{check.command_id};binding:{binding};"
            f"manifest:{authority.authority_manifest_digest};"
            f"authority:{artifact_digest(authority)};probe:{probe_execution_digest};"
            f"execution:{outcome.execution_digest};reports:{report_set_digest or 'none'};"
            f"behavior-contract:{behavior_contract_digest};"
            f"behavior-report:{behavior_report_digest or 'none'};"
            f"evidence:{evidence_digest or 'none'}"
        ),
        working_directory=session.context.run_directory,
        started_at=outcome.started_at,
        ended_at=outcome.ended_at,
        exit_code=outcome.exit_code,
        terminal=True,
        artifacts=artifacts,
    )


def _disposition(results: tuple[CheckResult, ...]) -> ValidationDisposition:
    required = tuple(result for result in results if result.required)
    if any(result.status is CheckStatus.UNAVAILABLE for result in required):
        return ValidationDisposition.ENVIRONMENT_UNAVAILABLE
    if any(result.status is CheckStatus.FAILED for result in required):
        return ValidationDisposition.RECOVERABLE_FAILURE
    if all(result.status is CheckStatus.PASSED for result in required):
        return ValidationDisposition.READY_FOR_HUMAN_REVIEW
    return ValidationDisposition.PLAN_INVALID


def _require_changes_match(changes: WorkspaceChanges, change_set: ChangeSet) -> None:
    if (
        changes.before_revision != change_set.base_revision
        or changes.changed_paths != change_set.changed_paths
        # StrictModel normalizes leading/trailing whitespace on the persisted
        # ChangeSet string; compare the workspace diff in that same canonical
        # form rather than treating its final newline as semantic drift.
        or changes.unified_diff.strip() != change_set.unified_diff
    ):
        raise PolicyViolation("MuleSoft workspace changes do not match the bound change set")


def _report_id(run_id: str, request_id: str, attempt: int) -> str:
    material = f"{run_id}\x00{request_id}\x00mulesoft\x00{attempt}".encode()
    return f"report-{hashlib.sha256(material).hexdigest()[:24]}"


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _safe_controller_executable(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PolicyViolation("runtime-owned container CLI is not installed") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PolicyViolation("runtime-owned container CLI is not a regular file")
    if metadata.st_nlink != 1:
        raise PolicyViolation("runtime-owned container CLI must have one link")
    if metadata.st_mode & 0o111 == 0 or metadata.st_mode & 0o022:
        raise PolicyViolation("runtime-owned container CLI permissions are unsafe")
    if metadata.st_uid not in {0, os.geteuid()}:
        raise PolicyViolation("runtime-owned container CLI has an unsupported owner")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise PolicyViolation("runtime-owned container CLI path is not canonical")
    _trusted_parent_records(resolved, "runtime-owned container CLI")
    return resolved


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
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise PolicyViolation("digest input changed while being read")
    finally:
        os.close(descriptor)
    return f"sha256:{digest.hexdigest()}"


def _require_result_binding(
    result: _ContainerCommandResult,
    expected_argv: tuple[str, ...],
) -> None:
    if result.argv != expected_argv:
        raise PolicyViolation("container runner result belongs to different argv")


def _require_command_result(
    result: _ContainerCommandResult,
    expected_argv: tuple[str, ...],
    role: str,
) -> None:
    _require_result_binding(result, expected_argv)
    if result.timed_out or result.exit_code != 0:
        raise PolicyViolation(f"{role} did not complete successfully")


def _sanitized_command_result(result: _ContainerCommandResult) -> Mapping[str, object]:
    return {
        "argv_digest": artifact_digest(result.argv),
        "exit_code": result.exit_code,
        "stdout_digest": f"sha256:{hashlib.sha256(result.stdout.encode()).hexdigest()}",
        "stderr_digest": f"sha256:{hashlib.sha256(result.stderr.encode()).hexdigest()}",
        "timed_out": result.timed_out,
    }


def _json_mapping(payload: str) -> Mapping[str, Any]:
    return _mapping(json.loads(payload), "container JSON")


def _mapping(value: object, role: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PolicyViolation(f"{role} must be a JSON object with string keys")
    return value


def _string_tuple(value: object, role: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PolicyViolation(f"{role} must be a string list")
    if len(value) > 256 or any(len(item) > 4096 for item in value):
        raise PolicyViolation(f"{role} exceeds the bounded contract")
    return tuple(value)


def _mount_spec(path: Path, destination: str, *, read_only: bool) -> str:
    root = _safe_directory(path, f"container mount {destination}")
    rendered = str(root)
    if "," in rendered or any(ord(character) < 32 for character in rendered):
        raise PolicyViolation("container mount path contains an unsafe delimiter")
    suffix = ",readonly=true" if read_only else ""
    return f"type=bind,src={rendered},dst={destination}{suffix}"


def _mount_mapping(specification: str) -> Mapping[str, str]:
    result: dict[str, str] = {}
    for component in specification.split(","):
        if "=" not in component:
            raise PolicyViolation("container mount component has no value")
        key, value = component.split("=", 1)
        if key in result or not key or not value:
            raise PolicyViolation("container mount component is invalid or duplicated")
        result[key] = value
    if set(result) not in (
        {"type", "src", "dst"},
        {"type", "src", "dst", "readonly"},
    ):
        raise PolicyViolation("container mount contains unsupported options")
    return result


def _parse_container_exit(payload: str) -> int:
    normalized = payload.strip()
    if not normalized.isascii() or not normalized.isdigit():
        raise PolicyViolation("container wait did not return a bounded exit code")
    value = int(normalized)
    if value < 0 or value > 255:
        raise PolicyViolation("container exit code is outside the supported range")
    return value


def _read_bounded_json(path: Path, *, max_bytes: int) -> Mapping[str, Any]:
    payload = _read_regular_file(path, max_bytes=max_bytes, role="container JSON evidence")
    try:
        return _mapping(json.loads(payload.decode("utf-8")), "container JSON evidence")
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("container JSON evidence is malformed") from exc


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
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise PolicyViolation(f"{role} changed while being read")
    finally:
        os.close(descriptor)
    return payload


def _read_surefire_reports(
    report_root: Path,
) -> tuple[tuple[bytes, ...], tuple[ArtifactDigest, ...], str]:
    try:
        report_root.lstat()
    except FileNotFoundError as exc:
        raise MuleSoftEvidenceError("MUnit produced no Surefire report directory") from exc
    root = _safe_directory(report_root, "MUnit Surefire report directory")
    bounded_entries: list[Path] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if len(bounded_entries) >= _CONTAINER_MAX_REPORT_DIRECTORY_ENTRIES:
                raise MuleSoftEvidenceError("MUnit report directory has too many artifacts")
            bounded_entries.append(Path(entry.path))
    payloads: list[bytes] = []
    artifacts: list[ArtifactDigest] = []
    total = 0
    for path in sorted(bounded_entries, key=lambda item: item.name):
        if not (path.name.startswith("TEST-") and path.name.endswith(".xml")):
            raise MuleSoftEvidenceError("MUnit report directory contains an unexpected artifact")
        if len(payloads) >= MAX_XML_REPORTS:
            raise MuleSoftEvidenceError("MUnit report count exceeds the parser limit")
        payload = _read_regular_file(
            path,
            max_bytes=MAX_XML_REPORT_BYTES,
            role="MUnit Surefire XML",
        )
        total += len(payload)
        if total > _CONTAINER_MAX_REPORT_TOTAL_BYTES:
            raise MuleSoftEvidenceError("MUnit aggregate report bytes exceed the limit")
        digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        payloads.append(payload)
        artifacts.append(
            ArtifactDigest(
                path=f"surefire-reports/{path.name}",
                sha256=digest,
                size_bytes=len(payload),
            )
        )
    if not payloads:
        raise MuleSoftEvidenceError("MUnit produced no Surefire XML reports")
    artifact_tuple = tuple(artifacts)
    report_set_digest = artifact_digest(
        tuple(artifact.model_dump(mode="json") for artifact in artifact_tuple)
    )
    return tuple(payloads), artifact_tuple, report_set_digest


def _install_controller_behavior_suite(
    candidate_root: Path,
    contract: _ControllerBehaviorContract,
    payload: bytes,
) -> None:
    """Add one controller-only suite to the disposable runtime copy."""

    root = _safe_directory(candidate_root, "controller behavior candidate root")
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
    if _file_digest(destination) != contract.suite_sha256:
        raise PolicyViolation("installed controller behavior suite digest mismatch")


def _validate_controller_behavior_reports(
    reports: tuple[bytes, ...],
    artifacts: tuple[ArtifactDigest, ...],
    contract: _ControllerBehaviorContract,
    contract_binding_digest: str,
) -> _ControllerBehaviorReportBinding:
    """Bind terminal JUnit evidence to both exact MUnit suites and test names."""

    if len(reports) != len(artifacts):
        raise MuleSoftEvidenceError("MUnit report payload and artifact counts differ")
    generated_identity = (_GENERATED_MUNIT_SUITE_NAME, _GENERATED_MUNIT_TEST_NAME)
    controller_identity = (contract.suite_name, contract.test_name)
    expected = {generated_identity, controller_identity}
    artifact_by_identity: dict[tuple[str, str], ArtifactDigest] = {}
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
        if len(testcases) != 1:
            raise MuleSoftEvidenceError("MUnit identity report must contain one direct test")
        identity = (root.attrib.get("name", ""), testcases[0].attrib.get("name", ""))
        if identity not in expected or identity in artifact_by_identity:
            raise MuleSoftEvidenceError("MUnit suite or test identity is unexpected or duplicated")
        artifact_by_identity[identity] = artifact
    if set(artifact_by_identity) != expected:
        raise MuleSoftEvidenceError("MUnit evidence omitted a required behavior suite")
    return _ControllerBehaviorReportBinding(
        contract_binding_digest=contract_binding_digest,
        controller_report=artifact_by_identity[controller_identity],
        controller_suite_name=contract.suite_name,
        controller_test_name=contract.test_name,
        supplemental_report=artifact_by_identity[generated_identity],
        supplemental_suite_name=_GENERATED_MUNIT_SUITE_NAME,
        supplemental_test_name=_GENERATED_MUNIT_TEST_NAME,
    )


def _remove_attempt_root(path: Path, scratch_root: Path) -> None:
    safe_scratch = _safe_directory(scratch_root, "session scratch directory")
    try:
        relative = path.resolve(strict=True).relative_to(safe_scratch)
    except (FileNotFoundError, ValueError) as exc:
        raise PolicyViolation("container validation scratch escaped before cleanup") from exc
    if len(relative.parts) != 1 or not relative.name.startswith("mulesoft-container-"):
        raise PolicyViolation("refusing to clean an unexpected container scratch path")
    shutil.rmtree(path)


def _make_tree_container_readable(root: Path) -> None:
    safe_root = _safe_directory(root, "container candidate copy")
    for current, directories, files in os.walk(safe_root, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            raise PolicyViolation("container candidate copy contains a directory symlink")
        os.chmod(current_path, 0o555)
        for name in directories:
            directory = current_path / name
            if directory.is_symlink():
                raise PolicyViolation("container candidate copy contains a directory symlink")
        for name in files:
            path = current_path / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise PolicyViolation("container candidate copy contains an unsafe file")
            os.chmod(path, 0o444)


def _make_tree_controller_writable(root: Path) -> None:
    for current, directories, _files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink():
            raise PolicyViolation("container candidate copy changed to a symlink")
        os.chmod(current_path, 0o700)
        for name in directories:
            directory = current_path / name
            if directory.is_symlink():
                raise PolicyViolation("container candidate copy changed to a symlink")


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


def _reject_oracle_path(path: Path, role: str) -> None:
    if any(part.casefold() in _ORACLE_SEGMENTS for part in path.parts):
        raise PolicyViolation(f"{role} cannot contain expected, golden, or oracle segments")


__all__ = [
    "MULESOFT_CANDIDATE_CONTRACT_COMMAND_ID",
    "MULESOFT_DEPENDENCY_CLOSURE_COMMAND_ID",
    "MULESOFT_MUNIT_ARGV",
    "MULESOFT_MUNIT_COMMAND_ID",
    "MULESOFT_PLATFORM_ADAPTER",
    "MULESOFT_RUNTIME_AUTHORITY_ANCHOR_KIND",
    "MULESOFT_RUNTIME_CONFIG",
    "MULESOFT_SCOPE_POLICY",
    "MULESOFT_SOURCE_VERSION",
    "MULESOFT_TARGET_RUNTIME",
    "MULESOFT_TARGET_VERSION",
    "MULESOFT_TOOLCHAIN_CONTRACT_COMMAND_ID",
    "MULESOFT_VALIDATION_COMMAND_IDS",
    "MULESOFT_WORKSPACE_FINGERPRINT_COMMAND_ID",
    "MuleSoftLocalValidator",
    "build_mulesoft_local_validator",
]
