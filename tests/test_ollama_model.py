from __future__ import annotations

import inspect
import io
import json
from collections.abc import Callable
from typing import Literal

import pytest
from pydantic import Field

import legacy_migration_agent.agent_runtime.ollama_model as ollama_module
from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectConversationReply,
    ArchitectManifestProposal,
    EngineerFilePlanOutcome,
    EngineerModelOutcome,
)
from legacy_migration_agent.agent_runtime.ollama_model import (
    OLLAMA_CHAT_URL,
    OLLAMA_TAGS_URL,
    OllamaStructuredModelClient,
    _LoopbackHttpTransport,
    _project_ollama_schema,
)
from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelOutputError,
    ModelRuntimeError,
    model_call_record,
)
from legacy_migration_agent.contracts import StrictModel
from legacy_migration_agent.core.observability import terminal_lifecycle_logging

REVISION_A = "a" * 64
REVISION_B = "b" * 64
MODEL_ID = "test-model:latest"


class Input(StrictModel):
    value: str


class Output(StrictModel):
    answer: str = Field(min_length=1)


class NestedConstrainedOutput(StrictModel):
    title: str = Field(min_length=3, max_length=12, pattern=r"^[a-z]+$")
    pattern: str = Field(default="safe", min_length=2, max_length=8)
    minimum: int = Field(ge=1, le=5)
    tags: tuple[str, ...] = Field(min_length=1, max_length=2)


class ConstrainedOutput(StrictModel):
    nested: NestedConstrainedOutput
    state: Literal["ready", "blocked"]
    fixed: Literal[True]


class FakeTransport:
    def __init__(self, chat_response: bytes, revisions: list[str] | None = None) -> None:
        self.chat_response = chat_response
        self.revisions = list(revisions or [REVISION_A])
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        *,
        path: str,
        payload: bytes | None,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> bytes:
        self.requests.append(
            {
                "path": path,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
                "maximum_response_bytes": maximum_response_bytes,
            }
        )
        if path == "/api/tags":
            revision = self.revisions.pop(0) if len(self.revisions) > 1 else self.revisions[0]
            return inventory(revision)
        if path == "/api/chat":
            return self.chat_response
        raise AssertionError("unexpected path")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSocket:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        clock: FakeClock | None = None,
        recv_delays: list[float] | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self.clock = clock
        self.recv_delays = list(recv_delays or [])
        self.sent = bytearray()
        self.timeouts: list[float] = []
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def send(self, data: memoryview) -> int:
        self.sent.extend(data)
        return len(data)

    def recv(self, maximum: int) -> bytes:
        if self.recv_delays and self.clock is not None:
            self.clock.advance(self.recv_delays.pop(0))
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > maximum:
            self.chunks.insert(0, chunk[maximum:])
            return chunk[:maximum]
        return chunk

    def close(self) -> None:
        self.closed = True


def approval() -> LiveModelApproval:
    return LiveModelApproval(
        allow_live_api=True,
        allow_prompt_data_sharing=True,
        approved_by="local-ui-reviewer",
    )


