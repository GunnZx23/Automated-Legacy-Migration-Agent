"""Tool-free structured-output adapter for a loopback Ollama runtime.

The endpoint is not configurable: model context can be sent only to the Ollama
daemon at ``127.0.0.1:11434``.  The public client has no transport-injection
parameter, so tests exercise a private transport seam without creating a
production client that can truthfully claim loopback evidence for another URL.
"""

from __future__ import annotations

import json
import re
import socket
import time
from collections.abc import Callable
from contextlib import closing
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ValidationError

from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
    ModelExecutionBoundary,
    ModelOutputError,
    ModelRuntimeError,
    ModelUsageEvidence,
    OutputModel,
)
from legacy_migration_agent.core.integrity import canonical_json_bytes
from legacy_migration_agent.core.observability import lifecycle_event

_OLLAMA_HOST = "127.0.0.1"
_OLLAMA_PORT = 11434
_CHAT_PATH: Literal["/api/chat"] = "/api/chat"
_TAGS_PATH: Literal["/api/tags"] = "/api/tags"
OLLAMA_CHAT_URL = f"http://{_OLLAMA_HOST}:{_OLLAMA_PORT}{_CHAT_PATH}"
OLLAMA_TAGS_URL = f"http://{_OLLAMA_HOST}:{_OLLAMA_PORT}{_TAGS_PATH}"
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 180.0
MIN_OLLAMA_TIMEOUT_SECONDS = 1.0
MAX_OLLAMA_TIMEOUT_SECONDS = 900.0
_MAX_PROMPT_BYTES = 256 * 1024
_MAX_INPUT_BYTES = 2 * 1024 * 1024
_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_HEADER_BYTES = 64 * 1024
_MAX_CHUNK_OVERHEAD_BYTES = 1024 * 1024
_READ_SIZE = 64 * 1024
_MODEL_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HEADER_NAME = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

# Ollama 0.32.x compiles the supplied schema into a generation grammar.  Its
# accepted JSON-Schema vocabulary is narrower than Pydantic's emitted schema,
# so send only the structural keywords needed to describe the JSON shape.
# String-length, numeric-range, default, title, and description keywords
# deliberately remain absent here. Pattern and array-cardinality constraints
# are retained because live Ollama 0.32.5 grammar probes accept them. The
# complete Pydantic model is still applied to returned JSON after generation.
_OLLAMA_SCHEMA_SCALARS = (
    "type",
    "$ref",
    "enum",
    "const",
    "pattern",
    "minItems",
    "maxItems",
)
_OLLAMA_SCHEMA_MAPPINGS = ("properties", "$defs")
_OLLAMA_SCHEMA_ALTERNATIVES = ("oneOf",)


class _SocketLike(Protocol):
    def settimeout(self, value: float) -> None: ...

    def send(self, data: memoryview) -> int: ...

    def recv(self, maximum: int) -> bytes: ...

    def close(self) -> None: ...


