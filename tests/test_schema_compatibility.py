import json
from pathlib import Path

from legacy_migration_agent.agent_runtime.agent_definitions import AgentDefinition
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectManifestProposal,
    ValidatorModelAdvisory,
)
from legacy_migration_agent.benchmark_corpus import BenchmarkCorpusManifest
from legacy_migration_agent.benchmark_execution import BenchmarkExecutionAnchor
from legacy_migration_agent.contracts import ApprovalAction, ImplementationIntervention
from legacy_migration_agent.evaluation import (
    PilotEvaluationRegistry,
    PilotEvaluationResults,
    PilotEvaluationVerification,
    PilotEvidenceReceipt,
)
from legacy_migration_agent.graphs.graph_assurance import GraphAssuranceReport
from legacy_migration_agent.graphs.graph_evaluation import GraphLabelReviewEvidence
from legacy_migration_agent.measured_evaluation import (
    BenchmarkLabelReviewEvidence,
    EvaluationCellReceipt,
    HumanReviewRubric,
    MeasuredEvaluationRegistry,
    MeasuredEvaluationVerification,
    MetricSummary,
)
from legacy_migration_agent.schema_compatibility import (
    PUBLIC_SCHEMA_MODELS,
    PUBLIC_SCHEMA_RELEASE,
    check_schema_snapshots,
    find_backward_incompatibilities,
    generated_schema,
    schema_filename,
)

PROJECT_ROOT = Path(__file__).parents[1]
SNAPSHOT_ROOT = PROJECT_ROOT / "schemas" / PUBLIC_SCHEMA_RELEASE
LEGACY_SNAPSHOT_ROOT = PROJECT_ROOT / "schemas" / "v1.0"

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
    "ValidatorModelAdvisory",
    "DependencyGraph",
    "StoredGraphSnapshot",
    "GraphAssuranceReport",
    "GraphLabelSet",
    "GraphLabelReviewEvidence",
    "GraphEvaluationReport",
    "FinalReviewRequest",
    "FinalReviewDecision",
    "FinalReviewRecord",
    "FinalReviewStatus",
    "ExternalCandidateReviewAttestation",
    "WikiCatalog",
    "RetrievalTrace",
    "MuleSoftValidationContext",
    "MuleSoftValidationEvidence",
    "MuleSoftCandidateValidationSummary",
    "EvaluationRegistry",
    "EvaluationResults",
    "EvaluationVerification",
    "PilotEvaluationRegistry",
    "PilotEvaluationResults",
    "PilotEvidenceReceipt",
    "PilotEvaluationVerification",
    "BenchmarkExecutionAnchor",
    "BenchmarkCorpusManifest",
    "BenchmarkLabelReviewEvidence",
    "MeasuredEvaluationRegistry",
    "HumanReviewRubric",
    "EvaluationCellReceipt",
    "MetricSummary",
    "MeasuredEvaluationVerification",
)
V2_ONLY_PUBLIC_SCHEMA_MODELS = frozenset(
    {
        "ValidatorModelAdvisory",
        "GraphLabelReviewEvidence",
        "GraphAssuranceReport",
        "ExternalCandidateReviewAttestation",
        "BenchmarkExecutionAnchor",
        "BenchmarkCorpusManifest",
        "BenchmarkLabelReviewEvidence",
        "MeasuredEvaluationRegistry",
        "HumanReviewRubric",
        "EvaluationCellReceipt",
        "MetricSummary",
        "MeasuredEvaluationVerification",
    }
)
EXPECTED_LEGACY_SCHEMA_MODELS = frozenset(
    (
        *(
            name
            for name in EXPECTED_PUBLIC_SCHEMA_MODELS
            if name not in V2_ONLY_PUBLIC_SCHEMA_MODELS
        ),
        "ValidatorAdvisory",
        "KnowledgeAuditEvent",
        "KnowledgeInvalidationRecord",
        "KnowledgeLookupResult",
        "KnowledgePromotionDecision",
        "KnowledgePromotionRecord",
        "KnowledgePromotionRequest",
        "MUnitLocalResult",
        "SalesforceValidationContext",
        "SalesforceValidationEvidence",
        "WikiPromotionReceipt",
        "WikiPromotionReport",
        "WikiReviewDecision",
        "WikiReviewRequest",
    )
)


