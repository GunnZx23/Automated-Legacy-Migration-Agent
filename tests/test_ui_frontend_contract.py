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


def test_failure_titles_include_unresolved_architect_risk_contract() -> None:
    _page, script, _stylesheet = _frontend()

    assert (
        'unresolved_question_risk_missing: "The Architect returned blocking questions '
        'without a corresponding human-decision risk."'
    ) in script


def test_engineer_policy_rejections_show_safe_reason_context() -> None:
    _page, script, _stylesheet = _frontend()

    for reason_code in (
        "correction_delta_required",
        "correction_no_material_changes",
        "correction_signal_coverage_missing",
        "correction_scope_invalid",
        "correction_identical_candidate",
        "file_plan_scope_mismatch",
        "file_plan_delta_mismatch",
        "workspace_scope_mismatch",
        "workspace_not_clean",
        "attempt_two_scope_expansion_invalid",
    ):
        assert f"{reason_code}:" in script
    assert "Controller reason (${failure.reason_code}): ${failure.summary}" in script
    assert "Reason code: ${failure.reason_code}" in script


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


def test_composer_sends_chat_and_inline_gate_launches_migration_separately() -> None:
    page, script, _stylesheet = _frontend()
    parser = _ElementAttributes()
    parser.feed(page)

    request = parser.by_id["request-input"]
    assert request["minlength"] == "1"
    assert request["maxlength"] == "2000"
    assert "disabled" not in request
    assert "launch-button" not in parser.by_id
    assert "launch-preview" not in parser.by_id

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
    assert "scenario_id: state.selectedScenarioId || null" in send
    assert "platform:" not in send
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
    assert "readiness.launchToken !== null" in script
    assert "readiness.scenarioId === state.selectedScenarioId" in script
    assert 'start.dataset.launchAction = "start";' in script
    assert 'start.addEventListener("click", launchMigration);' in script
    assert 'elements.requestForm.addEventListener("submit", sendConversationMessage)' in script
    assert 'elements.launchButton.addEventListener("click", launchMigration)' not in script
    assert 'label: "Scenario launch gate"' in script


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
    assert script.count('"capstone-author"') == 3
    assert script.count("not another agent") == 3
    assert ".decision-help" in stylesheet


def test_final_review_is_an_independent_non_authorizing_human_gate() -> None:
    page, script, stylesheet = _frontend()

    assert 'id="candidate-review-status"' in page
    gate = script[
        script.index("function finalReviewGate") : script.index(
            "function priorAttemptMessage", script.index("function finalReviewGate")
        )
    ]
    request = script[
        script.index("async function requestFinalReview") : script.index(
            "async function submitFinalReviewDecision",
            script.index("async function requestFinalReview"),
        )
    ]
    decide = script[
        script.index("async function submitFinalReviewDecision") : script.index(
            "async function submitRetry", script.index("async function submitFinalReviewDecision")
        )
    ]
    assert "not another agent or authenticated account" in gate
    assert "No choice authorizes Git, deployment, publication" in gate
    assert 'reviewer.value = "independent-reviewer";' in gate
    assert "request_changes" in gate
    assert "reviewer.readOnly = true;" in gate
    assert "`/api/sessions/${state.run.handle}/final-review/request`" in request
    assert "requester:" in request
    assert "designated_reviewer:" in request
    assert "`/api/sessions/${state.run.handle}/final-review/decision`" in decide
    assert '["accept", "reject", "request_changes"]' in decide
    assert ".final-review-facts" in stylesheet
    assert ".candidate-review-status" in stylesheet


