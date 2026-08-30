from __future__ import annotations

import hashlib
import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError
from ui_test_doubles import (
    LOCAL_MODEL_REVISION,
    make_ollama_client_test_double,
)

from legacy_migration_agent.agent_runtime.agent_definitions import load_agent_registry
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectAgent,
    ArchitectConversationContext,
    ArchitectConversationMessage,
    ArchitectConversationReply,
    ArchitectManifestProposal,
    _ArchitectConversationClarificationOutput,
)
from legacy_migration_agent.application import agent_run as agent_run_module
from legacy_migration_agent.application.architect_conversation import (
    MAX_CONVERSATIONS,
    ArchitectConversationExchange,
    ArchitectConversationStore,
    architect_conversation_launch_token,
)
from legacy_migration_agent.application.migration_scenarios import (
    migration_launch_contract,
)
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.ui.service import AgentUiError, AgentUiService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "test-model:latest"
ARCHITECT_VERSION = load_agent_registry(PROJECT_ROOT / "agents").get("architect").version


class _ConversationModel:
    provider = "test-double"
    model_id = "conversation/v1"
    live_invocation = False
    store_false_sent = False

    def __init__(self, reply: ArchitectConversationReply) -> None:
        self.reply = reply
        self.output_types: list[type[BaseModel]] = []

    def parse(self, *, system_prompt, input_value, output_type):
        assert system_prompt
        assert isinstance(input_value, ArchitectConversationContext)
        self.output_types.append(output_type)
        return output_type.model_validate(self.reply.model_dump(mode="python"))


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "agents", project / "agents")
    shutil.copytree(PROJECT_ROOT / "knowledge/wiki", project / "knowledge/wiki")
    shutil.copytree(PROJECT_ROOT / "fixtures", project / "fixtures")
    return project


def _service(project: Path) -> AgentUiService:
    return AgentUiService(project, ollama_model_id=MODEL_ID)


def _stub_ollama(
    monkeypatch: pytest.MonkeyPatch,
    project: Path,
    *,
    role_calls: list[str] | None = None,
    bound_revisions: list[str] | None = None,
    current_revision: str = LOCAL_MODEL_REVISION,
) -> None:
    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        make_ollama_client_test_double(
            project,
            role_calls=role_calls,
            bound_revisions=bound_revisions,
            current_revision=current_revision,
        ),
    )


def _persist_launched_conversation_copy(
    store: ArchitectConversationStore,
    conversation_id: str,
    exchange: ArchitectConversationExchange,
) -> None:
    store.create(
        conversation_id,
        initial_platform=exchange.selected_platform,
        initial_scenario_id=exchange.scenario_id,
    )
    ready = store.append_exchange(
        conversation_id,
        selected_platform=exchange.selected_platform,
        scenario_id=exchange.scenario_id,
        launch_contract_digest=exchange.launch_contract_digest,
        user_message=exchange.user_message,
        architect_run=exchange.architect_run,
    )
    launch_token = architect_conversation_launch_token(ready)
    assert launch_token is not None
    store.begin_launch(
        conversation_id,
        handle=conversation_id,
        expected_launch_token=launch_token,
    )
    store.record_launch(conversation_id, handle=conversation_id)


def test_architect_conversation_mode_is_typed_and_non_authorizing() -> None:
    context = ArchitectConversationContext(
        selected_platform=None,
        history=(ArchitectConversationMessage(role="user", content="Can you help me migrate?"),),
    )
    reply = ArchitectConversationReply(
        status="clarification_needed",
        assistant_message="Choose a Salesforce or MuleSoft slice first.",
        missing_information=("Select a migration slice.",),
    )
    model = _ConversationModel(reply)

    run = ArchitectAgent(load_agent_registry(PROJECT_ROOT / "agents"), model).converse(context)

    assert type(run.reply) is ArchitectConversationReply
    assert run.reply == reply
    assert model.output_types == [_ArchitectConversationClarificationOutput]
    assert run.model_call.agent_version == ARCHITECT_VERSION
    assert run.model_call.output_digest == artifact_digest(run.reply)
    assert not hasattr(run.reply, "approved")
    assert not hasattr(run.reply, "run_id")


