from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import legacy_migration_agent.application.agent_run as agent_run_module
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelCallRecord,
    ModelUsageEvidence,
)
from legacy_migration_agent.application.agent_run import AgentRunStatus
from legacy_migration_agent.contracts import MigrationRequest, MigrationTarget, Platform
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.workspace import content_revision
from legacy_migration_agent.evaluation import (
    EvaluationVerificationError,
    PilotEvidenceKind,
    PilotEvidenceReceipt,
    PilotResultReason,
    ResultStatus,
    build_agent_run_receipt,
    build_local_pilot_results,
    load_and_verify,
    load_and_verify_pilot,
    load_pilot_registry,
    load_pilot_results,
    load_registry,
    load_results,
    verify_evaluation,
    verify_pilot_evaluation,
    write_agent_run_pilot_snapshot,
    write_local_pilot_snapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_ROOT / "evaluation/benchmark-v1/registry.json"
RESULTS_PATH = PROJECT_ROOT / "evaluation/results.json"
PILOT_REGISTRY_PATH = PROJECT_ROOT / "evaluation/pilot-v1/registry.json"
PILOT_SNAPSHOT_DIR = PROJECT_ROOT / "evaluation/pilot-v1"


def _document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_document(tmp_path: Path, name: str, value: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_checked_in_evaluation_is_the_exact_unmeasured_cross_product() -> None:
    verification = load_and_verify(REGISTRY_PATH, RESULTS_PATH)
    results = load_results(RESULTS_PATH)

    assert verification.cell_count == 72
    assert verification.case_count == 6
    assert verification.configuration_count == 4
    assert verification.repetitions == 3
    assert verification.measured_cell_count == 0
    assert {cell.status for cell in results.cells} == {ResultStatus.NOT_PERFORMED}
    assert results.summary.complete is False
    assert results.summary.passed is False
    assert results.execution_boundary.provider_invoked is False
    assert results.execution_boundary.external_platform_invoked is False
    assert results.execution_boundary.external_authority_granted is False


def test_missing_cell_is_rejected(tmp_path: Path) -> None:
    document = _document(RESULTS_PATH)
    document["cells"].pop()

    with pytest.raises(EvaluationVerificationError):
        load_results(_write_document(tmp_path, "missing.json", document))


def test_duplicate_cell_is_rejected(tmp_path: Path) -> None:
    document = _document(RESULTS_PATH)
    document["cells"][1] = document["cells"][0]

    with pytest.raises(EvaluationVerificationError, match="invalid evaluation document"):
        load_results(_write_document(tmp_path, "duplicate.json", document))


def test_unknown_status_is_rejected(tmp_path: Path) -> None:
    document = _document(RESULTS_PATH)
    document["cells"][0]["status"] = "claimed_without_evidence"

    with pytest.raises(EvaluationVerificationError):
        load_results(_write_document(tmp_path, "status.json", document))


def test_summary_status_mismatch_is_rejected(tmp_path: Path) -> None:
    document = _document(RESULTS_PATH)
    document["summary"]["status_counts"]["not_performed"] = 71
    document["summary"]["status_counts"]["succeeded"] = 1

    with pytest.raises(EvaluationVerificationError):
        load_results(_write_document(tmp_path, "summary.json", document))


def test_tampered_cell_identity_fails_cross_artifact_verification(tmp_path: Path) -> None:
    document = _document(RESULTS_PATH)
    document["cells"][0]["cell_id"] = "tampered-cell"
    results = load_results(_write_document(tmp_path, "identity.json", document))

    with pytest.raises(EvaluationVerificationError, match="noncanonical evaluation cell ID"):
        verify_evaluation(load_registry(REGISTRY_PATH), results)


def test_changed_metric_threshold_is_rejected(tmp_path: Path) -> None:
    document = _document(REGISTRY_PATH)
    dependency_recall = next(
        metric for metric in document["metrics"] if metric["metric_id"] == "dependency_recall"
    )
    dependency_recall["threshold"] = 0.50

    with pytest.raises(EvaluationVerificationError):
        load_registry(_write_document(tmp_path, "registry.json", document))


def test_summary_metric_set_must_match_registry(tmp_path: Path) -> None:
    document = _document(RESULTS_PATH)
    document["summary"]["metrics"].reverse()
    results = load_results(_write_document(tmp_path, "metrics.json", document))

    with pytest.raises(EvaluationVerificationError, match="summary metrics"):
        verify_evaluation(load_registry(REGISTRY_PATH), results)


def test_checked_in_pilot_contains_only_unperformed_qwen_agent_cells() -> None:
    verification = load_and_verify_pilot(
        PROJECT_ROOT,
        PILOT_REGISTRY_PATH,
        PILOT_SNAPSHOT_DIR,
    )
    results = load_pilot_results(PILOT_SNAPSHOT_DIR / "results.json")

    registry = load_pilot_registry(PILOT_REGISTRY_PATH)

    assert verification.measured_cells == 0
    assert verification.not_performed_cells == 2
    assert verification.agent_run_receipts_verified == 0
    assert verification.agent_run_sources_reverified == 0
    assert verification.portable_agent_receipts_only is False
    assert verification.complete is False
    assert results.snapshot_status == "not_performed"
    assert tuple(cell.status for cell in results.cells) == (ResultStatus.NOT_PERFORMED,) * 2
    assert tuple(case.evaluation_mode.value for case in registry.cases) == (
        "agent_run",
        "agent_run",
    )
    assert tuple(case.expected_model_id for case in registry.cases) == (
        "qwen3.8:latest",
        "qwen3.8:latest",
    )
    assert all(cell.evidence_receipt_path is None for cell in results.cells)
    assert all(cell.evidence_receipt_digest is None for cell in results.cells)
    assert all("candidate_path" not in case for case in _document(PILOT_REGISTRY_PATH)["cases"])
    assert not tuple((PILOT_SNAPSHOT_DIR / "evidence").glob("*.json"))
    assert results.summary.agent_cells_planned == 2
    assert results.summary.agent_cells_measured == 0
    assert results.summary.model_quality_evaluated is False
    assert results.summary.external_platform_evaluated is False
    assert results.summary.passed is False


def test_pilot_registry_rejects_a_static_reference_candidate(tmp_path: Path) -> None:
    document = _document(PILOT_REGISTRY_PATH)
    document["cases"][0]["evaluation_mode"] = "local_static"
    document["cases"][0]["candidate_path"] = "private/unshipped-salesforce-candidate"

    with pytest.raises(EvaluationVerificationError, match="invalid evaluation document"):
        load_pilot_registry(_write_document(tmp_path, "static-candidate.json", document))


def test_local_pilot_build_only_initializes_unperformed_agent_cells() -> None:
    registry = load_pilot_registry(PILOT_REGISTRY_PATH)

    first, first_receipts = build_local_pilot_results(PROJECT_ROOT, registry)
    second, second_receipts = build_local_pilot_results(PROJECT_ROOT, registry)

    assert first == second
    assert first_receipts == second_receipts == ()
    assert first.summary.measured_cells == 0
    assert first.summary.agent_cells_measured == 0
    assert tuple(cell.status for cell in first.cells) == (ResultStatus.NOT_PERFORMED,) * 2
    assert all(cell.reason is PilotResultReason.AWAITING_QWEN_RUN for cell in first.cells)
    assert "Repository tests establish harness behavior" in first.limitations[1]


def test_local_pilot_snapshot_writer_is_idempotent_and_self_verifying(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"

    first = write_local_pilot_snapshot(PROJECT_ROOT, PILOT_REGISTRY_PATH, snapshot)
    second = write_local_pilot_snapshot(PROJECT_ROOT, PILOT_REGISTRY_PATH, snapshot)

    assert first == second
    assert load_and_verify_pilot(PROJECT_ROOT, PILOT_REGISTRY_PATH, snapshot).verified is True
    assert tuple(
        path.relative_to(snapshot).as_posix() for path in sorted(snapshot.rglob("*.json"))
    ) == ("results.json",)


def test_pilot_cell_order_tamper_fails_cross_artifact_verification(tmp_path: Path) -> None:
    results_document = _document(PILOT_SNAPSHOT_DIR / "results.json")
    results_document["cells"].reverse()
    results = load_pilot_results(_write_document(tmp_path, "results.json", results_document))

    with pytest.raises(EvaluationVerificationError, match="canonical registry order"):
        verify_pilot_evaluation(
            PROJECT_ROOT,
            load_pilot_registry(PILOT_REGISTRY_PATH),
            results,
            PILOT_SNAPSHOT_DIR,
        )


def test_pilot_not_performed_cell_cannot_claim_a_receipt(tmp_path: Path) -> None:
    results_document = _document(PILOT_SNAPSHOT_DIR / "results.json")
    results_document["cells"][0]["reason"] = PilotResultReason.VERIFIED_AGENT_RUN.value
    results_document["cells"][0]["evidence_receipt_path"] = "evidence/fake.json"
    results_document["cells"][0]["evidence_receipt_digest"] = "sha256:" + "0" * 64

    with pytest.raises(EvaluationVerificationError, match="invalid evaluation document"):
        load_pilot_results(_write_document(tmp_path, "results.json", results_document))


def test_agent_ingestion_records_verified_workflow_status_without_quality_claims(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fixture_source = PROJECT_ROOT / "fixtures/salesforce/account-contact-explorer"
    fixture_destination = tmp_path / "fixtures/salesforce/account-contact-explorer"
    fixture_destination.mkdir(parents=True)
    shutil.copytree(fixture_source / "input", fixture_destination / "input")
    shutil.copy2(fixture_source / "fixture.yaml", fixture_destination / "fixture.yaml")
    source = fixture_destination / "input"
    request = MigrationRequest(
        request_id="pilot-qwen-request",
        platform=Platform.SALESFORCE,
        repository="synthetic-fixture",
        base_revision=content_revision(source),
        target=MigrationTarget(
            entry_path="force-app/main/default/pages/LegacyAccountContactExplorer.page",
            target_runtime="Lightning Web Components",
            source_version="Salesforce API 67.0",
            target_version="Salesforce API 67.0",
            description="Migrate the bounded synthetic fixture.",
        ),
    )
    status = AgentRunStatus(
        run_id="pilot-qwen-run",
        thread_id="pilot-qwen-thread",
        request_id=request.request_id,
        request_digest=artifact_digest(request),
        platform=Platform.SALESFORCE,
        provider_id="ollama",
        model_id="qwen3.8:latest",
        status="completed",
        terminal_disposition="ready_for_human_review",
        execution_attempt=1,
        pending_nodes=(),
        task_failed=False,
    )
    run_dir = tmp_path / ".runs/pilot-qwen-run"
    model_evidence = run_dir / f"evidence/model-runs/{request.request_id}/architect.json"
    model_evidence.parent.mkdir(parents=True)
    model_call = ModelCallRecord(
        provider="ollama",
        model_id="qwen3.8:latest",
        agent_version="architect/v2",
        agent_definition_digest="sha256:" + "1" * 64,
        live_invocation=False,
        store_false_sent=False,
        execution_boundary="local_loopback",
        model_revision="sha256:" + "2" * 64,
        live_approval=LiveModelApproval(
            allow_live_api=True,
            allow_prompt_data_sharing=True,
            approved_by="test-operator",
        ),
        system_prompt_digest="sha256:" + "3" * 64,
        input_digest="sha256:" + "4" * 64,
        output_digest="sha256:" + "5" * 64,
        usage=ModelUsageEvidence(
            latency_ms=1250,
            provider_usage_reported=True,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
    )
    model_evidence.write_text(
        json.dumps({"model_call": model_call.model_dump(mode="json")}),
        encoding="utf-8",
    )
    (run_dir / "evidence/control").mkdir(parents=True)
    (run_dir / "evidence/control/manifest-approval.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "evidence/request.json").write_text(
        request.model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent_run_module,
        "get_agent_run_status",
        lambda *args, **kwargs: status,
    )

    receipt = build_agent_run_receipt(
        tmp_path,
        load_pilot_registry(PILOT_REGISTRY_PATH),
        case_id="salesforce-qwen-end-to-end",
        run_dir=run_dir,
        run_id=status.run_id,
        thread_id=status.thread_id,
    )

    assert receipt.status is ResultStatus.SUCCEEDED
    assert receipt.evidence_kind is PilotEvidenceKind.AGENT_RUN
    assert receipt.claims == ("agent_workflow_reached_ready_for_human_review",)
    assert receipt.boundary.provider.value == "invoked"
    assert receipt.boundary.external_platform.value == "unknown"
    assert receipt.boundary.authentication.value == "unknown"
    assert receipt.boundary.external_authority_granted is False
    assert receipt.agent_run_observation is not None
    assert receipt.agent_run_observation.manifest_gate_performed is True
    assert receipt.agent_run_observation.completed_role_calls == ("architect",)
    assert receipt.agent_run_observation.completed_model_calls == 1
    assert receipt.agent_run_observation.completed_model_latency_ms == 1250
    assert receipt.agent_run_observation.token_usage_complete is True
    assert receipt.agent_run_observation.input_tokens == 100
    assert receipt.agent_run_observation.output_tokens == 50
    assert receipt.agent_run_observation.total_tokens == 150
    assert receipt.agent_run_observation.model_quality_evaluated is False
    assert receipt.agent_run_observation.semantic_conformance_evaluated is False
    assert receipt.agent_run_observation.external_platform_evaluated is False
    assert receipt.agent_run_observation.human_acceptance_evaluated is False

    output = tmp_path / "pilot-with-qwen-observation"
    results = write_agent_run_pilot_snapshot(
        tmp_path,
        PILOT_REGISTRY_PATH,
        PILOT_SNAPSHOT_DIR,
        output,
        results_id="pilot-qwen-observation",
        case_id="salesforce-qwen-end-to-end",
        run_dir=run_dir,
        run_id=status.run_id,
        thread_id=status.thread_id,
    )
    verification = load_and_verify_pilot(tmp_path, PILOT_REGISTRY_PATH, output)

    assert results.snapshot_sequence == 2
    assert results.summary.measured_cells == 1
    assert results.summary.agent_cells_measured == 1
    assert results.summary.complete is False
    assert results.snapshot_status == "partially_measured"
    assert results.summary.model_quality_evaluated is False
    assert verification.measured_cells == 1
    assert verification.not_performed_cells == 1
    assert verification.agent_run_receipts_verified == 1
    assert verification.agent_run_sources_reverified == 1
    assert verification.portable_agent_receipts_only is False

    tampered_output = tmp_path / "pilot-with-tampered-qwen-observation"
    shutil.copytree(output, tampered_output)
    tampered_receipt_path = tampered_output / "evidence/salesforce-qwen-end-to-end.json"
    tampered_receipt_document = _document(tampered_receipt_path)
    tampered_receipt_document["agent_run_observation"]["completed_model_latency_ms"] = 999999
    tampered_receipt_path.write_text(
        json.dumps(tampered_receipt_document),
        encoding="utf-8",
    )
    tampered_receipt = PilotEvidenceReceipt.model_validate_json(tampered_receipt_path.read_bytes())
    tampered_results_path = tampered_output / "results.json"
    tampered_results_document = _document(tampered_results_path)
    tampered_results_document["cells"][0]["evidence_receipt_digest"] = artifact_digest(
        tampered_receipt
    )
    tampered_results_path.write_text(
        json.dumps(tampered_results_document),
        encoding="utf-8",
    )
    with pytest.raises(EvaluationVerificationError, match="source-run revalidation"):
        load_and_verify_pilot(tmp_path, PILOT_REGISTRY_PATH, tampered_output)

    shutil.rmtree(run_dir)
    portable_verification = load_and_verify_pilot(tmp_path, PILOT_REGISTRY_PATH, output)
    assert portable_verification.agent_run_receipts_verified == 1
    assert portable_verification.agent_run_sources_reverified == 0
    assert portable_verification.portable_agent_receipts_only is True
