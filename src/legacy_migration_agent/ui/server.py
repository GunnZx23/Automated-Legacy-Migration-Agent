"""Loopback-only HTTP transport for the conversational agent UI."""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import socket
import time
import webbrowser
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from pydantic import BaseModel

from legacy_migration_agent.agent_runtime.ollama_model import DEFAULT_OLLAMA_TIMEOUT_SECONDS
from legacy_migration_agent.core.observability import (
    lifecycle_event,
    terminal_lifecycle_logging,
)
from legacy_migration_agent.ui.service import AgentUiError, AgentUiService

_MAX_REQUEST_BYTES = 16 * 1024
_FINAL_REVIEW_WINDOW = timedelta(days=14)
_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_SESSION_PREFIX = "/api/sessions/"
_CONVERSATION_PREFIX = "/api/conversations/"
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'; object-src 'none'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_REQUEST_ERROR_MESSAGES = {
    "bad_request": "The request is invalid.",
    "csrf_required": "The request could not be authorized.",
    "duplicate_json_key": "The JSON request contains a duplicate field.",
    "internal_error": "The UI request could not be completed.",
    "invalid_content_length": "The request body length is invalid.",
    "invalid_content_type": "The request must use application/json with UTF-8.",
    "invalid_host": "The request host is not allowed.",
    "invalid_json": "The request body is not valid JSON.",
    "invalid_origin": "The request origin is not allowed.",
    "method_not_allowed": "That HTTP method is not allowed.",
    "not_found": "The requested UI resource was not found.",
    "payload_too_large": "The request body is too large.",
    "transfer_encoding_not_allowed": "Transfer-encoded request bodies are not allowed.",
}


class _RequestError(Exception):
    def __init__(self, status: HTTPStatus, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(code)


class _DuplicateJsonKey(ValueError):
    pass


class _AgentUiThreadingHTTPServer(ThreadingHTTPServer):
    """IPv4 server carrying immutable transport configuration and service state."""

    address_family = socket.AF_INET
    daemon_threads = True

    def __init__(
        self,
        project_root: Path,
        port: int,
        *,
        ollama_model_id: str,
        ollama_timeout_seconds: float,
    ) -> None:
        self.ui_service = AgentUiService(
            project_root,
            ollama_model_id=ollama_model_id,
            ollama_timeout_seconds=ollama_timeout_seconds,
        )
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), _AgentUiRequestHandler)