def test_architect_provider_branch_rejects_ready_without_controller_selected_platform() -> None:
    context = ArchitectConversationContext(
        selected_platform=None,
        history=(ArchitectConversationMessage(role="user", content="Migrate this legacy app."),),
    )
    reply = ArchitectConversationReply(
        status="ready_to_launch",
        assistant_message="Ready.",
        advisory_summary="The bounded controller scenario is ready for review.",
    )

    with pytest.raises(ValidationError, match="clarification_needed"):
        ArchitectAgent(
            load_agent_registry(PROJECT_ROOT / "agents"),
            _ConversationModel(reply),
        ).converse(context)


@pytest.mark.parametrize(
    "secret_text",
    (
        "Authorization: Bearer actual-token-value-123456",
        'password="response.password"',
        "password=hunter2",
        "token=randomIdentifier",
        "token=abcdefghijklmnop123456",
        "authToken=resolveToken()",
    ),
)
def test_conversation_store_rejects_model_secret_before_exchange_write(
    tmp_path: Path,
    secret_text: str,
) -> None:
    conversation_id = "a" * 24
    store = ArchitectConversationStore(tmp_path / "conversations")
    store.create(
        conversation_id,
        initial_platform=None,
        initial_scenario_id=None,
    )
    user_message = ArchitectConversationMessage(role="user", content="Help me plan a migration.")
    context = ArchitectConversationContext(
        selected_platform=None,
        history=(user_message,),
    )
    safe_reply = ArchitectConversationReply(
        status="clarification_needed",
        assistant_message="Select one bounded migration slice.",
        missing_information=("Select a migration slice.",),
    )
    run = ArchitectAgent(
        load_agent_registry(PROJECT_ROOT / "agents"),
        _ConversationModel(safe_reply),
    ).converse(context)
    unsafe_run = run.model_copy(
        update={"reply": safe_reply.model_copy(update={"assistant_message": secret_text})}
    )

    with pytest.raises(PolicyViolation, match="conversation exchange"):
        store.append_exchange(
            conversation_id,
            selected_platform=None,
            scenario_id=None,
            launch_contract_digest=None,
            user_message=user_message,
            architect_run=unsafe_run,
        )

    assert not (store.root / conversation_id / "exchange-0001.json").exists()
    persisted = b"".join(path.read_bytes() for path in store.root.rglob("*") if path.is_file())
    assert secret_text.encode() not in persisted


def test_conversation_store_allows_benign_request_token_code(tmp_path: Path) -> None:
    conversation_id = "b" * 24
    store = ArchitectConversationStore(tmp_path / "conversations")
    store.create(
        conversation_id,
        initial_platform=None,
        initial_scenario_id=None,
    )
    user_message = ArchitectConversationMessage(role="user", content="Help me plan a migration.")
    context = ArchitectConversationContext(
        selected_platform=None,
        history=(user_message,),
    )
    safe_reply = ArchitectConversationReply(
        status="clarification_needed",
        assistant_message="Select one bounded migration slice.",
        missing_information=("Select a migration slice.",),
    )
    run = ArchitectAgent(
        load_agent_registry(PROJECT_ROOT / "agents"),
        _ConversationModel(safe_reply),
    ).converse(context)
    benign = (
        "Use `const token = ++this.requestGeneration;` and read "
        "`response.accessToken`; `token=requestToken`; `token=requestGeneration`; and "
        "`token=currentRequest` without hard-coding a credential."
    )
    benign_run = run.model_copy(
        update={"reply": safe_reply.model_copy(update={"assistant_message": benign})}
    )

    snapshot = store.append_exchange(
        conversation_id,
        selected_platform=None,
        scenario_id=None,
        launch_contract_digest=None,
        user_message=user_message,
        architect_run=benign_run,
    )

    assert snapshot.exchanges[0].architect_run.reply.assistant_message == benign


def test_conversation_store_rejects_secret_in_loaded_exchange(tmp_path: Path) -> None:
    conversation_id = "c" * 24
    store = ArchitectConversationStore(tmp_path / "conversations")
    store.create(
        conversation_id,
        initial_platform=None,
        initial_scenario_id=None,
    )
    user_message = ArchitectConversationMessage(role="user", content="Help me plan a migration.")
    context = ArchitectConversationContext(
        selected_platform=None,
        history=(user_message,),
    )
    reply = ArchitectConversationReply(
        status="clarification_needed",
        assistant_message="Select one bounded migration slice.",
        missing_information=("Select a migration slice.",),
    )
    run = ArchitectAgent(
        load_agent_registry(PROJECT_ROOT / "agents"),
        _ConversationModel(reply),
    ).converse(context)
    store.append_exchange(
        conversation_id,
        selected_platform=None,
        scenario_id=None,
        launch_contract_digest=None,
        user_message=user_message,
        architect_run=run,
    )
    exchange_path = store.root / conversation_id / "exchange-0001.json"
    payload = json.loads(exchange_path.read_text(encoding="utf-8"))
    payload["architect_run"]["reply"]["assistant_message"] = (
        "Authorization: Bearer actual-token-value-123456"
    )
    exchange_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PolicyViolation, match="conversation exchange"):
        store.load(conversation_id)