class _OllamaTransport(Protocol):
    def request(
        self,
        *,
        path: Literal["/api/chat", "/api/tags"],
        payload: bytes | None,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> bytes: ...


class _LoopbackHttpTransport:
    """Minimal direct HTTP/1.1 transport with one monotonic wall deadline.

    It connects to the numeric loopback address directly, so environment proxy
    settings, DNS, redirects, and alternate hosts cannot affect the boundary.
    The private factory and clock parameters exist only for transport tests;
    :class:`OllamaStructuredModelClient` always constructs production defaults.
    """

    def __init__(
        self,
        *,
        socket_factory: Callable[..., _SocketLike] = socket.create_connection,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._socket_factory = socket_factory
        self._monotonic = monotonic

    def request(
        self,
        *,
        path: Literal["/api/chat", "/api/tags"],
        payload: bytes | None,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> bytes:
        if path not in {_CHAT_PATH, _TAGS_PATH}:
            raise ModelConfigurationError("local Ollama request path is not allowed")
        if path == _CHAT_PATH and payload is None:
            raise ModelConfigurationError("local Ollama chat request requires a body")
        if path == _TAGS_PATH and payload is not None:
            raise ModelConfigurationError("local Ollama inventory request cannot carry a body")
        deadline = self._monotonic() + timeout_seconds
        method = "POST" if payload is not None else "GET"
        body = payload or b""
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: {_OLLAMA_HOST}:{_OLLAMA_PORT}\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n"
            + ("Content-Type: application/json; charset=utf-8\r\n" if payload is not None else "")
            + f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("ascii") + body

        try:
            connection_timeout = _remaining(deadline, self._monotonic)
            connection = self._socket_factory(
                (_OLLAMA_HOST, _OLLAMA_PORT),
                timeout=connection_timeout,
            )
            with closing(connection):
                _send_with_deadline(connection, request, deadline, self._monotonic)
                return _receive_http_response(
                    connection,
                    deadline=deadline,
                    monotonic=self._monotonic,
                    maximum_response_bytes=maximum_response_bytes,
                )
        except (ModelConfigurationError, ModelOutputError, ModelRuntimeError):
            raise
        except (TimeoutError, OSError):
            raise ModelRuntimeError("local Ollama provider is unavailable") from None


class OllamaStructuredModelClient:
    """Strict JSON-Schema client with revision checks around one local call.

    Ollama accepts a mutable alias rather than an immutable digest reference.
    The adapter detects ordinary alias drift before, during, and between role
    calls, but the observed digest is not an atomic attestation from the chat
    response itself.
    """

    provider = "ollama"
    live_invocation = False
    store_false_sent = False
    execution_boundary: ModelExecutionBoundary = "local_loopback"

    def __init__(
        self,
        model_id: str,
        *,
        approval: LiveModelApproval,
        timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    ) -> None:
        normalized_model = model_id.strip()
        if not normalized_model or len(normalized_model) > 300:
            raise ModelConfigurationError("an explicit model_id is required")
        if any(character in normalized_model for character in ("\x00", "\r", "\n")):
            raise ModelConfigurationError("model_id contains a forbidden control character")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ModelConfigurationError("Ollama timeout must be a number")
        normalized_timeout = float(timeout_seconds)
        if not MIN_OLLAMA_TIMEOUT_SECONDS <= normalized_timeout <= MAX_OLLAMA_TIMEOUT_SECONDS:
            raise ModelConfigurationError("Ollama timeout is outside the supported range")
        if not isinstance(approval, LiveModelApproval):
            raise ModelConfigurationError("local model use requires explicit approval")
        self._model_id = normalized_model
        self._timeout_seconds = normalized_timeout
        self._transport: _OllamaTransport = _LoopbackHttpTransport()
        self._live_approval = LiveModelApproval.model_validate(approval.model_dump(mode="python"))
        self._last_usage: ModelUsageEvidence | None = None
        self._model_revision: str | None = None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_revision(self) -> str | None:
        return self._model_revision

    @property
    def live_approval(self) -> LiveModelApproval:
        return self._live_approval

    @property
    def last_usage(self) -> ModelUsageEvidence | None:
        return self._last_usage

    def bind_model_revision(self, expected_revision: str) -> None:
        """Bind only immutable evidence supplied by the trusted run loader."""

        if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_revision) is None:
            raise ModelConfigurationError("recorded local model revision is invalid")
        current = self._resolve_model_revision(timeout_seconds=self._timeout_seconds)
        if current != expected_revision:
            raise ModelConfigurationError("local Ollama model revision changed")
        if self._model_revision is not None and self._model_revision != expected_revision:
            raise ModelConfigurationError("local Ollama model revision changed")
        self._model_revision = expected_revision

    def parse(
        self,
        *,
        system_prompt: str,
        input_value: BaseModel,
        output_type: type[OutputModel],
    ) -> OutputModel:
        if not isinstance(input_value, BaseModel):
            raise TypeError("structured model input must be a Pydantic model")
        if not isinstance(output_type, type) or not issubclass(output_type, BaseModel):
            raise TypeError("output_type must be a Pydantic model class")
        if not system_prompt.strip() or "\x00" in system_prompt:
            raise ModelConfigurationError("system prompt must be non-empty text without NUL bytes")
        prompt_bytes = system_prompt.encode("utf-8")
        if len(prompt_bytes) > _MAX_PROMPT_BYTES:
            raise ModelConfigurationError("system prompt exceeds the local model size limit")
        input_bytes = canonical_json_bytes(input_value)
        if len(input_bytes) > _MAX_INPUT_BYTES:
            raise ModelConfigurationError("structured model input exceeds the local size limit")

        request_value = {
            "model": self.model_id,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_bytes.decode("utf-8")},
            ],
            "format": _project_ollama_schema(output_type.model_json_schema(mode="validation")),
            "options": {"temperature": 0},
        }
        request_bytes = _canonical_json(request_value)
        if len(request_bytes) > _MAX_REQUEST_BYTES:
            raise ModelConfigurationError("local Ollama request exceeds the body limit")

        deadline = time.monotonic() + self._timeout_seconds
        self._last_usage = None
        started_ns = time.perf_counter_ns()
        inventory_started_ns = time.perf_counter_ns()
        lifecycle_event("ollama.inventory.started", phase="before_generation")
        before_revision = self._resolve_model_revision(
            timeout_seconds=_remaining(deadline, time.monotonic)
        )
        lifecycle_event(
            "ollama.inventory.completed",
            phase="before_generation",
            elapsed_ms=_elapsed_milliseconds(inventory_started_ns),
        )
        if self._model_revision is not None and before_revision != self._model_revision:
            raise ModelConfigurationError("local Ollama model revision changed")
        generation_started_ns = time.perf_counter_ns()
        lifecycle_event("ollama.generation.started")
        response_bytes = self._safe_request(
            path=_CHAT_PATH,
            payload=request_bytes,
            timeout_seconds=_remaining(deadline, time.monotonic),
        )
        lifecycle_event(
            "ollama.generation.completed",
            elapsed_ms=_elapsed_milliseconds(generation_started_ns),
        )
        inventory_started_ns = time.perf_counter_ns()
        lifecycle_event("ollama.inventory.started", phase="after_generation")
        after_revision = self._resolve_model_revision(
            timeout_seconds=_remaining(deadline, time.monotonic)
        )
        lifecycle_event(
            "ollama.inventory.completed",
            phase="after_generation",
            elapsed_ms=_elapsed_milliseconds(inventory_started_ns),
        )
        if after_revision != before_revision:
            raise ModelConfigurationError("local Ollama model revision changed")
        self._model_revision = before_revision
        elapsed_ns = time.perf_counter_ns() - started_ns

        response = _decode_json_object(response_bytes, role="response")
        self._last_usage = _usage_evidence(response, elapsed_ns)
        response_model = response.get("model")
        if not isinstance(response_model, str) or response_model not in _model_aliases(
            self.model_id
        ):
            raise ModelOutputError("local Ollama response model identity did not match the request")
        if response.get("done") is not True:
            raise ModelOutputError("model response was incomplete")
        done_reason = response.get("done_reason")
        if done_reason not in (None, "stop"):
            raise ModelOutputError("model response was incomplete")
        message = response.get("message")
        if not isinstance(message, dict):
            raise ModelOutputError("local Ollama response did not contain a message")
        if message.get("tool_calls") not in (None, []):
            raise ModelOutputError("local Ollama response attempted an unauthorized tool call")
        content = message.get("content")
        if not isinstance(content, str):
            raise ModelOutputError("local Ollama response did not contain structured text")
        try:
            content_bytes = content.encode("utf-8")
        except UnicodeEncodeError as exc:  # pragma: no cover - defensive for lone surrogates
            raise ModelOutputError("local Ollama structured text was not valid UTF-8") from exc
        if len(content_bytes) > _MAX_RESPONSE_BYTES:
            raise ModelOutputError("local Ollama structured output exceeded the body limit")
        parsed = _decode_json_object(content_bytes, role="structured output")
        try:
            return output_type.model_validate_json(_canonical_json(parsed), strict=True)
        except ValidationError as exc:
            lifecycle_event(
                "ollama.output.rejected",
                phase="schema_validation",
                validation_errors=exc.error_count(),
            )
            raise ModelOutputError("model structured output failed schema validation") from exc

    def _resolve_model_revision(self, *, timeout_seconds: float) -> str:
        response = _decode_json_object(
            self._safe_request(
                path=_TAGS_PATH,
                payload=None,
                timeout_seconds=timeout_seconds,
            ),
            role="inventory response",
        )
        models = response.get("models")
        if not isinstance(models, list):
            raise ModelOutputError("local Ollama inventory did not contain a model list")
        aliases = _model_aliases(self.model_id)
        digests: set[str] = set()
        for item in models:
            if not isinstance(item, dict):
                raise ModelOutputError("local Ollama inventory contained an invalid model entry")
            names = {item.get("name"), item.get("model")}
            if not aliases.intersection(name for name in names if isinstance(name, str)):
                continue
            raw_digest = item.get("digest")
            if not isinstance(raw_digest, str) or _MODEL_DIGEST.fullmatch(raw_digest) is None:
                raise ModelOutputError("local Ollama inventory contained an invalid model digest")
            digests.add("sha256:" + raw_digest)
        if len(digests) != 1:
            raise ModelConfigurationError("local Ollama model alias is missing or ambiguous")
        return next(iter(digests))

    def _safe_request(
        self,
        *,
        path: Literal["/api/chat", "/api/tags"],
        payload: bytes | None,
        timeout_seconds: float,
    ) -> bytes:
        try:
            response = self._transport.request(
                path=path,
                payload=payload,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=_MAX_RESPONSE_BYTES,
            )
        except (ModelConfigurationError, ModelOutputError, ModelRuntimeError):
            raise
        except Exception:
            raise ModelRuntimeError("local Ollama provider is unavailable") from None
        if not isinstance(response, bytes):
            raise ModelOutputError("local Ollama response body was not bytes")
        if len(response) > _MAX_RESPONSE_BYTES:
            raise ModelOutputError("local Ollama response exceeded the body limit")
        return response