def test_public_schema_registry_covers_every_frozen_root_contract() -> None:
    assert tuple(model.__name__ for model in PUBLIC_SCHEMA_MODELS) == (
        EXPECTED_PUBLIC_SCHEMA_MODELS
    )


def test_legacy_v1_schema_inventory_remains_the_exact_52_file_release() -> None:
    expected_filenames = {
        f"{model_name}.schema.json" for model_name in EXPECTED_LEGACY_SCHEMA_MODELS
    }
    actual_filenames = {path.name for path in LEGACY_SNAPSHOT_ROOT.glob("*.schema.json")}

    assert len(EXPECTED_LEGACY_SCHEMA_MODELS) == 52
    assert actual_filenames == expected_filenames


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


def test_breaking_public_contracts_use_v2_without_overwriting_v1() -> None:
    assert PUBLIC_SCHEMA_RELEASE == "v2.0"

    breaking_models = (
        AgentDefinition,
        ArchitectManifestProposal,
        PilotEvaluationRegistry,
        PilotEvaluationResults,
        PilotEvidenceReceipt,
        PilotEvaluationVerification,
    )
    incompatibilities = {}
    for model in breaking_models:
        legacy_path = LEGACY_SNAPSHOT_ROOT / schema_filename(model)
        baseline = json.loads(legacy_path.read_text(encoding="utf-8"))
        incompatibilities[model.__name__] = find_backward_incompatibilities(
            baseline,
            generated_schema(model),
        )

    assert all(incompatibilities.values()), incompatibilities
    assert json.loads(
        (LEGACY_SNAPSHOT_ROOT / "AgentDefinition.schema.json").read_text(encoding="utf-8")
    )["$defs"]["AgentModelBehavior"]["properties"]["tools"] == {
        "const": "none",
        "title": "Tools",
        "type": "string",
    }


def test_implementation_intervention_schema_exposes_only_non_authorizing_actions() -> None:
    schema = generated_schema(ImplementationIntervention)

    assert schema["properties"]["requested_action"]["enum"] == [
        ApprovalAction.EXPAND_SCOPE.value,
        ApprovalAction.ACCEPT_HIGH_IMPACT_CHANGE.value,
    ]


def test_validator_model_schema_cannot_choose_runtime_unavailability() -> None:
    schema = generated_schema(ValidatorModelAdvisory)

    assert schema["properties"]["assessment"]["enum"] == [
        "supports_report",
        "raises_concern",
        "escalate",
    ]
    assert "unavailable" not in json.dumps(schema)


def test_active_benchmark_v2_models_have_stable_public_schema_names() -> None:
    active_models = (
        BenchmarkExecutionAnchor,
        BenchmarkCorpusManifest,
        BenchmarkLabelReviewEvidence,
        MeasuredEvaluationRegistry,
        HumanReviewRubric,
        EvaluationCellReceipt,
        MetricSummary,
        MeasuredEvaluationVerification,
    )

    assert tuple(schema_filename(model) for model in active_models) == (
        "BenchmarkExecutionAnchor.schema.json",
        "BenchmarkCorpusManifest.schema.json",
        "BenchmarkLabelReviewEvidence.schema.json",
        "MeasuredEvaluationRegistry.schema.json",
        "HumanReviewRubric.schema.json",
        "EvaluationCellReceipt.schema.json",
        "MetricSummary.schema.json",
        "MeasuredEvaluationVerification.schema.json",
    )


def test_graph_review_evidence_has_a_versioned_public_schema() -> None:
    schema = generated_schema(GraphLabelReviewEvidence)

    assert schema_filename(GraphLabelReviewEvidence) == ("GraphLabelReviewEvidence.schema.json")
    assert {
        "review_id",
        "fixture_id",
        "reviewer_id",
        "reviewed_at",
        "label_subject_digest",
        "attestation",
    } <= set(schema["required"])


def test_graph_assurance_report_has_a_strict_versioned_public_schema() -> None:
    schema = generated_schema(GraphAssuranceReport)

    assert schema_filename(GraphAssuranceReport) == "GraphAssuranceReport.schema.json"
    assert schema["additionalProperties"] is False
    assert {
        "platform",
        "source_revision",
        "dependency_graph_digest",
        "analyzer_version",
        "entry_paths",
        "source_digests",
        "parser_source_coverage",
        "unsupported_or_ambiguous_constructs",
        "detected_discrepancies",
        "security_sensitive_dependency_coverage",
        "status",
    } <= set(schema["required"])
    assert schema["$defs"]["GraphAssuranceStatus"]["enum"] == [
        "assured",
        "review_required",
        "blocked",
    ]


