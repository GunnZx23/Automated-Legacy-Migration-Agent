"""Bounded, evidence-bound classification of validation correction outcomes."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Final, Literal

from pydantic import Field, model_validator

from legacy_migration_agent.contracts import (
    ChangeSet,
    CheckStatus,
    Identifier,
    MigrationManifest,
    MigrationRequest,
    Sha256Digest,
    StrictModel,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import (
    PolicyViolation,
    RetryBudget,
    validate_change_set,
    validate_report,
)


class CorrectionAction(StrEnum):
    COMPLETE = "complete"
    RETRY_IMPLEMENTATION = "retry_implementation"
    REPLAN_WITH_NEW_APPROVAL = "replan_with_new_approval"
    STOP_ENVIRONMENT = "stop_environment"
    STOP_EXHAUSTED = "stop_exhausted"


# These checks attest controller-owned tooling or source integrity. Their
# evidence remains in the full report and public correction request, but a
# manifest-scoped Engineer cannot repair them by changing generated outputs.
CONTROLLER_ENVIRONMENT_COMMAND_IDS: Final[frozenset[str]] = frozenset(
    {
        "salesforce-toolchain-contract",
        "salesforce-jest-sandbox-probe",
        "mulesoft-toolchain-contract",
    }
)
CONTROLLER_INTEGRITY_COMMAND_IDS: Final[frozenset[str]] = frozenset(
    {
        "salesforce-workspace-fingerprint",
        "mulesoft-workspace-fingerprint",
    }
)
CONTROLLER_OWNED_VALIDATION_COMMAND_IDS: Final[frozenset[str]] = frozenset(
    {*CONTROLLER_ENVIRONMENT_COMMAND_IDS, *CONTROLLER_INTEGRITY_COMMAND_IDS}
)

_SALESFORCE_CANDIDATE_COMMAND_ID: Final = "salesforce-candidate-contract"
_NON_ENGINEER_REPAIRABLE_CANDIDATE_DIAGNOSTIC_IDS: Final[frozenset[str]] = frozenset(
    {
        "salesforce_candidate_inventory",
        "salesforce_candidate_unclassified",
    }
)
_LWC_LOAD_FAILURE_DIAGNOSTIC_IDS: Final[frozenset[str]] = frozenset(
    {
        "salesforce_lwc_javascript_contract",
        "salesforce_lwc_template_contract",
    }
)
_ZERO_TEST_LOAD_FAILURE_SUMMARY: Final = "suites=1 tests=0 failed-suites=1 failed-tests=0"
_JEST_LOAD_DEPENDENCIES: Final[tuple[tuple[str, str], ...]] = (
    ("salesforce-lwc-jest", "candidate_jest_execution_failure"),
    ("salesforce-lwc-controller-jest", "controller_jest_execution_failure"),
)


def validation_failure_dependencies(
    report: ValidationReport,
) -> dict[Identifier, Identifier]:
    """Identify one exact LWC load-failure cascade without changing its report.

    Candidate and controller Jest are presentation/correction dependants only
    when both suites failed before running a test and an earlier static LWC
    JavaScript or template contract failed. A normal Jest assertion failure,
    partial test execution, extra runtime diagnostic, missing receipt, or
    different ordering remains an independent failure.
    """

    results = report.results
    for root_index, root in enumerate(results):
        root_diagnostics = set(root.diagnostic_ids)
        if (
            not root.required
            or root.status is not CheckStatus.FAILED
            or root.command_id != _SALESFORCE_CANDIDATE_COMMAND_ID
            or root.receipt is None
            or root.receipt.exit_code == 0
            or not (root_diagnostics & _LWC_LOAD_FAILURE_DIAGNOSTIC_IDS)
            or not any(
                f"failure-code={diagnostic_id}" in root.summary
                for diagnostic_id in root_diagnostics & _LWC_LOAD_FAILURE_DIAGNOSTIC_IDS
            )
        ):
            continue

        dependencies: dict[Identifier, Identifier] = {}
        for command_id, diagnostic_id in _JEST_LOAD_DEPENDENCIES:
            matches = tuple(
                result
                for result in results[root_index + 1 :]
                if (
                    result.required
                    and result.status is CheckStatus.FAILED
                    and result.command_id == command_id
                    and result.receipt is not None
                    and result.receipt.exit_code != 0
                    and result.diagnostic_ids == (diagnostic_id,)
                    and _ZERO_TEST_LOAD_FAILURE_SUMMARY in result.summary
                )
            )
            if len(matches) != 1:
                break
            dependencies[matches[0].check_id] = root.check_id
        else:
            return dependencies
    return {}


def correction_failure_ids(report: ValidationReport) -> tuple[Identifier, ...]:
    """Project actionable root signals while retaining all raw report results."""

    dependent_check_ids = validation_failure_dependencies(report).keys()
    return tuple(
        dict.fromkeys(
            signal_id
            for result in report.results
            if (
                result.required
                and result.status is not CheckStatus.PASSED
                and result.check_id not in dependent_check_ids
            )
            for signal_id in (result.check_id, *result.diagnostic_ids)
        )
    )


def implementation_failure_ids(report: ValidationReport) -> tuple[Identifier, ...]:
    """Project only terminal failures that approved output changes can repair."""

    dependent_check_ids = validation_failure_dependencies(report).keys()
    return tuple(
        dict.fromkeys(
            signal_id
            for result in report.results
            if (
                result.required
                and result.status is CheckStatus.FAILED
                and result.command_id not in CONTROLLER_OWNED_VALIDATION_COMMAND_IDS
                and result.check_id not in dependent_check_ids
                and not (
                    result.command_id == _SALESFORCE_CANDIDATE_COMMAND_ID
                    and bool(result.diagnostic_ids)
                    and set(result.diagnostic_ids).issubset(
                        _NON_ENGINEER_REPAIRABLE_CANDIDATE_DIAGNOSTIC_IDS
                    )
                )
            )
            for signal_id in (result.check_id, *result.diagnostic_ids)
        )
    )


class CorrectionRequest(StrictModel):
    """Controller output bound to one exact failed validation attempt."""

    correction_id: Identifier
    request_id: Identifier
    manifest_id: Identifier
    change_set_id: Identifier
    report_id: Identifier
    base_revision: str
    manifest_digest: Sha256Digest
    report_digest: Sha256Digest
    change_set_digest: Sha256Digest
    failed_check_ids: tuple[Identifier, ...]
    completed_attempt: int = Field(ge=1)
    next_attempt: int | None = Field(default=None, ge=2)
    maximum_attempts: int = Field(ge=1)
    action: CorrectionAction
    requires_new_manifest_approval: bool
    requires_new_manifest_digest: bool
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_action_contract(self) -> CorrectionRequest:
        if self.action is CorrectionAction.COMPLETE:
            if self.failed_check_ids or self.next_attempt is not None:
                raise ValueError("complete correction outcome cannot contain failures or retry")
        elif not self.failed_check_ids:
            raise ValueError("non-complete correction outcomes require failed checks")
        if self.action is CorrectionAction.RETRY_IMPLEMENTATION:
            if self.next_attempt != self.completed_attempt + 1:
                raise ValueError("implementation retry must advance exactly one attempt")
            if self.requires_new_manifest_approval:
                raise ValueError(
                    "same-manifest implementation retry does not require a new approval"
                )
        elif self.next_attempt is not None:
            raise ValueError("only implementation retry can select a next attempt")
        if (
            self.action is CorrectionAction.REPLAN_WITH_NEW_APPROVAL
        ) is not self.requires_new_manifest_approval:
            raise ValueError("replanning is the only action that requires new manifest approval")
        if self.requires_new_manifest_digest is not self.requires_new_manifest_approval:
            raise ValueError("a new manifest approval must bind a new manifest digest")
        return self


class CorrectionApproval(StrictModel):
    """Explicit human authorization for one exact bounded retry.

    This approval cannot authorize a replan, scope expansion, external action,
    or a third attempt.  It is deliberately bound to the previous terminal
    evidence and to the already approved manifest bytes.
    """

    correction_id: Identifier
    request_id: Identifier
    manifest_id: Identifier
    manifest_digest: Sha256Digest
    report_id: Identifier
    report_digest: Sha256Digest
    change_set_digest: Sha256Digest
    base_revision: str
    completed_attempt: int = Field(ge=1)
    authorized_attempt: int = Field(ge=2)
    action: CorrectionAction
    reviewer: Identifier
    comment: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_retry_only(self) -> CorrectionApproval:
        if self.action is not CorrectionAction.RETRY_IMPLEMENTATION:
            raise ValueError("a correction approval can authorize only implementation retry")
        if self.authorized_attempt != self.completed_attempt + 1:
            raise ValueError("a correction approval must advance exactly one attempt")
        return self


class CorrectionController:
    """Classify a terminal attempt without silently extending scope or budget."""

    def __init__(self, *, maximum_attempts: int = 2) -> None:
        self.budget = RetryBudget(maximum_attempts=maximum_attempts)

    def evaluate(
        self,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        report: ValidationReport,
    ) -> CorrectionRequest:
        validate_report(report, manifest, change_set)
        implementation_failures = implementation_failure_ids(report)
        failed = correction_failure_ids(report)
        if report.disposition is ValidationDisposition.READY_FOR_HUMAN_REVIEW:
            action = CorrectionAction.COMPLETE
            next_attempt = None
            reason = "All required checks passed terminally; continue to human review."
        elif any(
            result.required
            and result.status is not CheckStatus.PASSED
            and result.command_id in CONTROLLER_INTEGRITY_COMMAND_IDS
            for result in report.results
        ):
            action = CorrectionAction.REPLAN_WITH_NEW_APPROVAL
            next_attempt = None
            reason = (
                "Controller-owned source-integrity evidence failed; bind a new base revision, "
                "produce a new manifest digest, and obtain exact approval before implementation."
            )
        elif any(
            result.required
            and result.status is CheckStatus.FAILED
            and result.command_id == _SALESFORCE_CANDIDATE_COMMAND_ID
            and bool(set(result.diagnostic_ids) & _NON_ENGINEER_REPAIRABLE_CANDIDATE_DIAGNOSTIC_IDS)
            for result in report.results
        ):
            action = CorrectionAction.REPLAN_WITH_NEW_APPROVAL
            next_attempt = None
            reason = (
                "The generated-candidate inventory or validation stage is structurally invalid; "
                "do not offer an Engineer retry that cannot repair the approved output boundary."
            )
        elif report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE:
            action = CorrectionAction.STOP_ENVIRONMENT
            next_attempt = None
            reason = (
                "A required execution environment or tool is unavailable; do not "
                "consume an implementation retry or claim success."
            )
        elif report.disposition in {
            ValidationDisposition.PLAN_INVALID,
            ValidationDisposition.DECISION_REQUIRED,
        }:
            action = CorrectionAction.REPLAN_WITH_NEW_APPROVAL
            next_attempt = None
            reason = (
                "The current manifest cannot safely authorize another implementation; "
                "produce and approve a new manifest digest."
            )
        elif report.disposition is ValidationDisposition.RECOVERABLE_FAILURE:
            if not implementation_failures:
                action = CorrectionAction.STOP_ENVIRONMENT
                next_attempt = None
                reason = (
                    "Only controller-owned environment or toolchain evidence failed; do not "
                    "send those checks to the manifest-scoped Engineer or consume a retry."
                )
            else:
                try:
                    self.budget.require_remaining(report.attempt)
                except PolicyViolation:
                    action = CorrectionAction.STOP_EXHAUSTED
                    next_attempt = None
                    reason = (
                        "The bounded implementation retry budget is exhausted; stop for "
                        "human diagnosis."
                    )
                else:
                    action = CorrectionAction.RETRY_IMPLEMENTATION
                    next_attempt = report.attempt + 1
                    reason = (
                        "A same-manifest implementation correction may run once more against "
                        "the exact approved paths and base revision."
                    )
        else:  # pragma: no cover - enum exhaustiveness guard
            raise AssertionError(f"unhandled disposition: {report.disposition}")

        material = "\x00".join(
            (
                report.report_id,
                artifact_digest(report),
                action.value,
                str(report.attempt),
            )
        )
        return CorrectionRequest(
            correction_id=f"correction-{hashlib.sha256(material.encode()).hexdigest()[:24]}",
            request_id=report.request_id,
            manifest_id=report.manifest_id,
            change_set_id=report.change_set_id,
            report_id=report.report_id,
            base_revision=report.base_revision,
            manifest_digest=artifact_digest(manifest),
            report_digest=artifact_digest(report),
            change_set_digest=artifact_digest(change_set),
            failed_check_ids=failed,
            completed_attempt=report.attempt,
            next_attempt=next_attempt,
            maximum_attempts=self.budget.maximum_attempts,
            action=action,
            requires_new_manifest_approval=(action is CorrectionAction.REPLAN_WITH_NEW_APPROVAL),
            requires_new_manifest_digest=(action is CorrectionAction.REPLAN_WITH_NEW_APPROVAL),
            reason=reason,
        )

    @staticmethod
    def approve_retry(
        request: CorrectionRequest,
        *,
        presented_correction_id: str,
        reviewer: str,
        comment: str = "",
    ) -> CorrectionApproval:
        """Create an exact approval only for a currently offered retry."""

        if presented_correction_id != request.correction_id:
            raise PolicyViolation("presented correction_id does not match the retry request")
        if request.action is not CorrectionAction.RETRY_IMPLEMENTATION:
            if request.action is CorrectionAction.REPLAN_WITH_NEW_APPROVAL:
                raise PolicyViolation(
                    "plan-invalid evidence requires a new manifest digest and approval"
                )
            raise PolicyViolation("the correction outcome does not authorize a retry")
        if request.next_attempt is None:
            raise PolicyViolation("the correction outcome does not select a next attempt")
        return CorrectionApproval(
            correction_id=request.correction_id,
            request_id=request.request_id,
            manifest_id=request.manifest_id,
            manifest_digest=request.manifest_digest,
            report_id=request.report_id,
            report_digest=request.report_digest,
            change_set_digest=request.change_set_digest,
            base_revision=request.base_revision,
            completed_attempt=request.completed_attempt,
            authorized_attempt=request.next_attempt,
            action=request.action,
            reviewer=reviewer,
            comment=comment,
        )


class CorrectionAttemptEvidence(StrictModel):
    """Exact controller-owned evidence supplied to bounded attempt two.

    The artifact deliberately excludes the human reviewer's identity and
    comment.  Those fields authorize the transition; they are not technical
    correction evidence and must never influence generated source.
    """

    evidence_id: Identifier
    request_id: Identifier
    manifest: MigrationManifest
    manifest_digest: Sha256Digest
    prior_change_set: ChangeSet
    prior_change_set_digest: Sha256Digest
    prior_validation_report: ValidationReport
    prior_validation_report_digest: Sha256Digest
    correction_request: CorrectionRequest
    correction_request_digest: Sha256Digest
    completed_attempt: Literal[1]
    authorized_attempt: Literal[2]
    evidence_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_evidence(self) -> CorrectionAttemptEvidence:
        if self.request_id != self.manifest.request_id:
            raise ValueError("correction evidence belongs to another request")
        if self.manifest_digest != artifact_digest(self.manifest):
            raise ValueError("correction manifest digest does not match its content")
        if self.prior_change_set_digest != artifact_digest(self.prior_change_set):
            raise ValueError("prior change-set digest does not match its content")
        if self.prior_validation_report_digest != artifact_digest(self.prior_validation_report):
            raise ValueError("prior validation-report digest does not match its content")
        if self.correction_request_digest != artifact_digest(self.correction_request):
            raise ValueError("correction-request digest does not match its content")
        validate_change_set(self.prior_change_set, self.manifest)
        validate_report(
            self.prior_validation_report,
            self.manifest,
            self.prior_change_set,
        )
        if self.prior_validation_report.attempt != self.completed_attempt:
            raise ValueError("correction evidence must describe completed attempt one")
        if (
            self.prior_validation_report.disposition
            is not ValidationDisposition.RECOVERABLE_FAILURE
        ):
            raise ValueError("attempt two requires a recoverable attempt-one report")
        expected_request = CorrectionController(maximum_attempts=2).evaluate(
            self.manifest,
            self.prior_change_set,
            self.prior_validation_report,
        )
        if self.correction_request != expected_request:
            raise ValueError("correction request does not match the prior evidence")
        if self.correction_request.next_attempt != self.authorized_attempt:
            raise ValueError("correction evidence does not authorize attempt two")
        expected_digest = _correction_attempt_evidence_digest(
            evidence_id=self.evidence_id,
            request_id=self.request_id,
            manifest=self.manifest,
            manifest_digest=self.manifest_digest,
            prior_change_set=self.prior_change_set,
            prior_change_set_digest=self.prior_change_set_digest,
            prior_validation_report=self.prior_validation_report,
            prior_validation_report_digest=self.prior_validation_report_digest,
            correction_request=self.correction_request,
            correction_request_digest=self.correction_request_digest,
            completed_attempt=self.completed_attempt,
            authorized_attempt=self.authorized_attempt,
        )
        if self.evidence_digest != expected_digest:
            raise ValueError("correction evidence digest does not match its content")
        return self

    @classmethod
    def freeze(
        cls,
        manifest: MigrationManifest,
        change_set: ChangeSet,
        report: ValidationReport,
    ) -> CorrectionAttemptEvidence:
        """Create the only correction evidence that can authorize attempt two."""

        correction_request = CorrectionController(maximum_attempts=2).evaluate(
            manifest,
            change_set,
            report,
        )
        if correction_request.action is not CorrectionAction.RETRY_IMPLEMENTATION:
            raise PolicyViolation("terminal evidence does not authorize correction attempt two")
        evidence_id = "retry-evidence-" + correction_request.correction_id.removeprefix(
            "correction-"
        )
        values = {
            "evidence_id": evidence_id,
            "request_id": correction_request.request_id,
            "manifest": manifest,
            "manifest_digest": artifact_digest(manifest),
            "prior_change_set": change_set,
            "prior_change_set_digest": artifact_digest(change_set),
            "prior_validation_report": report,
            "prior_validation_report_digest": artifact_digest(report),
            "correction_request": correction_request,
            "correction_request_digest": artifact_digest(correction_request),
            "completed_attempt": 1,
            "authorized_attempt": 2,
        }
        evidence_digest = _correction_attempt_evidence_digest(**values)
        return cls(
            evidence_id=evidence_id,
            request_id=correction_request.request_id,
            manifest=manifest,
            manifest_digest=artifact_digest(manifest),
            prior_change_set=change_set,
            prior_change_set_digest=artifact_digest(change_set),
            prior_validation_report=report,
            prior_validation_report_digest=artifact_digest(report),
            correction_request=correction_request,
            correction_request_digest=artifact_digest(correction_request),
            completed_attempt=1,
            authorized_attempt=2,
            evidence_digest=evidence_digest,
        )


def validate_correction_attempt_evidence(
    evidence: CorrectionAttemptEvidence,
    request: MigrationRequest,
    manifest: MigrationManifest,
) -> CorrectionAttemptEvidence:
    """Bind frozen correction evidence to the current request and manifest."""

    frozen = CorrectionAttemptEvidence.model_validate(evidence.model_dump(mode="python"))
    if frozen.request_id != request.request_id:
        raise PolicyViolation("correction evidence belongs to another migration request")
    if frozen.manifest != manifest:
        raise PolicyViolation("correction evidence belongs to different manifest bytes")
    if frozen.manifest_digest != artifact_digest(manifest):
        raise PolicyViolation("correction evidence manifest digest does not match")
    if frozen.manifest.base_revision != request.base_revision:
        raise PolicyViolation("correction evidence belongs to another base revision")
    return frozen


def _correction_attempt_evidence_digest(**values: object) -> Sha256Digest:
    """Hash every correction input without recursively hashing its digest."""

    serializable = {
        key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        for key, value in values.items()
    }
    return artifact_digest(serializable)


__all__ = [
    "CorrectionAction",
    "CorrectionApproval",
    "CorrectionAttemptEvidence",
    "CorrectionController",
    "CorrectionRequest",
    "CONTROLLER_ENVIRONMENT_COMMAND_IDS",
    "CONTROLLER_INTEGRITY_COMMAND_IDS",
    "CONTROLLER_OWNED_VALIDATION_COMMAND_IDS",
    "correction_failure_ids",
    "implementation_failure_ids",
    "validation_failure_dependencies",
    "validate_correction_attempt_evidence",
]
