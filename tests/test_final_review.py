from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import AnyHttpUrl, BaseModel

import legacy_migration_agent.application.final_review as final_review_module
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectContext,
    ArchitectManifestProposal,
    ArchitectModelContext,
    ArchitectRun,
    ArchitectSemanticDecision,
    EngineerFilePlan,
    EngineerFileUpdate,
    EngineerModelOutcome,
    EngineerRun,
    SourceFileEvidence,
    ValidatorAdvisory,
    ValidatorAgent,
    ValidatorAssessment,
    ValidatorEvidenceContext,
    expand_architect_proposal,
)
from legacy_migration_agent.agent_runtime.openai_model import ModelCallRecord
from legacy_migration_agent.application.final_review import (
    FINAL_REVIEW_DECIDED_KIND,
    FINAL_REVIEW_REQUESTED_KIND,
    FinalReviewRequest,
    decide_final_review,
    get_final_review_status,
    request_final_review,
)
from legacy_migration_agent.contracts import (
    ApprovalAction,
    ChangeSet,
    CheckResult,
    CheckStatus,
    EnvironmentKind,
    MigrationManifest,
    MigrationRequest,
    MigrationTarget,
    Platform,
    ToolReceipt,
    ValidationDisposition,
    ValidationReport,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.run_session import AgentDefinitionDigests, AgentRunSession
from legacy_migration_agent.core.scope_policy import MigrationScopePolicy, PlatformAdapter
from legacy_migration_agent.core.workspace import snapshot_tree
from legacy_migration_agent.graphs.graph_contracts import (
    DependencyGraph,
    DependencyNode,
    NodeKind,
    SourceDigest,
)
from legacy_migration_agent.knowledge.wiki import RetrievalHit, RetrievalTrace, WikiSource

DIGESTS = AgentDefinitionDigests(
    architect="sha256:" + "1" * 64,
    engineer="sha256:" + "2" * 64,
    validator="sha256:" + "3" * 64,
)


def _model_call(
    role: str,
    definition_digest: str,
    *,
    input_value: BaseModel | None = None,
    output_value: BaseModel | None = None,
) -> ModelCallRecord:
    version = {
        "architect": "architect/v4",
        "engineer": "engineer/v13",
        "validator": "validator/v3",
    }[role]
    return ModelCallRecord(
        provider="offline-provider",
        model_id="offline-model",
        agent_version=version,
        agent_definition_digest=definition_digest,
        live_invocation=False,
        store_false_sent=False,
        system_prompt_digest="sha256:" + "4" * 64,
        input_digest=(
            artifact_digest(input_value) if input_value is not None else "sha256:" + "5" * 64
        ),
        output_digest=(
            artifact_digest(output_value) if output_value is not None else "sha256:" + "6" * 64
        ),
    )


def _architect_context(request: MigrationRequest) -> ArchitectContext:
    graph = DependencyGraph(
        base_revision=request.base_revision,
        entry_paths=(request.target.entry_path,),
        source_digests=(
            SourceDigest(
                path=request.target.entry_path,
                sha256=hashlib.sha256(b"legacy\n").hexdigest(),
            ),
        ),
        nodes=(
            DependencyNode(
                node_id="source-node",
                kind=NodeKind.METADATA_FILE,
                name="legacy.txt",
                metadata_paths=(request.target.entry_path,),
                resolved=True,
            ),
        ),
        edges=(),
    )
    selected_content = "Use additive outputs and validate the exact local contract."
    selected_digest = "sha256:" + hashlib.sha256(selected_content.encode("utf-8")).hexdigest()
    hit = RetrievalHit(
        page_id="wiki-page",
        title="Synthetic migration evidence",
        path="pages/synthetic-migration.md",
        score=1.0,
        matched_fields=("title",),
        source_version=request.target.source_version,
        target_version=request.target.target_version,
        status="reviewed",
        owner="capstone-owner",
        last_verified=date(2026, 8, 24),
        page_digest="sha256:" + "9" * 64,
        selected_content=selected_content,
        selected_content_digest=selected_digest,
        sources=(
            WikiSource(
                title="Salesforce Developer Documentation",
                url=AnyHttpUrl("https://developer.salesforce.com/docs"),
                publisher="Salesforce",
                authority="official",
            ),
        ),
    )
    catalog_digest = "sha256:" + "a" * 64
    wiki = RetrievalTrace(
        query="synthetic migration",
        normalized_terms=("migration", "synthetic"),
        platform=request.platform,
        source_version=request.target.source_version,
        target_version=request.target.target_version,
        catalog_digest=catalog_digest,
        as_of=date(2026, 8, 24),
        max_age_days=365,
        max_primary_hits=1,
        expand_links=False,
        hits=(hit,),
        evidence_bundle_digest=artifact_digest(
            {
                "catalog_digest": catalog_digest,
                "selected_pages": [
                    {
                        "page_id": hit.page_id,
                        "page_digest": hit.page_digest,
                        "selected_content": hit.selected_content,
                        "selected_content_digest": hit.selected_content_digest,
                    }
                ],
            }
        ),
    )
    policy = MigrationScopePolicy(
        policy_id="final-review-scope",
        platform=request.platform,
        required_source_input_paths=(request.target.entry_path,),
        approved_output_paths=("target.txt",),
        forbidden_paths=(request.target.entry_path,),
        allowed_validation_command_ids=("local-contract",),
        required_validation_command_ids=("local-contract",),
        max_changed_files=1,
        required_approval_actions=(ApprovalAction.APPROVE_MANIFEST,),
    )
    return ArchitectContext(
        model_context=ArchitectModelContext(
            request=request,
            dependency_graph=graph,
            dependency_graph_digest=artifact_digest(graph),
            source_files=(
                SourceFileEvidence(
                    path=request.target.entry_path,
                    sha256="sha256:" + hashlib.sha256(b"legacy\n").hexdigest(),
                    content="legacy\n",
                ),
            ),
            wiki_trace=wiki,
            wiki_trace_digest=artifact_digest(wiki),
        ),
        platform_adapter=PlatformAdapter.bind(
            adapter_id="final-review-adapter",
            policy=policy,
        ),
    )


def _architect_output(context: ArchitectContext) -> ArchitectManifestProposal:
    return ArchitectManifestProposal(
        semantic_decisions=(
            ArchitectSemanticDecision(
                decision_id="additive-target",
                category="target_architecture",
                summary="Use an additive target.",
                evidence_ids=("source-node", "wiki-page"),
            ),
        ),
        cited_graph_nodes=("source-node",),
        cited_wiki_pages=("wiki-page",),
    )


def _completed_session(
    tmp_path: Path,
    *,
    validator_unavailable: bool = False,
) -> tuple[
    AgentRunSession,
    MigrationRequest,
    MigrationManifest,
    ChangeSet,
    ValidationReport,
]:
    project = tmp_path / "project"
    source = project / "source"
    source.mkdir(parents=True)
    (source / "legacy.txt").write_text("legacy\n", encoding="utf-8")
    revision = snapshot_tree(source).revision
    request = MigrationRequest(
        request_id="request-final-review",
        platform=Platform.SALESFORCE,
        repository="source",
        base_revision=revision,
        target=MigrationTarget(
            entry_path="legacy.txt",
            target_runtime="Salesforce API 67.0",
            source_version="Salesforce API 67.0",
            target_version="Salesforce API 67.0",
            description="Create one additive synthetic target.",
        ),
        requested_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    session = AgentRunSession.initialize(
        project,
        project / "runs" / "final-review",
        run_id="run-final-review",
        thread_id="thread-final-review",
        slice_id="salesforce-vf-to-lwc",
        source_root="source",
        request_digest=artifact_digest(request),
        agent_definition_digests=DIGESTS,
        provider_id="offline-provider",
        model_id="offline-model",
    )
    architect_context = _architect_context(request)
    manifest = expand_architect_proposal(
        _architect_output(architect_context),
        architect_context,
    ).manifest
    change_set = ChangeSet(
        change_set_id="changes-final-review",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        base_revision=revision,
        changed_paths=("target.txt",),
        unified_diff=(
            "diff --git a/target.txt b/target.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/target.txt\n"
            "@@ -0,0 +1 @@\n"
            "+target\n"
        ),
    )
    completed = datetime(2026, 8, 24, 12, tzinfo=UTC)
    receipt = ToolReceipt(
        receipt_id="receipt-local-contract",
        tool_id="local-contract",
        request_id=request.request_id,
        run_id=session.context.run_id,
        attempt=1,
        base_revision=revision,
        environment=EnvironmentKind.LOCAL,
        input_artifact_digest=artifact_digest(change_set),
        operation="controller-local-contract",
        working_directory="source",
        started_at=completed,
        ended_at=completed,
        exit_code=0,
        terminal=True,
    )
    report = ValidationReport(
        report_id="report-final-review",
        request_id=request.request_id,
        manifest_id=manifest.manifest_id,
        change_set_id=change_set.change_set_id,
        base_revision=revision,
        results=(
            CheckResult(
                check_id="local-contract",
                command_id="local-contract",
                required=True,
                status=CheckStatus.PASSED,
                receipt=receipt,
                summary="The exact local contract passed.",
            ),
        ),
        disposition=ValidationDisposition.READY_FOR_HUMAN_REVIEW,
        attempt=1,
        completed_at=completed,
    )
    _write_role_evidence(
        session,
        request,
        manifest,
        change_set,
        report,
        validator_unavailable=validator_unavailable,
    )
    _freeze_completed_lifecycle(session)
    return session, request, manifest, change_set, report


def _write_role_evidence(
    session: AgentRunSession,
    request: MigrationRequest,
    manifest: MigrationManifest,
    change_set: ChangeSet,
    report: ValidationReport,
    *,
    validator_unavailable: bool,
) -> None:
    root = f"model-runs/{request.request_id}"
    context = _architect_context(request)
    agent_output = _architect_output(context)
    proposal = expand_architect_proposal(agent_output, context)
    assert proposal.manifest == manifest
    architect = ArchitectRun(
        agent_output=agent_output,
        proposal=proposal,
        model_call=_model_call(
            "architect",
            DIGESTS.architect,
            input_value=context.model_context,
            output_value=agent_output,
        ),
    )
    file_plan = EngineerFilePlan(
        updates=(EngineerFileUpdate(path="target.txt", content="target\n"),)
    )
    engineer_outcome = EngineerModelOutcome.for_file_plan(file_plan)
    engineer = EngineerRun(
        model_outcome=engineer_outcome,
        change_set=change_set,
        workspace_after_revision="sha256:" + "8" * 64,
        model_call=_model_call(
            "engineer",
            DIGESTS.engineer,
            output_value=engineer_outcome,
        ),
    )
    advisory = ValidatorAdvisory(
        manifest_digest=artifact_digest(manifest),
        change_set_digest=artifact_digest(change_set),
        report_digest=artifact_digest(report),
        assessment="supports_report",
        summary="The deterministic evidence supports final human review.",
        cited_check_ids=("local-contract",),
        cited_receipt_digests=(artifact_digest(report.results[0].receipt),),
        advisory_only=True,
    )
    validator_context = ValidatorEvidenceContext.freeze(manifest, change_set, report)
    validator = (
        ValidatorAgent.unavailable(
            validator_context,
            reason_code="model_call_failed",
            attempted=True,
        )
        if validator_unavailable
        else ValidatorAssessment(
            advisory=advisory,
            authoritative_disposition=report.disposition,
            all_required_checks_terminal_and_passed=True,
            model_call=_model_call(
                "validator",
                DIGESTS.validator,
                input_value=validator_context,
                output_value=advisory,
            ),
        )
    )
    session.store.write_json(f"{root}/dependency-graph.json", context.dependency_graph)
    session.store.write_json(f"{root}/wiki-trace.json", context.wiki_trace)
    session.store.write_json(f"{root}/architect-context.json", context)
    session.store.write_json(f"{root}/architect.json", architect)
    session.store.write_json(f"{root}/engineer-attempt-1.json", engineer)
    session.store.write_json(f"{root}/report-attempt-1.json", report)
    session.store.write_json(f"{root}/validator-attempt-1.json", validator)


def _freeze_completed_lifecycle(session: AgentRunSession) -> None:
    kind = "agent-run-resumed"
    session.write_index(kind)
    index = session.store.read_json(f"indexes/{kind}.json")
    session.bind_runtime_anchor(
        kind,
        {
            "schema_version": "1.0",
            "lifecycle_kind": kind,
            "run_id": session.context.run_id,
            "thread_id": session.context.thread_id,
            "request_digest": session.context.request_digest,
            "lifecycle_index_digest": artifact_digest(index),
        },
    )


def _request(
    session: AgentRunSession,
    request: MigrationRequest,
    manifest: MigrationManifest,
    change_set: ChangeSet,
    report: ValidationReport,
) -> FinalReviewRequest:
    now = datetime.now(UTC)
    return request_final_review(
        session,
        request,
        manifest,
        change_set,
        report,
        requester="migration-owner",
        designated_reviewer="final-reviewer",
        requested_at=now,
        expires_at=now + timedelta(days=2),
    )


def test_final_review_is_durable_and_grants_no_external_authority(tmp_path: Path) -> None:
    session, migration_request, manifest, change_set, report = _completed_session(tmp_path)
    _request(session, migration_request, manifest, change_set, report)

    pending = get_final_review_status(AgentRunSession.load(session.project_root, session.run_dir))
    assert pending.status == "awaiting_final_review"
    assert session.has_runtime_anchor(FINAL_REVIEW_REQUESTED_KIND)

    reloaded = AgentRunSession.load(session.project_root, session.run_dir)
    record = decide_final_review(
        reloaded,
        reviewer="final-reviewer",
        selection="accept",
        decided_at=datetime.now(UTC),
        comment="Candidate accepted for a separate manually authorized next action.",
    )
    status = get_final_review_status(AgentRunSession.load(session.project_root, session.run_dir))

    assert record.outcome == "accepted"
    assert record.candidate_accepted is True
    assert record.external_actions_authorized == ()
    assert record.deployment_performed is False
    assert record.publication_performed is False
    assert status.status == "accepted"
    assert status.external_actions_authorized == ()
    assert session.has_runtime_anchor(FINAL_REVIEW_DECIDED_KIND)


def test_interrupted_review_request_resumes_the_same_immutable_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, migration_request, manifest, change_set, report = _completed_session(tmp_path)
    requested_at = datetime.now(UTC)
    expires_at = requested_at + timedelta(days=2)
    freeze = final_review_module._freeze_lifecycle

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(final_review_module, "_freeze_lifecycle", interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        request_final_review(
            session,
            migration_request,
            manifest,
            change_set,
            report,
            requester="migration-owner",
            designated_reviewer="final-reviewer",
            requested_at=requested_at,
            expires_at=expires_at,
        )
    assert not session.has_runtime_anchor(FINAL_REVIEW_REQUESTED_KIND)

    monkeypatch.setattr(final_review_module, "_freeze_lifecycle", freeze)
    recovered = request_final_review(
        session,
        migration_request,
        manifest,
        change_set,
        report,
        requester="migration-owner",
        designated_reviewer="final-reviewer",
        requested_at=requested_at + timedelta(minutes=1),
        expires_at=expires_at + timedelta(minutes=1),
    )

    assert recovered.requested_at == requested_at
    assert recovered.expires_at == expires_at
    assert session.has_runtime_anchor(FINAL_REVIEW_REQUESTED_KIND)


def test_interrupted_review_decision_resumes_the_same_immutable_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, migration_request, manifest, change_set, report = _completed_session(tmp_path)
    _request(session, migration_request, manifest, change_set, report)
    decided_at = datetime.now(UTC)
    freeze = final_review_module._freeze_lifecycle

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(final_review_module, "_freeze_lifecycle", interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        decide_final_review(
            session,
            reviewer="final-reviewer",
            selection="accept",
            decided_at=decided_at,
            comment="Reviewed candidate.",
        )
    assert not session.has_runtime_anchor(FINAL_REVIEW_DECIDED_KIND)

    monkeypatch.setattr(final_review_module, "_freeze_lifecycle", freeze)
    recovered = decide_final_review(
        session,
        reviewer="final-reviewer",
        selection="accept",
        decided_at=decided_at + timedelta(minutes=1),
        comment="Reviewed candidate.",
    )

    assert recovered.decided_at == decided_at
    assert recovered.outcome == "accepted"
    assert session.has_runtime_anchor(FINAL_REVIEW_DECIDED_KIND)


def test_expired_review_can_be_closed_but_not_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, migration_request, manifest, change_set, report = _completed_session(tmp_path)
    requested_at = datetime.now(UTC)
    expires_at = requested_at + timedelta(hours=1)
    request_final_review(
        session,
        migration_request,
        manifest,
        change_set,
        report,
        requester="migration-owner",
        designated_reviewer="final-reviewer",
        requested_at=requested_at,
        expires_at=expires_at,
    )
    future = expires_at + timedelta(minutes=1)

    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return future if tz is not None else future.replace(tzinfo=None)

    monkeypatch.setattr(final_review_module, "datetime", FutureDateTime)
    assert get_final_review_status(session).status == "expired"
    with pytest.raises(PolicyViolation, match="cannot be accepted"):
        decide_final_review(
            session,
            reviewer="final-reviewer",
            selection="accept",
            decided_at=future,
        )

    record = decide_final_review(
        session,
        reviewer="final-reviewer",
        selection="request_changes",
        decided_at=future,
        comment="Review window expired; start a fresh review after revision.",
    )
    assert record.outcome == "changes_requested"


def test_final_review_accepts_explicit_validator_advisory_unavailable_evidence(
    tmp_path: Path,
) -> None:
    session, migration_request, manifest, change_set, report = _completed_session(
        tmp_path,
        validator_unavailable=True,
    )
    validator = ValidatorAssessment.model_validate(
        session.store.read_json(
            f"model-runs/{migration_request.request_id}/validator-attempt-1.json"
        )
    )

    review = _request(session, migration_request, manifest, change_set, report)

    assert validator.advisory.assessment == "unavailable"
    assert validator.unavailable_receipt is not None
    assert review.validator_assessment_digest == artifact_digest(validator)


@pytest.mark.parametrize(
    ("selection", "outcome", "next_action"),
    [
        ("reject", "rejected", "stop_request"),
        ("request_changes", "changes_requested", "revise_and_start_new_review"),
    ],
)
def test_non_accepting_decisions_stop_or_require_a_new_review(
    tmp_path: Path,
    selection: str,
    outcome: str,
    next_action: str,
) -> None:
    session, migration_request, manifest, change_set, report = _completed_session(tmp_path)
    _request(session, migration_request, manifest, change_set, report)

    record = decide_final_review(
        session,
        reviewer="final-reviewer",
        selection=selection,  # type: ignore[arg-type]
        decided_at=datetime.now(UTC),
    )

    assert record.outcome == outcome
    assert record.next_action == next_action
    assert record.candidate_accepted is False
    assert record.external_actions_authorized == ()


def test_final_review_decision_is_one_use_and_reviewer_bound(tmp_path: Path) -> None:
    session, migration_request, manifest, change_set, report = _completed_session(tmp_path)
    _request(session, migration_request, manifest, change_set, report)

    with pytest.raises(PolicyViolation, match="transferred"):
        decide_final_review(
            session,
            reviewer="another-reviewer",
            selection="accept",
            decided_at=datetime.now(UTC),
        )
    decide_final_review(
        session,
        reviewer="final-reviewer",
        selection="accept",
        decided_at=datetime.now(UTC),
    )
    with pytest.raises(PolicyViolation, match="already been decided"):
        decide_final_review(
            session,
            reviewer="final-reviewer",
            selection="accept",
            decided_at=datetime.now(UTC),
        )


def test_final_review_rejects_unpersisted_or_drifting_role_artifacts(tmp_path: Path) -> None:
    session, migration_request, manifest, change_set, report = _completed_session(tmp_path)
    drifted = manifest.model_copy(
        update={
            "transformations": (
                manifest.transformations[0].model_copy(update={"description": "Drifted plan."}),
                *manifest.transformations[1:],
            )
        }
    )

    with pytest.raises(PolicyViolation, match="Architect artifact"):
        _request(session, migration_request, drifted, change_set, report)


def test_final_review_rejects_non_ready_evidence(tmp_path: Path) -> None:
    session, migration_request, manifest, change_set, report = _completed_session(tmp_path)
    receipt = report.results[0].receipt
    assert receipt is not None
    failed_receipt = receipt.model_copy(update={"exit_code": 1})
    failed_result = report.results[0].model_copy(
        update={"status": CheckStatus.FAILED, "receipt": failed_receipt}
    )
    failed_report = report.model_copy(
        update={
            "results": (failed_result,),
            "disposition": ValidationDisposition.RECOVERABLE_FAILURE,
        }
    )

    with pytest.raises(PolicyViolation, match="ready_for_human_review"):
        _request(session, migration_request, manifest, change_set, failed_report)


def test_final_review_keeps_secrets_and_local_paths_out_of_evidence(tmp_path: Path) -> None:
    session, migration_request, manifest, change_set, report = _completed_session(tmp_path)
    _request(session, migration_request, manifest, change_set, report)
    secret = "sk-review-secret-123456789"

    with pytest.raises(PolicyViolation, match="API-key-shaped"):
        decide_final_review(
            session,
            reviewer="final-reviewer",
            selection="accept",
            decided_at=datetime.now(UTC),
            comment=f"leak {secret} from /private/tmp/review",
        )

    all_bytes = b"".join(path.read_bytes() for path in session.run_dir.rglob("*") if path.is_file())
    assert secret.encode() not in all_bytes
    assert b"/private/tmp/review" not in all_bytes


def test_final_review_detects_request_tampering(tmp_path: Path) -> None:
    session, migration_request, manifest, change_set, report = _completed_session(tmp_path)
    _request(session, migration_request, manifest, change_set, report)
    request_path = session.evidence_dir / "final-review" / "request.json"
    payload = request_path.read_text(encoding="utf-8")
    request_path.write_text(payload.replace("final-reviewer", "other-reviewer"), encoding="utf-8")

    with pytest.raises(PolicyViolation, match="digest mismatch"):
        get_final_review_status(AgentRunSession.load(session.project_root, session.run_dir))
