"use strict";

const state = {
  csrfToken: "",
  model: null,
  modelReadiness: null,
  scenarios: [],
  selectedPlatform: "",
  selectedScenarioId: "",
  platformSelectionDirty: false,
  conversationSyncUncertain: false,
  conversation: null,
  run: null,
  exportResult: null,
  busy: false,
  decision: null,
  retryApproval: null,
  inspectionView: "evidence",
  candidateView: "diff",
  selectedCandidatePath: "",
  busyStartedAt: null,
  busyTimer: null,
  progressTimer: null,
  progressRole: null,
};

const PROGRESS_POLL_INTERVAL_MS = 2500;
const LIVE_PROGRESS_ROLES = new Set([
  "Engineer → Validator",
  "Engineer correction → Validator",
]);

const RUN_HANDLE_STORAGE_KEY = "legacy-migration-agent.current-run-handle";
const CONVERSATION_ID_STORAGE_KEY = "legacy-migration-agent.current-conversation-id";
const RUN_HANDLE_PATTERN = /^[0-9a-f]{24}$/;
const CONVERSATION_ID_PATTERN = /^[0-9a-f]{24}$/;
const SHA256_PATTERN = /^sha256:[0-9a-f]{64}$/;

class AgentUiApiError extends Error {
  constructor(message, code, status) {
    super(message);
    this.name = "AgentUiApiError";
    this.code = code;
    this.status = status;
  }
}

const elements = {
  activityDisclosure: document.querySelector("#activity-disclosure"),
  alert: document.querySelector("#alert"),
  boundaryNotice: document.querySelector("#boundary-notice"),
  boundaryTitle: document.querySelector("#boundary-title"),
  candidateDiff: document.querySelector("#candidate-diff"),
  candidateProvenance: document.querySelector("#candidate-provenance"),
  candidateReviewStatus: document.querySelector("#candidate-review-status"),
  candidateSection: document.querySelector("#candidate-section"),
  changesTab: document.querySelector("#changes-tab"),
  changesView: document.querySelector("#changes-view"),
  changedFiles: document.querySelector("#changed-files"),
  codeViewerPath: document.querySelector("#code-viewer-path"),
  conversation: document.querySelector("#conversation"),
  decisionForm: null,
  diffViewButton: document.querySelector("#diff-view-button"),
  downloadButton: document.querySelector("#download-button"),
  evidenceTab: document.querySelector("#evidence-tab"),
  evidenceView: document.querySelector("#evidence-view"),
  exportButton: document.querySelector("#export-button"),
  exportStatus: document.querySelector("#export-status"),
  failureDiagnostic: document.querySelector("#failure-diagnostic"),
  failureFacts: document.querySelector("#failure-facts"),
  failureGuidance: document.querySelector("#failure-guidance"),
  failureTitle: document.querySelector("#failure-title"),
  fileCount: document.querySelector("#file-count"),
  fileCountBadge: document.querySelector("#file-count-badge"),
  fileViewButton: document.querySelector("#file-view-button"),
  focusRequestButton: document.querySelector("#focus-request-button"),
  graphSummary: document.querySelector("#graph-summary"),
  harnessStages: document.querySelector("#harness-stages"),
  manifestPaths: document.querySelector("#manifest-paths"),
  manifestPathsTitle: document.querySelector("#manifest-paths-title"),
  metrics: document.querySelector("#metrics"),
  modeBadge: document.querySelector("#mode-badge"),
  modelBadge: document.querySelector("#model-badge"),
  modelActivitySection: document.querySelector("#model-activity-section"),
  modelCallCount: document.querySelector("#model-call-count"),
  modelCallList: document.querySelector("#model-call-list"),
  modelRevisionBadge: document.querySelector("#model-revision-badge"),
  newChatButton: document.querySelector("#new-chat-button"),
  requestForm: document.querySelector("#request-form"),
  requestHelp: document.querySelector("#request-help"),
  requestInput: document.querySelector("#request-input"),
  runtimePulse: document.querySelector("#runtime-pulse"),
  runTimer: document.querySelector("#run-timer"),
  runStatus: document.querySelector("#run-status"),
  scenarioList: document.querySelector("#scenario-list"),
  sendButton: document.querySelector("#send-button"),
  sendLabel: document.querySelector("#send-button .button-label"),
  typingCopy: document.querySelector("#typing-copy"),
  typingIndicator: document.querySelector("#typing-indicator"),
  typingRole: document.querySelector("#typing-role"),
  validationResults: document.querySelector("#validation-results"),
  validationSection: document.querySelector("#validation-section"),
  validatorAssessment: document.querySelector("#validator-assessment"),
  validatorConcerns: document.querySelector("#validator-concerns"),
  validatorConcernsGroup: document.querySelector("#validator-concerns-group"),
  validatorSummary: document.querySelector("#validator-summary"),
  validationSummaryCount: document.querySelector("#validation-summary-count"),
  wikiHits: document.querySelector("#wiki-hits"),
  workspaceGrid: document.querySelector(".workspace-grid"),
};

const roleAvatars = {
  Architect: "A",
  Engineer: "E",
  Validator: "V",
  Controller: "C",
  You: "Y",
};

const roleDescriptions = {
  Architect: "Migration design",
  Engineer: "Candidate generation",
  Validator: "Independent review",
  Controller: "Policy & workflow",
  You: "Human reviewer",
};

const readyHarnessStages = [
  {
    key: "architect",
    label: "Architect",
    state: "ready",
    detail: "Ready for a normal migration conversation.",
  },
  {
    key: "approval",
    label: "Scenario launch gate",
    state: "waiting",
    detail: "Waits for a selected controller-owned contract and explicit Start migration.",
  },
  {
    key: "engineer",
    label: "Engineer",
    state: "waiting",
    detail: "Cannot run before explicit manifest approval.",
  },
  {
    key: "validator",
    label: "Validator",
    state: "waiting",
    detail: "Cannot run before an isolated candidate exists.",
  },
];

const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function scrollBehavior() {
  return prefersReducedMotion.matches ? "auto" : "smooth";
}

function clear(element) {
  element.replaceChildren();
}

function textElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  element.textContent = text;
  return element;
}

function humanize(value) {
  return String(value || "").replaceAll("_", " ");
}

function formatElapsed(milliseconds) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function updateRunTimer() {
  if (state.busyStartedAt === null) {
    return;
  }
  elements.runTimer.textContent = formatElapsed(Date.now() - state.busyStartedAt);
  // Tick the currently-active harness stage in lockstep so the big "What the
  // system is doing" panel advances every second, not just the header clock.
  const activeStatus = elements.harnessStages.querySelector(".harness-stage.is-active em");
  if (activeStatus) {
    activeStatus.textContent = liveStageStatus("active");
  }
}

function startRunTimer() {
  if (state.busyTimer !== null) {
    window.clearInterval(state.busyTimer);
  }
  state.busyStartedAt = Date.now();
  elements.runTimer.textContent = "0:00";
  elements.runTimer.hidden = false;
  state.busyTimer = window.setInterval(updateRunTimer, 1000);
}

function stopRunTimer() {
  if (state.busyTimer !== null) {
    window.clearInterval(state.busyTimer);
    state.busyTimer = null;
  }
  if (state.busyStartedAt !== null) {
    updateRunTimer();
    elements.runTimer.textContent = `${elements.runTimer.textContent} elapsed`;
    state.busyStartedAt = null;
  }
}

function selectInspectionView(view, focus = false) {
  const candidateAvailable = Boolean(state.run?.candidate);
  const selected = view === "changes" && candidateAvailable ? "changes" : "evidence";
  state.inspectionView = selected;
  const changesSelected = selected === "changes";
  elements.evidenceView.hidden = changesSelected;
  elements.changesView.hidden = !changesSelected;
  elements.evidenceTab.classList.toggle("is-active", !changesSelected);
  elements.changesTab.classList.toggle("is-active", changesSelected);
  elements.evidenceTab.setAttribute("aria-selected", String(!changesSelected));
  elements.changesTab.setAttribute("aria-selected", String(changesSelected));
  elements.workspaceGrid.classList.toggle("is-code-review", changesSelected);
  elements.evidenceTab.tabIndex = changesSelected ? -1 : 0;
  elements.changesTab.tabIndex = changesSelected ? 0 : -1;
  if (focus) {
    (changesSelected ? elements.changesTab : elements.evidenceTab).focus();
  }
}

function inspectionTabKeydown(event) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
    return;
  }
  event.preventDefault();
  if (event.key === "Home" || event.key === "ArrowLeft" || !state.run?.candidate) {
    selectInspectionView("evidence", true);
  } else {
    selectInspectionView("changes", true);
  }
}

function showAlert(message) {
  elements.alert.textContent = message;
  elements.alert.hidden = false;
  elements.alert.focus();
}

function clearAlert() {
  elements.alert.hidden = true;
  elements.alert.textContent = "";
}

function validRunHandle(value) {
  return typeof value === "string" && RUN_HANDLE_PATTERN.test(value);
}

function clearStoredRunHandle() {
  try {
    window.localStorage.removeItem(RUN_HANDLE_STORAGE_KEY);
  } catch (_error) {
    // Storage may be unavailable; the in-memory interaction remains usable.
  }
}

function validConversationId(value) {
  return typeof value === "string" && CONVERSATION_ID_PATTERN.test(value);
}

function clearStoredConversationId() {
  try {
    window.localStorage.removeItem(CONVERSATION_ID_STORAGE_KEY);
  } catch (_error) {
    // Storage may be unavailable; the in-memory conversation remains usable.
  }
}

function storedConversationId() {
  let conversationId = null;
  try {
    conversationId = window.localStorage.getItem(CONVERSATION_ID_STORAGE_KEY);
  } catch (_error) {
    return "";
  }
  if (!validConversationId(conversationId)) {
    clearStoredConversationId();
    return "";
  }
  return conversationId;
}

function rememberConversationId(conversationId) {
  if (!validConversationId(conversationId)) {
    clearStoredConversationId();
    return false;
  }
  try {
    window.localStorage.setItem(CONVERSATION_ID_STORAGE_KEY, conversationId);
  } catch (_error) {
    // Storage is optional; the durable server conversation remains authoritative.
  }
  return true;
}

function markNewConversationRequested() {
  clearStoredRunHandle();
  clearStoredConversationId();
}

function storedRunHandle() {
  let handle = null;
  try {
    handle = window.localStorage.getItem(RUN_HANDLE_STORAGE_KEY);
  } catch (_error) {
    return "";
  }
  if (!validRunHandle(handle)) {
    clearStoredRunHandle();
    return "";
  }
  return handle;
}

function rememberRunHandle(handle) {
  if (!validRunHandle(handle)) {
    clearStoredRunHandle();
    return false;
  }
  try {
    window.localStorage.setItem(RUN_HANDLE_STORAGE_KEY, handle);
  } catch (_error) {
    // Storage is optional; durable server evidence remains authoritative.
  }
  return true;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json; charset=utf-8");
  }
  if (options.method === "POST" || options.requiresCsrf) {
    headers.set("X-Agent-UI-CSRF", state.csrfToken);
  }
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    credentials: "same-origin",
  });
  if (!response.ok) {
    let message = "The agent request could not be completed.";
    let code = "request_failed";
    try {
      const payload = await response.json();
      if (payload.error && typeof payload.error.message === "string") {
        message = payload.error.message;
      }
      if (payload.error && typeof payload.error.code === "string") {
        code = payload.error.code;
      }
    } catch (_error) {
      // Keep the safe fallback; transport details are intentionally not displayed.
    }
    throw new AgentUiApiError(message, code, response.status);
  }
  return response;
}

function scenarioById(scenarioId) {
  return state.scenarios.find((scenario) => scenario.scenario_id === scenarioId) || null;
}

function scenarioForPlatform(platform) {
  return state.scenarios.find((scenario) => scenario.platform === platform) || null;
}

function conversationFromPayload(payload) {
  const view = payload?.conversation || payload?.view || payload;
  if (!view || !validConversationId(view.conversation_id)) {
    throw new AgentUiApiError(
      "The agent returned an invalid conversation identity.",
      "conversation_identity_mismatch",
      409,
    );
  }
  return view;
}

function conversationMessages(conversation) {
  const messages = conversation?.messages || conversation?.turns;
  return Array.isArray(messages) ? messages : [];
}

function conversationReadiness(conversation) {
  const readiness = conversation?.readiness || conversation?.latest || {};
  const ready = readiness.ready === true || readiness.status === "ready_to_plan";
  const launchToken =
    typeof readiness.launch_token === "string" && SHA256_PATTERN.test(readiness.launch_token)
      ? readiness.launch_token
      : null;
  return {
    ready,
    platform: readiness.platform || conversation?.selected_platform || null,
    scenarioId:
      readiness.scenario_id || conversation?.selected_scenario_id || null,
    canonicalRequest: readiness.canonical_request || null,
    advisorySummary: readiness.advisory_summary || null,
    launchContractDigest: readiness.launch_contract_digest || null,
    launchToken,
    missingInformation: Array.isArray(readiness.missing_information)
      ? readiness.missing_information
      : [],
  };
}

function conversationMessageContent(message) {
  for (const field of ["content", "message", "text", "assistant_message"]) {
    if (typeof message?.[field] === "string" && message[field].trim()) {
      return message[field].trim();
    }
  }
  return "The Architect returned an empty public message.";
}

function conversationMessageRole(message) {
  return message?.role === "user" || message?.role === "human" ? "You" : "Architect";
}

function configuredModelId() {
  if (!state.model || typeof state.model.model_id !== "string" || state.model.model_id.trim() === "") {
    return "Model identity unavailable";
  }
  return state.model.model_id;
}

const MODEL_RUNTIME_PRESENTATIONS = Object.freeze({
  ollama: Object.freeze({
    executionBoundary: "local_loopback",
    providerLabel: "Ollama",
    inferenceLabel: "Local Ollama",
    runtimeLabel: "local Ollama runtime",
    boundaryTitle: "Local Ollama inference boundary",
    remote: false,
  }),
  "claude-cli": Object.freeze({
    executionBoundary: "remote_provider_managed",
    providerLabel: "Claude CLI",
    inferenceLabel: "Claude CLI remote",
    runtimeLabel: "authenticated Claude CLI runtime",
    boundaryTitle: "Remote provider-managed inference boundary",
    remote: true,
  }),
});

function modelRuntimePresentation(model = state.model) {
  const providerId = model?.provider_id || model?.provider;
  if (
    typeof providerId !== "string" ||
    !Object.hasOwn(MODEL_RUNTIME_PRESENTATIONS, providerId)
  ) {
    return null;
  }
  return MODEL_RUNTIME_PRESENTATIONS[providerId];
}

