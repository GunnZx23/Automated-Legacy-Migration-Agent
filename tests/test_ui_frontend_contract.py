from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

STATIC_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "legacy_migration_agent" / "ui" / "static"
)


class _ElementAttributes(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str | None]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag
        values = dict(attrs)
        identifier = values.get("id")
        if identifier:
            self.by_id[identifier] = values


def _frontend() -> tuple[str, str, str]:
    return tuple(
        (STATIC_ROOT / name).read_text(encoding="utf-8")
        for name in ("index.html", "app.js", "styles.css")
    )  # type: ignore[return-value]


def test_new_chat_is_initially_visible_and_not_gate_disabled() -> None:
    page, script, _stylesheet = _frontend()
    parser = _ElementAttributes()
    parser.feed(page)
    attributes = parser.by_id["new-chat-button"]

    assert "hidden" not in attributes
    assert "disabled" not in attributes
    composer_state = script[
        script.index("function updateComposerState()") : script.index(
            "function renderHarnessStages", script.index("function updateComposerState()")
        )
    ]
    assert "elements.newChatButton.hidden = false;" in composer_state
    assert "elements.newChatButton.disabled = state.busy;" in composer_state
    assert "awaiting_approval" not in composer_state

    new_chat = script[
        script.index('async function startNewChat(initialDraft = "")') : script.index(
            "function renderFailureDiagnostic",
            script.index('async function startNewChat(initialDraft = "")'),
        )
    ]
    assert "if (state.busy)" in new_chat
    assert "awaiting_approval" not in new_chat
    assert 'api("/api/conversations"' in script
    assert "markNewConversationRequested();" in new_chat
    assert "state.run = null;" in new_chat
    assert 'method: "DELETE"' not in new_chat
    assert "let conversationOpened = false;" in new_chat
    assert "conversationOpened = true;" in new_chat
    assert new_chat.index("state.busy = false;") < new_chat.index("elements.requestInput.focus();")
    assert new_chat.index("updateComposerState();", new_chat.index("finally")) < new_chat.index(
        "elements.requestInput.focus();"
    )
    assert (
        'elements.focusRequestButton.addEventListener("click", '
        '() => startNewChat(state.run?.prompt || ""));'
    ) in script
    assert 'typeof initialDraft === "string" ? initialDraft.slice(0, 2000) : ""' in new_chat


def test_runtime_readiness_is_server_owned_and_gates_model_requests() -> None:
    page, script, _stylesheet = _frontend()

    assert 'role="status" aria-live="polite"' in page
    assert 'const readinessResponse = await api("/api/readiness");' in script
    assert "state.modelReadiness = modelReadinessFromPayload" in script
    assert "payload.provider !== state.model?.provider" in script
    assert "payload.model_id !== state.model?.model_id" in script
    assert "payload.configured !== true" in script
    assert "state.modelReadiness?.ollama_reachable === true" in script
    assert "state.modelReadiness?.model_installed === true" in script
    assert "!modelRuntimeReady()" in script
    assert "Ollama reachable · model installed" in script
    assert "Ollama configured · runtime unreachable" in script
    assert "Ollama reachable · model unavailable" in script
    assert "Ollama reachable · model unverified" in script
    assert "provider-select" not in page
    assert "selectedProviderId" not in script


def test_composer_sends_chat_and_launches_migration_separately() -> None:
    page, script, _stylesheet = _frontend()
    parser = _ElementAttributes()
    parser.feed(page)

    request = parser.by_id["request-input"]
    assert request["minlength"] == "1"
    assert request["maxlength"] == "2000"
    assert "disabled" not in request
    assert "launch-button" in parser.by_id
    assert "hidden" in parser.by_id["launch-button"]
    assert "launch-preview" in parser.by_id
    assert "hidden" in parser.by_id["launch-preview"]

    send = script[
        script.index("async function sendConversationMessage") : script.index(
            "async function launchMigration"
        )
    ]
    launch = script[
        script.index("async function launchMigration") : script.index(
            "async function submitDecision"
        )
    ]
    assert "`/api/conversations/${messageConversationId}/messages`" in send
    assert "message," in send
    assert "platform: state.selectedPlatform || null" in send
    assert 'api("/api/sessions"' not in send
    assert "`/api/conversations/${launchConversationId}/launch`" in launch
    assert "body: { launch_token: readiness.launchToken }" in launch
    assert "payload.run || payload.session || payload.run_view" in launch
    assert 'error.code === "stale_conversation"' in launch
    assert "await api(`/api/conversations/${launchConversationId}`)" in launch
    assert "const readyToLaunch =" in script
    assert "modelRuntimeReady()" in script
    assert "!state.platformSelectionDirty" in script
    assert "!state.conversationSyncUncertain" in script
    assert (
        "elements.launchButton.hidden = runDisplayed || conversationLaunched || !previewAvailable;"
        in script
    )
    assert "readiness.launchToken !== null" in script
    assert 'elements.requestForm.addEventListener("submit", sendConversationMessage)' in script
    assert 'elements.launchButton.addEventListener("click", launchMigration)' in script


