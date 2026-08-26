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


def test_checked_in_pilot_is_honestly_partial_and_reexecutes_static_claims() -> None:
    verification = load_and_verify_pilot(
        PROJECT_ROOT,
        PILOT_REGISTRY_PATH,
        PILOT_SNAPSHOT_DIR,
    )
    results = load_pilot_results(PILOT_SNAPSHOT_DIR / "results.json")

    assert verification.measured_cells == 2
    assert verification.not_performed_cells == 2
    assert verification.local_static_receipts_reexecuted == 2
    assert verification.agent_run_receipts_verified == 0
    assert verification.agent_run_sources_reverified == 0
    assert verification.portable_agent_receipts_only is False
    assert verification.complete is False
    assert tuple(cell.status for cell in results.cells) == (
        ResultStatus.SUCCEEDED,
        ResultStatus.SUCCEEDED,
        ResultStatus.NOT_PERFORMED,
        ResultStatus.NOT_PERFORMED,
    )
    assert results.summary.model_quality_evaluated is False
    assert results.summary.external_platform_evaluated is False
    assert results.summary.passed is False


def test_local_pilot_build_is_deterministic_and_invokes_no_external_boundary() -> None:
    registry = load_pilot_registry(PILOT_REGISTRY_PATH)

    first, first_receipts = build_local_pilot_results(PROJECT_ROOT, registry)
    second, second_receipts = build_local_pilot_results(PROJECT_ROOT, registry)

    assert first == second
    assert first_receipts == second_receipts
    assert all(
        receipt.evidence_kind is PilotEvidenceKind.LOCAL_STATIC for receipt in first_receipts
    )
    for receipt in first_receipts:
        assert receipt.boundary.provider.value == "not_invoked"
        assert receipt.boundary.external_platform.value == "not_invoked"
        assert receipt.boundary.authentication.value == "not_invoked"
        assert receipt.boundary.subprocess.value == "not_invoked"
        assert receipt.boundary.human_gate.value == "not_invoked"
        assert receipt.boundary.external_authority_granted is False
        assert receipt.local_observation is not None
        assert receipt.local_observation.tests_executed is False
        assert receipt.local_observation.model_quality_evaluated is False


def test_local_pilot_snapshot_writer_is_idempotent_and_self_verifying(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"

    first = write_local_pilot_snapshot(PROJECT_ROOT, PILOT_REGISTRY_PATH, snapshot)
    second = write_local_pilot_snapshot(PROJECT_ROOT, PILOT_REGISTRY_PATH, snapshot)

    assert first == second
    assert load_and_verify_pilot(PROJECT_ROOT, PILOT_REGISTRY_PATH, snapshot).verified is True
    assert tuple(
        path.relative_to(snapshot).as_posix() for path in sorted(snapshot.rglob("*.json"))
    ) == (
        "evidence/mulesoft-static-fixture-contract.json",
        "evidence/salesforce-static-fixture-contract.json",
        "results.json",
    )


def test_pilot_receipt_tamper_is_rejected_even_if_result_digest_is_recomputed(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    shutil.copytree(PILOT_SNAPSHOT_DIR, snapshot)
    receipt_path = snapshot / "evidence/salesforce-static-fixture-contract.json"
    receipt_document = _document(receipt_path)
    receipt_document["limitations"][0] = "Tampered but schema-valid limitation."
    receipt_path.write_text(json.dumps(receipt_document), encoding="utf-8")
    receipt = PilotEvidenceReceipt.model_validate_json(receipt_path.read_bytes())
    results_path = snapshot / "results.json"
    results_document = _document(results_path)
    results_document["cells"][0]["evidence_receipt_digest"] = artifact_digest(receipt)
    results_path.write_text(json.dumps(results_document), encoding="utf-8")

    with pytest.raises(EvaluationVerificationError, match="differs from re-execution"):
        load_and_verify_pilot(PROJECT_ROOT, PILOT_REGISTRY_PATH, snapshot)


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


def test_pilot_measured_reason_must_match_case_mode(tmp_path: Path) -> None:
    results_document = _document(PILOT_SNAPSHOT_DIR / "results.json")
    results_document["cells"][0]["reason"] = PilotResultReason.VERIFIED_AGENT_RUN.value
    results = load_pilot_results(_write_document(tmp_path, "results.json", results_document))

    with pytest.raises(EvaluationVerificationError, match="reason differs"):
        verify_pilot_evaluation(
            PROJECT_ROOT,
            load_pilot_registry(PILOT_REGISTRY_PATH),
            results,
            PILOT_SNAPSHOT_DIR,
        )


def test_pilot_not_performed_cell_cannot_claim_a_receipt(tmp_path: Path) -> None:
    results_document = _document(PILOT_SNAPSHOT_DIR / "results.json")
    results_document["cells"][2]["reason"] = PilotResultReason.VERIFIED_AGENT_RUN.value
    results_document["cells"][2]["evidence_receipt_path"] = "evidence/fake.json"
    results_document["cells"][2]["evidence_receipt_digest"] = "sha256:" + "0" * 64

    with pytest.raises(EvaluationVerificationError, match="invalid evaluation document"):
        load_pilot_results(_write_document(tmp_path, "results.json", results_document))


def test_agent_ingestion_records_verified_workflow_status_without_quality_claims(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shutil.copytree(PROJECT_ROOT / "fixtures", tmp_path / "fixtures")
    fixture_destination = tmp_path / "fixtures/salesforce/account-contact-explorer"
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
    assert results.summary.measured_cells == 3
    assert results.summary.agent_cells_measured == 1
    assert results.summary.complete is False
    assert results.summary.model_quality_evaluated is False
    assert verification.measured_cells == 3
    assert verification.not_performed_cells == 1
    assert verification.agent_run_receipts_verified == 1
    assert verification.agent_run_sources_reverified == 1
    assert verification.portable_agent_receipts_only is False
    assert verification.local_static_receipts_reexecuted == 2

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
    tampered_results_document["cells"][2]["evidence_receipt_digest"] = artifact_digest(
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
