from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

import legacy_migration_agent.agent_runtime.claude_cli_model as claude_cli_model
from legacy_migration_agent.agent_runtime.claude_cli_model import (
    ClaudeCliStructuredModelClient,
)
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectConversationReply,
    ArchitectManifestProposal,
    EngineerModelOutcome,
    _ArchitectProviderManifestProposal,
    _scoped_engineer_model_outcome_type,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelOutputError,
    ModelRuntimeError,
)
from legacy_migration_agent.contracts import StrictModel

_TEST_EXECUTABLE_DIGEST = "sha256:" + "e" * 64
_TEST_ENVIRONMENT_DIGEST = "sha256:" + "d" * 64
_TEST_SETTINGS_DIGEST = "sha256:" + "a" * 64
_TEST_SETTINGS_ARGUMENT = (
    '{"apiKeyHelper":"/test/bin/credential-helper auth claude",'
    '"env":{"ANTHROPIC_DEFAULT_SONNET_MODEL":"us.anthropic.claude-sonnet-5",'
    '"CLAUDE_CODE_USE_BEDROCK":"1"}}'
)


class Input(StrictModel):
    value: str


class Output(StrictModel):
    answer: str = Field(min_length=1)


def _approval() -> LiveModelApproval:
    return LiveModelApproval(
        allow_live_api=True,
        allow_prompt_data_sharing=True,
        approved_by="course-demo-operator",
        approved_remote_provider_id="bedrock",
    )


def _completed(
    command: Sequence[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=tuple(command),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _install_scripted_cli(
    monkeypatch: pytest.MonkeyPatch,
    invoke: Callable[[Sequence[str], dict[str, Any]], subprocess.CompletedProcess[str]],
) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        claude_cli_model,
        "_resolve_claude_executable",
        lambda: claude_cli_model._ClaudeExecutableIdentity(
            path="claude",
            file_digest=_TEST_EXECUTABLE_DIGEST,
            device=1,
            inode=2,
            size=3,
            modified_ns=4,
            changed_ns=5,
            owner_uid=6,
            mode=0o755,
        ),
    )
    monkeypatch.setattr(
        claude_cli_model,
        "_verify_executable_unchanged",
        lambda _identity: None,
    )
    monkeypatch.setattr(
        claude_cli_model,
        "_controlled_claude_environment",
        lambda: ({"PATH": "/test/bin"}, _TEST_ENVIRONMENT_DIGEST),
    )
    monkeypatch.setattr(
        claude_cli_model,
        "_resolve_claude_auth_settings",
        lambda _provider: claude_cli_model._ClaudeAuthSettings(
            argument=_TEST_SETTINGS_ARGUMENT,
            digest=_TEST_SETTINGS_DIGEST,
        ),
    )

    def fake_run(
        command: Sequence[str],
        *,
        input_text: str | None,
        timeout_seconds: float,
        cwd: str,
        environment: dict[str, str],
        stdout_limit: int,
        stderr_limit: int,
    ) -> claude_cli_model._BoundedProcessResult:
        normalized = tuple(command)
        kwargs: dict[str, Any] = {
            "input": input_text,
            "timeout": timeout_seconds,
            "cwd": cwd,
            "env": dict(environment),
            "stdout_limit": stdout_limit,
            "stderr_limit": stderr_limit,
        }
        calls.append((normalized, kwargs))
        if normalized[1:] == ("--version",):
            scripted = _completed(normalized, stdout="2.1.251 (Claude Code)\n")
        elif normalized[-3:] == ("auth", "status", "--json"):
            scripted = _completed(
                normalized,
                stdout=json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": "third_party",
                        "apiProvider": "bedrock",
                    }
                ),
            )
        else:
            scripted = invoke(normalized, kwargs)
        stdout_bytes = scripted.stdout.encode("utf-8")
        stderr_bytes = scripted.stderr.encode("utf-8")
        return claude_cli_model._BoundedProcessResult(
            args=normalized,
            returncode=scripted.returncode,
            stdout=stdout_bytes[:stdout_limit].decode("utf-8", errors="replace"),
            stderr=stderr_bytes[:stderr_limit].decode("utf-8", errors="replace"),
            stdout_exceeded=len(stdout_bytes) > stdout_limit,
            stderr_exceeded=len(stderr_bytes) > stderr_limit,
        )

    monkeypatch.setattr(claude_cli_model, "_run_bounded_command", fake_run)
    return calls


def _argument_after(command: Sequence[str], flag: str) -> str:
    position = command.index(flag)
    return command[position + 1]