class _AgentUiRequestHandler(BaseHTTPRequestHandler):
    """Serve the fixed agent API and packaged static assets."""

    protocol_version = "HTTP/1.1"
    server_version = "LegacyMigrationAgentUI"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._respond_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Keep unknown-method failures inside the public JSON API contract."""

        if code == HTTPStatus.NOT_IMPLEMENTED:
            self._respond_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed")
            return
        super().send_error(code, message, explain)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default logging so request input is never echoed to the console."""

    @property
    def _ui_server(self) -> _AgentUiThreadingHTTPServer:
        return cast(_AgentUiThreadingHTTPServer, self.server)

    def _dispatch(self, method: str) -> None:
        action = _normalized_http_action(method, self.path)
        started_ns = time.perf_counter_ns()
        if method == "POST":
            lifecycle_event("ui.http.started", method=method, action=action)
        try:
            self._validate_request_context()
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                raise _RequestError(HTTPStatus.NOT_FOUND, "not_found")
            path = parsed.path
            if method == "GET":
                self._get(path)
            else:
                self._post(path)
            if method == "POST":
                lifecycle_event(
                    "ui.http.completed",
                    method=method,
                    action=action,
                    status=(
                        HTTPStatus.CREATED.value
                        if action
                        in {
                            "conversation.create",
                            "conversation.launch",
                            "final_review.request",
                        }
                        else 200
                    ),
                    elapsed_ms=_elapsed_milliseconds(started_ns),
                )
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except _RequestError as exc:
            lifecycle_event(
                "ui.http.failed",
                level=logging.WARNING,
                method=method,
                action=action,
                status=exc.status.value,
                public_code=exc.code,
                elapsed_ms=_elapsed_milliseconds(started_ns),
            )
            self._respond_error(exc.status, exc.code)
        except AgentUiError as exc:
            lifecycle_event(
                "ui.http.failed",
                level=logging.WARNING,
                method=method,
                action=action,
                status=HTTPStatus.BAD_REQUEST.value,
                public_code=exc.code,
                elapsed_ms=_elapsed_milliseconds(started_ns),
            )
            self._respond_agent_error(HTTPStatus.BAD_REQUEST, exc)
        except Exception as error:
            lifecycle_event(
                "ui.http.failed",
                level=logging.ERROR,
                method=method,
                action=action,
                status=HTTPStatus.INTERNAL_SERVER_ERROR.value,
                public_code="internal_error",
                error_type=type(error).__name__,
                elapsed_ms=_elapsed_milliseconds(started_ns),
            )
            self._respond_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")

    def _get(self, path: str) -> None:
        if path == "/":
            self._respond_file(_STATIC_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if path == "/assets/app.js":
            self._respond_file(
                _STATIC_ROOT / "app.js",
                "text/javascript; charset=utf-8",
            )
            return
        if path == "/assets/styles.css":
            self._respond_file(
                _STATIC_ROOT / "styles.css",
                "text/css; charset=utf-8",
            )
            return
        if path == "/api/config":
            self._respond_json(
                HTTPStatus.OK,
                {
                    "csrf_token": self._ui_server.csrf_token,
                    "request_limit_bytes": _MAX_REQUEST_BYTES,
                    "transport": "loopback_only",
                    "model": self._ui_server.ui_service.model_configuration(),
                },
            )
            return
        if path == "/api/readiness":
            self._respond_json(
                HTTPStatus.OK,
                self._ui_server.ui_service.runtime_readiness(),
            )
            return
        if path == "/api/scenarios":
            self._respond_json(
                HTTPStatus.OK,
                {"scenarios": self._ui_server.ui_service.scenarios()},
            )
            return
        if path == "/api/sessions/latest":
            latest_run = self._ui_server.ui_service.latest()
            self._respond_json(
                HTTPStatus.OK,
                {"run": None if latest_run is None else _json_value(latest_run)},
            )
            return

        conversation_route = self._parse_conversation_route(path)
        if conversation_route is not None:
            conversation_id, suffix = conversation_route
            if suffix == "":
                conversation_view = self._ui_server.ui_service.get_conversation(conversation_id)
                self._respond_json(HTTPStatus.OK, _json_value(conversation_view))
                return
            raise _RequestError(HTTPStatus.NOT_FOUND, "not_found")

        session_route = self._parse_session_route(path)
        if session_route is None:
            raise _RequestError(HTTPStatus.NOT_FOUND, "not_found")
        handle, suffix = session_route
        if suffix == "":
            run_view = self._ui_server.ui_service.get(handle)
            self._respond_json(HTTPStatus.OK, _json_value(run_view))
            return
        if suffix == "/candidate.zip":
            self._require_csrf()
            payload = self._ui_server.ui_service.candidate_zip(handle)
            self._respond_bytes(
                HTTPStatus.OK,
                payload,
                "application/zip",
                extra_headers={
                    "Content-Disposition": (
                        f'attachment; filename="migration-candidate-{handle}.zip"'
                    )
                },
            )
            return
        raise _RequestError(HTTPStatus.NOT_FOUND, "not_found")

    def _post(self, path: str) -> None:
        self._require_csrf()
        if path == "/api/conversations":
            body = self._read_json_object()
            _require_exact_fields(body, {"scenario_id"})
            scenario_id = body["scenario_id"]
            if scenario_id is not None and not isinstance(scenario_id, str):
                raise _RequestError(HTTPStatus.BAD_REQUEST, "bad_request")
            conversation_view = self._ui_server.ui_service.create_conversation(
                scenario_id=scenario_id
            )
            self._respond_json(HTTPStatus.CREATED, _json_value(conversation_view))
            return

        conversation_route = self._parse_conversation_route(path)
        if conversation_route is not None:
            conversation_id, suffix = conversation_route
            if suffix == "/messages":
                body = self._read_json_object()
                _require_exact_fields(body, {"message", "scenario_id"})
                message = body["message"]
                scenario_id = body["scenario_id"]
                if not isinstance(message, str) or (
                    scenario_id is not None and not isinstance(scenario_id, str)
                ):
                    raise _RequestError(HTTPStatus.BAD_REQUEST, "bad_request")
                conversation_view = self._ui_server.ui_service.send_conversation_message(
                    conversation_id,
                    message=message,
                    scenario_id=scenario_id,
                )
                self._respond_json(HTTPStatus.OK, _json_value(conversation_view))
                return
            if suffix == "/launch":
                body = self._read_json_object()
                _require_exact_fields(body, {"launch_token"})
                launch_token = body["launch_token"]
                if not isinstance(launch_token, str):
                    raise _RequestError(HTTPStatus.BAD_REQUEST, "bad_request")
                conversation, run = self._ui_server.ui_service.launch_conversation(
                    conversation_id,
                    launch_token=launch_token,
                )
                self._respond_json(
                    HTTPStatus.CREATED,
                    {
                        "conversation": _json_value(conversation),
                        "run": _json_value(run),
                    },
                )
                return
            raise _RequestError(HTTPStatus.NOT_FOUND, "not_found")

        session_route = self._parse_session_route(path)
        if session_route is None:
            raise _RequestError(HTTPStatus.NOT_FOUND, "not_found")
        handle, suffix = session_route
        if suffix == "/final-review/request":
            body = self._read_json_object()
            _require_exact_fields(body, {"requester", "designated_reviewer"})
            requester = body["requester"]
            designated_reviewer = body["designated_reviewer"]
            if not isinstance(requester, str) or not isinstance(designated_reviewer, str):
                raise _RequestError(HTTPStatus.BAD_REQUEST, "bad_request")
            requested_at = datetime.now(UTC)
            run_view = self._ui_server.ui_service.request_final_review(
                handle,
                requester=requester,
                designated_reviewer=designated_reviewer,
                requested_at=requested_at,
                expires_at=requested_at + _FINAL_REVIEW_WINDOW,
            )
            self._respond_json(HTTPStatus.CREATED, _json_value(run_view))
            return
        if suffix == "/final-review/decision":
            body = self._read_json_object()
            _require_exact_fields(body, {"selection", "reviewer", "comment"})
            selection = body["selection"]
            reviewer = body["reviewer"]
            comment = body["comment"]
            if (
                not isinstance(selection, str)
                or not isinstance(reviewer, str)
                or not isinstance(comment, str)
            ):
                raise _RequestError(HTTPStatus.BAD_REQUEST, "bad_request")
            run_view = self._ui_server.ui_service.decide_final_review(
                handle,
                selection=selection,
                reviewer=reviewer,
                comment=comment,
                decided_at=datetime.now(UTC),
            )
            self._respond_json(HTTPStatus.OK, _json_value(run_view))
            return
        if suffix == "/export":
            body = self._read_json_object()
            _require_exact_fields(body, set())
            result = self._ui_server.ui_service.export_candidate(handle)
            self._respond_json(HTTPStatus.OK, _json_value(result))
            return
        if suffix == "/retry":
            body = self._read_json_object()
            _require_exact_fields(body, {"correction_id", "reviewer", "comment"})
            correction_id = body["correction_id"]
            reviewer = body["reviewer"]
            comment = body["comment"]
            if (
                not isinstance(correction_id, str)
                or not isinstance(reviewer, str)
                or not isinstance(comment, str)
            ):
                raise _RequestError(HTTPStatus.BAD_REQUEST, "bad_request")
            run_view = self._ui_server.ui_service.retry(
                handle,
                correction_id=correction_id,
                reviewer=reviewer,
                comment=comment,
            )
            self._respond_json(HTTPStatus.OK, _json_value(run_view))
            return
        if suffix != "/decision":
            raise _RequestError(HTTPStatus.NOT_FOUND, "not_found")
        body = self._read_json_object()
        _require_exact_fields(body, {"selection", "reviewer", "comment"})
        selection = body["selection"]
        reviewer = body["reviewer"]
        comment = body["comment"]
        if (
            not isinstance(selection, str)
            or not isinstance(reviewer, str)
            or not isinstance(comment, str)
        ):
            raise _RequestError(HTTPStatus.BAD_REQUEST, "bad_request")
        run_view = self._ui_server.ui_service.decide(
            handle,
            selection=selection,
            reviewer=reviewer,
            comment=comment,
        )
        self._respond_json(HTTPStatus.OK, _json_value(run_view))

    def _validate_request_context(self) -> None:
        host_values = self.headers.get_all("Host", failobj=[])
        port = self._ui_server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if len(host_values) != 1 or host_values[0].lower() not in allowed_hosts:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "invalid_host")

        origin_values = self.headers.get_all("Origin", failobj=[])
        if len(origin_values) > 1:
            raise _RequestError(HTTPStatus.FORBIDDEN, "invalid_origin")
        expected_origin = f"http://{host_values[0].lower()}"
        if origin_values and origin_values[0].lower() != expected_origin:
            raise _RequestError(HTTPStatus.FORBIDDEN, "invalid_origin")

    def _require_csrf(self) -> None:
        values = self.headers.get_all("X-Agent-UI-CSRF", failobj=[])
        if len(values) != 1 or not hmac.compare_digest(values[0], self._ui_server.csrf_token):
            raise _RequestError(HTTPStatus.FORBIDDEN, "csrf_required")

    def _read_json_object(self) -> dict[str, object]:
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            raise _RequestError(
                HTTPStatus.BAD_REQUEST,
                "transfer_encoding_not_allowed",
            )
        content_lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(content_lengths) != 1:
            raise _RequestError(HTTPStatus.LENGTH_REQUIRED, "invalid_content_length")
        try:
            length = int(content_lengths[0], 10)
        except ValueError as exc:
            raise _RequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_content_length",
            ) from exc
        if length < 0:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "invalid_content_length")
        if length > _MAX_REQUEST_BYTES:
            raise _RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large")
        content_types = self.headers.get_all("Content-Type", failobj=[])
        if len(content_types) != 1 or not _is_json_content_type(content_types[0]):
            raise _RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_content_type")

        payload = self.rfile.read(length)
        try:
            decoded = payload.decode("utf-8")
            value = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
        except _DuplicateJsonKey as exc:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "duplicate_json_key") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "invalid_json") from exc
        if not isinstance(value, dict):
            raise _RequestError(HTTPStatus.BAD_REQUEST, "bad_request")
        return cast(dict[str, object], value)

    @staticmethod
    def _parse_session_route(path: str) -> tuple[str, str] | None:
        if not path.startswith(_SESSION_PREFIX):
            return None
        remainder = path.removeprefix(_SESSION_PREFIX)
        handle, separator, suffix = remainder.partition("/")
        if not _valid_handle(handle):
            return None
        return handle, f"/{suffix}" if separator else ""

    @staticmethod
    def _parse_conversation_route(path: str) -> tuple[str, str] | None:
        if not path.startswith(_CONVERSATION_PREFIX):
            return None
        remainder = path.removeprefix(_CONVERSATION_PREFIX)
        conversation_id, separator, suffix = remainder.partition("/")
        if not _valid_handle(conversation_id):
            return None
        return conversation_id, f"/{suffix}" if separator else ""

    def _respond_file(self, path: Path, content_type: str) -> None:
        try:
            payload = path.read_bytes()
        except OSError:
            raise _RequestError(HTTPStatus.NOT_FOUND, "not_found") from None
        self._respond_bytes(HTTPStatus.OK, payload, content_type)

    def _respond_json(self, status: HTTPStatus, value: object) -> None:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._respond_bytes(status, payload, "application/json; charset=utf-8")

    def _respond_error(self, status: HTTPStatus, code: str) -> None:
        safe_code = code if code in _REQUEST_ERROR_MESSAGES else "internal_error"
        message = _REQUEST_ERROR_MESSAGES[safe_code]
        self._respond_json(status, {"error": {"code": safe_code, "message": message}})

    def _respond_agent_error(self, status: HTTPStatus, error: AgentUiError) -> None:
        """Return only the service-owned stable code and its pre-sanitized message."""

        self._respond_json(
            status,
            {"error": {"code": error.code, "message": error.public_message}},
        )

    def _respond_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        for name, value in _SECURITY_HEADERS.items():
            self.send_header(name, value)
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)