def test_launched_history_above_capacity_does_not_block_new_intake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)
    service = _service(project)
    seed = service.create_conversation(scenario_id="salesforce-vf-to-lwc")
    service.send_conversation_message(
        seed.conversation_id,
        message="Review the bounded Visualforce migration before launch.",
        scenario_id="salesforce-vf-to-lwc",
    )
    store = ArchitectConversationStore(project / ".runs/agent-ui/conversations")
    exchange = store.load(seed.conversation_id).exchanges[-1]
    for index in range(1, MAX_CONVERSATIONS + 2):
        _persist_launched_conversation_copy(store, f"{index:024x}", exchange)

    assert store.conversation_count() == MAX_CONVERSATIONS + 2
    assert store.unlaunched_conversation_count() == 1

    created = service.create_conversation(scenario_id=None)

    assert created.status == "open"
    assert store.conversation_count() == MAX_CONVERSATIONS + 3
    assert store.unlaunched_conversation_count() == 2


def test_max_unlaunched_conversations_blocks_new_intake(tmp_path: Path) -> None:
    project = _project(tmp_path)
    service = _service(project)
    for _ in range(MAX_CONVERSATIONS):
        service.create_conversation(scenario_id=None)

    store = ArchitectConversationStore(project / ".runs/agent-ui/conversations")
    assert store.conversation_count() == MAX_CONVERSATIONS
    assert store.unlaunched_conversation_count() == MAX_CONVERSATIONS

    with pytest.raises(AgentUiError) as capacity:
        service.create_conversation(scenario_id=None)

    assert capacity.value.code == "conversation_capacity_reached"


def test_malformed_owned_conversation_counts_toward_unlaunched_capacity(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    service = _service(project)
    for _ in range(MAX_CONVERSATIONS - 1):
        service.create_conversation(scenario_id=None)
    store = ArchitectConversationStore(project / ".runs/agent-ui/conversations")
    ArtifactStore(store.root).write_json(
        f"{'f' * 24}/header.json",
        {"schema_version": "1.0", "conversation_id": "not-a-valid-id"},
    )

    assert store.unlaunched_conversation_count() == MAX_CONVERSATIONS
    with pytest.raises(AgentUiError) as capacity:
        service.create_conversation(scenario_id=None)

    assert capacity.value.code == "conversation_capacity_reached"


def test_retired_pre_scenario_launch_receipt_releases_intake_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)
    service = _service(project)
    seed = service.create_conversation(scenario_id="salesforce-vf-to-lwc")
    service.send_conversation_message(
        seed.conversation_id,
        message="Review the bounded Visualforce migration before launch.",
        scenario_id="salesforce-vf-to-lwc",
    )
    store = ArchitectConversationStore(project / ".runs/agent-ui/conversations")
    exchange = store.load(seed.conversation_id).exchanges[-1]
    raw_exchange = exchange.model_dump(mode="json")
    raw_exchange.pop("scenario_id")
    raw_exchange.pop("launch_contract_digest")
    raw_reply = raw_exchange["architect_run"]["reply"]
    refined_request = raw_reply.pop("advisory_summary")
    raw_reply["refined_request"] = refined_request
    model_revision = exchange.architect_run.model_call.model_revision
    assert model_revision is not None
    legacy_id = "e" * 24
    legacy_record = {
        "schema_version": "1.0",
        "handle": "d" * 24,
        "selected_platform": "salesforce",
        "refined_request_digest": (
            "sha256:" + hashlib.sha256(refined_request.encode("utf-8")).hexdigest()
        ),
        "model_revision": model_revision,
        "launch_token": "sha256:" + "c" * 64,
    }
    artifacts = ArtifactStore(store.root)
    artifacts.write_json(
        f"{legacy_id}/header.json",
        {
            "schema_version": "1.0",
            "conversation_id": legacy_id,
            "initial_platform": None,
        },
    )
    artifacts.write_json(f"{legacy_id}/exchange-0001.json", raw_exchange)
    artifacts.write_json(f"{legacy_id}/launch-intent.json", legacy_record)
    artifacts.write_json(f"{legacy_id}/launch.json", legacy_record)

    assert store.conversation_count() == 2
    assert store.unlaunched_conversation_count() == 1