def test_cli_uses_native_schema_tool_free_bare_mode_and_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "provider prose is not the structured-output authority",
        "structured_output": {"answer": "migrated"},
        "usage": {
            "input_tokens": 11,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 5,
            "output_tokens": 7,
        },
    }
    calls = _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(command, stdout=json.dumps(envelope)),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
        timeout_seconds=120,
    )

    result = client.parse(
        system_prompt="You are the bounded Engineer role.",
        input_value=Input(value="frozen source context"),
        output_type=Output,
    )

    assert result == Output(answer="migrated")
    invoke_command, invoke_kwargs = calls[-1]
    assert invoke_command[0] == "claude"
    assert {
        "--print",
        "--bare",
        "--no-chrome",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--strict-mcp-config",
    } <= set(invoke_command)
    assert "--safe-mode" not in invoke_command
    assert "--restricted" not in invoke_command
    assert _argument_after(invoke_command, "--permission-mode") == "dontAsk"
    assert _argument_after(invoke_command, "--tools") == ""
    assert _argument_after(invoke_command, "--settings") == _TEST_SETTINGS_ARGUMENT
    assert "--dangerously-skip-permissions" not in invoke_command
    assert _argument_after(invoke_command, "--output-format") == "json"
    provider_schema = json.loads(_argument_after(invoke_command, "--json-schema"))
    assert provider_schema["required"] == ["answer"]
    assert provider_schema["additionalProperties"] is False
    assert provider_schema["properties"]["answer"]["minLength"] == 1
    assert _argument_after(invoke_command, "--effort") == "high"
    assert _argument_after(invoke_command, "--model") == "claude-sonnet-5"
    assert '"value":"frozen source context"' in invoke_kwargs["input"]
    assert '"required":["answer"]' not in invoke_kwargs["input"]
    assert "frozen source context" not in invoke_command
    assert invoke_kwargs["env"] == {"PATH": "/test/bin"}

    assert client.provider == "claude-cli"
    assert client.execution_boundary == "remote_provider_managed"
    assert client.live_invocation is True
    assert client.store_false_sent is False
    assert client.live_approval == _approval()
    assert client.model_revision is None
    expected_runtime_identity = (
        "sha256:"
        + hashlib.sha256(
            b"claude-cli\x00claude-sonnet-5\x002.1.251 (Claude Code)\x00bedrock"
            b"\x00claude"
            + b"\x00"
            + _TEST_EXECUTABLE_DIGEST.encode("ascii")
            + b"\x00"
            + _TEST_ENVIRONMENT_DIGEST.encode("ascii")
            + b"\x00"
            + _TEST_SETTINGS_DIGEST.encode("ascii")
            + b"\x00high"
        ).hexdigest()
    )
    assert client.runtime_identity_digest == expected_runtime_identity
    assert client.remote_provider_id == "bedrock"
    assert client.last_usage is not None
    assert client.last_usage.provider_usage_reported is True
    assert client.last_usage.input_tokens == 19
    assert client.last_usage.output_tokens == 7
    assert client.last_usage.total_tokens == 26


def test_cli_lifecycle_reports_only_bounded_request_and_result_measurements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        claude_cli_model,
        "lifecycle_event",
        lambda event, **fields: events.append((event, fields)),
    )
    structured_output = {"answer": "private-result-π"}
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": structured_output,
        "usage": {
            "input_tokens": 13,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "output_tokens": 5,
        },
    }
    stdout = json.dumps(envelope)
    calls = _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=stdout,
            stderr="bounded-provider-warning",
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
        timeout_seconds=120,
    )
    system_prompt = "private-system-/private/secret/prompt-π"
    input_value = Input(value="private-input-api-key-secret-π")

    assert client.parse(
        system_prompt=system_prompt,
        input_value=input_value,
        output_type=Output,
    ) == Output(answer="private-result-π")

    invoke_command, invoke_kwargs = calls[-1]
    generation_started = next(
        fields for event, fields in events if event == "claude_cli.generation.started"
    )
    assert generation_started == {
        "model_id": "claude-sonnet-5",
        "output_type": "Output",
        "system_prompt_bytes": len(system_prompt.encode("utf-8")),
        "input_bytes": len(claude_cli_model.canonical_json_bytes(input_value)),
        "user_prompt_bytes": len(invoke_kwargs["input"].encode("utf-8")),
        "provider_schema_bytes": len(
            _argument_after(invoke_command, "--json-schema").encode("utf-8")
        ),
        "timeout_seconds": 120.0,
    }
    invoke_completed = next(
        fields for event, fields in events if event == "claude_cli.invoke.completed"
    )
    assert invoke_completed == {
        "model_id": "claude-sonnet-5",
        "elapsed_ms": invoke_completed["elapsed_ms"],
        "stdout_bytes": len(stdout.encode("utf-8")),
        "stderr_bytes": len(b"bounded-provider-warning"),
        "structured_output_bytes": len(claude_cli_model.canonical_json_bytes(structured_output)),
        "provider_usage_reported": True,
        "provider_usage_suspect": False,
        "reported_input_tokens": 18,
        "reported_output_tokens": 5,
        "reported_total_tokens": 23,
    }
    assert isinstance(invoke_completed["elapsed_ms"], int)
    assert invoke_completed["elapsed_ms"] >= 0
    exposed = repr(events)
    for private_value in (
        system_prompt,
        input_value.value,
        structured_output["answer"],
        "bounded-provider-warning",
        "/private/secret/prompt",
        "api-key-secret",
    ):
        assert private_value not in exposed


def test_cli_zero_usage_on_nonempty_output_is_diagnostic_only_suspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        claude_cli_model,
        "lifecycle_event",
        lambda event, **fields: events.append((event, fields)),
    )
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {"answer": "nonempty"},
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    assert client.parse(
        system_prompt="bounded",
        input_value=Input(value="frozen"),
        output_type=Output,
    ) == Output(answer="nonempty")

    invoke_completed = next(
        fields for event, fields in events if event == "claude_cli.invoke.completed"
    )
    assert invoke_completed["provider_usage_reported"] is True
    assert invoke_completed["provider_usage_suspect"] is True
    assert invoke_completed["reported_input_tokens"] == 0
    assert invoke_completed["reported_output_tokens"] == 0
    assert invoke_completed["reported_total_tokens"] == 0
    assert client.last_usage is not None
    assert client.last_usage.provider_usage_reported is True
    assert client.last_usage.input_tokens == 0
    assert client.last_usage.output_tokens == 0
    assert client.last_usage.total_tokens == 0