function validModelConfiguration(model) {
  const presentation = modelRuntimePresentation(model);
  return Boolean(
    presentation &&
      typeof model?.model_id === "string" &&
      model.model_id.trim() !== "" &&
      model.execution_boundary === presentation.executionBoundary,
  );
}

function configuredProviderLabel() {
  return modelRuntimePresentation()?.providerLabel || "Model provider";
}

function configuredInferenceLabel() {
  return modelRuntimePresentation()?.inferenceLabel || "Configured model";
}

function providerRecoveryGuidance(modelId, boundaries = null) {
  const runtime = modelRuntimePresentation(boundaries) || modelRuntimePresentation();
  if (runtime?.remote) {
    return `Confirm the Claude CLI is installed, authenticated, and authorized for ${modelId}, then send a new request.`;
  }
  if (runtime?.providerLabel === "Ollama") {
    return `Confirm Ollama is running and ${modelId} is installed, then send a new request.`;
  }
  return `Confirm the configured provider can reach ${modelId}, then send a new request.`;
}

function modelRuntimeReady() {
  return (
    state.modelReadiness?.status === "ready" &&
    state.modelReadiness?.configured === true &&
    state.modelReadiness?.runtime_reachable === true &&
    state.modelReadiness?.model_available === true
  );
}

function modelReadinessFromPayload(payload) {
  const allowedStatuses = new Set([
    "ready",
    "provider_unreachable",
    "model_unavailable",
    "runtime_unverified",
    "readiness_unavailable",
  ]);
  if (
    !payload ||
    payload.provider !== state.model?.provider ||
    payload.model_id !== state.model?.model_id ||
    payload.configured !== true ||
    !allowedStatuses.has(payload.status) ||
    ![true, false, null].includes(payload.runtime_reachable) ||
    ![true, false, null].includes(payload.model_available)
  ) {
    throw new AgentUiApiError(
      "The model-provider readiness response was invalid.",
      "readiness_invalid",
      409,
    );
  }
  if (
    (payload.status === "ready" &&
      (payload.runtime_reachable !== true || payload.model_available !== true)) ||
    (payload.status === "provider_unreachable" && payload.runtime_reachable !== false) ||
    (payload.status === "provider_unreachable" && payload.model_available !== null) ||
    (payload.status === "model_unavailable" &&
      (payload.runtime_reachable !== true || payload.model_available !== false)) ||
    (payload.status === "runtime_unverified" &&
      (payload.runtime_reachable !== true || payload.model_available !== null)) ||
    (payload.status === "readiness_unavailable" &&
      (payload.runtime_reachable !== null || payload.model_available !== null))
  ) {
    throw new AgentUiApiError(
      "The model-provider readiness facts were inconsistent.",
      "readiness_invalid",
      409,
    );
  }
  return payload;
}

function modelReadinessPresentation() {
  const modelId = configuredModelId();
  const status = state.modelReadiness?.status;
  const runtime = modelRuntimePresentation();
  const providerLabel = runtime?.providerLabel || "Model provider";
  const runtimeLabel = runtime?.runtimeLabel || "configured model runtime";
  if (status === "ready") {
    if (runtime?.remote) {
      return {
        badge: "Claude CLI ready · remote inference",
        notice: `${modelId} is available through the authenticated Claude CLI. Browser traffic stays on the loopback UI server, while prompts and bounded source context are sent to the remote provider under the approval recorded when the server started. Local Claude session persistence is disabled; this interface does not claim provider-side no-store or zero-data-retention. No Salesforce org, Mule runtime, deployment, publication, or source mutation is authorized.`,
      };
    }
    return {
      badge: "Ollama ready · local inference",
      notice: `${modelId} is installed in the reachable local Ollama runtime. Prompts stay inside the server-owned loopback model boundary; no Salesforce org, Mule runtime, deployment, publication, or source mutation is authorized.`,
    };
  }
  if (status === "provider_unreachable") {
    return {
      badge: `${providerLabel} configured · runtime unreachable`,
      notice: `${modelId} is configured, but the ${runtimeLabel} is not reachable. Correct the provider runtime or authentication, then reload this page. No prompt was sent.`,
    };
  }
  if (status === "model_unavailable") {
    return {
      badge: `${providerLabel} reachable · model unavailable`,
      notice: `The ${runtimeLabel} is reachable, but it could not verify ${modelId} as the configured model identity. Correct that server-owned identity, then reload this page. No prompt was sent.`,
    };
  }
  if (status === "runtime_unverified") {
    return {
      badge: `${providerLabel} reachable · identity unverified`,
      notice: `The ${runtimeLabel} responded, but it could not safely verify the runtime identity for ${modelId}. No prompt was sent; correct the runtime state and reload this page.`,
    };
  }
  if (status === "readiness_unavailable") {
    return {
      badge: `${providerLabel} configured · readiness unavailable`,
      notice: `${modelId} is configured, but the server could not complete its bounded provider readiness check. No prompt was sent.`,
    };
  }
  return {
    badge: `${providerLabel} configured · checking readiness`,
    notice: `${modelId} is the server-owned model. Checking the configured provider runtime before enabling model requests.`,
  };
}

function updateModelPresentation(boundaries = null, failure = null) {
  const providerAttempted = boundaries
    ? boundaries.provider_attempted ?? boundaries.provider_invoked === true
    : false;
  const structuredResponseAccepted = boundaries
    ? boundaries.structured_response_accepted ?? boundaries.provider_invoked === true
    : false;
  const modelId = boundaries?.model_id || configuredModelId();
  const runtime = modelRuntimePresentation(boundaries) || modelRuntimePresentation();
  const inferenceLabel = runtime?.inferenceLabel || "Configured model";
  const readinessPresentation = modelReadinessPresentation();

  elements.modeBadge.textContent = boundaries
    ? structuredResponseAccepted
      ? `${inferenceLabel} inference`
      : providerAttempted
        ? failure?.response_received === true
          ? `${inferenceLabel} · output rejected`
          : `${inferenceLabel} · call stopped`
        : `${inferenceLabel} · not invoked`
    : readinessPresentation.badge;
  elements.runtimePulse.classList.toggle(
    "is-verified",
    boundaries ? structuredResponseAccepted : modelRuntimeReady(),
  );
  elements.runtimePulse.classList.toggle(
    "is-stopped",
    boundaries
      ? Boolean(providerAttempted && !structuredResponseAccepted)
      : Boolean(state.modelReadiness && !modelRuntimeReady()),
  );
  elements.modelBadge.textContent = modelId;
  const runtimeIdentity = runtime?.remote
    ? boundaries?.runtime_identity_digest
    : boundaries?.model_revision;
  if (typeof runtimeIdentity === "string" && runtimeIdentity.startsWith("sha256:")) {
    const abbreviated = `${runtimeIdentity.slice(0, 19)}…`;
    const identityKind = runtime?.remote ? "runtime" : "rev";
    const accessibleKind = runtime?.remote ? "runtime identity" : "observed model revision";
    elements.modelRevisionBadge.textContent = `${identityKind} ${abbreviated}`;
    elements.modelRevisionBadge.title = runtimeIdentity;
    elements.modelRevisionBadge.setAttribute(
      "aria-label",
      `Full ${accessibleKind} ${runtimeIdentity}`,
    );
    elements.modelRevisionBadge.hidden = false;
  } else {
    elements.modelRevisionBadge.textContent = "Runtime identity unavailable";
    elements.modelRevisionBadge.removeAttribute("title");
    elements.modelRevisionBadge.removeAttribute("aria-label");
    elements.modelRevisionBadge.hidden = true;
  }
  elements.boundaryTitle.textContent = runtime?.boundaryTitle || "Model execution boundary";

  if (boundaries?.notice) {
    elements.boundaryNotice.textContent = boundaries.notice;
  } else {
    elements.boundaryNotice.textContent = readinessPresentation.notice;
  }

  elements.requestInput.placeholder = "Message the Architect about your migration";
  elements.requestInput.setAttribute("aria-label", "Message for the configured Architect model");
}

function updateRequestHelp() {
  if (!state.model) {
    elements.requestHelp.textContent = "The server-owned model configuration is unavailable.";
    return;
  }
  if (state.run?.status === "awaiting_approval") {
    elements.requestHelp.textContent =
      "Use Approve or Reject for this manifest, or choose New chat to leave it pending and start a separate conversation.";
    return;
  }
  if (state.run) {
    elements.requestHelp.textContent =
      "This saved run is read-only here. Choose New chat to start a separate conversation without changing it.";
    return;
  }
  if (!modelRuntimeReady()) {
    const status = state.modelReadiness?.status;
    elements.requestHelp.textContent =
      status === "provider_unreachable"
        ? `${configuredProviderLabel()} is not reachable. Correct the provider runtime or authentication and reload before sending.`
        : status === "model_unavailable"
          ? `${configuredProviderLabel()} is reachable, but ${configuredModelId()} is not available as the configured model.`
          : status === "runtime_unverified"
            ? `${configuredProviderLabel()} is reachable, but the configured runtime identity could not be verified safely.`
            : "Waiting for the server-owned model-provider readiness check.";
    return;
  }
  if (state.conversation?.status === "launch_pending") {
    elements.requestHelp.textContent =
      "This reviewed request is frozen by an interrupted launch. Retry Start migration or choose New chat.";
    return;
  }
  if (state.conversation?.status === "launched") {
    elements.requestHelp.textContent =
      "This conversation already launched a saved run. Choose New chat to begin another request.";
    return;
  }
  const readiness = conversationReadiness(state.conversation);
  if (state.conversationSyncUncertain) {
    elements.requestHelp.textContent =
      "The latest conversation state could not be confirmed. Send another message or reload before starting migration.";
    return;
  }
  if (state.platformSelectionDirty) {
    elements.requestHelp.textContent =
      "Send your next message to apply this target to the conversation before migration can start.";
    return;
  }
  if (readiness.ready) {
    elements.requestHelp.textContent =
      "The selected scenario is ready. Review the Controller question, or keep chatting for advisory guidance.";
    return;
  }
  if (!state.selectedPlatform) {
    elements.requestHelp.textContent =
      "Send any message. Select Salesforce or MuleSoft before launch so the controller, not the model, owns the target.";
    return;
  }
  elements.requestHelp.textContent =
    "Chat normally about the selected target. Enter sends; Shift+Enter adds a line.";
}

function updateComposerState() {
  const runDisplayed = Boolean(state.run);
  const conversationStatus = state.conversation?.status || "open";
  const launchPending = conversationStatus === "launch_pending";
  const conversationLaunched = conversationStatus === "launched";
  const conversationFrozen = launchPending || conversationLaunched;
  const prompt = elements.requestInput.value.trim();
  const validPrompt = prompt.length >= 1 && prompt.length <= 2000;
  const readiness = conversationReadiness(state.conversation);
  const readyToLaunch =
    readiness.ready &&
    readiness.launchToken !== null &&
    readiness.scenarioId === state.selectedScenarioId &&
    modelRuntimeReady() &&
    !state.platformSelectionDirty &&
    !state.conversationSyncUncertain;
  elements.requestInput.disabled = state.busy || runDisplayed || conversationFrozen;
  elements.sendButton.disabled =
    state.busy ||
    runDisplayed ||
    conversationFrozen ||
    !state.model ||
    !modelRuntimeReady() ||
    !validPrompt;
  elements.sendLabel.textContent = state.busy ? "Working…" : "Send";
  const launchStart = elements.conversation.querySelector('[data-launch-action="start"]');
  if (launchStart instanceof HTMLButtonElement) {
    launchStart.disabled =
      state.busy || runDisplayed || conversationLaunched || !readyToLaunch;
  }
  const keepRefining = elements.conversation.querySelector('[data-launch-action="refine"]');
  if (keepRefining instanceof HTMLButtonElement) {
    keepRefining.disabled = state.busy || runDisplayed || conversationFrozen;
  }
  elements.newChatButton.hidden = false;
  elements.newChatButton.disabled = state.busy;
  elements.focusRequestButton.disabled = state.busy;
  elements.newChatButton.title = runDisplayed
    ? "Start a new chat. The displayed run remains saved and unchanged."
    : "Start a fresh conversation.";
  elements.scenarioList.querySelectorAll("button").forEach((button) => {
    button.disabled = state.busy || runDisplayed || conversationFrozen;
    button.setAttribute("aria-pressed", String(button.dataset.platform === state.selectedPlatform));
  });
  updateRequestHelp();
}

function renderHarnessStages(stages) {
  const byKey = new Map((stages || []).map((item) => [item.key, item]));
  elements.harnessStages.querySelectorAll(".harness-stage").forEach((item, index) => {
    const current = byKey.get(item.dataset.stage);
    if (!current) {
      return;
    }
    item.className = `harness-stage is-${current.state}`;
    const marker = item.querySelector(".stage-marker");
    const label = item.querySelector("strong");
    const detail = item.querySelector("div span");
    const status = item.querySelector("em");
    marker.textContent = String(index + 1).padStart(2, "0");
    label.textContent = current.label;
    detail.textContent = current.detail;
    detail.title = current.detail;
    status.textContent = liveStageStatus(current.state);
  });
}

function liveStageStatus(stateName) {
  // The active stage carries a live elapsed clock so the harness panel visibly
  // advances every second in every busy phase (Architect, Engineer, Validator),
  // even before any progress artifact exists to poll.
  if (stateName === "active" && state.busyStartedAt !== null) {
    return `${humanize(stateName)} · ${formatElapsed(Date.now() - state.busyStartedAt)}`;
  }
  return humanize(stateName);
}