def test_service_persists_public_turns_and_binds_one_model_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    bound_revisions: list[str] = []
    _stub_ollama(
        monkeypatch,
        project,
        role_calls=role_calls,
        bound_revisions=bound_revisions,
    )
    service = _service(project)

    created = service.create_conversation(scenario_id=None)
    first = service.send_conversation_message(
        created.conversation_id,
        message="Can you help me modernize a legacy integration?",
        scenario_id=None,
    )
    second = service.send_conversation_message(
        created.conversation_id,
        message="Use the Salesforce Visualforce to LWC slice.",
        scenario_id="salesforce-vf-to-lwc",
    )
    reloaded = service.get_conversation(created.conversation_id)

    assert first.status == "open"
    assert first.readiness.ready is False
    assert second.status == "ready"
    assert second.selected_platform == "salesforce"
    assert tuple(message.role for message in second.messages) == (
        "user",
        "architect",
        "user",
        "architect",
    )
    assert reloaded == second
    expected_contract_digest = artifact_digest(migration_launch_contract("salesforce-vf-to-lwc"))
    assert second.readiness.launch_contract_digest == expected_contract_digest
    persisted = ArchitectConversationStore(project / ".runs" / "agent-ui" / "conversations").load(
        created.conversation_id
    )
    assert persisted.exchanges[-1].launch_contract_digest == expected_contract_digest
    assert role_calls == [
        "_ArchitectConversationClarificationOutput",
        "_ArchitectConversationReadyOutput",
    ]
    assert bound_revisions == [LOCAL_MODEL_REVISION]
    run_entries = tuple(
        path.name for path in (project / ".runs" / "agent-ui").iterdir() if len(path.name) == 24
    )
    assert run_entries == ()


def test_schema_valid_exchange_tampering_is_rejected_before_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)
    service = _service(project)
    conversation = service.create_conversation(scenario_id="salesforce-vf-to-lwc")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        scenario_id="salesforce-vf-to-lwc",
    )
    exchange_path = (
        project
        / ".runs"
        / "agent-ui"
        / "conversations"
        / ready.conversation_id
        / "exchange-0001.json"
    )
    payload = json.loads(exchange_path.read_text(encoding="utf-8"))
    payload["architect_run"]["reply"]["advisory_summary"] = (
        "Replace the recorded advisory with different schema-valid prose."
    )
    exchange_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AgentUiError) as raised:
        service.launch_conversation(
            ready.conversation_id,
            launch_token=str(ready.readiness.launch_token),
        )
    assert raised.value.code == "conversation_unavailable"
    assert not (project / ".runs" / "agent-ui" / ready.conversation_id).exists()


def test_launch_contract_drift_after_intake_fails_before_run_or_manifest_model_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    _stub_ollama(monkeypatch, project, role_calls=role_calls)
    service = _service(project)
    conversation = service.create_conversation(scenario_id="salesforce-vf-to-lwc")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Explain the bounded Visualforce migration before launch.",
        scenario_id="salesforce-vf-to-lwc",
    )
    pre_drift_token = ready.readiness.launch_token
    pre_drift_digest = ready.readiness.launch_contract_digest
    assert pre_drift_token is not None
    assert pre_drift_digest is not None
    assert role_calls == ["_ArchitectConversationReadyOutput"]

    canonical = migration_launch_contract("salesforce-vf-to-lwc")
    drifted = canonical.model_copy(
        update={
            "approved_output_paths": (
                *canonical.approved_output_paths,
                "force-app/main/default/lwc/unreviewed/unreviewed.js",
            )
        }
    )
    assert artifact_digest(drifted) != pre_drift_digest
    monkeypatch.setattr(
        "legacy_migration_agent.application.architect_conversation.migration_launch_contract",
        lambda scenario_id: (
            drifted
            if scenario_id == "salesforce-vf-to-lwc"
            else migration_launch_contract(scenario_id)
        ),
    )
    role_calls.clear()

    with pytest.raises(AgentUiError) as raised:
        service.launch_conversation(
            conversation.conversation_id,
            launch_token=pre_drift_token,
        )

    assert raised.value.code == "conversation_unavailable"
    assert role_calls == []
    assert not any(len(path.name) == 24 for path in (project / ".runs" / "agent-ui").iterdir())