def response(content: str, **extra: object) -> bytes:
    return json.dumps(
        {
            "model": MODEL_ID,
            "done": True,
            "done_reason": "stop",
            "message": {"role": "assistant", "content": content},
            **extra,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def inventory(revision: str) -> bytes:
    return json.dumps(
        {
            "models": [
                {
                    "name": MODEL_ID,
                    "model": MODEL_ID,
                    "digest": revision,
                }
            ]
        },
        separators=(",", ":"),
    ).encode("utf-8")


def client_with(
    monkeypatch: pytest.MonkeyPatch,
    transport: FakeTransport,
    *,
    timeout_seconds: float = 42,
) -> OllamaStructuredModelClient:
    monkeypatch.setattr(ollama_module, "_LoopbackHttpTransport", lambda: transport)
    return OllamaStructuredModelClient(
        MODEL_ID,
        approval=approval(),
        timeout_seconds=timeout_seconds,
    )


def http_response(
    body: bytes,
    *,
    status: int = 200,
    headers: tuple[tuple[str, str], ...] = (),
) -> bytes:
    reason = "OK" if status == 200 else "Redirect"
    header_lines = [f"HTTP/1.1 {status} {reason}", f"Content-Length: {len(body)}"]
    header_lines.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii") + body


def transport_with(
    fake_socket: FakeSocket,
    *,
    clock: Callable[[], float] | None = None,
) -> tuple[_LoopbackHttpTransport, list[tuple[tuple[str, int], float]]]:
    calls: list[tuple[tuple[str, int], float]] = []

    def factory(address: tuple[str, int], *, timeout: float) -> FakeSocket:
        calls.append((address, timeout))
        return fake_socket

    return (
        _LoopbackHttpTransport(
            socket_factory=factory,
            monotonic=clock or (lambda: 0.0),
        ),
        calls,
    )


def test_ollama_adapter_posts_schema_to_fixed_loopback_without_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport(response('{"answer":"bounded"}', prompt_eval_count=31, eval_count=7))
    client = client_with(monkeypatch, transport)
    terminal_output = io.StringIO()
    with terminal_lifecycle_logging(stream=terminal_output):
        result = client.parse(
            system_prompt="You are a bounded role.",
            input_value=Input(value="frozen"),
            output_type=Output,
        )

    assert result == Output(answer="bounded")
    assert OLLAMA_CHAT_URL == "http://127.0.0.1:11434/api/chat"
    assert OLLAMA_TAGS_URL == "http://127.0.0.1:11434/api/tags"
    assert [request["path"] for request in transport.requests] == [
        "/api/tags",
        "/api/chat",
        "/api/tags",
    ]
    chat_request = transport.requests[1]
    assert chat_request["payload"] is not None
    sent = json.loads(chat_request["payload"])
    assert sent["model"] == MODEL_ID
    assert sent["stream"] is False
    assert sent["think"] is False
    assert sent["options"] == {"temperature": 0}
    assert sent["format"] == _project_ollama_schema(Output.model_json_schema(mode="validation"))
    assert sent["messages"] == [
        {"role": "system", "content": "You are a bounded role."},
        {"role": "user", "content": '{"value":"frozen"}'},
    ]
    assert "tools" not in sent
    assert "transport" not in inspect.signature(OllamaStructuredModelClient).parameters
    assert client.provider == "ollama"
    assert client.execution_boundary == "local_loopback"
    assert client.model_revision == "sha256:" + REVISION_A
    assert client.live_invocation is False
    assert client.store_false_sent is False
    assert client.last_usage is not None
    assert client.last_usage.provider_usage_reported is True
    assert client.last_usage.input_tokens == 31
    assert client.last_usage.output_tokens == 7
    assert client.last_usage.total_tokens == 38
    lifecycle_log = terminal_output.getvalue()
    ordered_events = (
        'event=ollama.inventory.started phase="before_generation"',
        'event=ollama.inventory.completed phase="before_generation"',
        "event=ollama.generation.started",
        "event=ollama.generation.completed",
        'event=ollama.inventory.started phase="after_generation"',
        'event=ollama.inventory.completed phase="after_generation"',
    )
    positions = [lifecycle_log.index(event) for event in ordered_events]
    assert positions == sorted(positions)
    assert "You are a bounded role" not in lifecycle_log
    assert '"value":"frozen"' not in lifecycle_log
    assert '"answer":"bounded"' not in lifecycle_log


def test_ollama_schema_projection_preserves_structure_and_property_names() -> None:
    schema = _project_ollama_schema(ConstrainedOutput.model_json_schema(mode="validation"))

    assert schema["type"] == "object"
    assert "title" not in schema
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["nested", "state", "fixed"]
    assert schema["properties"]["nested"] == {"$ref": "#/$defs/NestedConstrainedOutput"}
    assert schema["properties"]["state"] == {
        "enum": ["ready", "blocked"],
        "type": "string",
    }
    assert schema["properties"]["fixed"] == {"const": True, "type": "boolean"}
    nested = schema["$defs"]["NestedConstrainedOutput"]
    assert "title" not in nested
    assert set(nested["properties"]) == {"title", "pattern", "minimum", "tags"}
    assert nested["properties"]["title"] == {
        "pattern": "^[a-z]+$",
        "type": "string",
    }
    assert nested["properties"]["pattern"] == {"type": "string"}
    assert nested["properties"]["minimum"] == {"type": "integer"}
    assert nested["properties"]["tags"] == {
        "items": {"type": "string"},
        "maxItems": 2,
        "minItems": 1,
        "type": "array",
    }
    assert nested["required"] == ["title", "minimum", "tags"]
    assert nested["additionalProperties"] is False


def test_ollama_schema_projection_preserves_current_discriminated_union_shape() -> None:
    schema = _project_ollama_schema(EngineerModelOutcome.model_json_schema(mode="validation"))

    result = schema["properties"]["result"]
    assert result["oneOf"] == [
        {"$ref": "#/$defs/EngineerFilePlanOutcome"},
        {"$ref": "#/$defs/EngineerInterventionOutcome"},
    ]
    assert "discriminator" not in result
    assert schema["$defs"]["EngineerFilePlanOutcome"]["properties"]["kind"] == {
        "const": "file_plan",
        "type": "string",
    }


def test_ollama_file_plan_only_schema_excludes_intervention_branch() -> None:
    schema = _project_ollama_schema(EngineerFilePlanOutcome.model_json_schema(mode="validation"))

    encoded = json.dumps(schema, sort_keys=True)
    assert '"const": "file_plan"' in encoded
    assert "decision_required" not in encoded
    assert "intervention" not in encoded


def test_ollama_schema_projection_preserves_architect_conversation_states() -> None:
    schema = _project_ollama_schema(ArchitectConversationReply.model_json_schema(mode="validation"))

    assert schema["type"] == "object"
    clarification, ready = schema["oneOf"]
    assert clarification["properties"]["advisory_summary"] == {"type": "null"}
    assert clarification["properties"]["missing_information"]["minItems"] == 1
    assert clarification["properties"]["missing_information"]["maxItems"] == 8
    assert ready["properties"]["advisory_summary"] == {"type": "string"}
    assert ready["properties"]["missing_information"]["minItems"] == 0
    assert ready["properties"]["missing_information"]["maxItems"] == 0
    assert all(
        branch["required"]
        == [
            "status",
            "assistant_message",
            "advisory_summary",
            "missing_information",
        ]
        for branch in schema["oneOf"]
    )


def test_ollama_schema_projection_preserves_architect_risk_pair_correlation() -> None:
    source = ArchitectManifestProposal.model_json_schema(mode="validation")
    schema = _project_ollama_schema(source)

    source_risk = source["$defs"]["ArchitectRiskObservation"]
    projected_risk = schema["$defs"]["ArchitectRiskObservation"]
    assert projected_risk["oneOf"] == source_risk["oneOf"]
    assert projected_risk["properties"]["hazard_reason"]["oneOf"] == [
        {"$ref": "#/$defs/RiskReason"},
        {"type": "null"},
    ]


def test_ollama_architect_conversation_rejects_cross_state_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = json.dumps(
        {
            "status": "ready_to_launch",
            "assistant_message": "Ready.",
            "advisory_summary": None,
            "missing_information": [],
        }
    )
    transport = FakeTransport(response(content))
    client = client_with(monkeypatch, transport)
    terminal_output = io.StringIO()

    with terminal_lifecycle_logging(stream=terminal_output):
        with pytest.raises(ModelOutputError, match="failed schema validation"):
            client.parse(
                system_prompt="bounded",
                input_value=Input(value="x"),
                output_type=ArchitectConversationReply,
            )

    sent = json.loads(transport.requests[1]["payload"])
    assert sent["format"] == _project_ollama_schema(
        ArchitectConversationReply.model_json_schema(mode="validation")
    )
    lifecycle_log = terminal_output.getvalue()
    assert "event=ollama.output.rejected" in lifecycle_log
    assert 'phase="schema_validation"' in lifecycle_log
    assert "validation_errors=1" in lifecycle_log
    assert "Ready." not in lifecycle_log


def test_ollama_request_uses_projection_but_full_model_still_rejects_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = json.dumps(
        {
            "nested": {
                "title": "x",
                "pattern": "safe",
                "minimum": 99,
                "tags": ["x"],
            },
            "state": "ready",
            "fixed": True,
        }
    )
    transport = FakeTransport(response(content))
    client = client_with(monkeypatch, transport)

    with pytest.raises(ModelOutputError, match="failed schema validation"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="x"),
            output_type=ConstrainedOutput,
        )

    sent = json.loads(transport.requests[1]["payload"])
    assert sent["format"] == _project_ollama_schema(
        ConstrainedOutput.model_json_schema(mode="validation")
    )
    assert sent["format"] != ConstrainedOutput.model_json_schema(mode="validation")
    nested = sent["format"]["$defs"]["NestedConstrainedOutput"]
    assert nested["properties"]["title"] == {
        "pattern": "^[a-z]+$",
        "type": "string",
    }
    assert nested["properties"]["pattern"] == {"type": "string"}
    assert nested["properties"]["minimum"] == {"type": "integer"}
    assert nested["properties"]["tags"] == {
        "items": {"type": "string"},
        "maxItems": 2,
        "minItems": 1,
        "type": "array",
    }


def test_local_model_record_binds_alias_digest_and_retains_approval_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = client_with(monkeypatch, FakeTransport(response('{"answer":"ok"}')))
    input_value = Input(value="frozen")
    output_value = client.parse(
        system_prompt="bounded",
        input_value=input_value,
        output_type=Output,
    )

    record = model_call_record(
        client,
        agent_version="role/v1",
        agent_definition_digest="sha256:" + "a" * 64,
        system_prompt="bounded",
        input_value=input_value,
        output_value=output_value,
    )

    assert record.execution_boundary == "local_loopback"
    assert record.model_revision == "sha256:" + REVISION_A
    assert record.live_invocation is False
    assert record.store_false_sent is False
    assert record.live_approval == approval()
    assert record.usage is not None
    assert record.usage.provider_usage_reported is False


def test_ollama_adapter_rejects_alias_digest_drift_during_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = client_with(
        monkeypatch,
        FakeTransport(response('{"answer":"ok"}'), revisions=[REVISION_A, REVISION_B]),
    )

    with pytest.raises(ModelConfigurationError, match="revision changed"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="x"),
            output_type=Output,
        )
    assert client.model_revision is None