def test_ready_request_is_rendered_in_inline_controller_gate() -> None:
    page, script, stylesheet = _frontend()

    assert 'id="launch-preview"' not in page
    gate = script[
        script.index("function conversationLaunchGate") : script.index(
            "function approvalGate", script.index("function conversationLaunchGate")
        )
    ]
    render = script[
        script.index("function renderConversationView") : script.index(
            "async function createConversation", script.index("function renderConversationView")
        )
    ]
    refine = script[
        script.index("function keepRefiningConversation") : script.index(
            "function conversationLaunchGate", script.index("function keepRefiningConversation")
        )
    ]
    assert '"Controller · explicit launch gate"' in gate
    assert 'launchPending ? "Retry this exact migration launch?" : "Start this migration?"' in gate
    assert 'textElement("p", "launch-gate-request", readiness.canonicalRequest)' in gate
    assert "Architect advisory:" in gate
    assert "readiness.advisorySummary" in gate
    assert "readiness.launchContractDigest" in gate
    assert "const scenario = scenarioById(readiness.scenarioId);" in gate
    assert "scenario.platform !== readiness.platform" in gate
    assert 'textElement("dt", "", "Source")' in gate
    assert 'textElement("dt", "", "Target")' in gate
    assert (
        'request.setAttribute("aria-label", "Controller-owned canonical migration request")' in gate
    )
    assert "Chat and Architect prose are advisory" in gate
    assert 'start.addEventListener("click", launchMigration);' in gate
    assert 'refine.addEventListener("click", keepRefiningConversation);' in gate
    assert "elements.requestInput.focus();" in refine
    assert "api(" not in refine
    assert "elements.conversation.append(launchGate);" in render
    assert ".launch-gate-request" in stylesheet
    assert ".scenario-boundary" in stylesheet


def test_controller_checks_and_validator_advisory_are_attributed_separately() -> None:
    page, script, _stylesheet = _frontend()

    assert "Authoritative checks → non-authoritative advisory" in page
    assert "Public model activity" in page
    controller = script[
        script.index("function controllerValidationMessage") : script.index(
            "function validatorMessage", script.index("function controllerValidationMessage")
        )
    ]
    validator = script[
        script.index("function validatorMessage") : script.index(
            "function completionMessage", script.index("function validatorMessage")
        )
    ]
    assert 'messageArticle(\n    "Controller"' in controller
    assert "controller-owned deterministic checks" in controller
    assert 'messageArticle(\n    "Validator"' in validator
    assert 'typeof assessment === "string"' in validator
    assert "assessment.replaceAll" not in validator
    assert "advisory unavailable" in validator


def test_architecture_attribution_separates_controller_and_agent_ownership() -> None:
    page, script, _stylesheet = _frontend()
    service_family = "\n".join(
        (STATIC_ROOT.parent / module).read_text(encoding="utf-8")
        for module in ("service.py", "projection.py")
    )

    working = script[
        script.index("function renderWorkingHarness") : script.index(
            "function setBusy", script.index("function renderWorkingHarness")
        )
    ]
    architect = script[
        script.index("function architectMessage") : script.index(
            "function reviewList", script.index("function architectMessage")
        )
    ]
    approval = script[
        script.index("function approvalGate") : script.index(
            "function correctionGate", script.index("function approvalGate")
        )
    ]

    assert "Controller → Architect" in page
    assert "Exact source + graph/Wiki evidence → semantic recommendation" in page
    assert "Controller expansion + digest-bound human decision" in page
    assert "Proposes isolated file content" in page
    assert "Authoritative checks → non-authoritative advisory" in page
    assert "Controller-retrieved LLM Wiki evidence" in page
    assert "Controller-expanded manifest paths" in page

    assert "Controller binds the exact source inputs" in working
    assert "builds the dependency graph" in working
    assert "retrieves curated Wiki evidence" in working
    assert "Architect then selects bounded evidence IDs" in working
    assert "Controller expands an accepted semantic recommendation" in working
    assert "Engineer proposes isolated file content" in working
    assert "it cannot change the deterministic disposition" in working

    assert "evidence-bound semantic recommendation" in architect
    assert "selected graph" in architect
    assert "selected Wiki" in architect
    assert "The Controller supplied exact digest-bound source evidence" in architect
    assert "built the dependency graph" in architect
    assert "paths, checks, implementation-contract text, approval actions" in architect
    assert "Selected graph ID" in script
    assert "Selected Wiki page ID" in script
    assert "Architect-selected evidence IDs" in script
    assert "Controller expanded the Architect's semantic recommendation" in approval

    assert "built" in service_family
    assert "Controller bound the exact source inputs" in service_family
    assert "Architect selected " in service_family
    assert "bounded graph/Wiki IDs" in service_family
    assert "Controller expanded " in service_family
    assert "it into the exact manifest" in service_family
    assert "the Architect does not author approval actions" in service_family
    assert "the Architect does not copy or author implementation-contract entries" in service_family
    assert "typed output fields, not native tool calls" in service_family

    for stale_claim in (
        "The Architect proposes",
        "Available after Architect analysis",
        "Mapping dependencies and retrieving Wiki guidance",
        "The Architect omitted the required human approval gate",
        "The Architect changed the controller-owned implementation contract",
        "the role is tool-free",
    ):
        assert stale_claim not in page
        assert stale_claim not in script
        assert stale_claim not in service_family