def test_launch_intent_reconciles_one_run_after_receipt_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    _stub_ollama(monkeypatch, project, role_calls=role_calls)
    service = _service(project)
    conversation = service.create_conversation(scenario_id="salesforce-vf-to-lwc")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        scenario_id="salesforce-vf-to-lwc",
    )

    original = ArchitectConversationStore.record_launch
    attempts = 0

    def fail_once(self, conversation_id: str, *, handle: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated receipt publication interruption")
        return original(self, conversation_id, handle=handle)

    monkeypatch.setattr(ArchitectConversationStore, "record_launch", fail_once)
    with pytest.raises(AgentUiError) as raised:
        service.launch_conversation(
            ready.conversation_id,
            launch_token=str(ready.readiness.launch_token),
        )
    assert raised.value.code == "conversation_unavailable"

    reconciled_conversation, reconciled_run = service.launch_conversation(
        ready.conversation_id,
        launch_token=str(ready.readiness.launch_token),
    )

    assert reconciled_conversation.status == "launched"
    assert reconciled_conversation.launch_handle == reconciled_run.handle
    run_entries = tuple(
        path.name for path in (project / ".runs" / "agent-ui").iterdir() if len(path.name) == 24
    )
    assert run_entries == (reconciled_run.handle,)
    assert role_calls.count("ArchitectManifestProposal") == 1

    repeated_conversation, repeated_run = service.launch_conversation(
        ready.conversation_id,
        launch_token=str(ready.readiness.launch_token),
    )
    assert repeated_conversation == reconciled_conversation
    assert repeated_run == reconciled_run
    assert attempts == 2
    assert role_calls.count("ArchitectManifestProposal") == 1


def test_launch_intent_rejects_a_run_with_a_different_scenario_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)
    service = _service(project)
    conversation = service.create_conversation(scenario_id="salesforce-vf-to-lwc")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        scenario_id="salesforce-vf-to-lwc",
    )
    foreign_run = service.start(migration_launch_contract("mulesoft-mule3-to-mule4"))
    store = ArchitectConversationStore(project / ".runs" / "agent-ui" / "conversations")
    store.begin_launch(
        ready.conversation_id,
        handle=foreign_run.handle,
        expected_launch_token=str(ready.readiness.launch_token),
    )

    with pytest.raises(AgentUiError) as raised:
        service.launch_conversation(
            ready.conversation_id,
            launch_token=str(ready.readiness.launch_token),
        )

    assert raised.value.code == "conversation_unavailable"
    conversation_root = store.root / ready.conversation_id
    assert (conversation_root / "launch-intent.json").is_file()
    assert not (conversation_root / "launch.json").exists()


def test_launch_fails_closed_when_ollama_alias_drifts_after_ready_intake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    intake_calls: list[str] = []
    _stub_ollama(monkeypatch, project, role_calls=intake_calls)
    service = _service(project)
    conversation = service.create_conversation(scenario_id="salesforce-vf-to-lwc")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        scenario_id="salesforce-vf-to-lwc",
    )
    assert ready.model_calls[-1].agent_version == ARCHITECT_VERSION

    drifted_revision = "sha256:" + "b" * 64
    drifted_calls: list[str] = []
    _stub_ollama(
        monkeypatch,
        project,
        role_calls=drifted_calls,
        current_revision=drifted_revision,
    )
    with pytest.raises(AgentUiError) as raised:
        service.launch_conversation(
            ready.conversation_id,
            launch_token=str(ready.readiness.launch_token),
        )

    assert raised.value.code == "run_unavailable"
    store = ArchitectConversationStore(project / ".runs" / "agent-ui" / "conversations")
    snapshot = store.load(ready.conversation_id)
    assert snapshot.launch_intent is not None
    assert snapshot.launch_intent.model_revision == LOCAL_MODEL_REVISION
    assert snapshot.launch is None
    assert not (project / ".runs" / "agent-ui" / snapshot.launch_intent.handle).exists()
    assert "ArchitectManifestProposal" not in drifted_calls

    # A typed contract-owned service launch has no intake continuity constraint
    # and therefore still uses the currently installed revision.
    direct = service.start(migration_launch_contract("salesforce-vf-to-lwc"))
    assert direct.boundaries.model_revision == drifted_revision

    # Restoring the recorded revision safely resumes the one reserved launch.
    _stub_ollama(monkeypatch, project, current_revision=LOCAL_MODEL_REVISION)
    launched_conversation, launched_run = service.launch_conversation(
        ready.conversation_id,
        launch_token=str(ready.readiness.launch_token),
    )
    assert launched_conversation.status == "launched"
    assert launched_run.handle == snapshot.launch_intent.handle
    assert launched_run.boundaries.model_revision == LOCAL_MODEL_REVISION