@pytest.mark.parametrize(
    "content",
    [
        '{"answer":"first","answer":"second"}',
        '{"answer":1}',
        '{"answer":"ok","extra":"unauthorized"}',
        "[]",
        "NaN",
    ],
)
def test_ollama_adapter_rejects_duplicate_or_schema_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    client = client_with(monkeypatch, FakeTransport(response(content)))
    with pytest.raises(ModelOutputError):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="x"),
            output_type=Output,
        )


def test_ollama_adapter_rejects_invalid_outer_json_utf8_and_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_payloads = (
        b'{"done":true,"done":true,"message":{"content":"{}"}}',
        b"\xff",
        response(
            '{"answer":"ok"}',
            message={
                "role": "assistant",
                "content": '{"answer":"ok"}',
                "tool_calls": [{"function": {"name": "shell"}}],
            },
        ),
    )
    for payload in invalid_payloads:
        client = client_with(monkeypatch, FakeTransport(payload))
        with pytest.raises(ModelOutputError):
            client.parse(
                system_prompt="bounded",
                input_value=Input(value="x"),
                output_type=Output,
            )


def test_ollama_adapter_classifies_nonterminal_output_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "model": MODEL_ID,
            "done": False,
            "message": {"role": "assistant", "content": ""},
        }
    ).encode("utf-8")
    client = client_with(monkeypatch, FakeTransport(payload))
    with pytest.raises(ModelOutputError, match="response was incomplete"):
        client.parse(
            system_prompt="bounded",
            input_value=Input(value="x"),
            output_type=Output,
        )