function renderWorkingHarness(role) {
  if (role === "Architect intake") {
    renderHarnessStages([
      {
        key: "architect",
        label: "Architect intake",
        state: "active",
        detail: "Waiting for one typed public model reply; private reasoning is not requested.",
      },
      {
        key: "approval",
        label: "Scenario launch gate",
        state: "waiting",
        detail: "No migration run has been started.",
      },
      {
        key: "engineer",
        label: "Engineer",
        state: "waiting",
        detail: "Cannot run during conversational intake.",
      },
      {
        key: "validator",
        label: "Validator",
        state: "waiting",
        detail: "Cannot run before an isolated candidate exists.",
      },
    ]);
    return;
  }
  if (role === "Architect") {
    renderHarnessStages([
      {
        key: "architect",
        label: "Controller → Architect",
        state: "active",
        detail: "Controller binds the exact source inputs, builds the dependency graph, and retrieves curated Wiki evidence; Architect then selects bounded evidence IDs and returns a typed semantic recommendation.",
      },
      { key: "approval", label: "Manifest approval", state: "pending", detail: "Controller expands an accepted semantic recommendation into the exact digest-bound manifest before human review." },
      { key: "engineer", label: "Engineer", state: "pending", detail: "Cannot propose isolated file content before human approval." },
      { key: "validator", label: "Controller checks + Validator", state: "pending", detail: "Waits for a candidate, then Controller checks run before the optional Validator advisory." },
    ]);
    return;
  }
  if (role === "Engineer → Validator") {
    renderHarnessStages([
      { key: "architect", label: "Controller → Architect", state: "complete", detail: "Architect returned the semantic recommendation; Controller expanded it into the exact manifest." },
      { key: "approval", label: "Approval gate", state: "complete", detail: "Exact persisted manifest approved." },
      {
        key: "engineer",
        label: "Engineer + Controller checks",
        state: "active",
        detail: "Engineer proposes isolated file content; Controller then runs deterministic checks. Verified sub-stage results appear when the synchronous request returns.",
      },
      {
        key: "validator",
        label: "Validator advisory",
        state: "pending",
        detail: "May review the Controller report within the aggregate request; it cannot change the deterministic disposition.",
      },
    ]);
    return;
  }
  if (role === "Engineer correction → Validator") {
    renderHarnessStages([
      { key: "architect", label: "Controller → Architect", state: "complete", detail: "The original Controller-expanded manifest remains unchanged." },
      { key: "approval", label: "Correction gate", state: "complete", detail: "The reviewer authorized the exact offered attempt 2." },
      {
        key: "engineer",
        label: "Engineer · attempt 2",
        state: "active",
        detail: "Proposing changed-file content for the typed failed-check and diagnostic signals within the unchanged manifest.",
      },
      {
        key: "validator",
        label: "Controller checks + Validator · attempt 2",
        state: "pending",
        detail: "Controller checks run first; Validator may then provide a non-authoritative advisory.",
      },
    ]);
    return;
  }
  if (state.run?.stages) {
    renderHarnessStages(state.run.stages);
  }
}

function renderLiveProgressHarness(payload, attempt2) {
  if (!payload || typeof payload.phase !== "string") {
    return;
  }
  const phase = payload.phase;
  if (phase !== "engineer" && phase !== "validator" && phase !== "complete") {
    // "pending" (Engineer not dispatched yet) or "unknown" (transient read
    // error): keep the static busy harness that setBusy already rendered.
    return;
  }
  const elapsed =
    typeof payload.elapsed_seconds === "number" && Number.isFinite(payload.elapsed_seconds)
      ? payload.elapsed_seconds
      : null;
  const clock = elapsed === null ? "" : ` · ${formatElapsed(elapsed * 1000)} in this phase`;
  const attemptSuffix = attempt2 ? " · attempt 2" : "";
  const engineerState = phase === "engineer" ? "active" : "complete";
  let validatorState = "pending";
  if (phase === "validator") {
    validatorState = "active";
  } else if (phase === "complete") {
    validatorState = "complete";
  }
  const engineerDetail =
    phase === "engineer"
      ? `Engineer is proposing isolated file content${clock}. Verified sub-stage results appear when the request returns.`
      : "Engineer returned proposed file content; Controller deterministic checks are running.";
  let validatorDetail;
  if (phase === "validator") {
    validatorDetail = `Controller deterministic checks and the optional Validator advisory are running${clock}.`;
  } else if (phase === "complete") {
    validatorDetail =
      "Controller checks and the Validator advisory finished; finalizing the verified report.";
  } else {
    validatorDetail =
      "Waits for the Engineer candidate before Controller checks and the Validator advisory run.";
  }
  renderHarnessStages([
    {
      key: "architect",
      label: "Controller → Architect",
      state: "complete",
      detail: attempt2
        ? "The original Controller-expanded manifest remains unchanged."
        : "Architect returned the semantic recommendation; Controller expanded it into the exact manifest.",
    },
    {
      key: "approval",
      label: attempt2 ? "Correction gate" : "Approval gate",
      state: "complete",
      detail: attempt2
        ? "The reviewer authorized the exact offered attempt 2."
        : "Exact persisted manifest approved.",
    },
    {
      key: "engineer",
      label: `Engineer${attemptSuffix}`,
      state: engineerState,
      detail: engineerDetail,
    },
    {
      key: "validator",
      label: `Controller checks + Validator${attemptSuffix}`,
      state: validatorState,
      detail: validatorDetail,
    },
  ]);
}

async function pollProgress(handle, attempt2) {
  if (!state.busy || state.run?.handle !== handle) {
    return;
  }
  let payload = null;
  try {
    const response = await api(`/api/sessions/${handle}/progress`);
    payload = await response.json();
  } catch (_error) {
    // A single poll failure is not fatal; keep the last rendered harness and
    // let the next tick — or the authoritative response — recover.
    return;
  }
  if (!state.busy || state.run?.handle !== handle) {
    return;
  }
  renderLiveProgressHarness(payload, attempt2);
}

function startProgressPoll(role) {
  stopProgressPoll();
  const handle = state.run?.handle;
  if (!validRunHandle(handle)) {
    return;
  }
  const attempt2 = role === "Engineer correction → Validator";
  state.progressRole = role;
  state.progressTimer = window.setInterval(() => {
    void pollProgress(handle, attempt2);
  }, PROGRESS_POLL_INTERVAL_MS);
  void pollProgress(handle, attempt2);
}

function stopProgressPoll() {
  if (state.progressTimer !== null) {
    window.clearInterval(state.progressTimer);
    state.progressTimer = null;
  }
  state.progressRole = null;
}

function setBusy(busy, role = "Architect", copy = "Reviewing the migration request") {
  const wasBusy = state.busy;
  state.busy = busy;
  elements.sendLabel.textContent = busy ? "Working…" : "Send";
  elements.downloadButton.disabled = busy;
  elements.exportButton.disabled =
    busy ||
    Boolean(
      state.exportResult &&
        state.run?.candidate &&
        state.exportResult.handle === state.run.handle &&
        state.exportResult.attempt === state.run.candidate.attempt,
    );
  if (elements.decisionForm) {
    elements.decisionForm.querySelectorAll("button, input, textarea").forEach((control) => {
      control.disabled = busy;
    });
  }
  elements.typingRole.textContent = role;
  elements.typingCopy.textContent = copy;
  const modelOperation =
    role === "Architect intake" ||
    role === "Architect" ||
    role === "Engineer → Validator" ||
    role === "Engineer correction → Validator";
  elements.activityDisclosure.textContent = modelOperation
    ? "Waiting for typed model output · private reasoning is not displayed"
    : "Controller-owned non-model operation · no model response is being claimed";
  elements.typingIndicator.hidden = !busy;
  if (busy && !wasBusy) {
    startRunTimer();
  } else if (!busy && wasBusy) {
    stopRunTimer();
  }
  if (busy) {
    renderWorkingHarness(role);
  } else if (state.run?.stages) {
    renderHarnessStages(state.run.stages);
  }
  if (busy && LIVE_PROGRESS_ROLES.has(role)) {
    // Advance the harness in real time from the run store while the gated
    // continuation holds the per-run lock for the full multi-minute execution.
    startProgressPoll(role);
  } else {
    stopProgressPoll();
  }
  updateComposerState();
  if (busy) {
    elements.typingIndicator.scrollIntoView({ behavior: scrollBehavior(), block: "nearest" });
  }
}

function renderScenarios() {
  clear(elements.scenarioList);
  state.scenarios.forEach((scenario) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "suggestion-button";
    button.dataset.platform = scenario.platform;
    button.dataset.scenarioId = scenario.scenario_id;
    button.dataset.shortLabel = scenario.platform === "salesforce" ? "SF" : "M4";
    button.setAttribute("aria-pressed", String(scenario.scenario_id === state.selectedScenarioId));
    button.textContent = scenario.title;
    button.title = `${scenario.source} → ${scenario.target}`;
    button.addEventListener("click", () => {
      if (state.busy) {
        return;
      }
      const currentText = elements.requestInput.value.trim();
      const currentTextIsExample = state.scenarios.some(
        (candidate) => candidate.canonical_request === currentText,
      );
      state.selectedPlatform = scenario.platform;
      state.selectedScenarioId = scenario.scenario_id;
      state.platformSelectionDirty =
        Boolean(state.conversation) &&
        (state.conversation.selected_platform !== scenario.platform ||
          state.conversation.selected_scenario_id !== scenario.scenario_id);
      if (!currentText || currentTextIsExample) {
        elements.requestInput.value = scenario.canonical_request;
      }
      updateComposerState();
      elements.requestInput.focus();
      clearAlert();
    });
    elements.scenarioList.append(button);
  });
  elements.scenarioList.setAttribute("aria-busy", "false");
  updateComposerState();
}

function stage(run, key) {
  return run.stages.find((item) => item.key === key) || null;
}

function messageArticle(role, paragraphs, options = {}) {
  const article = document.createElement("article");
  const user = role === "You";
  const controller = role === "Controller";
  article.className = `chat-message ${user ? "user-message" : "assistant-message"}${controller ? " controller-message" : ""}`;

  const avatar = textElement(
    "div",
    `avatar ${role.toLowerCase()}-avatar`,
    roleAvatars[role] || "A",
  );
  avatar.setAttribute("aria-hidden", "true");
  const stack = document.createElement("div");
  stack.className = "message-stack";
  const label = textElement("span", "message-role", role);
  const descriptor = roleDescriptions[role];
  if (descriptor) {
    label.append(textElement("em", "", descriptor));
  }
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  paragraphs.forEach((paragraph) => bubble.append(textElement("p", "", paragraph)));
  if (options.meta) {
    bubble.append(textElement("p", "message-meta", options.meta));
  }
  stack.append(label, bubble);
  article.append(avatar, stack);
  return article;
}

function architectMessage(run) {
  const architect = stage(run, "architect");
  const pathCount = run.manifest ? run.manifest.approved_paths.length : 0;
  if (!architect || architect.state !== "complete") {
    const providerAttempted =
      run.boundaries.provider_attempted ?? run.boundaries.provider_invoked === true;
    return messageArticle(
      providerAttempted ? "Architect" : "Controller",
      [architect?.detail || "The architecture step did not complete."],
      {
        meta: providerAttempted
          ? `Configured provider attempt: ${run.boundaries.model_id} · no accepted role output is claimed`
          : "Controller preflight · model provider not invoked",
      },
    );
  }
  const decisions = (run.manifest?.public_decisions || []).map(
    (decision) => `Public decision: ${decision}`,
  );
  const unresolved = (run.manifest?.unresolved_questions || []).map(
    (question) => `Unresolved question: ${question}`,
  );
  const graphSelectionCount = run.manifest?.cited_graph_nodes?.length || 0;
  const wikiSelectionCount = run.manifest?.cited_wiki_pages?.length || 0;
  const message = messageArticle(
    "Architect",
    [
      `I returned an evidence-bound semantic recommendation using ${graphSelectionCount} selected graph ${graphSelectionCount === 1 ? "ID" : "IDs"} and ${wikiSelectionCount} selected Wiki ${wikiSelectionCount === 1 ? "page ID" : "page IDs"}.`,
      `The Controller supplied exact digest-bound source evidence, built the dependency graph, and retrieved ${run.metrics.wiki_hits} relevant Wiki ${run.metrics.wiki_hits === 1 ? "entry" : "entries"} before my response. It then expanded the accepted recommendation into an exact ${pathCount}-path manifest; paths, checks, implementation-contract text, approval actions, and manifest identity are Controller-owned.`,
      ...decisions,
      ...unresolved,
      run.planning_decision
        ? "The plan requires a separate human planning decision; the approve/reject manifest gate did not open."
        : "The controller has paused the workflow for your decision. Approval creates an isolated candidate only; it does not deploy anything.",
    ],
    {
      meta: `${modelRuntimePresentation(run.boundaries)?.providerLabel || "Configured provider"} model: ${run.boundaries.model_id} · public structured decisions; private reasoning is not shown`,
    },
  );
  return message;
}

function reviewList(title, values, emptyText) {
  const section = document.createElement("section");
  section.className = "manifest-review-section";
  section.append(textElement("h4", "", title));
  const list = document.createElement("ul");
  list.className = "manifest-review-list";
  if (!values || values.length === 0) {
    list.append(textElement("li", "muted", emptyText));
  } else {
    values.forEach((value) => list.append(textElement("li", "", value)));
  }
  section.append(list);
  return section;
}

function manifestReview(manifest) {
  const review = document.createElement("div");
  review.className = "manifest-review";
  review.setAttribute("aria-label", "Digest-bound manifest details");
  const digest = textElement("code", "manifest-digest", manifest.manifest_digest);
  digest.title = manifest.manifest_digest;
  review.append(
    textElement("h3", "manifest-review-title", "Digest-bound plan under review"),
    textElement("p", "manifest-review-status", `Manifest status: ${manifest.status.replaceAll("_", " ")}`),
    digest,
    reviewList("Public Architect decisions", manifest.public_decisions, "No public decisions are available."),
    reviewList(
      "Controller-owned implementation contract",
      manifest.implementation_contract,
      "No implementation acceptance contract is available.",
    ),
  );

  const transformations = document.createElement("section");
  transformations.className = "manifest-review-section";
  transformations.append(textElement("h4", "", "Transformations"));
  manifest.transformations.forEach((transformation) => {
    const item = document.createElement("div");
    item.className = "manifest-review-item";
    item.append(
      textElement("strong", "", transformation.step_id),
      textElement("p", "", transformation.description),
      textElement("p", "manifest-review-paths", `Inputs: ${transformation.input_paths.join(", ")}`),
      textElement("p", "manifest-review-paths", `Outputs: ${transformation.output_paths.join(", ")}`),
    );
    transformations.append(item);
  });
  review.append(transformations);

  const validations = document.createElement("section");
  validations.className = "manifest-review-section";
  validations.append(textElement("h4", "", "Validation commands"));
  manifest.validation_commands.forEach((command) => {
    const requirement = command.required ? "required" : "optional";
    const item = document.createElement("div");
    item.className = "manifest-review-item";
    item.append(
      textElement("strong", "", `${command.check_id} · ${command.command_id}`),
      textElement("p", "", command.purpose),
      textElement("p", "message-meta", `${command.environment} · ${requirement}`),
    );
    validations.append(item);
  });
  review.append(validations);

  const riskLines = manifest.risks.map(
    (risk) => `${risk.category.replaceAll("_", " ")}: ${risk.summary}${risk.requires_human_decision ? " (human decision required)" : ""}`,
  );
  review.append(
    reviewList("Risks", riskLines, "No manifest risks were reported."),
    reviewList(
      "Required approvals",
      manifest.required_approvals.map((approval) => approval.replaceAll("_", " ")),
      "No additional approval actions were declared.",
    ),
  );
  const graphCitations = Array.isArray(manifest.cited_graph_nodes)
    ? manifest.cited_graph_nodes.map((citation) => `Selected graph ID: ${citation}`)
    : [];
  const wikiCitations = Array.isArray(manifest.cited_wiki_pages)
    ? manifest.cited_wiki_pages.map((citation) => `Selected Wiki page ID: ${citation}`)
    : [];
  if (graphCitations.length > 0 || wikiCitations.length > 0) {
    review.append(
      reviewList(
        "Architect-selected evidence IDs",
        [...graphCitations, ...wikiCitations],
        "No evidence citations are available.",
      ),
    );
  }
  return review;
}

