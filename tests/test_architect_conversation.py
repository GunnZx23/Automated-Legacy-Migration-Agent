from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel
from ui_test_doubles import (
    LOCAL_MODEL_REVISION,
    make_ollama_client_test_double,
)

from legacy_migration_agent.agent_runtime.agent_definitions import load_agent_registry
from legacy_migration_agent.agent_runtime.model_agents import (
    AgentRuntimeError,
    ArchitectAgent,
    ArchitectConversationContext,
    ArchitectConversationMessage,
    ArchitectConversationReply,
)
from legacy_migration_agent.application.architect_conversation import (
    ArchitectConversationStore,
)
from legacy_migration_agent.ui.service import AgentUiError, AgentUiService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "qwen3.6:latest"


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
        return self.reply


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

    assert run.reply == reply
    assert model.output_types == [ArchitectConversationReply]
    assert run.model_call.agent_version == "architect/v3"
    assert not hasattr(run.reply, "approved")
    assert not hasattr(run.reply, "run_id")


def test_architect_cannot_claim_ready_without_controller_selected_platform() -> None:
    context = ArchitectConversationContext(
        selected_platform=None,
        history=(ArchitectConversationMessage(role="user", content="Migrate this legacy app."),),
    )
    reply = ArchitectConversationReply(
        status="ready_to_launch",
        assistant_message="Ready.",
        refined_request="Migrate the bounded legacy application additively.",
    )

    with pytest.raises(AgentRuntimeError, match="controller-selected platform"):
        ArchitectAgent(
            load_agent_registry(PROJECT_ROOT / "agents"),
            _ConversationModel(reply),
        ).converse(context)


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

    created = service.create_conversation(platform=None)
    first = service.send_conversation_message(
        created.conversation_id,
        message="Can you help me modernize a legacy integration?",
        platform=None,
    )
    second = service.send_conversation_message(
        created.conversation_id,
        message="Use the Salesforce Visualforce to LWC slice.",
        platform="salesforce",
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
    assert role_calls == ["ArchitectConversationReply", "ArchitectConversationReply"]
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
    conversation = service.create_conversation(platform="salesforce")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        platform="salesforce",
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
    payload["architect_run"]["reply"]["refined_request"] = (
        "Replace the reviewed request with a different but schema-valid migration."
    )
    exchange_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AgentUiError) as raised:
        service.launch_conversation(
            ready.conversation_id,
            launch_token=str(ready.readiness.launch_token),
        )
    assert raised.value.code == "conversation_unavailable"
    assert not (project / ".runs" / "agent-ui" / ready.conversation_id).exists()


def test_launch_intent_reconciles_one_run_after_receipt_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    _stub_ollama(monkeypatch, project, role_calls=role_calls)
    service = _service(project)
    conversation = service.create_conversation(platform="salesforce")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        platform="salesforce",
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


@pytest.mark.parametrize(
    ("foreign_platform", "foreign_prompt"),
    (
        (
            "mulesoft",
            "Migrate the bounded Mule 3 fixture additively to Mule 4.",
        ),
        (
            "salesforce",
            "Migrate a different bounded Salesforce request than the refined conversation.",
        ),
    ),
)
def test_launch_intent_rejects_a_run_with_different_platform_or_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    foreign_platform: str,
    foreign_prompt: str,
) -> None:
    project = _project(tmp_path)
    _stub_ollama(monkeypatch, project)
    service = _service(project)
    conversation = service.create_conversation(platform="salesforce")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        platform="salesforce",
    )
    foreign_run = service.start(foreign_platform, prompt=foreign_prompt)
    store = ArchitectConversationStore(project / ".runs" / "agent-ui" / "conversations")
    store.begin_launch(ready.conversation_id, handle=foreign_run.handle)

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
    conversation = service.create_conversation(platform="salesforce")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        platform="salesforce",
    )
    assert ready.model_calls[-1].agent_version == "architect/v3"

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

    # Direct sessions have no intake continuity constraint and therefore still
    # use the currently installed revision.
    direct = service.start(
        "salesforce",
        prompt="Migrate the bounded Visualforce fixture in a separate direct session.",
    )
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
    conversation = service.create_conversation(platform="salesforce")

    tab_a = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        platform="salesforce",
    )
    tab_a_token = tab_a.readiness.launch_token
    assert tab_a_token is not None

    tab_b = service.send_conversation_message(
        conversation.conversation_id,
        message="Keep the same slice, but incorporate this newer clarification.",
        platform="salesforce",
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
    assert launched_run.prompt == tab_b.readiness.refined_request


def test_pending_launch_rejects_messages_before_invoking_the_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    role_calls: list[str] = []
    _stub_ollama(monkeypatch, project, role_calls=role_calls)
    service = _service(project)
    conversation = service.create_conversation(platform="salesforce")
    ready = service.send_conversation_message(
        conversation.conversation_id,
        message="Migrate the bounded Visualforce fixture to an additive LWC.",
        platform="salesforce",
    )
    launch_token = ready.readiness.launch_token
    assert launch_token is not None
    assert role_calls == ["ArchitectConversationReply"]

    store = ArchitectConversationStore(project / ".runs" / "agent-ui" / "conversations")
    reserved_handle = "c" * 24
    store.begin_launch(conversation.conversation_id, handle=reserved_handle)
    pending = service.get_conversation(conversation.conversation_id)

    assert pending.status == "launch_pending"
    assert pending.readiness.ready is True
    assert pending.readiness.launch_token == launch_token
    assert pending.launch_handle is None

    with pytest.raises(AgentUiError) as raised:
        service.send_conversation_message(
            conversation.conversation_id,
            message="Change the request after launch began.",
            platform="salesforce",
        )
    assert raised.value.code == "conversation_launch_pending"
    assert role_calls == ["ArchitectConversationReply"]
    assert len(store.load(conversation.conversation_id).exchanges) == 1

    launched_conversation, launched_run = service.launch_conversation(
        conversation.conversation_id,
        launch_token=launch_token,
    )
    assert launched_conversation.status == "launched"
    assert launched_run.handle == reserved_handle