def test_active_benchmark_v2_schemas_expose_execution_and_evidence_bindings() -> None:
    anchor = generated_schema(BenchmarkExecutionAnchor)
    corpus = generated_schema(BenchmarkCorpusManifest)
    registry = generated_schema(MeasuredEvaluationRegistry)
    rubric = generated_schema(HumanReviewRubric)
    receipt = generated_schema(EvaluationCellReceipt)

    assert anchor["properties"]["provider_id"]["const"] == "claude-cli"
    assert anchor["properties"]["execution_boundary"]["const"] == ("remote_provider_managed")
    assert anchor["properties"]["runtime_inventory"]["minItems"] == 1
    assert corpus["properties"]["runs"]["minItems"] == 18
    assert corpus["properties"]["runs"]["maxItems"] == 18
    assert registry["properties"]["repetitions"]["const"] == 3
    assert registry["properties"]["cases"]["minItems"] == 3
    assert registry["properties"]["configurations"]["minItems"] == 2
    assert rubric["properties"]["review_method"]["const"] == ("independent_human_review")
    assert {
        "run_evidence_digest",
        "runtime_identity_digest",
        "execution_anchor_digest",
    } <= set(rubric["required"])
    assert receipt["properties"]["execution_boundary"]["const"] == ("remote_provider_managed")
    assert receipt["properties"]["run_evidence"]["$ref"].endswith("/VerifiedAgentRunEvidence")
    assert receipt["properties"]["human_rubric"]["$ref"].endswith("/HumanReviewRubric")


def test_active_benchmark_v2_result_schemas_require_the_complete_matrix() -> None:
    summary = generated_schema(MetricSummary)
    verification = generated_schema(MeasuredEvaluationVerification)

    assert summary["properties"]["planned_cells"]["const"] == 18
    assert summary["properties"]["verified_cells"]["const"] == 18
    assert summary["properties"]["arm_summaries"]["minItems"] == 2
    assert summary["properties"]["case_summaries"]["minItems"] == 3
    assert verification["properties"]["expected_cells"]["const"] == 18
    assert verification["properties"]["verified_cells"]["const"] == 18
    assert {
        "receipt_set_digest",
        "summary_digest",
        "quality_gate_passed",
        "passed",
    } <= set(verification["required"])


def test_v2_model_call_record_exposes_provider_managed_runtime_identity_only() -> None:
    current = json.loads(
        (SNAPSHOT_ROOT / "ModelCallRecord.schema.json").read_text(encoding="utf-8")
    )
    legacy = json.loads(
        (LEGACY_SNAPSHOT_ROOT / "ModelCallRecord.schema.json").read_text(encoding="utf-8")
    )

    current_boundaries = current["properties"]["execution_boundary"]["anyOf"][0]["enum"]
    legacy_boundaries = legacy["properties"]["execution_boundary"]["anyOf"][0]["enum"]
    assert "remote_provider_managed" in current_boundaries
    assert "runtime_identity_digest" in current["properties"]
    assert "runtime_identity_digest" not in current["required"]
    assert "remote_provider_managed" not in legacy_boundaries
    assert "runtime_identity_digest" not in legacy["properties"]


def test_agent_run_status_schema_exposes_role_artifact_persistence_failure() -> None:
    current = json.loads((SNAPSHOT_ROOT / "AgentRunStatus.schema.json").read_text(encoding="utf-8"))

    reason_codes = current["$defs"]["AgentRunFailure"]["properties"]["reason_code"]["anyOf"][0][
        "enum"
    ]
    assert "output_evidence_local_path" in reason_codes


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


def test_compatibility_check_accepts_constant_widened_to_enum_superset() -> None:
    baseline = {"type": "string", "const": "deterministic_lexical"}
    current = {
        "type": "string",
        "enum": ["deterministic_lexical", "benchmark_no_wiki_control"],
    }

    assert find_backward_incompatibilities(baseline, current) == ()


def test_compatibility_check_rejects_enum_that_removes_baseline_constant() -> None:
    baseline = {"type": "string", "const": "deterministic_lexical"}
    current = {"type": "string", "enum": ["benchmark_no_wiki_control"]}

    issues = find_backward_incompatibilities(baseline, current)

    assert [(issue.path, issue.rule) for issue in issues] == [("$", "enum-narrowed")]


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
