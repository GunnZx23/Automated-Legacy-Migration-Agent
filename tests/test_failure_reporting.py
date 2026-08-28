from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from legacy_migration_agent.agent_runtime.model_workflow import _sanitized_role_policy_error
from legacy_migration_agent.application.agent_run_contracts import (
    AgentRunFailure,
    agent_run_failure_explanation,
)
from legacy_migration_agent.core.observability import lifecycle_event, terminal_lifecycle_logging
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.run_session import AgentDefinitionDigests
from legacy_migration_agent.ui.projection import _failure_view

_DIGEST = "sha256:" + "a" * 64
_DEFINITION_DIGESTS = AgentDefinitionDigests(
    architect=_DIGEST,
    engineer=_DIGEST,
    validator=_DIGEST,
)


@pytest.mark.parametrize(
    ("message", "reason_code"),
    (
        (
            "controller-classified correction requires a changed-file Engineer delta",
            "correction_delta_required",
        ),
        (
            "Engineer correction delta contains no material file changes",
            "correction_no_material_changes",
        ),
        (
            "Engineer correction delta does not cover repair signals: secret-signal",
            "correction_signal_coverage_missing",
        ),
        (
            "Engineer correction delta contains paths outside the code-owned repair boundary: "
            "/Users/private/secret.js",
            "correction_scope_invalid",
        ),
        (
            "Engineer correction delta produced an identical attempt-one candidate",
            "correction_identical_candidate",
        ),
        (
            "Engineer file plan scope mismatch (missing: /Users/private/secret.js)",
            "file_plan_scope_mismatch",
        ),
        (
            "Engineer actual filesystem delta does not equal the proposed update paths",
            "file_plan_delta_mismatch",
        ),
        (
            "Engineer workspace scope does not exactly match the manifest",
            "workspace_scope_mismatch",
        ),
        ("Engineer requires a clean isolated workspace", "workspace_not_clean"),
        (
            "attempt-two scope expansion must identify a specifically required path outside "
            "manifest.approved_paths",
            "attempt_two_scope_expansion_invalid",
        ),
    ),
)
def test_engineer_policy_reason_is_durable_logged_and_projected(
    message: str,
    reason_code: str,
) -> None:
    sanitized = _sanitized_role_policy_error("engineer", PolicyViolation(message))
    assert sanitized.reason_code == reason_code
    summary, guidance = agent_run_failure_explanation(sanitized.reason_code, "engineer")
    failure = AgentRunFailure(
        failure_id=f"failure-{reason_code}",
        run_id="run-safe-policy-report",
        thread_id="thread-safe-policy-report",
        request_id="request-safe-policy-report",
        operation="retry",
        seam="engineer",
        category="invalid",
        reason_code=sanitized.reason_code,
        summary=summary,
        guidance=guidance,
        attempt=2,
        request_digest=_DIGEST,
        operation_input_digest=_DIGEST,
        session_context_digest=_DIGEST,
        source_revision=_DIGEST,
        agent_definition_digests=_DEFINITION_DIGESTS,
    )

    durable_json = failure.model_dump_json()
    assert f'"reason_code":"{reason_code}"' in durable_json
    assert summary in durable_json
    assert guidance in durable_json
    assert "/Users/" not in durable_json
    assert "secret.js" not in durable_json
    assert "secret-signal" not in durable_json

    projected = _failure_view(SimpleNamespace(failure=failure))  # type: ignore[arg-type]
    assert projected is not None
    assert projected.reason_code == reason_code
    assert projected.summary == summary
    assert projected.guidance == guidance

    terminal_output = io.StringIO()
    with terminal_lifecycle_logging(stream=terminal_output):
        lifecycle_event(
            "workflow.operation.failed",
            operation="retry",
            attempt=2,
            seam="engineer",
            category="invalid",
            reason_code=reason_code,
            failure_summary=summary,
            failure_guidance=guidance,
        )
    lifecycle_log = terminal_output.getvalue()
    assert f'reason_code="{reason_code}"' in lifecycle_log
    assert "failure_summary=" in lifecycle_log
    assert "failure_guidance=" in lifecycle_log
    assert "/Users/" not in lifecycle_log
    assert "secret.js" not in lifecycle_log
    assert "secret-signal" not in lifecycle_log


def test_unmapped_engineer_rejection_uses_generic_sanitized_fallback() -> None:
    sanitized = _sanitized_role_policy_error(
        "engineer",
        PolicyViolation("unknown rejection with /Users/private/secret.js"),
    )

    assert sanitized.reason_code == "policy_rejected"
    assert str(sanitized) == "model_role_policy_failure:engineer:policy_rejected"
    assert "/Users/" not in str(sanitized)
    assert "secret.js" not in str(sanitized)
