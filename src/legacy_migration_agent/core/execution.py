"""Constrained execution boundary for predeclared validation commands.

The migration workflow never executes model-generated shell text.  Callers
select a command from :class:`~legacy_migration_agent.core.policies.CommandRegistry`
and receive a portable receipt plus bounded diagnostic output.  Command
arguments are accepted only when they exactly match a vector declared in the
registry, and each
process receives the spec's exact sanitized environment rather than inheriting
the host environment. This is a policy boundary, not an OS sandbox: an
allowlisted executable still has the filesystem and process privileges of this
Python process. Deployments needing hostile-code isolation must add a separate
container or operating-system sandbox.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from legacy_migration_agent.contracts import (
    ArtifactDigest,
    EnvironmentKind,
    ToolReceipt,
    validate_relative_path,
)
from legacy_migration_agent.core.policies import (
    CommandRegistry,
    ExecutionApproval,
    PolicyViolation,
    RetryBudget,
    ensure_paths_within_repository,
    require_execution_approval,
)
from legacy_migration_agent.core.redaction import SecretRedactor

DEFAULT_MAX_OUTPUT_CHARS = 16_384
DEFAULT_TIMEOUT_SECONDS = 60.0
TIMEOUT_EXIT_CODE = 124
OUTPUT_LIMIT_EXIT_CODE = 125


@dataclass(frozen=True)
class ExecutionResult:
    """Result returned to the validator after one bounded command attempt."""

    receipt: ToolReceipt
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


def execution_binding(
    request_id: str,
    run_id: str,
    command_id: str,
    attempt: int,
    base_revision: str,
    environment: EnvironmentKind,
    artifact_digest: str,
) -> str:
    """Return the stable digest that binds a receipt to its execution context."""

    if not request_id or not run_id or not command_id or not base_revision:
        raise ValueError("execution binding identifiers and base_revision must be non-empty")
    if attempt < 1:
        raise ValueError("attempt must be positive")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest) is None:
        raise ValueError("artifact_digest must be a sha256 digest")
    material = "\x00".join(
        (
            request_id,
            run_id,
            command_id,
            str(attempt),
            base_revision,
            environment.value,
            artifact_digest,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class SafeCommandRunner:
    """Execute allowlisted commands with repository-path policy checks.

    ``CommandRegistry`` supplies the executable and fixed argument prefix.
    ``SafeCommandRunner`` adds repository confinement, retry and timeout limits,
    bounded output, exact approval checks, sanitized process environments, and
    evidence receipts. It always invokes ``subprocess`` with an argument
    vector; no shell is involved. Repository confinement here validates paths
    named by the workflow, but does not confine arbitrary accesses performed by
    the executable itself.
    """

    def __init__(
        self,
        registry: CommandRegistry,
        repository_root: Path,
        retry_budget: RetryBudget,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        redactor: SecretRedactor | None = None,
    ) -> None:
        root = repository_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("repository_root must be a directory")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_output_chars < 1:
            raise ValueError("max_output_chars must be positive")
        self._registry = registry
        self._repository_root = root
        self._retry_budget = retry_budget
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars
        self._redactor = redactor or SecretRedactor()
        self._consumed_approval_nonces: set[str] = set()
        self._nonce_lock = threading.Lock()

    def run(
        self,
        command_id: str,
        *,
        request_id: str,
        run_id: str,
        attempt: int,
        base_revision: str,
        environment: EnvironmentKind,
        artifact_digest: str,
        working_directory: Path,
        args: Sequence[str] = (),
        decision_id: str | None = None,
        approval: ExecutionApproval | None = None,
        timeout_seconds: float | None = None,
    ) -> ExecutionResult:
        """Execute one allowlisted command and emit a terminal receipt.

        Policy errors (unknown commands, exhausted retry budgets, or path
        escapes) are rejected before spawning a process and therefore do not
        produce a misleading execution receipt.
        """

        if attempt < 1:
            raise ValueError("attempt must be positive")
        self._retry_budget.require_remaining(attempt - 1)
        resolved_workdir = self._resolve_working_directory(working_directory)
        resolved_command = self._registry.resolve(
            command_id,
            tuple(args),
            resolved_workdir,
            environment,
        )
        self._prepare_artifact_paths(resolved_command.spec.artifact_paths)

        effective_timeout = self._timeout_seconds if timeout_seconds is None else timeout_seconds
        if effective_timeout <= 0:
            raise ValueError("timeout_seconds must be positive")

        binding = execution_binding(
            request_id,
            run_id,
            command_id,
            attempt,
            base_revision,
            environment,
            artifact_digest,
        )
        require_execution_approval(
            resolved_command.spec,
            decision_id=decision_id,
            request_id=request_id,
            artifact_digest=artifact_digest,
            base_revision=base_revision,
            command_id=command_id,
            environment=environment,
            approval=approval,
        )
        if resolved_command.spec.requires_approval is not None and approval and approval.nonce:
            self._consume_approval_nonce(approval.nonce)

        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        exit_code, raw_stdout, raw_stderr, timed_out, output_exceeded = _run_bounded_process(
            resolved_command.argv,
            cwd=resolved_workdir,
            environment=resolved_command.spec.environment_map(),
            timeout_seconds=effective_timeout,
            max_output_bytes=self._max_output_chars * 4,
        )
        if timed_out:
            timeout_message = f"command timed out after {effective_timeout:g} seconds"
            raw_stderr = f"{raw_stderr}\n{timeout_message}" if raw_stderr else timeout_message
        elif output_exceeded:
            limit_message = "command exceeded the controller output limit"
            raw_stderr = f"{raw_stderr}\n{limit_message}" if raw_stderr else limit_message

        duration_seconds = max(0.0, time.monotonic() - started_monotonic)
        ended_at = datetime.now(UTC)
        redacted_stdout, stdout_changed = _redact_output(self._redactor, raw_stdout)
        redacted_stderr, stderr_changed = _redact_output(self._redactor, raw_stderr)
        stdout = _bounded(redacted_stdout, self._max_output_chars)
        stderr = _bounded(redacted_stderr, self._max_output_chars)
        artifacts = self._collect_artifact_digests(resolved_command.spec.artifact_paths)
        receipt = ToolReceipt(
            receipt_id=f"receipt-{binding[:24]}",
            tool_id=command_id,
            request_id=request_id,
            run_id=run_id,
            attempt=attempt,
            base_revision=base_revision,
            environment=environment,
            input_artifact_digest=artifact_digest,
            operation=f"allowlisted-command:{command_id};binding:{binding}",
            working_directory=self._portable_working_directory(resolved_workdir),
            started_at=started_at,
            ended_at=ended_at,
            exit_code=exit_code,
            terminal=True,
            stdout_digest=_digest(redacted_stdout),
            stderr_digest=_digest(redacted_stderr),
            artifacts=artifacts,
            redacted=stdout_changed or stderr_changed,
        )
        return ExecutionResult(
            receipt=receipt,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            timed_out=timed_out,
        )

    def _resolve_working_directory(self, working_directory: Path) -> Path:
        try:
            resolved = working_directory.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PolicyViolation("working directory does not exist") from exc
        if not resolved.is_dir():
            raise PolicyViolation("working directory is not a directory")
        try:
            resolved.relative_to(self._repository_root)
        except ValueError as exc:
            raise PolicyViolation("working directory escapes repository root") from exc
        return resolved

    def _portable_working_directory(self, working_directory: Path) -> str:
        relative = working_directory.relative_to(self._repository_root)
        return relative.as_posix() or "."

    def _prepare_artifact_paths(self, artifact_paths: Sequence[str]) -> tuple[str, ...]:
        """Validate declared locations before spawn without claiming outputs exist."""

        normalized = tuple(validate_relative_path(path) for path in artifact_paths)
        ensure_paths_within_repository(self._repository_root, normalized)
        return normalized

    def _collect_artifact_digests(
        self,
        artifact_paths: Sequence[str],
    ) -> tuple[ArtifactDigest, ...]:
        """Verify and hash declared artifacts after the process terminates.

        Every path component is checked for symlinks and the final object is
        opened with ``O_NOFOLLOW`` when the platform supports it. These checks
        reduce path-substitution risk but are not a replacement for an OS
        sandbox against a concurrently hostile process.
        """

        normalized = self._prepare_artifact_paths(artifact_paths)
        artifacts: list[ArtifactDigest] = []
        for path in normalized:
            candidate = self._repository_root.joinpath(*path.split("/"))
            self._reject_symlink_components(candidate, path)
            try:
                metadata = candidate.lstat()
            except FileNotFoundError as exc:
                raise PolicyViolation(f"declared artifact was not produced: {path}") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise PolicyViolation(
                    f"declared artifact is not a regular non-symlink file: {path}"
                )
            try:
                candidate.resolve(strict=True).relative_to(self._repository_root)
            except (FileNotFoundError, ValueError) as exc:
                raise PolicyViolation(f"declared artifact escapes repository root: {path}") from exc

            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(candidate, flags)
            except OSError as exc:
                raise PolicyViolation(
                    f"declared artifact could not be opened safely: {path}"
                ) from exc
            try:
                opened_before = os.fstat(descriptor)
                if not stat.S_ISREG(opened_before.st_mode):
                    raise PolicyViolation(f"declared artifact is not a regular file: {path}")
                digest = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                opened_after = os.fstat(descriptor)
            finally:
                os.close(descriptor)

            if (
                opened_before.st_dev,
                opened_before.st_ino,
                opened_before.st_size,
                opened_before.st_mtime_ns,
            ) != (
                opened_after.st_dev,
                opened_after.st_ino,
                opened_after.st_size,
                opened_after.st_mtime_ns,
            ):
                raise PolicyViolation(f"declared artifact changed while it was hashed: {path}")
            current = candidate.lstat()
            if (current.st_dev, current.st_ino) != (opened_after.st_dev, opened_after.st_ino):
                raise PolicyViolation(f"declared artifact was replaced while it was hashed: {path}")
            artifacts.append(
                ArtifactDigest(
                    path=path,
                    sha256=f"sha256:{digest.hexdigest()}",
                    size_bytes=opened_after.st_size,
                )
            )
        return tuple(artifacts)

    def _reject_symlink_components(self, candidate: Path, portable_path: str) -> None:
        current = self._repository_root
        for part in candidate.relative_to(self._repository_root).parts:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError as exc:
                raise PolicyViolation(
                    f"declared artifact was not produced: {portable_path}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise PolicyViolation(f"declared artifact uses a symlink path: {portable_path}")

    def _consume_approval_nonce(self, nonce: str) -> None:
        with self._nonce_lock:
            if nonce in self._consumed_approval_nonces:
                raise PolicyViolation("execution approval nonce has already been used")
            self._consumed_approval_nonces.add(nonce)


def _run_bounded_process(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    max_output_bytes: int,
) -> tuple[int, str, str, bool, bool]:
    """Stream bounded output and terminate the full child process group."""

    process = subprocess.Popen(  # noqa: S603 - argv is resolved by CommandRegistry
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=dict(environment),
        close_fds=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    lengths = {"stdout": 0, "stderr": 0}
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    output_exceeded = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process_group(process)
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                payload = os.read(key.fd, 8192)
                stream = str(key.data)
                if not payload:
                    selector.unregister(key.fileobj)
                    continue
                available = max_output_bytes - lengths[stream]
                if available > 0:
                    retained = payload[:available]
                    chunks[stream].append(retained)
                    lengths[stream] += len(retained)
                if len(payload) > available:
                    output_exceeded = True
                    _kill_process_group(process)
                    break
            if output_exceeded:
                break
        if timed_out:
            exit_code = TIMEOUT_EXIT_CODE
        elif output_exceeded:
            exit_code = OUTPUT_LIMIT_EXIT_CODE
        else:
            exit_code = process.wait(timeout=5)
        if timed_out or output_exceeded:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_process_group(process)
                process.wait(timeout=5)
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            _kill_process_group(process)
            process.wait(timeout=5)
    stdout = b"".join(chunks["stdout"]).decode("utf-8", errors="replace")
    stderr = b"".join(chunks["stderr"]).decode("utf-8", errors="replace")
    return exit_code, stdout, stderr, timed_out, output_exceeded


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()


def _redact_output(redactor: SecretRedactor, output: str) -> tuple[str, bool]:
    """Apply redaction while preserving the process output's outer whitespace.

    ``RedactionResult`` is a strict contract whose string normalization trims
    outer whitespace. Receipts, however, digest the complete redacted stream,
    so the original prefix and suffix are restored after redaction.
    """

    if not output or not output.strip():
        result = redactor.redact(output)
        return output, result.changed
    content_start = len(output) - len(output.lstrip())
    content_end = len(output.rstrip())
    result = redactor.redact(output[content_start:content_end])
    return f"{output[:content_start]}{result.text}{output[content_end:]}", result.changed


def _bounded(output: str, limit: int) -> str:
    if len(output) <= limit:
        return output
    marker = "\n...[output truncated]"
    if limit <= len(marker):
        return marker[:limit]
    return f"{output[: limit - len(marker)]}{marker}"


def _digest(output: str) -> str:
    return f"sha256:{hashlib.sha256(output.encode('utf-8')).hexdigest()}"
