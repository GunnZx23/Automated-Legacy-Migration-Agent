from __future__ import annotations

import http.client
import json
import re
import socket
import threading
from collections.abc import Iterator, Mapping
from html.parser import HTMLParser
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest

from legacy_migration_agent.core.observability import terminal_lifecycle_logging
from legacy_migration_agent.ui import server as server_module
from legacy_migration_agent.ui.server import build_ui_server


class _IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.counts: dict[str, int] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag
        for name, value in attrs:
            if name == "id" and value is not None:
                self.counts[value] = self.counts.get(value, 0) + 1


class _FakeAgentUiService:
    def __init__(
        self,
        project_root: Path,
        *,
        ollama_model_id: str,
        ollama_timeout_seconds: float,
    ) -> None:
        self.project_root = project_root
        self.ollama_model_id = ollama_model_id
        self.ollama_timeout_seconds = ollama_timeout_seconds
        self.decisions: list[tuple[str, str, str, str]] = []
        self.exports: list[str] = []
        self.retries: list[tuple[str, str, str, str]] = []
        self.conversation_messages: list[tuple[str, str, str | None]] = []
        self.conversation_platform: str | None = None
        self.conversation_launched = False

    def model_configuration(self) -> dict[str, object]:
        return {
            "provider": "ollama",
            "model_id": self.ollama_model_id,
            "execution_boundary": "local_loopback",
        }

    def runtime_readiness(self) -> dict[str, object]:
        return {
            "provider": "ollama",
            "model_id": self.ollama_model_id,
            "configured": True,
            "ollama_reachable": True,
            "model_installed": True,
            "status": "ready",
        }

    def scenarios(self) -> tuple[dict[str, object], ...]:
        return (
            {
                "platform": "salesforce",
                "title": "Visualforce to Lightning Web Components",
                "prompt": "Migrate the bounded Account explorer fixture to LWC.",
            },
            {
                "platform": "mulesoft",
                "title": "Mule 3 to Mule 4",
                "prompt": "Migrate the bounded customer status flow to Mule 4.",
            },
        )

    def start(
        self,
        platform: str,
        *,
        prompt: str,
    ) -> dict[str, object]:
        view = _run_view(platform=platform, status="awaiting_approval", candidate=False)
        view["prompt"] = prompt
        return view

    def create_conversation(self, *, platform: str | None) -> dict[str, object]:
        self.conversation_platform = platform
        self.conversation_launched = False
        self.conversation_messages.clear()
        return _conversation_view(
            platform=platform,
            ready=False,
            launched=False,
            user_message="",
        )

    def get_conversation(self, conversation_id: str) -> dict[str, object]:
        if conversation_id != "feedfacefeedfacefeedface":
            raise RuntimeError("unknown conversation")
        ready = bool(self.conversation_messages and self.conversation_platform)
        return _conversation_view(
            platform=self.conversation_platform,
            ready=ready,
            launched=self.conversation_launched,
            user_message=(self.conversation_messages[-1][1] if self.conversation_messages else ""),
            launch_token=(_fake_launch_token(len(self.conversation_messages)) if ready else None),
        )

    def send_conversation_message(
        self,
        conversation_id: str,
        *,
        message: str,
        platform: str | None,
    ) -> dict[str, object]:
        self.conversation_platform = platform
        self.conversation_messages.append((conversation_id, message, platform))
        return _conversation_view(
            platform=platform,
            ready=platform is not None,
            launched=False,
            user_message=message,
            launch_token=(
                _fake_launch_token(len(self.conversation_messages))
                if platform is not None
                else None
            ),
        )

    def launch_conversation(
        self,
        conversation_id: str,
        *,
        launch_token: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        assert conversation_id == "feedfacefeedfacefeedface"
        expected_token = _fake_launch_token(len(self.conversation_messages))
        if launch_token != expected_token:
            raise server_module.AgentUiError("stale_conversation")
        self.conversation_launched = True
        conversation = _conversation_view(
            platform=self.conversation_platform,
            ready=True,
            launched=True,
            user_message=self.conversation_messages[-1][1],
            launch_token=expected_token,
        )
        run = _run_view(
            platform=self.conversation_platform or "salesforce",
            status="awaiting_approval",
            candidate=False,
        )
        run["prompt"] = conversation["readiness"]["refined_request"]
        return conversation, run

    def get(self, handle: str) -> dict[str, object]:
        if handle == "beadfeedbeadfeedbeadfeed":
            raise server_module.AgentUiError("run_unavailable")
        if handle == "deadbeefdeadbeefdeadbeef":
            raise RuntimeError("secret=/Users/example/private-project")
        return _run_view(platform="salesforce", status="awaiting_approval", candidate=False)

    def latest(self) -> dict[str, object]:
        return _run_view(platform="salesforce", status="awaiting_approval", candidate=False)

    def decide(
        self,
        handle: str,
        *,
        selection: str,
        reviewer: str,
        comment: str,
    ) -> dict[str, object]:
        self.decisions.append((handle, selection, reviewer, comment))
        return _run_view(
            platform="salesforce",
            status="completed" if selection == "approve" else "rejected",
            candidate=selection == "approve",
        )

    def candidate_zip(self, handle: str) -> bytes:
        return b"PK\x03\x04fake-candidate"

    def export_candidate(self, handle: str) -> dict[str, object]:
        self.exports.append(handle)
        return {
            "schema_version": "1.0",
            "platform": "salesforce",
            "handle": handle,
            "attempt": 1,
            "export_root": f"output/salesforce-{handle}/attempt-1",
            "candidate_path": f"output/salesforce-{handle}/attempt-1/candidate",
            "archive_path": f"output/salesforce-{handle}/attempt-1/candidate.zip",
            "receipt_path": f"output/salesforce-{handle}/attempt-1/export.json",
            "file_count": 1,
            "candidate_digest": "sha256:" + "c" * 64,
            "archive_sha256": "sha256:" + "d" * 64,
            "validation_disposition": "ready_for_human_review",
            "ready_for_human_review": True,
        }

    def retry(
        self,
        handle: str,
        *,
        correction_id: str,
        reviewer: str,
        comment: str,
    ) -> dict[str, object]:
        self.retries.append((handle, correction_id, reviewer, comment))
        view = _run_view(platform="salesforce", status="completed", candidate=True)
        view["execution_attempt"] = 2
        return view


def _conversation_view(
    *,
    platform: str | None,
    ready: bool,
    launched: bool,
    user_message: str = "Migrate the bounded legacy fixture.",
    launch_token: str | None = None,
) -> dict[str, object]:
    refined_request = (
        "Migrate the bounded legacy fixture additively and validate it locally." if ready else None
    )
    return {
        "schema_version": "1.0",
        "conversation_id": "feedfacefeedfacefeedface",
        "status": "launched" if launched else "ready" if ready else "open",
        "selected_platform": platform,
        "messages": (
            [
                {"sequence": 1, "role": "user", "content": user_message},
                {
                    "sequence": 2,
                    "role": "architect",
                    "content": (
                        "The request is ready for an explicit plan launch."
                        if ready
                        else "Choose a migration slice so I can refine the request."
                    ),
                },
            ]
            if user_message
            else []
        ),
        "readiness": {
            "ready": ready,
            "platform": platform,
            "refined_request": refined_request,
            "missing_information": [] if ready else ["Select a migration slice."],
            "launch_token": launch_token,
        },
        "model_calls": (
            [
                {
                    "exchange": 1,
                    "role": "architect",
                    "agent_version": "architect/v3",
                    "latency_ms": 7,
                    "input_tokens": 40,
                    "output_tokens": 20,
                    "total_tokens": 60,
                    "input_digest": "sha256:" + "a" * 64,
                    "output_digest": "sha256:" + "b" * 64,
                }
            ]
            if user_message
            else []
        ),
        "launch_handle": "abc123def456abc123def456" if launched else None,
    }


def _fake_launch_token(exchange: int) -> str:
    return "sha256:" + f"{exchange:064x}"


def _run_view(*, platform: str, status: str, candidate: bool) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "handle": "abc123def456abc123def456",
        "platform": platform,
        "scenario_title": "Fixture migration",
        "prompt": "Migrate this bounded synthetic fixture.",
        "status": status,
        "terminal_disposition": (
            "environment_unavailable"
            if candidate
            else "plan_invalid"
            if status == "rejected"
            else None
        ),
        "execution_attempt": 1,
        "boundaries": {
            "provider_attempted": True,
            "provider_invoked": True,
            "model_call_record_persisted": True,
            "structured_response_accepted": True,
            "provider_id": "ollama",
            "model_id": "qwen3.6:latest",
            "model_revision": "sha256:" + "a" * 64,
            "execution_boundary": "local_loopback",
            "external_platform_invoked": False,
            "source_mutated": False,
            "deployment_performed": False,
            "mode": "local_ollama",
            "notice": "Synthetic fixture only.",
        },
        "stages": [
            {
                "key": "architect",
                "label": "Architect",
                "state": "complete",
                "detail": "Bounded plan created.",
            },
            {
                "key": "approval",
                "label": "Approval gate",
                "state": "complete" if candidate else "active",
                "detail": "Human decision required." if not candidate else "Approved.",
            },
            {
                "key": "engineer",
                "label": "Engineer",
                "state": "complete" if candidate else "pending",
                "detail": "Candidate files created." if candidate else "Waiting for approval.",
            },
            {
                "key": "validator",
                "label": "Validator",
                "state": "complete" if candidate else "pending",
                "detail": "Local checks complete." if candidate else "Waiting for candidate.",
            },
        ],
        "metrics": {
            "graph_nodes": 4,
            "graph_edges": 3,
            "wiki_hits": 2,
            "changed_files": 1 if candidate else 0,
        },
        "model_calls": [
            {
                "role": role,
                "attempt": 1,
                "agent_version": f"{role}/v1",
                "latency_ms": 7,
                "input_tokens": 80,
                "output_tokens": 20,
                "total_tokens": 100,
                "input_digest": "sha256:" + character * 64,
                "output_digest": "sha256:" + character * 64,
            }
            for role, character in (
                (("architect", "c"), ("engineer", "d"), ("validator", "e"))
                if candidate
                else (("architect", "c"),)
            )
        ],
        "manifest": {
            "approval_required": True,
            "manifest_id": "manifest-1",
            "manifest_digest": "sha256:" + "b" * 64,
            "status": "planned",
            "approved_paths": ["force-app/main/default/lwc/accountExplorer/accountExplorer.js"],
            "public_decisions": [
                "Use additive LWC and Apex output files within the approved fixture boundary."
            ],
            "implementation_contract": [
                "Generate a sharing-aware Apex service and accessible LWC behavior."
            ],
            "cited_graph_nodes": ["visualforce:account-explorer"],
            "cited_wiki_pages": ["lwc-migration"],
            "unresolved_questions": [],
            "transformations": [
                {
                    "step_id": "create-lwc-controller",
                    "description": "Create the bounded LWC JavaScript controller.",
                    "input_paths": ["force-app/main/default/pages/AccountExplorer.page"],
                    "output_paths": [
                        "force-app/main/default/lwc/accountExplorer/accountExplorer.js"
                    ],
                }
            ],
            "validation_commands": [
                {
                    "check_id": "salesforce-contract",
                    "command_id": "validate-salesforce-fixture",
                    "purpose": "Run deterministic local contract checks.",
                    "environment": "local",
                    "required": True,
                }
            ],
            "risks": [
                {
                    "category": "behavioral_parity",
                    "summary": "External Salesforce behavior still requires human validation.",
                    "evidence": ["knowledge/wiki/lwc-migration.md"],
                    "requires_human_decision": False,
                }
            ],
            "required_approvals": ["approve_manifest"],
        },
        "evidence": {
            "wiki_hits": [
                {
                    "page_id": "lwc-migration",
                    "title": "LWC migration guide",
                    "path": "knowledge/wiki/lwc-migration.md",
                }
            ],
            "graph_summary": "4 nodes and 3 dependency edges were resolved.",
        },
        "candidate": (
            {
                "available": True,
                "attempt": 1,
                "download_available": True,
                "changed_paths": ["force-app/main/default/lwc/accountExplorer/accountExplorer.js"],
                "unified_diff": (
                    "diff --git a/force-app/main/default/lwc/accountExplorer/"
                    "accountExplorer.js b/force-app/main/default/lwc/accountExplorer/"
                    "accountExplorer.js\n"
                    "new file mode 100644\n"
                    "--- /dev/null\n"
                    "+++ b/force-app/main/default/lwc/accountExplorer/accountExplorer.js\n"
                    "@@ -0,0 +1,5 @@\n"
                    "+import { LightningElement } from 'lwc';\n"
                    "+\n"
                    "+export default class AccountExplorer extends LightningElement {\n"
                    "+    records = [];\n"
                    "+}\n"
                ),
                "files": [
                    {
                        "path": ("force-app/main/default/lwc/accountExplorer/accountExplorer.js"),
                        "content": (
                            "import { LightningElement } from 'lwc';\n\n"
                            "export default class AccountExplorer extends LightningElement {\n"
                            "    records = [];\n"
                            "}\n"
                        ),
                    }
                ],
                "assumptions": ["The fixture Apex contract remains stable."],
            }
            if candidate
            else None
        ),
        "validation": (
            {
                "disposition": "environment_unavailable",
                "attempt": 1,
                "results": [
                    {
                        "check_id": "local-contract",
                        "status": "passed",
                        "summary": "Local contract checks passed.",
                        "diagnostic_ids": [],
                    }
                ],
                "validator_summary": "External platform validation remains required.",
                "advisory_assessment": "raises_concern",
                "validator_completed": True,
                "concerns": ["Salesforce org validation has not run."],
                "deterministic_report_controls_disposition": True,
                "final_review_enabled": False,
            }
            if candidate
            else None
        ),
        "human_decision": (
            {
                "selection": "approve" if candidate else "reject",
                "reviewer": "course-reviewer",
                "comment": "Approved the bounded manifest." if candidate else "",
                "manifest_digest": "sha256:" + "b" * 64,
            }
            if status in {"completed", "rejected"}
            else None
        ),
        "correction": None,
        "attempt_history": [],
    }


