"""Durable lifecycle, checkpoint, and status verification for agent runs."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel

from legacy_migration_agent.agent_runtime.correction import (
    CorrectionApproval,
    CorrectionRequest,
)
from legacy_migration_agent.agent_runtime.model_workflow import ModelAgentWorkflowRoles
from legacy_migration_agent.application.agent_run_contracts import (
    AGENT_RUN_CORRECTION_APPROVAL_PATH,
    AGENT_RUN_CORRECTION_AUTHORIZATION_INTENT_KIND,
    AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
    AGENT_RUN_FAILURE_KIND,
    AGENT_RUN_FAILURE_PATH,
    AGENT_RUN_MANIFEST_APPROVAL_PATH,
    AGENT_RUN_MANIFEST_AUTHORIZATION_INTENT_KIND,
    AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
    AGENT_RUN_STATUS_ROOT,
    AgentRunCheckpointFieldDigest,
    AgentRunCheckpointInterruptProjection,
    AgentRunCheckpointProjection,
    AgentRunCheckpointTaskProjection,
    AgentRunConfig,
    AgentRunCorrectionSummary,
    AgentRunFailure,
    AgentRunInterruptSummary,
    AgentRunLifecycleAnchor,
    AgentRunStatus,
)
from legacy_migration_agent.application.agent_run_models import _OperationSeamTracker
from legacy_migration_agent.application.final_review_status import (
    FINAL_REVIEW_DECIDED_KIND,
    FINAL_REVIEW_REQUESTED_KIND,
    read_final_review_status,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    DecisionRequest,
    MigrationManifest,
    MigrationRequest,
    Sha256Digest,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.run_session import AgentRunSession
from legacy_migration_agent.workflow import ManifestApproval

_AuthorizationIntentVerifier = Callable[..., None]
_AuthorizationWriteHook = Callable[[str, str], None]


@dataclass(frozen=True)
class _RunComponents:
    session: AgentRunSession
    request: MigrationRequest
    config: AgentRunConfig
    workflow: Any
    model_roles: ModelAgentWorkflowRoles
    seam_tracker: _OperationSeamTracker
    failure: AgentRunFailure | None = None
    in_flight_operation: _InFlightModelOperation | None = None
    in_flight_terminal: bool = False


@dataclass(frozen=True)
class _VerifiedOperationLifecycle:
    """One runtime-anchored operation and its exact workflow projection."""

    kind: str
    status: AgentRunStatus
    workflow_status: AgentRunStatus
    checkpoint: AgentRunCheckpointProjection
    failure: AgentRunFailure | None = None


@dataclass(frozen=True)
class _PartialAuthorization:
    """One runtime-anchored authorization whose lifecycle is not complete."""

    kind: str
    approval: ManifestApproval | CorrectionApproval | None


@dataclass(frozen=True)
class _InFlightModelOperation:
    """One attempt that advanced beyond its last portable lifecycle."""

    attempt: Literal[1, 2]
    pending_seam: Literal["engineer", "validator"] | None
    artifact_paths: tuple[str, ...]
    portable_freeze_prefix: tuple[str, ...]
    controller_failed_before_provider: bool = False


def _portable_artifact_exists(session: AgentRunSession, path: str) -> bool:
    try:
        session.store.read_json(path)
    except FileNotFoundError:
        return False
    return True


def _workflow_artifact_exists(session: AgentRunSession, path: str) -> bool:
    try:
        session.store.read_json(path)
    except FileNotFoundError:
        return False
    return True


def _canonical_checkpoint_value(value: object) -> object:
    """Convert one strictly deserialized checkpoint value to canonical JSON."""

    if isinstance(value, BaseModel):
        return _canonical_checkpoint_value(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return _canonical_checkpoint_value(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PolicyViolation("checkpoint mappings require string keys")
            normalized[key] = _canonical_checkpoint_value(item)
        return normalized
    if isinstance(value, (tuple, list)):
        return [_canonical_checkpoint_value(item) for item in value]
    if isinstance(value, BaseException):
        return {
            "exception_type": f"{type(value).__module__}.{type(value).__qualname__}",
            "arguments": [_canonical_checkpoint_value(item) for item in value.args],
        }
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise PolicyViolation(
        "checkpoint contains an unsupported value type: "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _checkpoint_value_digest(value: object) -> Sha256Digest:
    return artifact_digest(_canonical_checkpoint_value(value))


def _checkpoint_projection(components: _RunComponents) -> AgentRunCheckpointProjection:
    """Project the complete current checkpoint without exposing opaque values."""

    snapshot = components.workflow.snapshot(thread_id=components.session.context.thread_id)
    values = cast(Mapping[str, object], snapshot.values)
    try:
        checkpoint_request = MigrationRequest.model_validate(values["request"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("checkpoint lacks a canonical migration request") from exc
    if checkpoint_request != components.request:
        raise PolicyViolation("checkpoint request differs from canonical run evidence")

    field_digests = tuple(
        AgentRunCheckpointFieldDigest(
            name=name,
            value_digest=_checkpoint_value_digest(value),
        )
        for name, value in sorted(values.items())
    )
    tasks: list[AgentRunCheckpointTaskProjection] = []
    for task in snapshot.tasks:
        interrupts = tuple(
            AgentRunCheckpointInterruptProjection(
                interrupt_id=str(pending.id),
                value_digest=_checkpoint_value_digest(pending.value),
            )
            for pending in task.interrupts
        )
        tasks.append(
            AgentRunCheckpointTaskProjection(
                task_id=str(task.id),
                name=str(task.name),
                path_digest=_checkpoint_value_digest(task.path),
                error_digest=_checkpoint_value_digest(task.error),
                interrupts=interrupts,
                state_digest=_checkpoint_value_digest(task.state),
                result_digest=_checkpoint_value_digest(task.result),
            )
        )
    projection = AgentRunCheckpointProjection(
        run_id=components.session.context.run_id,
        thread_id=components.session.context.thread_id,
        request=checkpoint_request,
        values=field_digests,
        next=tuple(str(node) for node in snapshot.next),
        tasks=tuple(tasks),
    )
    components.session.validate_portable_evidence(projection)
    return projection


def _freeze_operation_evidence(
    session: AgentRunSession,
    kind: str,
    status: AgentRunStatus,
    workflow_status: AgentRunStatus,
    checkpoint: AgentRunCheckpointProjection,
    *,
    after_authorization_write: _AuthorizationWriteHook,
) -> None:
    """Freeze portable artifacts together with the exact workflow projection."""

    # Authorization snapshots are deliberately pre-execution boundaries.  An
    # exact resubmission after process loss may already have an uncompleted
    # runtime lease; promoting it here would widen the immutable authorization
    # index before the controller can turn that claim into a terminal failure.
    if kind not in {
        AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
        AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
    }:
        session.promote_model_operation_artifacts()
    session.store.write_json(_status_projection_path(kind), status)
    after_authorization_write(kind, "status")
    session.store.write_json(_workflow_projection_path(kind), workflow_status)
    after_authorization_write(kind, "workflow_status")
    session.store.write_json(_checkpoint_projection_path(kind), checkpoint)
    after_authorization_write(kind, "checkpoint")
    session.write_index(kind)
    after_authorization_write(kind, "index")
    index_payload = session.store.read_json(f"indexes/{kind}.json")
    anchor = AgentRunLifecycleAnchor(
        lifecycle_kind=kind,
        run_id=session.context.run_id,
        thread_id=session.context.thread_id,
        request_digest=session.context.request_digest,
        lifecycle_index_digest=artifact_digest(index_payload),
    )
    session.bind_runtime_anchor(kind, anchor)
    after_authorization_write(kind, "anchor")
    session.verify_index(kind, exact=True)
    session.verify_runtime_anchor(kind, anchor)


def _status_projection_path(kind: str) -> str:
    return f"{AGENT_RUN_STATUS_ROOT}/{kind}.json"


def _workflow_projection_path(kind: str) -> str:
    return f"{AGENT_RUN_STATUS_ROOT}/{kind}-workflow.json"


def _checkpoint_projection_path(kind: str) -> str:
    return f"{AGENT_RUN_STATUS_ROOT}/{kind}-checkpoint.json"


def _verify_latest_operation_evidence(
    session: AgentRunSession,
    request: MigrationRequest,
    *,
    pending_authorization: ManifestApproval | CorrectionApproval | None,
    verify_authorization_intent: _AuthorizationIntentVerifier,
) -> _VerifiedOperationLifecycle:
    if session.has_runtime_anchor(AGENT_RUN_FAILURE_KIND):
        _verify_operation_lifecycle(session, AGENT_RUN_FAILURE_KIND, exact=True)
        try:
            failure = AgentRunFailure.model_validate(
                session.store.read_json(AGENT_RUN_FAILURE_PATH)
            )
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("agent-run failure evidence is malformed") from exc
        expected = (
            session.context.run_id,
            session.context.thread_id,
            request.request_id,
            session.context.request_digest,
            artifact_digest(session.context),
            session.context.source_revision,
            session.context.agent_definition_digests,
        )
        actual = (
            failure.run_id,
            failure.thread_id,
            failure.request_id,
            failure.request_digest,
            failure.session_context_digest,
            failure.source_revision,
            failure.agent_definition_digests,
        )
        if actual != expected:
            raise PolicyViolation("agent-run failure evidence differs from the run session")
        status = _read_status_projection(session, AGENT_RUN_FAILURE_KIND, request)
        workflow_status = _read_workflow_projection(
            session,
            AGENT_RUN_FAILURE_KIND,
            request,
        )
        if status.failure != failure:
            raise PolicyViolation("failed status projection differs from failure evidence")
        checkpoint = _read_checkpoint_projection(
            session,
            AGENT_RUN_FAILURE_KIND,
            request,
        )
        _verify_completed_authorizations_for_lifecycle(
            session,
            request,
            manifest_required=failure.operation in {"resume", "retry"},
            correction_required=failure.operation == "retry",
            verify_authorization_intent=verify_authorization_intent,
        )
        return _VerifiedOperationLifecycle(
            kind=AGENT_RUN_FAILURE_KIND,
            status=status,
            workflow_status=workflow_status,
            checkpoint=checkpoint,
            failure=failure,
        )

    latest: str | None = None
    for kind in (
        "agent-run-retried",
        AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
        "agent-run-resumed",
        AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
        "agent-run-planned",
    ):
        if session.has_runtime_anchor(kind):
            latest = kind
            break
    if latest is None:
        raise PolicyViolation("run has no completed agent operation lifecycle evidence")
    # A completed run may later acquire one governed final-review lifecycle.
    # In that case the original operation inventory remains immutable but is
    # intentionally no longer the complete artifact set.  Verify the newer
    # lifecycle exactly before relaxing only the older inventory comparison.
    has_final_review = _verify_subsequent_final_review(session)
    partial = _partial_authorization(
        session,
        verify_authorization_intent=verify_authorization_intent,
    )
    if partial is not None and has_final_review:
        raise PolicyViolation("partial authorization cannot follow final review")
    runtime_model_paths = session.model_operation_artifact_paths()
    try:
        latest_index = cast(
            Mapping[str, object],
            session.store.read_json(f"indexes/{latest}.json"),
        )
        latest_indexed_paths = {
            str(cast(Mapping[str, object], item)["path"])
            for item in cast(tuple[object, ...] | list[object], latest_index["artifacts"])
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("latest lifecycle inventory is malformed") from exc
    has_unbound_runtime_transaction = any(
        path not in latest_indexed_paths for path in runtime_model_paths
    )
    _verify_operation_lifecycle(
        session,
        latest,
        exact=not has_final_review and partial is None and not has_unbound_runtime_transaction,
    )
    status = _read_status_projection(session, latest, request)
    workflow_status = _read_workflow_projection(session, latest, request)
    checkpoint = _read_checkpoint_projection(session, latest, request)
    _verify_completed_authorizations_for_lifecycle(
        session,
        request,
        manifest_required=latest
        in {
            AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
            "agent-run-resumed",
            AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
            "agent-run-retried",
        },
        correction_required=latest in {AGENT_RUN_CORRECTION_AUTHORIZED_KIND, "agent-run-retried"},
        verify_authorization_intent=verify_authorization_intent,
    )
    if partial is not None:
        _verify_partial_authorization(
            session,
            request,
            base_kind=latest,
            base_status=status,
            base_workflow_status=workflow_status,
            base_checkpoint=checkpoint,
            partial=partial,
            pending_authorization=pending_authorization,
            verify_authorization_intent=verify_authorization_intent,
        )
    return _VerifiedOperationLifecycle(
        kind=latest,
        status=status,
        workflow_status=workflow_status,
        checkpoint=checkpoint,
    )


def _partial_authorization(
    session: AgentRunSession,
    *,
    verify_authorization_intent: _AuthorizationIntentVerifier,
) -> _PartialAuthorization | None:
    """Return one intent-anchored but incomplete authorization transaction."""

    manifest_intent = session.has_runtime_anchor(AGENT_RUN_MANIFEST_AUTHORIZATION_INTENT_KIND)
    correction_intent = session.has_runtime_anchor(AGENT_RUN_CORRECTION_AUTHORIZATION_INTENT_KIND)
    manifest_approval = _portable_artifact_exists(session, AGENT_RUN_MANIFEST_APPROVAL_PATH)
    correction_approval = _portable_artifact_exists(session, AGENT_RUN_CORRECTION_APPROVAL_PATH)
    if manifest_approval and not manifest_intent:
        raise PolicyViolation("manifest authorization lacks an immutable runtime intent")
    if correction_approval and not correction_intent:
        raise PolicyViolation("correction authorization lacks an immutable runtime intent")
    manifest_partial = manifest_intent and not session.has_runtime_anchor(
        AGENT_RUN_MANIFEST_AUTHORIZED_KIND
    )
    correction_partial = correction_intent and not session.has_runtime_anchor(
        AGENT_RUN_CORRECTION_AUTHORIZED_KIND
    )
    if manifest_partial and correction_partial:
        raise PolicyViolation("run contains multiple partial authorization transactions")
    if manifest_partial:
        approval: ManifestApproval | None = None
        if manifest_approval:
            try:
                approval = ManifestApproval.model_validate(
                    session.store.read_json(AGENT_RUN_MANIFEST_APPROVAL_PATH)
                )
            except (TypeError, ValueError) as exc:
                raise PolicyViolation("partial manifest authorization is malformed") from exc
            session.validate_portable_evidence(approval)
            verify_authorization_intent(
                session,
                operation="resume",
                kind=AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
                control_path=AGENT_RUN_MANIFEST_APPROVAL_PATH,
                approval=approval,
            )
        return _PartialAuthorization(
            kind=AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
            approval=approval,
        )
    if correction_partial:
        correction: CorrectionApproval | None = None
        if correction_approval:
            try:
                correction = CorrectionApproval.model_validate(
                    session.store.read_json(AGENT_RUN_CORRECTION_APPROVAL_PATH)
                )
            except (TypeError, ValueError) as exc:
                raise PolicyViolation("partial correction authorization is malformed") from exc
            session.validate_portable_evidence(correction)
            verify_authorization_intent(
                session,
                operation="retry",
                kind=AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
                control_path=AGENT_RUN_CORRECTION_APPROVAL_PATH,
                approval=correction,
            )
        return _PartialAuthorization(
            kind=AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
            approval=correction,
        )
    return None


def _verify_partial_authorization(
    session: AgentRunSession,
    request: MigrationRequest,
    *,
    base_kind: str,
    base_status: AgentRunStatus,
    base_workflow_status: AgentRunStatus,
    base_checkpoint: AgentRunCheckpointProjection,
    partial: _PartialAuthorization,
    pending_authorization: ManifestApproval | CorrectionApproval | None,
    verify_authorization_intent: _AuthorizationIntentVerifier,
) -> None:
    """Verify every possible prefix of the immutable authorization writes."""

    partial_kind = partial.kind
    approval = partial.approval
    if partial_kind == AGENT_RUN_MANIFEST_AUTHORIZED_KIND:
        if base_kind != "agent-run-planned":
            raise PolicyViolation("partial manifest authorization has the wrong base lifecycle")
        if approval is not None and not isinstance(approval, ManifestApproval):
            raise PolicyViolation("partial manifest authorization has the wrong evidence type")
        approval_path = AGENT_RUN_MANIFEST_APPROVAL_PATH
        operation: Literal["resume", "retry"] = "resume"
    elif partial_kind == AGENT_RUN_CORRECTION_AUTHORIZED_KIND:
        if base_kind != "agent-run-resumed":
            raise PolicyViolation("partial correction authorization has the wrong base lifecycle")
        if approval is not None and not isinstance(approval, CorrectionApproval):
            raise PolicyViolation("partial correction authorization has the wrong evidence type")
        approval_path = AGENT_RUN_CORRECTION_APPROVAL_PATH
        operation = "retry"
    else:  # pragma: no cover - internal caller supplies only two constants
        raise AssertionError(f"unknown partial authorization kind: {partial_kind}")
    if (
        approval is not None
        and pending_authorization is not None
        and pending_authorization != approval
    ):
        raise PolicyViolation("approval differs from the partial authorization transaction")
    presented = approval if approval is not None else pending_authorization
    if presented is not None:
        if operation == "resume" and not isinstance(presented, ManifestApproval):
            raise PolicyViolation("manifest authorization has the wrong evidence type")
        if operation == "retry" and not isinstance(presented, CorrectionApproval):
            raise PolicyViolation("correction authorization has the wrong evidence type")
        verify_authorization_intent(
            session,
            operation=operation,
            kind=partial_kind,
            control_path=approval_path,
            approval=presented,
        )

    base_index_path = f"indexes/{base_kind}.json"
    try:
        base_index = session.store.read_json(base_index_path)
        base_paths = {
            str(item["path"]) for item in cast(list[dict[str, Any]], base_index["artifacts"])
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("base lifecycle inventory is malformed") from exc
    current_paths = set(_portable_evidence_paths(session.evidence_dir))
    current_paths.discard(base_index_path)
    extra_paths = current_paths - base_paths
    write_order = (
        approval_path,
        _status_projection_path(partial_kind),
        _workflow_projection_path(partial_kind),
        _checkpoint_projection_path(partial_kind),
        f"indexes/{partial_kind}.json",
    )
    valid_prefixes = {frozenset(write_order[:length]) for length in range(0, 6)}
    if frozenset(extra_paths) not in valid_prefixes:
        raise PolicyViolation("partial authorization contains an unexpected artifact inventory")

    if _status_projection_path(partial_kind) in extra_paths:
        if _read_status_projection(session, partial_kind, request) != base_status:
            raise PolicyViolation("partial authorization status differs from its base lifecycle")
    if _workflow_projection_path(partial_kind) in extra_paths:
        if _read_workflow_projection(session, partial_kind, request) != base_workflow_status:
            raise PolicyViolation(
                "partial authorization workflow status differs from its base lifecycle"
            )
    if _checkpoint_projection_path(partial_kind) in extra_paths:
        if _read_checkpoint_projection(session, partial_kind, request) != base_checkpoint:
            raise PolicyViolation(
                "partial authorization checkpoint differs from its base lifecycle"
            )
    partial_index_path = f"indexes/{partial_kind}.json"
    if partial_index_path in extra_paths:
        session.verify_index(partial_kind, exact=True)


def _verify_completed_authorizations_for_lifecycle(
    session: AgentRunSession,
    request: MigrationRequest,
    *,
    manifest_required: bool,
    correction_required: bool,
    verify_authorization_intent: _AuthorizationIntentVerifier,
) -> None:
    """Require each consumed human decision to match its prepublication intent."""

    if not manifest_required and session.has_runtime_anchor(AGENT_RUN_MANIFEST_AUTHORIZED_KIND):
        raise PolicyViolation("manifest authorization is inconsistent with the lifecycle")
    if not correction_required and session.has_runtime_anchor(AGENT_RUN_CORRECTION_AUTHORIZED_KIND):
        raise PolicyViolation("correction authorization is inconsistent with the lifecycle")
    if manifest_required:
        if not session.has_runtime_anchor(AGENT_RUN_MANIFEST_AUTHORIZED_KIND):
            raise PolicyViolation("lifecycle lacks completed manifest authorization")
        try:
            manifest = ManifestApproval.model_validate(
                session.store.read_json(AGENT_RUN_MANIFEST_APPROVAL_PATH)
            )
        except FileNotFoundError as exc:
            raise PolicyViolation(
                "completed manifest authorization lacks approval evidence"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("completed manifest authorization is malformed") from exc
        if manifest.request_id != request.request_id:
            raise PolicyViolation("manifest authorization belongs to another request")
        verify_authorization_intent(
            session,
            operation="resume",
            kind=AGENT_RUN_MANIFEST_AUTHORIZED_KIND,
            control_path=AGENT_RUN_MANIFEST_APPROVAL_PATH,
            approval=manifest,
        )
    if correction_required:
        if not session.has_runtime_anchor(AGENT_RUN_CORRECTION_AUTHORIZED_KIND):
            raise PolicyViolation("lifecycle lacks completed correction authorization")
        try:
            correction = CorrectionApproval.model_validate(
                session.store.read_json(AGENT_RUN_CORRECTION_APPROVAL_PATH)
            )
        except FileNotFoundError as exc:
            raise PolicyViolation(
                "completed correction authorization lacks approval evidence"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("completed correction authorization is malformed") from exc
        if correction.request_id != request.request_id:
            raise PolicyViolation("correction authorization belongs to another request")
        verify_authorization_intent(
            session,
            operation="retry",
            kind=AGENT_RUN_CORRECTION_AUTHORIZED_KIND,
            control_path=AGENT_RUN_CORRECTION_APPROVAL_PATH,
            approval=correction,
        )


def _portable_evidence_paths(root: Path) -> tuple[str, ...]:
    """Safely enumerate the portable JSON inventory without following links."""

    paths: list[str] = []

    def scan(directory: Path) -> None:
        with os.scandir(directory) as children:
            ordered = sorted(children, key=lambda child: child.name)
        for child in ordered:
            metadata = child.stat(follow_symlinks=False)
            relative = Path(child.path).relative_to(root).as_posix()
            if stat.S_ISLNK(metadata.st_mode):
                raise PolicyViolation(f"portable evidence cannot contain symlinks: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                scan(Path(child.path))
                continue
            if not stat.S_ISREG(metadata.st_mode) or not relative.endswith(".json"):
                raise PolicyViolation(
                    f"portable evidence contains an unsupported artifact: {relative}"
                )
            paths.append(validate_relative_path(relative))

    scan(root)
    return tuple(sorted(paths))


def _verify_operation_lifecycle(
    session: AgentRunSession,
    kind: str,
    *,
    exact: bool,
) -> None:
    session.verify_index(kind, exact=exact)
    index_payload = session.store.read_json(f"indexes/{kind}.json")
    session.verify_runtime_anchor(
        kind,
        AgentRunLifecycleAnchor(
            lifecycle_kind=kind,
            run_id=session.context.run_id,
            thread_id=session.context.thread_id,
            request_digest=session.context.request_digest,
            lifecycle_index_digest=artifact_digest(index_payload),
        ),
    )


def _read_status_projection(
    session: AgentRunSession,
    kind: str,
    request: MigrationRequest,
) -> AgentRunStatus:
    try:
        status = AgentRunStatus.model_validate(
            session.store.read_json(_status_projection_path(kind))
        )
    except FileNotFoundError as exc:
        raise PolicyViolation("agent-run lifecycle lacks a status projection") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("agent-run status projection is malformed") from exc
    _validate_status_projection_binding(session, request, status)
    return status


def _validate_status_projection_binding(
    session: AgentRunSession,
    request: MigrationRequest,
    status: AgentRunStatus,
) -> None:
    expected = (
        session.context.run_id,
        session.context.thread_id,
        request.request_id,
        session.context.request_digest,
        request.platform,
        session.context.provider_id,
        session.context.model_id,
    )
    actual = (
        status.run_id,
        status.thread_id,
        status.request_id,
        status.request_digest,
        status.platform,
        status.provider_id,
        status.model_id,
    )
    if actual != expected:
        raise PolicyViolation("agent-run status projection differs from the run session")


def _read_workflow_projection(
    session: AgentRunSession,
    kind: str,
    request: MigrationRequest,
) -> AgentRunStatus:
    try:
        status = AgentRunStatus.model_validate(
            session.store.read_json(_workflow_projection_path(kind))
        )
    except FileNotFoundError as exc:
        raise PolicyViolation("agent-run lifecycle lacks a workflow projection") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("agent-run workflow projection is malformed") from exc
    _validate_status_projection_binding(session, request, status)
    return status


def _read_checkpoint_projection(
    session: AgentRunSession,
    kind: str,
    request: MigrationRequest,
) -> AgentRunCheckpointProjection:
    try:
        checkpoint = AgentRunCheckpointProjection.model_validate(
            session.store.read_json(_checkpoint_projection_path(kind))
        )
    except FileNotFoundError as exc:
        raise PolicyViolation("agent-run lifecycle lacks a checkpoint projection") from exc
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("agent-run checkpoint projection is malformed") from exc
    expected = (
        session.context.run_id,
        session.context.thread_id,
        request,
    )
    actual = (
        checkpoint.run_id,
        checkpoint.thread_id,
        checkpoint.request,
    )
    if actual != expected:
        raise PolicyViolation("agent-run checkpoint projection differs from the run session")
    return checkpoint


def _verify_subsequent_final_review(session: AgentRunSession) -> bool:
    """Verify a later final-review checkpoint without invoking any provider."""

    has_requested = session.has_runtime_anchor(FINAL_REVIEW_REQUESTED_KIND)
    has_decided = session.has_runtime_anchor(FINAL_REVIEW_DECIDED_KIND)
    if not has_requested and not has_decided:
        return False
    if has_decided and not has_requested:
        raise PolicyViolation("final-review decision exists without its request lifecycle")
    read_final_review_status(session)
    return True


def _pending_manifest_interrupt(
    snapshot: Any,
) -> tuple[DecisionRequest, str, Sha256Digest] | None:
    """Return and internally cross-check the one exact manifest interrupt."""

    candidates: list[Mapping[str, Any]] = []
    for task in snapshot.tasks:
        for pending in task.interrupts:
            value = pending.value
            if not isinstance(value, Mapping) or value.get("kind") != "manifest_approval":
                raise PolicyViolation("workflow contains an unknown pending interrupt")
            candidates.append(value)
    if not candidates:
        return None
    if len(candidates) != 1:
        raise PolicyViolation("workflow contains multiple pending manifest interrupts")

    values = cast(dict[str, Any], snapshot.values)
    try:
        decision = DecisionRequest.model_validate(candidates[0]["decision"])
        request = MigrationRequest.model_validate(values["request"])
        manifest = MigrationManifest.model_validate(values["manifest"])
        manifest_digest = cast(Sha256Digest, values["manifest_digest"])
        interrupt_manifest_id = str(candidates[0]["manifest_id"])
        interrupt_manifest_digest = str(candidates[0]["manifest_digest"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation("pending manifest interrupt is malformed") from exc
    if artifact_digest(manifest) != manifest_digest:
        raise PolicyViolation("checkpointed manifest digest does not match manifest")
    expected = (
        request.request_id,
        manifest.manifest_id,
        manifest_digest,
        ApprovalAction.APPROVE_MANIFEST,
    )
    actual = (
        decision.request_id,
        interrupt_manifest_id,
        interrupt_manifest_digest,
        decision.requested_action,
    )
    if actual != expected:
        raise PolicyViolation("pending manifest interrupt differs from checkpoint state")
    return decision, manifest.manifest_id, manifest_digest


def _status_from_components(
    components: _RunComponents,
    *,
    surface_failure: bool = True,
) -> AgentRunStatus:
    snapshot = components.workflow.snapshot(thread_id=components.session.context.thread_id)
    values = cast(dict[str, Any], snapshot.values)
    manifest = values.get("manifest")
    manifest_id = getattr(manifest, "manifest_id", None)
    manifest_digest = values.get("manifest_digest")
    interrupt_summary: AgentRunInterruptSummary | None = None
    pending = _pending_manifest_interrupt(snapshot)
    if pending is not None:
        decision, pending_manifest_id, pending_manifest_digest = pending
        interrupt_summary = AgentRunInterruptSummary(
            decision_id=decision.decision_id,
            request_id=decision.request_id,
            manifest_id=pending_manifest_id,
            manifest_digest=pending_manifest_digest,
            requested_action=decision.requested_action,
            options=cast(
                tuple[Literal["approve", "reject", "modify"], ...],
                decision.options,
            ),
        )
    correction_summary: AgentRunCorrectionSummary | None = None
    raw_correction = values.get("correction_request")
    if raw_correction is not None:
        correction = CorrectionRequest.model_validate(raw_correction)
        correction_summary = AgentRunCorrectionSummary(
            correction_id=correction.correction_id,
            request_id=correction.request_id,
            manifest_id=correction.manifest_id,
            manifest_digest=correction.manifest_digest,
            report_id=correction.report_id,
            report_digest=correction.report_digest,
            change_set_digest=correction.change_set_digest,
            base_revision=correction.base_revision,
            completed_attempt=correction.completed_attempt,
            authorized_attempt=correction.next_attempt,
            action=correction.action,
        )
    failure = components.failure
    failed_terminal = failure is not None and surface_failure
    status = AgentRunStatus(
        run_id=components.session.context.run_id,
        thread_id=components.session.context.thread_id,
        request_id=components.request.request_id,
        request_digest=components.session.context.request_digest,
        platform=components.request.platform,
        provider_id=components.session.context.provider_id,
        model_id=components.session.context.model_id,
        status="failed" if failed_terminal else str(values.get("status", "unknown")),
        terminal_disposition=(
            "controlled_failure"
            if failed_terminal
            else (
                str(values["terminal_disposition"])
                if values.get("terminal_disposition") is not None
                else None
            )
        ),
        execution_attempt=(
            failure.attempt
            if failure is not None and surface_failure
            else int(values.get("execution_attempt", 1))
        ),
        pending_nodes=() if failed_terminal else tuple(snapshot.next),
        task_failed=(
            True
            if failed_terminal
            else any(getattr(task, "error", None) is not None for task in snapshot.tasks)
        ),
        manifest_id=manifest_id,
        manifest_digest=manifest_digest,
        interrupt=None if failed_terminal else interrupt_summary,
        correction=None if failed_terminal else correction_summary,
        failure=failure if failed_terminal else None,
    )
    components.session.validate_portable_evidence(status)
    return status