def test_ollama_adapter_sanitizes_transport_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-do-not-leak-provider-detail"

    class ExplodingTransport:
        def request(self, *args: object, **kwargs: object) -> bytes:
            del args, kwargs
            raise RuntimeError(secret)

    monkeypatch.setattr(ollama_module, "_LoopbackHttpTransport", ExplodingTransport)
    client = OllamaStructuredModelClient(MODEL_ID, approval=approval())
    with pytest.raises(ModelRuntimeError) as caught:
        client.parse(
            system_prompt="bounded",
            input_value=Input(value=secret),
            output_type=Output,
        )
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None


def test_ollama_adapter_rejects_bad_configuration_before_transport() -> None:
    with pytest.raises(ModelConfigurationError, match="model_id"):
        OllamaStructuredModelClient("\n", approval=approval())
    with pytest.raises(ModelConfigurationError, match="timeout"):
        OllamaStructuredModelClient(MODEL_ID, approval=approval(), timeout_seconds=0)
    with pytest.raises(ModelConfigurationError, match="approval"):
        OllamaStructuredModelClient(MODEL_ID, approval=None)  # type: ignore[arg-type]


def test_private_transport_uses_fixed_numeric_loopback_and_no_redirects() -> None:
    payload = inventory(REVISION_A)
    fake_socket = FakeSocket([http_response(payload)])
    transport, calls = transport_with(fake_socket)

    result = transport.request(
        path="/api/tags",
        payload=None,
        timeout_seconds=5,
        maximum_response_bytes=1024,
    )

    assert result == payload
    assert calls == [(("127.0.0.1", 11434), 5.0)]
    assert fake_socket.sent.startswith(b"GET /api/tags HTTP/1.1\r\n")
    assert b"Host: 127.0.0.1:11434\r\n" in fake_socket.sent
    assert b"Connection: close\r\n" in fake_socket.sent
    assert fake_socket.closed is True

    redirect_socket = FakeSocket(
        [http_response(b"", status=302, headers=(("Location", "https://example.com"),))]
    )
    redirect_transport, redirect_calls = transport_with(redirect_socket)
    with pytest.raises(ModelRuntimeError, match="non-success"):
        redirect_transport.request(
            path="/api/tags",
            payload=None,
            timeout_seconds=5,
            maximum_response_bytes=1024,
        )
    assert len(redirect_calls) == 1