@pytest.fixture
def ui_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[server_module.ThreadingHTTPServer]:
    monkeypatch.setattr(server_module, "AgentUiService", _FakeAgentUiService)
    server = build_ui_server(tmp_path, port=0, ollama_model_id="qwen3.6:latest")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    server: server_module.ThreadingHTTPServer,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    port = server.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=body, headers=dict(headers or {}))
    response = connection.getresponse()
    payload = response.read()
    response_headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, payload


def _json_request(
    server: server_module.ThreadingHTTPServer,
    method: str,
    path: str,
    value: object,
    *,
    csrf: str | None = None,
    content_type: str = "application/json; charset=utf-8",
    extra_headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": content_type}
    if csrf is not None:
        headers["X-Agent-UI-CSRF"] = csrf
    if extra_headers:
        headers.update(extra_headers)
    return _request(
        server,
        method,
        path,
        body=json.dumps(value).encode(),
        headers=headers,
    )


def _payload(body: bytes) -> Any:
    return json.loads(body)


def _csrf(server: server_module.ThreadingHTTPServer) -> str:
    status, _, body = _request(server, "GET", "/api/config")
    assert status == HTTPStatus.OK
    return str(_payload(body)["csrf_token"])


def test_serves_frontend_assets_config_and_scenarios(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    assert ui_server.server_address[0] == "127.0.0.1"
    assert ui_server.address_family == socket.AF_INET

    status, headers, body = _request(ui_server, "GET", "/")
    assert status == HTTPStatus.OK
    assert headers["content-type"] == "text/html; charset=utf-8"
    page = body.decode()
    assert "Ask the agent to migrate a bounded legacy system." in page
    assert "Migration conversation" in page
    assert 'id="new-chat-button"' in page
    assert 'id="export-button"' in page
    assert "Save candidate to output/" in page
    assert "optional examples, not the only messages you" in page
    assert "Model prose is advisory" in page
    assert "Loading local Ollama" in page
    assert "No org/runtime/deployment" in page
    assert "server-owned Ollama configuration" in page
    assert "provider-select" not in page
    assert '<script src="/assets/app.js" defer></script>' in page
    assert "<style" not in page

    status, _, script = _request(ui_server, "GET", "/assets/app.js")
    assert status == HTTPStatus.OK
    assert b"X-Agent-UI-CSRF" in script
    assert b"candidate.zip" in script
    assert b"/export`" in script
    assert b'messageArticle("You", [run.prompt])' in script
    assert b'elements.newChatButton.addEventListener("click", () => startNewChat())' in script
    assert b'api("/api/conversations"' in script
    assert b"/messages`" in script
    assert b"/launch`" in script
    assert b"currentTextIsExample" in script
    assert b"Local Ollama inference" in script
    assert b"Local model attempt:" in script
    assert b"provider_id: state.selectedProviderId" not in script
    assert b"selectedProviderId" not in script
    assert b"message," in script
    assert b"platform: state.selectedPlatform || null" in script
    assert b"Engineer and Validator were not invoked" in script
    assert b'approve.addEventListener("click", () => submitDecision("approve"))' in script
    assert b"api(`/api/sessions/${state.run.handle}/retry`" in script
    assert b"correction_id: state.run.correction.correction_id" in script
    assert 'role === "Engineer correction → Validator"'.encode() in script
    assert b"Typed diagnostics:" in script
    assert b'if (selection !== "approve" && selection !== "reject")' in script
    assert b"elements.decisionForm.reportValidity()" in script
    assert b"candidate.download_available !== false" in script
    assert b'elements.exportButton.addEventListener("click", exportCandidate)' in script
    assert b"Candidate-only export; validation disposition:" in script
    assert b"updateModelPresentation();" in script
    assert "Local Ollama · awaiting Architect reply".encode() in script
    assert "Local Ollama · awaiting Architect plan".encode() in script
    assert b'setRunStatus("architect responding")' in script
    assert b'setRunStatus("architect planning")' in script
    assert b'"engineer and validator working"' in script
    assert b'"recording decision"' in script
    assert (
        b"Migration candidate needs correction before external validation or deployment." in script
    )
    assert b"Failed check:" in script
    assert b"validation.validator_completed !== false" in script
    assert b'run.status === "rejected"' in script
    assert b'run.status === "completed" ? "Approved manifest paths"' in script
    assert b"Digest-bound plan under review" in script
    assert b"Public Architect decisions" in script
    assert b"Controller-owned implementation contract" in script
    assert b"Transformations" in script
    assert b"Validation commands" in script
    assert b"Required approvals" in script
    assert b"Public assumption:" in script
    assert b"Advisory concern:" in script
    assert b"Deterministic disposition (authoritative)" in script
    assert b"Full observed model revision" in script
    assert script.index(b"elements.conversation.append(decision)") < script.index(
        b"elements.conversation.append(interventionMessage(run))"
    )
    assert b"prefers-reduced-motion: reduce" in script
    assert b"textContent" in script

    status, _, stylesheet = _request(ui_server, "GET", "/assets/styles.css")
    assert status == HTTPStatus.OK
    assert b".chat-message" in stylesheet
    assert b"grid-template-columns: minmax(0, 1.35fr) minmax(360px, 0.75fr)" in stylesheet
    assert b"min-height: 440px" in stylesheet
    assert b"max-height: min(70vh, 880px)" in stylesheet
    assert b"@media (max-width: 1040px)" in stylesheet
    assert b".suggestion-button" in stylesheet
    assert b".manifest-review" in stylesheet
    assert b".concern-list" in stylesheet

    status, _, config = _request(ui_server, "GET", "/api/config")
    assert status == HTTPStatus.OK
    assert _payload(config)["transport"] == "loopback_only"
    assert _payload(config)["model"] == {
        "provider": "ollama",
        "model_id": "qwen3.6:latest",
        "execution_boundary": "local_loopback",
    }
    assert "providers" not in _payload(config)
    assert "default_provider_id" not in _payload(config)
    assert len(_payload(config)["csrf_token"]) >= 32

    status, _, scenarios = _request(ui_server, "GET", "/api/scenarios")
    assert status == HTTPStatus.OK
    assert [item["platform"] for item in _payload(scenarios)["scenarios"]] == [
        "salesforce",
        "mulesoft",
    ]

    status, _, readiness = _request(ui_server, "GET", "/api/readiness")
    assert status == HTTPStatus.OK
    assert _payload(readiness) == {
        "provider": "ollama",
        "model_id": "qwen3.6:latest",
        "configured": True,
        "ollama_reachable": True,
        "model_installed": True,
        "status": "ready",
    }
    assert "endpoint" not in _payload(readiness)
    assert "error" not in _payload(readiness)


def test_frontend_query_selector_targets_exist_exactly_once() -> None:
    static_root = Path(server_module.__file__).resolve().parent / "static"
    page = (static_root / "index.html").read_text(encoding="utf-8")
    script = (static_root / "app.js").read_text(encoding="utf-8")
    selector_ids = set(re.findall(r'document\.querySelector\("#([A-Za-z0-9_-]+)', script))
    collector = _IdCollector()
    collector.feed(page)

    assert selector_ids
    assert {
        identifier for identifier in selector_ids if collector.counts.get(identifier) != 1
    } == set()
    assert {identifier for identifier, count in collector.counts.items() if count != 1} == set()


def test_frontend_preserves_retry_context_for_attempt_two_interventions() -> None:
    script_path = Path(server_module.__file__).resolve().parent / "static/app.js"
    script = script_path.read_text(encoding="utf-8")
    start = script.index("} else if (run.intervention) {")
    end = script.index("} else if (run.failure) {", start)
    intervention_branch = script[start:end]

    assert intervention_branch.index("priorAttemptMessage(run)") < intervention_branch.index(
        "interventionMessage(run)"
    )
    assert intervention_branch.index("correctionDecisionMessage(run)") < intervention_branch.index(
        "interventionMessage(run)"
    )


def test_frontend_promises_retry_only_when_controller_offers_it() -> None:
    script_path = Path(server_module.__file__).resolve().parent / "static/app.js"
    script = script_path.read_text(encoding="utf-8")
    start = script.index("function completionMessage(run)")
    end = script.index("function rejectionMessage()", start)
    completion = script[start:end]

    retry_copy = completion.index("One bounded corrective attempt is available")
    retry_guard = completion.index("if (run.correction?.retry_available)")
    no_retry_copy = completion.index("No retry gate was opened")
    assert retry_guard < retry_copy < no_retry_copy


def test_frontend_restores_only_a_valid_persistent_run_handle() -> None:
    script_path = Path(server_module.__file__).resolve().parent / "static/app.js"
    script = script_path.read_text(encoding="utf-8")
    restore_block = script[script.index("async function restoreStoredRun()") :]

    assert "const RUN_HANDLE_PATTERN = /^[0-9a-f]{24}$/;" in script
    assert "window.localStorage.getItem(RUN_HANDLE_STORAGE_KEY)" in script
    assert "window.localStorage.setItem(RUN_HANDLE_STORAGE_KEY, handle)" in script
    assert "window.localStorage.removeItem(RUN_HANDLE_STORAGE_KEY)" in script
    assert "NEW_CONVERSATION_STORAGE_KEY" in script
    assert "window.localStorage.removeItem(NEW_CONVERSATION_STORAGE_KEY)" in script
    assert "if (!handle && newConversationRequested())" in restore_block
    assert 'const response = await api("/api/sessions/latest");' in restore_block
    assert restore_block.index("newConversationRequested()") < restore_block.index(
        'api("/api/sessions/latest")'
    )
    assert "if (!validRunHandle(handle))" in script
    assert "if (!run || !rememberRunHandle(run.handle))" in script
    assert "const response = await api(`/api/sessions/${handle}`);" in restore_block
    assert "run.handle !== handle" in restore_block
    assert "const definitiveLoss" in restore_block
    assert '"run_unavailable"' in restore_block
    stored_restore = restore_block[
        restore_block.index("const response = await api(`/api/sessions/${handle}`);") :
    ]
    assert stored_restore.index("clearStoredRunHandle();") < stored_restore.index("showAlert(")
    assert "Its handle was preserved; reload to try again." in restore_block
    assert "await restoreStoredRun();" in restore_block

    new_chat_block = script[
        script.index('async function startNewChat(initialDraft = "")') : script.index(
            "function renderFailureDiagnostic",
            script.index('async function startNewChat(initialDraft = "")'),
        )
    ]
    assert new_chat_block.index("markNewConversationRequested();") < new_chat_block.index(
        "state.run = null;"
    )
    assert new_chat_block.index("createConversation(null)") < new_chat_block.index(
        "state.run = null;"
    )
    assert 'method: "DELETE"' not in new_chat_block


def test_latest_session_route_recovers_a_verifiable_run(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    status, _, body = _request(ui_server, "GET", "/api/sessions/latest")

    assert status == HTTPStatus.OK
    recovered = _payload(body)["run"]
    assert recovered["handle"] == "abc123def456abc123def456"
    assert recovered["status"] == "awaiting_approval"


def test_conversation_routes_separate_messages_from_explicit_launch(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    csrf = _csrf(ui_server)
    status, _, created_body = _json_request(
        ui_server,
        "POST",
        "/api/conversations",
        {"platform": None},
        csrf=csrf,
    )
    assert status == HTTPStatus.CREATED
    created = _payload(created_body)
    conversation_id = created["conversation_id"]
    assert created["status"] == "open"
    assert created["readiness"]["ready"] is False

    status, _, message_body = _json_request(
        ui_server,
        "POST",
        f"/api/conversations/{conversation_id}/messages",
        {
            "message": "Use the Salesforce Visualforce to LWC slice.",
            "platform": "salesforce",
        },
        csrf=csrf,
    )
    assert status == HTTPStatus.OK
    conversation = _payload(message_body)
    assert conversation["status"] == "ready"
    assert conversation["selected_platform"] == "salesforce"
    assert conversation["messages"][-1]["role"] == "architect"
    assert conversation["model_calls"][0]["agent_version"] == "architect/v3"
    tab_a_token = conversation["readiness"]["launch_token"]
    assert isinstance(tab_a_token, str)
    assert ui_server.ui_service.conversation_launched is False

    status, _, newer_body = _json_request(
        ui_server,
        "POST",
        f"/api/conversations/{conversation_id}/messages",
        {
            "message": "This is a newer clarification from another browser tab.",
            "platform": "salesforce",
        },
        csrf=csrf,
    )
    assert status == HTTPStatus.OK
    tab_b_token = _payload(newer_body)["readiness"]["launch_token"]
    assert isinstance(tab_b_token, str)
    assert tab_b_token != tab_a_token

    status, _, stale_body = _json_request(
        ui_server,
        "POST",
        f"/api/conversations/{conversation_id}/launch",
        {"launch_token": tab_a_token},
        csrf=csrf,
    )
    assert status == HTTPStatus.BAD_REQUEST
    assert _payload(stale_body)["error"]["code"] == "stale_conversation"
    assert ui_server.ui_service.conversation_launched is False

    status, _, read_body = _request(
        ui_server,
        "GET",
        f"/api/conversations/{conversation_id}",
    )
    assert status == HTTPStatus.OK
    assert _payload(read_body)["readiness"]["ready"] is True
    assert _payload(read_body)["readiness"]["launch_token"] == tab_b_token

    status, _, launch_body = _json_request(
        ui_server,
        "POST",
        f"/api/conversations/{conversation_id}/launch",
        {"launch_token": tab_b_token},
        csrf=csrf,
    )
    assert status == HTTPStatus.CREATED
    launched = _payload(launch_body)
    assert launched["conversation"]["status"] == "launched"
    assert launched["run"]["status"] == "awaiting_approval"
    assert ui_server.ui_service.conversation_launched is True

    status, _, rejected = _json_request(
        ui_server,
        "POST",
        f"/api/conversations/{conversation_id}/messages",
        {
            "message": "Do not accept browser-selected provider or authority fields.",
            "platform": "salesforce",
            "approved": True,
        },
        csrf=csrf,
    )
    assert status == HTTPStatus.BAD_REQUEST
    assert _payload(rejected)["error"]["code"] == "bad_request"


def test_disconnect_during_response_does_not_attempt_a_second_error_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = object.__new__(server_module._AgentUiRequestHandler)
    handler.path = "/api/config"
    handler.close_connection = False
    error_responses: list[tuple[HTTPStatus, str]] = []
    monkeypatch.setattr(handler, "_validate_request_context", lambda: None)

    def disconnect(_path: str) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(handler, "_get", disconnect)
    monkeypatch.setattr(
        handler,
        "_respond_error",
        lambda status, code: error_responses.append((status, code)),
    )

    handler._dispatch("GET")

    assert handler.close_connection is True
    assert error_responses == []


def test_session_decision_readback_and_csrf_protected_candidate_download(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    csrf = _csrf(ui_server)
    status, _, created = _json_request(
        ui_server,
        "POST",
        "/api/sessions",
        {
            "platform": "salesforce",
            "prompt": "Migrate this bounded synthetic fixture.",
        },
        csrf=csrf,
    )
    assert status == HTTPStatus.CREATED
    assert _payload(created)["status"] == "awaiting_approval"
    manifest = _payload(created)["manifest"]
    assert manifest["manifest_digest"] == "sha256:" + "b" * 64
    assert manifest["public_decisions"]
    assert manifest["transformations"][0]["step_id"] == "create-lwc-controller"
    assert manifest["validation_commands"][0]["required"] is True
    assert manifest["risks"][0]["category"] == "behavioral_parity"
    assert manifest["required_approvals"] == ["approve_manifest"]

    handle = _payload(created)["handle"]
    status, _, readback = _request(ui_server, "GET", f"/api/sessions/{handle}")
    assert status == HTTPStatus.OK
    assert _payload(readback)["handle"] == handle

    status, _, decided = _json_request(
        ui_server,
        "POST",
        f"/api/sessions/{handle}/decision",
        {
            "selection": "approve",
            "reviewer": "course-reviewer",
            "comment": "Bounded candidate approved.",
        },
        csrf=csrf,
    )
    assert status == HTTPStatus.OK
    assert _payload(decided)["candidate"]["available"] is True
    assert _payload(decided)["candidate"]["assumptions"] == [
        "The fixture Apex contract remains stable."
    ]
    assert _payload(decided)["validation"]["concerns"] == ["Salesforce org validation has not run."]
    assert _payload(decided)["validation"]["deterministic_report_controls_disposition"] is True

    status, _, rejected = _request(
        ui_server,
        "GET",
        f"/api/sessions/{handle}/candidate.zip",
    )
    assert status == HTTPStatus.FORBIDDEN
    assert _payload(rejected)["error"]["code"] == "csrf_required"

    status, headers, archive = _request(
        ui_server,
        "GET",
        f"/api/sessions/{handle}/candidate.zip",
        headers={"X-Agent-UI-CSRF": csrf},
    )
    assert status == HTTPStatus.OK
    assert headers["content-type"] == "application/zip"
    assert headers["content-disposition"].endswith(f'{handle}.zip"')
    assert archive.startswith(b"PK\x03\x04")

    status, _, exported = _json_request(
        ui_server,
        "POST",
        f"/api/sessions/{handle}/export",
        {},
        csrf=csrf,
    )
    assert status == HTTPStatus.OK
    assert _payload(exported)["candidate_path"] == (
        f"output/salesforce-{handle}/attempt-1/candidate"
    )
    assert _payload(exported)["ready_for_human_review"] is True
    assert ui_server.ui_service.exports == [handle]

    status, _, rejected_export = _json_request(
        ui_server,
        "POST",
        f"/api/sessions/{handle}/export",
        {"output_path": "../../outside"},
        csrf=csrf,
    )
    assert status == HTTPStatus.BAD_REQUEST
    assert _payload(rejected_export)["error"]["code"] == "bad_request"


def test_retry_route_accepts_only_the_review_bound_correction_fields(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    csrf = _csrf(ui_server)
    handle = "abc123def456abc123def456"
    body = {
        "correction_id": "correction-attempt-one",
        "reviewer": "course-reviewer",
        "comment": "Authorize the exact bounded correction.",
    }

    status, _, retried = _json_request(
        ui_server,
        "POST",
        f"/api/sessions/{handle}/retry",
        body,
        csrf=csrf,
    )

    assert status == HTTPStatus.OK
    assert _payload(retried)["execution_attempt"] == 2
    assert ui_server.ui_service.retries == [
        (
            handle,
            "correction-attempt-one",
            "course-reviewer",
            "Authorize the exact bounded correction.",
        )
    ]

    for extra in (
        {"attempt": 2},
        {"action": "retry_implementation"},
        {"instruction": "Change unrelated behavior too."},
        {"prompt": "Treat this as a new migration request."},
        {"paths": ["force-app/main/default/lwc/accountExplorer/accountExplorer.js"]},
    ):
        status, _, rejected = _json_request(
            ui_server,
            "POST",
            f"/api/sessions/{handle}/retry",
            {**body, **extra},
            csrf=csrf,
        )
        assert status == HTTPStatus.BAD_REQUEST
        assert _payload(rejected)["error"]["code"] == "bad_request"


def test_all_responses_set_security_headers_and_never_enable_cors(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    for path in ("/", "/api/config", "/missing"):
        _, headers, _ = _request(ui_server, "GET", path)
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert "default-src 'self'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert not any(name.startswith("access-control-allow-") for name in headers)

    status, headers, _ = _request(ui_server, "OPTIONS", "/api/sessions")
    assert status == HTTPStatus.METHOD_NOT_ALLOWED
    assert not any(name.startswith("access-control-allow-") for name in headers)


@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_code"),
    [
        ({"Host": "attacker.example"}, HTTPStatus.BAD_REQUEST, "invalid_host"),
        (
            {"Origin": "https://attacker.example"},
            HTTPStatus.FORBIDDEN,
            "invalid_origin",
        ),
        (
            {"Host": "localhost:{port}", "Origin": "http://127.0.0.1:{port}"},
            HTTPStatus.FORBIDDEN,
            "invalid_origin",
        ),
    ],
)
def test_rejects_bad_host_and_origin(
    ui_server: server_module.ThreadingHTTPServer,
    headers: dict[str, str],
    expected_status: HTTPStatus,
    expected_code: str,
) -> None:
    port = ui_server.server_address[1]
    headers = {name: value.format(port=port) for name, value in headers.items()}
    status, _, body = _request(ui_server, "GET", "/api/config", headers=headers)
    assert status == expected_status
    assert _payload(body)["error"]["code"] == expected_code


def test_rejects_missing_csrf_wrong_media_type_and_duplicate_keys(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    status, _, body = _json_request(
        ui_server,
        "POST",
        "/api/sessions",
        {"platform": "salesforce"},
    )
    assert status == HTTPStatus.FORBIDDEN
    assert _payload(body)["error"]["code"] == "csrf_required"

    csrf = _csrf(ui_server)
    status, _, body = _json_request(
        ui_server,
        "POST",
        "/api/sessions",
        {"platform": "salesforce"},
        csrf=csrf,
        content_type="text/plain",
    )
    assert status == HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    assert _payload(body)["error"]["code"] == "invalid_content_type"

    status, _, body = _request(
        ui_server,
        "POST",
        "/api/sessions",
        body=b'{"platform":"salesforce","platform":"mulesoft"}',
        headers={
            "Content-Type": "application/json",
            "X-Agent-UI-CSRF": csrf,
        },
    )
    assert status == HTTPStatus.BAD_REQUEST
    assert _payload(body)["error"]["code"] == "duplicate_json_key"


def test_rejects_oversize_body_unframed_body_and_transfer_encoding(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    csrf = _csrf(ui_server)
    status, _, body = _request(
        ui_server,
        "POST",
        "/api/sessions",
        body=b"x" * (16 * 1024 + 1),
        headers={
            "Content-Type": "application/json",
            "X-Agent-UI-CSRF": csrf,
        },
    )
    assert status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert _payload(body)["error"]["code"] == "payload_too_large"

    port = ui_server.server_address[1]
    no_length = _raw_request(
        port,
        (
            "POST /api/sessions HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            f"X-Agent-UI-CSRF: {csrf}\r\n"
            "Connection: close\r\n\r\n"
            '{"platform":"salesforce"}'
        ).encode(),
    )
    assert no_length.startswith(b"HTTP/1.1 411")
    assert b'"code":"invalid_content_length"' in no_length

    chunked = _raw_request(
        port,
        (
            "POST /api/sessions HTTP/1.1\r\n"
            f"Host: localhost:{port}\r\n"
            "Content-Type: application/json\r\n"
            "Transfer-Encoding: chunked\r\n"
            f"X-Agent-UI-CSRF: {csrf}\r\n"
            "Connection: close\r\n\r\n"
            "2\r\n{}\r\n0\r\n\r\n"
        ).encode(),
    )
    assert chunked.startswith(b"HTTP/1.1 400")
    assert b'"code":"transfer_encoding_not_allowed"' in chunked


def _raw_request(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while chunk := connection.recv(8192):
            chunks.append(chunk)
    return b"".join(chunks)


def test_rejects_unlisted_routes_and_sanitizes_internal_failures(
    ui_server: server_module.ThreadingHTTPServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for path in (
        "/api/chat",
        "/api/files",
        "/api/commands/run",
        "/api/sessions/abc123def456abc123def456/messages",
        "/api/sessions/abc/../../etc/passwd",
        "/assets/../server.py",
        "/api/config?path=/tmp/private",
    ):
        status, _, body = _request(ui_server, "GET", path)
        assert status == HTTPStatus.NOT_FOUND
        assert _payload(body)["error"]["code"] == "not_found"

    with terminal_lifecycle_logging():
        status, _, body = _request(
            ui_server,
            "GET",
            "/api/sessions/deadbeefdeadbeefdeadbeef",
        )
    terminal_log = capsys.readouterr().err
    serialized = body.decode()
    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert _payload(body)["error"] == {
        "code": "internal_error",
        "message": "The UI request could not be completed.",
    }
    assert "secret" not in serialized
    assert "/Users/" not in serialized
    assert "event=ui.http.failed" in terminal_log
    assert 'action="session.read"' in terminal_log
    assert 'public_code="internal_error"' in terminal_log
    assert 'error_type="RuntimeError"' in terminal_log
    assert "secret" not in terminal_log
    assert "/Users/" not in terminal_log


def test_service_error_uses_its_complete_sanitized_message_contract(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    status, _, body = _request(
        ui_server,
        "GET",
        "/api/sessions/beadfeedbeadfeedbeadfeed",
    )

    assert status == HTTPStatus.BAD_REQUEST
    assert _payload(body)["error"] == {
        "code": "run_unavailable",
        "message": "The agent run could not be verified safely.",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", "http://attacker.example/api/chat"),
        ("api_key", "must-not-cross-the-browser-boundary"),
        ("command", "rm -rf /"),
        ("source_root", "/tmp/escape"),
        ("deploy", True),
        ("provider_id", "ollama"),
        ("ollama_timeout_seconds", 600),
        ("file_path", "private/source.cls"),
        ("raw_model_output", "hidden provider response"),
        ("messages", [{"role": "user", "content": "remember this"}]),
        ("history", ["prior run"]),
        ("previous_handle", "abc123def456abc123def456"),
        ("parent_handle", "abc123def456abc123def456"),
        ("thinking", True),
        ("debug", True),
    ],
)
def test_rejects_extra_request_fields(
    ui_server: server_module.ThreadingHTTPServer,
    field: str,
    value: object,
) -> None:
    status, _, body = _json_request(
        ui_server,
        "POST",
        "/api/sessions",
        {
            "platform": "salesforce",
            "prompt": "Migrate this bounded synthetic fixture.",
            field: value,
        },
        csrf=_csrf(ui_server),
    )
    assert status == HTTPStatus.BAD_REQUEST
    assert _payload(body)["error"]["code"] == "bad_request"


def test_serve_ui_prints_the_loopback_url_and_honors_open_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _FakeServer:
        server_address = ("127.0.0.1", 9137)

        def __init__(self) -> None:
            self.served = False
            self.closed = False

        def serve_forever(self) -> None:
            self.served = True
            raise KeyboardInterrupt

        def server_close(self) -> None:
            self.closed = True

    fake_server = _FakeServer()
    opened: list[str] = []
    captured: list[tuple[Path, int, str, float]] = []

    def fake_build_ui_server(
        project_root: Path,
        *,
        port: int,
        ollama_model_id: str,
        ollama_timeout_seconds: float,
    ) -> _FakeServer:
        captured.append((project_root, port, ollama_model_id, ollama_timeout_seconds))
        return fake_server

    monkeypatch.setattr(
        server_module,
        "build_ui_server",
        fake_build_ui_server,
    )
    monkeypatch.setattr(server_module.webbrowser, "open", lambda url: opened.append(url))

    server_module.serve_ui(
        tmp_path,
        port=9137,
        open_browser=True,
        ollama_model_id="qwen3.8:latest",
        ollama_timeout_seconds=600.0,
    )

    captured_output = capsys.readouterr()
    assert captured_output.out == (
        "Agent UI available at http://127.0.0.1:9137/\n\nAgent UI stopped.\n"
    )
    assert "event=ui.provider.configured" in captured_output.err
    assert 'provider="ollama"' in captured_output.err
    assert 'model_id="qwen3.8:latest"' in captured_output.err
    assert "event=ui.server.ready" in captured_output.err
    assert "event=ui.server.stopped" in captured_output.err
    assert "connected" not in captured_output.err
    assert opened == ["http://127.0.0.1:9137/"]
    assert captured == [(tmp_path, 9137, "qwen3.8:latest", 600.0)]
    assert fake_server.served is True
    assert fake_server.closed is True