def test_conversation_identity_is_persisted_and_restored_before_run_fallback() -> None:
    _page, script, _stylesheet = _frontend()

    assert "CONVERSATION_ID_STORAGE_KEY" in script
    assert "window.localStorage.setItem(CONVERSATION_ID_STORAGE_KEY, conversationId)" in script
    assert "window.localStorage.getItem(CONVERSATION_ID_STORAGE_KEY)" in script
    assert "const response = await api(`/api/conversations/${conversationId}`);" in script
    assert script.index("await restoreStoredConversation();") < script.index(
        "await restoreStoredRun();"
    )
    send = script[
        script.index("async function sendConversationMessage") : script.index(
            "async function launchMigration"
        )
    ]
    auto_create = send[send.index("conversation = await createConversation") :]
    assert auto_create.index("markNewConversationRequested();") < auto_create.index(
        "rememberConversationId(conversation.conversation_id);"
    )
    restore = script[
        script.index("async function restoreStoredConversation()") : script.index(
            "async function restoreStoredRun()"
        )
    ]
    assert "rememberConversationId(conversation.conversation_id);" in restore
    assert (
        'conversation.status === "launched" && validRunHandle(conversation.launch_handle)'
        in restore
    )
    assert "clearStoredRunHandle();" in restore
    assert restore.index("clearStoredRunHandle();") < restore.index(
        "renderConversationView(conversation);"
    )


def test_ambiguous_message_response_refetches_and_blocks_stale_launch() -> None:
    _page, script, _stylesheet = _frontend()
    send = script[
        script.index("async function sendConversationMessage") : script.index(
            "async function launchMigration"
        )
    ]
    composer = script[
        script.index("function updateComposerState()") : script.index(
            "function renderHarnessStages", script.index("function updateComposerState()")
        )
    ]
    launch = script[
        script.index("async function launchMigration") : script.index(
            "async function submitDecision"
        )
    ]

    assert "messageRequestStarted = true;" in send
    assert "await api(`/api/conversations/${messageConversationId}`)" in send
    assert (
        "state.conversationSyncUncertain = messageRequestStarted && !recoveredConversation;" in send
    )
    assert "!state.conversationSyncUncertain" in composer
    assert "state.conversationSyncUncertain" in launch


def test_reviewer_is_clearly_a_human_audit_identity() -> None:
    _page, script, stylesheet = _frontend()

    assert script.count('textElement("label", "", "Human reviewer ID")') == 2
    assert script.count('"capstone-author"') == 2
    assert script.count("not another agent") == 2
    assert ".decision-help" in stylesheet


def test_ready_request_is_rendered_before_explicit_launch() -> None:
    page, script, stylesheet = _frontend()

    assert "Refined request to launch" in page
    assert "Review this exact text before starting the migration." in page
    assert 'launchPreviewRequest: document.querySelector("#launch-preview-request")' in script
    assert "elements.launchPreviewRequest.textContent = previewAvailable ? refinedRequest" in script
    assert "elements.launchPreviewPlatform.textContent = previewAvailable" in script
    assert ".launch-preview" in stylesheet


def test_launch_pending_freezes_intake_but_keeps_new_chat_and_retry_available() -> None:
    _page, script, _stylesheet = _frontend()
    composer = script[
        script.index("function updateComposerState()") : script.index(
            "function renderHarnessStages", script.index("function updateComposerState()")
        )
    ]
    send = script[
        script.index("async function sendConversationMessage") : script.index(
            "async function launchMigration"
        )
    ]

    assert 'const launchPending = conversationStatus === "launch_pending";' in composer
    assert "const conversationFrozen = launchPending || conversationLaunched;" in composer
    assert (
        "elements.requestInput.disabled = state.busy || runDisplayed || conversationFrozen;"
        in composer
    )
    assert "elements.newChatButton.hidden = false;" in composer
    assert "elements.newChatButton.disabled = state.busy;" in composer
    assert 'state.conversation?.status === "launch_pending"' in send
    assert "Retry Start migration" in send