def test_private_transport_enforces_body_limit_and_decodes_chunked_response() -> None:
    oversized_socket = FakeSocket([http_response(b"x" * 11)])
    oversized_transport, _ = transport_with(oversized_socket)
    with pytest.raises(ModelOutputError, match="body limit"):
        oversized_transport.request(
            path="/api/tags",
            payload=None,
            timeout_seconds=5,
            maximum_response_bytes=10,
        )

    chunked = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"
    )
    chunked_socket = FakeSocket([chunked])
    chunked_transport, _ = transport_with(chunked_socket)
    assert (
        chunked_transport.request(
            path="/api/tags",
            payload=None,
            timeout_seconds=5,
            maximum_response_bytes=10,
        )
        == b"abcde"
    )


def test_private_transport_enforces_monotonic_deadline_during_slow_body() -> None:
    clock = FakeClock()
    header_and_first_byte = b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\na"
    slow_socket = FakeSocket(
        [header_and_first_byte, b"b", b"c"],
        clock=clock,
        recv_delays=[0.4, 0.7, 0.1],
    )
    transport, _ = transport_with(slow_socket, clock=clock)

    with pytest.raises(ModelRuntimeError, match="deadline"):
        transport.request(
            path="/api/tags",
            payload=None,
            timeout_seconds=1,
            maximum_response_bytes=10,
        )
    assert slow_socket.timeouts[0] <= 1
    assert slow_socket.timeouts[-1] < slow_socket.timeouts[0]
