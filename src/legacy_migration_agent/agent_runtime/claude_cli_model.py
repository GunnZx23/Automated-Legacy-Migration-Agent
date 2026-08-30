"""Truthful, tool-free structured-output adapter for the local Claude CLI.

The executable runs locally, but inference is remote. The adapter therefore
records ``provider="claude-cli"`` and the ``remote_provider_managed`` execution
boundary. It uses Claude's bare non-customized mode, disables native tools,
skills, MCP, browser integration, and session persistence, and supplies only a
validated credential-helper/approved-provider settings subset. Admin-managed
provider policy can still apply. These controls do not claim that the upstream
provider stores nothing; provider retention remains governed by the operator's
configured Claude account or third-party provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import urlsplit

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

DEFAULT_CLAUDE_TIMEOUT_SECONDS = 240.0
MIN_CLAUDE_TIMEOUT_SECONDS = 1.0
MAX_CLAUDE_TIMEOUT_SECONDS = 900.0
CLAUDE_EFFORT = "high"
_MAX_SYSTEM_PROMPT_BYTES = 256 * 1024
_MAX_INPUT_BYTES = 2 * 1024 * 1024
_MAX_USER_PROMPT_BYTES = 4 * 1024 * 1024
_MAX_CLI_ENVELOPE_BYTES = 16 * 1024 * 1024
_MAX_CLI_STDERR_BYTES = 1024 * 1024
_MAX_CLI_PROBE_BYTES = 64 * 1024
_MAX_SETTINGS_BYTES = 256 * 1024
_MAX_CA_BUNDLE_BYTES = 2 * 1024 * 1024
_MAX_USAGE_DIAGNOSTIC_TOKENS = 1_000_000_000_000
_VALIDATION_DIAGNOSTIC_INTEGER_LIMITS = {
    "approved": 64,
    "proposed": 64,
    "unique": 64,
    "missing": 64,
    "extra": 64,
    "total_content_chars": _MAX_CLI_ENVELOPE_BYTES,
    "assumptions": 64,
}
_VALIDATION_DIAGNOSTIC_BOOLEAN_FIELDS = frozenset(
    {"scope_valid", "exact_coverage", "exact_coverage_required"}
)
_VERSION_PROBE_TIMEOUT_SECONDS = 30.0
_SAFE_IDENTIFIER = re.compile(r"^[^\x00\r\n]{1,300}$")
_UNSAFE_HELPER_SHELL_TEXT = re.compile(r"[\x00-\x1f\x7f;&|<>`$*?\[\]{}()!#~\\]")
_BANNED_ROUTING_ENVIRONMENT = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "NODE_PATH",
    }
)
_PASSTHROUGH_CLAUDE_ENVIRONMENT_KEYS = frozenset(
    {
        "COLORTERM",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
    }
)
_SECRET_PRESENCE_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)
_PROXY_ENVIRONMENT_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def _trusted_claude_executable_roots() -> tuple[Path, ...]:
    """Return recognized native, npm, and Homebrew Claude installation roots."""

    home = Path.home()
    return (
        home / ".local" / "share" / "claude" / "versions",
        home / ".local" / "lib" / "node_modules" / "@anthropic-ai" / "claude-code",
        home / ".npm-global" / "lib" / "node_modules" / "@anthropic-ai" / "claude-code",
        Path("/opt/homebrew/Caskroom/claude-code"),
        Path("/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code"),
        Path("/usr/local/Caskroom/claude-code"),
        Path("/usr/local/lib/node_modules/@anthropic-ai/claude-code"),
    )


@dataclass(frozen=True, slots=True)
class _ClaudeExecutableIdentity:
    path: str
    file_digest: str
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    owner_uid: int
    mode: int


@dataclass(frozen=True, slots=True)
class _ClaudeAuthSettings:
    """Canonical, non-secret settings passed explicitly to the isolated CLI."""

    argument: str
    digest: str
    process_environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _CredentialHelperIdentity:
    argument: str
    digest: str


@dataclass(frozen=True, slots=True)
class _BoundedProcessResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    stdout_exceeded: bool
    stderr_exceeded: bool


@dataclass(frozen=True, slots=True)
class _UsageDiagnostics:
    provider_usage_reported: bool
    provider_usage_suspect: bool
    reported_input_tokens: int | None
    reported_output_tokens: int | None
    reported_total_tokens: int | None


def _resolve_claude_auth_settings(approved_provider: str) -> _ClaudeAuthSettings:
    """Extract only approved credentials and identity-bound provider routing.

    Claude's ``--restricted`` mode prevents the configured third-party credential
    helper from completing.  ``--bare`` is the purpose-built non-customized mode,
    but it requires ``apiKeyHelper`` to be supplied through ``--settings``.  Read
    the operator-owned settings file safely and pass a canonical object containing
    no hooks, plugins, MCP configuration, tool policy, or telemetry.  A Bedrock
    enterprise gateway and its CA bundle are retained only after strict validation;
    their exact values and file bytes are bound into the runtime identity.
    """

    if _SAFE_IDENTIFIER.fullmatch(approved_provider) is None:
        raise ModelConfigurationError("the approved Claude provider identifier is invalid")
    settings_path = Path.home() / ".claude" / "settings.json"
    payload = _read_secure_settings_file(settings_path)
    try:
        source = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ModelConfigurationError("Claude credential settings are invalid") from exc
    if not isinstance(source, dict):
        raise ModelConfigurationError("Claude credential settings are invalid")
    api_key_helper = source.get("apiKeyHelper")
    if not isinstance(api_key_helper, str) or _SAFE_IDENTIFIER.fullmatch(api_key_helper) is None:
        raise ModelConfigurationError("Claude credential settings lack a safe apiKeyHelper")
    helper = _validate_credential_helper(api_key_helper)

    minimal_environment: dict[str, str] = {}
    bound_file_digests: list[str] = []
    source_environment = source.get("env")
    if source_environment is not None and not isinstance(source_environment, dict):
        raise ModelConfigurationError("Claude credential settings environment is invalid")
    if approved_provider == "bedrock":
        if not isinstance(source_environment, dict):
            raise ModelConfigurationError("Claude credential settings do not select Bedrock")
        use_bedrock = source_environment.get("CLAUDE_CODE_USE_BEDROCK")
        if not isinstance(use_bedrock, str) or use_bedrock.strip().lower() not in {"1", "true"}:
            raise ModelConfigurationError("Claude credential settings do not select Bedrock")
        minimal_environment["CLAUDE_CODE_USE_BEDROCK"] = "1"
        concrete_model = source_environment.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
        if concrete_model is not None:
            if (
                not isinstance(concrete_model, str)
                or _SAFE_IDENTIFIER.fullmatch(concrete_model) is None
            ):
                raise ModelConfigurationError("Claude Bedrock model mapping is invalid")
            minimal_environment["ANTHROPIC_DEFAULT_SONNET_MODEL"] = concrete_model

        gateway = source_environment.get("ANTHROPIC_BEDROCK_BASE_URL")
        if gateway is not None:
            minimal_environment["ANTHROPIC_BEDROCK_BASE_URL"] = _validate_bedrock_gateway_url(
                gateway
            )

        skip_bedrock_auth = source_environment.get("CLAUDE_CODE_SKIP_BEDROCK_AUTH")
        if skip_bedrock_auth is not None:
            if (
                not isinstance(skip_bedrock_auth, str)
                or skip_bedrock_auth.strip().lower() not in {"1", "true"}
                or "ANTHROPIC_BEDROCK_BASE_URL" not in minimal_environment
            ):
                raise ModelConfigurationError(
                    "Claude Bedrock gateway authentication mode is invalid"
                )
            minimal_environment["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] = "1"

        ca_bundle = source_environment.get("NODE_EXTRA_CA_CERTS")
        if ca_bundle is not None:
            if "ANTHROPIC_BEDROCK_BASE_URL" not in minimal_environment:
                raise ModelConfigurationError(
                    "Claude custom CA requires an approved Bedrock enterprise gateway"
                )
            ca_path, ca_digest = _resolve_claude_ca_bundle(ca_bundle)
            minimal_environment["NODE_EXTRA_CA_CERTS"] = ca_path
            bound_file_digests.append(ca_digest)

        tls_verification = source_environment.get("NODE_TLS_REJECT_UNAUTHORIZED")
        if tls_verification is not None:
            if not isinstance(tls_verification, str) or tls_verification.strip() != "1":
                raise ModelConfigurationError("Claude TLS verification setting is invalid")
            minimal_environment["NODE_TLS_REJECT_UNAUTHORIZED"] = "1"

    minimal_settings = {
        "apiKeyHelper": helper.argument,
        "env": minimal_environment,
    }
    argument = json.dumps(minimal_settings, sort_keys=True, separators=(",", ":"))
    identity = (
        argument.encode("utf-8")
        + b"\x00"
        + helper.digest.encode("ascii")
        + b"\x00"
        + b"\x00".join(item.encode("ascii") for item in sorted(bound_file_digests))
    )
    return _ClaudeAuthSettings(
        argument=argument,
        digest="sha256:" + hashlib.sha256(identity).hexdigest(),
        process_environment=tuple(sorted(minimal_environment.items())),
    )


def _validate_bedrock_gateway_url(value: object) -> str:
    """Return one canonical HTTPS gateway URL without credential-bearing parts."""

    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ModelConfigurationError("Claude Bedrock enterprise gateway URL is invalid")
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ModelConfigurationError("Claude Bedrock enterprise gateway URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise ModelConfigurationError("Claude Bedrock enterprise gateway URL is invalid")
    return candidate.rstrip("/")


def _resolve_claude_ca_bundle(value: object) -> tuple[str, str]:
    """Validate and digest one local CA bundle used by the approved gateway."""

    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ModelConfigurationError("Claude enterprise gateway CA bundle is invalid")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ModelConfigurationError("Claude enterprise gateway CA bundle must be absolute")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise ModelConfigurationError("Claude enterprise gateway CA bundle is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ModelConfigurationError("Claude enterprise gateway CA bundle must be a regular file")
    if metadata.st_uid not in {os.getuid(), 0}:
        raise ModelConfigurationError("Claude enterprise gateway CA bundle has an untrusted owner")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ModelConfigurationError("Claude enterprise gateway CA bundle is group/world writable")
    if not 1 <= metadata.st_size <= _MAX_CA_BUNDLE_BYTES:
        raise ModelConfigurationError("Claude enterprise gateway CA bundle has an unsupported size")
    _validate_credential_helper_directory_chain(resolved.parent)
    return str(resolved), _regular_file_digest(resolved, metadata)


def _validate_credential_helper(command: str) -> _CredentialHelperIdentity:
    """Bind a simple absolute credential-helper command without shell expansion."""

    if _UNSAFE_HELPER_SHELL_TEXT.search(command):
        raise ModelConfigurationError("Claude apiKeyHelper contains unsafe shell syntax")
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ModelConfigurationError("Claude apiKeyHelper syntax is invalid") from exc
    if not 1 <= len(tokens) <= 16 or any(
        _SAFE_IDENTIFIER.fullmatch(token) is None for token in tokens
    ):
        raise ModelConfigurationError("Claude apiKeyHelper syntax is invalid")
    executable = Path(tokens[0])
    if not executable.is_absolute():
        raise ModelConfigurationError("Claude apiKeyHelper executable must be absolute")
    try:
        resolved = executable.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise ModelConfigurationError("Claude apiKeyHelper executable is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ModelConfigurationError("Claude apiKeyHelper executable must be a regular file")
    if metadata.st_uid not in {os.getuid(), 0}:
        raise ModelConfigurationError("Claude apiKeyHelper executable has an untrusted owner")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ModelConfigurationError("Claude apiKeyHelper executable is group/world writable")
    _validate_credential_helper_directory_chain(resolved.parent)
    digest = _regular_file_digest(resolved, metadata)
    identity = str(resolved).encode("utf-8") + b"\x00" + digest.encode("ascii")
    return _CredentialHelperIdentity(
        argument=shlex.join((str(resolved), *tokens[1:])),
        digest="sha256:" + hashlib.sha256(identity).hexdigest(),
    )


def _validate_credential_helper_directory_chain(directory: Path) -> None:
    """Reject replaceable helper parents below one recognized local anchor."""

    candidates = (
        Path.home(),
        Path(tempfile.gettempdir()),
        Path("/Applications"),
        Path("/opt/homebrew"),
        Path("/usr/local"),
    )
    anchors: list[Path] = []
    for candidate in candidates:
        try:
            anchor = candidate.resolve(strict=True)
        except OSError:
            continue
        if directory.is_relative_to(anchor):
            anchors.append(anchor)
    if not anchors:
        raise ModelConfigurationError("Claude apiKeyHelper is outside recognized local roots")
    anchor = max(anchors, key=lambda item: len(item.parts))
    current = directory
    while current != anchor:
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise ModelConfigurationError(
                "Claude apiKeyHelper installation directory is unavailable"
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise ModelConfigurationError(
                "Claude apiKeyHelper installation path is not a directory"
            )
        if metadata.st_uid not in {os.getuid(), 0}:
            raise ModelConfigurationError(
                "Claude apiKeyHelper installation directory has an untrusted owner"
            )
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ModelConfigurationError(
                "Claude apiKeyHelper installation directory is group/world writable"
            )
        current = current.parent


def _read_secure_settings_file(path: Path) -> bytes:
    """Read one bounded user-owned regular file without following symlinks."""

    try:
        expected = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ModelConfigurationError("Claude credential settings are unavailable") from exc
    if not stat.S_ISREG(expected.st_mode):
        raise ModelConfigurationError("Claude credential settings must be a regular file")
    if expected.st_uid != os.getuid():
        raise ModelConfigurationError("Claude credential settings have an untrusted owner")
    if expected.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ModelConfigurationError("Claude credential settings are group/world writable")
    if expected.st_size > _MAX_SETTINGS_BYTES:
        raise ModelConfigurationError("Claude credential settings exceed the supported size")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ModelConfigurationError("Claude credential settings cannot be opened safely") from exc
    try:
        observed = os.fstat(descriptor)
        expected_identity = (
            expected.st_dev,
            expected.st_ino,
            expected.st_size,
            expected.st_mtime_ns,
            expected.st_ctime_ns,
        )
        observed_identity = (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        )
        if not stat.S_ISREG(observed.st_mode) or observed_identity != expected_identity:
            raise ModelConfigurationError("Claude credential settings changed while inspected")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, 64 * 1024):
            total += len(chunk)
            if total > _MAX_SETTINGS_BYTES:
                raise ModelConfigurationError(
                    "Claude credential settings exceed the supported size"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if after_identity != observed_identity:
            raise ModelConfigurationError("Claude credential settings changed while read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _resolve_claude_executable() -> _ClaudeExecutableIdentity:
    override = os.environ.get("LEGACY_MIGRATION_CLAUDE_CLI_PATH", "").strip()
    if override:
        raise ModelConfigurationError("live Claude use does not permit an executable-path override")
    selected = shutil.which("claude")
    if selected is None:
        raise ModelRuntimeError("the Claude CLI is not installed")
    try:
        path = Path(selected).resolve(strict=True)
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ModelRuntimeError("the Claude CLI executable is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ModelConfigurationError("the Claude CLI executable must be a regular file")
    trusted_root = _trusted_executable_root(path)
    if metadata.st_uid not in {os.getuid(), 0}:
        raise ModelConfigurationError("the Claude CLI executable has an untrusted owner")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ModelConfigurationError("the Claude CLI executable is group/world writable")
    _validate_executable_directory_chain(path.parent, trusted_root)
    file_digest = _regular_file_digest(path, metadata)
    return _ClaudeExecutableIdentity(
        path=str(path),
        file_digest=file_digest,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
        owner_uid=metadata.st_uid,
        mode=stat.S_IMODE(metadata.st_mode),
    )


def _trusted_executable_root(path: Path) -> Path:
    for candidate in _trusted_claude_executable_roots():
        try:
            root = candidate.resolve(strict=True)
        except OSError:
            continue
        if path.is_relative_to(root):
            return root
    raise ModelConfigurationError(
        "the Claude CLI executable is outside recognized installation roots"
    )


def _validate_executable_directory_chain(directory: Path, root: Path) -> None:
    current = directory
    while True:
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise ModelRuntimeError("the Claude CLI installation directory is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise ModelConfigurationError("the Claude CLI installation path is not a directory")
        if metadata.st_uid not in {os.getuid(), 0}:
            raise ModelConfigurationError(
                "the Claude CLI installation directory has an untrusted owner"
            )
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ModelConfigurationError(
                "the Claude CLI installation directory is group/world writable"
            )
        if current == root:
            return
        if current == current.parent or not current.is_relative_to(root):
            raise ModelConfigurationError(
                "the Claude CLI executable escaped its recognized installation root"
            )
        current = current.parent


def _regular_file_digest(path: Path, expected: os.stat_result) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ModelRuntimeError("the Claude CLI executable cannot be opened safely") from exc
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
        ) != (expected.st_dev, expected.st_ino, expected.st_size):
            raise ModelConfigurationError("the Claude CLI executable changed while inspected")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        ):
            raise ModelConfigurationError("the Claude CLI executable changed while hashed")
        return f"sha256:{digest.hexdigest()}"
    finally:
        os.close(descriptor)


def _stream_digest(value: str | None) -> str:
    payload = (value or "").encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _utf8_size(value: str | None) -> int:
    """Return a content-free byte count for one retained CLI stream."""

    return len((value or "").encode("utf-8", errors="replace"))


def _canonical_json_size(value: object) -> int | None:
    """Return a content-free canonical JSON size when serialization is safe."""

    try:
        return len(canonical_json_bytes(value))
    except (RecursionError, TypeError, UnicodeError, ValueError):
        return None


def _usage_diagnostic_fields(
    envelope: Mapping[str, object],
    *,
    structured_output: object,
) -> _UsageDiagnostics:
    """Return sanitized provider-usage diagnostics without changing public evidence.

    A provider envelope that reports zero input and output tokens alongside a
    non-empty structured output is internally inconsistent. Mark that case as
    suspect while retaining the provider-reported zero values. Provider-controlled
    integers outside a deliberately generous lifecycle bound are marked suspect
    and omitted so observability cannot turn a successful model call into a
    serialization failure. ``_usage_evidence`` remains the persisted contract
    authority and intentionally retains its existing semantics.
    """

    usage = envelope.get("usage")
    input_tokens: int | None = None
    output_tokens: int | None = None
    if isinstance(usage, dict):
        input_tokens = _token_sum(
            usage,
            ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"),
        )
        output_tokens = _token_sum(usage, ("output_tokens",))
    provider_usage_reported = input_tokens is not None or output_tokens is not None
    resolved_input = input_tokens or 0
    resolved_output = output_tokens or 0
    diagnostics_bounded = (
        resolved_input <= _MAX_USAGE_DIAGNOSTIC_TOKENS
        and resolved_output <= _MAX_USAGE_DIAGNOSTIC_TOKENS
        and resolved_input <= _MAX_USAGE_DIAGNOSTIC_TOKENS - resolved_output
    )
    structured_output_nonempty = not (
        structured_output is None
        or structured_output == ""
        or structured_output == []
        or structured_output == {}
    )
    provider_usage_suspect = provider_usage_reported and (
        not diagnostics_bounded
        or (structured_output_nonempty and resolved_input == 0 and resolved_output == 0)
    )
    diagnostics_available = provider_usage_reported and diagnostics_bounded
    return _UsageDiagnostics(
        provider_usage_reported=provider_usage_reported,
        provider_usage_suspect=provider_usage_suspect,
        reported_input_tokens=resolved_input if diagnostics_available else None,
        reported_output_tokens=resolved_output if diagnostics_available else None,
        reported_total_tokens=(resolved_input + resolved_output if diagnostics_available else None),
    )


def _validation_error_contract(error: ValidationError) -> tuple[str, str, bool]:
    """Return bounded schema locations and error kinds without model-authored values."""

    locations: list[str] = []
    kinds: list[str] = []
    errors = error.errors(include_input=False, include_url=False)
    for item in errors[:16]:
        raw_location = item.get("loc", ())
        location_parts: list[str] = []
        if isinstance(raw_location, tuple):
            for part in raw_location:
                if isinstance(part, int) and 0 <= part <= 1_000_000:
                    location_parts.append(str(part))
                elif isinstance(part, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", part):
                    location_parts.append(part)
                else:
                    location_parts.append("field")
        locations.append(".".join(location_parts) or "root")
        raw_kind = item.get("type")
        kinds.append(
            raw_kind
            if isinstance(raw_kind, str) and re.fullmatch(r"[a-z0-9_.-]{1,80}", raw_kind)
            else "validation_error"
        )
    return ",".join(locations), ",".join(kinds), len(errors) > 16


def _provider_validation_diagnostics(
    output_type: type[BaseModel],
    structured_output: object,
) -> dict[str, int | bool]:
    """Retain only fixed, bounded scalar diagnostics from an output contract hook."""

    hook = getattr(output_type, "provider_validation_diagnostics", None)
    if not callable(hook):
        return {}
    try:
        raw = hook(structured_output)
    except Exception:  # noqa: BLE001 - diagnostics cannot replace the schema error
        return {}
    if not isinstance(raw, Mapping):
        return {}
    safe: dict[str, int | bool] = {}
    for field, limit in _VALIDATION_DIAGNOSTIC_INTEGER_LIMITS.items():
        value = raw.get(field)
        if type(value) is int and 0 <= value <= limit:
            safe[field] = value
    for field in _VALIDATION_DIAGNOSTIC_BOOLEAN_FIELDS:
        value = raw.get(field)
        if type(value) is bool:
            safe[field] = value
    return safe


def _text_exceeds_limit(value: str | None, limit: int) -> bool:
    if value is None:
        return False
    if len(value) > limit:
        return True
    return len(value.encode("utf-8", errors="replace")) > limit


def _run_bounded_command(
    command: Sequence[str],
    *,
    input_text: str | None,
    timeout_seconds: float,
    cwd: str,
    environment: Mapping[str, str],
    stdout_limit: int,
    stderr_limit: int,
) -> _BoundedProcessResult:
    """Run one subprocess while retaining at most the declared stream limits."""

    normalized_command = tuple(command)
    if stdout_limit < 1 or stderr_limit < 1:
        raise ValueError("subprocess stream limits must be positive")
    input_bytes = None if input_text is None else input_text.encode("utf-8")
    process = subprocess.Popen(
        normalized_command,
        stdin=subprocess.DEVNULL if input_bytes is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=dict(environment),
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        _kill_process_group(process)
        raise ModelRuntimeError("the Claude CLI output streams were unavailable")

    stdout = bytearray()
    stderr = bytearray()
    exceeded = {"stdout": False, "stderr": False}
    selector = selectors.DefaultSelector()
    streams: dict[int, tuple[str, BinaryIO]] = {}

    def register_reader(name: str, stream: BinaryIO) -> None:
        descriptor = stream.fileno()
        os.set_blocking(descriptor, False)
        streams[descriptor] = (name, stream)
        selector.register(descriptor, selectors.EVENT_READ)

    def close_descriptor(descriptor: int) -> None:
        entry = streams.pop(descriptor, None)
        try:
            selector.unregister(descriptor)
        except (KeyError, ValueError):
            pass
        if entry is not None:
            entry[1].close()

    register_reader("stdout", cast("BinaryIO", process.stdout))
    register_reader("stderr", cast("BinaryIO", process.stderr))
    stdin_descriptor: int | None = None
    if input_bytes is not None:
        if process.stdin is None:
            _kill_process_group(process)
            raise ModelRuntimeError("the Claude CLI input stream was unavailable")
        stdin_descriptor = process.stdin.fileno()
        os.set_blocking(stdin_descriptor, False)
        selector.register(stdin_descriptor, selectors.EVENT_WRITE)
    input_offset = 0
    deadline = time.monotonic() + timeout_seconds

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(normalized_command, timeout_seconds)
            events = selector.select(remaining)
            for key, mask in events:
                descriptor = int(key.fd)
                if descriptor == stdin_descriptor and mask & selectors.EVENT_WRITE:
                    assert process.stdin is not None
                    if input_bytes is None or input_offset >= len(input_bytes):
                        selector.unregister(descriptor)
                        process.stdin.close()
                        stdin_descriptor = None
                        continue
                    try:
                        written = os.write(
                            descriptor, input_bytes[input_offset : input_offset + 64 * 1024]
                        )
                    except BrokenPipeError:
                        selector.unregister(descriptor)
                        process.stdin.close()
                        stdin_descriptor = None
                    else:
                        input_offset += written
                    continue
                if not mask & selectors.EVENT_READ:
                    continue
                try:
                    chunk = os.read(descriptor, 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    close_descriptor(descriptor)
                    continue
                name, _stream = streams[descriptor]
                destination = stdout if name == "stdout" else stderr
                limit = stdout_limit if name == "stdout" else stderr_limit
                available = max(0, limit - len(destination))
                destination.extend(chunk[:available])
                if len(chunk) > available:
                    exceeded[name] = True
                    _kill_process_group(process)
                    close_descriptor(descriptor)

            if exceeded["stdout"] or exceeded["stderr"]:
                break
    except BaseException:
        _kill_process_group(process)
        raise
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        for descriptor in tuple(streams):
            close_descriptor(descriptor)
        selector.close()
        if process.poll() is None:
            _kill_process_group(process)

    returncode = process.wait()
    try:
        stdout_text = bytes(stdout).decode("utf-8")
        stderr_text = bytes(stderr).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModelOutputError("Claude CLI returned invalid UTF-8 output") from exc
    return _BoundedProcessResult(
        args=normalized_command,
        returncode=returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        stdout_exceeded=exceeded["stdout"],
        stderr_exceeded=exceeded["stderr"],
    )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill and reap the isolated subprocess group without exposing its output."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _sanitized_cli_failure_category(stdout: str | None) -> str:
    """Classify a Claude JSON failure without exposing provider prose."""

    if _text_exceeds_limit(stdout, _MAX_CLI_ENVELOPE_BYTES):
        return "oversized_failure_envelope"
    try:
        envelope = json.loads(stdout or "")
    except (TypeError, ValueError, UnicodeError):
        return "non_json_failure"
    if not isinstance(envelope, dict):
        return "invalid_failure_envelope"
    terminal_reason = str(envelope.get("terminal_reason") or "").casefold()
    result = str(envelope.get("result") or "").casefold()
    combined = f"{terminal_reason}\n{result}"
    if terminal_reason == "api_error":
        return "provider_api_error"
    if terminal_reason:
        return "provider_terminal_failure"
    if "not logged in" in combined or "authentication" in combined or "credential" in combined:
        return "authentication_unavailable"
    if "json schema" in combined or ("schema" in combined and "structured" in combined):
        return "structured_output_schema"
    if "rate limit" in combined or "too many requests" in combined:
        return "provider_rate_limit"
    if "overloaded" in combined or "capacity" in combined:
        return "provider_capacity"
    if "context" in combined and ("long" in combined or "token" in combined):
        return "input_limit"
    if "model" in combined and ("not found" in combined or "unsupported" in combined):
        return "model_unavailable"
    return "cli_nonzero_exit"


def _normalize_claude_provider_schema(schema: Mapping[str, object]) -> dict[str, object]:
    """Return Claude's validation schema without unsupported annotations.

    Pydantic emits an OpenAPI-style ``discriminator`` annotation for tagged
    unions. Claude's strict structured-output mode rejects that annotation, but
    supports the adjacent JSON Schema ``oneOf`` branches and their ``$ref``
    targets. Recursively remove only that non-validation annotation; preserve
    every validation keyword, branch, definition, and scalar value unchanged.
    """

    schema_map_keywords = frozenset(
        {"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"}
    )
    schema_sequence_keywords = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
    schema_value_keywords = frozenset(
        {
            "additionalProperties",
            "contains",
            "contentSchema",
            "else",
            "if",
            "items",
            "not",
            "propertyNames",
            "then",
            "unevaluatedItems",
            "unevaluatedProperties",
        }
    )

    def copy_json(value: object) -> object:
        if isinstance(value, Mapping):
            copied: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ModelConfigurationError("Claude output schema contained an invalid key")
                copied[key] = copy_json(item)
            return copied
        if isinstance(value, list):
            return [copy_json(item) for item in value]
        return value

    def normalize_schema_map(value: Mapping[object, object]) -> dict[str, object]:
        normalized: dict[str, object] = {}
        for name, child in value.items():
            if not isinstance(name, str):
                raise ModelConfigurationError("Claude output schema contained an invalid key")
            normalized[name] = normalize_schema_node(child)
        return normalized

    def normalize_schema_node(value: object) -> object:
        if isinstance(value, bool):
            return value
        if not isinstance(value, Mapping):
            return copy_json(value)
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ModelConfigurationError("Claude output schema contained an invalid key")
            if key == "discriminator":
                continue
            if key in schema_map_keywords and isinstance(item, Mapping):
                normalized[key] = normalize_schema_map(item)
            elif key in schema_sequence_keywords and isinstance(item, list):
                normalized[key] = [normalize_schema_node(child) for child in item]
            elif key == "dependencies" and isinstance(item, Mapping):
                dependencies: dict[str, object] = {}
                for name, child in item.items():
                    if not isinstance(name, str):
                        raise ModelConfigurationError(
                            "Claude output schema contained an invalid key"
                        )
                    dependencies[name] = (
                        normalize_schema_node(child)
                        if isinstance(child, (Mapping, bool))
                        else copy_json(child)
                    )
                normalized[key] = dependencies
            elif key in schema_value_keywords and isinstance(item, (Mapping, list, bool)):
                if isinstance(item, list):
                    normalized[key] = [normalize_schema_node(child) for child in item]
                else:
                    normalized[key] = normalize_schema_node(item)
            else:
                normalized[key] = copy_json(item)
        return normalized

    normalized = normalize_schema_node(schema)
    return cast(dict[str, object], normalized)


def _project_claude_root_correlated_union(
    schema: Mapping[str, object],
) -> dict[str, object]:
    """Project one conservative root object union for Claude compatibility.

    Some Claude/Bedrock structured-output paths do not terminate reliably for a
    root ``type: object`` combined with ``oneOf`` closed-object branches. Only
    project that root union when every branch has the same fields and required
    list and contains no branch-level validation keywords beyond its closed
    object shape. Each field retains the distinct branch schemas as ``anyOf``;
    the original Pydantic model remains the authority for cross-field
    correlations after generation. Nested unions are never visited.
    """

    copied = _normalize_claude_provider_schema(schema)
    branches = copied.get("oneOf")
    if copied.get("type") != "object" or not isinstance(branches, list) or len(branches) < 2:
        return copied

    allowed_branch_keys = frozenset(
        {
            "$comment",
            "additionalProperties",
            "deprecated",
            "description",
            "examples",
            "properties",
            "readOnly",
            "required",
            "title",
            "type",
            "writeOnly",
        }
    )
    object_branches: list[Mapping[str, object]] = []
    property_names: tuple[str, ...] | None = None
    required: tuple[str, ...] | None = None
    for branch in branches:
        if not isinstance(branch, Mapping):
            return copied
        if set(branch) - allowed_branch_keys:
            return copied
        properties = branch.get("properties")
        branch_required = branch.get("required")
        if (
            branch.get("type") != "object"
            or branch.get("additionalProperties") is not False
            or not isinstance(properties, Mapping)
            or not isinstance(branch_required, list)
            or any(not isinstance(name, str) for name in properties)
            or any(not isinstance(name, str) for name in branch_required)
        ):
            return copied
        current_names = tuple(cast(str, name) for name in properties)
        current_required = tuple(cast(str, name) for name in branch_required)
        if property_names is None:
            property_names = current_names
            required = current_required
        elif set(current_names) != set(property_names) or current_required != required:
            return copied
        object_branches.append(cast(Mapping[str, object], branch))

    if property_names is None or required is None:
        return copied
    projected_properties: dict[str, object] = {}
    for name in property_names:
        variants: list[object] = []
        for branch in object_branches:
            properties = cast(Mapping[str, object], branch["properties"])
            variant = properties[name]
            if variant not in variants:
                variants.append(variant)
        projected_properties[name] = variants[0] if len(variants) == 1 else {"anyOf": variants}

    projected = dict(copied)
    projected.pop("oneOf")
    projected["additionalProperties"] = False
    projected["properties"] = projected_properties
    projected["required"] = list(required)
    return projected


def _controlled_claude_environment() -> tuple[dict[str, str], str]:
    """Reject endpoint/code-injection overrides and pass a minimal bound environment."""

    source_environment = dict(os.environ)
    prohibited = tuple(
        sorted(
            key
            for key, value in source_environment.items()
            if value.strip() and _is_unsupported_claude_environment_key(key)
        )
    )
    if prohibited:
        raise ModelConfigurationError(
            "Claude runtime contains an unsupported endpoint or code-injection override"
        )
    environment = {
        key: value
        for key, value in source_environment.items()
        if key in _PASSTHROUGH_CLAUDE_ENVIRONMENT_KEYS or key.startswith("LC_")
    }
    for key in _PROXY_ENVIRONMENT_KEYS:
        value = source_environment.get(key, "").strip()
        if value:
            environment[key] = _validate_loopback_proxy_url(value)
    identity = {
        "retained_environment": {
            key: _stream_digest(value) for key, value in sorted(environment.items())
        },
        "credential_modes": {
            key: bool(environment.get(key)) for key in sorted(_SECRET_PRESENCE_KEYS)
        },
        "prohibited_overrides": (),
    }
    return environment, _stream_digest(json.dumps(identity, sort_keys=True, separators=(",", ":")))


def _validate_loopback_proxy_url(value: str) -> str:
    """Allow only credential-free loopback proxy mediation for the child CLI."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ModelConfigurationError("Claude runtime proxy configuration is invalid") from exc
    allowed_schemes = {"http", "https", "socks5", "socks5h"}
    if (
        parsed.scheme not in allowed_schemes
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ModelConfigurationError("Claude runtime proxy configuration is invalid")
    return value


def _is_unsupported_claude_environment_key(key: str) -> bool:
    if key in _BANNED_ROUTING_ENVIRONMENT or key.startswith("AWS_ENDPOINT_URL"):
        return True
    endpoint_markers = ("BASE_URL", "API_HOST", "API_URL", "ENDPOINT", "UNIX_SOCKET")
    return key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "AWS_", "BEDROCK_")) and any(
        marker in key for marker in endpoint_markers
    )


