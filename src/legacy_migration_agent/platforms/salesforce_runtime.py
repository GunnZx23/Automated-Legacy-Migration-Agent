"""Supported Salesforce preset and deterministic local validation runtime.

The model may author only the eleven Salesforce solution files declared by
``SALESFORCE_AGENT_OUTPUT_PATHS``.  Dependency manifests, dependency locks,
Jest configuration, executable paths, and command arguments remain trusted
controller inputs.  This module intentionally performs local validation only;
even a completely successful report is merely ready for human review and does
not claim Salesforce sandbox validation, deployment, or production acceptance.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Final, cast

from legacy_migration_agent.agent_runtime.agent_definitions import AgentRegistry, AgentRole
from legacy_migration_agent.contracts import (
    ApprovalAction,
    ChangeSet,
    CheckResult,
    CheckStatus,
    EnvironmentKind,
    MigrationManifest,
    MigrationRequest,
    Platform,
    ValidationReport,
)
from legacy_migration_agent.core.execution import ExecutionResult, SafeCommandRunner
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.observability import lifecycle_event
from legacy_migration_agent.core.policies import (
    CommandRegistry,
    CommandSpec,
    PolicyViolation,
    RetryBudget,
    validate_change_set,
    validate_manifest_for_request,
    validate_report,
)
from legacy_migration_agent.core.run_session import AgentDefinitionDigests, AgentRunSession
from legacy_migration_agent.core.scope_policy import (
    MigrationScopePolicy,
    PlatformAdapter,
    validate_manifest_transformation_scope,
)
from legacy_migration_agent.core.workspace import IsolatedWorkspace, WorkspaceChanges, snapshot_tree
from legacy_migration_agent.graphs.dependency_graph import (
    SALESFORCE_ANALYZER_VERSION,
    build_salesforce_dependency_graph,
)
from legacy_migration_agent.platforms import salesforce_result_parsing as _result_parsing
from legacy_migration_agent.platforms.local_checks import (
    LWC_CONTROLLER_TEST_PATH,
    LWC_JEST_TOOLCHAIN_DIGESTS,
    LWC_JEST_VERSION,
    LWC_TEST_PATH,
    SALESFORCE_AGENT_OUTPUT_PATHS,
    SALESFORCE_IMPLEMENTATION_CONTRACT,
    tree_fingerprint,
)
from legacy_migration_agent.platforms.platform_runtime import PlatformRuntimeConfig
from legacy_migration_agent.platforms.salesforce_result_parsing import (
    SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID,
    SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID,
    SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
    SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID,
    SALESFORCE_LWC_JEST_COMMAND_ID,
    SALESFORCE_MIN_CANDIDATE_LWC_JEST_TESTS,
    SALESFORCE_SANDBOX_PROBE_COMMAND_ID,
    SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID,
    SALESFORCE_WORKSPACE_FINGERPRINT_COMMAND_ID,
)

_candidate_failure_evidence = _result_parsing._candidate_failure_evidence
_candidate_failure_summary = _result_parsing._candidate_failure_summary
_controller_jest_failure_evidence = _result_parsing._controller_jest_failure_evidence
_controller_jest_summary = _result_parsing._controller_jest_summary
_disposition = _result_parsing._disposition
_jest_failure_summary = _result_parsing._jest_failure_summary
_jest_summary = _result_parsing._jest_summary
_json_object = _result_parsing._json_object
_require_fields = _result_parsing._require_fields
_result_from_execution = _result_parsing._result_from_execution
_summary_parser = _result_parsing._summary_parser
_unavailable_result = _result_parsing._unavailable_result
_unmet_runtime_prerequisite = _result_parsing._unmet_runtime_prerequisite


SALESFORCE_VALIDATION_COMMAND_IDS: Final = (
    SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID,
    SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID,
    SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID,
    SALESFORCE_SANDBOX_PROBE_COMMAND_ID,
    SALESFORCE_LWC_JEST_COMMAND_ID,
    SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
    SALESFORCE_WORKSPACE_FINGERPRINT_COMMAND_ID,
)
_PYTHON_COMMAND_IDS: Final = frozenset(
    {
        SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID,
        SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID,
        SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID,
        SALESFORCE_WORKSPACE_FINGERPRINT_COMMAND_ID,
    }
)

SALESFORCE_SOURCE_ENTRY: Final = "force-app/main/default/pages/LegacyAccountContactExplorer.page"
SALESFORCE_TARGET_RUNTIME: Final = "Lightning Web Components with Apex"
SALESFORCE_API_RUNTIME: Final = "Salesforce API 67.0"
SALESFORCE_TRANSFORMATION_INPUT_PATHS: Final = tuple(
    sorted(
        (
            ".forceignore",
            "force-app/main/default/classes/LegacyAccountContactExplorerController.cls",
            "force-app/main/default/classes/LegacyAccountContactExplorerController.cls-meta.xml",
            "force-app/main/default/classes/LegacyAcctContactExplorerCtrlTest.cls",
            "force-app/main/default/classes/LegacyAcctContactExplorerCtrlTest.cls-meta.xml",
            "force-app/main/default/pages/LegacyAccountContactExplorer.page",
            "force-app/main/default/pages/LegacyAccountContactExplorer.page-meta.xml",
            "force-app/main/default/permissionsets/AccountContactExplorerUser.permissionset-meta.xml",
            "sfdx-project.json",
        )
    )
)

SALESFORCE_RUNTIME_CONFIG: Final = PlatformRuntimeConfig(
    platform=Platform.SALESFORCE,
    analyzer_version=SALESFORCE_ANALYZER_VERSION,
    graph_builder=build_salesforce_dependency_graph,
)

SALESFORCE_SCOPE_POLICY: Final = MigrationScopePolicy(
    policy_id="salesforce-vf-to-lwc-v11",
    platform=Platform.SALESFORCE,
    required_source_input_paths=SALESFORCE_TRANSFORMATION_INPUT_PATHS,
    approved_output_paths=SALESFORCE_AGENT_OUTPUT_PATHS,
    forbidden_paths=("jest.config.js", "package-lock.json", "package.json"),
    allowed_validation_command_ids=SALESFORCE_VALIDATION_COMMAND_IDS,
    required_validation_command_ids=SALESFORCE_VALIDATION_COMMAND_IDS,
    required_implementation_contract=SALESFORCE_IMPLEMENTATION_CONTRACT,
    max_changed_files=len(SALESFORCE_AGENT_OUTPUT_PATHS),
    required_approval_actions=(ApprovalAction.APPROVE_MANIFEST,),
)

SALESFORCE_PLATFORM_ADAPTER: Final = PlatformAdapter.bind(
    adapter_id="salesforce-vf-to-lwc-v11",
    policy=SALESFORCE_SCOPE_POLICY,
)

_TOOLCHAIN_RELATIVE: Final = Path("tooling/lwc-jest")
_JEST_ENTRY_RELATIVE: Final = Path("node_modules/jest/bin/jest.js")
_INSTALLED_TOOLCHAIN_FILES: Final = (
    Path("node_modules/@salesforce/sfdx-lwc-jest/package.json"),
    Path("node_modules/@salesforce/sfdx-lwc-jest/config.js"),
    Path("node_modules/jest/package.json"),
    Path("node_modules/jest-cli/bin/jest.js"),
    _JEST_ENTRY_RELATIVE,
)
_TOOLCHAIN_FILES: Final = tuple(LWC_JEST_TOOLCHAIN_DIGESTS)
_ORACLE_SEGMENTS: Final = frozenset({"expected", "golden", "oracle"})
_MAX_COMMAND_OUTPUT_CHARS: Final = 128 * 1024
_SANDBOX_BACKEND_ID: Final = "macos-sandbox-exec-v2"
_MACOS_SANDBOX_EXEC: Final = Path("/usr/bin/sandbox-exec")
_PACKAGE_BOUNDARY_FILENAME: Final = "package.json"
_PACKAGE_BOUNDARY_PENDING_FILENAME: Final = ".legacy-migration-package-boundary.pending"
_PACKAGE_BOUNDARY_POLICY_ID: Final = "salesforce-jest-package-boundary-v2"
_MAX_SANDBOX_EPOCHS_PER_ATTEMPT: Final = 8
_PACKAGE_BOUNDARY_BYTES: Final = (
    b'{\n  "name": "legacy-migration-candidate-boundary",\n'
    b'  "private": true,\n  "browserslist": ["defaults"]\n}\n'
)
_PACKAGE_BOUNDARY_SHA256: Final = "sha256:" + hashlib.sha256(_PACKAGE_BOUNDARY_BYTES).hexdigest()
_SUPPORTED_NODE_PATHS: Final = (
    Path("/usr/bin/node"),
    Path("/usr/local/bin/node"),
    Path("/opt/homebrew/bin/node"),
)
_HOMEBREW_NODE_CELLARS: Final = {
    Path("/usr/local/bin/node"): Path("/usr/local/Cellar/node"),
    Path("/opt/homebrew/bin/node"): Path("/opt/homebrew/Cellar/node"),
}
# Controller-owned identity of the approved external npm installation. Checked-in
# toolchain files are independently bound by LWC_JEST_TOOLCHAIN_DIGESTS, so changing
# a controller test cannot drift this node_modules-only authority.
_PINNED_NODE_MODULES_TREE_FINGERPRINT: Final = (
    "sha256:0e07e903284f743a968c08ae820d32ff79b8b8ebc7e0b725bd3b74c1ebcfce1d"
)


_SANDBOX_PROBE_PROGRAM: Final = r"""
import errno
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

(
    challenge,
    probe_digest,
    profile_digest,
    candidate_file,
    toolchain_file,
    package_boundary_file,
    package_boundary_device,
    package_boundary_inode,
    package_boundary_mode,
    package_boundary_uid,
    package_boundary_gid,
    package_boundary_size,
    package_boundary_link_count,
    package_boundary_sha256,
    record_path,
    outside_read,
    outside_write,
) = sys.argv[1:]

def denied(operation):
    try:
        operation()
    except PermissionError:
        return True
    except OSError as error:
        return error.errno in {errno.EACCES, errno.EPERM}
    return False