function keepRefiningConversation() {
  if (state.busy || state.run || state.conversation?.status === "launch_pending") {
    return;
  }
  elements.requestInput.focus();
  elements.requestInput.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
}

function conversationLaunchGate(conversation) {
  const readiness = conversationReadiness(conversation);
  const scenario = scenarioById(readiness.scenarioId);
  const launchPending = conversation.status === "launch_pending";
  if (
    !readiness.ready ||
    readiness.launchToken === null ||
    typeof readiness.canonicalRequest !== "string" ||
    readiness.canonicalRequest.trim() === "" ||
    typeof readiness.launchContractDigest !== "string" ||
    !SHA256_PATTERN.test(readiness.launchContractDigest) ||
    !["salesforce", "mulesoft"].includes(readiness.platform) ||
    !scenario ||
    scenario.platform !== readiness.platform
  ) {
    return null;
  }

  const article = document.createElement("article");
  article.className = "gate-card launch-gate";
  article.setAttribute("aria-labelledby", "launch-gate-title");
  const heading = document.createElement("div");
  heading.className = "gate-heading";
  heading.append(
    textElement("span", "gate-icon", "?"),
    textElement("span", "gate-owner", "Controller · explicit launch gate"),
    textElement(
      "h3",
      "",
      launchPending ? "Retry this exact migration launch?" : "Start this migration?",
    ),
  );
  heading.querySelector("h3").id = "launch-gate-title";

  const request = textElement("p", "launch-gate-request", readiness.canonicalRequest);
  request.setAttribute("aria-label", "Controller-owned canonical migration request");
  article.append(
    heading,
    textElement(
      "p",
      "gate-description",
      launchPending
        ? "The exact token-bound launch was interrupted. Retry resumes that same launch; it does not create a different request."
        : "Review the controller-owned canonical request below. Chat and Architect prose are advisory and cannot change this immutable launch contract.",
    ),
    textElement("span", "launch-gate-platform", scenario.title),
    (() => {
      const boundary = document.createElement("dl");
      boundary.className = "scenario-boundary";
      boundary.append(
        textElement("dt", "", "Source"),
        textElement("dd", "", scenario.source),
        textElement("dt", "", "Target"),
        textElement("dd", "", scenario.target),
      );
      return boundary;
    })(),
    request,
    textElement(
      "p",
      "launch-gate-advisory",
      readiness.advisorySummary
        ? `Architect advisory: ${readiness.advisorySummary}`
        : "Architect advisory summary unavailable; the canonical request remains authoritative.",
    ),
  );

  const form = document.createElement("form");
  form.id = "launch-form";
  form.className = "decision-form";
  const actions = document.createElement("div");
  actions.className = "decision-actions";
  const start = textElement(
    "button",
    "button button-primary",
    launchPending ? "Retry Start migration" : "Start migration",
  );
  start.type = "button";
  start.dataset.launchAction = "start";
  start.addEventListener("click", launchMigration);
  actions.append(start);
  if (!launchPending) {
    const refine = textElement("button", "button button-secondary", "Keep chatting");
    refine.type = "button";
    refine.dataset.launchAction = "refine";
    refine.addEventListener("click", keepRefiningConversation);
    actions.append(refine);
  }
  form.append(actions);
  form.addEventListener("submit", (event) => event.preventDefault());
  article.append(form);
  elements.decisionForm = form;
  return article;
}

function approvalGate(run) {
  const article = document.createElement("article");
  article.className = "gate-card";
  article.setAttribute("aria-labelledby", "approval-title");
  const heading = document.createElement("div");
  heading.className = "gate-heading";
  heading.append(
    textElement("span", "gate-icon", "!"),
    textElement("span", "gate-owner", "Controller · human approval gate"),
    textElement("h3", "", "Review the bounded manifest"),
  );
  heading.querySelector("h3").id = "approval-title";
  article.append(
    heading,
    textElement(
      "p",
      "gate-description",
      `The Controller expanded the Architect's semantic recommendation into this exact ${run.manifest.approved_paths.length}-path manifest. Approval authorizes an isolated candidate, never an org, runtime, publication, or deployment action.`,
    ),
    manifestReview(run.manifest),
  );

  const form = document.createElement("form");
  form.id = "decision-form";
  form.className = "decision-form";
  const reviewerLabel = textElement("label", "", "Human reviewer ID");
  reviewerLabel.htmlFor = "reviewer";
  const reviewer = document.createElement("input");
  reviewer.id = "reviewer";
  reviewer.name = "reviewer";
  reviewer.value = "capstone-author";
  reviewer.maxLength = 160;
  reviewer.autocomplete = "off";
  reviewer.setAttribute("aria-describedby", "reviewer-help");
  reviewer.required = true;
  const reviewerHelp = textElement(
    "p",
    "decision-help",
    "This is your human audit identity in the local approval record, not another agent.",
  );
  reviewerHelp.id = "reviewer-help";
  const commentLabel = textElement("label", "", "Comment (optional)");
  commentLabel.htmlFor = "decision-comment";
  const comment = document.createElement("textarea");
  comment.id = "decision-comment";
  comment.name = "comment";
  comment.rows = 2;
  comment.maxLength = 500;
  comment.placeholder = "Add a short decision note";
  const actions = document.createElement("div");
  actions.className = "decision-actions";
  const approve = textElement("button", "button button-primary", "Approve & create candidate");
  approve.type = "button";
  approve.value = "approve";
  const reject = textElement("button", "button button-secondary", "Reject");
  reject.type = "button";
  reject.value = "reject";
  approve.addEventListener("click", () => submitDecision("approve"));
  reject.addEventListener("click", () => submitDecision("reject"));
  actions.append(approve, reject);
  form.append(reviewerLabel, reviewer, reviewerHelp, commentLabel, comment, actions);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    showAlert("Choose Approve or Reject to record an explicit human decision.");
  });
  article.append(form);
  elements.decisionForm = form;
  return article;
}

function correctionGate(run) {
  const correction = run.correction;
  const resuming = Boolean(correction.approval);
  const article = document.createElement("article");
  article.className = "gate-card";
  article.setAttribute("aria-labelledby", "correction-title");
  const heading = document.createElement("div");
  heading.className = "gate-heading";
  heading.append(
    textElement("span", "gate-icon", "2"),
    textElement("span", "gate-owner", "Controller · bounded correction gate"),
    textElement(
      "h3",
      "",
      resuming ? "Resume the authorized corrective attempt" : "Review one corrective attempt",
    ),
  );
  heading.querySelector("h3").id = "correction-title";
  article.append(
    heading,
    textElement(
      "p",
      "gate-description",
      resuming
        ? "The attempt-2 approval was durably recorded before execution was interrupted. Resubmitting these exact reviewer fields resumes that same authorization; it does not create another attempt."
        : "Attempt 1 completed with a recoverable validation failure. Approval authorizes only attempt 2 against the same manifest, base revision, and source snapshot.",
    ),
    reviewList(
      "Root correction signals",
      correction.failed_check_ids,
      "No root correction signals were offered.",
    ),
  );
  const attemptOne = (run.attempt_history || []).find((item) => item.attempt === 1);
  if (attemptOne?.diagnostic_ids?.length) {
    article.append(
      reviewList(
        "Typed diagnostics",
        attemptOne.diagnostic_ids,
        "No diagnostic identifiers were recorded.",
      ),
    );
  }

  const form = document.createElement("form");
  form.id = "correction-form";
  form.className = "decision-form";
  const reviewerLabel = textElement("label", "", "Human reviewer ID");
  reviewerLabel.htmlFor = "correction-reviewer";
  const reviewer = document.createElement("input");
  reviewer.id = "correction-reviewer";
  reviewer.name = "reviewer";
  reviewer.value = correction.approval?.reviewer || "capstone-author";
  reviewer.maxLength = 160;
  reviewer.autocomplete = "off";
  reviewer.setAttribute("aria-describedby", "correction-reviewer-help");
  reviewer.required = true;
  const reviewerHelp = textElement(
    "p",
    "decision-help",
    "This is your human audit identity in the local approval record, not another agent.",
  );
  reviewerHelp.id = "correction-reviewer-help";
  const commentLabel = textElement("label", "", "Correction approval note (optional)");
  commentLabel.htmlFor = "correction-comment";
  const comment = document.createElement("textarea");
  comment.id = "correction-comment";
  comment.name = "comment";
  comment.rows = 2;
  comment.maxLength = 500;
  comment.placeholder = "Authorize the exact offered correction";
  comment.value = correction.approval?.comment || "";
  const actions = document.createElement("div");
  actions.className = "decision-actions";
  const approve = textElement(
    "button",
    "button button-primary",
    resuming ? "Resume authorized attempt 2" : "Approve corrective attempt 2",
  );
  approve.type = "button";
  approve.addEventListener("click", submitRetry);
  actions.append(approve);
  form.append(reviewerLabel, reviewer, reviewerHelp, commentLabel, comment, actions);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitRetry();
  });
  article.append(form);
  elements.decisionForm = form;
  return article;
}

function finalReviewGate(run) {
  const review = run.final_review;
  if (!review?.eligible) {
    return null;
  }
  const article = document.createElement("article");
  article.className = "gate-card final-review-gate";
  article.setAttribute("aria-labelledby", "final-review-title");
  const heading = document.createElement("div");
  heading.className = "gate-heading";
  heading.append(
    textElement("span", "gate-icon", "✓"),
    textElement("span", "gate-owner", "Controller · independent human review"),
    textElement(
      "h3",
      "",
      review.status === "not_requested"
        ? "Request final human review"
        : review.status === "awaiting_final_review"
          ? "Record the designated reviewer's decision"
          : review.status === "expired"
            ? "Close the expired final review"
          : "Final human review recorded",
    ),
  );
  heading.querySelector("h3").id = "final-review-title";
  article.append(
    heading,
    textElement(
      "p",
      "gate-description",
      "This checkpoint is bound to the exact candidate and validation evidence. Human labels are declarative audit identities, not another agent or authenticated account. No choice authorizes Git, deployment, publication, or an external platform action.",
    ),
  );

  if (review.status === "not_requested" && review.can_request) {
    const form = document.createElement("form");
    form.id = "final-review-request-form";
    form.className = "decision-form";
    const requesterLabel = textElement("label", "", "Requesting human ID");
    requesterLabel.htmlFor = "final-review-requester";
    const requester = document.createElement("input");
    requester.id = "final-review-requester";
    requester.name = "requester";
    requester.value = "capstone-author";
    requester.maxLength = 160;
    requester.autocomplete = "off";
    requester.required = true;
    const reviewerLabel = textElement("label", "", "Independent human reviewer ID");
    reviewerLabel.htmlFor = "final-review-designated-reviewer";
    const reviewer = document.createElement("input");
    reviewer.id = "final-review-designated-reviewer";
    reviewer.name = "designated_reviewer";
    reviewer.value = "independent-reviewer";
    reviewer.maxLength = 160;
    reviewer.autocomplete = "off";
    reviewer.required = true;
    const help = textElement(
      "p",
      "decision-help",
      "Use two different human audit labels. This local app records them but does not authenticate either identity.",
    );
    const actions = document.createElement("div");
    actions.className = "decision-actions";
    const request = textElement("button", "button button-primary", "Request final review");
    request.type = "button";
    request.addEventListener("click", requestFinalReview);
    actions.append(request);
    form.append(requesterLabel, requester, reviewerLabel, reviewer, help, actions);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      requestFinalReview();
    });
    article.append(form);
    elements.decisionForm = form;
    return article;
  }

  const facts = document.createElement("dl");
  facts.className = "final-review-facts";
  facts.append(
    textElement("dt", "", "Requester"),
    textElement("dd", "", review.requester || "Unavailable"),
    textElement("dt", "", "Designated reviewer"),
    textElement("dd", "", review.designated_reviewer || "Unavailable"),
    textElement("dt", "", "Status"),
    textElement("dd", "", humanize(review.status)),
  );
  article.append(facts);

  if (["awaiting_final_review", "expired"].includes(review.status) && review.can_decide) {
    const form = document.createElement("form");
    form.id = "final-review-decision-form";
    form.className = "decision-form";
    const reviewerLabel = textElement("label", "", "Designated human reviewer ID");
    reviewerLabel.htmlFor = "final-review-reviewer";
    const reviewer = document.createElement("input");
    reviewer.id = "final-review-reviewer";
    reviewer.name = "reviewer";
    reviewer.value = review.designated_reviewer || "";
    reviewer.maxLength = 160;
    reviewer.readOnly = true;
    reviewer.required = true;
    const commentLabel = textElement("label", "", "Final review comment (optional)");
    commentLabel.htmlFor = "final-review-comment";
    const comment = document.createElement("textarea");
    comment.id = "final-review-comment";
    comment.name = "comment";
    comment.rows = 2;
    comment.maxLength = 2000;
    const actions = document.createElement("div");
    actions.className = "decision-actions";
    const decisions = review.status === "expired"
      ? [
          ["request_changes", "Close and request changes"],
          ["reject", "Close and reject candidate"],
        ]
      : [
          ["accept", "Accept candidate"],
          ["request_changes", "Request changes"],
          ["reject", "Reject candidate"],
        ];
    decisions.forEach(([selection, label], index) => {
      const button = textElement(
        "button",
        index === 0 ? "button button-primary" : "button button-secondary",
        label,
      );
      button.type = "button";
      button.addEventListener("click", () => submitFinalReviewDecision(selection));
      actions.append(button);
    });
    form.append(reviewerLabel, reviewer, commentLabel, comment, actions);
    form.addEventListener("submit", (event) => event.preventDefault());
    article.append(form);
    elements.decisionForm = form;
    return article;
  }

  article.append(
    messageArticle(
      "Controller",
      [
        review.status === "accepted"
          ? "The designated human accepted this candidate for a separate next manual action. No external action was authorized."
          : review.status === "changes_requested"
            ? "The designated human requested changes. A new bounded run and new final review are required."
            : "The designated human rejected this candidate. The request stops here.",
      ],
      {
        meta: `${review.reviewer || review.designated_reviewer || "reviewer unavailable"}${review.comment ? ` · ${review.comment}` : ""}`,
      },
    ),
  );
  return article;
}