def _verify_executable_unchanged(identity: _ClaudeExecutableIdentity) -> None:
    try:
        metadata = Path(identity.path).stat(follow_symlinks=False)
    except OSError as exc:
        raise ModelConfigurationError("the Claude CLI executable identity changed") from exc
    observed = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
    )
    expected = (
        identity.device,
        identity.inode,
        identity.size,
        identity.modified_ns,
        identity.changed_ns,
        identity.owner_uid,
        identity.mode,
    )
    if not stat.S_ISREG(metadata.st_mode) or observed != expected:
        raise ModelConfigurationError("the Claude CLI executable identity changed")


class ClaudeCliStructuredModelClient:
    """Structured-output client backed by one approved Claude CLI identity."""

    provider = "claude-cli"
    live_invocation = True
    store_false_sent = False
    execution_boundary: ModelExecutionBoundary = "remote_provider_managed"

    def __init__(
        self,
        model_id: str,
        *,
        approval: LiveModelApproval,
        timeout_seconds: float = DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    ) -> None:
        normalized_model = model_id.strip()
        if _SAFE_IDENTIFIER.fullmatch(normalized_model) is None:
            raise ModelConfigurationError("an explicit model_id is required")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ModelConfigurationError("Claude timeout must be a number")
        normalized_timeout = float(timeout_seconds)
        if not MIN_CLAUDE_TIMEOUT_SECONDS <= normalized_timeout <= MAX_CLAUDE_TIMEOUT_SECONDS:
            raise ModelConfigurationError("Claude timeout is outside the supported range")
        if not isinstance(approval, LiveModelApproval):
            raise ModelConfigurationError("live Claude use requires explicit approval")
        approved_provider = approval.approved_remote_provider_id
        if approved_provider is None:
            raise ModelConfigurationError(
                "live Claude use requires an explicitly approved remote provider"
            )
        self._model_id = normalized_model
        self._timeout_seconds = normalized_timeout
        self._executable_identity = _resolve_claude_executable()
        self._executable = self._executable_identity.path
        self._workdir = tempfile.gettempdir()
        self._live_approval = LiveModelApproval.model_validate(approval.model_dump(mode="python"))
        self._approved_remote_provider_id = approved_provider
        self._auth_settings = _resolve_claude_auth_settings(approved_provider)
        self._last_usage: ModelUsageEvidence | None = None
        self._runtime_identity_digest: str | None = None
        self._runtime_environment_digest: str | None = None
        self._remote_provider_id: str | None = None

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_revision(self) -> str | None:
        """Claude CLI does not expose an observed provider model revision."""

        return None

    @property
    def runtime_identity_digest(self) -> str | None:
        return self._runtime_identity_digest

    @property
    def remote_provider_id(self) -> str | None:
        """Return the authenticated CLI provider observed by source-free preflight."""

        return self._remote_provider_id

    def resolve_runtime_identity(self) -> str:
        """Probe and freeze the CLI, model alias, and authenticated provider identity.

        This preflight sends no migration source or role prompt to Claude.  It exists so
        a measured run can bind its execution anchor before the first model request.
        """

        current = self._resolve_model_revision(timeout_seconds=self._timeout_seconds)
        if self._runtime_identity_digest is not None and self._runtime_identity_digest != current:
            raise ModelConfigurationError("Claude runtime revision changed")
        self._runtime_identity_digest = current
        return current

    @property
    def live_approval(self) -> LiveModelApproval:
        return self._live_approval

    @property
    def last_usage(self) -> ModelUsageEvidence | None:
        return self._last_usage

    def bind_runtime_identity(self, expected_revision: str) -> None:
        """Bind continuation to the same CLI version, alias, and auth provider."""

        if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_revision) is None:
            raise ModelConfigurationError("recorded Claude runtime revision is invalid")
        current = self.resolve_runtime_identity()
        if current != expected_revision:
            raise ModelConfigurationError("Claude runtime revision changed")
        if (
            self._runtime_identity_digest is not None
            and self._runtime_identity_digest != expected_revision
        ):
            raise ModelConfigurationError("Claude runtime revision changed")
        self._runtime_identity_digest = expected_revision

    def _resolve_model_revision(self, *, timeout_seconds: float) -> str:
        """Prove the CLI and auth seam, returning a stable runtime identity digest."""

        probe_timeout = min(float(timeout_seconds), _VERSION_PROBE_TIMEOUT_SECONDS)
        _verify_executable_unchanged(self._executable_identity)
        current_settings = _resolve_claude_auth_settings(self._approved_remote_provider_id)
        if current_settings.digest != self._auth_settings.digest:
            raise ModelConfigurationError("Claude credential settings changed")
        environment, environment_digest = _controlled_claude_environment()
        environment.update(self._auth_settings.process_environment)
        version = self._run_probe(
            (self._executable, "--version"),
            timeout_seconds=probe_timeout,
            failure="the Claude CLI is unavailable",
            environment=environment,
        ).stdout.strip()
        auth = self._run_probe(
            (
                self._executable,
                "--bare",
                "--no-chrome",
                "--disable-slash-commands",
                "--strict-mcp-config",
                "--settings",
                self._auth_settings.argument,
                "auth",
                "status",
                "--json",
            ),
            timeout_seconds=probe_timeout,
            failure="the Claude CLI authentication status is unavailable",
            environment=environment,
        )
        try:
            auth_payload = json.loads(auth.stdout)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ModelOutputError("Claude CLI authentication status was invalid") from exc
        if not isinstance(auth_payload, dict):
            raise ModelOutputError("Claude CLI authentication status was invalid")
        if auth_payload.get("loggedIn") is not True:
            raise ModelConfigurationError("the Claude CLI is not authenticated")
        provider = auth_payload.get("apiProvider")
        if not isinstance(provider, str) or _SAFE_IDENTIFIER.fullmatch(provider) is None:
            raise ModelOutputError("Claude CLI authentication provider was invalid")
        if provider != self._approved_remote_provider_id:
            raise ModelConfigurationError(
                "Claude authenticated provider does not match the explicit approval"
            )
        if self._remote_provider_id is not None and self._remote_provider_id != provider:
            raise ModelConfigurationError("Claude authentication provider changed")
        if (
            self._runtime_environment_digest is not None
            and self._runtime_environment_digest != environment_digest
        ):
            raise ModelConfigurationError("Claude runtime routing environment changed")
        digest = hashlib.sha256(
            b"claude-cli\x00"
            + self._model_id.encode("utf-8")
            + b"\x00"
            + version.encode("utf-8")
            + b"\x00"
            + provider.encode("utf-8")
            + b"\x00"
            + self._executable_identity.path.encode("utf-8")
            + b"\x00"
            + self._executable_identity.file_digest.encode("ascii")
            + b"\x00"
            + environment_digest.encode("ascii")
            + b"\x00"
            + self._auth_settings.digest.encode("ascii")
            + b"\x00"
            + CLAUDE_EFFORT.encode("ascii")
        ).hexdigest()
        self._remote_provider_id = provider
        self._runtime_environment_digest = environment_digest
        return "sha256:" + digest

    def _run_probe(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        failure: str,
        environment: Mapping[str, str],
    ) -> _BoundedProcessResult:
        try:
            completed = _run_bounded_command(
                command,
                input_text=None,
                timeout_seconds=max(timeout_seconds, 1.0),
                cwd=self._workdir,
                environment=environment,
                stdout_limit=_MAX_CLI_PROBE_BYTES,
                stderr_limit=_MAX_CLI_PROBE_BYTES,
            )
        except FileNotFoundError as exc:
            raise ModelRuntimeError("the Claude CLI is not installed") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelRuntimeError(failure) from exc
        if completed.stdout_exceeded or completed.stderr_exceeded:
            raise ModelRuntimeError(failure)
        if completed.returncode != 0:
            raise ModelRuntimeError(failure)
        return completed

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
        if len(system_prompt.encode("utf-8")) > _MAX_SYSTEM_PROMPT_BYTES:
            raise ModelConfigurationError("system prompt exceeds the Claude size limit")
        input_bytes = canonical_json_bytes(input_value)
        if len(input_bytes) > _MAX_INPUT_BYTES:
            raise ModelConfigurationError("structured model input exceeds the Claude size limit")
        self.resolve_runtime_identity()

        schema = output_type.model_json_schema(mode="validation")
        provider_schema = _project_claude_root_correlated_union(schema)
        provider_schema_json = json.dumps(
            provider_schema,
            sort_keys=True,
            separators=(",", ":"),
        )
        user_prompt = (
            "Produce one structured output for an automated legacy-migration workflow. "
            "The following JSON is the complete authoritative input:\n\n"
            + input_bytes.decode("utf-8")
            + "\n\nReturn exactly one native structured output matching the provider-enforced "
            "JSON Schema, with no prose or markdown."
        )
        if len(user_prompt.encode("utf-8")) > _MAX_USER_PROMPT_BYTES:
            raise ModelConfigurationError("Claude request exceeds the prompt size limit")

        lifecycle_event(
            "claude_cli.generation.started",
            model_id=self._model_id,
            output_type=output_type.__name__,
            system_prompt_bytes=len(system_prompt.encode("utf-8")),
            input_bytes=len(input_bytes),
            user_prompt_bytes=len(user_prompt.encode("utf-8")),
            provider_schema_bytes=len(provider_schema_json.encode("utf-8")),
            timeout_seconds=self._timeout_seconds,
        )
        started_ns = time.perf_counter_ns()
        envelope = self._invoke(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            provider_schema_json=provider_schema_json,
        )
        elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
        structured_output = envelope.get("structured_output")
        if not isinstance(structured_output, dict):
            lifecycle_event(
                "claude_cli.output.rejected",
                phase="native_structured_output",
                output_type=output_type.__name__,
            )
            raise ModelOutputError("Claude response did not contain native structured output")
        self._last_usage = _usage_evidence(envelope, elapsed_ms)
        try:
            parsed = output_type.model_validate(structured_output)
        except ValidationError as exc:
            validation_locations, validation_kinds, validation_truncated = (
                _validation_error_contract(exc)
            )
            provider_diagnostics = _provider_validation_diagnostics(
                output_type,
                structured_output,
            )
            lifecycle_event(
                "claude_cli.output.rejected",
                phase="schema_validation",
                output_type=output_type.__name__,
                validation_errors=exc.error_count(),
                validation_locations=validation_locations,
                validation_kinds=validation_kinds,
                validation_truncated=validation_truncated,
                **provider_diagnostics,
            )
            raise ModelOutputError("Claude structured output failed schema validation") from exc
        lifecycle_event(
            "claude_cli.generation.completed",
            output_type=output_type.__name__,
            elapsed_ms=elapsed_ms,
        )
        return parsed

    def _invoke(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        provider_schema_json: str,
    ) -> Mapping[str, object]:
        command = (
            self._executable,
            "--print",
            "--bare",
            "--no-chrome",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--permission-mode",
            "dontAsk",
            "--tools",
            "",
            "--settings",
            self._auth_settings.argument,
            "--system-prompt",
            system_prompt,
            "--output-format",
            "json",
            "--json-schema",
            provider_schema_json,
            "--effort",
            CLAUDE_EFFORT,
            "--model",
            self._model_id,
        )
        started_ns = time.perf_counter_ns()
        try:
            _verify_executable_unchanged(self._executable_identity)
            environment, environment_digest = _controlled_claude_environment()
            if environment_digest != self._runtime_environment_digest:
                raise ModelConfigurationError("Claude runtime routing environment changed")
            current_settings = _resolve_claude_auth_settings(self._approved_remote_provider_id)
            if current_settings.digest != self._auth_settings.digest:
                raise ModelConfigurationError("Claude credential settings changed")
            environment.update(self._auth_settings.process_environment)
        except ModelConfigurationError as exc:
            lifecycle_event(
                "claude_cli.invoke.preflight_failed",
                model_id=self._model_id,
                failure_type=type(exc).__name__,
            )
            raise
        lifecycle_event(
            "claude_cli.invoke.started",
            model_id=self._model_id,
            timeout_seconds=self._timeout_seconds,
        )
        try:
            completed = _run_bounded_command(
                command,
                input_text=user_prompt,
                timeout_seconds=self._timeout_seconds,
                cwd=self._workdir,
                environment=environment,
                stdout_limit=_MAX_CLI_ENVELOPE_BYTES,
                stderr_limit=_MAX_CLI_STDERR_BYTES,
            )
        except FileNotFoundError as exc:
            raise ModelRuntimeError("the Claude CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            lifecycle_event(
                "claude_cli.invoke.timeout",
                model_id=self._model_id,
                timeout_seconds=self._timeout_seconds,
                elapsed_ms=max(0, (time.perf_counter_ns() - started_ns) // 1_000_000),
            )
            raise ModelRuntimeError("Claude request exceeded its deadline") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelRuntimeError("the Claude CLI is unavailable") from exc
        elapsed_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
        if completed.stdout_exceeded or completed.stderr_exceeded:
            lifecycle_event(
                "claude_cli.invoke.output_limit_exceeded",
                model_id=self._model_id,
                elapsed_ms=elapsed_ms,
                stdout_bytes=_utf8_size(completed.stdout),
                stderr_bytes=_utf8_size(completed.stderr),
                stdout_exceeded=completed.stdout_exceeded,
                stderr_exceeded=completed.stderr_exceeded,
                stdout_digest=_stream_digest(completed.stdout),
                stderr_digest=_stream_digest(completed.stderr),
            )
            if completed.returncode == 0:
                raise ModelOutputError("Claude CLI envelope exceeded the body limit")
            raise ModelRuntimeError("the Claude CLI request did not complete")
        if completed.returncode != 0:
            failure_category = _sanitized_cli_failure_category(completed.stdout)
            lifecycle_event(
                "claude_cli.invoke.nonzero_exit",
                model_id=self._model_id,
                return_code=completed.returncode,
                elapsed_ms=elapsed_ms,
                failure_category=failure_category,
                stdout_bytes=_utf8_size(completed.stdout),
                stderr_bytes=_utf8_size(completed.stderr),
                stdout_digest=_stream_digest(completed.stdout),
                stderr_digest=_stream_digest(completed.stderr),
            )
            raise ModelRuntimeError("the Claude CLI request did not complete")
        try:
            envelope = json.loads(completed.stdout)
        except (TypeError, ValueError, UnicodeError) as exc:
            lifecycle_event(
                "claude_cli.invoke.non_json_envelope",
                model_id=self._model_id,
                elapsed_ms=elapsed_ms,
                stdout_bytes=_utf8_size(completed.stdout),
                stderr_bytes=_utf8_size(completed.stderr),
                stdout_digest=_stream_digest(completed.stdout),
                stderr_digest=_stream_digest(completed.stderr),
            )
            raise ModelOutputError("Claude CLI did not return a JSON envelope") from exc
        if not isinstance(envelope, dict):
            raise ModelOutputError("Claude CLI envelope was not a JSON object")
        if envelope.get("is_error") is True or envelope.get("subtype") != "success":
            lifecycle_event(
                "claude_cli.invoke.unsuccessful",
                model_id=self._model_id,
                elapsed_ms=elapsed_ms,
                stdout_bytes=_utf8_size(completed.stdout),
                stderr_bytes=_utf8_size(completed.stderr),
                subtype_digest=_stream_digest(str(envelope.get("subtype") or "unknown")),
                is_error=envelope.get("is_error") is True,
            )
            raise ModelRuntimeError("Claude did not complete the request successfully")
        structured_output = envelope.get("structured_output")
        usage_diagnostics = _usage_diagnostic_fields(
            envelope,
            structured_output=structured_output,
        )
        lifecycle_event(
            "claude_cli.invoke.completed",
            model_id=self._model_id,
            elapsed_ms=elapsed_ms,
            stdout_bytes=_utf8_size(completed.stdout),
            stderr_bytes=_utf8_size(completed.stderr),
            structured_output_bytes=_canonical_json_size(structured_output),
            provider_usage_reported=usage_diagnostics.provider_usage_reported,
            provider_usage_suspect=usage_diagnostics.provider_usage_suspect,
            reported_input_tokens=usage_diagnostics.reported_input_tokens,
            reported_output_tokens=usage_diagnostics.reported_output_tokens,
            reported_total_tokens=usage_diagnostics.reported_total_tokens,
        )
        return envelope


def _usage_evidence(envelope: Mapping[str, object], elapsed_ms: int) -> ModelUsageEvidence:
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return ModelUsageEvidence(latency_ms=elapsed_ms, provider_usage_reported=False)
    input_tokens = _token_sum(
        usage,
        ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"),
    )
    output_tokens = _token_sum(usage, ("output_tokens",))
    if input_tokens is None and output_tokens is None:
        return ModelUsageEvidence(latency_ms=elapsed_ms, provider_usage_reported=False)
    resolved_input = input_tokens or 0
    resolved_output = output_tokens or 0
    return ModelUsageEvidence(
        latency_ms=elapsed_ms,
        provider_usage_reported=True,
        input_tokens=resolved_input,
        output_tokens=resolved_output,
        total_tokens=resolved_input + resolved_output,
    )


def _token_sum(usage: Mapping[str, object], keys: tuple[str, ...]) -> int | None:
    total = 0
    seen = False
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            total += value
            seen = True
    return total if seen else None


__all__ = [
    "ClaudeCliStructuredModelClient",
    "CLAUDE_EFFORT",
    "DEFAULT_CLAUDE_TIMEOUT_SECONDS",
    "MAX_CLAUDE_TIMEOUT_SECONDS",
    "MIN_CLAUDE_TIMEOUT_SECONDS",
]