def test_cli_unbounded_provider_usage_cannot_break_lifecycle_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        claude_cli_model,
        "lifecycle_event",
        lambda event, **fields: events.append((event, fields)),
    )
    huge_token_count = int("9" * 4_300)
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {"answer": "nonempty"},
                    "usage": {
                        "input_tokens": huge_token_count,
                        "output_tokens": 1,
                    },
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
        timeout_seconds=120,
    )

    assert client.parse(
        system_prompt="bounded",
        input_value=Input(value="frozen"),
        output_type=Output,
    ) == Output(answer="nonempty")

    invoke_completed = next(
        fields for event, fields in events if event == "claude_cli.invoke.completed"
    )
    assert invoke_completed["provider_usage_reported"] is True
    assert invoke_completed["provider_usage_suspect"] is True
    assert invoke_completed["reported_input_tokens"] is None
    assert invoke_completed["reported_output_tokens"] is None
    assert invoke_completed["reported_total_tokens"] is None
    json.dumps(invoke_completed)


@pytest.mark.parametrize(
    ("invoke_result", "error_type", "message"),
    [
        (
            {"returncode": 7, "stdout": "raw-output-secret", "stderr": "raw-error-secret"},
            ModelRuntimeError,
            "request did not complete",
        ),
        (
            {"returncode": 0, "stdout": "raw-output-secret", "stderr": ""},
            ModelOutputError,
            "JSON envelope",
        ),
    ],
)
def test_cli_failures_expose_only_sanitized_errors_and_stream_digests(
    monkeypatch: pytest.MonkeyPatch,
    invoke_result: dict[str, Any],
    error_type: type[Exception],
    message: str,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        claude_cli_model,
        "lifecycle_event",
        lambda event, **fields: events.append((event, fields)),
    )
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(command, **invoke_result),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    with pytest.raises(error_type, match=message) as raised:
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="private source"),
            output_type=Output,
        )

    exposed = f"{raised.value!s}\n{events!r}"
    assert "raw-output-secret" not in exposed
    assert "raw-error-secret" not in exposed
    assert any("stdout_digest" in fields or "stderr_digest" in fields for _event, fields in events)


def test_cli_digests_untrusted_failure_subtype_instead_of_logging_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        claude_cli_model,
        "lifecycle_event",
        lambda event, **fields: events.append((event, fields)),
    )
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "provider-secret-subtype",
                    "is_error": True,
                    "structured_output": None,
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    with pytest.raises(ModelRuntimeError, match="did not complete.*successfully") as raised:
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="private source"),
            output_type=Output,
        )

    exposed = f"{raised.value!s}\n{events!r}"
    assert "provider-secret-subtype" not in exposed
    assert any("subtype_digest" in fields for _event, fields in events)


def test_cli_classifies_nonzero_json_failure_without_exposing_provider_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        claude_cli_model,
        "lifecycle_event",
        lambda event, **fields: events.append((event, fields)),
    )
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            returncode=1,
            stdout=json.dumps(
                {
                    "terminal_reason": "api_error",
                    "result": "Structured output JSON schema rejected; private-provider-detail",
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    with pytest.raises(ModelRuntimeError, match="request did not complete"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="private source"),
            output_type=Output,
        )

    exposed = repr(events)
    assert "private-provider-detail" not in exposed
    assert any(
        fields.get("failure_category") == "provider_api_error" and "stdout_digest" in fields
        for _event, fields in events
    )


def test_cli_rejects_missing_approval_and_schema_invalid_structured_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ModelConfigurationError, match="explicit approval"):
        ClaudeCliStructuredModelClient(
            "claude-sonnet-5",
            approval=None,  # type: ignore[arg-type]
        )

    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        claude_cli_model,
        "lifecycle_event",
        lambda event, **fields: events.append((event, fields)),
    )
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {
                        "answer": "",
                        "unexpected": "private-validation-detail",
                    },
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    with pytest.raises(ModelOutputError, match="schema validation"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="frozen"),
            output_type=Output,
        )
    rejection = next(fields for event, fields in events if event == "claude_cli.output.rejected")
    assert rejection["validation_locations"] == "answer,unexpected"
    assert rejection["validation_kinds"] == "string_too_short,extra_forbidden"
    assert rejection["validation_truncated"] is False
    assert "frozen" not in repr(rejection)
    assert "private-validation-detail" not in repr(rejection)