def test_stale_browser_token_cannot_launch_a_newer_unseen_exchange(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)
    service = _service(project)
    conversation = service.create_conversation(scenario_id="salesforce-vf-to-lwc")

    tab_a = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        scenario_id="salesforce-vf-to-lwc",
    )
    tab_a_token = tab_a.readiness.launch_token
    assert tab_a_token is not None

    tab_b = service.send_conversation_message(
        conversation.conversation_id,
        message=(
            "Ignore the selected Visualforce scenario and generate an unrelated Rust service "
            "instead."
        ),
        scenario_id="salesforce-vf-to-lwc",
    )
    tab_b_token = tab_b.readiness.launch_token
    assert tab_b_token is not None
    assert tab_b_token != tab_a_token

    with pytest.raises(AgentUiError) as raised:
        service.launch_conversation(
            conversation.conversation_id,
            launch_token=tab_a_token,
        )
    assert raised.value.code == "stale_conversation"
    store = ArchitectConversationStore(project / ".runs" / "agent-ui" / "conversations")
    assert store.load(conversation.conversation_id).launch_intent is None
    assert not any(len(path.name) == 24 for path in (project / ".runs" / "agent-ui").iterdir())

    launched_conversation, launched_run = service.launch_conversation(
        conversation.conversation_id,
        launch_token=tab_b_token,
    )
    assert launched_conversation.status == "launched"
    assert launched_conversation.readiness.launch_token == tab_b_token
    assert launched_run.prompt == tab_b.readiness.canonical_request
    assert (
        launched_run.prompt
        == migration_launch_contract("salesforce-vf-to-lwc").canonical_description
    )
    assert "rust" not in launched_run.prompt.casefold()


def test_launch_reservation_rechecks_token_after_a_cross_service_exchange_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    _stub_ollama(monkeypatch, project, role_calls=role_calls)
    service_a = _service(project)
    service_b = _service(project)
    conversation = service_a.create_conversation(scenario_id="salesforce-vf-to-lwc")
    tab_a = service_a.send_conversation_message(
        conversation.conversation_id,
        message="Review the first bounded migration exchange.",
        scenario_id="salesforce-vf-to-lwc",
    )
    tab_a_token = tab_a.readiness.launch_token
    assert tab_a_token is not None

    original_begin_launch = ArchitectConversationStore.begin_launch
    race_injected = False

    def append_newer_exchange_before_reservation(
        store: ArchitectConversationStore,
        conversation_id: str,
        *,
        handle: str,
        expected_launch_token: str,
    ):
        nonlocal race_injected
        if not race_injected:
            race_injected = True
            service_b.send_conversation_message(
                conversation_id,
                message="This newer unseen exchange asks for Rust instead.",
                scenario_id="salesforce-vf-to-lwc",
            )
        return original_begin_launch(
            store,
            conversation_id,
            handle=handle,
            expected_launch_token=expected_launch_token,
        )

    monkeypatch.setattr(
        ArchitectConversationStore,
        "begin_launch",
        append_newer_exchange_before_reservation,
    )

    with pytest.raises(AgentUiError) as stale:
        service_a.launch_conversation(
            conversation.conversation_id,
            launch_token=tab_a_token,
        )

    assert stale.value.code == "stale_conversation"
    store = ArchitectConversationStore(project / ".runs" / "agent-ui" / "conversations")
    raced_snapshot = store.load(conversation.conversation_id)
    assert len(raced_snapshot.exchanges) == 2
    assert raced_snapshot.launch_intent is None
    assert not any(len(path.name) == 24 for path in (project / ".runs" / "agent-ui").iterdir())

    current = service_a.get_conversation(conversation.conversation_id)
    assert current.readiness.launch_token is not None
    assert current.readiness.launch_token != tab_a_token
    launched, run = service_a.launch_conversation(
        conversation.conversation_id,
        launch_token=current.readiness.launch_token,
    )
    assert launched.status == "launched"
    assert run.prompt == migration_launch_contract("salesforce-vf-to-lwc").canonical_description
    assert "rust" not in run.prompt.casefold()