def readable(path):
    try:
        with Path(path).open("rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True

candidate_read_allowed = readable(candidate_file)
toolchain_read_allowed = readable(toolchain_file)
boundary_descriptor = None
try:
    boundary_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    boundary_descriptor = os.open(package_boundary_file, boundary_flags)
    boundary_metadata = os.fstat(boundary_descriptor)
    boundary_chunks = []
    while chunk := os.read(boundary_descriptor, 4096):
        boundary_chunks.append(chunk)
    boundary_content = b"".join(boundary_chunks)
    package_boundary_read_allowed = True
    package_boundary_verified = (
        boundary_metadata.st_dev == int(package_boundary_device)
        and boundary_metadata.st_ino == int(package_boundary_inode)
        and boundary_metadata.st_mode == int(package_boundary_mode)
        and boundary_metadata.st_uid == int(package_boundary_uid)
        and boundary_metadata.st_gid == int(package_boundary_gid)
        and boundary_metadata.st_size == int(package_boundary_size)
        and boundary_metadata.st_nlink == int(package_boundary_link_count)
        and "sha256:" + hashlib.sha256(boundary_content).hexdigest()
        == package_boundary_sha256
    )
except OSError:
    package_boundary_read_allowed = False
    package_boundary_verified = False
finally:
    if boundary_descriptor is not None:
        os.close(boundary_descriptor)
external_read_denied = denied(lambda: Path(outside_read).read_bytes())
external_write_denied = denied(
    lambda: Path(outside_write).write_text("sandbox escaped", encoding="utf-8")
)
scratch_canary = Path(record_path).with_suffix(".scratch-canary")
try:
    scratch_canary.write_text("scratch allowed", encoding="utf-8")
    scratch_write_allowed = scratch_canary.read_text(encoding="utf-8") == "scratch allowed"
    scratch_canary.unlink()
except OSError:
    scratch_write_allowed = False
network_denied = denied(
    lambda: socket.create_connection(("127.0.0.1", 9), timeout=0.2)
)
child_process_denied = denied(
    lambda: subprocess.run(
        [sys.executable, "-I", "-c", "pass"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
)
payload = {
    "check": "salesforce-jest-sandbox-probe",
    "passed": all(
        (
            candidate_read_allowed,
            toolchain_read_allowed,
            package_boundary_read_allowed,
            package_boundary_verified,
            scratch_write_allowed,
            external_read_denied,
            external_write_denied,
            network_denied,
            child_process_denied,
        )
    ),
    "backend_id": "macos-sandbox-exec-v2",
    "challenge": challenge,
    "probe_digest": probe_digest,
    "profile_digest": profile_digest,
    "candidate_read_allowed": candidate_read_allowed,
    "toolchain_read_allowed": toolchain_read_allowed,
    "package_boundary_read_allowed": package_boundary_read_allowed,
    "package_boundary_verified": package_boundary_verified,
    "package_boundary_path": package_boundary_file,
    "package_boundary_device": int(package_boundary_device),
    "package_boundary_inode": int(package_boundary_inode),
    "package_boundary_mode": int(package_boundary_mode),
    "package_boundary_uid": int(package_boundary_uid),
    "package_boundary_gid": int(package_boundary_gid),
    "package_boundary_size": int(package_boundary_size),
    "package_boundary_link_count": int(package_boundary_link_count),
    "package_boundary_sha256": package_boundary_sha256,
    "scratch_write_allowed": scratch_write_allowed,
    "external_read_denied": external_read_denied,
    "external_write_denied": external_write_denied,
    "network_denied": network_denied,
    "child_process_denied": child_process_denied,
}
record_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
record_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
record_descriptor = os.open(record_path, record_flags, 0o600)
try:
    remaining = memoryview(record_bytes)
    while remaining:
        written = os.write(record_descriptor, remaining)
        if written <= 0:
            raise OSError("sandbox probe record write did not progress")
        remaining = remaining[written:]
    os.fsync(record_descriptor)
finally:
    os.close(record_descriptor)
print(json.dumps(payload, sort_keys=True))
raise SystemExit(0 if payload["passed"] else 65)
""".strip()
_SANDBOX_PROBE_DIGEST: Final = (
    "sha256:" + hashlib.sha256(_SANDBOX_PROBE_PROGRAM.encode("utf-8")).hexdigest()
)
_SANDBOX_PROBE_BOOTSTRAP: Final = (
    "import base64;exec(compile(base64.b64decode("
    + repr(base64.b64encode(_SANDBOX_PROBE_PROGRAM.encode("utf-8")).decode("ascii"))
    + "),'<salesforce-sandbox-probe>','exec'))"
)

_SummaryParser = Callable[[ExecutionResult, Path], str]


@dataclass(frozen=True)
class _SandboxProbeBinding:
    attempt: int
    epoch_id: str
    challenge: str
    profile: str
    profile_digest: str
    candidate_profile: str | None
    candidate_profile_digest: str | None
    backend_digest: str
    probe_python_digest: str
    record_path: Path
    outside_write_path: Path
    candidate_fingerprint: str
    change_digest: str
    toolchain_fingerprint: str
    node_digest: str | None
    node_binding_digest: str | None
    package_boundary: _PackageBoundaryBinding


@dataclass(frozen=True)
class _VerifiedSandboxEvidence:
    binding: _SandboxProbeBinding
    record_digest: str
    policy_anchor_kind: str
    policy_anchor_payload: Mapping[str, str | int | None]
    epoch_anchor_kind: str
    epoch_anchor_payload: Mapping[str, str | int | None]


@dataclass(frozen=True)
class _PackageBoundaryBinding:
    descriptor: int = dataclass_field(repr=False, compare=False)
    path: Path
    parent_path: Path
    parent_device: int
    parent_inode: int
    parent_mode: int
    parent_uid: int
    parent_gid: int
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    link_count: int
    sha256: str


@dataclass(frozen=True)
class _ControllerPythonBinding:
    lexical_path: str
    lexical_device: int
    lexical_inode: int
    lexical_mode: int
    lexical_uid: int
    link_target: str | None
    resolved_path: str
    resolved_device: int
    resolved_inode: int
    resolved_mode: int
    resolved_uid: int
    resolved_sha256: str


@dataclass(frozen=True)
class _ExecutablePathComponent:
    path: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    link_target: str | None


@dataclass(frozen=True)
class _NodeExecutableBinding:
    lexical_path: str
    lexical_device: int
    lexical_inode: int
    lexical_mode: int
    lexical_uid: int
    lexical_gid: int
    link_target: str | None
    resolved_path: str
    resolved_device: int
    resolved_inode: int
    resolved_mode: int
    resolved_uid: int
    resolved_gid: int
    resolved_sha256: str
    path_components: tuple[_ExecutablePathComponent, ...]


class SalesforceLocalValidator:
    """Session-bound callable compatible with ``ModelAgentWorkflowRoles``.

    Executable discovery happens once, before any model output is considered.
    Every process is then resolved from a fixed command ID to a fixed argument
    vector and executed through :class:`SafeCommandRunner` without a shell.
    """

    def __init__(
        self,
        session: AgentRunSession,
        registry: AgentRegistry,
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._session = session
        self._registry = registry
        self._repository_root = _safe_directory(session.project_root, "project root")
        self._scratch_root = _safe_descendant_directory(
            self._repository_root,
            session.scratch_dir,
            "session scratch directory",
        )
        self._source_root = _safe_descendant_directory(
            self._repository_root,
            session.source_root,
            "session source root",
        )
        self._toolchain_root = self._repository_root / _TOOLCHAIN_RELATIVE
        marker_token = hashlib.sha256(session.context.run_id.encode()).hexdigest()[:16]
        self._sandbox_escape_marker = Path("/private/tmp") / (
            f".salesforce-sandbox-escape-{marker_token}"
        )
        # The already-running controller process is the trust boundary.  No
        # caller or model may select a different executable for local checks.
        self._python_executable = Path(sys.executable)
        self._controller_python_binding = _capture_controller_python(self._python_executable)
        self._probe_python = Path(self._controller_python_binding.resolved_path)
        self._controller_runtime_roots = _controller_runtime_roots()
        self._controller_python_anchor_kind = "salesforce-controller-python-v1"
        self._controller_python_anchor_payload = {
            **_controller_python_payload(self._controller_python_binding),
            "runtime_read_roots": tuple(map(str, self._controller_runtime_roots)),
        }
        self._sandbox_backend = _discover_protected_executable(_MACOS_SANDBOX_EXEC)
        # Node is relevant only when this host has the protected execution
        # boundary that can safely run candidate Jest. Linux CI intentionally
        # records that check as unavailable instead of binding an unrelated
        # preinstalled Node executable.
        self._node_binding = (
            _discover_supported_node() if self._sandbox_backend is not None else None
        )
        self._node_executable = (
            Path(self._node_binding.resolved_path) if self._node_binding is not None else None
        )
        self._node_anchor_kind = "salesforce-node-runtime-v1"
        self._node_anchor_payload = (
            _node_binding_payload(self._node_binding) if self._node_binding is not None else None
        )
        self._timeout_seconds = timeout_seconds
        self._context_digest = artifact_digest(session.context)
        self._agent_definition_digests = session.context.agent_definition_digests
        self._verify_session_integrity()
        self._session.bind_runtime_anchor(
            self._controller_python_anchor_kind,
            self._controller_python_anchor_payload,
        )
        self._verify_controller_python()
        if self._node_anchor_payload is not None:
            self._session.bind_runtime_anchor(
                self._node_anchor_kind,
                self._node_anchor_payload,
            )
            self._verify_node_runtime()

    def __call__(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        workspace: IsolatedWorkspace,
        attempt: int,
    ) -> ValidationReport:
        """Run the exact local Salesforce plan and return bounded evidence."""

        self._preflight(request, manifest, change_set, workspace, attempt)
        candidate_root = _safe_descendant_directory(
            self._repository_root,
            workspace.root,
            "candidate workspace",
        )
        package_boundary = _create_package_boundary(candidate_root)
        try:
            report = self._validate_with_package_boundary(
                request,
                manifest,
                change_set,
                workspace,
                attempt,
                package_boundary,
            )
        except BaseException as validation_error:
            try:
                _remove_package_boundary(package_boundary)
            except BaseException as cleanup_error:
                raise cleanup_error from validation_error
            raise
        else:
            _remove_package_boundary(package_boundary)
            return report

    def _validate_with_package_boundary(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        workspace: IsolatedWorkspace,
        attempt: int,
        package_boundary: _PackageBoundaryBinding,
    ) -> ValidationReport:
        """Execute validation while one exact adjacent package boundary is bound."""

        self._preflight(request, manifest, change_set, workspace, attempt)
        self._verify_controller_python()
        initial_changes = workspace.audit_changes()
        _require_changes_match(initial_changes, change_set)
        initial_fingerprint = tree_fingerprint(workspace.root)
        initial_evidence_fingerprint = tree_fingerprint(self._session.evidence_dir)
        workspace.assert_source_unchanged()

        candidate_root = _safe_descendant_directory(
            self._repository_root,
            workspace.root,
            "candidate workspace",
        )
        change_digest = artifact_digest(change_set)
        availability, specs, probe_binding = self._command_specs(
            candidate_root,
            initial_fingerprint,
            attempt,
            change_digest,
            package_boundary,
        )
        runner = (
            SafeCommandRunner(
                CommandRegistry(specs),
                self._repository_root,
                RetryBudget(maximum_attempts=2),
                timeout_seconds=self._timeout_seconds,
                max_output_chars=_MAX_COMMAND_OUTPUT_CHARS,
            )
            if specs
            else None
        )
        planned = {check.command_id: check for check in manifest.validation_plan}
        results: list[CheckResult] = []
        by_command: dict[str, CheckResult] = {}
        verified_sandbox: _VerifiedSandboxEvidence | None = None

        for ordinal, command_id in enumerate(SALESFORCE_VALIDATION_COMMAND_IDS, start=1):
            check = planned[command_id]
            unavailable_reason = availability[command_id]
            prerequisite_reason = _unmet_runtime_prerequisite(command_id, by_command)
            if prerequisite_reason is not None:
                unavailable_reason = prerequisite_reason
            lifecycle_event(
                "validation.command.considered",
                attempt=attempt,
                ordinal=ordinal,
                total=len(SALESFORCE_VALIDATION_COMMAND_IDS),
                check_id=check.check_id,
                command_id=command_id,
            )
            command_started_ns: int | None = None
            if unavailable_reason is not None:
                result = _unavailable_result(check, unavailable_reason)
                blocked_by = tuple(
                    prerequisite
                    for prerequisite in SALESFORCE_VALIDATION_COMMAND_IDS
                    if prerequisite_reason is not None and prerequisite in prerequisite_reason
                )
                lifecycle_event(
                    "validation.command.blocked",
                    level=logging.WARNING,
                    attempt=attempt,
                    check_id=check.check_id,
                    command_id=command_id,
                    reason_code=(
                        "prerequisite_failed"
                        if prerequisite_reason is not None
                        else "environment_unavailable"
                    ),
                    blocked_by=",".join(blocked_by) or "none",
                )
            else:
                command_started_ns = time.perf_counter_ns()
                lifecycle_event(
                    "validation.command.started",
                    attempt=attempt,
                    ordinal=ordinal,
                    total=len(SALESFORCE_VALIDATION_COMMAND_IDS),
                    check_id=check.check_id,
                    command_id=command_id,
                )
                if runner is None:  # pragma: no cover - availability and specs are paired
                    raise AssertionError("available command has no SafeCommandRunner")
                if command_id == SALESFORCE_SANDBOX_PROBE_COMMAND_ID:
                    if probe_binding is None:  # pragma: no cover - availability rejects it
                        raise AssertionError("available sandbox probe has no binding")
                    self._verify_probe_binding(probe_binding, candidate_root)
                if command_id in {
                    SALESFORCE_LWC_JEST_COMMAND_ID,
                    SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
                }:
                    if verified_sandbox is None:  # pragma: no cover - prerequisite rejects it
                        raise AssertionError("available Jest command has no verified sandbox")
                    self._verify_verified_sandbox(verified_sandbox, candidate_root)
                is_controller_python_command = command_id in (
                    *_PYTHON_COMMAND_IDS,
                    SALESFORCE_SANDBOX_PROBE_COMMAND_ID,
                )
                is_node_command = command_id in (
                    SALESFORCE_SANDBOX_PROBE_COMMAND_ID,
                    SALESFORCE_LWC_JEST_COMMAND_ID,
                    SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
                )
                if is_controller_python_command:
                    self._verify_controller_python()
                if is_node_command and self._node_binding is not None:
                    self._verify_node_runtime()
                try:
                    execution = runner.run(
                        command_id,
                        request_id=request.request_id,
                        run_id=self._session.context.run_id,
                        attempt=attempt,
                        base_revision=manifest.base_revision,
                        environment=EnvironmentKind.LOCAL,
                        artifact_digest=change_digest,
                        working_directory=_working_directory(
                            command_id,
                            candidate_root,
                            self._repository_root,
                        ),
                    )
                except Exception as error:
                    lifecycle_event(
                        "validation.command.failed",
                        level=logging.ERROR,
                        attempt=attempt,
                        check_id=check.check_id,
                        command_id=command_id,
                        error_type=type(error).__name__,
                        elapsed_ms=max(
                            0,
                            (time.perf_counter_ns() - command_started_ns) // 1_000_000,
                        ),
                    )
                    raise
                finally:
                    if is_controller_python_command:
                        self._verify_controller_python()
                    if is_node_command and self._node_binding is not None:
                        self._verify_node_runtime()
                    if command_id == SALESFORCE_SANDBOX_PROBE_COMMAND_ID:
                        assert probe_binding is not None
                        self._verify_probe_binding(probe_binding, candidate_root)
                    if command_id in {
                        SALESFORCE_LWC_JEST_COMMAND_ID,
                        SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
                    }:
                        assert verified_sandbox is not None
                        self._verify_verified_sandbox(verified_sandbox, candidate_root)
                parser = (
                    _probe_summary_parser(probe_binding)
                    if command_id == SALESFORCE_SANDBOX_PROBE_COMMAND_ID
                    else _summary_parser(
                        command_id,
                        controller_test_path=self._toolchain_root / LWC_CONTROLLER_TEST_PATH,
                    )
                )
                result = _result_from_execution(
                    check,
                    execution,
                    candidate_root,
                    parser,
                    controller_test_path=self._toolchain_root / LWC_CONTROLLER_TEST_PATH,
                )
                if (
                    command_id == SALESFORCE_SANDBOX_PROBE_COMMAND_ID
                    and self._sandbox_escape_marker.exists()
                ):
                    raise PolicyViolation("OS sandbox probe wrote outside the run scratch area")
                if (
                    command_id == SALESFORCE_SANDBOX_PROBE_COMMAND_ID
                    and result.status is CheckStatus.PASSED
                ):
                    assert probe_binding is not None
                    verified_sandbox = self._bind_verified_sandbox(
                        probe_binding,
                        candidate_root,
                        attempt,
                    )
                    result = result.model_copy(
                        update={
                            "summary": (
                                f"{result.summary} anchor="
                                f"{artifact_digest(verified_sandbox.epoch_anchor_payload)}."
                            )
                        }
                    )
            failure_match = re.search(
                r"\bfailure[-_]code=([a-z][a-z0-9_.:-]{0,159})\b",
                result.summary,
            )
            lifecycle_event(
                "validation.command.completed",
                attempt=attempt,
                check_id=check.check_id,
                command_id=command_id,
                status=result.status.value,
                exit_code=(result.receipt.exit_code if result.receipt is not None else None),
                diagnostic_ids=",".join(result.diagnostic_ids) or "none",
                failure_code=(failure_match.group(1) if failure_match is not None else None),
                elapsed_ms=(
                    max(0, (time.perf_counter_ns() - command_started_ns) // 1_000_000)
                    if command_started_ns is not None
                    else None
                ),
            )
            results.append(result)
            by_command[command_id] = result

        self._verify_session_integrity()
        self._verify_controller_python()
        workspace.assert_source_unchanged()
        if tree_fingerprint(self._session.evidence_dir) != initial_evidence_fingerprint:
            raise PolicyViolation("deterministic Salesforce validation mutated lifecycle evidence")
        final_fingerprint = tree_fingerprint(candidate_root)
        final_changes = workspace.audit_changes()
        if final_fingerprint != initial_fingerprint:
            raise PolicyViolation("deterministic Salesforce validation mutated the candidate tree")
        _require_changes_match(final_changes, change_set)

        disposition = _disposition(tuple(results))
        report = ValidationReport(
            report_id=_report_id(self._session.context.run_id, request.request_id, attempt),
            request_id=request.request_id,
            manifest_id=manifest.manifest_id,
            change_set_id=change_set.change_set_id,
            base_revision=manifest.base_revision,
            results=tuple(results),
            disposition=disposition,
            attempt=attempt,
        )
        validate_report(report, manifest, change_set)
        return report

    def _verify_controller_python(self) -> None:
        if _capture_controller_python(self._python_executable) != self._controller_python_binding:
            raise PolicyViolation("the controller Python executable identity changed")
        if _controller_runtime_roots() != self._controller_runtime_roots:
            raise PolicyViolation("the controller Python runtime roots changed")
        self._session.verify_runtime_anchor(
            self._controller_python_anchor_kind,
            self._controller_python_anchor_payload,
        )

    def _verify_node_runtime(self) -> None:
        if self._node_binding is None or self._node_anchor_payload is None:
            raise PolicyViolation("the supported Node executable is unavailable")
        if _discover_supported_node() != self._node_binding:
            raise PolicyViolation("the supported Node executable identity changed")
        self._session.verify_runtime_anchor(
            self._node_anchor_kind,
            self._node_anchor_payload,
        )

    def _verify_session_integrity(self) -> None:
        self._session.verify_index("initialized", exact=False)
        self._session.verify_source_revision()
        if artifact_digest(self._session.context) != self._context_digest:
            raise PolicyViolation("Salesforce validator session context drifted")
        if self._session.context.agent_definition_digests != self._agent_definition_digests:
            raise PolicyViolation("Salesforce validator agent-definition digests drifted")
        actual = _agent_definition_digests(self._registry)
        if actual != self._session.context.agent_definition_digests:
            raise PolicyViolation("loaded agent definitions do not match the run session")
        loaded = AgentRunSession.load(self._repository_root, self._session.run_dir)
        if loaded.context != self._session.context or loaded.run_dir != self._session.run_dir:
            raise PolicyViolation("Salesforce validator is bound to a foreign run session")

        evidence = snapshot_tree(self._session.evidence_dir)
        index_names: list[str] = []
        for entry in evidence.entries:
            path = Path(entry.path)
            if not entry.path.startswith("indexes/"):
                continue
            if len(path.parts) != 2 or path.suffix != ".json":
                raise PolicyViolation("Salesforce run contains an invalid lifecycle index path")
            index_names.append(path.stem)
        if "initialized" not in index_names:
            raise PolicyViolation("Salesforce run has no initialized lifecycle index")
        if len(index_names) != len(set(index_names)):
            raise PolicyViolation("Salesforce run contains duplicate lifecycle indexes")
        for kind in index_names:
            self._session.verify_index(kind, exact=False)

    def _verify_probe_binding(
        self,
        binding: _SandboxProbeBinding,
        candidate_root: Path,
    ) -> None:
        _verify_package_boundary(binding.package_boundary, candidate_root)
        backend = self._sandbox_backend
        discovered_backend = _discover_protected_executable(_MACOS_SANDBOX_EXEC)
        if backend is None or backend != discovered_backend:
            raise PolicyViolation("the supported OS sandbox backend is unavailable")
        self._verify_controller_python()
        if _file_sha256(backend) != binding.backend_digest:
            raise PolicyViolation("the supported OS sandbox executable changed")
        if self._controller_python_binding.resolved_sha256 != binding.probe_python_digest:
            raise PolicyViolation("the controller-owned sandbox probe runtime changed")
        if _digest_text(binding.profile) != binding.profile_digest:
            raise PolicyViolation("the controller-owned sandbox profile changed")
        if binding.candidate_profile is None:
            if (
                binding.candidate_profile_digest is not None
                or binding.node_digest is not None
                or binding.node_binding_digest is not None
            ):
                raise PolicyViolation("the candidate sandbox profile binding is inconsistent")
        elif _digest_text(binding.candidate_profile) != binding.candidate_profile_digest:
            raise PolicyViolation("the controller-owned candidate sandbox profile changed")
        expected_epoch = _sandbox_epoch_id(
            run_id=self._session.context.run_id,
            request_digest=self._session.context.request_digest,
            candidate_root=candidate_root,
            attempt=binding.attempt,
            candidate_fingerprint=binding.candidate_fingerprint,
            change_digest=binding.change_digest,
            profile_digest=binding.profile_digest,
            candidate_profile_digest=binding.candidate_profile_digest,
            backend_digest=binding.backend_digest,
            probe_python_digest=binding.probe_python_digest,
            toolchain_fingerprint=binding.toolchain_fingerprint,
            node_digest=binding.node_digest,
            node_binding_digest=binding.node_binding_digest,
            package_boundary=binding.package_boundary,
        )
        expected_record = self._scratch_root / (
            f"salesforce-sandbox-probe-{binding.attempt}-{expected_epoch[:24]}.json"
        )
        if (
            binding.attempt not in {1, 2}
            or binding.epoch_id != expected_epoch
            or binding.challenge != expected_epoch
            or binding.record_path != expected_record
        ):
            raise PolicyViolation("the sandbox epoch binding changed")
        if tree_fingerprint(candidate_root) != binding.candidate_fingerprint:
            raise PolicyViolation("the candidate changed after sandbox challenge creation")
        current_toolchain = _full_tree_fingerprint(self._toolchain_root)
        if current_toolchain != binding.toolchain_fingerprint:
            raise PolicyViolation("the Jest toolchain changed after sandbox challenge creation")
        if binding.node_digest is not None:
            self._verify_node_runtime()
            if (
                self._node_binding is None
                or self._node_binding.resolved_sha256 != binding.node_digest
                or artifact_digest(_node_binding_payload(self._node_binding))
                != binding.node_binding_digest
            ):
                raise PolicyViolation("the supported Node executable changed")
        if binding.record_path.parent != self._scratch_root:
            raise PolicyViolation("sandbox probe evidence is outside the run scratch area")
        if binding.outside_write_path != self._sandbox_escape_marker:
            raise PolicyViolation("sandbox probe canary binding changed")
        if self._sandbox_escape_marker.exists():
            raise PolicyViolation("OS sandbox probe wrote outside the run scratch area")

    def _bind_verified_sandbox(
        self,
        binding: _SandboxProbeBinding,
        candidate_root: Path,
        attempt: int,
    ) -> _VerifiedSandboxEvidence:
        self._verify_probe_binding(binding, candidate_root)
        record = _read_probe_record(binding.record_path)
        record_digest = artifact_digest(record)
        if binding.attempt != attempt:
            raise PolicyViolation("sandbox epoch is bound to a different attempt")
        policy_anchor_kind = f"salesforce-jest-sandbox-policy-{attempt}"
        policy_anchor_payload: Mapping[str, str | int | None] = {
            "run_id": self._session.context.run_id,
            "attempt": attempt,
            "backend_id": _SANDBOX_BACKEND_ID,
            "backend_digest": binding.backend_digest,
            "probe_python_digest": binding.probe_python_digest,
            "probe_digest": _SANDBOX_PROBE_DIGEST,
            "candidate_fingerprint": binding.candidate_fingerprint,
            "change_digest": binding.change_digest,
            "toolchain_fingerprint": binding.toolchain_fingerprint,
            "node_digest": binding.node_digest,
            "node_binding_digest": binding.node_binding_digest,
            "package_boundary_policy_id": _PACKAGE_BOUNDARY_POLICY_ID,
            "package_boundary_mode": binding.package_boundary.mode,
            "package_boundary_uid": binding.package_boundary.uid,
            "package_boundary_gid": binding.package_boundary.gid,
            "package_boundary_size": binding.package_boundary.size,
            "package_boundary_link_count": binding.package_boundary.link_count,
            "package_boundary_sha256": binding.package_boundary.sha256,
        }
        epoch_anchor_kind = f"salesforce-jest-sandbox-epoch-{attempt}-{binding.epoch_id[:24]}"
        epoch_anchor_payload: Mapping[str, str | int | None] = {
            **policy_anchor_payload,
            "epoch_id": binding.epoch_id,
            "challenge": binding.challenge,
            "profile_digest": binding.profile_digest,
            "candidate_profile_digest": binding.candidate_profile_digest,
            "record_path": str(binding.record_path),
            "record_digest": record_digest,
            "package_boundary_path": str(binding.package_boundary.path),
            "package_boundary_parent_device": str(binding.package_boundary.parent_device),
            "package_boundary_parent_inode": str(binding.package_boundary.parent_inode),
            "package_boundary_parent_mode": str(binding.package_boundary.parent_mode),
            "package_boundary_parent_uid": str(binding.package_boundary.parent_uid),
            "package_boundary_parent_gid": str(binding.package_boundary.parent_gid),
            "package_boundary_device": str(binding.package_boundary.device),
            "package_boundary_inode": str(binding.package_boundary.inode),
        }
        self._session.bind_runtime_anchor(policy_anchor_kind, policy_anchor_payload)
        self._require_sandbox_epoch_capacity(epoch_anchor_kind, attempt)
        self._session.bind_runtime_anchor(epoch_anchor_kind, epoch_anchor_payload)
        verified = _VerifiedSandboxEvidence(
            binding=binding,
            record_digest=record_digest,
            policy_anchor_kind=policy_anchor_kind,
            policy_anchor_payload=policy_anchor_payload,
            epoch_anchor_kind=epoch_anchor_kind,
            epoch_anchor_payload=epoch_anchor_payload,
        )
        self._verify_bound_probe(verified, candidate_root)
        return verified

    def _require_sandbox_epoch_capacity(self, epoch_anchor_kind: str, attempt: int) -> None:
        """Bound anchored and crash-left epochs before any new probe can spawn."""

        anchor_prefix = f"salesforce-jest-sandbox-epoch-{attempt}-"
        record_prefix = f"salesforce-sandbox-probe-{attempt}-"
        anchor_match = re.compile(rf"{re.escape(anchor_prefix)}([0-9a-f]{{24}})")
        record_match = re.compile(rf"{re.escape(record_prefix)}([0-9a-f]{{24}})\.json")
        current_match = anchor_match.fullmatch(epoch_anchor_kind)
        if current_match is None:
            raise PolicyViolation("sandbox epoch anchor kind is invalid")
        current_epoch = current_match.group(1)
        observed_epochs: set[str] = set()

        for path in self._session.runtime_anchors_dir.glob(f"{anchor_prefix}*.json"):
            match = anchor_match.fullmatch(path.stem)
            if match is None:
                raise PolicyViolation("sandbox epoch anchor inventory is invalid")
            self._session.has_runtime_anchor(path.stem)
            observed_epochs.add(match.group(1))
        for path in self._scratch_root.glob(f"{record_prefix}*.json"):
            match = record_match.fullmatch(path.name)
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise PolicyViolation("sandbox epoch record inventory is unavailable") from exc
            if (
                match is None
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
            ):
                raise PolicyViolation("sandbox epoch record inventory is invalid")
            observed_epochs.add(match.group(1))

        if current_epoch not in observed_epochs and (
            len(observed_epochs) >= _MAX_SANDBOX_EPOCHS_PER_ATTEMPT
        ):
            raise PolicyViolation("sandbox epoch recovery budget is exhausted")
        if len(observed_epochs) > _MAX_SANDBOX_EPOCHS_PER_ATTEMPT:
            raise PolicyViolation("sandbox epoch inventory exceeds its recovery budget")

    def _verify_bound_probe(
        self,
        evidence: _VerifiedSandboxEvidence,
        candidate_root: Path,
    ) -> None:
        self._verify_probe_binding(evidence.binding, candidate_root)
        if (
            artifact_digest(_read_probe_record(evidence.binding.record_path))
            != evidence.record_digest
        ):
            raise PolicyViolation("sandbox probe evidence changed after verification")
        self._session.verify_runtime_anchor(
            evidence.policy_anchor_kind,
            evidence.policy_anchor_payload,
        )
        self._session.verify_runtime_anchor(
            evidence.epoch_anchor_kind,
            evidence.epoch_anchor_payload,
        )

    def _verify_verified_sandbox(
        self,
        evidence: _VerifiedSandboxEvidence,
        candidate_root: Path,
    ) -> None:
        self._verify_bound_probe(evidence, candidate_root)
        node_modules = self._toolchain_root / "node_modules"
        if _full_tree_fingerprint(node_modules) != _PINNED_NODE_MODULES_TREE_FINGERPRINT:
            raise PolicyViolation(
                "installed Jest dependencies lost their controller-pinned identity"
            )
        if (
            evidence.binding.node_digest is None
            or evidence.binding.candidate_profile is None
            or evidence.binding.candidate_profile_digest is None
        ):
            raise PolicyViolation("no supported protected Node sandbox binding is available")

    def _preflight(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        workspace: IsolatedWorkspace,
        attempt: int,
    ) -> None:
        if attempt not in {1, 2}:
            raise PolicyViolation("Salesforce local validation supports attempts 1 and 2 only")
        self._verify_session_integrity()
        if artifact_digest(request) != self._session.context.request_digest:
            raise PolicyViolation("migration request does not match the bound run session")
        if request.base_revision != self._session.context.source_revision:
            raise PolicyViolation("migration request revision does not match the run session")
        if request.repository != self._session.context.source_root:
            raise PolicyViolation("Salesforce request repository does not match the source root")
        if request.target.entry_path != SALESFORCE_SOURCE_ENTRY:
            raise PolicyViolation("Salesforce request must target the fixed Visualforce entry")
        if request.target.target_runtime != SALESFORCE_TARGET_RUNTIME:
            raise PolicyViolation(
                "Salesforce request must target Lightning Web Components with Apex"
            )
        if (
            request.target.source_version != SALESFORCE_API_RUNTIME
            or request.target.target_version != SALESFORCE_API_RUNTIME
        ):
            raise PolicyViolation(
                "Salesforce request must remain on the supported API 67.0 contract"
            )
        validate_manifest_for_request(manifest, request)
        SALESFORCE_PLATFORM_ADAPTER.validate_manifest(manifest, request)
        validate_change_set(change_set, manifest)
        if manifest.approved_paths != SALESFORCE_AGENT_OUTPUT_PATHS:
            raise PolicyViolation("Salesforce manifest must declare the exact eleven output paths")
        if set(workspace.approved_paths) != set(SALESFORCE_AGENT_OUTPUT_PATHS):
            raise PolicyViolation("Salesforce workspace does not have the exact manifest scope")
        if workspace.base_revision != request.base_revision:
            raise PolicyViolation("Salesforce workspace is stale for the migration request")
        if workspace.source_root != self._source_root:
            raise PolicyViolation("Salesforce workspace belongs to a different source tree")
        candidate_parent = _safe_descendant_directory(
            self._repository_root,
            workspace.root.parent,
            "candidate workspace container",
        )
        if (
            candidate_parent.parent != self._session.workspaces_dir
            or workspace.root.name != "repository"
        ):
            raise PolicyViolation("Salesforce workspace is not owned by this run session")

        validate_manifest_transformation_scope(
            manifest,
            required_source_input_paths=SALESFORCE_TRANSFORMATION_INPUT_PATHS,
            approved_output_paths=SALESFORCE_AGENT_OUTPUT_PATHS,
        )

        commands = tuple(check.command_id for check in manifest.validation_plan)
        if len(commands) != len(set(commands)):
            raise PolicyViolation("Salesforce validation command IDs must be unique")
        if commands != SALESFORCE_VALIDATION_COMMAND_IDS:
            raise PolicyViolation("Salesforce validation plan has command drift or reordering")
        if any(not check.required for check in manifest.validation_plan):
            raise PolicyViolation("every supported Salesforce local check must be required")
        if any(
            check.environment is not EnvironmentKind.LOCAL for check in manifest.validation_plan
        ):
            raise PolicyViolation("Salesforce local validation commands must use local environment")

        _reject_oracle_path(self._repository_root, "project root")
        _reject_oracle_path(self._source_root, "source root")
        _reject_oracle_path(workspace.root, "candidate workspace")
        _reject_oracle_path(self._toolchain_root, "Jest toolchain")
        _reject_oracle_path(self._scratch_root, "session scratch directory")

    def _command_specs(
        self,
        candidate_root: Path,
        initial_fingerprint: str,
        attempt: int,
        change_digest: str,
        package_boundary: _PackageBoundaryBinding,
    ) -> tuple[
        dict[str, str | None],
        tuple[CommandSpec, ...],
        _SandboxProbeBinding | None,
    ]:
        python_available = _executable_available(self._python_executable)
        toolchain_available = _toolchain_contract_available(
            self._repository_root,
            self._toolchain_root,
        )
        backend_reason = self._sandbox_unavailable_reason()
        probe_binding: _SandboxProbeBinding | None = None
        if backend_reason is None:
            try:
                probe_binding = self._build_probe_binding(
                    candidate_root,
                    initial_fingerprint,
                    attempt,
                    change_digest,
                    package_boundary,
                )
            except PolicyViolation:
                backend_reason = "the controller-owned sandbox challenge could not be created"
        jest_reason = _jest_unavailable_reason(
            self._node_executable,
            self._repository_root,
            self._toolchain_root,
            self._scratch_root,
        )
        if jest_reason is None and backend_reason is not None:
            jest_reason = backend_reason

        availability: dict[str, str | None] = {
            SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID: (
                None if python_available else "the configured Python executable is unavailable"
            ),
            SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID: (
                None if python_available else "the configured Python executable is unavailable"
            ),
            SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID: (
                None
                if python_available and toolchain_available
                else (
                    "the configured Python executable is unavailable"
                    if not python_available
                    else "the pinned Jest toolchain files are unavailable"
                )
            ),
            SALESFORCE_SANDBOX_PROBE_COMMAND_ID: backend_reason,
            SALESFORCE_LWC_JEST_COMMAND_ID: jest_reason,
            SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID: jest_reason,
            SALESFORCE_WORKSPACE_FINGERPRINT_COMMAND_ID: (
                None if python_available else "the configured Python executable is unavailable"
            ),
        }
        if not python_available and availability[SALESFORCE_LWC_JEST_COMMAND_ID] is None:
            # Jest still depends on the Python toolchain-contract check.
            availability[SALESFORCE_LWC_JEST_COMMAND_ID] = (
                "the configured Python executable is unavailable"
            )
            availability[SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID] = (
                "the configured Python executable is unavailable"
            )

        specs: list[CommandSpec] = []
        python = str(self._python_executable)
        python_environment = (
            ("PATH", "/usr/bin:/bin"),
            ("PYTHONHASHSEED", "0"),
            ("PYTHONIOENCODING", "utf-8"),
        )
        if availability[SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID] is None:
            specs.append(
                _python_check_spec(
                    SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID,
                    python,
                    "candidate-contract",
                    candidate_root,
                    python_environment,
                )
            )
            specs.append(
                _python_check_spec(
                    SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID,
                    python,
                    "dependency-closure",
                    candidate_root,
                    python_environment,
                )
            )
            specs.append(
                CommandSpec(
                    command_id=SALESFORCE_WORKSPACE_FINGERPRINT_COMMAND_ID,
                    argv_prefix=(
                        python,
                        "-I",
                        "-B",
                        "-m",
                        "legacy_migration_agent.platforms.local_checks",
                        "workspace-revision",
                        "--expected",
                        initial_fingerprint,
                    ),
                    allowed_working_directories=(candidate_root,),
                    sanitized_environment=python_environment,
                )
            )
        if availability[SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID] is None:
            specs.append(
                CommandSpec(
                    command_id=SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID,
                    argv_prefix=(
                        python,
                        "-I",
                        "-B",
                        "-m",
                        "legacy_migration_agent.platforms.local_checks",
                        "toolchain-contract",
                        "--toolchain-root",
                        str(self._toolchain_root),
                    ),
                    allowed_working_directories=(self._repository_root,),
                    sanitized_environment=python_environment,
                )
            )
        if availability[SALESFORCE_SANDBOX_PROBE_COMMAND_ID] is None:
            if (
                probe_binding is None or self._sandbox_backend is None or self._probe_python is None
            ):  # pragma: no cover - availability rejects it
                raise AssertionError("available sandbox probe has no controller binding")
            specs.append(
                CommandSpec(
                    command_id=SALESFORCE_SANDBOX_PROBE_COMMAND_ID,
                    argv_prefix=(
                        str(self._sandbox_backend),
                        "-p",
                        probe_binding.profile,
                        str(self._probe_python),
                        "-I",
                        "-c",
                        _SANDBOX_PROBE_BOOTSTRAP,
                        probe_binding.challenge,
                        _SANDBOX_PROBE_DIGEST,
                        probe_binding.profile_digest,
                        str(candidate_root / LWC_TEST_PATH),
                        str(self._toolchain_root / "package-lock.json"),
                        str(probe_binding.package_boundary.path),
                        str(probe_binding.package_boundary.device),
                        str(probe_binding.package_boundary.inode),
                        str(probe_binding.package_boundary.mode),
                        str(probe_binding.package_boundary.uid),
                        str(probe_binding.package_boundary.gid),
                        str(probe_binding.package_boundary.size),
                        str(probe_binding.package_boundary.link_count),
                        probe_binding.package_boundary.sha256,
                        str(probe_binding.record_path),
                        str(self._session.state_dir / "runtime.json"),
                        str(self._sandbox_escape_marker),
                    ),
                    allowed_working_directories=(candidate_root,),
                    sanitized_environment=_sandbox_environment(
                        self._scratch_root,
                        self._toolchain_root,
                    ),
                )
            )
        if availability[SALESFORCE_LWC_JEST_COMMAND_ID] is None:
            if (
                probe_binding is None
                or probe_binding.candidate_profile is None
                or self._sandbox_backend is None
                or self._node_executable is None
            ):  # pragma: no cover - availability rejects it
                raise AssertionError("available Jest command has no controller sandbox binding")
            jest_entry = self._toolchain_root / _JEST_ENTRY_RELATIVE
            specs.append(
                CommandSpec(
                    command_id=SALESFORCE_LWC_JEST_COMMAND_ID,
                    argv_prefix=(
                        str(self._sandbox_backend),
                        "-p",
                        probe_binding.candidate_profile,
                        str(self._node_executable),
                        str(jest_entry),
                        "--config",
                        str(self._toolchain_root / "jest.config.js"),
                        "--rootDir",
                        str(candidate_root),
                        "--runInBand",
                        "--no-cache",
                        "--json",
                        "--runTestsByPath",
                        str(candidate_root / LWC_TEST_PATH),
                    ),
                    allowed_working_directories=(candidate_root,),
                    sanitized_environment=_sandbox_environment(
                        self._scratch_root,
                        self._toolchain_root,
                    ),
                )
            )
        if availability[SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID] is None:
            if (
                probe_binding is None
                or probe_binding.candidate_profile is None
                or self._sandbox_backend is None
                or self._node_executable is None
            ):  # pragma: no cover - availability rejects it
                raise AssertionError(
                    "available controller Jest command has no controller sandbox binding"
                )
            jest_entry = self._toolchain_root / _JEST_ENTRY_RELATIVE
            specs.append(
                CommandSpec(
                    command_id=SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID,
                    argv_prefix=(
                        str(self._sandbox_backend),
                        "-p",
                        probe_binding.candidate_profile,
                        str(self._node_executable),
                        str(jest_entry),
                        "--config",
                        str(self._toolchain_root / "jest.config.js"),
                        "--rootDir",
                        str(self._toolchain_root),
                        "--runInBand",
                        "--no-cache",
                        "--json",
                        "--runTestsByPath",
                        str(self._toolchain_root / LWC_CONTROLLER_TEST_PATH),
                    ),
                    allowed_working_directories=(candidate_root,),
                    sanitized_environment=_sandbox_environment(
                        self._scratch_root,
                        self._toolchain_root,
                    ),
                )
            )
        return availability, tuple(specs), probe_binding

    def _sandbox_unavailable_reason(self) -> str | None:
        if self._sandbox_backend is None or self._sandbox_backend != _discover_protected_executable(
            _MACOS_SANDBOX_EXEC
        ):
            return "the protected macOS sandbox-exec backend is unavailable"
        try:
            self._verify_controller_python()
            if self._node_binding is not None:
                self._verify_node_runtime()
        except PolicyViolation:
            return "the bound sandbox probe or Node runtime identity changed"
        if self._sandbox_escape_marker.exists():
            return "the OS sandbox escape canary already exists"
        return None

    def _build_probe_binding(
        self,
        candidate_root: Path,
        initial_fingerprint: str,
        attempt: int,
        change_digest: str,
        package_boundary: _PackageBoundaryBinding,
    ) -> _SandboxProbeBinding:
        backend = self._sandbox_backend
        probe_python = self._probe_python
        if backend is None or probe_python is None:
            raise PolicyViolation("the supported macOS sandbox backend is unavailable")
        _verify_package_boundary(package_boundary, candidate_root)
        profile = _sandbox_profile(
            candidate_root,
            self._toolchain_root,
            self._scratch_root,
            probe_python,
            project_root=self._repository_root,
            private_roots=(self._session.run_dir, self._session.state_dir),
            runtime_roots=self._controller_runtime_roots,
            exact_read_files=(package_boundary.path,),
        )
        node = self._node_executable
        node_runtime_roots = (
            _node_runtime_roots(self._node_binding) if self._node_binding is not None else ()
        )
        candidate_profile = (
            _sandbox_profile(
                candidate_root,
                self._toolchain_root,
                self._scratch_root,
                node,
                project_root=self._repository_root,
                private_roots=(self._session.run_dir, self._session.state_dir),
                runtime_roots=node_runtime_roots,
                exact_read_files=(package_boundary.path,),
            )
            if node is not None
            else None
        )
        profile_digest = _digest_text(profile)
        candidate_profile_digest = (
            _digest_text(candidate_profile) if candidate_profile is not None else None
        )
        backend_digest = _file_sha256(backend)
        probe_python_digest = _file_sha256(probe_python)
        toolchain_fingerprint = _full_tree_fingerprint(self._toolchain_root)
        node_digest = self._node_binding.resolved_sha256 if self._node_binding else None
        node_binding_digest = (
            artifact_digest(_node_binding_payload(self._node_binding))
            if self._node_binding is not None
            else None
        )
        epoch_id = _sandbox_epoch_id(
            run_id=self._session.context.run_id,
            request_digest=self._session.context.request_digest,
            candidate_root=candidate_root,
            attempt=attempt,
            candidate_fingerprint=initial_fingerprint,
            change_digest=change_digest,
            profile_digest=profile_digest,
            candidate_profile_digest=candidate_profile_digest,
            backend_digest=backend_digest,
            probe_python_digest=probe_python_digest,
            toolchain_fingerprint=toolchain_fingerprint,
            node_digest=node_digest,
            node_binding_digest=node_binding_digest,
            package_boundary=package_boundary,
        )
        challenge = epoch_id
        epoch_anchor_kind = f"salesforce-jest-sandbox-epoch-{attempt}-{epoch_id[:24]}"
        record_path = self._scratch_root / (
            f"salesforce-sandbox-probe-{attempt}-{epoch_id[:24]}.json"
        )
        self._require_sandbox_epoch_capacity(epoch_anchor_kind, attempt)
        return _SandboxProbeBinding(
            attempt=attempt,
            epoch_id=epoch_id,
            challenge=challenge,
            profile=profile,
            profile_digest=profile_digest,
            candidate_profile=candidate_profile,
            candidate_profile_digest=candidate_profile_digest,
            backend_digest=backend_digest,
            probe_python_digest=probe_python_digest,
            record_path=record_path,
            outside_write_path=self._sandbox_escape_marker,
            candidate_fingerprint=initial_fingerprint,
            change_digest=change_digest,
            toolchain_fingerprint=toolchain_fingerprint,
            node_digest=node_digest,
            node_binding_digest=node_binding_digest,
            package_boundary=package_boundary,
        )


def build_salesforce_local_validator(
    session: AgentRunSession,
    registry: AgentRegistry,
    *,
    timeout_seconds: float = 120.0,
) -> SalesforceLocalValidator:
    """Bind the supported Salesforce validator to one immutable run session."""

    return SalesforceLocalValidator(
        session,
        registry,
        timeout_seconds=timeout_seconds,
    )


def _python_check_spec(
    command_id: str,
    python: str,
    subcommand: str,
    candidate_root: Path,
    environment: tuple[tuple[str, str], ...],
) -> CommandSpec:
    return CommandSpec(
        command_id=command_id,
        argv_prefix=(
            python,
            "-I",
            "-B",
            "-m",
            "legacy_migration_agent.platforms.local_checks",
            subcommand,
        ),
        allowed_working_directories=(candidate_root,),
        sanitized_environment=environment,
    )


def _working_directory(command_id: str, candidate_root: Path, repository_root: Path) -> Path:
    if command_id == SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID:
        return repository_root
    return candidate_root


def _probe_summary_parser(binding: _SandboxProbeBinding | None) -> _SummaryParser:
    if binding is None:
        raise AssertionError("sandbox probe parser requires a controller binding")

    def parse(execution: ExecutionResult, candidate_root: Path) -> str:
        return _sandbox_probe_summary(execution, candidate_root, binding)

    return parse


def _sandbox_probe_summary(
    execution: ExecutionResult,
    _candidate_root: Path,
    binding: _SandboxProbeBinding,
) -> str:
    value = _json_object(execution.stdout)
    _require_fields(
        value,
        {
            "check": "salesforce-jest-sandbox-probe",
            "passed": True,
            "backend_id": _SANDBOX_BACKEND_ID,
            "challenge": binding.challenge,
            "probe_digest": _SANDBOX_PROBE_DIGEST,
            "profile_digest": binding.profile_digest,
            "candidate_read_allowed": True,
            "toolchain_read_allowed": True,
            "package_boundary_read_allowed": True,
            "package_boundary_verified": True,
            "package_boundary_path": str(binding.package_boundary.path),
            "package_boundary_device": binding.package_boundary.device,
            "package_boundary_inode": binding.package_boundary.inode,
            "package_boundary_mode": binding.package_boundary.mode,
            "package_boundary_uid": binding.package_boundary.uid,
            "package_boundary_gid": binding.package_boundary.gid,
            "package_boundary_size": binding.package_boundary.size,
            "package_boundary_link_count": binding.package_boundary.link_count,
            "package_boundary_sha256": binding.package_boundary.sha256,
            "scratch_write_allowed": True,
            "external_read_denied": True,
            "external_write_denied": True,
            "network_denied": True,
            "child_process_denied": True,
        },
    )
    record = _read_probe_record(binding.record_path)
    if record != value:
        raise ValueError("sandbox probe stdout and controller-observed record differ")
    return (
        "Controller-owned OS sandbox probe passed all nine authority checks; "
        f"challenge={_digest_text(binding.challenge)}; "
        f"probe={_SANDBOX_PROBE_DIGEST}; profile={binding.profile_digest}; "
        f"record={artifact_digest(record)}; stdout={execution.receipt.stdout_digest}."
    )


def _agent_definition_digests(registry: AgentRegistry) -> AgentDefinitionDigests:
    return AgentDefinitionDigests(
        architect=registry.get(AgentRole.ARCHITECT).definition_digest,
        engineer=registry.get(AgentRole.ENGINEER).definition_digest,
        validator=registry.get(AgentRole.VALIDATOR).definition_digest,
    )


def _create_package_boundary(candidate_root: Path) -> _PackageBoundaryBinding:
    """Atomically publish one controller-owned package boundary beside a candidate."""

    safe_candidate = _safe_directory(candidate_root, "candidate workspace")
    parent = _safe_directory(safe_candidate.parent, "candidate workspace container")
    parent_metadata = parent.lstat()
    boundary = parent / _PACKAGE_BOUNDARY_FILENAME
    pending = parent / _PACKAGE_BOUNDARY_PENDING_FILENAME
    parent_descriptor = _open_directory_no_follow(parent, "candidate workspace container")
    file_descriptor: int | None = None
    binding_descriptor: int | None = None
    staged_identity: tuple[int, int] | None = None
    published = False
    try:
        opened_parent = os.fstat(parent_descriptor)
        if (opened_parent.st_dev, opened_parent.st_ino) != (
            parent_metadata.st_dev,
            parent_metadata.st_ino,
        ):
            raise PolicyViolation("candidate workspace container changed during boundary creation")
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or stat.S_IMODE(opened_parent.st_mode) != 0o700
            or opened_parent.st_uid != os.geteuid()
            or opened_parent.st_gid != os.getegid()
        ):
            raise PolicyViolation("candidate workspace container permissions are invalid")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_descriptor = os.open(
                pending.name,
                flags,
                0o400,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise PolicyViolation("package boundary staging path is unavailable") from exc
        remaining = memoryview(_PACKAGE_BOUNDARY_BYTES)
        while remaining:
            written = os.write(file_descriptor, remaining)
            if written <= 0:  # pragma: no cover - defensive short-write guard
                raise PolicyViolation("package boundary could not be written completely")
            remaining = remaining[written:]
        os.fsync(file_descriptor)
        staged = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(staged.st_mode)
            or stat.S_IMODE(staged.st_mode) != 0o400
            or staged.st_size != len(_PACKAGE_BOUNDARY_BYTES)
            or staged.st_uid != os.geteuid()
            or staged.st_gid != os.getegid()
            or staged.st_nlink != 1
        ):
            raise PolicyViolation("package boundary staging identity is invalid")
        staged_identity = staged.st_dev, staged.st_ino
        os.close(file_descriptor)
        file_descriptor = None
        try:
            os.link(
                pending.name,
                boundary.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise PolicyViolation(
                "package boundary already exists or could not be published"
            ) from exc
        published = True
        os.unlink(pending.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)

        boundary_descriptor = _open_regular_at(
            parent_descriptor,
            boundary.name,
            "controller-owned package boundary",
        )
        try:
            metadata = os.fstat(boundary_descriptor)
            content = _read_exact_file_descriptor(
                boundary_descriptor,
                maximum_bytes=len(_PACKAGE_BOUNDARY_BYTES),
            )
            binding_descriptor = os.dup(boundary_descriptor)
            os.set_inheritable(binding_descriptor, False)
        finally:
            os.close(boundary_descriptor)
        if (metadata.st_dev, metadata.st_ino) != staged_identity:
            raise PolicyViolation("package boundary identity changed during publication")
        binding = _PackageBoundaryBinding(
            descriptor=binding_descriptor,
            path=boundary,
            parent_path=parent,
            parent_device=opened_parent.st_dev,
            parent_inode=opened_parent.st_ino,
            parent_mode=opened_parent.st_mode,
            parent_uid=opened_parent.st_uid,
            parent_gid=opened_parent.st_gid,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            uid=metadata.st_uid,
            gid=metadata.st_gid,
            size=metadata.st_size,
            link_count=metadata.st_nlink,
            sha256="sha256:" + hashlib.sha256(content).hexdigest(),
        )
        _verify_package_boundary(binding, safe_candidate)
        binding_descriptor = None
        return binding
    except BaseException:
        if published and staged_identity is not None:
            try:
                actual = os.stat(
                    boundary.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (actual.st_dev, actual.st_ino) == staged_identity:
                    os.unlink(boundary.name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        raise
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if binding_descriptor is not None:
            os.close(binding_descriptor)
        if staged_identity is not None:
            try:
                pending_metadata = os.stat(
                    pending.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                if (pending_metadata.st_dev, pending_metadata.st_ino) == staged_identity:
                    os.unlink(pending.name, dir_fd=parent_descriptor)
        os.close(parent_descriptor)


def _verify_package_boundary(
    binding: _PackageBoundaryBinding,
    candidate_root: Path,
) -> None:
    """Verify the exact adjacent file, its bytes, and its captured stat identity."""

    expected_parent = Path(os.path.abspath(candidate_root)).parent
    expected_path = expected_parent / _PACKAGE_BOUNDARY_FILENAME
    if binding.parent_path != expected_parent or binding.path != expected_path:
        raise PolicyViolation("controller-owned package boundary path changed")
    try:
        pinned_before = os.fstat(binding.descriptor)
        os.lseek(binding.descriptor, 0, os.SEEK_SET)
        pinned_content = _read_exact_file_descriptor(
            binding.descriptor,
            maximum_bytes=len(_PACKAGE_BOUNDARY_BYTES),
        )
        pinned_after = os.fstat(binding.descriptor)
    except OSError as exc:
        raise PolicyViolation("controller-owned package boundary descriptor changed") from exc
    pinned_stability = (
        pinned_before.st_dev,
        pinned_before.st_ino,
        pinned_before.st_mode,
        pinned_before.st_uid,
        pinned_before.st_gid,
        pinned_before.st_size,
        pinned_before.st_nlink,
        pinned_before.st_mtime_ns,
        pinned_before.st_ctime_ns,
    )
    if pinned_stability != (
        pinned_after.st_dev,
        pinned_after.st_ino,
        pinned_after.st_mode,
        pinned_after.st_uid,
        pinned_after.st_gid,
        pinned_after.st_size,
        pinned_after.st_nlink,
        pinned_after.st_mtime_ns,
        pinned_after.st_ctime_ns,
    ):
        raise PolicyViolation("controller-owned package boundary changed while being read")
    parent_descriptor = _open_directory_no_follow(
        binding.parent_path,
        "candidate workspace container",
    )
    try:
        parent_metadata = os.fstat(parent_descriptor)
        if (
            (
                parent_metadata.st_dev,
                parent_metadata.st_ino,
                parent_metadata.st_mode,
                parent_metadata.st_uid,
                parent_metadata.st_gid,
            )
            != (
                binding.parent_device,
                binding.parent_inode,
                binding.parent_mode,
                binding.parent_uid,
                binding.parent_gid,
            )
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or parent_metadata.st_uid != os.geteuid()
            or parent_metadata.st_gid != os.getegid()
        ):
            raise PolicyViolation("controller-owned package boundary parent changed")
        boundary_descriptor = _open_regular_at(
            parent_descriptor,
            binding.path.name,
            "controller-owned package boundary",
        )
        try:
            metadata = os.fstat(boundary_descriptor)
            content = _read_exact_file_descriptor(
                boundary_descriptor,
                maximum_bytes=len(_PACKAGE_BOUNDARY_BYTES),
            )
        finally:
            os.close(boundary_descriptor)
    finally:
        os.close(parent_descriptor)
    pinned_observed = (
        pinned_before.st_dev,
        pinned_before.st_ino,
        pinned_before.st_mode,
        pinned_before.st_uid,
        pinned_before.st_gid,
        pinned_before.st_size,
        pinned_before.st_nlink,
    )
    path_observed = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_nlink,
    )
    expected = (
        binding.device,
        binding.inode,
        binding.mode,
        binding.uid,
        binding.gid,
        binding.size,
        binding.link_count,
    )
    pinned_digest = "sha256:" + hashlib.sha256(pinned_content).hexdigest()
    path_digest = "sha256:" + hashlib.sha256(content).hexdigest()
    if (
        pinned_observed != expected
        or path_observed != expected
        or stat.S_IMODE(metadata.st_mode) != 0o400
        or metadata.st_nlink != 1
        or pinned_content != _PACKAGE_BOUNDARY_BYTES
        or content != _PACKAGE_BOUNDARY_BYTES
        or pinned_digest != binding.sha256
        or path_digest != binding.sha256
        or pinned_digest != _PACKAGE_BOUNDARY_SHA256
        or path_digest != _PACKAGE_BOUNDARY_SHA256
    ):
        raise PolicyViolation("controller-owned package boundary changed")


def _remove_package_boundary(binding: _PackageBoundaryBinding) -> None:
    """Remove one bound leaf and always release its pinned descriptor."""

    try:
        _remove_package_boundary_leaf(binding)
    finally:
        try:
            os.close(binding.descriptor)
        except OSError as exc:
            raise PolicyViolation(
                "controller-owned package boundary descriptor could not be released"
            ) from exc


def _remove_package_boundary_leaf(binding: _PackageBoundaryBinding) -> None:
    """Remove exactly the bound leaf and report any pre-cleanup drift."""

    drift: BaseException | None = None
    try:
        _verify_package_boundary(binding, binding.parent_path / "repository")
    except BaseException as exc:
        drift = exc

    parent_descriptor = _open_directory_no_follow(
        binding.parent_path,
        "candidate workspace container",
    )
    cleanup_error: BaseException | None = None
    try:
        parent_metadata = os.fstat(parent_descriptor)
        if (
            parent_metadata.st_dev,
            parent_metadata.st_ino,
            parent_metadata.st_mode,
            parent_metadata.st_uid,
            parent_metadata.st_gid,
        ) != (
            binding.parent_device,
            binding.parent_inode,
            binding.parent_mode,
            binding.parent_uid,
            binding.parent_gid,
        ):
            cleanup_error = PolicyViolation(
                "controller-owned package boundary parent changed before cleanup"
            )
        else:
            try:
                metadata = os.stat(
                    binding.path.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                metadata = None
            if metadata is not None:
                if (metadata.st_dev, metadata.st_ino) == (binding.device, binding.inode):
                    os.unlink(binding.path.name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
                else:
                    cleanup_error = PolicyViolation(
                        "controller-owned package boundary leaf was substituted"
                    )
            if cleanup_error is None:
                try:
                    os.stat(
                        binding.path.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    cleanup_error = PolicyViolation(
                        "controller-owned package boundary could not be removed"
                    )
    finally:
        os.close(parent_descriptor)

    if cleanup_error is not None:
        raise cleanup_error
    if drift is not None:
        raise PolicyViolation("controller-owned package boundary drifted before cleanup") from drift


def _open_directory_no_follow(path: Path, role: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PolicyViolation(f"{role} could not be opened safely") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise PolicyViolation(f"{role} is not a directory")
    return descriptor


def _open_regular_at(parent_descriptor: int, name: str, role: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise PolicyViolation(f"{role} could not be opened safely") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise PolicyViolation(f"{role} is not a regular file")
    return descriptor


def _read_exact_file_descriptor(descriptor: int, *, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := os.read(descriptor, 4096):
        total += len(chunk)
        if total > maximum_bytes:
            raise PolicyViolation("controller-owned package boundary is oversized")
        chunks.append(chunk)
    return b"".join(chunks)


def _sandbox_environment(
    scratch_root: Path,
    toolchain_root: Path,
) -> tuple[tuple[str, str], ...]:
    return (
        ("CI", "true"),
        ("FORCE_COLOR", "0"),
        ("HOME", str(scratch_root)),
        ("NODE_ENV", "test"),
        ("NODE_PATH", str(toolchain_root / "node_modules")),
        ("NO_COLOR", "1"),
        ("PATH", "/usr/bin:/bin"),
        ("SF_DISABLE_TELEMETRY", "true"),
        ("TMPDIR", str(scratch_root)),
        ("XDG_CACHE_HOME", str(scratch_root)),
    )


def _sandbox_profile(
    candidate_root: Path,
    toolchain_root: Path,
    scratch_root: Path,
    executable: Path,
    *,
    project_root: Path,
    private_roots: tuple[Path, ...],
    runtime_roots: tuple[Path, ...],
    exact_read_files: tuple[Path, ...] = (),
) -> str:
    """Return the controller-owned macOS policy for one resolved runtime.

    ``allow default`` is required for macOS runtime bootstrap services.  The
    policy therefore does not claim whole-host read confinement: it explicitly
    removes read authority for user and run-private data, then restores only
    the candidate, pinned toolchain, scratch, and bound runtime roots.  Writes,
    networking, forks, and executable selection remain explicit-deny surfaces.
    """

    def literal(path: Path) -> str:
        return json.dumps(str(path), ensure_ascii=True)

    denied_read_roots = _unique_paths(
        (
            Path("/Users"),
            project_root,
            *private_roots,
        )
    )
    readable_roots = _unique_paths(
        (
            candidate_root,
            toolchain_root,
            scratch_root,
            *runtime_roots,
        )
    )
    deny_read_rules = " ".join(f"(subpath {literal(path)})" for path in denied_read_roots)
    read_rules = " ".join(f"(subpath {literal(path)})" for path in readable_roots)
    exact_read_rules = " ".join(
        f"(literal {literal(path)})" for path in _unique_paths(exact_read_files)
    )
    traversal_rules = " ".join(
        f"(literal {literal(path)})"
        for path in _path_traversal_literals(
            (*readable_roots, *_unique_paths(exact_read_files), executable)
        )
    )
    return " ".join(
        (
            "(version 1)",
            "(allow default)",
            f"(deny file-read* {deny_read_rules})",
            f"(allow file-read-metadata {traversal_rules})",
            (f"(allow file-read* {read_rules} (literal {literal(executable)}) {exact_read_rules})"),
            "(deny file-write*)",
            f"(allow file-write* (subpath {literal(scratch_root)}))",
            "(deny network*)",
            "(deny process-fork)",
            "(deny process-exec)",
            f"(allow process-exec (literal {literal(executable)}))",
        )
    )


def _unique_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    return tuple(Path(path) for path in dict.fromkeys(map(str, paths)))


def _path_traversal_literals(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    traversal: list[Path] = []
    for path in paths:
        absolute = Path(os.path.abspath(path))
        for parent in reversed(absolute.parents):
            if parent not in traversal:
                traversal.append(parent)
        if absolute not in traversal:
            traversal.append(absolute)
    return tuple(traversal)


def _digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sandbox_epoch_id(
    *,
    run_id: str,
    request_digest: str,
    candidate_root: Path,
    attempt: int,
    candidate_fingerprint: str,
    change_digest: str,
    profile_digest: str,
    candidate_profile_digest: str | None,
    backend_digest: str,
    probe_python_digest: str,
    toolchain_fingerprint: str,
    node_digest: str | None,
    node_binding_digest: str | None,
    package_boundary: _PackageBoundaryBinding,
) -> str:
    """Identify one exact, non-reusable validator workspace epoch."""

    material = "\x00".join(
        (
            _PACKAGE_BOUNDARY_POLICY_ID,
            run_id,
            request_digest,
            str(attempt),
            str(Path(os.path.abspath(candidate_root))),
            candidate_fingerprint,
            change_digest,
            profile_digest,
            candidate_profile_digest or "",
            backend_digest,
            probe_python_digest,
            toolchain_fingerprint,
            node_digest or "",
            node_binding_digest or "",
            str(package_boundary.path),
            str(package_boundary.parent_device),
            str(package_boundary.parent_inode),
            str(package_boundary.parent_mode),
            str(package_boundary.parent_uid),
            str(package_boundary.parent_gid),
            str(package_boundary.device),
            str(package_boundary.inode),
            str(package_boundary.mode),
            str(package_boundary.uid),
            str(package_boundary.gid),
            str(package_boundary.size),
            str(package_boundary.link_count),
            package_boundary.sha256,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _read_probe_record(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError("sandbox probe record is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("sandbox probe record is not a regular file")
    if metadata.st_size < 2 or metadata.st_size > 16 * 1024:
        raise ValueError("sandbox probe record size is invalid")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("sandbox probe record is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("sandbox probe record must be a JSON object")
    return cast(dict[str, Any], value)


def _require_changes_match(changes: WorkspaceChanges, change_set: ChangeSet) -> None:
    if changes.deleted_paths:
        raise PolicyViolation("Salesforce migration cannot delete candidate files")
    if changes.changed_paths != change_set.changed_paths:
        raise PolicyViolation("candidate workspace paths do not match the change set")
    if changes.unified_diff.strip() != change_set.unified_diff.strip():
        raise PolicyViolation("candidate workspace diff does not match the change set")


def _report_id(run_id: str, request_id: str, attempt: int) -> str:
    material = f"{run_id}\x00{request_id}\x00{attempt}".encode()
    return f"report-salesforce-{hashlib.sha256(material).hexdigest()[:24]}"


def _capture_controller_python(path: Path) -> _ControllerPythonBinding:
    if not path.is_absolute():
        raise PolicyViolation("the controller Python executable path is not absolute")
    lexical = Path(os.path.abspath(path))
    try:
        lexical_metadata = lexical.lstat()
        if stat.S_ISLNK(lexical_metadata.st_mode):
            link_target: str | None = os.readlink(lexical)
        elif stat.S_ISREG(lexical_metadata.st_mode):
            link_target = None
        else:
            raise PolicyViolation(
                "the controller Python executable is not a regular file or symlink"
            )
        resolved = lexical.resolve(strict=True)
        resolved_metadata = resolved.stat()
    except PolicyViolation:
        raise
    except OSError as exc:
        raise PolicyViolation("the controller Python executable is unavailable") from exc
    if not stat.S_ISREG(resolved_metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise PolicyViolation("the controller Python target is not executable")
    return _ControllerPythonBinding(
        lexical_path=str(lexical),
        lexical_device=lexical_metadata.st_dev,
        lexical_inode=lexical_metadata.st_ino,
        lexical_mode=lexical_metadata.st_mode,
        lexical_uid=lexical_metadata.st_uid,
        link_target=link_target,
        resolved_path=str(resolved),
        resolved_device=resolved_metadata.st_dev,
        resolved_inode=resolved_metadata.st_ino,
        resolved_mode=resolved_metadata.st_mode,
        resolved_uid=resolved_metadata.st_uid,
        resolved_sha256=_file_sha256(resolved),
    )


def _controller_python_payload(binding: _ControllerPythonBinding) -> dict[str, object]:
    return {
        "lexical_path": binding.lexical_path,
        "lexical_device": binding.lexical_device,
        "lexical_inode": binding.lexical_inode,
        "lexical_mode": binding.lexical_mode,
        "lexical_uid": binding.lexical_uid,
        "link_target": binding.link_target,
        "resolved_path": binding.resolved_path,
        "resolved_device": binding.resolved_device,
        "resolved_inode": binding.resolved_inode,
        "resolved_mode": binding.resolved_mode,
        "resolved_uid": binding.resolved_uid,
        "resolved_sha256": binding.resolved_sha256,
    }


def _controller_runtime_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for raw in (sys.base_prefix, sys.prefix):
        path = Path(raw)
        if not path.is_absolute():
            raise PolicyViolation("the controller Python runtime prefix is not absolute")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise PolicyViolation("the controller Python runtime prefix is unavailable") from exc
        if not resolved.is_dir():
            raise PolicyViolation("the controller Python runtime prefix is not a directory")
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _executable_available(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and os.access(resolved, os.X_OK)


def _discover_protected_executable(path: Path) -> Path | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not os.access(path, os.X_OK)
    ):
        return None
    return path


def _discover_supported_node() -> _NodeExecutableBinding | None:
    for lexical in _SUPPORTED_NODE_PATHS:
        try:
            lexical_metadata = lexical.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PolicyViolation("the supported Node executable is unavailable") from exc
        if stat.S_ISREG(lexical_metadata.st_mode):
            if _discover_protected_executable(lexical) is None:
                raise PolicyViolation("the protected system Node executable is invalid")
            return _capture_node_binding(lexical, cellar_root=None)
        if not stat.S_ISLNK(lexical_metadata.st_mode):
            raise PolicyViolation("the supported Node executable has an invalid file type")
        cellar_root = _HOMEBREW_NODE_CELLARS.get(lexical)
        if cellar_root is None:  # pragma: no cover - constants are controller-owned
            raise PolicyViolation("the supported Node path has no target policy")
        return _capture_node_binding(lexical, cellar_root=cellar_root)
    return None


def _capture_node_binding(
    path: Path,
    *,
    cellar_root: Path | None,
) -> _NodeExecutableBinding:
    if not path.is_absolute():
        raise PolicyViolation("the Node executable path is not absolute")
    lexical = Path(os.path.abspath(path))
    try:
        lexical_metadata = lexical.lstat()
        is_link = stat.S_ISLNK(lexical_metadata.st_mode)
        if cellar_root is None:
            if is_link or not stat.S_ISREG(lexical_metadata.st_mode):
                raise PolicyViolation("the system Node executable is not a regular file")
            link_target: str | None = None
        else:
            if not is_link:
                raise PolicyViolation("the Homebrew Node executable is not a symlink")
            link_target = os.readlink(lexical)
        resolved = lexical.resolve(strict=True)
        resolved_metadata = resolved.stat()
    except PolicyViolation:
        raise
    except OSError as exc:
        raise PolicyViolation("the Node executable is unavailable") from exc

    if cellar_root is not None:
        try:
            resolved.relative_to(cellar_root)
        except ValueError as exc:
            raise PolicyViolation("the Homebrew Node symlink target is outside its Cellar") from exc
    if not stat.S_ISREG(resolved_metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise PolicyViolation("the Node target is not a regular executable")
    _require_trusted_node_metadata(resolved, resolved_metadata, is_symlink=False)
    components = _capture_trusted_node_components(lexical, resolved)
    return _NodeExecutableBinding(
        lexical_path=str(lexical),
        lexical_device=lexical_metadata.st_dev,
        lexical_inode=lexical_metadata.st_ino,
        lexical_mode=lexical_metadata.st_mode,
        lexical_uid=lexical_metadata.st_uid,
        lexical_gid=lexical_metadata.st_gid,
        link_target=link_target,
        resolved_path=str(resolved),
        resolved_device=resolved_metadata.st_dev,
        resolved_inode=resolved_metadata.st_ino,
        resolved_mode=resolved_metadata.st_mode,
        resolved_uid=resolved_metadata.st_uid,
        resolved_gid=resolved_metadata.st_gid,
        resolved_sha256=_file_sha256(resolved),
        path_components=components,
    )


def _capture_trusted_node_components(
    lexical: Path,
    resolved: Path,
) -> tuple[_ExecutablePathComponent, ...]:
    captured: dict[str, _ExecutablePathComponent] = {}
    for leaf in (lexical, resolved):
        current = Path(leaf.anchor)
        for part in leaf.parts[1:]:
            current /= part
            try:
                metadata = current.lstat()
                is_link = stat.S_ISLNK(metadata.st_mode)
                link_target = os.readlink(current) if is_link else None
            except OSError as exc:
                raise PolicyViolation("a Node executable path component is unavailable") from exc
            _require_trusted_node_metadata(current, metadata, is_symlink=is_link)
            captured[str(current)] = _ExecutablePathComponent(
                path=str(current),
                device=metadata.st_dev,
                inode=metadata.st_ino,
                mode=metadata.st_mode,
                uid=metadata.st_uid,
                gid=metadata.st_gid,
                link_target=link_target,
            )
    return tuple(captured[path] for path in sorted(captured))


def _require_trusted_node_metadata(
    path: Path,
    metadata: os.stat_result,
    *,
    is_symlink: bool,
) -> None:
    if metadata.st_uid not in {0, os.geteuid()}:
        raise PolicyViolation(f"the Node executable path has an untrusted owner: {path}")
    # Symlink permission bits are not authorization bits on macOS.  Every
    # containing directory and the resolved regular leaf is still checked.
    if not is_symlink and stat.S_IMODE(metadata.st_mode) & 0o002:
        raise PolicyViolation(f"the Node executable path is world-writable: {path}")


def _node_binding_payload(binding: _NodeExecutableBinding) -> dict[str, object]:
    return {
        "lexical_path": binding.lexical_path,
        "lexical_device": binding.lexical_device,
        "lexical_inode": binding.lexical_inode,
        "lexical_mode": binding.lexical_mode,
        "lexical_uid": binding.lexical_uid,
        "lexical_gid": binding.lexical_gid,
        "link_target": binding.link_target,
        "resolved_path": binding.resolved_path,
        "resolved_device": binding.resolved_device,
        "resolved_inode": binding.resolved_inode,
        "resolved_mode": binding.resolved_mode,
        "resolved_uid": binding.resolved_uid,
        "resolved_gid": binding.resolved_gid,
        "resolved_sha256": binding.resolved_sha256,
        "path_components": tuple(
            {
                "path": component.path,
                "device": component.device,
                "inode": component.inode,
                "mode": component.mode,
                "uid": component.uid,
                "gid": component.gid,
                "link_target": component.link_target,
            }
            for component in binding.path_components
        ),
    }


def _node_runtime_roots(binding: _NodeExecutableBinding) -> tuple[Path, ...]:
    return _unique_paths(
        (
            Path(binding.lexical_path).parent,
            Path(binding.resolved_path).parent,
        )
    )


def _file_sha256(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PolicyViolation("attested executable could not be opened safely") from exc
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PolicyViolation("attested executable is not a regular file")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return f"sha256:{digest.hexdigest()}"


def _full_tree_fingerprint(root: Path) -> str:
    """Hash every toolchain directory, file byte, mode, and confined link."""

    safe_root = _safe_directory(root, "attested Jest toolchain")
    _reject_oracle_path(safe_root, "attested Jest toolchain")
    digest = hashlib.sha256()
    entries = 0

    def frame(kind: bytes, relative: str, mode: int, payload: bytes = b"") -> None:
        nonlocal entries
        entries += 1
        if entries > 250_000:
            raise PolicyViolation("attested Jest toolchain contains too many entries")
        path_bytes = relative.encode("utf-8")
        digest.update(kind)
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(mode.to_bytes(4, "big"))
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    def walk(directory: Path, relative_directory: str) -> None:
        try:
            with os.scandir(directory) as children:
                ordered = tuple(sorted(children, key=lambda child: child.name))
        except OSError as exc:
            raise PolicyViolation("attested Jest toolchain could not be inventoried") from exc
        for child in ordered:
            relative = f"{relative_directory}/{child.name}" if relative_directory else child.name
            if any(part.casefold() in _ORACLE_SEGMENTS for part in relative.split("/")):
                raise PolicyViolation("attested Jest toolchain contains oracle-named content")
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise PolicyViolation("attested Jest toolchain entry became unavailable") from exc
            mode = stat.S_IMODE(metadata.st_mode)
            child_path = Path(child.path)
            if stat.S_ISDIR(metadata.st_mode):
                frame(b"d", relative, mode)
                walk(child_path, relative)
                continue
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    target_text = os.readlink(child.path)
                    target = child_path.resolve(strict=True)
                    target.relative_to(safe_root)
                except (OSError, ValueError) as exc:
                    raise PolicyViolation("attested Jest toolchain link escapes its root") from exc
                frame(b"l", relative, mode, os.fsencode(target_text))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise PolicyViolation("attested Jest toolchain contains a special file")
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(child.path, flags)
            except OSError as exc:
                raise PolicyViolation("attested Jest toolchain file could not be opened") from exc
            file_digest = hashlib.sha256()
            size = 0
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                ) != (metadata.st_dev, metadata.st_ino):
                    raise PolicyViolation("attested Jest toolchain changed during hashing")
                while chunk := os.read(descriptor, 1024 * 1024):
                    size += len(chunk)
                    file_digest.update(chunk)
            finally:
                os.close(descriptor)
            payload = size.to_bytes(8, "big") + file_digest.digest()
            frame(b"f", relative, mode, payload)

    walk(safe_root, "")
    return f"sha256:{digest.hexdigest()}"


def _toolchain_contract_available(repository_root: Path, root: Path) -> bool:
    try:
        safe_root = _safe_descendant_directory(repository_root, root, "Jest toolchain")
        for filename in _TOOLCHAIN_FILES:
            _safe_descendant_regular_file(
                repository_root,
                safe_root / filename,
                "Jest toolchain file",
            )
    except PolicyViolation:
        return False
    return True


def _jest_unavailable_reason(
    node_executable: Path | None,
    repository_root: Path,
    toolchain_root: Path,
    scratch_root: Path,
) -> str | None:
    if not _executable_available(node_executable):
        return "no supported protected Node executable is available"
    try:
        safe_toolchain = _safe_descendant_directory(
            repository_root,
            toolchain_root,
            "Jest toolchain",
        )
        _safe_descendant_regular_file(
            repository_root,
            safe_toolchain / "jest.config.js",
            "Jest configuration",
        )
        _safe_descendant_directory(
            repository_root,
            safe_toolchain / "node_modules",
            "Jest node_modules",
        )
        for relative in _INSTALLED_TOOLCHAIN_FILES:
            _safe_descendant_regular_file(
                repository_root,
                safe_toolchain / relative,
                "installed Jest toolchain file",
            )
        sfdx_package = _read_bounded_json_object(
            safe_toolchain / "node_modules/@salesforce/sfdx-lwc-jest/package.json"
        )
        jest_package = _read_bounded_json_object(safe_toolchain / "node_modules/jest/package.json")
        if (
            sfdx_package.get("name") != "@salesforce/sfdx-lwc-jest"
            or sfdx_package.get("version") != LWC_JEST_VERSION
            or jest_package.get("name") != "jest"
        ):
            raise PolicyViolation("installed Jest packages do not match the pinned toolchain")
        if (
            _full_tree_fingerprint(safe_toolchain / "node_modules")
            != _PINNED_NODE_MODULES_TREE_FINGERPRINT
        ):
            raise PolicyViolation(
                "installed Jest dependencies do not match their controller identity"
            )
        _safe_directory(scratch_root, "session scratch directory")
    except PolicyViolation:
        return "the pinned Jest toolchain or its installed node_modules is unavailable"
    return None


def _read_bounded_json_object(path: Path) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise PolicyViolation("installed Jest package metadata is unavailable") from exc
    if len(content) > 64 * 1024 or b"\x00" in content:
        raise PolicyViolation("installed Jest package metadata is invalid")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("installed Jest package metadata is invalid") from exc
    if not isinstance(value, dict):
        raise PolicyViolation("installed Jest package metadata is invalid")
    return cast(dict[str, Any], value)


def _safe_directory(path: Path, role: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PolicyViolation(f"{role} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation(f"{role} must be a non-symlink directory")
    return path.resolve(strict=True)


def _safe_descendant_directory(root: Path, path: Path, role: str) -> Path:
    lexical = Path(os.path.abspath(path))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise PolicyViolation(f"{role} escapes the project root") from exc
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:  # pragma: no cover - resolved tree vanished concurrently
            raise PolicyViolation(f"{role} became unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PolicyViolation(f"{role} contains a symlink component")
        if not stat.S_ISDIR(metadata.st_mode):
            raise PolicyViolation(f"{role} must be a directory")
    return lexical.resolve(strict=True)


def _safe_descendant_regular_file(root: Path, path: Path, role: str) -> Path:
    lexical = Path(os.path.abspath(path))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise PolicyViolation(f"{role} escapes the project root") from exc
    if not relative.parts:
        raise PolicyViolation(f"{role} must be a regular file")
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise PolicyViolation(f"{role} is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PolicyViolation(f"{role} contains a symlink component")
        is_leaf = index == len(relative.parts) - 1
        if is_leaf and not stat.S_ISREG(metadata.st_mode):
            raise PolicyViolation(f"{role} must be a regular file")
        if not is_leaf and not stat.S_ISDIR(metadata.st_mode):
            raise PolicyViolation(f"{role} parent must be a directory")
    return lexical.resolve(strict=True)


def _reject_oracle_path(path: Path, role: str) -> None:
    if any(part.casefold() in _ORACLE_SEGMENTS for part in path.parts):
        raise PolicyViolation(f"{role} cannot use expected, golden, or oracle content")


__all__ = [
    "SALESFORCE_AGENT_OUTPUT_PATHS",
    "SALESFORCE_ANALYZER_VERSION",
    "SALESFORCE_API_RUNTIME",
    "SALESFORCE_CANDIDATE_CONTRACT_COMMAND_ID",
    "SALESFORCE_CANDIDATE_JEST_EXECUTION_FAILURE_DIAGNOSTIC_ID",
    "SALESFORCE_CONTROLLER_LWC_JEST_COMMAND_ID",
    "SALESFORCE_DEPENDENCY_CLOSURE_COMMAND_ID",
    "SALESFORCE_LWC_JEST_COMMAND_ID",
    "SALESFORCE_MIN_CANDIDATE_LWC_JEST_TESTS",
    "SALESFORCE_PLATFORM_ADAPTER",
    "SALESFORCE_RUNTIME_CONFIG",
    "SALESFORCE_SANDBOX_PROBE_COMMAND_ID",
    "SALESFORCE_SCOPE_POLICY",
    "SALESFORCE_SOURCE_ENTRY",
    "SALESFORCE_TARGET_RUNTIME",
    "SALESFORCE_TOOLCHAIN_CONTRACT_COMMAND_ID",
    "SALESFORCE_TRANSFORMATION_INPUT_PATHS",
    "SALESFORCE_VALIDATION_COMMAND_IDS",
    "SALESFORCE_WORKSPACE_FINGERPRINT_COMMAND_ID",
    "SalesforceLocalValidator",
    "build_salesforce_local_validator",
]