function priorAttemptMessage(run) {
  const prior = (run.attempt_history || []).find((item) => item.attempt === 1);
  if (!prior || run.execution_attempt !== 2) {
    return null;
  }
  const failedChecks = prior.failed_check_ids.map((id) => `Failed check: ${id}`);
  const diagnostics = prior.diagnostic_ids.map((id) => `Diagnostic: ${id}`);
  return messageArticle(
    "Controller",
    [
      "Attempt 1 stopped at a recoverable validation boundary.",
      ...failedChecks,
      ...diagnostics,
    ],
    { meta: `Attempt 1 · ${humanize(prior.disposition)}` },
  );
}

function correctionDecisionMessage(run) {
  const approval = run.correction?.approval || state.retryApproval;
  if (!approval) {
    return null;
  }
  return messageArticle(
    "You",
    ["I authorize the exact bounded correction attempt."],
    {
      meta: `${approval.reviewer}${approval.comment ? ` · ${approval.comment}` : ""} · attempt 2 only`,
    },
  );
}

function decisionMessage(run) {
  const decision = run?.human_decision || state.decision;
  if (!decision) {
    return null;
  }
  const action =
    decision.selection === "approve"
      ? "I approve this bounded manifest."
      : "I reject this manifest.";
  return messageArticle("You", [action], {
    meta: `${decision.reviewer}${decision.comment ? ` · ${decision.comment}` : ""}`,
  });
}

function engineerMessage(run) {
  const engineer = stage(run, "engineer");
  const count = run.candidate.changed_paths.length;
  const assumptions = run.candidate.assumptions.map(
    (assumption) => `Public assumption: ${assumption}`,
  );
  return messageArticle(
    "Engineer",
    [
      engineer ? engineer.detail : "I created the migration candidate in an isolated workspace.",
      `The candidate contains ${count} changed ${count === 1 ? "file" : "files"}. Source fixtures remain unchanged, and the exact paths and unified diff are available in Run evidence.`,
      ...assumptions,
    ],
    { meta: `Implementation attempt ${run.execution_attempt}` },
  );
}

function controllerValidationMessage(run) {
  const passed = run.validation.results.filter((result) => result.status === "passed").length;
  const failedResults = run.validation.results.filter((result) => result.status === "failed");
  const rootFailures = failedResults.filter((result) => !result.dependent_on);
  const dependentChecks = failedResults.filter((result) => Boolean(result.dependent_on));
  const unavailable = run.validation.results.filter(
    (result) => result.status === "unavailable",
  ).length;
  return messageArticle(
    "Controller",
    [
      `I ran the controller-owned deterministic checks against attempt ${run.validation.attempt}. Their report alone controls the ${humanize(run.validation.disposition)} disposition.`,
      ...rootFailures.flatMap((result) => [
        `Failed check: ${result.check_id}. ${result.summary}`,
        ...(result.diagnostic_ids || []).map((id) => `Typed diagnostic: ${id}`),
      ]),
      ...(dependentChecks.length
        ? [
            `${dependentChecks.length} downstream Jest ${dependentChecks.length === 1 ? "check was" : "checks were"} classified as dependent because the root LWC load failure prevented all tests from running.`,
          ]
        : []),
    ],
    {
      meta: dependentChecks.length
        ? `Authoritative deterministic evidence · ${passed} passed · ${rootFailures.length} root ${rootFailures.length === 1 ? "failure" : "failures"} · ${dependentChecks.length} dependent ${dependentChecks.length === 1 ? "check" : "checks"} · ${unavailable} unavailable`
        : `Authoritative deterministic evidence · ${passed} passed, ${failedResults.length} failed, ${unavailable} unavailable`,
    },
  );
}

function validatorMessage(run) {
  const assessment = run.validation.advisory_assessment;
  const completed = run.validation.validator_completed !== false && typeof assessment === "string";
  return messageArticle(
    "Validator",
    [
      completed
        ? run.validation.validator_summary
        : "My advisory review was unavailable. The candidate and controller-owned deterministic disposition remain intact.",
      ...run.validation.concerns.map((concern) => `Advisory concern: ${concern}`),
    ],
    {
      meta: completed
        ? `Non-authoritative advisory: ${humanize(assessment)}`
        : "Non-authoritative advisory unavailable · no deterministic result was changed",
    },
  );
}

function completionMessage(run) {
  if (run.validation.disposition === "ready_for_human_review") {
    return messageArticle("Controller", [
      "Migration candidate passed every required local check and is ready for human review.",
      "Download the ZIP or save the exact candidate files to output/. Salesforce org validation, final acceptance, and deployment still require separate authorization.",
    ]);
  }
  if (run.execution_attempt === 2) {
    const terminalCorrection =
      run.correction?.completed_attempt === 2 ? run.correction : null;
    return messageArticle(
      "Controller",
      [
        terminalCorrection?.reason ||
          "Corrective attempt 2 stopped and requires human diagnosis.",
        "The isolated diff remains available for debugging. No third attempt, source mutation, external validation, or deployment was authorized.",
      ],
      {
        meta: `Attempt 2 · controller action: ${humanize(terminalCorrection?.action || "stop_exhausted")}`,
      },
    );
  }
  if (run.correction?.retry_available) {
    return messageArticle("Controller", [
      "Migration candidate needs correction before external validation or deployment. One bounded corrective attempt is available only through the reviewer gate below.",
      "The unvalidated diff, download, and status-labelled output export remain available for debugging; the source repository and external platforms were not changed.",
    ]);
  }
  const terminalCorrection =
    run.correction?.completed_attempt === 1 ? run.correction : null;
  return messageArticle(
    "Controller",
    [
      terminalCorrection?.reason ||
        "The current result does not authorize a same-manifest corrective attempt.",
      "No retry gate was opened. You may save the candidate to output/ with its non-ready validation receipt, then resolve the environment or planning decision before starting a new run.",
    ],
    {
      meta: `Attempt 1 · controller action: ${humanize(terminalCorrection?.action || run.validation.disposition)}`,
    },
  );
}

function rejectionMessage() {
  return messageArticle("Controller", [
    "The request was stopped at the human approval gate. Engineer and Validator were not invoked, and no migration candidate was created.",
  ]);
}

function interventionMessage(run) {
  const intervention = run.intervention;
  const concerns = intervention.public_concerns.map((concern) => `Concern: ${concern}`);
  return messageArticle(
    "Controller",
    [
      "The bounded workflow stopped and requires a human decision. Engineer and Validator completion is not claimed.",
      intervention.reason,
      ...concerns,
      `Recommendation: ${intervention.recommendation}`,
    ],
    {
      meta: `Requested action: ${intervention.requested_action.replaceAll("_", " ")} · ${intervention.affected_paths.length} affected path${intervention.affected_paths.length === 1 ? "" : "s"}`,
    },
  );
}

function planningDecisionMessage(run) {
  const decision = run.planning_decision;
  const questions = decision.unresolved_questions.map(
    (question) => `Unresolved question: ${question}`,
  );
  const approvals = decision.required_approvals.map(
    (approval) => `Required planning action: ${approval.replaceAll("_", " ")}`,
  );
  const article = messageArticle(
    "Controller",
    [decision.summary, ...questions, ...approvals],
    {
      meta: `Manifest ${decision.manifest_id} · approval gate not opened`,
    },
  );
  const bubble = article.querySelector(".message-bubble");
  if (bubble && run.manifest) {
    bubble.append(manifestReview(run.manifest));
  }
  return article;
}

function failureMessage(run) {
  const failure = run.failure;
  const architect = stage(run, "architect");
  const engineer = stage(run, "engineer");
  let outcome;
  if (engineer?.state === "complete") {
    outcome = run.validation && failure.seam === "validator"
      ? "Engineer completed an isolated candidate and deterministic checks completed, but the Validator advisory failed. The read-only diff remains available; candidate download is withheld."
      : "Engineer completed an isolated candidate, but deterministic validation did not complete. The read-only diff remains available; candidate download is withheld.";
  } else if (architect?.state === "complete") {
    outcome =
      "Architect completed, but Engineer did not complete a verified candidate and Validator completion is not claimed.";
  } else if (failure.schema_valid === true && failure.policy_valid === false) {
    outcome =
      "Architect returned a schema-valid structured response, but the controller rejected it at the policy boundary. Engineer and Validator were not invoked.";
  } else {
    outcome =
      "Architect did not produce an accepted structured response; Engineer and Validator were not invoked.";
  }
  return messageArticle(
    "Controller",
    [
      `The workflow ended in a controlled failure during ${failure.operation}.`,
      outcome,
      `Controller reason (${failure.reason_code}): ${failure.summary}`,
      failure.guidance || "Use New chat to discuss or select a supported scenario and start a new immutable run.",
    ],
    {
      meta: `Reason code: ${failure.reason_code} · category: ${failure.category.replaceAll("_", " ")} · seam: ${failure.seam.replaceAll("_", " ")} · ${failure.retry_eligible ? "retry eligible" : "not retry eligible"}`,
    },
  );
}

function renderConversation(run) {
  elements.decisionForm = null;
  elements.conversation.setAttribute("aria-live", "off");
  clear(elements.conversation);
  const intakeBelongsToRun =
    state.conversation?.launch_handle === run.handle &&
    conversationMessages(state.conversation).length > 0;
  if (intakeBelongsToRun) {
    conversationMessages(state.conversation).forEach((message) => {
      elements.conversation.append(
        messageArticle(conversationMessageRole(message), [conversationMessageContent(message)]),
      );
    });
  } else {
    elements.conversation.append(messageArticle("You", [run.prompt]));
  }
  const scenario = scenarioById(run.scenario_id);
  if (scenario) {
    elements.conversation.append(
      messageArticle(
        "Controller",
        [
          `Bound scenario: ${scenario.title}.`,
          `Source: ${scenario.source}. Target: ${scenario.target}`,
        ],
        { meta: `Scenario ${scenario.scenario_id} · immutable run boundary` },
      ),
    );
  }
  elements.conversation.append(architectMessage(run));
  if (run.status === "awaiting_approval") {
    elements.conversation.append(approvalGate(run));
  } else {
    const decision = decisionMessage(run);
    if (decision) {
      elements.conversation.append(decision);
    }
    if (run.planning_decision) {
      elements.conversation.append(planningDecisionMessage(run));
    } else if (run.intervention) {
      const prior = priorAttemptMessage(run);
      const retryDecision = correctionDecisionMessage(run);
      if (prior) {
        elements.conversation.append(prior);
      }
      if (retryDecision) {
        elements.conversation.append(retryDecision);
      }
      elements.conversation.append(interventionMessage(run));
    } else if (run.failure) {
      const prior = priorAttemptMessage(run);
      const retryDecision = correctionDecisionMessage(run);
      if (prior) {
        elements.conversation.append(prior);
      }
      if (retryDecision) {
        elements.conversation.append(retryDecision);
      }
      elements.conversation.append(failureMessage(run));
    } else if (
      run.status === "implementing" &&
      run.execution_attempt === 2 &&
      run.correction?.retry_available &&
      run.correction?.approval &&
      run.candidate &&
      run.validation
    ) {
      const prior = priorAttemptMessage(run);
      const retryDecision = correctionDecisionMessage(run);
      if (prior) {
        elements.conversation.append(prior);
      }
      if (retryDecision) {
        elements.conversation.append(retryDecision);
      }
      elements.conversation.append(
        messageArticle(
          "Controller",
          [
            "The exact attempt-2 approval was recorded, but execution stopped before the Engineer provider call began.",
            "Resubmit the unchanged reviewer fields below to resume that same bounded attempt. No new attempt or additional authority will be created.",
          ],
          { meta: "Attempt 2 · authorized Engineer recovery pending" },
        ),
        correctionGate(run),
      );
    } else if (run.status === "completed" && run.candidate && run.validation) {
      const prior = priorAttemptMessage(run);
      const retryDecision = correctionDecisionMessage(run);
      if (prior) {
        elements.conversation.append(prior);
      }
      if (retryDecision) {
        elements.conversation.append(retryDecision);
      }
      elements.conversation.append(
        engineerMessage(run),
        controllerValidationMessage(run),
        validatorMessage(run),
        completionMessage(run),
      );
      const finalReview = finalReviewGate(run);
      if (finalReview) {
        elements.conversation.append(finalReview);
      }
      if (run.correction?.retry_available) {
        elements.conversation.append(correctionGate(run));
      }
    } else if (run.status === "rejected") {
      elements.conversation.append(rejectionMessage());
    }
  }
  requestAnimationFrame(() => elements.conversation.setAttribute("aria-live", "polite"));
}

function renderPendingMessage(message) {
  elements.decisionForm = null;
  elements.conversation.append(messageArticle("You", [message]));
  elements.conversation.lastElementChild?.scrollIntoView({
    behavior: scrollBehavior(),
    block: "nearest",
  });
}

function renderEmptyConversation() {
  elements.decisionForm = null;
  elements.conversation.setAttribute("aria-live", "off");
  clear(elements.conversation);
  elements.conversation.append(
    messageArticle("Architect", [
      "Tell me what you want to modernize. The Salesforce and MuleSoft buttons are optional examples, not the only messages you can send.",
    ]),
  );
  elements.conversation.scrollTop = 0;
  requestAnimationFrame(() => elements.conversation.setAttribute("aria-live", "polite"));
}

function renderConversationModelActivity(conversation) {
  const calls = Array.isArray(conversation?.model_calls) ? conversation.model_calls : [];
  if (calls.length === 0) {
    elements.modelActivitySection.hidden = true;
    return;
  }
  elements.modelActivitySection.hidden = false;
  elements.modelCallCount.textContent =
    `${calls.length} intake ${calls.length === 1 ? "turn" : "turns"}`;
  clear(elements.modelCallList);
  calls.forEach((call, index) => {
    const latency = durationLabel(call.latency_ms);
    const receipt = shortReceipt(call);
    elements.modelCallList.append(
      modelCallEntry({
        role: "architect",
        title: "Architect",
        descriptor: roleDescriptions.Architect,
        subline: `Turn ${index + 1} · intake exchange ${call.exchange || 1} · structured reply`,
        metaParts: [
          latency ? `latency ${latency}` : "",
          tokenSummary(call),
          receipt ? `receipt ${receipt}` : "",
        ],
      }),
    );
  });
}