def test_saved_run_must_match_server_supplied_scenario_metadata() -> None:
    _page, script, _stylesheet = _frontend()

    render_run = script[
        script.index("function renderRun(run)") : script.index(
            "async function sendConversationMessage", script.index("function renderRun(run)")
        )
    ]
    assert "const scenario = scenarioById(run.scenario_id);" in render_run
    assert "scenario.platform !== run.platform" in render_run
    assert "scenario.title !== run.scenario_title" in render_run
    assert "state.selectedScenarioId = run.scenario_id;" in render_run


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
    gate = script[
        script.index("function conversationLaunchGate") : script.index(
            "function approvalGate", script.index("function conversationLaunchGate")
        )
    ]
    assert 'launchPending ? "Retry Start migration" : "Start migration"' in gate
    assert "if (!launchPending)" in gate
    assert 'textElement("button", "button button-secondary", "Keep chatting")' in gate


def test_interrupted_authorized_retry_renders_the_existing_resume_gate() -> None:
    _page, script, _stylesheet = _frontend()
    render_conversation = script[
        script.index("function renderConversation(run)") : script.index(
            "function renderPendingMessage", script.index("function renderConversation(run)")
        )
    ]

    assert 'run.status === "implementing"' in render_conversation
    assert "run.execution_attempt === 2" in render_conversation
    assert "run.correction?.retry_available" in render_conversation
    assert "run.correction?.approval" in render_conversation
    assert "execution stopped before the Engineer provider call began" in render_conversation
    assert "correctionGate(run)" in render_conversation


def test_validation_ui_distinguishes_root_failure_from_dependent_zero_test_checks() -> None:
    _page, script, stylesheet = _frontend()
    controller_message = script[
        script.index("function controllerValidationMessage") : script.index(
            "function validatorMessage", script.index("function controllerValidationMessage")
        )
    ]
    correction_gate = script[
        script.index("function correctionGate") : script.index(
            "function finalReviewGate", script.index("function correctionGate")
        )
    ]
    evidence = script[
        script.index("function renderEvidence") : script.index(
            "function setRunStatus", script.index("function renderEvidence")
        )
    ]

    assert "!result.dependent_on" in controller_message
    assert "Boolean(result.dependent_on)" in controller_message
    assert "root LWC load failure prevented all tests from running" in controller_message
    assert '"Root correction signals"' in correction_gate
    assert 'result.status === "failed" && !result.dependent_on' in evidence
    assert 'dependent ? "dependent" : result.status' in evidence
    assert "No tests ran because ${result.dependent_on}" in evidence
    assert 'root ${rootFailures === 1 ? "failure" : "failures"}' in evidence
    assert 'dependent ${dependentChecks === 1 ? "check" : "checks"}' in evidence
    assert ".validation-result.dependent" in stylesheet
    assert ".validation-result.dependent .validation-status" in stylesheet
