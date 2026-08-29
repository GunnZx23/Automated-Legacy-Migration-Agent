from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from pydantic import Field

import legacy_migration_agent.agent_runtime.claude_cli_model as claude_cli_model
from legacy_migration_agent.agent_runtime.claude_cli_model import (
    ClaudeCliStructuredModelClient,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelOutputError,
    ModelRuntimeError,
)
from legacy_migration_agent.contracts import StrictModel


class Input(StrictModel):
    value: str


class Output(StrictModel):
    answer: str = Field(min_length=1)


def _approval() -> LiveModelApproval:
    return LiveModelApproval(
        allow_live_api=True,
        allow_prompt_data_sharing=True,
        approved_by="course-demo-operator",
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

    def fake_run(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        normalized = tuple(command)
        calls.append((normalized, kwargs))
        if normalized[1:] == ("--version",):
            return _completed(normalized, stdout="2.1.251 (Claude Code)\n")
        if normalized[1:] == ("auth", "status", "--json"):
            return _completed(
                normalized,
                stdout=json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": "third_party",
                        "apiProvider": "bedrock",
                    }
                ),
            )
        return invoke(normalized, kwargs)

    monkeypatch.setattr(claude_cli_model.subprocess, "run", fake_run)
    return calls


def _argument_after(command: Sequence[str], flag: str) -> str:
    position = command.index(flag)
    return command[position + 1]


def test_cli_uses_native_schema_tool_free_safe_mode_and_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
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
        "--safe-mode",
        "--no-chrome",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--restricted",
    } <= set(invoke_command)
    assert _argument_after(invoke_command, "--permission-mode") == "dontAsk"
    assert _argument_after(invoke_command, "--tools") == ""
    assert "--dangerously-skip-permissions" not in invoke_command
    assert _argument_after(invoke_command, "--output-format") == "json"
    assert json.loads(_argument_after(invoke_command, "--json-schema")) == (
        Output.model_json_schema(mode="validation")
    )
    assert _argument_after(invoke_command, "--model") == "claude-sonnet-5"
    assert '"value":"frozen source context"' in invoke_kwargs["input"]
    assert "frozen source context" not in invoke_command

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
        ).hexdigest()
    )
    assert client.runtime_identity_digest == expected_runtime_identity
    assert client.last_usage is not None
    assert client.last_usage.provider_usage_reported is True
    assert client.last_usage.input_tokens == 19
    assert client.last_usage.output_tokens == 7
    assert client.last_usage.total_tokens == 26


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


def test_cli_rejects_missing_approval_and_malformed_native_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ModelConfigurationError, match="explicit approval"):
        ClaudeCliStructuredModelClient(
            "claude-sonnet-5",
            approval=None,  # type: ignore[arg-type]
        )

    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": {"answer": "", "unexpected": True},
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


def test_cli_never_treats_prose_result_as_native_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_scripted_cli(
        monkeypatch,
        lambda command, _kwargs: _completed(
            command,
            stdout=json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "result": '{"answer":"looks structured but is prose"}',
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
    current = client._resolve_model_revision(timeout_seconds=30)

    client.bind_runtime_identity(current)

    assert client.runtime_identity_digest == current
    assert client.model_revision is None
    with pytest.raises(ModelConfigurationError, match="runtime revision changed"):
        client.bind_runtime_identity("sha256:" + "f" * 64)