def test_cli_scoped_engineer_rejection_logs_only_aggregate_plan_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = "generated/private-alpha.txt"
    private_content = "private-generated-content"
    output_type = _scoped_engineer_model_outcome_type((private_path, "generated/private-beta.txt"))
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        claude_cli_model,
        "lifecycle_event",
        lambda event, **fields: events.append((event, fields)),
    )
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {
                        "result": {
                            "kind": "file_plan",
                            "file_plan": {
                                "updates": [
                                    {
                                        "path": private_path,
                                        "content": private_content,
                                    }
                                ],
                                "assumptions": ["private-assumption"],
                            },
                        }
                    },
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    with pytest.raises(ModelOutputError, match="schema validation"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="frozen"),
            output_type=output_type,
        )

    rejection = next(fields for event, fields in events if event == "claude_cli.output.rejected")
    assert {
        field: rejection[field]
        for field in (
            "approved",
            "proposed",
            "unique",
            "missing",
            "extra",
            "scope_valid",
            "exact_coverage",
            "exact_coverage_required",
            "total_content_chars",
            "assumptions",
        )
    } == {
        "approved": 2,
        "proposed": 1,
        "unique": 1,
        "missing": 1,
        "extra": 0,
        "scope_valid": False,
        "exact_coverage": False,
        "exact_coverage_required": True,
        "total_content_chars": len(private_content),
        "assumptions": 1,
    }
    assert private_path not in repr(rejection)
    assert private_content not in repr(rejection)
    assert "private-assumption" not in repr(rejection)


@pytest.mark.parametrize(
    "result_text",
    (
        '{"answer":"looks structured"}',
        'prose before {"answer":"looks structured"}',
    ),
)
def test_cli_never_treats_result_only_text_as_native_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    result_text: str,
) -> None:
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "result": result_text,
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    with pytest.raises(ModelOutputError, match="native structured output"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="frozen"),
            output_type=Output,
        )


@pytest.mark.parametrize("structured_output", (None, "text", [{"answer": "array-root"}]))
def test_cli_rejects_non_object_native_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    structured_output: object,
) -> None:
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": structured_output,
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    with pytest.raises(ModelOutputError, match="native structured output"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="frozen"),
            output_type=Output,
        )


def test_cli_real_architect_union_schema_uses_native_contract_and_validates_controller_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {
                        "status": "ready_to_launch",
                        "assistant_message": "The bounded request is ready.",
                        "advisory_summary": "The selected migration is ready to plan.",
                        "missing_information": [],
                    },
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    result = client.parse(
        system_prompt="bounded",
        input_value=Input(value="frozen"),
        output_type=ArchitectConversationReply,
    )

    assert result.status == "ready_to_launch"
    invoke_command, invoke_kwargs = calls[-1]
    provider_schema = json.loads(_argument_after(invoke_command, "--json-schema"))
    assert "oneOf" not in provider_schema
    assert provider_schema["type"] == "object"
    assert provider_schema["additionalProperties"] is False
    assert provider_schema["required"] == [
        "status",
        "assistant_message",
        "advisory_summary",
        "missing_information",
    ]
    assert provider_schema["properties"]["status"] == {
        "anyOf": [
            {"const": "clarification_needed", "type": "string"},
            {"const": "ready_to_launch", "type": "string"},
        ]
    }
    assert provider_schema["properties"]["assistant_message"] == {
        "maxLength": 2000,
        "minLength": 1,
        "type": "string",
    }
    assert '"oneOf"' not in invoke_kwargs["input"]


def test_cli_original_architect_contract_rejects_projected_cross_branch_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {
                        "status": "ready_to_launch",
                        "assistant_message": "The bounded request is ready.",
                        "advisory_summary": None,
                        "missing_information": [],
                    },
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    with pytest.raises(ModelOutputError, match="schema validation"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="frozen"),
            output_type=ArchitectConversationReply,
        )

    provider_schema = json.loads(_argument_after(calls[-1][0], "--json-schema"))
    assert "oneOf" not in provider_schema
    assert provider_schema["properties"]["advisory_summary"] == {
        "anyOf": [
            {"type": "null"},
            {"maxLength": 1000, "minLength": 10, "type": "string"},
        ]
    }


def test_cli_root_union_projection_refuses_nonidentical_or_constrained_branches() -> None:
    source = ArchitectConversationReply.model_json_schema(mode="validation")
    differing_fields = json.loads(json.dumps(source))
    differing_fields["oneOf"][1]["properties"]["unexpected"] = {"type": "string"}
    constrained_branch = json.loads(json.dumps(source))
    constrained_branch["oneOf"][0]["minProperties"] = 4

    for schema in (differing_fields, constrained_branch):
        projected = claude_cli_model._project_claude_root_correlated_union(schema)
        assert "oneOf" in projected
        assert "properties" not in projected


def test_cli_provider_schema_removes_only_discriminator_from_engineer_union() -> None:
    original = EngineerModelOutcome.model_json_schema(mode="validation")
    normalized = claude_cli_model._normalize_claude_provider_schema(original)
    projected = claude_cli_model._project_claude_root_correlated_union(original)
    expected = json.loads(json.dumps(original))
    expected["properties"]["result"].pop("discriminator")

    def keys(value: object) -> list[str]:
        if isinstance(value, dict):
            found: list[str] = []
            for key, item in value.items():
                found.append(key)
                found.extend(keys(item))
            return found
        if isinstance(value, list):
            return [key for item in value for key in keys(item)]
        return []

    original_result = original["properties"]["result"]
    normalized_result = normalized["properties"]["result"]
    assert "discriminator" in original_result
    assert "discriminator" not in keys(normalized)
    assert normalized_result["oneOf"] == original_result["oneOf"]
    assert normalized["$defs"] == original["$defs"]
    assert normalized["required"] == original["required"]
    assert normalized["additionalProperties"] is False
    assert normalized == expected
    assert projected == normalized
    assert projected["properties"]["result"]["oneOf"] == original_result["oneOf"]