function renderConversationView(conversation) {
  if (!rememberConversationId(conversation.conversation_id)) {
    throw new Error("The agent returned an invalid conversation identity.");
  }
  state.conversation = conversation;
  state.conversationSyncUncertain = false;
  if (["salesforce", "mulesoft"].includes(conversation.selected_platform)) {
    state.selectedPlatform = conversation.selected_platform;
  }
  if (typeof conversation.selected_scenario_id === "string") {
    state.selectedScenarioId = conversation.selected_scenario_id;
  } else if (state.selectedPlatform) {
    state.selectedScenarioId = scenarioForPlatform(state.selectedPlatform)?.scenario_id || "";
  }
  state.platformSelectionDirty = false;
  elements.decisionForm = null;
  elements.conversation.setAttribute("aria-live", "off");
  clear(elements.conversation);
  const messages = conversationMessages(conversation);
  if (messages.length === 0) {
    renderEmptyConversation();
  } else {
    messages.forEach((message) => {
      elements.conversation.append(
        messageArticle(conversationMessageRole(message), [conversationMessageContent(message)]),
      );
    });
  }
  const launchGate = conversationLaunchGate(conversation);
  if (launchGate) {
    elements.conversation.append(launchGate);
  }
  elements.conversation.scrollTop = elements.conversation.scrollHeight;
  requestAnimationFrame(() => elements.conversation.setAttribute("aria-live", "polite"));
  const readiness = conversationReadiness(conversation);
  const launchPending = conversation.status === "launch_pending";
  renderHarnessStages([
    {
      key: "architect",
      label: "Architect intake",
      state: messages.length > 0 ? "complete" : "ready",
      detail: messages.length > 0
        ? "Typed public reply recorded; private reasoning was not requested or stored."
        : "Ready for a normal conversation about the migration request.",
    },
    {
      key: "approval",
      label: "Scenario launch gate",
      state: launchPending ? "active" : readiness.ready ? "ready" : "waiting",
      detail: launchPending
        ? "An exact launch is reserved. Retry Start migration to recover that same run."
        : readiness.ready
          ? "Start migration to create a digest-bound plan for human review."
        : "Opens only after a supported scenario is selected and its canonical contract is explicitly launched.",
    },
    {
      key: "engineer",
      label: "Engineer",
      state: "waiting",
      detail: "Cannot run before a digest-bound plan is approved.",
    },
    {
      key: "validator",
      label: "Validator",
      state: "waiting",
      detail: "Cannot run before an isolated candidate exists.",
    },
  ]);
  renderConversationModelActivity(conversation);
  updateModelPresentation();
  setRunStatus(
    launchPending ? "launch interrupted · retry available" : readiness.ready ? "request ready" : "conversation open",
  );
  renderScenarios();
  updateComposerState();
}

async function createConversation(scenarioId = null) {
  const response = await api("/api/conversations", {
    method: "POST",
    body: { scenario_id: scenarioId },
  });
  return conversationFromPayload(await response.json());
}

async function startNewChat(initialDraft = "") {
  if (state.busy) {
    return;
  }
  clearAlert();
  state.busy = true;
  if (elements.decisionForm) {
    elements.decisionForm.querySelectorAll("button, input, textarea").forEach((control) => {
      control.disabled = true;
    });
  }
  elements.newChatButton.textContent = "Opening…";
  updateComposerState();
  let conversationOpened = false;
  try {
    const conversation = await createConversation(null);
    markNewConversationRequested();
    rememberConversationId(conversation.conversation_id);
    state.conversation = conversation;
    state.run = null;
    state.exportResult = null;
    state.decision = null;
    state.retryApproval = null;
    state.selectedPlatform = "";
    state.platformSelectionDirty = false;
    stopRunTimer();
    elements.runTimer.hidden = true;
    elements.runTimer.textContent = "0:00";
    elements.requestInput.value =
      typeof initialDraft === "string" ? initialDraft.slice(0, 2000) : "";
    resetEvidence();
    selectInspectionView("evidence");
    updateModelPresentation();
    renderConversationView(conversation);
    conversationOpened = true;
  } catch (error) {
    showAlert(error instanceof Error ? error.message : "A new chat could not be opened.");
  } finally {
    state.busy = false;
    if (elements.decisionForm) {
      elements.decisionForm.querySelectorAll("button, input, textarea").forEach((control) => {
        control.disabled = false;
      });
    }
    elements.newChatButton.textContent = "New chat";
    updateComposerState();
    if (conversationOpened) {
      elements.requestInput.focus();
      elements.requestInput.setSelectionRange(
        elements.requestInput.value.length,
        elements.requestInput.value.length,
      );
      elements.requestInput.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
    }
  }
}

function renderFailureDiagnostic(run) {
  const failure = run.failure;
  elements.failureDiagnostic.hidden = !failure;
  clear(elements.failureFacts);
  if (!failure) {
    elements.failureTitle.textContent = "";
    elements.failureGuidance.textContent = "";
    return;
  }

  const failureTitles = {
    configuration_invalid: "The model-provider configuration was rejected.",
    provider_refusal: "The model provider declined the structured role request.",
    response_incomplete: "The model-provider response ended before it was complete.",
    structured_output_invalid: "The model response did not satisfy the typed role contract.",
    unauthorized_tool_call: "The controller blocked a native provider tool call outside the typed role response.",
    model_inventory_invalid: "The selected model or runtime identity could not be verified.",
    provider_response_invalid: "The model-provider response failed protocol validation.",
    required_approval_missing: "The Controller-expanded manifest was missing the required human approval binding.",
    implementation_contract_invalid: "The Controller-expanded manifest failed its controller-owned implementation-contract check.",
    transformation_scope_invalid: "The Controller-expanded manifest crossed the frozen transformation boundary.",
    unresolved_question_risk_missing: "The Architect returned blocking questions without a corresponding human-decision risk.",
    correction_delta_required: "Engineer attempt 2 did not return the required changed-file correction delta.",
    correction_no_material_changes: "Engineer attempt 2 returned no material content changes.",
    correction_signal_coverage_missing: "Engineer attempt 2 did not cover every repair signal.",
    correction_scope_invalid: "Engineer attempt 2 crossed the allowed repair boundary.",
    correction_identical_candidate: "Engineer attempt 2 reproduced the attempt-1 candidate.",
    file_plan_scope_mismatch: "The Engineer file plan did not match the approved output scope.",
    file_plan_delta_mismatch: "The workspace changes did not match the Engineer file plan.",
    workspace_scope_mismatch: "The Engineer workspace did not match the manifest scope.",
    workspace_not_clean: "The Engineer workspace was not clean before applying the plan.",
    attempt_two_scope_expansion_invalid: "Engineer attempt 2 requested an invalid scope expansion.",
    policy_rejected: "The controller rejected the structured response at its policy boundary.",
    provider_timeout: "The model provider exceeded the inference deadline.",
    provider_unavailable: "The model-provider request could not be completed.",
    deterministic_validation_failed: "The deterministic validation boundary could not complete.",
    internal_failure: "A sanitized internal boundary stopped the workflow.",
  };
  elements.failureTitle.textContent =
    failureTitles[failure.reason_code] ||
    `The ${humanize(failure.seam)} boundary stopped the workflow.`;

  if (typeof failure.guidance === "string" && failure.guidance.trim() !== "") {
    elements.failureGuidance.textContent = failure.guidance;
  } else if (failure.response_received === false || failure.category === "provider_unavailable") {
    elements.failureGuidance.textContent =
      `${providerRecoveryGuidance(run.boundaries.model_id, run.boundaries)} ` +
      "This immutable run did not authorize downstream work.";
  } else if (failure.category === "invalid") {
    elements.failureGuidance.textContent =
      "Start a new run with the bounded slice prompt. The controller stopped this run before an invalid output could reach another role.";
  } else {
    elements.failureGuidance.textContent =
      "Review the public failure facts below, correct the model-provider state or request if needed, and start a new run. No external action was taken.";
  }

  const facts = [
    ["reason", failure.reason_code ? humanize(failure.reason_code) : ""],
    ["phase", failure.phase ? humanize(failure.phase) : ""],
    ["seam", humanize(failure.seam)],
    ["category", humanize(failure.category)],
    ["attempt", String(failure.attempt)],
    ["response", typeof failure.response_received === "boolean" ? (failure.response_received ? "received" : "not received") : ""],
    ["schema", typeof failure.schema_valid === "boolean" ? (failure.schema_valid ? "valid" : "invalid") : ""],
    ["policy", typeof failure.policy_valid === "boolean" ? (failure.policy_valid ? "valid" : "invalid") : ""],
    ["resume", failure.retry_eligible ? "eligible" : "new run required"],
  ];
  facts.filter(([, value]) => value !== "").forEach(([label, value]) => {
    const item = document.createElement("div");
    item.append(textElement("dt", "", label), textElement("dd", "", value));
    elements.failureFacts.append(item);
  });
}

function durationLabel(milliseconds) {
  if (typeof milliseconds !== "number" || !Number.isFinite(milliseconds) || milliseconds < 0) {
    return "";
  }
  if (milliseconds < 1000) {
    return `${Math.round(milliseconds)} ms`;
  }
  return `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 1 : 0)} s`;
}

function tokenValue(call, direction) {
  const tokenCounts = call.token_counts && typeof call.token_counts === "object"
    ? call.token_counts
    : {};
  const names = direction === "input"
    ? ["input_tokens", "prompt_tokens", "input", "prompt"]
    : ["output_tokens", "completion_tokens", "output", "completion"];
  for (const name of names) {
    const value = call[name] ?? tokenCounts[name];
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) {
      return Math.round(value);
    }
  }
  return null;
}

function shortReceipt(call) {
  const digests = call.digests && typeof call.digests === "object" ? call.digests : {};
  const candidates = [
    call.response_digest,
    call.output_digest,
    digests.response,
    digests.output,
  ];
  const digest = candidates.find(
    (value) => typeof value === "string" && value.startsWith("sha256:"),
  );
  return digest ? `${digest.slice(0, 15)}…` : "";
}

