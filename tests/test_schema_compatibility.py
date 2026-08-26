from pathlib import Path

from legacy_migration_agent.contracts import ApprovalAction, ImplementationIntervention
from legacy_migration_agent.schema_compatibility import (
    PUBLIC_SCHEMA_MODELS,
    check_schema_snapshots,
    find_backward_incompatibilities,
    generated_schema,
    schema_filename,
)

PROJECT_ROOT = Path(__file__).parents[1]
SNAPSHOT_ROOT = PROJECT_ROOT / "schemas" / "v1.0"

EXPECTED_PUBLIC_SCHEMA_MODELS = (
    "MigrationRequest",
    "ImplementationIntervention",
    "PlanningIntervention",
    "MigrationManifest",
    "ManifestApproval",
    "ChangeSet",
    "ValidationReport",
    "CorrectionRequest",
    "CorrectionApproval",
    "DecisionRequest",
    "ToolReceipt",
    "AgentDefinition",
    "AgentRunConfig",
    "AgentRunStatus",
    "LiveModelApproval",
    "ModelCallRecord",
    "ArchitectManifestProposal",
    "EngineerModelOutcome",
    "ValidatorAdvisory",
    "DependencyGraph",
    "StoredGraphSnapshot",
    "GraphLabelSet",
    "GraphEvaluationReport",
    "FinalReviewRequest",
    "FinalReviewDecision",
    "FinalReviewRecord",
    "FinalReviewStatus",
    "WikiCatalog",
    "RetrievalTrace",
    "WikiReviewRequest",
    "WikiReviewDecision",
    "WikiPromotionReport",
    "WikiPromotionReceipt",
    "KnowledgePromotionRequest",
    "KnowledgePromotionDecision",
    "KnowledgePromotionRecord",
    "KnowledgeInvalidationRecord",
    "KnowledgeLookupResult",
    "KnowledgeAuditEvent",
    "SalesforceValidationContext",
    "SalesforceValidationEvidence",
    "MuleSoftValidationContext",
    "MUnitLocalResult",
    "MuleSoftValidationEvidence",
    "MuleSoftCandidateValidationSummary",
    "EvaluationRegistry",
    "EvaluationResults",
    "EvaluationVerification",
    "PilotEvaluationRegistry",
    "PilotEvaluationResults",
    "PilotEvidenceReceipt",
    "PilotEvaluationVerification",
)


def test_public_schema_registry_covers_every_frozen_root_contract() -> None:
    assert tuple(model.__name__ for model in PUBLIC_SCHEMA_MODELS) == (
        EXPECTED_PUBLIC_SCHEMA_MODELS
    )


def test_public_schema_snapshots_are_complete_compatible_and_exact() -> None:
    checks = check_schema_snapshots(SNAPSHOT_ROOT)

    assert [check.model_name for check in checks] == [
        model.__name__ for model in PUBLIC_SCHEMA_MODELS
    ]
    assert {check.snapshot_path.name for check in checks} == {
        schema_filename(model) for model in PUBLIC_SCHEMA_MODELS
    }
    incompatible = {
        check.model_name: [issue.describe() for issue in check.compatibility_issues]
        for check in checks
        if check.compatibility_issues
    }
    drifted = [check.model_name for check in checks if not check.exact_match]
    assert incompatible == {}, incompatible
    assert drifted == [], (
        "public contract schema drift requires compatibility review and an intentional "
        f"snapshot refresh: {drifted}"
    )


def test_implementation_intervention_schema_exposes_only_non_authorizing_actions() -> None:
    schema = generated_schema(ImplementationIntervention)

    assert schema["properties"]["requested_action"]["enum"] == [
        ApprovalAction.EXPAND_SCOPE.value,
        ApprovalAction.ACCEPT_HIGH_IMPACT_CHANGE.value,
    ]


def test_compatibility_check_rejects_required_fields_and_narrowed_enums() -> None:
    baseline = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "state": {"type": "string", "enum": ["planned", "approved"]},
            "note": {"type": "string"},
        },
        "required": ["state"],
    }
    current = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "state": {"type": "string", "enum": ["approved"]},
            "note": {"type": "string"},
        },
        "required": ["state", "note"],
    }

    issues = find_backward_incompatibilities(baseline, current)

    assert {(issue.path, issue.rule) for issue in issues} == {
        ("$.properties.note", "required-field-added"),
        ("$.properties.state", "enum-narrowed"),
    }


def test_compatibility_check_accepts_optional_properties_and_wider_enums() -> None:
    baseline = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "state": {"type": "string", "enum": ["planned"]},
        },
        "required": ["state"],
    }
    current = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "state": {"type": "string", "enum": ["planned", "approved"]},
            "note": {"type": "string"},
        },
        "required": ["state"],
    }

    assert find_backward_incompatibilities(baseline, current) == ()


def test_compatibility_check_rejects_removed_property_in_strict_object() -> None:
    baseline = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"request_id": {"type": "string"}},
        "required": ["request_id"],
    }
    current = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }

    issues = find_backward_incompatibilities(baseline, current)

    assert [(issue.path, issue.rule) for issue in issues] == [
        ("$.properties.request_id", "property-removed")
    ]
