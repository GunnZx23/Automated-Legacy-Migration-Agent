"""Deterministic policies that constrain agent behavior."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from legacy_migration_agent.contracts import (
    ApprovalAction,
    ChangeSet,
    EnvironmentKind,
    ManifestStatus,
    MigrationManifest,
    MigrationRequest,
    ValidationReport,
    validate_relative_path,
)


class PolicyViolation(RuntimeError):
    """Raised when a requested action violates an enforceable policy."""


def validate_manifest_for_request(manifest: MigrationManifest, request: MigrationRequest) -> None:
    if manifest.request_id != request.request_id:
        raise PolicyViolation("manifest request_id does not match the migration request")
    if manifest.platform is not request.platform:
        raise PolicyViolation("manifest platform does not match the migration request")
    if manifest.base_revision != request.base_revision:
        raise PolicyViolation("manifest is stale for the requested repository revision")
    if manifest.status is ManifestStatus.DECISION_REQUIRED:
        raise PolicyViolation("manifest requires a human decision before implementation")
    approved = set(manifest.approved_paths)
    outputs = {
        path for transformation in manifest.transformations for path in transformation.output_paths
    }
    unapproved_outputs = sorted(outputs - approved)
    if unapproved_outputs:
        raise PolicyViolation(
            "manifest transformations produce paths outside the approved scope: "
            + ", ".join(unapproved_outputs)
        )
    unexplained_paths = sorted(approved - outputs)
    if unexplained_paths:
        raise PolicyViolation(
            "manifest approved paths are not explained by a transformation: "
            + ", ".join(unexplained_paths)
        )


def validate_change_set(change_set: ChangeSet, manifest: MigrationManifest) -> None:
    if change_set.request_id != manifest.request_id:
        raise PolicyViolation("change set request_id does not match the manifest")
    if change_set.manifest_id != manifest.manifest_id:
        raise PolicyViolation("change set was produced for a different manifest")
    if change_set.base_revision != manifest.base_revision:
        raise PolicyViolation("change set is stale for the manifest revision")
    approved = set(manifest.approved_paths)
    changed = set(change_set.changed_paths)
    disallowed = sorted(changed - approved)
    if disallowed:
        raise PolicyViolation(
            f"change set contains paths outside the manifest: {', '.join(disallowed)}"
        )


def validate_report(
    report: ValidationReport,
    manifest: MigrationManifest,
    change_set: ChangeSet,
) -> None:
    # Import lazily to avoid the integrity -> policies import dependency at
    # module initialization while still using the one canonical artifact hash.
    from legacy_migration_agent.core.integrity import artifact_digest

    if report.request_id != manifest.request_id:
        raise PolicyViolation("validation report request_id does not match the manifest")
    if report.manifest_id != manifest.manifest_id:
        raise PolicyViolation("validation report manifest_id does not match the manifest")
    if report.change_set_id != change_set.change_set_id:
        raise PolicyViolation("validation report change_set_id does not match the change set")
    if report.base_revision != manifest.base_revision:
        raise PolicyViolation("validation report is stale for the manifest revision")
    planned_by_id = {check.check_id: check for check in manifest.validation_plan}
    reported_by_id = {result.check_id: result for result in report.results}
    if len(reported_by_id) != len(report.results):
        raise PolicyViolation("validation report contains duplicate check results")
    missing = sorted(set(planned_by_id) - set(reported_by_id))
    if missing:
        raise PolicyViolation(f"validation report omits planned checks: {', '.join(missing)}")
    extra = sorted(set(reported_by_id) - set(planned_by_id))
    if extra:
        raise PolicyViolation(f"validation report contains unplanned checks: {', '.join(extra)}")
    for check_id, planned in planned_by_id.items():
        result = reported_by_id[check_id]
        if result.command_id != planned.command_id:
            raise PolicyViolation(f"validation result {check_id} uses the wrong command_id")
        if result.required is not planned.required:
            raise PolicyViolation(f"validation result {check_id} changes the planned required flag")
        if result.receipt is None:
            continue
        receipt = result.receipt
        if receipt.tool_id != planned.command_id:
            raise PolicyViolation(f"validation result {check_id} has a receipt from the wrong tool")
        if receipt.request_id != report.request_id:
            raise PolicyViolation(f"validation result {check_id} has a receipt for another request")
        if receipt.base_revision != report.base_revision:
            raise PolicyViolation(f"validation result {check_id} has a stale receipt")
        if receipt.environment is not planned.environment:
            raise PolicyViolation(f"validation result {check_id} ran in the wrong environment")
        if receipt.attempt != report.attempt:
            raise PolicyViolation(
                f"validation result {check_id} has a receipt from another attempt"
            )
        if receipt.input_artifact_digest != artifact_digest(change_set):
            raise PolicyViolation(
                f"validation result {check_id} is not bound to the exact change set"
            )

    receipt_run_ids = {
        result.receipt.run_id for result in report.results if result.receipt is not None
    }
    if len(receipt_run_ids) > 1:
        raise PolicyViolation("validation receipts belong to different workflow runs")


@dataclass(frozen=True)
class ExecutionApproval:
    """Human authorization bound to exactly one execution context.

    A nonce is optional, but when supplied the runner consumes it before the
    process is spawned and rejects every later reuse by that runner instance.
    """

    decision_id: str
    request_id: str
    action: ApprovalAction
    artifact_digest: str
    base_revision: str
    command_id: str
    environment: EnvironmentKind
    reviewer: str
    nonce: str | None = None

    def __post_init__(self) -> None:
        for name in ("decision_id", "request_id", "base_revision", "command_id", "reviewer"):
            value = getattr(self, name)
            if not value or _has_forbidden_control(value):
                raise ValueError(f"{name} must be non-empty and cannot contain control characters")
        if not _is_sha256_digest(self.artifact_digest):
            raise ValueError("artifact_digest must be a sha256 digest")
        if self.nonce is not None and (not self.nonce or _has_forbidden_control(self.nonce)):
            raise ValueError("nonce must be non-empty and cannot contain control characters")


@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    argv_prefix: tuple[str, ...]
    allowed_working_directories: tuple[Path, ...]
    allowed_argument_vectors: tuple[tuple[str, ...], ...] = ((),)
    sanitized_environment: tuple[tuple[str, str], ...] = ()
    allowed_environments: tuple[EnvironmentKind, ...] = (EnvironmentKind.LOCAL,)
    artifact_paths: tuple[str, ...] = ()
    requires_approval: ApprovalAction | None = None

    def __post_init__(self) -> None:
        if not self.command_id or _has_forbidden_control(self.command_id):
            raise ValueError("command_id must be non-empty and cannot contain control characters")
        if not self.argv_prefix:
            raise ValueError("argv_prefix must contain an executable")
        if not self.allowed_working_directories:
            raise ValueError("at least one working directory must be allowed")
        if not self.allowed_argument_vectors:
            raise ValueError("at least one exact argument vector must be allowed")
        if not self.allowed_environments:
            raise ValueError("at least one execution environment must be allowed")
        static_values = (
            *self.argv_prefix,
            *(arg for vector in self.allowed_argument_vectors for arg in vector),
        )
        if any(_has_forbidden_control(value) for value in static_values):
            raise ValueError("command values cannot contain NUL or line-break characters")
        environment_names = tuple(name for name, _ in self.sanitized_environment)
        if len(environment_names) != len(set(environment_names)):
            raise ValueError("sanitized environment names must be unique")
        for name, value in self.sanitized_environment:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid environment variable name: {name}")
            if _has_forbidden_control(value):
                raise ValueError(
                    f"environment variable {name} contains a forbidden control character"
                )
        normalized_artifacts = tuple(validate_relative_path(path) for path in self.artifact_paths)
        if len(normalized_artifacts) != len(set(normalized_artifacts)):
            raise ValueError("artifact_paths must be unique")

    def environment_map(self) -> dict[str, str]:
        """Return a fresh exact environment; the host environment is excluded."""

        return dict(self.sanitized_environment)


@dataclass(frozen=True)
class ResolvedCommand:
    """Immutable, fully policy-resolved command safe to hand to the runner."""

    spec: CommandSpec
    argv: tuple[str, ...]


class CommandRegistry:
    """Resolve predeclared command IDs without evaluating generated shell text."""

    def __init__(self, specs: Iterable[CommandSpec]):
        entries: dict[str, CommandSpec] = {}
        for spec in specs:
            if spec.command_id in entries:
                raise ValueError(f"duplicate command_id: {spec.command_id}")
            entries[spec.command_id] = spec
        if not entries:
            raise ValueError("at least one command specification is required")
        self._specs: Mapping[str, CommandSpec] = entries

    def get_spec(self, command_id: str) -> CommandSpec:
        """Return the immutable registered spec or fail closed."""

        try:
            return self._specs[command_id]
        except KeyError as exc:
            raise PolicyViolation(f"unknown command_id: {command_id}") from exc

    def resolve(
        self,
        command_id: str,
        args: Sequence[str],
        working_directory: Path,
        environment: EnvironmentKind,
    ) -> ResolvedCommand:
        spec = self.get_spec(command_id)
        resolved_workdir = working_directory.resolve()
        allowed = tuple(path.resolve() for path in spec.allowed_working_directories)
        if resolved_workdir not in allowed:
            raise PolicyViolation("working directory is not authorized for this command")
        resolved_args = tuple(args)
        if resolved_args not in spec.allowed_argument_vectors:
            raise PolicyViolation("command arguments do not match an exact allowed argument vector")
        if environment not in spec.allowed_environments:
            raise PolicyViolation("execution environment is not authorized for this command")
        return ResolvedCommand(spec=spec, argv=(*spec.argv_prefix, *resolved_args))


def require_execution_approval(
    spec: CommandSpec,
    *,
    decision_id: str | None,
    request_id: str,
    artifact_digest: str,
    base_revision: str,
    command_id: str,
    environment: EnvironmentKind,
    approval: ExecutionApproval | None,
) -> None:
    """Reject an approval-gated command unless every binding matches exactly."""

    if spec.requires_approval is None:
        return
    if approval is None:
        raise PolicyViolation(f"{spec.requires_approval.value} requires an execution approval")
    expected = {
        "decision_id": decision_id,
        "request_id": request_id,
        "action": spec.requires_approval,
        "artifact_digest": artifact_digest,
        "base_revision": base_revision,
        "command_id": command_id,
        "environment": environment,
    }
    for field_name, expected_value in expected.items():
        if expected_value is None or getattr(approval, field_name) != expected_value:
            raise PolicyViolation(f"execution approval {field_name} does not match")
    if not approval.reviewer:
        raise PolicyViolation("execution approval must identify a reviewer")


@dataclass(frozen=True)
class RetryBudget:
    maximum_attempts: int

    def __post_init__(self) -> None:
        if self.maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")

    def require_remaining(self, completed_attempts: int) -> None:
        if completed_attempts >= self.maximum_attempts:
            raise PolicyViolation("correction retry budget is exhausted")


def ensure_paths_within_repository(
    repository_root: Path, relative_paths: Iterable[str]
) -> tuple[Path, ...]:
    """Resolve paths and reject lexical, traversal, and symlink escapes."""

    root = repository_root.resolve(strict=True)
    resolved: list[Path] = []
    for raw_path in relative_paths:
        normalized = validate_relative_path(raw_path)
        candidate = root.joinpath(*normalized.split("/"))
        parent = candidate.parent.resolve(strict=True)
        try:
            parent.relative_to(root)
        except ValueError as exc:
            raise PolicyViolation(f"path escapes repository root: {raw_path}") from exc
        if candidate.exists() and candidate.is_symlink():
            target = candidate.resolve(strict=True)
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise PolicyViolation(f"symlink escapes repository root: {raw_path}") from exc
        resolved.append(candidate)
    return tuple(resolved)


def _has_forbidden_control(value: str) -> bool:
    return any(character in value for character in ("\x00", "\n", "\r"))


def _is_sha256_digest(value: str) -> bool:
    return re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