def test_cli_provider_schema_preserves_architect_risk_pair_correlation() -> None:
    original = ArchitectManifestProposal.model_json_schema(mode="validation")
    normalized = claude_cli_model._normalize_claude_provider_schema(original)

    original_risk = original["$defs"]["ArchitectRiskObservation"]
    normalized_risk = normalized["$defs"]["ArchitectRiskObservation"]
    assert normalized_risk["oneOf"] == original_risk["oneOf"]
    assert normalized_risk["properties"]["hazard_reason"]["oneOf"] == [
        {"$ref": "#/$defs/RiskReason"},
        {"type": "null"},
    ]
    assert all(
        "const" in branch["properties"]["category"]
        and "const" in branch["properties"]["hazard_reason"]
        for branch in normalized_risk["oneOf"][1:]
    )


def test_cli_architect_provider_schema_rejects_path_prose_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_summary = "Read migration evidence from /tmp/private-architect-output."
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        claude_cli_model,
        "lifecycle_event",
        lambda event, **fields: events.append((event, fields)),
    )
    calls = _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {
                        "semantic_decisions": [
                            {
                                "decision_id": "portable-prose-test",
                                "category": "target_architecture",
                                "summary": private_summary,
                                "evidence_ids": ["node-1"],
                            }
                        ],
                        "cited_graph_nodes": ["node-1"],
                        "cited_wiki_pages": ["wiki-1"],
                        "risk_observations": [],
                        "unresolved_questions": [],
                    },
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    with pytest.raises(ModelOutputError, match="schema validation"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="frozen"),
            output_type=_ArchitectProviderManifestProposal,
        )

    provider_schema = json.loads(_argument_after(calls[-1][0], "--json-schema"))
    assert (
        provider_schema["$defs"]["ArchitectSemanticDecision"]["properties"]["summary"]["pattern"]
        == r"^[^/\\]*$"
    )
    rejection = next(fields for event, fields in events if event == "claude_cli.output.rejected")
    assert rejection["validation_kinds"] == "value_error"
    assert private_summary not in repr(rejection)
    assert private_summary not in repr(events)


def test_cli_provider_schema_preserves_fields_and_examples_named_discriminator() -> None:
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"discriminator": {"type": "string", "minLength": 3}},
        "required": ["discriminator"],
        "examples": [{"discriminator": "kept-data"}],
        "oneOf": [
            {
                "discriminator": {"propertyName": "kind"},
                "type": "object",
                "required": ["discriminator"],
            }
        ],
    }

    normalized = claude_cli_model._normalize_claude_provider_schema(schema)

    assert normalized["properties"] == schema["properties"]
    assert normalized["examples"] == schema["examples"]
    assert normalized["oneOf"][0] == {
        "type": "object",
        "required": ["discriminator"],
    }


def test_cli_rejects_oversized_raw_envelope_before_result_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        claude_cli_model,
        "lifecycle_event",
        lambda event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(claude_cli_model, "_MAX_CLI_ENVELOPE_BYTES", 64)
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {"answer": "migrated"},
                    "unrelated": "x" * 128,
                }
            ),
        ),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    with pytest.raises(ModelOutputError, match="envelope exceeded the body limit"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="frozen"),
            output_type=Output,
        )
    limit_event = next(
        fields for event, fields in events if event == "claude_cli.invoke.output_limit_exceeded"
    )
    assert limit_event["stdout_bytes"] == 64
    assert limit_event["stderr_bytes"] == 0
    assert limit_event["stdout_exceeded"] is True
    assert limit_event["stderr_exceeded"] is False
    assert "x" * 128 not in repr(events)


@pytest.mark.parametrize("stream_name", ("stdout", "stderr"))
def test_bounded_process_stops_at_each_stream_limit(
    tmp_path: Path,
    stream_name: str,
) -> None:
    target = "stdout" if stream_name == "stdout" else "stderr"
    script = f"import sys; sys.{target}.buffer.write(b'x' * 4096); sys.{target}.flush()"

    result = claude_cli_model._run_bounded_command(
        (sys.executable, "-c", script),
        input_text=None,
        timeout_seconds=5.0,
        cwd=str(tmp_path),
        environment={},
        stdout_limit=64,
        stderr_limit=64,
    )

    assert result.stdout_exceeded is (stream_name == "stdout")
    assert result.stderr_exceeded is (stream_name == "stderr")
    assert len(result.stdout.encode("utf-8")) <= 64
    assert len(result.stderr.encode("utf-8")) <= 64


def test_bounded_process_times_out_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reaped_returncodes: list[int | None] = []
    original_kill = claude_cli_model._kill_process_group

    def tracked_kill(process: subprocess.Popen[bytes]) -> None:
        original_kill(process)
        reaped_returncodes.append(process.returncode)

    monkeypatch.setattr(claude_cli_model, "_kill_process_group", tracked_kill)
    with pytest.raises(subprocess.TimeoutExpired):
        claude_cli_model._run_bounded_command(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            input_text=None,
            timeout_seconds=0.05,
            cwd=str(tmp_path),
            environment={},
            stdout_limit=64,
            stderr_limit=64,
        )
    assert reaped_returncodes
    assert all(returncode is not None for returncode in reaped_returncodes)