function capitalizeRole(role) {
  const value = typeof role === "string" ? role.trim() : "";
  if (value === "") {
    return "";
  }
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function tokenSummary(call) {
  const input = tokenValue(call, "input");
  const output = tokenValue(call, "output");
  const format = (value) => Number(value).toLocaleString();
  if (input !== null && output !== null) {
    return `${format(input)} in → ${format(output)} out`;
  }
  if (input !== null) {
    return `${format(input)} in`;
  }
  if (output !== null) {
    return `${format(output)} out`;
  }
  return "";
}

// Build one agent turn in the activity timeline: a role-colored avatar, the
// role's human name + descriptor, a sequence sub-line, and metadata chips.
// Shared by intake, full-run, and the empty provider-boundary state so every
// row reads with the same conversational identity.
function modelCallEntry({ role, title, descriptor, subline, metaParts, fallbackMeta }) {
  const normalizedRole = typeof role === "string" ? role.trim().toLowerCase() : "";
  const item = document.createElement("li");
  item.className = `model-call-item${normalizedRole ? ` is-${normalizedRole}` : ""}`;
  const badgeText =
    roleAvatars[capitalizeRole(normalizedRole)] ||
    (normalizedRole ? normalizedRole.charAt(0).toUpperCase() : "AI");
  const badge = textElement(
    "span",
    `model-call-role${normalizedRole ? ` is-${normalizedRole}` : ""}`,
    badgeText,
  );
  badge.setAttribute("aria-hidden", "true");
  const body = document.createElement("div");
  body.className = "model-call-body";
  const heading = textElement("strong", "", title);
  if (descriptor) {
    heading.append(textElement("em", "model-call-descriptor", descriptor));
  }
  body.append(heading);
  if (subline) {
    body.append(textElement("span", "model-call-turn", subline));
  }
  const meta = document.createElement("span");
  meta.className = "model-call-meta";
  (metaParts || [])
    .filter(Boolean)
    .forEach((value) => meta.append(textElement("span", "", value)));
  if (!meta.hasChildNodes()) {
    meta.append(textElement("span", "", fallbackMeta || "typed call receipt recorded"));
  }
  body.append(meta);
  item.append(badge, body);
  return item;
}

function renderModelActivity(run) {
  const calls = Array.isArray(run.model_calls) ? run.model_calls : [];
  const providerAttempted =
    run.boundaries.provider_attempted ?? run.boundaries.provider_invoked === true;
  const structuredResponseAccepted =
    run.boundaries.structured_response_accepted ?? run.boundaries.provider_invoked === true;
  elements.modelActivitySection.hidden = false;
  elements.modelCallCount.textContent =
    `${calls.length} agent ${calls.length === 1 ? "turn" : "turns"}`;
  clear(elements.modelCallList);

  if (calls.length === 0) {
    const invocationState =
      run.boundaries.provider_invoked === true
        ? "provider invoked"
        : run.boundaries.provider_invoked === false
          ? "not invoked"
          : providerAttempted
            ? "invocation not proven"
            : "not invoked";
    elements.modelCallList.append(
      modelCallEntry({
        role: "",
        title: "Provider boundary",
        subline: "No accepted model turn in this run",
        metaParts: [
          invocationState,
          structuredResponseAccepted ? "typed output accepted" : "no accepted typed output",
          run.boundaries.model_call_record_persisted
            ? "model receipt persisted"
            : "no model receipt persisted",
        ],
      }),
    );
    return;
  }

  calls.forEach((call, index) => {
    const role = typeof call.role === "string" && call.role.trim() !== "" ? call.role : "model";
    const attempt = call.attempt || 1;
    const latency = durationLabel(call.latency_ms);
    const receipt = shortReceipt(call);
    const subline =
      `Turn ${index + 1} · ${attempt > 1 ? `correction attempt ${attempt}` : "structured reply"}`;
    elements.modelCallList.append(
      modelCallEntry({
        role,
        title: capitalizeRole(role),
        descriptor: roleDescriptions[capitalizeRole(role)],
        subline,
        metaParts: [
          latency ? `latency ${latency}` : "",
          tokenSummary(call),
          receipt ? `receipt ${receipt}` : "",
        ],
      }),
    );
  });
}

function extractDiffForPath(unifiedDiff, path) {
  if (!path || !unifiedDiff.includes("diff --git ")) {
    return unifiedDiff;
  }
  const lines = unifiedDiff.split("\n");
  const start = lines.findIndex(
    (line) => line.startsWith("diff --git ") && line.includes(` b/${path}`),
  );
  if (start < 0) {
    return unifiedDiff;
  }
  const next = lines.findIndex((line, index) => index > start && line.startsWith("diff --git "));
  return lines.slice(start, next < 0 ? lines.length : next).join("\n");
}

function renderCodeLines(content, isDiff) {
  clear(elements.candidateDiff);
  const lines = String(content || "").split("\n");
  lines.forEach((line) => {
    const rendered = textElement("span", "code-line", line || " ");
    if (isDiff) {
      if (
        line.startsWith("diff --git ") ||
        line.startsWith("index ") ||
        line.startsWith("--- ") ||
        line.startsWith("+++ ")
      ) {
        rendered.classList.add("diff-header");
      } else if (line.startsWith("@@")) {
        rendered.classList.add("diff-hunk");
      } else if (line.startsWith("+")) {
        rendered.classList.add("diff-addition");
      } else if (line.startsWith("-")) {
        rendered.classList.add("diff-deletion");
      }
    }
    elements.candidateDiff.append(rendered);
  });
}

function renderCandidateCode() {
  const candidate = state.run?.candidate;
  if (!candidate) {
    return;
  }
  const path = state.selectedCandidatePath || candidate.changed_paths[0] || "Candidate";
  state.selectedCandidatePath = path;
  const diffSelected = state.candidateView === "diff";
  elements.diffViewButton.classList.toggle("is-active", diffSelected);
  elements.fileViewButton.classList.toggle("is-active", !diffSelected);
  elements.diffViewButton.setAttribute("aria-pressed", String(diffSelected));
  elements.fileViewButton.setAttribute("aria-pressed", String(!diffSelected));
  elements.changedFiles.querySelectorAll("button").forEach((button) => {
    button.setAttribute("aria-current", String(button.dataset.path === path));
  });

  if (diffSelected) {
    elements.codeViewerPath.textContent = `${path} · unified diff`;
    renderCodeLines(extractDiffForPath(candidate.unified_diff, path), true);
    elements.candidateDiff.setAttribute("aria-label", `Unified diff for ${path}`);
    return;
  }
  const candidateFile = candidate.files.find((item) => item.path === path);
  elements.codeViewerPath.textContent = path;
  renderCodeLines(
    candidateFile
      ? candidateFile.content
      : "Generated file content is unavailable in this stored run. Review the unified diff instead.",
    false,
  );
  elements.candidateDiff.setAttribute("aria-label", `Generated file ${path}`);
}

function renderCandidate(candidate) {
  const available = Boolean(candidate);
  const downloadable = available && candidate.download_available !== false;
  const exported = Boolean(
    downloadable &&
      state.exportResult &&
      state.run &&
      state.exportResult.handle === state.run.handle &&
      state.exportResult.attempt === candidate.attempt,
  );
  elements.candidateSection.hidden = !available;
  elements.downloadButton.hidden = !downloadable;
  elements.exportButton.hidden = !downloadable;
  const readyForReview = state.run?.validation?.disposition === "ready_for_human_review";
  elements.downloadButton.textContent = readyForReview
    ? "↓ Download review candidate"
    : "↓ Download failed candidate for debugging";
  elements.exportButton.disabled = state.busy || exported;
  elements.exportButton.textContent = exported
    ? "Saved to output/"
    : readyForReview
      ? "↳ Save review candidate"
      : "↳ Save debugging evidence";
  elements.exportStatus.hidden = !exported;
  elements.exportStatus.textContent = exported
    ? `${state.exportResult.file_count} files saved to ${state.exportResult.candidate_path}. ` +
      (state.exportResult.ready_for_human_review
        ? "Local checks passed; final human and platform review remain separate."
        : `Candidate-only export; validation disposition: ${humanize(state.exportResult.validation_disposition)}.`)
    : "";
  const finalReview = state.run?.final_review;
  elements.candidateReviewStatus.textContent = !candidate
    ? ""
    : !readyForReview
      ? `Validation disposition: ${humanize(state.run?.validation?.disposition)}. Debugging evidence only.`
      : finalReview?.status === "accepted"
        ? "Final human review: accepted. External actions remain unauthorized."
        : finalReview?.status === "rejected"
          ? "Final human review: rejected."
          : finalReview?.status === "changes_requested"
            ? "Final human review: changes requested; start a new bounded run."
            : finalReview?.status === "awaiting_final_review"
              ? `Final human review pending: ${finalReview.designated_reviewer}.`
              : "Ready for independent final human review; review has not been requested.";
  elements.changesTab.disabled = !available;
  elements.changesTab.setAttribute("aria-disabled", String(!available));
  const count = candidate?.changed_paths.length || 0;
  const priorCandidate = Boolean(
    candidate && state.run && candidate.attempt !== state.run.execution_attempt,
  );
  elements.candidateProvenance.textContent = candidate
    ? priorCandidate
      ? `Prior attempt ${candidate.attempt} candidate · read-only debugging evidence`
      : readyForReview
        ? `Attempt ${candidate.attempt} review candidate · ready for independent human review`
        : `Attempt ${candidate.attempt} failed candidate · debugging evidence only`
    : "Isolated candidate";
  elements.fileCountBadge.textContent = String(count);
  elements.fileCount.textContent = `${count} file${count === 1 ? "" : "s"}`;
  clear(elements.changedFiles);
  if (!candidate) {
    state.selectedCandidatePath = "";
    selectInspectionView("evidence");
    return;
  }
  if (!candidate.changed_paths.includes(state.selectedCandidatePath)) {
    state.selectedCandidatePath = candidate.changed_paths[0] || "";
  }
  candidate.changed_paths.forEach((path) => {
    const item = document.createElement("li");
    const button = textElement("button", "file-button", path);
    button.type = "button";
    button.dataset.path = path;
    button.title = path;
    button.addEventListener("click", () => {
      state.selectedCandidatePath = path;
      renderCandidateCode();
    });
    item.append(button);
    elements.changedFiles.append(item);
  });
  renderCandidateCode();
  selectInspectionView("changes");
}

function resetEvidence() {
  clear(elements.metrics);
  ["Graph nodes", "Graph edges", "Wiki hits", "Changed files"].forEach((label) => {
    const wrapper = document.createElement("div");
    wrapper.append(textElement("dt", "", label), textElement("dd", "", "—"));
    elements.metrics.append(wrapper);
  });
  renderList(
    elements.manifestPaths,
    [],
    "Available after Controller manifest expansion",
    "code-path",
  );
  elements.graphSummary.textContent = "Evidence will appear here.";
  renderList(elements.wikiHits, [], "No retrieval evidence yet");
  state.candidateView = "diff";
  state.selectedCandidatePath = "";
  renderCandidate(null);
  elements.validationSection.hidden = true;
  elements.validationSummaryCount.textContent = "";
  elements.validatorAssessment.textContent = "";
  elements.validatorConcernsGroup.hidden = true;
  clear(elements.validatorConcerns);
  elements.downloadButton.hidden = true;
  elements.exportButton.hidden = true;
  elements.exportStatus.hidden = true;
  elements.exportStatus.textContent = "";
  elements.failureDiagnostic.hidden = true;
  clear(elements.failureFacts);
  elements.modelActivitySection.hidden = true;
  elements.modelCallCount.textContent = "";
  clear(elements.modelCallList);
}

function renderMetrics(metrics) {
  const values = [
    ["Graph nodes", metrics.graph_nodes],
    ["Graph edges", metrics.graph_edges],
    ["Wiki hits", metrics.wiki_hits],
    ["Changed files", metrics.changed_files],
  ];
  clear(elements.metrics);
  values.forEach(([label, value]) => {
    const wrapper = document.createElement("div");
    wrapper.append(textElement("dt", "", label), textElement("dd", "", String(value)));
    elements.metrics.append(wrapper);
  });
}

function renderList(element, values, emptyText, className = "") {
  clear(element);
  if (!values || values.length === 0) {
    element.classList.add("empty-list");
    element.append(textElement("li", "", emptyText));
    return;
  }
  element.classList.remove("empty-list");
  values.forEach((value) => element.append(textElement("li", className, value)));
}

function renderEvidence(run) {
  renderMetrics(run.metrics);
  renderModelActivity(run);
  const paths = run.manifest ? run.manifest.approved_paths : [];
  elements.manifestPathsTitle.textContent =
    run.status === "completed"
      ? "Approved Controller-expanded paths"
      : "Controller-expanded manifest paths";
  renderList(
    elements.manifestPaths,
    paths,
    "Available after Controller manifest expansion",
    "code-path",
  );
  elements.graphSummary.textContent = run.evidence.graph_summary;
  clear(elements.wikiHits);
  if (run.evidence.wiki_hits.length === 0) {
    elements.wikiHits.classList.add("empty-list");
    elements.wikiHits.append(textElement("li", "", "No retrieval evidence yet"));
  } else {
    elements.wikiHits.classList.remove("empty-list");
    run.evidence.wiki_hits.forEach((hit) => {
      const item = document.createElement("li");
      item.append(
        textElement("strong", "", hit.title),
        textElement("span", "code-path", hit.path),
      );
      elements.wikiHits.append(item);
    });
  }

  renderCandidate(run.candidate);
  renderFailureDiagnostic(run);

  const validation = run.validation;
  elements.validationSection.hidden = !validation;
  if (validation) {
    const passed = validation.results.filter((result) => result.status === "passed").length;
    const rootFailures = validation.results.filter(
      (result) => result.status === "failed" && !result.dependent_on,
    ).length;
    const dependentChecks = validation.results.filter((result) =>
      Boolean(result.dependent_on),
    ).length;
    elements.validationSummaryCount.textContent = dependentChecks
      ? `${passed}/${validation.results.length} passed · ${rootFailures} root ${rootFailures === 1 ? "failure" : "failures"} · ${dependentChecks} dependent ${dependentChecks === 1 ? "check" : "checks"}`
      : `${passed}/${validation.results.length} passed`;
    elements.validatorSummary.textContent = validation.validator_summary;
    elements.validatorAssessment.textContent =
      validation.validator_completed !== false && validation.advisory_assessment
        ? `Validator advisory: ${validation.advisory_assessment.replaceAll("_", " ")}. ` +
          `Deterministic disposition is authoritative: ${validation.disposition.replaceAll("_", " ")}.`
        : `Validator advisory did not complete. Deterministic disposition remains authoritative: ${validation.disposition.replaceAll("_", " ")}.`;
    elements.validatorConcernsGroup.hidden = validation.concerns.length === 0;
    renderList(
      elements.validatorConcerns,
      validation.concerns,
      "No advisory concerns are available.",
    );
    clear(elements.validationResults);
    validation.results.forEach((result) => {
      const item = document.createElement("li");
      const dependent = Boolean(result.dependent_on);
      item.className = `validation-result ${dependent ? "dependent" : result.status}`;
      item.append(
        textElement("span", "validation-dot", ""),
        textElement("strong", "", result.check_id),
        textElement("span", "validation-status", dependent ? "dependent" : result.status),
        textElement(
          "p",
          "",
          dependent
            ? `No tests ran because ${result.dependent_on} prevented the LWC module from loading. Fix that root failure, then rerun this check. Raw result: ${result.summary}`
            : result.summary,
        ),
      );
      if (result.diagnostic_ids?.length) {
        item.append(
          textElement(
            "p",
            "message-meta",
            `Typed diagnostics: ${result.diagnostic_ids.join(", ")}`,
          ),
        );
      }
      elements.validationResults.append(item);
    });
  } else {
    elements.validationSummaryCount.textContent = "";
  }
}

function setRunStatus(text, statusClass = "") {
  elements.runStatus.textContent = text;
  elements.runStatus.title = text;
  elements.runStatus.className = statusClass
    ? `status-chip ${statusClass}`
    : "status-chip";
}

function renderRun(run) {
  if (!run || !rememberRunHandle(run.handle)) {
    throw new Error("The agent returned an invalid run handle.");
  }
  if (
    state.exportResult &&
    (state.exportResult.handle !== run.handle ||
      state.exportResult.attempt !== run.candidate?.attempt)
  ) {
    state.exportResult = null;
  }
  state.run = run;
  const scenario = scenarioById(run.scenario_id);
  if (!scenario || scenario.platform !== run.platform || scenario.title !== run.scenario_title) {
    throw new Error("The saved run does not match a known migration scenario.");
  }
  if (run.correction?.approval) {
    state.retryApproval = null;
  }
  if (state.scenarios.some((candidate) => candidate.scenario_id === run.scenario_id)) {
    state.selectedPlatform = run.platform;
    state.selectedScenarioId = run.scenario_id;
    state.platformSelectionDirty = false;
    elements.requestInput.value = run.prompt;
  }
  const statusText =
    run.status === "completed" ? "workflow completed" : run.status.replaceAll("_", " ");
  const dispositionText =
    run.status === "rejected"
      ? "human stop"
      : run.terminal_disposition
        ? `disposition: ${run.terminal_disposition.replaceAll("_", " ")}`
        : "";
  const renderedStatus = dispositionText
    ? `attempt ${run.execution_attempt} · ${statusText} · ${dispositionText}`
    : `attempt ${run.execution_attempt} · ${statusText}`;
  setRunStatus(renderedStatus, `status-${run.status}`);
  updateModelPresentation(run.boundaries, run.failure);
  renderHarnessStages(run.stages);
  renderConversation(run);
  renderEvidence(run);
  const conversationTarget = elements.conversation.lastElementChild;
  if (conversationTarget instanceof HTMLElement) {
    conversationTarget.tabIndex = -1;
    conversationTarget.focus({ preventScroll: true });
    conversationTarget.scrollIntoView({ behavior: scrollBehavior(), block: "nearest" });
  }
  updateComposerState();
}

async function sendConversationMessage(event) {
  event.preventDefault();
  if (state.busy) {
    return;
  }
  if (state.run) {
    showAlert("Choose New chat to start a separate conversation without changing this saved run.");
    return;
  }
  if (state.conversation?.status === "launch_pending") {
    showAlert("Retry Start migration for this frozen request, or choose New chat.");
    return;
  }
  if (state.conversation?.status === "launched") {
    showAlert("This conversation already launched a run. Choose New chat for another request.");
    return;
  }
  const message = elements.requestInput.value.trim();
  if (!state.model) {
    showAlert("The server-owned model configuration is unavailable.");
    return;
  }
  if (!modelRuntimeReady()) {
    showAlert("The configured model provider is not ready. Correct the runtime state and reload.");
    return;
  }
  if (message.length < 1 || message.length > 2000) {
    showAlert("Enter a message between 1 and 2000 characters.");
    return;
  }
  clearAlert();
  const previousConversation = state.conversation;
  let conversation = previousConversation;
  let messageConversationId = "";
  let messageRequestStarted = false;
  resetEvidence();
  updateModelPresentation();
  elements.modeBadge.textContent = `${configuredInferenceLabel()} · awaiting Architect reply`;
  setRunStatus("architect responding");
  setBusy(true, "Architect intake", "Responding to your migration message");
  try {
    if (!conversation) {
      conversation = await createConversation(state.selectedScenarioId || null);
      // An open conversation is now the active UI context. Clear any stale
      // run handle before remembering it so reload cannot replace this chat
      // with an unrelated prior run after a transient restore failure.
      markNewConversationRequested();
      state.conversation = conversation;
      rememberConversationId(conversation.conversation_id);
      renderConversationView(conversation);
    }
    renderPendingMessage(message);
    messageConversationId = conversation.conversation_id;
    messageRequestStarted = true;
    const response = await api(
      `/api/conversations/${messageConversationId}/messages`,
      {
        method: "POST",
        body: {
          message,
          scenario_id: state.selectedScenarioId || null,
        },
      },
    );
    const updated = conversationFromPayload(await response.json());
    elements.requestInput.value = "";
    renderConversationView(updated);
  } catch (error) {
    let recoveredConversation = null;
    if (messageRequestStarted && validConversationId(messageConversationId)) {
      try {
        const recoveryResponse = await api(`/api/conversations/${messageConversationId}`);
        const recovered = conversationFromPayload(await recoveryResponse.json());
        if (recovered.conversation_id !== messageConversationId) {
          throw new AgentUiApiError(
            "Recovered conversation identity mismatch.",
            "conversation_identity_mismatch",
            409,
          );
        }
        recoveredConversation = recovered;
      } catch (_recoveryError) {
        // The POST outcome is ambiguous. Keep launch disabled until a later
        // message or reload obtains an authoritative server snapshot.
      }
    }
    const fallbackConversation = recoveredConversation || previousConversation || conversation;
    state.conversation = fallbackConversation;
    if (fallbackConversation) {
      renderConversationView(fallbackConversation);
    } else {
      renderEmptyConversation();
      renderHarnessStages(readyHarnessStages);
    }
    state.conversationSyncUncertain = messageRequestStarted && !recoveredConversation;
    updateComposerState();
    const failureMessage = error instanceof Error ? error.message : "The Architect could not respond.";
    showAlert(
      state.conversationSyncUncertain
        ? `${failureMessage} The latest conversation state could not be confirmed, so migration launch is disabled.`
        : failureMessage,
    );
  } finally {
    setBusy(false);
  }
}

async function launchMigration() {
  if (state.busy || state.run || !state.conversation) {
    return;
  }
  if (!modelRuntimeReady()) {
    showAlert("The configured model provider is not ready. Correct the runtime state and reload.");
    return;
  }
  const readiness = conversationReadiness(state.conversation);
  if (
    !readiness.ready ||
    state.platformSelectionDirty ||
    state.conversationSyncUncertain ||
    readiness.launchToken === null
  ) {
    showAlert(
      "Select a supported scenario and receive an Architect advisory before starting the migration.",
    );
    return;
  }
  const previousConversation = state.conversation;
  const launchConversationId = state.conversation.conversation_id;
  clearAlert();
  updateModelPresentation();
  elements.modeBadge.textContent = `${configuredInferenceLabel()} · awaiting Architect plan`;
  setRunStatus("architect planning");
  setBusy(
    true,
    "Architect",
    "Controller preparing exact source, graph, and Wiki evidence; Architect will return a semantic recommendation",
  );
  try {
    const response = await api(
      `/api/conversations/${launchConversationId}/launch`,
      {
        method: "POST",
        body: { launch_token: readiness.launchToken },
      },
    );
    const payload = await response.json();
    if (payload.conversation) {
      state.conversation = conversationFromPayload(payload.conversation);
      rememberConversationId(state.conversation.conversation_id);
    }
    const run = payload.run || payload.session || payload.run_view;
    if (!run) {
      throw new AgentUiApiError(
        "The migration launch did not return a run.",
        "run_unavailable",
        409,
      );
    }
    renderRun(run);
  } catch (error) {
    let authoritativeConversation = null;
    try {
      const conversationResponse = await api(`/api/conversations/${launchConversationId}`);
      const recovered = conversationFromPayload(await conversationResponse.json());
      if (recovered.conversation_id !== launchConversationId) {
        throw new AgentUiApiError(
          "Recovered conversation identity mismatch.",
          "conversation_identity_mismatch",
          409,
        );
      }
      authoritativeConversation = recovered;
    } catch (_recoveryError) {
      // Retain the reviewed snapshot. Repeating launch with the same token is
      // safe because the server makes exact-token launch idempotent.
    }
    if (authoritativeConversation) {
      state.conversation = authoritativeConversation;
      rememberConversationId(authoritativeConversation.conversation_id);
      if (validRunHandle(authoritativeConversation.launch_handle)) {
        try {
          const runResponse = await api(
            `/api/sessions/${authoritativeConversation.launch_handle}`,
          );
          const recoveredRun = await runResponse.json();
          if (recoveredRun.handle !== authoritativeConversation.launch_handle) {
            throw new AgentUiApiError(
              "Recovered run identity mismatch.",
              "run_identity_mismatch",
              409,
            );
          }
          renderRun(recoveredRun);
          showAlert("The launch response was interrupted, but the exact saved run was recovered.");
          return;
        } catch (_runRecoveryError) {
          // The conversation remains authoritative and its exact launch can be
          // retried safely after the run becomes readable.
        }
      }
      renderConversationView(authoritativeConversation);
    } else {
      state.conversation = previousConversation;
      renderConversationView(previousConversation);
    }
    const failureMessage =
      error instanceof Error ? error.message : "The migration could not be started.";
    showAlert(
      error instanceof AgentUiApiError && error.code === "stale_conversation"
        ? "This conversation changed after the preview was shown. Review the refreshed request before starting migration."
        : failureMessage,
    );
  } finally {
    setBusy(false);
  }
}

async function submitDecision(selection) {
  if (!state.run || state.busy) {
    return;
  }
  if (selection !== "approve" && selection !== "reject") {
    showAlert("Choose Approve or Reject to record an explicit human decision.");
    return;
  }
  if (!elements.decisionForm || !elements.decisionForm.reportValidity()) {
    return;
  }
  const formData = new FormData(elements.decisionForm);
  state.decision = {
    selection,
    reviewer: String(formData.get("reviewer") || ""),
    comment: String(formData.get("comment") || ""),
  };
  clearAlert();
  setRunStatus(
    selection === "approve" ? "engineer and validator working" : "recording decision",
  );
  setBusy(
    true,
    selection === "approve" ? "Engineer → Validator" : "Controller",
    selection === "approve"
      ? "Running the gated continuation; verified stages appear when it returns"
      : "Recording the stop decision",
  );
  try {
    const response = await api(`/api/sessions/${state.run.handle}/decision`, {
      method: "POST",
      body: state.decision,
    });
    renderRun(await response.json());
  } catch (error) {
    state.decision = null;
    renderRun(state.run);
    showAlert(error instanceof Error ? error.message : "The decision could not be saved.");
  } finally {
    setBusy(false);
  }
}

async function requestFinalReview() {
  if (!state.run?.final_review?.can_request || state.busy || !elements.decisionForm) {
    return;
  }
  if (!elements.decisionForm.reportValidity()) {
    return;
  }
  const formData = new FormData(elements.decisionForm);
  const body = {
    requester: String(formData.get("requester") || ""),
    designated_reviewer: String(formData.get("designated_reviewer") || ""),
  };
  clearAlert();
  setBusy(true, "Controller", "Binding the exact candidate to an independent human review");
  try {
    const response = await api(`/api/sessions/${state.run.handle}/final-review/request`, {
      method: "POST",
      body,
    });
    renderRun(await response.json());
  } catch (error) {
    showAlert(error instanceof Error ? error.message : "Final review could not be requested.");
  } finally {
    setBusy(false);
  }
}

async function submitFinalReviewDecision(selection) {
  if (
    !state.run?.final_review?.can_decide ||
    state.busy ||
    !elements.decisionForm ||
    !["accept", "reject", "request_changes"].includes(selection)
  ) {
    return;
  }
  if (!elements.decisionForm.reportValidity()) {
    return;
  }
  const formData = new FormData(elements.decisionForm);
  const body = {
    selection,
    reviewer: String(formData.get("reviewer") || ""),
    comment: String(formData.get("comment") || ""),
  };
  clearAlert();
  setBusy(true, "Controller", "Recording the one-use final human review decision");
  try {
    const response = await api(`/api/sessions/${state.run.handle}/final-review/decision`, {
      method: "POST",
      body,
    });
    renderRun(await response.json());
  } catch (error) {
    showAlert(error instanceof Error ? error.message : "Final review could not be recorded.");
  } finally {
    setBusy(false);
  }
}

async function submitRetry() {
  if (!state.run || state.busy || !state.run.correction?.retry_available) {
    return;
  }
  if (!elements.decisionForm || !elements.decisionForm.reportValidity()) {
    return;
  }
  const formData = new FormData(elements.decisionForm);
  const reviewer = String(formData.get("reviewer") || "");
  const comment = String(formData.get("comment") || "");
  state.retryApproval = { reviewer, comment };
  clearAlert();
  setRunStatus("corrective attempt 2 working");
  setBusy(
    true,
    "Engineer correction → Validator",
    "Applying the exact typed validation feedback within the unchanged manifest",
  );
  try {
    const response = await api(`/api/sessions/${state.run.handle}/retry`, {
      method: "POST",
      body: {
        correction_id: state.run.correction.correction_id,
        reviewer,
        comment,
      },
    });
    renderRun(await response.json());
  } catch (error) {
    state.retryApproval = null;
    renderRun(state.run);
    showAlert(error instanceof Error ? error.message : "The correction could not be authorized.");
  } finally {
    setBusy(false);
  }
}

async function downloadCandidate() {
  if (!state.run || !state.run.candidate || state.busy) {
    return;
  }
  clearAlert();
  setBusy(true, "Controller", "Preparing the isolated candidate archive");
  try {
    const response = await api(`/api/sessions/${state.run.handle}/candidate.zip`, {
      requiresCsrf: true,
    });
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download =
      `migration-candidate-${state.run.platform}-${state.run.handle}-` +
      `attempt-${state.run.candidate.attempt}.zip`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    showAlert(error instanceof Error ? error.message : "The candidate could not be downloaded.");
  } finally {
    setBusy(false);
  }
}

async function exportCandidate() {
  if (
    !state.run ||
    !state.run.candidate ||
    state.run.candidate.download_available === false ||
    state.busy
  ) {
    return;
  }
  const handle = state.run.handle;
  const attempt = state.run.candidate.attempt;
  clearAlert();
  setBusy(true, "Controller", "Saving the persisted candidate bytes to output/");
  try {
    const response = await api(`/api/sessions/${handle}/export`, {
      method: "POST",
      body: {},
    });
    const result = await response.json();
    if (result.handle !== handle || result.attempt !== attempt) {
      throw new AgentUiApiError(
        "The candidate export identity did not match the displayed run.",
        "run_identity_mismatch",
        409,
      );
    }
    state.exportResult = result;
    renderCandidate(state.run.candidate);
  } catch (error) {
    showAlert(error instanceof Error ? error.message : "The candidate could not be saved.");
  } finally {
    setBusy(false);
  }
}

async function restoreStoredConversation() {
  const conversationId = storedConversationId();
  if (!conversationId) {
    return;
  }
  try {
    const response = await api(`/api/conversations/${conversationId}`);
    const conversation = conversationFromPayload(await response.json());
    if (conversation.conversation_id !== conversationId) {
      throw new AgentUiApiError(
        "Stored conversation identity mismatch.",
        "conversation_identity_mismatch",
        409,
      );
    }
    state.conversation = conversation;
    rememberConversationId(conversation.conversation_id);
    if (["salesforce", "mulesoft"].includes(conversation.selected_platform)) {
      state.selectedPlatform = conversation.selected_platform;
    }
    state.platformSelectionDirty = false;
    if (conversation.status === "launched" && validRunHandle(conversation.launch_handle)) {
      rememberRunHandle(conversation.launch_handle);
    } else {
      // A verified open or launch-pending conversation is the browser's mode
      // authority. Ignore an unrelated run handle another tab may have stored.
      clearStoredRunHandle();
      renderConversationView(conversation);
    }
  } catch (error) {
    const definitiveLoss =
      error instanceof AgentUiApiError &&
      [
        "unknown_conversation",
        "conversation_identity_mismatch",
        "conversation_unavailable",
      ].includes(error.code);
    if (definitiveLoss) {
      clearStoredConversationId();
    }
    showAlert(
      definitiveLoss
        ? "The previously open conversation is unavailable. You can start a new chat."
        : "The previously open conversation could not be refreshed; reload to try again.",
    );
  }
}

async function restoreStoredRun() {
  const handle = storedRunHandle();
  if (!handle) {
    // Passive startup restores only an identity bound to this browser. A
    // launched conversation records its exact handle before reaching here;
    // otherwise an unrelated historical run must never replace the chat.
    return;
  }
  try {
    const response = await api(`/api/sessions/${handle}`);
    const run = await response.json();
    if (!run || run.handle !== handle) {
      throw new AgentUiApiError("Stored run identity mismatch.", "run_identity_mismatch", 409);
    }
    renderRun(run);
  } catch (error) {
    const definitiveLoss =
      error instanceof AgentUiApiError &&
      ["unknown_run", "run_identity_mismatch", "run_unavailable"].includes(error.code);
    if (definitiveLoss) {
      clearStoredRunHandle();
    }
    state.run = null;
    state.conversation = state.conversation || null;
    state.exportResult = null;
    state.decision = null;
    updateComposerState();
    showAlert(
      definitiveLoss
        ? "The previously open run is unavailable. You can start a new migration."
        : "The previously open run could not be refreshed. Its handle was preserved; reload to try again.",
    );
  }
}

async function initialize() {
  try {
    const configResponse = await api("/api/config");
    const config = await configResponse.json();
    state.csrfToken = config.csrf_token;
    if (!validModelConfiguration(config.model)) {
      throw new Error("The server-owned model-provider configuration is invalid.");
    }
    state.model = config.model;
    updateModelPresentation();
    updateComposerState();
    const readinessResponse = await api("/api/readiness");
    state.modelReadiness = modelReadinessFromPayload(await readinessResponse.json());
    updateModelPresentation();
    updateComposerState();
    const scenarioResponse = await api("/api/scenarios");
    const payload = await scenarioResponse.json();
    state.scenarios = payload.scenarios;
    renderScenarios();
    await restoreStoredConversation();
    await restoreStoredRun();
  } catch (error) {
    elements.scenarioList.setAttribute("aria-busy", "false");
    showAlert(error instanceof Error ? error.message : "The agent UI could not be initialized.");
  }
}

elements.requestForm.addEventListener("submit", sendConversationMessage);
elements.newChatButton.addEventListener("click", () => startNewChat());
elements.requestInput.addEventListener("input", updateComposerState);
elements.requestInput.addEventListener("keydown", (event) => {
  if (
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.isComposing &&
    !elements.sendButton.disabled
  ) {
    event.preventDefault();
    elements.requestForm.requestSubmit();
  }
});
elements.evidenceTab.addEventListener("click", () => selectInspectionView("evidence"));
elements.changesTab.addEventListener("click", () => selectInspectionView("changes"));
elements.evidenceTab.addEventListener("keydown", inspectionTabKeydown);
elements.changesTab.addEventListener("keydown", inspectionTabKeydown);
elements.diffViewButton.addEventListener("click", () => {
  state.candidateView = "diff";
  renderCandidateCode();
});
elements.fileViewButton.addEventListener("click", () => {
  state.candidateView = "file";
  renderCandidateCode();
});
elements.focusRequestButton.addEventListener("click", () => startNewChat(state.run?.prompt || ""));
elements.downloadButton.addEventListener("click", downloadCandidate);
elements.exportButton.addEventListener("click", exportCandidate);
initialize();