def build_ui_server(
    project_root: Path,
    *,
    port: int = 0,
    ollama_model_id: str,
    ollama_timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
) -> ThreadingHTTPServer:
    """Build a loopback-only server; a zero port asks the OS for a free port."""

    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    return _AgentUiThreadingHTTPServer(
        project_root.resolve(),
        port,
        ollama_model_id=ollama_model_id,
        ollama_timeout_seconds=ollama_timeout_seconds,
    )


def serve_ui(
    project_root: Path,
    *,
    port: int = 8765,
    open_browser: bool = False,
    ollama_model_id: str,
    ollama_timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
) -> None:
    """Serve the agent UI until interrupted, optionally opening its local URL."""

    with terminal_lifecycle_logging():
        lifecycle_event(
            "ui.provider.configured",
            provider="ollama",
            model_id=ollama_model_id,
            execution_boundary="local_loopback",
            timeout_seconds=ollama_timeout_seconds,
        )
        lifecycle_event("ui.server.starting", requested_port=port)
        try:
            server = build_ui_server(
                project_root,
                port=port,
                ollama_model_id=ollama_model_id,
                ollama_timeout_seconds=ollama_timeout_seconds,
            )
        except Exception as error:
            lifecycle_event(
                "ui.server.failed",
                level=logging.ERROR,
                phase="startup",
                error_type=type(error).__name__,
            )
            raise
        actual_port = server.server_address[1]
        url = f"http://127.0.0.1:{actual_port}/"
        lifecycle_event("ui.server.ready", host="127.0.0.1", port=actual_port)
        print(f"Agent UI available at {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nAgent UI stopped.", flush=True)
        except Exception as error:
            lifecycle_event(
                "ui.server.failed",
                level=logging.ERROR,
                phase="serve",
                error_type=type(error).__name__,
            )
            raise
        finally:
            server.server_close()
            lifecycle_event("ui.server.stopped")


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _normalized_http_action(method: str, raw_path: str) -> str:
    """Classify a route without ever returning user-controlled path text."""

    try:
        path = urlsplit(raw_path).path
    except ValueError:
        return "unknown"
    if method == "GET":
        exact_get = {
            "/": "page",
            "/assets/app.js": "asset.javascript",
            "/assets/styles.css": "asset.stylesheet",
            "/api/config": "config.read",
            "/api/readiness": "provider.readiness",
            "/api/scenarios": "scenarios.read",
            "/api/sessions/latest": "session.latest",
        }
        if path in exact_get:
            return exact_get[path]
        if path.startswith(_SESSION_PREFIX) and path.endswith("/candidate.zip"):
            return "candidate.download"
        if path.startswith(_SESSION_PREFIX):
            return "session.read"
        if path.startswith(_CONVERSATION_PREFIX):
            return "conversation.read"
        return "unknown"
    if method == "POST":
        if path == "/api/conversations":
            return "conversation.create"
        if path.startswith(_CONVERSATION_PREFIX) and path.endswith("/messages"):
            return "conversation.message"
        if path.startswith(_CONVERSATION_PREFIX) and path.endswith("/launch"):
            return "conversation.launch"
        if path.startswith(_SESSION_PREFIX) and path.endswith("/final-review/request"):
            return "final_review.request"
        if path.startswith(_SESSION_PREFIX) and path.endswith("/final-review/decision"):
            return "final_review.decision"
        if path.startswith(_SESSION_PREFIX) and path.endswith("/decision"):
            return "manifest.decision"
        if path.startswith(_SESSION_PREFIX) and path.endswith("/export"):
            return "candidate.export"
        if path.startswith(_SESSION_PREFIX) and path.endswith("/retry"):
            return "correction.retry"
    return "unknown"


def _elapsed_milliseconds(started_ns: int) -> int:
    return max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)


def _require_exact_fields(body: Mapping[str, object], expected: set[str]) -> None:
    if set(body) != expected:
        raise _RequestError(HTTPStatus.BAD_REQUEST, "bad_request")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _is_json_content_type(value: str) -> bool:
    parts = [part.strip() for part in value.split(";")]
    if not parts or parts[0].lower() != "application/json":
        return False
    if len(parts) == 1:
        return True
    if len(parts) != 2:
        return False
    name, separator, charset = parts[1].partition("=")
    return (
        bool(separator)
        and name.strip().lower() == "charset"
        and charset.strip().strip('"').lower() == "utf-8"
    )


def _valid_handle(value: str) -> bool:
    return len(value) == 24 and all(character in "0123456789abcdef" for character in value)