def _project_ollama_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project a Pydantic schema onto Ollama's structural grammar subset.

    Property and definition names are data, not schema keywords, so they are
    retained verbatim even when a name such as ``title`` or ``pattern`` matches
    a keyword that is intentionally removed from schema nodes.
    """

    projected: dict[str, Any] = {}
    for keyword in _OLLAMA_SCHEMA_SCALARS:
        if keyword in schema:
            projected[keyword] = schema[keyword]

    required = schema.get("required")
    if required is not None:
        projected["required"] = required

    for keyword in _OLLAMA_SCHEMA_MAPPINGS:
        value = schema.get(keyword)
        if value is None:
            continue
        if not isinstance(value, dict):  # pragma: no cover - Pydantic invariant
            raise ModelConfigurationError("Pydantic produced an invalid JSON Schema")
        projected[keyword] = {
            name: _project_ollama_schema_node(child) for name, child in value.items()
        }

    items = schema.get("items")
    if items is not None:
        projected["items"] = _project_ollama_schema_node(items)

    additional = schema.get("additionalProperties")
    if additional is not None:
        projected["additionalProperties"] = _project_ollama_schema_node(additional)

    for keyword in _OLLAMA_SCHEMA_ALTERNATIVES:
        alternatives = schema.get(keyword)
        if alternatives is None:
            continue
        if not isinstance(alternatives, list):  # pragma: no cover - Pydantic invariant
            raise ModelConfigurationError("Pydantic produced an invalid JSON Schema")
        projected[keyword] = [
            _project_ollama_schema_node(alternative) for alternative in alternatives
        ]
    return projected


def _project_ollama_schema_node(schema: object) -> object:
    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, dict):  # pragma: no cover - Pydantic invariant
        raise ModelConfigurationError("Pydantic produced an invalid JSON Schema")
    if any(not isinstance(key, str) for key in schema):  # pragma: no cover - defensive
        raise ModelConfigurationError("Pydantic produced an invalid JSON Schema")
    return _project_ollama_schema(cast(dict[str, Any], schema))


def _remaining(deadline: float, monotonic: Callable[[], float]) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise ModelRuntimeError("local Ollama request exceeded its deadline")
    return remaining


def _elapsed_milliseconds(started_ns: int) -> int:
    return max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)


def _send_with_deadline(
    connection: _SocketLike,
    payload: bytes,
    deadline: float,
    monotonic: Callable[[], float],
) -> None:
    view = memoryview(payload)
    sent = 0
    while sent < len(view):
        connection.settimeout(_remaining(deadline, monotonic))
        count = connection.send(view[sent:])
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ModelRuntimeError("local Ollama provider closed during request")
        sent += count
        _remaining(deadline, monotonic)


def _receive_http_response(
    connection: _SocketLike,
    *,
    deadline: float,
    monotonic: Callable[[], float],
    maximum_response_bytes: int,
) -> bytes:
    wire = bytearray()
    header_end = -1
    while header_end < 0:
        if len(wire) > _MAX_HEADER_BYTES:
            raise ModelOutputError("local Ollama response headers exceeded the limit")
        wire.extend(_receive_chunk(connection, deadline, monotonic))
        header_end = wire.find(b"\r\n\r\n")
    if header_end > _MAX_HEADER_BYTES:
        raise ModelOutputError("local Ollama response headers exceeded the limit")
    status, headers = _parse_response_headers(bytes(wire[:header_end]))
    if status != 200:
        raise ModelRuntimeError("local Ollama provider returned a non-success status")
    body = bytearray(wire[header_end + 4 :])
    transfer_encoding = headers.get("transfer-encoding")
    content_length = headers.get("content-length")
    if transfer_encoding is not None and content_length is not None:
        raise ModelOutputError("local Ollama response used conflicting body framing")
    if transfer_encoding is not None:
        if transfer_encoding.lower() != "chunked":
            raise ModelOutputError("local Ollama response used unsupported transfer encoding")
        while True:
            decoded = _decode_chunked_body(bytes(body), maximum_response_bytes)
            if decoded is not None:
                return decoded
            if len(body) > maximum_response_bytes + _MAX_CHUNK_OVERHEAD_BYTES:
                raise ModelOutputError("local Ollama response exceeded the body limit")
            body.extend(_receive_chunk(connection, deadline, monotonic))
    if content_length is not None:
        if "," in content_length or not content_length.isascii():
            raise ModelOutputError("local Ollama response declared an invalid body size")
        try:
            expected = int(content_length)
        except ValueError as exc:
            raise ModelOutputError("local Ollama response declared an invalid body size") from exc
        if expected < 0 or expected > maximum_response_bytes:
            raise ModelOutputError("local Ollama response exceeded the body limit")
        while len(body) < expected:
            body.extend(_receive_chunk(connection, deadline, monotonic))
        if len(body) != expected:
            raise ModelOutputError("local Ollama response body length was invalid")
        return bytes(body)
    while True:
        if len(body) > maximum_response_bytes:
            raise ModelOutputError("local Ollama response exceeded the body limit")
        chunk = _receive_chunk(connection, deadline, monotonic, allow_eof=True)
        if not chunk:
            return bytes(body)
        body.extend(chunk)


def _receive_chunk(
    connection: _SocketLike,
    deadline: float,
    monotonic: Callable[[], float],
    *,
    allow_eof: bool = False,
) -> bytes:
    connection.settimeout(_remaining(deadline, monotonic))
    try:
        chunk = connection.recv(_READ_SIZE)
    except TimeoutError:
        raise ModelRuntimeError("local Ollama request exceeded its deadline") from None
    _remaining(deadline, monotonic)
    if not isinstance(chunk, bytes):
        raise ModelOutputError("local Ollama response body was not bytes")
    if not chunk and not allow_eof:
        raise ModelOutputError("local Ollama response ended before its body was complete")
    return chunk


def _parse_response_headers(payload: bytes) -> tuple[int, dict[str, str]]:
    lines = payload.split(b"\r\n")
    if not lines or len(lines[0]) > 1024:
        raise ModelOutputError("local Ollama response status line was invalid")
    parts = lines[0].split(b" ", 2)
    if len(parts) < 2 or parts[0] not in {b"HTTP/1.0", b"HTTP/1.1"}:
        raise ModelOutputError("local Ollama response status line was invalid")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise ModelOutputError("local Ollama response status line was invalid") from exc
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"} or b":" not in line:
            raise ModelOutputError("local Ollama response contained invalid headers")
        name, raw_value = line.split(b":", 1)
        if _HEADER_NAME.fullmatch(name) is None:
            raise ModelOutputError("local Ollama response contained invalid headers")
        key = name.decode("ascii").lower()
        if key in headers:
            raise ModelOutputError("local Ollama response contained duplicate headers")
        headers[key] = raw_value.decode("latin-1").strip()
    return status, headers


def _decode_chunked_body(payload: bytes, maximum: int) -> bytes | None:
    position = 0
    decoded = bytearray()
    while True:
        line_end = payload.find(b"\r\n", position)
        if line_end < 0:
            return None
        size_line = payload[position:line_end].split(b";", 1)[0]
        if not size_line or len(size_line) > 16:
            raise ModelOutputError("local Ollama response had invalid chunk framing")
        try:
            size = int(size_line, 16)
        except ValueError as exc:
            raise ModelOutputError("local Ollama response had invalid chunk framing") from exc
        position = line_end + 2
        if size == 0:
            trailer_end = payload.find(b"\r\n\r\n", position)
            if payload[position : position + 2] == b"\r\n":
                consumed = position + 2
            elif trailer_end >= 0:
                consumed = trailer_end + 4
            else:
                return None
            if consumed != len(payload):
                raise ModelOutputError("local Ollama response had trailing chunk data")
            return bytes(decoded)
        end = position + size
        if end + 2 > len(payload):
            return None
        if payload[end : end + 2] != b"\r\n":
            raise ModelOutputError("local Ollama response had invalid chunk framing")
        decoded.extend(payload[position:end])
        if len(decoded) > maximum:
            raise ModelOutputError("local Ollama response exceeded the body limit")
        position = end + 2


def _model_aliases(model_id: str) -> set[str]:
    aliases = {model_id}
    final_segment = model_id.rsplit("/", 1)[-1]
    if ":" not in final_segment:
        aliases.add(model_id + ":latest")
    return aliases


def _decode_json_object(payload: bytes, *, role: str) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ModelOutputError(f"local Ollama {role} was not valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise ModelOutputError(f"local Ollama {role} was not valid JSON") from exc
    if not isinstance(value, dict):
        raise ModelOutputError(f"local Ollama {role} was not a JSON object")
    return cast(dict[str, Any], value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _usage_evidence(response: dict[str, Any], elapsed_ns: int) -> ModelUsageEvidence:
    latency_ms = max(0, elapsed_ns // 1_000_000)
    raw_input = response.get("prompt_eval_count")
    raw_output = response.get("eval_count")
    if raw_input is None and raw_output is None:
        return ModelUsageEvidence(latency_ms=latency_ms, provider_usage_reported=False)
    input_tokens = _token_count(raw_input)
    output_tokens = _token_count(raw_output)
    return ModelUsageEvidence(
        latency_ms=latency_ms,
        provider_usage_reported=True,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _token_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelOutputError("local Ollama response usage contains an invalid token count")
    return value