def test_cli_runtime_binding_detects_changed_runtime_without_claiming_model_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(command, stdout="unused"),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )
    current = client.resolve_runtime_identity()

    client.bind_runtime_identity(current)

    assert client.runtime_identity_digest == current
    assert client.model_revision is None
    with pytest.raises(ModelConfigurationError, match="runtime revision changed"):
        client.bind_runtime_identity("sha256:" + "f" * 64)


def test_cli_runtime_identity_preflight_sends_no_model_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: pytest.fail(f"unexpected model invocation: {command!r}"),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    first = client.resolve_runtime_identity()
    second = client.resolve_runtime_identity()

    assert first == second == client.runtime_identity_digest
    assert [command[1:] for command, _kwargs in calls if command[1:] == ("--version",)] == [
        ("--version",),
        ("--version",),
    ]
    auth_commands = [
        command for command, _kwargs in calls if command[-3:] == ("auth", "status", "--json")
    ]
    assert len(auth_commands) == 2
    assert all("--bare" in command for command in auth_commands)
    assert all(
        _argument_after(command, "--settings") == _TEST_SETTINGS_ARGUMENT
        for command in auth_commands
    )
    assert all(kwargs["env"] == {"PATH": "/test/bin"} for _command, kwargs in calls)


def test_cli_rejects_executable_path_override_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEGACY_MIGRATION_CLAUDE_CLI_PATH", "/tmp/unapproved-claude")
    monkeypatch.setattr(
        claude_cli_model,
        "_run_bounded_command",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not run"),
    )

    with pytest.raises(ModelConfigurationError, match="executable-path override"):
        ClaudeCliStructuredModelClient(
            "claude-sonnet-5",
            approval=_approval(),
        )


@pytest.mark.parametrize(
    "variable",
    (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_AWS_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_GOOGLE_CLOUD_BASE_URL",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "ANTHROPIC_API_HOST",
        "ANTHROPIC_UNIX_SOCKET",
        "CLAUDE_CODE_API_BASE_URL",
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
        "AWS_BEDROCK_ENDPOINT_URL",
        "NODE_OPTIONS",
    ),
)
def test_cli_rejects_endpoint_and_code_injection_overrides_before_prompt(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    monkeypatch.setattr(
        claude_cli_model,
        "_resolve_claude_executable",
        lambda: claude_cli_model._ClaudeExecutableIdentity(
            path="claude",
            file_digest=_TEST_EXECUTABLE_DIGEST,
            device=1,
            inode=2,
            size=3,
            modified_ns=4,
            changed_ns=5,
            owner_uid=6,
            mode=0o755,
        ),
    )
    monkeypatch.setattr(
        claude_cli_model,
        "_verify_executable_unchanged",
        lambda _identity: None,
    )
    monkeypatch.setattr(
        claude_cli_model,
        "_resolve_claude_auth_settings",
        lambda _provider: claude_cli_model._ClaudeAuthSettings(
            argument=_TEST_SETTINGS_ARGUMENT,
            digest=_TEST_SETTINGS_DIGEST,
        ),
    )
    monkeypatch.setenv(variable, "unapproved")
    monkeypatch.setattr(
        claude_cli_model,
        "_run_bounded_command",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not run"),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )

    with pytest.raises(ModelConfigurationError, match="unsupported endpoint"):
        client.resolve_runtime_identity()


def test_controlled_environment_drops_unlisted_values_and_binds_every_retained_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNRELATED_PRIVATE_VALUE", "must-not-reach-child")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:41001")
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:41002")
    monkeypatch.setenv("ALL_PROXY", "socks5h://[::1]:41003")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/tmp/unapproved-proxy-ca.pem")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/unapproved-proxy-ca.pem")
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    environment, first_digest = claude_cli_model._controlled_claude_environment()

    assert "UNRELATED_PRIVATE_VALUE" not in environment
    assert environment["HTTP_PROXY"] == "http://127.0.0.1:41001"
    assert environment["HTTPS_PROXY"] == "http://localhost:41002"
    assert environment["ALL_PROXY"] == "socks5h://[::1]:41003"
    assert {"NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE"}.isdisjoint(environment)
    assert environment["LANG"] == "en_US.UTF-8"
    monkeypatch.setenv("LANG", "C.UTF-8")
    _changed_environment, second_digest = claude_cli_model._controlled_claude_environment()
    assert first_digest != second_digest


def test_controlled_environment_rejects_non_loopback_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example.test:443")

    with pytest.raises(ModelConfigurationError, match="proxy configuration"):
        claude_cli_model._controlled_claude_environment()


