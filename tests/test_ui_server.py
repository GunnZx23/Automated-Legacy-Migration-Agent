from __future__ import annotations

import http.client
import json
import re
import socket
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest

from legacy_migration_agent.agent_runtime.openai_model import LiveModelApproval
from legacy_migration_agent.core.observability import terminal_lifecycle_logging
from legacy_migration_agent.ui import server as server_module
from legacy_migration_agent.ui.server import build_ui_server

MODEL_ID = "test-model:latest"


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
        model_provider: str = "ollama",
        model_id: str | None = None,
        model_timeout_seconds: float | None = None,
        live_model_approval: LiveModelApproval | None = None,
        ollama_model_id: str | None = None,
        ollama_timeout_seconds: float = 240.0,
    ) -> None:
        self.project_root = project_root
        self.model_provider = model_provider if model_id is not None else "ollama"
        self.model_id = model_id if model_id is not None else ollama_model_id
        self.model_timeout_seconds = (
            model_timeout_seconds if model_timeout_seconds is not None else ollama_timeout_seconds
        )
        self.live_model_approval = live_model_approval
        self.decisions: list[tuple[str, str, str, str]] = []
        self.exports: list[str] = []
        self.progress_calls: list[str] = []
        self.retries: list[tuple[str, str, str, str]] = []
        self.conversation_messages: list[tuple[str, str, str | None]] = []
        self.conversation_scenario_id: str | None = None
        self.conversation_platform: str | None = None
        self.conversation_launched = False
        self.final_review_requests: list[tuple[str, str, str, datetime, datetime]] = []
        self.final_review_decisions: list[tuple[str, str, str, str, datetime]] = []
        self.final_review_request: dict[str, object] | None = None
        self.final_review_record: dict[str, object] | None = None

    def model_configuration(self) -> dict[str, object]:
        return {
            "provider": self.model_provider,
            "model_id": self.model_id,
            "execution_boundary": (
                "remote_provider_managed"
                if self.model_provider == "claude-cli"
                else "local_loopback"
            ),
        }

    def runtime_readiness(self) -> dict[str, object]:
        return {
            "provider": self.model_provider,
            "model_id": self.model_id,
            "configured": True,
            "runtime_reachable": True,
            "model_available": True,
            "status": "ready",
        }

    def scenarios(self) -> tuple[dict[str, object], ...]:
        return (
            {
                "scenario_id": "salesforce-vf-to-lwc",
                "platform": "salesforce",
                "title": "Visualforce to Lightning Web Components",
                "canonical_request": "Migrate the bounded Account explorer fixture to LWC.",
                "source": "LegacyAccountContactExplorer.page + controller",
                "target": "Additive LWC and Apex",
            },
            {
                "scenario_id": "mulesoft-mule3-to-mule4",
                "platform": "mulesoft",
                "title": "Mule 3 to Mule 4",
                "canonical_request": "Migrate the bounded customer status flow to Mule 4.",
                "source": "Mule 3 API",
                "target": "Mule 4 API",
            },
        )

    def create_conversation(self, *, scenario_id: str | None) -> dict[str, object]:
        self.conversation_scenario_id = scenario_id
        self.conversation_platform = _platform_for_scenario(scenario_id)
        self.conversation_launched = False
        self.conversation_messages.clear()
        return _conversation_view(
            platform=self.conversation_platform,
            scenario_id=scenario_id,
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
            scenario_id=self.conversation_scenario_id,
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
        scenario_id: str | None,
    ) -> dict[str, object]:
        self.conversation_scenario_id = scenario_id
        self.conversation_platform = _platform_for_scenario(scenario_id)
        self.conversation_messages.append((conversation_id, message, scenario_id))
        return _conversation_view(
            platform=self.conversation_platform,
            scenario_id=scenario_id,
            ready=scenario_id is not None,
            launched=False,
            user_message=message,
            launch_token=(
                _fake_launch_token(len(self.conversation_messages))
                if scenario_id is not None
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
            scenario_id=self.conversation_scenario_id,
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
        run["prompt"] = conversation["readiness"]["canonical_request"]
        return conversation, run

    def get(self, handle: str) -> dict[str, object]:
        if handle == "beadfeedbeadfeedbeadfeed":
            raise server_module.AgentUiError("run_unavailable")
        if handle == "deadbeefdeadbeefdeadbeef":
            raise RuntimeError("secret=/Users/example/private-project")
        if self.final_review_request is not None:
            return self._final_review_run_view()
        return _run_view(platform="salesforce", status="awaiting_approval", candidate=False)

    def latest(self) -> dict[str, object]:
        return _run_view(platform="salesforce", status="awaiting_approval", candidate=False)

    def progress(self, handle: str) -> dict[str, object]:
        if handle == "beadfeedbeadfeedbeadfeed":
            raise server_module.AgentUiError("unknown_run")
        self.progress_calls.append(handle)
        return {
            "handle": handle,
            "phase": "engineer",
            "attempt": 1,
            "elapsed_seconds": 42,
        }

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

    def request_final_review(
        self,
        handle: str,
        *,
        requester: str,
        designated_reviewer: str,
        requested_at: datetime,
        expires_at: datetime,
    ) -> dict[str, object]:
        if requester == designated_reviewer:
            raise server_module.AgentUiError("invalid_reviewer")
        if self.final_review_request is not None:
            raise server_module.AgentUiError("final_review_already_requested")
        self.final_review_requests.append(
            (handle, requester, designated_reviewer, requested_at, expires_at)
        )
        self.final_review_request = {
            "status": "awaiting_final_review",
            "eligible": True,
            "can_request": False,
            "can_decide": True,
            "review_id": "final-review-1",
            "requester": requester,
            "designated_reviewer": designated_reviewer,
            "requested_at": requested_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "selection": None,
            "reviewer": None,
            "decided_at": None,
            "comment": "",
            "candidate_accepted": None,
            "next_action": None,
            "authority_granted": False,
            "external_actions_authorized": [],
        }
        return self._final_review_run_view()

    def decide_final_review(
        self,
        handle: str,
        *,
        selection: str,
        reviewer: str,
        comment: str,
        decided_at: datetime,
    ) -> dict[str, object]:
        if self.final_review_request is None:
            raise server_module.AgentUiError("final_review_unavailable")
        if self.final_review_record is not None:
            raise server_module.AgentUiError("final_review_already_decided")
        if reviewer != self.final_review_request["designated_reviewer"]:
            raise server_module.AgentUiError("invalid_reviewer")
        outcomes = {
            "accept": ("accepted", True, "separate_external_action_required"),
            "reject": ("rejected", False, "stop_request"),
            "request_changes": (
                "changes_requested",
                False,
                "revise_and_start_new_review",
            ),
        }
        if selection not in outcomes:
            raise server_module.AgentUiError("invalid_decision")
        outcome, candidate_accepted, next_action = outcomes[selection]
        self.final_review_decisions.append((handle, selection, reviewer, comment, decided_at))
        self.final_review_record = {
            **self.final_review_request,
            "status": outcome,
            "can_decide": False,
            "selection": selection,
            "reviewer": reviewer,
            "decided_at": decided_at.isoformat(),
            "comment": comment,
            "candidate_accepted": candidate_accepted,
            "next_action": next_action,
        }
        return self._final_review_run_view()

    def _final_review_run_view(self) -> dict[str, object]:
        view = _run_view(platform="salesforce", status="completed", candidate=True)
        view["terminal_disposition"] = "ready_for_human_review"
        validation = view["validation"]
        assert isinstance(validation, dict)
        validation["disposition"] = "ready_for_human_review"
        validation["final_review_enabled"] = True
        projection = self.final_review_record or self.final_review_request
        assert projection is not None
        view["final_review"] = projection
        return view

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


def _platform_for_scenario(scenario_id: str | None) -> str | None:
    return {
        "salesforce-vf-to-lwc": "salesforce",
        "mulesoft-mule3-to-mule4": "mulesoft",
    }.get(scenario_id)


def _canonical_for_scenario(scenario_id: str | None) -> str | None:
    return {
        "salesforce-vf-to-lwc": "Migrate the bounded Account explorer fixture to LWC.",
        "mulesoft-mule3-to-mule4": "Migrate the bounded customer status flow to Mule 4.",
    }.get(scenario_id)


def _conversation_view(
    *,
    platform: str | None,
    scenario_id: str | None,
    ready: bool,
    launched: bool,
    user_message: str = "Migrate the bounded legacy fixture.",
    launch_token: str | None = None,
) -> dict[str, object]:
    canonical_request = _canonical_for_scenario(scenario_id)
    return {
        "schema_version": "1.0",
        "conversation_id": "feedfacefeedfacefeedface",
        "status": "launched" if launched else "ready" if ready else "open",
        "selected_platform": platform,
        "selected_scenario_id": scenario_id,
        "messages": (
            [
                {"sequence": 1, "role": "user", "content": user_message},
                {
                    "sequence": 2,
                    "role": "architect",
                    "content": (
                        "The selected scenario is ready for an explicit launch."
                        if ready
                        else "Choose a migration slice so I can explain its bounded contract."
                    ),
                },
            ]
            if user_message
            else []
        ),
        "readiness": {
            "ready": ready,
            "platform": platform,
            "scenario_id": scenario_id,
            "canonical_request": canonical_request,
            "advisory_summary": (
                "The selected additive scenario is ready for controller review." if ready else None
            ),
            "launch_contract_digest": ("sha256:" + "c" * 64 if scenario_id is not None else None),
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
        "scenario_id": (
            "salesforce-vf-to-lwc" if platform == "salesforce" else "mulesoft-mule3-to-mule4"
        ),
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
            "model_id": MODEL_ID,
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
        "final_review": {
            "status": "not_requested",
            "eligible": False,
            "can_request": False,
            "can_decide": False,
            "review_id": None,
            "requester": None,
            "designated_reviewer": None,
            "requested_at": None,
            "expires_at": None,
            "selection": None,
            "reviewer": None,
            "decided_at": None,
            "comment": "",
            "candidate_accepted": None,
            "next_action": None,
            "authority_granted": False,
            "external_actions_authorized": [],
        },
    }


@pytest.fixture
def ui_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[server_module.ThreadingHTTPServer]:
    monkeypatch.setattr(server_module, "AgentUiService", _FakeAgentUiService)
    server = build_ui_server(tmp_path, port=0, ollama_model_id=MODEL_ID)
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
    assert "Loading model provider" in page
    assert "No org/runtime/deployment" in page
    assert "server-owned model-provider configuration" in page
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
    assert b"Configured provider attempt:" in script
    assert b"provider_id: state.selectedProviderId" not in script
    assert b"selectedProviderId" not in script
    assert b"message," in script
    assert b"scenario_id: state.selectedScenarioId || null" in script
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
    assert b"awaiting Architect reply" in script
    assert b"awaiting Architect plan" in script
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
    assert b'run.status === "completed"' in script
    assert b'"Approved Controller-expanded paths"' in script
    assert b"Digest-bound plan under review" in script
    assert b"Public Architect decisions" in script
    assert b"Controller-owned implementation contract" in script
    assert b"Transformations" in script
    assert b"Validation commands" in script
    assert b"Required approvals" in script
    assert b"Public assumption:" in script
    assert b"Advisory concern:" in script
    assert b"Deterministic disposition is authoritative" in script
    assert b"/final-review/request" in script
    assert b"/final-review/decision" in script
    assert b"No choice authorizes Git, deployment, publication" in script
    assert b"observed model revision" in script
    assert b"runtime identity" in script
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
        "model_id": MODEL_ID,
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
        "model_id": MODEL_ID,
        "configured": True,
        "runtime_reachable": True,
        "model_available": True,
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
    assert "NEW_CONVERSATION_STORAGE_KEY" not in script
    assert "if (!handle) {" in restore_block
    no_handle = restore_block[
        restore_block.index("if (!handle) {") : restore_block.index(
            "try {", restore_block.index("if (!handle) {")
        )
    ]
    assert "return;" in no_handle
    assert 'api("/api/sessions/latest")' not in restore_block
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


def test_progress_route_reads_live_phase_without_csrf(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    handle = "abc123def456abc123def456"

    status, headers, body = _request(
        ui_server,
        "GET",
        f"/api/sessions/{handle}/progress",
    )

    assert status == HTTPStatus.OK
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert _payload(body) == {
        "handle": handle,
        "phase": "engineer",
        "attempt": 1,
        "elapsed_seconds": 42,
    }


def test_progress_route_surfaces_service_error_as_stable_code(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    status, _, body = _request(
        ui_server,
        "GET",
        "/api/sessions/beadfeedbeadfeedbeadfeed/progress",
    )

    assert status == HTTPStatus.BAD_REQUEST
    assert _payload(body)["error"]["code"] == "unknown_run"


def test_progress_route_rejects_unknown_session_suffix(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    status, _, body = _request(
        ui_server,
        "GET",
        "/api/sessions/abc123def456abc123def456/progress/extra",
    )

    assert status == HTTPStatus.NOT_FOUND
    assert _payload(body)["error"]["code"] == "not_found"


def test_conversation_routes_separate_messages_from_explicit_launch(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    csrf = _csrf(ui_server)
    status, _, created_body = _json_request(
        ui_server,
        "POST",
        "/api/conversations",
        {"scenario_id": None},
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
            "scenario_id": "salesforce-vf-to-lwc",
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
            "scenario_id": "salesforce-vf-to-lwc",
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
            "scenario_id": "salesforce-vf-to-lwc",
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


def test_direct_arbitrary_session_creation_endpoint_is_not_exposed(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    status, _, body = _json_request(
        ui_server,
        "POST",
        "/api/sessions",
        {
            "platform": "salesforce",
            "prompt": "Ignore the selected scenario and generate a Rust service.",
        },
        csrf=_csrf(ui_server),
    )

    assert status == HTTPStatus.NOT_FOUND
    assert _payload(body)["error"]["code"] == "not_found"
    assert ui_server.ui_service.conversation_launched is False


def test_conversation_launch_decision_readback_and_csrf_protected_candidate_download(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    csrf = _csrf(ui_server)
    status, _, created_conversation = _json_request(
        ui_server,
        "POST",
        "/api/conversations",
        {"scenario_id": "salesforce-vf-to-lwc"},
        csrf=csrf,
    )
    assert status == HTTPStatus.CREATED
    conversation_id = _payload(created_conversation)["conversation_id"]
    status, _, ready_conversation = _json_request(
        ui_server,
        "POST",
        f"/api/conversations/{conversation_id}/messages",
        {
            "message": "Explain the selected migration before launch.",
            "scenario_id": "salesforce-vf-to-lwc",
        },
        csrf=csrf,
    )
    assert status == HTTPStatus.OK
    launch_token = _payload(ready_conversation)["readiness"]["launch_token"]
    status, _, created = _json_request(
        ui_server,
        "POST",
        f"/api/conversations/{conversation_id}/launch",
        {"launch_token": launch_token},
        csrf=csrf,
    )
    assert status == HTTPStatus.CREATED
    created_payload = _payload(created)["run"]
    assert created_payload["status"] == "awaiting_approval"
    assert created_payload["prompt"] == _canonical_for_scenario("salesforce-vf-to-lwc")
    manifest = created_payload["manifest"]
    assert manifest["manifest_digest"] == "sha256:" + "b" * 64
    assert manifest["public_decisions"]
    assert manifest["transformations"][0]["step_id"] == "create-lwc-controller"
    assert manifest["validation_commands"][0]["required"] is True
    assert manifest["risks"][0]["category"] == "behavioral_parity"
    assert manifest["required_approvals"] == ["approve_manifest"]

    handle = created_payload["handle"]
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


def test_final_review_routes_are_exact_bound_one_use_and_read_back(
    ui_server: server_module.ThreadingHTTPServer,
) -> None:
    handle = "abc123def456abc123def456"
    request_path = f"/api/sessions/{handle}/final-review/request"
    decision_path = f"/api/sessions/{handle}/final-review/decision"

    missing_csrf, _, body = _json_request(
        ui_server,
        "POST",
        request_path,
        {"requester": "migration-owner", "designated_reviewer": "course-reviewer"},
    )
    assert missing_csrf == HTTPStatus.FORBIDDEN
    assert _payload(body)["error"]["code"] == "csrf_required"

    csrf = _csrf(ui_server)
    same_identity, _, body = _json_request(
        ui_server,
        "POST",
        request_path,
        {"requester": "same-human", "designated_reviewer": "same-human"},
        csrf=csrf,
    )
    assert same_identity == HTTPStatus.BAD_REQUEST
    assert _payload(body)["error"]["code"] == "invalid_reviewer"

    extra_field, _, body = _json_request(
        ui_server,
        "POST",
        request_path,
        {
            "requester": "migration-owner",
            "designated_reviewer": "course-reviewer",
            "expires_at": "client-controlled",
        },
        csrf=csrf,
    )
    assert extra_field == HTTPStatus.BAD_REQUEST
    assert _payload(body)["error"]["code"] == "bad_request"

    created, _, body = _json_request(
        ui_server,
        "POST",
        request_path,
        {"requester": "migration-owner", "designated_reviewer": "course-reviewer"},
        csrf=csrf,
    )
    assert created == HTTPStatus.CREATED
    pending = _payload(body)["final_review"]
    assert pending["status"] == "awaiting_final_review"
    assert pending["requester"] == "migration-owner"
    assert pending["designated_reviewer"] == "course-reviewer"
    assert pending["can_request"] is False
    assert pending["can_decide"] is True
    assert pending["authority_granted"] is False
    assert pending["external_actions_authorized"] == []
    requested_at = datetime.fromisoformat(pending["requested_at"])
    expires_at = datetime.fromisoformat(pending["expires_at"])
    assert requested_at.tzinfo is UTC
    assert expires_at - requested_at == timedelta(days=14)

    read_status, _, body = _request(ui_server, "GET", f"/api/sessions/{handle}")
    assert read_status == HTTPStatus.OK
    assert _payload(body)["final_review"] == pending

    no_decision_csrf, _, body = _json_request(
        ui_server,
        "POST",
        decision_path,
        {"selection": "accept", "reviewer": "course-reviewer", "comment": "Reviewed."},
    )
    assert no_decision_csrf == HTTPStatus.FORBIDDEN
    assert _payload(body)["error"]["code"] == "csrf_required"

    extra_decision_field, _, body = _json_request(
        ui_server,
        "POST",
        decision_path,
        {
            "selection": "accept",
            "reviewer": "course-reviewer",
            "comment": "Reviewed.",
            "authority": "deploy",
        },
        csrf=csrf,
    )
    assert extra_decision_field == HTTPStatus.BAD_REQUEST
    assert _payload(body)["error"]["code"] == "bad_request"

    wrong_reviewer, _, body = _json_request(
        ui_server,
        "POST",
        decision_path,
        {"selection": "accept", "reviewer": "another-human", "comment": "Reviewed."},
        csrf=csrf,
    )
    assert wrong_reviewer == HTTPStatus.BAD_REQUEST
    assert _payload(body)["error"]["code"] == "invalid_reviewer"

    decided, _, body = _json_request(
        ui_server,
        "POST",
        decision_path,
        {
            "selection": "accept",
            "reviewer": "course-reviewer",
            "comment": "Candidate accepted; deployment remains separate.",
        },
        csrf=csrf,
    )
    assert decided == HTTPStatus.OK
    accepted = _payload(body)["final_review"]
    assert accepted["status"] == "accepted"
    assert accepted["selection"] == "accept"
    assert accepted["candidate_accepted"] is True
    assert accepted["next_action"] == "separate_external_action_required"
    assert accepted["authority_granted"] is False
    assert accepted["external_actions_authorized"] == []

    read_status, _, body = _request(ui_server, "GET", f"/api/sessions/{handle}")
    assert read_status == HTTPStatus.OK
    assert _payload(body)["final_review"] == accepted

    repeated, _, body = _json_request(
        ui_server,
        "POST",
        decision_path,
        {"selection": "reject", "reviewer": "course-reviewer", "comment": "Again."},
        csrf=csrf,
    )
    assert repeated == HTTPStatus.BAD_REQUEST
    assert _payload(body)["error"]["code"] == "final_review_already_decided"

    repeated_request, _, body = _json_request(
        ui_server,
        "POST",
        request_path,
        {"requester": "migration-owner", "designated_reviewer": "course-reviewer"},
        csrf=csrf,
    )
    assert repeated_request == HTTPStatus.BAD_REQUEST
    assert _payload(body)["error"]["code"] == "final_review_already_requested"


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

    status, headers, _ = _request(ui_server, "OPTIONS", "/api/conversations")
    assert status == HTTPStatus.METHOD_NOT_ALLOWED
    assert not any(name.startswith("access-control-allow-") for name in headers)


@pytest.mark.parametrize("method", ("PUT", "PATCH", "DELETE", "TRACE", "BREW"))
def test_unsupported_http_methods_use_the_json_error_contract(
    ui_server: server_module.ThreadingHTTPServer,
    method: str,
) -> None:
    status, headers, body = _request(ui_server, method, "/api/conversations")

    assert status == HTTPStatus.METHOD_NOT_ALLOWED
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert headers["cache-control"] == "no-store"
    assert _payload(body) == {
        "error": {
            "code": "method_not_allowed",
            "message": "That HTTP method is not allowed.",
        }
    }


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
        "/api/conversations",
        {"scenario_id": "salesforce-vf-to-lwc"},
    )
    assert status == HTTPStatus.FORBIDDEN
    assert _payload(body)["error"]["code"] == "csrf_required"

    csrf = _csrf(ui_server)
    status, _, body = _json_request(
        ui_server,
        "POST",
        "/api/conversations",
        {"scenario_id": "salesforce-vf-to-lwc"},
        csrf=csrf,
        content_type="text/plain",
    )
    assert status == HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    assert _payload(body)["error"]["code"] == "invalid_content_type"

    status, _, body = _request(
        ui_server,
        "POST",
        "/api/conversations",
        body=(b'{"scenario_id":"salesforce-vf-to-lwc","scenario_id":"mulesoft-mule3-to-mule4"}'),
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
        "/api/conversations",
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
            "POST /api/conversations HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            f"X-Agent-UI-CSRF: {csrf}\r\n"
            "Connection: close\r\n\r\n"
            '{"scenario_id":"salesforce-vf-to-lwc"}'
        ).encode(),
    )
    assert no_length.startswith(b"HTTP/1.1 411")
    assert b'"code":"invalid_content_length"' in no_length

    chunked = _raw_request(
        port,
        (
            "POST /api/conversations HTTP/1.1\r\n"
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
        "/api/conversations",
        {
            "scenario_id": "salesforce-vf-to-lwc",
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
    captured: list[
        tuple[
            Path,
            int,
            str,
            str | None,
            float | None,
            LiveModelApproval | None,
            str | None,
            float,
        ]
    ] = []

    def fake_build_ui_server(
        project_root: Path,
        *,
        port: int,
        model_provider: str,
        model_id: str | None,
        model_timeout_seconds: float | None,
        live_model_approval: LiveModelApproval | None,
        ollama_model_id: str | None,
        ollama_timeout_seconds: float,
    ) -> _FakeServer:
        captured.append(
            (
                project_root,
                port,
                model_provider,
                model_id,
                model_timeout_seconds,
                live_model_approval,
                ollama_model_id,
                ollama_timeout_seconds,
            )
        )
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
    assert captured == [(tmp_path, 9137, "ollama", None, None, None, "qwen3.8:latest", 600.0)]
    assert fake_server.served is True
    assert fake_server.closed is True


def test_build_ui_server_wires_an_explicit_claude_cli_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(server_module, "AgentUiService", _FakeAgentUiService)
    approval = LiveModelApproval(
        allow_live_api=True,
        allow_prompt_data_sharing=True,
        approved_by="demo-operator",
    )

    server = build_ui_server(
        tmp_path,
        port=0,
        model_provider="claude-cli",
        model_id="claude-sonnet-5",
        model_timeout_seconds=900.0,
        live_model_approval=approval,
    )
    try:
        service = server.ui_service
        assert service.model_configuration() == {
            "provider": "claude-cli",
            "model_id": "claude-sonnet-5",
            "execution_boundary": "remote_provider_managed",
        }
        assert service.model_timeout_seconds == 900.0
        assert service.live_model_approval == approval
    finally:
        server.server_close()
