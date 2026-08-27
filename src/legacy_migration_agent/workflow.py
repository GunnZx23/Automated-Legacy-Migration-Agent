"""Deterministic, checkpointed orchestration for a migration attempt.

The graph deliberately keeps role implementations injectable. Production
AgentRun composition supplies model-backed roles and tool-backed validators;
focused tests can supply deterministic functions without changing the approval
boundary.

The approval node has an important LangGraph property: code before
``interrupt`` is pure and repeatable.  LangGraph restarts that node when a
``Command(resume=...)`` is supplied, so no write or other external effect may
be placed before the interrupt.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Protocol, TypedDict, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import Field

from legacy_migration_agent.agent_runtime.correction import (
    CorrectionAction,
    CorrectionApproval,
    CorrectionAttemptEvidence,
    CorrectionController,
    CorrectionRequest,
    validate_correction_attempt_evidence,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    ChangeSet,
    DecisionRequest,
    Identifier,
    ImplementationIntervention,
    ManifestStatus,
    MigrationManifest,
    MigrationRequest,
    PlanningIntervention,
    StrictModel,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.observability import lifecycle_event
from legacy_migration_agent.core.policies import (
    PolicyViolation,
    validate_change_set,
    validate_manifest_for_request,
    validate_report,
)

MANIFEST_APPROVAL_ACTION = ApprovalAction.APPROVE_MANIFEST


class Architect(Protocol):
    """Produce one bounded manifest or a non-authorizing preflight stop."""

    def __call__(
        self,
        request: MigrationRequest,
    ) -> MigrationManifest | PlanningIntervention: ...


class Engineer(Protocol):
    """Produce a candidate change set within an approved manifest."""

    def __call__(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
    ) -> ChangeSet | ImplementationIntervention: ...


class Validator(Protocol):
    """Evaluate a candidate and return evidence-backed check results."""

    def __call__(
        self,
        request: MigrationRequest,
        manifest: MigrationManifest,
        change_set: ChangeSet,
    ) -> ValidationReport: ...


class ApprovalSelection(str):
    """Allowed human responses at the manifest gate."""

    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"


class ManifestApproval(StrictModel):
    """Human response bound to one decision and one manifest byte model."""

    decision_id: Identifier
    request_id: Identifier
    manifest_id: Identifier
    manifest_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    requested_action: ApprovalAction
    selection: str = Field(pattern=r"^(approve|reject|modify)$")
    reviewer: Identifier
    comment: str = Field(default="", max_length=2000)


class WorkflowStatus(str):
    """Stable status values exposed by the workflow state."""

    AWAITING_APPROVAL = "awaiting_approval"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    COMPLETED = "completed"
    REJECTED = "rejected"
    MODIFICATION_REQUESTED = "modification_requested"
    DECISION_REQUIRED = "decision_required"


class WorkflowState(TypedDict, total=False):
    request: MigrationRequest
    planning_intervention: PlanningIntervention
    planning_intervention_digest: str
    implementation_intervention: ImplementationIntervention
    implementation_intervention_digest: str
    manifest: MigrationManifest
    manifest_digest: str
    decision_request: DecisionRequest
    approval: ManifestApproval
    approval_outcome: str
    execution_attempt: int
    change_set: ChangeSet
    validation_report: ValidationReport
    correction_request: Mapping[str, Any]
    correction_approval: Mapping[str, Any]
    correction_attempt_evidence: Mapping[str, Any]
    status: str
    terminal_disposition: ValidationDisposition
    terminal_reason: str


def manifest_digest(manifest: MigrationManifest) -> str:
    """Return a canonical digest of all manifest fields and their ordering."""

    canonical = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(canonical).hexdigest()}"


def manifest_decision_request(
    request: MigrationRequest,
    manifest: MigrationManifest,
    digest: str,
) -> DecisionRequest:
    """Build the deterministic decision contract shown at the interrupt."""

    if manifest.status is not ManifestStatus.PLANNED:
        raise PolicyViolation("only a planned manifest can request implementation approval")
    digest_token = digest.removeprefix("sha256:")[:24]
    return DecisionRequest(
        decision_id=f"manifest-approval-{digest_token}",
        request_id=request.request_id,
        reason=(
            "The Architect produced a bounded migration manifest. Human approval "
            "is required before the Engineer may generate any changes."
        ),
        requested_action=MANIFEST_APPROVAL_ACTION,
        affected_paths=manifest.approved_paths,
        evidence=(
            f"manifest_id={manifest.manifest_id}",
            f"manifest_digest={digest}",
            f"base_revision={manifest.base_revision}",
        ),
        options=(
            ApprovalSelection.APPROVE,
            ApprovalSelection.REJECT,
            ApprovalSelection.MODIFY,
        ),
        recommendation=(
            "Approve only when the paths, transformations, validation plan, and "
            "identified risks are acceptable for this exact repository revision."
        ),
    )


def validate_planning_intervention_for_request(
    intervention: PlanningIntervention,
    request: MigrationRequest,
) -> None:
    """Bind a terminal preflight outcome to one exact migration request."""

    if intervention.request_id != request.request_id:
        raise PolicyViolation("planning intervention belongs to another request")
    if intervention.request_digest != artifact_digest(request):
        raise PolicyViolation("planning intervention request digest does not match")
    if intervention.platform is not request.platform:
        raise PolicyViolation("planning intervention platform does not match the request")
    if intervention.base_revision != request.base_revision:
        raise PolicyViolation("planning intervention is stale for the requested revision")
    if request.target.entry_path not in intervention.affected_paths:
        raise PolicyViolation("planning intervention does not cover the request target entry")
    if not any(
        request.target.entry_path in evidence.affected_paths for evidence in intervention.evidence
    ):
        raise PolicyViolation("planning intervention evidence does not cover the request target")


def planning_intervention_decision_request(
    request: MigrationRequest,
    intervention: PlanningIntervention,
    digest: str,
) -> DecisionRequest:
    """Build a terminal request to resolve evidence and begin a new plan."""

    validate_planning_intervention_for_request(intervention, request)
    digest_token = digest.removeprefix("sha256:")[:24]
    return DecisionRequest(
        decision_id=f"planning-intervention-{digest_token}",
        request_id=request.request_id,
        reason=intervention.reason,
        requested_action=intervention.requested_action,
        affected_paths=intervention.affected_paths,
        evidence=(
            f"planning_intervention_id={intervention.intervention_id}",
            f"planning_intervention_digest={digest}",
            f"request_digest={intervention.request_digest}",
            *(f"{item.source}_digest={item.source_digest}" for item in intervention.evidence),
        ),
        options=tuple(option.value for option in intervention.options),
        recommendation=intervention.recommendation,
    )


def validate_implementation_intervention_for_manifest(
    intervention: ImplementationIntervention,
    request: MigrationRequest,
    manifest: MigrationManifest,
) -> None:
    """Bind an Engineer stop to the exact approved request and manifest bytes."""

    if intervention.request_id != request.request_id:
        raise PolicyViolation("implementation intervention belongs to another request")
    if intervention.request_digest != artifact_digest(request):
        raise PolicyViolation("implementation intervention request digest does not match")
    if intervention.manifest_id != manifest.manifest_id:
        raise PolicyViolation("implementation intervention belongs to another manifest")
    if intervention.manifest_digest != artifact_digest(manifest):
        raise PolicyViolation("implementation intervention manifest digest does not match")
    if intervention.base_revision != request.base_revision:
        raise PolicyViolation("implementation intervention is stale for the requested revision")
    approved_affected = set(intervention.affected_paths) & set(manifest.approved_paths)
    if not approved_affected:
        raise PolicyViolation(
            "implementation intervention does not cover any manifest-approved output"
        )
    if not any(set(item.affected_paths) & approved_affected for item in intervention.evidence):
        raise PolicyViolation(
            "implementation intervention evidence does not cover an approved output"
        )


def implementation_intervention_decision_request(
    request: MigrationRequest,
    manifest: MigrationManifest,
    intervention: ImplementationIntervention,
    digest: str,
) -> DecisionRequest:
    """Build a terminal, non-authorizing decision for an Engineer stop."""

    validate_implementation_intervention_for_manifest(intervention, request, manifest)
    digest_token = digest.removeprefix("sha256:")[:24]
    return DecisionRequest(
        decision_id=f"implementation-intervention-{digest_token}",
        request_id=request.request_id,
        reason=(
            "The Engineer stopped without producing or applying any file updates because "
            f"the approved implementation evidence is insufficient: {intervention.reason} "
            "This terminal result grants no implementation authority."
        ),
        requested_action=intervention.requested_action,
        affected_paths=intervention.affected_paths,
        evidence=(
            f"implementation_intervention_id={intervention.intervention_id}",
            f"implementation_intervention_digest={digest}",
            f"request_digest={intervention.request_digest}",
            f"manifest_id={intervention.manifest_id}",
            f"manifest_digest={intervention.manifest_digest}",
            f"agent_version={intervention.agent_version}",
            f"agent_definition_digest={intervention.agent_definition_digest}",
            f"input_evidence_digest={intervention.input_evidence_digest}",
            *(f"concern={concern}" for concern in intervention.public_concerns),
            *(
                f"{item.source}_digest={item.source_digest}:{item.summary}"
                for item in intervention.evidence
            ),
        ),
        options=tuple(option.value for option in intervention.options),
        recommendation=(
            "Resolve the cited evidence or scope issue, then create a new or revised "
            "manifest and obtain a new exact approval before implementation. A correction "
            "also requires its own newly evidence-bound decision; this stop cannot reuse or "
            "extend prior authority."
        ),
    )


def decision_required_manifest_request(
    request: MigrationRequest,
    manifest: MigrationManifest,
    digest: str,
) -> DecisionRequest:
    """Explain why a model plan stopped without creating approval authority."""

    if manifest.status is not ManifestStatus.DECISION_REQUIRED:
        raise PolicyViolation("manifest is not a decision-required planning outcome")
    _validate_manifest_request_binding(manifest, request)
    planning_actions = {
        ApprovalAction.EXPAND_SCOPE,
        ApprovalAction.ACCEPT_HIGH_IMPACT_CHANGE,
        ApprovalAction.DESTRUCTIVE_CHANGE,
    }
    requested_action = next(
        (action for action in manifest.required_approvals if action in planning_actions),
        ApprovalAction.EXPAND_SCOPE,
    )
    affected_paths = tuple(
        dict.fromkeys(
            (
                *manifest.approved_paths,
                *(
                    dependency.path
                    for dependency in manifest.dependencies
                    if not dependency.resolved
                ),
            )
        )
    )
    evidence = [
        f"manifest_id={manifest.manifest_id}",
        f"manifest_digest={digest}",
        f"base_revision={manifest.base_revision}",
    ]
    evidence.extend(
        f"unresolved_dependency={dependency.path}:{dependency.relation}"
        for dependency in manifest.dependencies
        if not dependency.resolved
    )
    evidence.extend(
        f"planning_risk={risk.category.value}:{risk.summary}" for risk in manifest.risks
    )
    return DecisionRequest(
        decision_id=f"planning-manifest-{digest.removeprefix('sha256:')[:24]}",
        request_id=request.request_id,
        reason=(
            "The Architect stopped during planning because evidence or a material risk "
            "requires a human decision. This terminal result cannot authorize implementation."
        ),
        requested_action=requested_action,
        affected_paths=affected_paths,
        evidence=tuple(evidence),
        options=("resolve_evidence_and_replan", "stop_request"),
        recommendation=(
            "Resolve the cited evidence or risk, then start a new planning run with a new "
            "manifest digest. Do not approve or implement this stopped plan."
        ),
    )


def _validate_manifest_request_binding(
    manifest: MigrationManifest,
    request: MigrationRequest,
) -> None:
    if manifest.request_id != request.request_id:
        raise PolicyViolation("manifest request_id does not match the migration request")
    if manifest.platform is not request.platform:
        raise PolicyViolation("manifest platform does not match the migration request")
    if manifest.base_revision != request.base_revision:
        raise PolicyViolation("manifest is stale for the requested repository revision")
    outputs = {
        path for transformation in manifest.transformations for path in transformation.output_paths
    }
    if outputs != set(manifest.approved_paths):
        raise PolicyViolation(
            "manifest approved paths must exactly equal its transformation outputs"
        )


class MigrationWorkflow:
    """Facade around the compiled graph with safe start/resume operations."""

    def __init__(
        self,
        architect: Architect,
        engineer: Engineer,
        validator: Validator,
        *,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        maximum_execution_attempts: int = 2,
    ) -> None:
        if maximum_execution_attempts not in {1, 2}:
            raise ValueError("maximum_execution_attempts must be 1 or 2")
        self._architect = architect
        self._engineer = engineer
        self._validator = validator
        self._maximum_execution_attempts = maximum_execution_attempts
        self.checkpointer = checkpointer or InMemorySaver()
        self.graph = self._compile()

    def _compile(self) -> Any:
        builder = StateGraph(WorkflowState)
        builder.add_node("architect", self._architect_node)
        builder.add_node("decision_required", self._decision_required_node)
        builder.add_node("approval_gate", self._approval_node)
        builder.add_node("engineer", self._engineer_node)
        builder.add_node("validator", self._validator_node)
        builder.add_node("rejected", self._rejected_node)
        builder.add_node("modification_requested", self._modify_node)

        builder.add_edge(START, "architect")
        builder.add_conditional_edges(
            "architect",
            self._route_architect,
            {
                WorkflowStatus.AWAITING_APPROVAL: "approval_gate",
                WorkflowStatus.DECISION_REQUIRED: "decision_required",
            },
        )
        builder.add_conditional_edges(
            "approval_gate",
            self._route_approval,
            {
                ApprovalSelection.APPROVE: "engineer",
                ApprovalSelection.REJECT: "rejected",
                ApprovalSelection.MODIFY: "modification_requested",
            },
        )
        builder.add_conditional_edges(
            "engineer",
            self._route_engineer,
            {
                WorkflowStatus.VALIDATING: "validator",
                WorkflowStatus.DECISION_REQUIRED: "decision_required",
            },
        )
        builder.add_edge("validator", END)
        builder.add_edge("decision_required", END)
        builder.add_edge("rejected", END)
        builder.add_edge("modification_requested", END)
        return builder.compile(checkpointer=self.checkpointer)

    @staticmethod
    def _config(thread_id: str) -> dict[str, dict[str, str]]:
        normalized = thread_id.strip()
        if not normalized:
            raise ValueError("thread_id must be non-empty")
        return {"configurable": {"thread_id": normalized}}

    def start(self, request: MigrationRequest, *, thread_id: str) -> Any:
        """Start a fresh workflow and return its LangGraph v2 output.

        A thread ID cannot be reused. This prevents an input from being merged
        into a prior request's checkpoint history.
        """

        config = self._config(thread_id)
        existing = self.graph.get_state(config)
        if existing.values or existing.next:
            raise PolicyViolation("thread_id already has workflow state")
        return self.graph.invoke({"request": request}, config=config, version="v2")

    def resume(
        self,
        approval: ManifestApproval | Mapping[str, Any],
        *,
        thread_id: str,
    ) -> Any:
        """Resume the pending approval interrupt on the same exact thread."""

        if approval is None:
            # Kept explicit even though the annotation excludes None: passing a
            # null resume value is unsafe because it is not a human decision.
            raise ValueError("approval cannot be None")
        config = self._config(thread_id)
        snapshot = self.graph.get_state(config)
        pending_task = next(
            (task for task in snapshot.tasks if task.name == "approval_gate" and task.interrupts),
            None,
        )
        pending_approval = pending_task is not None
        if not pending_approval:
            raise PolicyViolation("thread does not have a pending manifest approval interrupt")

        parsed_approval = (
            approval
            if isinstance(approval, ManifestApproval)
            else ManifestApproval.model_validate(dict(approval))
        )
        interrupt_value = cast(Any, pending_task).interrupts[0].value
        if interrupt_value.get("kind") != "manifest_approval":
            raise PolicyViolation("thread is paused at an unknown interrupt")
        decision = DecisionRequest.model_validate(interrupt_value["decision"])
        manifest = cast(MigrationManifest, snapshot.values["manifest"])
        current_digest = manifest_digest(manifest)
        self._validate_approval(
            parsed_approval,
            decision,
            manifest,
            current_digest,
        )

        # Validate before invoking Command(resume=...). A resume value that
        # raises inside a node is recorded as a failed task by LangGraph; doing
        # the boundary check first preserves the pending interrupt for retry.
        payload = parsed_approval.model_dump(mode="json")
        return self.graph.invoke(
            Command(resume=payload),
            config=config,
            version="v2",
        )

    def snapshot(self, *, thread_id: str) -> Any:
        """Return the current immutable checkpoint snapshot for inspection."""

        return self.graph.get_state(self._config(thread_id))

    def continue_local_failure(self, *, thread_id: str) -> Any:
        """Retry a checkpointed local Engineer or Validator task.

        This path exists for process crashes or failed deterministic local
        nodes after the manifest approval has already been consumed.  It never
        supplies or bypasses an interrupt value, and it cannot replay a
        terminal run.  Role implementations remain responsible for making
        their disposable local work idempotent.
        """

        config = self._config(thread_id)
        snapshot = self.graph.get_state(config)
        if any(task.interrupts for task in snapshot.tasks):
            raise PolicyViolation("an interrupted workflow must resume with an exact decision")
        pending = tuple(snapshot.next)
        if not pending:
            raise PolicyViolation("workflow has no pending local task to continue")
        if not set(pending) <= {"engineer", "validator"}:
            raise PolicyViolation("workflow is not paused at a retryable local task")
        return self.graph.invoke(None, config=config, version="v2")

    def retry_recoverable(
        self,
        approval: CorrectionApproval | Mapping[str, Any],
        *,
        thread_id: str,
    ) -> Any:
        """Authorize and execute exactly one checkpointed local attempt 2.

        The terminal attempt-1 report must classify the failure as recoverable.
        Before either role executes, the exact human approval is written into a
        new checkpoint whose only pending node is Engineer.  A process restart
        may resubmit only the byte-equivalent approval and continue that same
        pending Engineer/Validator task.  No third attempt is routable.
        """

        parsed = (
            approval
            if isinstance(approval, CorrectionApproval)
            else CorrectionApproval.model_validate(dict(approval))
        )
        if self._maximum_execution_attempts == 1:
            raise PolicyViolation("this workflow capability profile disables correction retries")
        config = self._config(thread_id)
        snapshot = self.graph.get_state(config)
        if any(task.interrupts for task in snapshot.tasks):
            raise PolicyViolation("manifest approval must complete before correction")

        state = cast(WorkflowState, snapshot.values)
        request = cast(MigrationRequest, state.get("request"))
        manifest = cast(MigrationManifest, state.get("manifest"))
        change_set = cast(ChangeSet, state.get("change_set"))
        report = cast(ValidationReport, state.get("validation_report"))
        if not all((request, manifest, change_set, report)):
            raise PolicyViolation("workflow lacks terminal evidence for a correction retry")
        validate_manifest_for_request(manifest, request)
        validate_change_set(change_set, manifest)
        validate_report(report, manifest, change_set)
        current_digest = manifest_digest(manifest)
        if state.get("manifest_digest") != current_digest:
            raise PolicyViolation("checkpointed manifest digest does not match manifest")

        expected = CorrectionController(maximum_attempts=self._maximum_execution_attempts).evaluate(
            manifest,
            change_set,
            report,
        )
        recorded_request = state.get("correction_request")
        if recorded_request is not None:
            if CorrectionRequest.model_validate(recorded_request) != expected:
                raise PolicyViolation("checkpointed correction request is not bound to the report")
        self._validate_correction_approval(parsed, expected)
        correction_evidence = CorrectionAttemptEvidence.freeze(manifest, change_set, report)
        if correction_evidence.correction_request != expected:
            raise PolicyViolation("correction evidence is not bound to the terminal report")

        recorded_approval = state.get("correction_approval")
        if recorded_approval is None:
            if snapshot.next:
                raise PolicyViolation("workflow has an unrelated pending local task")
            if report.attempt != 1:
                raise PolicyViolation("only terminal attempt 1 can authorize attempt 2")
            self.graph.update_state(
                config,
                {
                    "correction_request": expected.model_dump(mode="json"),
                    "correction_approval": parsed.model_dump(mode="json"),
                    "correction_attempt_evidence": correction_evidence.model_dump(mode="json"),
                    "execution_attempt": parsed.authorized_attempt,
                    "status": WorkflowStatus.IMPLEMENTING,
                    "terminal_reason": (
                        "Human authorized the exact bounded local correction attempt."
                    ),
                },
                as_node="approval_gate",
            )
            snapshot = self.graph.get_state(config)
        else:
            if CorrectionApproval.model_validate(recorded_approval) != parsed:
                raise PolicyViolation(
                    "retry arguments differ from the checkpointed correction approval"
                )
            try:
                recorded_evidence = CorrectionAttemptEvidence.model_validate(
                    state["correction_attempt_evidence"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PolicyViolation("checkpointed retry lacks exact correction evidence") from exc
            if recorded_evidence != correction_evidence:
                raise PolicyViolation(
                    "checkpointed correction evidence differs from terminal evidence"
                )

        pending = tuple(snapshot.next)
        if not pending:
            raise PolicyViolation("bounded correction retry has already completed")
        if not set(pending) <= {"engineer", "validator"}:
            raise PolicyViolation("workflow is not paused at a correction task")
        return self.graph.invoke(None, config=config, version="v2")

    def _architect_node(self, state: WorkflowState) -> WorkflowState:
        request = state["request"]
        outcome = self._architect(request)
        if isinstance(outcome, PlanningIntervention):
            intervention = PlanningIntervention.model_validate(outcome.model_dump(mode="python"))
            validate_planning_intervention_for_request(intervention, request)
            return {
                "planning_intervention": intervention,
                "planning_intervention_digest": artifact_digest(intervention),
                "status": WorkflowStatus.DECISION_REQUIRED,
            }

        manifest = MigrationManifest.model_validate(outcome.model_dump(mode="python"))
        _validate_manifest_request_binding(manifest, request)
        if manifest.status is ManifestStatus.DECISION_REQUIRED:
            return {
                "manifest": manifest,
                "manifest_digest": manifest_digest(manifest),
                "status": WorkflowStatus.DECISION_REQUIRED,
            }
        validate_manifest_for_request(manifest, request)
        return {
            "manifest": manifest,
            "manifest_digest": manifest_digest(manifest),
            "status": WorkflowStatus.AWAITING_APPROVAL,
        }

    @staticmethod
    def _route_architect(state: WorkflowState) -> str:
        status = state["status"]
        if status not in {
            WorkflowStatus.AWAITING_APPROVAL,
            WorkflowStatus.DECISION_REQUIRED,
        }:
            raise PolicyViolation("Architect produced an unroutable planning status")
        return status

    @staticmethod
    def _decision_required_node(state: WorkflowState) -> WorkflowState:
        request = state["request"]
        implementation = state.get("implementation_intervention")
        intervention = state.get("planning_intervention")
        manifest = state.get("manifest")
        if implementation is not None:
            if intervention is not None or manifest is None:
                raise PolicyViolation(
                    "implementation decision state has conflicting planning outcomes"
                )
            validate_implementation_intervention_for_manifest(
                implementation,
                request,
                manifest,
            )
            digest = artifact_digest(implementation)
            if state.get("implementation_intervention_digest") != digest:
                raise PolicyViolation(
                    "checkpointed implementation intervention digest does not match"
                )
            decision = implementation_intervention_decision_request(
                request,
                manifest,
                implementation,
                digest,
            )
            return {
                "decision_request": decision,
                "status": WorkflowStatus.DECISION_REQUIRED,
                "terminal_disposition": ValidationDisposition.DECISION_REQUIRED,
                "terminal_reason": decision.reason,
            }
        if (intervention is None) == (manifest is None):
            raise PolicyViolation(
                "decision-required planning state must contain exactly one outcome"
            )
        if intervention is not None:
            validate_planning_intervention_for_request(intervention, request)
            digest = artifact_digest(intervention)
            if state.get("planning_intervention_digest") != digest:
                raise PolicyViolation("checkpointed planning intervention digest does not match")
            decision = planning_intervention_decision_request(
                request,
                intervention,
                digest,
            )
        else:
            assert manifest is not None
            digest = manifest_digest(manifest)
            if state.get("manifest_digest") != digest:
                raise PolicyViolation("checkpointed manifest digest does not match manifest")
            decision = decision_required_manifest_request(request, manifest, digest)
        return {
            "decision_request": decision,
            "status": WorkflowStatus.DECISION_REQUIRED,
            "terminal_disposition": ValidationDisposition.DECISION_REQUIRED,
            "terminal_reason": decision.reason,
        }

    def _approval_node(self, state: WorkflowState) -> WorkflowState:
        request = state["request"]
        manifest = state["manifest"]
        if manifest.status is not ManifestStatus.PLANNED:
            raise PolicyViolation("only a planned manifest can enter the approval gate")

        # Everything before interrupt is deterministic and side-effect free.
        # LangGraph restarts this node after Command(resume=...).
        current_digest = manifest_digest(manifest)
        if state["manifest_digest"] != current_digest:
            raise PolicyViolation("checkpointed manifest digest does not match manifest")
        decision = manifest_decision_request(request, manifest, current_digest)
        raw_approval = interrupt(
            {
                "kind": "manifest_approval",
                "decision": decision.model_dump(mode="json"),
                "manifest_id": manifest.manifest_id,
                "manifest_digest": current_digest,
            }
        )
        approval = ManifestApproval.model_validate(raw_approval)
        self._validate_approval(approval, decision, manifest, current_digest)
        return {
            "decision_request": decision,
            "approval": approval,
            "approval_outcome": approval.selection,
            "status": (
                WorkflowStatus.IMPLEMENTING
                if approval.selection == ApprovalSelection.APPROVE
                else WorkflowStatus.AWAITING_APPROVAL
            ),
        }

    @staticmethod
    def _validate_approval(
        approval: ManifestApproval,
        decision: DecisionRequest,
        manifest: MigrationManifest,
        digest: str,
    ) -> None:
        if approval.decision_id != decision.decision_id:
            raise PolicyViolation("approval belongs to a different decision")
        if approval.request_id != decision.request_id:
            raise PolicyViolation("approval belongs to a different migration request")
        if approval.manifest_id != manifest.manifest_id:
            raise PolicyViolation("approval belongs to a different manifest")
        if approval.manifest_digest != digest:
            raise PolicyViolation("approval manifest digest does not match")
        if approval.requested_action is not decision.requested_action:
            raise PolicyViolation("approval authorizes a different action")

    @staticmethod
    def _route_approval(state: WorkflowState) -> str:
        return state["approval_outcome"]

    def _engineer_node(self, state: WorkflowState) -> WorkflowState:
        request = state["request"]
        manifest = state["manifest"]
        approval = state["approval"]
        digest = manifest_digest(manifest)
        decision = state["decision_request"]

        validate_manifest_for_request(manifest, request)
        self._validate_approval(approval, decision, manifest, digest)
        if approval.selection != ApprovalSelection.APPROVE:
            raise PolicyViolation("Engineer cannot run without an approve decision")
        if state["manifest_digest"] != digest:
            raise PolicyViolation("manifest changed after approval")

        attempt = int(state.get("execution_attempt", 1))
        previous_change_set = state.get("change_set")
        correction_evidence = None
        if attempt > 1:
            correction_evidence = self._validate_checkpointed_retry(
                state,
                request,
                manifest,
                attempt,
            )
            if correction_evidence.prior_change_set != previous_change_set:
                raise PolicyViolation(
                    "correction evidence does not match the checkpointed prior change set"
                )
            if correction_evidence.prior_validation_report != state.get("validation_report"):
                raise PolicyViolation(
                    "correction evidence does not match the checkpointed prior report"
                )
        outcome = self._invoke_attempt_role(
            self._engineer,
            (request, manifest),
            attempt,
            role_name="Engineer",
            correction=correction_evidence,
        )
        if isinstance(outcome, ImplementationIntervention):
            intervention = ImplementationIntervention.model_validate(
                outcome.model_dump(mode="python")
            )
            validate_implementation_intervention_for_manifest(
                intervention,
                request,
                manifest,
            )
            return {
                "implementation_intervention": intervention,
                "implementation_intervention_digest": artifact_digest(intervention),
                "status": WorkflowStatus.DECISION_REQUIRED,
            }
        change_set = ChangeSet.model_validate(outcome)
        validate_change_set(change_set, manifest)
        if attempt > 1:
            assert previous_change_set is not None
            if change_set.changed_paths != previous_change_set.changed_paths:
                raise PolicyViolation("correction attempt changed the previously approved path set")
        return {
            "change_set": change_set,
            "status": WorkflowStatus.VALIDATING,
        }

    @staticmethod
    def _route_engineer(state: WorkflowState) -> str:
        status = state["status"]
        if status not in {
            WorkflowStatus.VALIDATING,
            WorkflowStatus.DECISION_REQUIRED,
        }:
            raise PolicyViolation("Engineer produced an unroutable implementation status")
        return status

    def _validator_node(self, state: WorkflowState) -> WorkflowState:
        request = state["request"]
        manifest = state["manifest"]
        change_set = state["change_set"]
        attempt = int(state.get("execution_attempt", 1))
        correction_evidence = None
        if attempt > 1:
            correction_evidence = self._validate_checkpointed_retry(
                state,
                request,
                manifest,
                attempt,
            )
            if correction_evidence.prior_validation_report != state.get("validation_report"):
                raise PolicyViolation(
                    "correction evidence does not match the checkpointed prior report"
                )
        report = self._invoke_attempt_role(
            self._validator,
            (request, manifest, change_set),
            attempt,
            role_name="Validator",
            correction=correction_evidence,
        )
        validate_report(report, manifest, change_set)
        if report.attempt != attempt:
            raise PolicyViolation("Validator report does not match the execution attempt")
        correction = CorrectionController(
            maximum_attempts=self._maximum_execution_attempts
        ).evaluate(
            manifest,
            change_set,
            report,
        )
        lifecycle_event(
            "correction.classified",
            correction_id=correction.correction_id,
            attempt=attempt,
            disposition=report.disposition.value,
            action=correction.action.value,
            next_attempt=correction.next_attempt,
            maximum_attempts=correction.maximum_attempts,
            failed_signal_count=len(correction.failed_check_ids),
            failed_signals=",".join(correction.failed_check_ids) or "none",
            retry_available=correction.action is CorrectionAction.RETRY_IMPLEMENTATION,
        )
        return {
            "validation_report": report,
            "correction_request": correction.model_dump(mode="json"),
            "status": WorkflowStatus.COMPLETED,
            "terminal_disposition": report.disposition,
            "terminal_reason": correction.reason,
        }

    @staticmethod
    def _invoke_attempt_role(
        role: Any,
        positional: tuple[Any, ...],
        attempt: int,
        *,
        role_name: str,
        correction: CorrectionAttemptEvidence | None = None,
    ) -> Any:
        if attempt == 1:
            if correction is not None:
                raise PolicyViolation(f"{role_name} attempt one cannot receive correction evidence")
            return role(*positional)
        if correction is None:
            raise PolicyViolation(f"{role_name} attempt two requires correction evidence")
        try:
            inspect.signature(role).bind(
                *positional,
                attempt=attempt,
                correction=correction,
            )
        except (TypeError, ValueError) as exc:
            raise PolicyViolation(
                f"{role_name} does not support explicit evidence-bound correction"
            ) from exc
        return role(*positional, attempt=attempt, correction=correction)

    @staticmethod
    def _validate_correction_approval(
        approval: CorrectionApproval,
        request: CorrectionRequest,
    ) -> None:
        if request.action is not CorrectionAction.RETRY_IMPLEMENTATION:
            if request.action is CorrectionAction.REPLAN_WITH_NEW_APPROVAL:
                raise PolicyViolation(
                    "plan-invalid evidence requires a new manifest digest and approval"
                )
            raise PolicyViolation("terminal evidence does not authorize a correction retry")
        expected = {
            "correction_id": request.correction_id,
            "request_id": request.request_id,
            "manifest_id": request.manifest_id,
            "manifest_digest": request.manifest_digest,
            "report_id": request.report_id,
            "report_digest": request.report_digest,
            "change_set_digest": request.change_set_digest,
            "base_revision": request.base_revision,
            "completed_attempt": request.completed_attempt,
            "authorized_attempt": request.next_attempt,
            "action": request.action,
        }
        for field, value in expected.items():
            if getattr(approval, field) != value:
                raise PolicyViolation(f"correction approval {field} does not match")

    @classmethod
    def _validate_checkpointed_retry(
        cls,
        state: WorkflowState,
        migration_request: MigrationRequest,
        manifest: MigrationManifest,
        attempt: int,
    ) -> CorrectionAttemptEvidence:
        try:
            request = CorrectionRequest.model_validate(state["correction_request"])
            approval = CorrectionApproval.model_validate(state["correction_approval"])
            evidence = CorrectionAttemptEvidence.model_validate(
                state["correction_attempt_evidence"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyViolation(
                "correction task lacks exact checkpointed approval and evidence"
            ) from exc
        cls._validate_correction_approval(approval, request)
        validate_correction_attempt_evidence(evidence, migration_request, manifest)
        if evidence.correction_request != request:
            raise PolicyViolation("correction evidence differs from the checkpointed request")
        if approval.authorized_attempt != attempt:
            raise PolicyViolation("correction approval belongs to another attempt")
        if request.manifest_digest != artifact_digest(manifest):
            raise PolicyViolation("correction approval belongs to different manifest bytes")
        return evidence

    @staticmethod
    def _rejected_node(state: WorkflowState) -> WorkflowState:
        approval = state["approval"]
        return {
            "status": WorkflowStatus.REJECTED,
            "terminal_disposition": ValidationDisposition.PLAN_INVALID,
            "terminal_reason": approval.comment or "Human reviewer rejected the manifest.",
        }

    @staticmethod
    def _modify_node(state: WorkflowState) -> WorkflowState:
        approval = state["approval"]
        return {
            "status": WorkflowStatus.MODIFICATION_REQUESTED,
            "terminal_disposition": ValidationDisposition.DECISION_REQUIRED,
            "terminal_reason": (
                approval.comment or "Human reviewer requested manifest modifications."
            ),
        }


def build_workflow(
    architect: Architect,
    engineer: Engineer,
    validator: Validator,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    maximum_execution_attempts: int = 2,
) -> MigrationWorkflow:
    """Build a checkpointed three-role workflow."""

    return MigrationWorkflow(
        architect,
        engineer,
        validator,
        checkpointer=checkpointer,
        maximum_execution_attempts=maximum_execution_attempts,
    )