def test_concurrent_exact_token_launches_converge_on_one_run_and_one_manifest_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    base_client = make_ollama_client_test_double(project, role_calls=role_calls)
    manifest_started = threading.Event()
    release_manifest = threading.Event()

    class BlockingManifestClient(base_client):
        def parse(self, *, system_prompt, input_value, output_type):
            if issubclass(output_type, ArchitectManifestProposal):
                manifest_started.set()
                assert release_manifest.wait(timeout=10)
            return super().parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        BlockingManifestClient,
    )
    service_a = _service(project)
    service_b = _service(project)
    conversation = service_a.create_conversation(scenario_id="salesforce-vf-to-lwc")
    ready = service_a.send_conversation_message(
        conversation.conversation_id,
        message="Review the bounded Visualforce migration before launch.",
        scenario_id="salesforce-vf-to-lwc",
    )
    launch_token = ready.readiness.launch_token
    assert launch_token is not None

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            service_a.launch_conversation,
            conversation.conversation_id,
            launch_token=launch_token,
        )
        assert manifest_started.wait(timeout=10)
        second = executor.submit(
            service_b.launch_conversation,
            conversation.conversation_id,
            launch_token=launch_token,
        )
        release_manifest.set()
        first_conversation, first_run = first.result(timeout=10)
        second_conversation, second_run = second.result(timeout=10)

    assert first_conversation.status == "launched"
    assert second_conversation == first_conversation
    assert second_run == first_run
    assert role_calls == ["_ArchitectConversationReadyOutput", "ArchitectManifestProposal"]
    repeated_conversation, repeated_run = service_b.launch_conversation(
        conversation.conversation_id,
        launch_token=launch_token,
    )
    assert repeated_conversation == first_conversation
    assert repeated_run == first_run
    assert role_calls == ["_ArchitectConversationReadyOutput", "ArchitectManifestProposal"]
    run_entries = tuple(
        path.name for path in (project / ".runs" / "agent-ui").iterdir() if len(path.name) == 24
    )
    assert run_entries == (first_run.handle,)


def test_controlled_manifest_architect_failure_records_and_recovers_one_terminal_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    working_client = make_ollama_client_test_double(project, role_calls=role_calls)
    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        working_client,
    )
    service = _service(project)
    conversation = service.create_conversation(scenario_id="salesforce-vf-to-lwc")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Review the bounded Visualforce migration before launch.",
        scenario_id="salesforce-vf-to-lwc",
    )
    launch_token = ready.readiness.launch_token
    assert launch_token is not None

    class FailingManifestClient(working_client):
        def parse(self, *, system_prompt, input_value, output_type):
            if issubclass(output_type, ArchitectManifestProposal):
                role_calls.append(output_type.__name__)
                raise RuntimeError("simulated manifest Architect failure")
            return super().parse(
                system_prompt=system_prompt,
                input_value=input_value,
                output_type=output_type,
            )

    monkeypatch.setattr(
        "legacy_migration_agent.application.agent_run.OllamaStructuredModelClient",
        FailingManifestClient,
    )

    launched, failed_run = service.launch_conversation(
        conversation.conversation_id,
        launch_token=launch_token,
    )

    assert launched.status == "launched"
    assert launched.launch_handle == failed_run.handle
    assert failed_run.status == "failed"
    assert failed_run.terminal_disposition == "controlled_failure"
    assert failed_run.failure is not None
    assert failed_run.failure.seam == "architect"
    assert failed_run.boundaries.model_call_record_persisted is False
    assert failed_run.boundaries.model_revision is None
    persisted = ArchitectConversationStore(project / ".runs" / "agent-ui" / "conversations").load(
        conversation.conversation_id
    )
    assert persisted.launch_intent is not None
    assert persisted.launch is not None
    assert persisted.launch.handle == failed_run.handle
    assert role_calls == ["_ArchitectConversationReadyOutput", "ArchitectManifestProposal"]

    repeated_conversation, repeated_run = service.launch_conversation(
        conversation.conversation_id,
        launch_token=launch_token,
    )
    assert repeated_conversation == launched
    assert repeated_run == failed_run
    assert service.latest() == failed_run
    assert role_calls == ["_ArchitectConversationReadyOutput", "ArchitectManifestProposal"]
    run_entries = tuple(
        path.name for path in (project / ".runs" / "agent-ui").iterdir() if len(path.name) == 24
    )
    assert run_entries == (failed_run.handle,)