def test_cli_requires_and_matches_explicit_remote_provider_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_provider = LiveModelApproval(
        allow_live_api=True,
        allow_prompt_data_sharing=True,
        approved_by="course-demo-operator",
    )
    monkeypatch.setattr(
        claude_cli_model,
        "_resolve_claude_executable",
        lambda: claude_cli_model._ClaudeExecutableIdentity(
            path="claude",
            file_digest=_TEST_EXECUTABLE_DIGEST,
            device=1,
            inode=2,
            size=3,
            modified_ns=4,
            changed_ns=5,
            owner_uid=6,
            mode=0o755,
        ),
    )

    with pytest.raises(ModelConfigurationError, match="explicitly approved remote provider"):
        ClaudeCliStructuredModelClient(
            "claude-sonnet-5",
            approval=missing_provider,
        )

    calls = _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: pytest.fail(f"unexpected model invocation: {command!r}"),
    )
    wrong_provider = _approval().model_copy(update={"approved_remote_provider_id": "anthropic"})
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=wrong_provider,
    )

    with pytest.raises(ModelConfigurationError, match="does not match the explicit approval"):
        client.resolve_runtime_identity()
    assert calls[0][0][1:] == ("--version",)
    assert calls[1][0][-3:] == ("auth", "status", "--json")


def test_cli_rejects_routing_or_executable_drift_before_model_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: pytest.fail(f"unexpected model invocation: {command!r}"),
    )
    environment_digest = [_TEST_ENVIRONMENT_DIGEST]
    executable_valid = [True]
    monkeypatch.setattr(
        claude_cli_model,
        "_controlled_claude_environment",
        lambda: ({"PATH": "/test/bin"}, environment_digest[0]),
    )

    def verify_executable(_identity: object) -> None:
        if not executable_valid[0]:
            raise ModelConfigurationError("the Claude CLI executable identity changed")

    monkeypatch.setattr(claude_cli_model, "_verify_executable_unchanged", verify_executable)
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )
    client.resolve_runtime_identity()
    environment_digest[0] = "sha256:" + "c" * 64

    with pytest.raises(ModelConfigurationError, match="routing environment changed"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="must not be sent"),
            output_type=Output,
        )
    assert all("--print" not in command for command, _kwargs in calls)

    environment_digest[0] = _TEST_ENVIRONMENT_DIGEST
    executable_valid[0] = False
    with pytest.raises(ModelConfigurationError, match="executable identity changed"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="must not be sent"),
            output_type=Output,
        )
    assert all("--print" not in command for command, _kwargs in calls)


def test_cli_passes_only_bound_credential_settings_and_ignores_customizations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    helper = tmp_path / "credential-helper"
    helper.write_bytes(b"bounded helper")
    helper.chmod(0o755)
    settings_directory = tmp_path / ".claude"
    settings_directory.mkdir()
    settings = settings_directory / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "apiKeyHelper": f"'{helper}' auth claude",
                "env": {
                    "CLAUDE_CODE_USE_BEDROCK": "true",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "us.anthropic.claude-sonnet-5",
                    "ANTHROPIC_BASE_URL": "https://must-not-be-forwarded.invalid",
                    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80",
                },
                "hooks": {"PreToolUse": ["must-not-be-forwarded"]},
                "enabledPlugins": {"must-not-be-forwarded": True},
                "permissions": {"allow": ["Bash"]},
            }
        ),
        encoding="utf-8",
    )
    settings.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = claude_cli_model._resolve_claude_auth_settings("bedrock")

    assert json.loads(resolved.argument) == {
        "apiKeyHelper": f"{helper.resolve()} auth claude",
        "env": {
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "us.anthropic.claude-sonnet-5",
            "CLAUDE_CODE_USE_BEDROCK": "1",
        },
    }
    assert resolved.digest.startswith("sha256:")
    helper.write_bytes(b"changed helper")
    helper.chmod(0o755)
    assert claude_cli_model._resolve_claude_auth_settings("bedrock").digest != resolved.digest


def test_cli_passes_only_validated_identity_bound_enterprise_bedrock_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    helper = tmp_path / "credential-helper"
    helper.write_bytes(b"bounded helper")
    helper.chmod(0o755)
    ca_bundle = tmp_path / "enterprise-ca.pem"
    ca_bundle.write_bytes(b"bounded enterprise CA")
    ca_bundle.chmod(0o644)
    settings_directory = tmp_path / ".claude"
    settings_directory.mkdir()
    settings = settings_directory / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "apiKeyHelper": f"'{helper}' auth claude",
                "env": {
                    "ANTHROPIC_BASE_URL": "https://must-not-be-forwarded.invalid",
                    "ANTHROPIC_BEDROCK_BASE_URL": "https://gateway.example.test/bedrock/",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "us.anthropic.claude-sonnet-5",
                    "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "true",
                    "CLAUDE_CODE_USE_BEDROCK": "true",
                    "NODE_EXTRA_CA_CERTS": str(ca_bundle),
                    "NODE_TLS_REJECT_UNAUTHORIZED": "1",
                },
                "hooks": {"PreToolUse": ["must-not-be-forwarded"]},
                "enabledPlugins": {"must-not-be-forwarded": True},
            }
        ),
        encoding="utf-8",
    )
    settings.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = claude_cli_model._resolve_claude_auth_settings("bedrock")
    routed_environment = json.loads(resolved.argument)["env"]

    assert routed_environment == {
        "ANTHROPIC_BEDROCK_BASE_URL": "https://gateway.example.test/bedrock",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "us.anthropic.claude-sonnet-5",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "NODE_EXTRA_CA_CERTS": str(ca_bundle.resolve()),
        "NODE_TLS_REJECT_UNAUTHORIZED": "1",
    }
    assert "ANTHROPIC_BASE_URL" not in routed_environment
    first_digest = resolved.digest
    ca_bundle.write_bytes(b"changed enterprise CA")
    ca_bundle.chmod(0o644)
    assert claude_cli_model._resolve_claude_auth_settings("bedrock").digest != first_digest