def test_incomplete_reserved_bootstrap_recovers_the_exact_conversation_run_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    _stub_ollama(monkeypatch, project, role_calls=role_calls)
    service = _service(project)
    conversation = service.create_conversation(scenario_id="salesforce-vf-to-lwc")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Review the bounded Visualforce migration before launch.",
        scenario_id="salesforce-vf-to-lwc",
    )
    launch_token = ready.readiness.launch_token
    assert launch_token is not None

    original_write = agent_run_module._write_run_evidence
    monkeypatch.setattr(
        agent_run_module,
        "_write_run_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("simulated interruption after session reservation")
        ),
    )
    with pytest.raises(AgentUiError) as interrupted:
        service.launch_conversation(
            conversation.conversation_id,
            launch_token=launch_token,
        )
    assert interrupted.value.code == "run_unavailable"

    store = ArchitectConversationStore(project / ".runs" / "agent-ui" / "conversations")
    pending = store.load(conversation.conversation_id)
    assert pending.launch_intent is not None
    assert pending.launch is None
    reserved_handle = pending.launch_intent.handle
    assert (project / ".runs" / "agent-ui" / reserved_handle).is_dir()
    assert role_calls == ["_ArchitectConversationReadyOutput"]

    monkeypatch.setattr(agent_run_module, "_write_run_evidence", original_write)
    launched, recovered = service.launch_conversation(
        conversation.conversation_id,
        launch_token=launch_token,
    )

    assert launched.status == "launched"
    assert recovered.handle == reserved_handle
    assert recovered.status == "awaiting_approval"
    assert role_calls == ["_ArchitectConversationReadyOutput", "ArchitectManifestProposal"]
    repeated_conversation, repeated_run = service.launch_conversation(
        conversation.conversation_id,
        launch_token=launch_token,
    )
    assert repeated_conversation == launched
    assert repeated_run == recovered
    assert role_calls == ["_ArchitectConversationReadyOutput", "ArchitectManifestProposal"]
    run_entries = tuple(
        path.name for path in (project / ".runs" / "agent-ui").iterdir() if len(path.name) == 24
    )
    assert run_entries == (reserved_handle,)


def test_pending_launch_rejects_messages_before_invoking_the_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    _stub_ollama(monkeypatch, project, role_calls=role_calls)
    service = _service(project)
    conversation = service.create_conversation(scenario_id="salesforce-vf-to-lwc")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        scenario_id="salesforce-vf-to-lwc",
    )
    launch_token = ready.readiness.launch_token
    assert launch_token is not None
    assert role_calls == ["_ArchitectConversationReadyOutput"]

    store = ArchitectConversationStore(project / ".runs" / "agent-ui" / "conversations")
    reserved_handle = "c" * 24
    store.begin_launch(
        conversation.conversation_id,
        handle=reserved_handle,
        expected_launch_token=launch_token,
    )
    pending = service.get_conversation(conversation.conversation_id)

    assert pending.status == "launch_pending"
    assert pending.readiness.ready is True
    assert pending.readiness.launch_token == launch_token
    assert pending.launch_handle is None

    with pytest.raises(AgentUiError) as raised:
        service.send_conversation_message(
            conversation.conversation_id,
            message="Change the request after launch began.",
            scenario_id="salesforce-vf-to-lwc",
        )
    assert raised.value.code == "conversation_launch_pending"
    assert role_calls == ["_ArchitectConversationReadyOutput"]
    assert len(store.load(conversation.conversation_id).exchanges) == 1

    launched_conversation, launched_run = service.launch_conversation(
        conversation.conversation_id,
        launch_token=launch_token,
    )
    assert launched_conversation.status == "launched"
    assert launched_run.handle == reserved_handle