@pytest.mark.parametrize(
    ("key", "value", "message"),
    (
        (
            "ANTHROPIC_BEDROCK_BASE_URL",
            "http://gateway.example.test/bedrock",
            "enterprise gateway URL",
        ),
        ("NODE_TLS_REJECT_UNAUTHORIZED", "0", "TLS verification"),
        ("CLAUDE_CODE_SKIP_BEDROCK_AUTH", "1", "gateway authentication mode"),
    ),
)
def test_cli_rejects_unsafe_enterprise_bedrock_route_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    key: str,
    value: str,
    message: str,
) -> None:
    helper = tmp_path / "credential-helper"
    helper.write_bytes(b"bounded helper")
    helper.chmod(0o755)
    settings_directory = tmp_path / ".claude"
    settings_directory.mkdir()
    settings = settings_directory / "settings.json"
    environment = {"CLAUDE_CODE_USE_BEDROCK": "1", key: value}
    settings.write_text(
        json.dumps({"apiKeyHelper": str(helper), "env": environment}),
        encoding="utf-8",
    )
    settings.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(ModelConfigurationError, match=message):
        claude_cli_model._resolve_claude_auth_settings("bedrock")


def test_cli_canonicalizes_helper_symlink_and_rejects_writable_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_helper = tmp_path / "real-helper"
    real_helper.write_bytes(b"bounded helper")
    real_helper.chmod(0o755)
    helper_link = tmp_path / "helper-link"
    helper_link.symlink_to(real_helper)
    settings_directory = tmp_path / ".claude"
    settings_directory.mkdir()
    settings = settings_directory / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "apiKeyHelper": f"'{helper_link}' auth claude",
                "env": {"CLAUDE_CODE_USE_BEDROCK": "1"},
            }
        ),
        encoding="utf-8",
    )
    settings.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = claude_cli_model._resolve_claude_auth_settings("bedrock")

    assert json.loads(resolved.argument)["apiKeyHelper"] == (f"{real_helper.resolve()} auth claude")

    writable_parent = tmp_path / "writable"
    writable_parent.mkdir()
    writable_parent.chmod(0o777)
    unsafe_helper = writable_parent / "helper"
    unsafe_helper.write_bytes(b"unsafe location")
    unsafe_helper.chmod(0o755)
    settings.write_text(
        json.dumps(
            {
                "apiKeyHelper": f"'{unsafe_helper}' auth claude",
                "env": {"CLAUDE_CODE_USE_BEDROCK": "1"},
            }
        ),
        encoding="utf-8",
    )
    settings.chmod(0o600)

    with pytest.raises(ModelConfigurationError, match="directory is group/world writable"):
        claude_cli_model._resolve_claude_auth_settings("bedrock")


@pytest.mark.parametrize(
    "helper_command",
    (
        "relative-helper auth claude",
        "/test/helper $(unapproved-command)",
        "/test/helper; unapproved-command",
    ),
)
def test_cli_rejects_unsafe_credential_helper_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    helper_command: str,
) -> None:
    settings_directory = tmp_path / ".claude"
    settings_directory.mkdir()
    settings = settings_directory / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "apiKeyHelper": helper_command,
                "env": {"CLAUDE_CODE_USE_BEDROCK": "1"},
            }
        ),
        encoding="utf-8",
    )
    settings.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(ModelConfigurationError, match="apiKeyHelper"):
        claude_cli_model._resolve_claude_auth_settings("bedrock")


def test_cli_rejects_settings_drift_before_model_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: pytest.fail(f"unexpected model invocation: {command!r}"),
    )
    client = ClaudeCliStructuredModelClient(
        "claude-sonnet-5",
        approval=_approval(),
    )
    client.resolve_runtime_identity()
    monkeypatch.setattr(
        claude_cli_model,
        "_resolve_claude_auth_settings",
        lambda _provider: claude_cli_model._ClaudeAuthSettings(
            argument=_TEST_SETTINGS_ARGUMENT,
            digest="sha256:" + "b" * 64,
        ),
    )

    with pytest.raises(ModelConfigurationError, match="credential settings changed"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="must not be sent"),
            output_type=Output,
        )
    assert all("--print" not in command for command, _kwargs in calls)


def test_executable_resolver_rejects_writable_binary_and_hashes_secure_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "claude"
    executable.write_bytes(b"bounded fake CLI")
    executable.chmod(0o777)
    monkeypatch.delenv("LEGACY_MIGRATION_CLAUDE_CLI_PATH", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(ModelConfigurationError, match="recognized installation roots"):
        claude_cli_model._resolve_claude_executable()

    monkeypatch.setattr(
        claude_cli_model,
        "_trusted_claude_executable_roots",
        lambda: (tmp_path,),
    )
    with pytest.raises(ModelConfigurationError, match="group/world writable"):
        claude_cli_model._resolve_claude_executable()

    executable.chmod(0o755)
    identity = claude_cli_model._resolve_claude_executable()

    assert identity.path == str(executable.resolve())
    assert identity.file_digest == ("sha256:" + hashlib.sha256(b"bounded fake CLI").hexdigest())
