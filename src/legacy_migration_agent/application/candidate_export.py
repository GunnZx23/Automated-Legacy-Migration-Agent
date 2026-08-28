"""Immutable, deterministic export of generated migration candidates.

The exporter is deliberately unaware of candidate eligibility.  Its caller
selects the terminal validation disposition and the exact UTF-8 files to
export; this module only validates and packages those bytes beneath the
project-local ``output`` directory.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator

from legacy_migration_agent.contracts import Sha256Digest, StrictModel, validate_relative_path
from legacy_migration_agent.core.integrity import canonical_json_bytes
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.redaction import assert_no_high_confidence_secrets

_PLATFORM_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_HANDLE_PATTERN = re.compile(r"^[0-9a-f]{24}$")
_DISPOSITION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_READY_DISPOSITION = "ready_for_human_review"


class CandidateFileDigest(StrictModel):
    """Digest-bound inventory entry for one exported candidate file."""

    path: str
    size_bytes: int = Field(ge=0)
    sha256: Sha256Digest

    @field_validator("path")
    @classmethod
    def require_exact_relative_path(cls, value: str) -> str:
        normalized = validate_relative_path(value)
        if normalized != value:
            raise ValueError("candidate paths must already be canonical POSIX relative paths")
        return value


class CandidateExportResult(StrictModel):
    """Strict, repository-relative result returned to an export caller."""

    schema_version: Literal["1.0"] = "1.0"
    platform: str
    handle: str
    attempt: Literal[1, 2]
    export_root: str
    candidate_path: str
    archive_path: str
    receipt_path: str
    file_count: int = Field(ge=1)
    files: tuple[CandidateFileDigest, ...]
    candidate_digest: Sha256Digest
    archive_kind: Literal["candidate_changes", "source_plus_candidate_overlay"]
    archive_file_count: int = Field(ge=1)
    archive_sha256: Sha256Digest
    receipt_sha256: Sha256Digest
    validation_disposition: str
    ready_for_human_review: bool


class _CandidateExportReceipt(StrictModel):
    """On-disk receipt that intentionally excludes prompts and host paths."""

    schema_version: Literal["1.0"] = "1.0"
    export_kind: Literal["candidate_export"] = "candidate_export"
    platform: str
    handle: str
    attempt: Literal[1, 2]
    export_root: str
    candidate_path: str
    archive_path: str
    receipt_path: str
    validation_disposition: str
    ready_for_human_review: bool
    manifest_digest: Sha256Digest
    change_set_digest: Sha256Digest
    changed_paths: tuple[str, ...]
    file_count: int = Field(ge=1)
    files: tuple[CandidateFileDigest, ...]
    candidate_digest: Sha256Digest
    archive_kind: Literal["candidate_changes", "source_plus_candidate_overlay"]
    archive_file_count: int = Field(ge=1)
    archive_sha256: Sha256Digest


def build_candidate_archive(
    files: Sequence[tuple[str, str]],
    *,
    base_files: Sequence[tuple[str, str]] = (),
) -> bytes:
    """Return a deterministic candidate ZIP, optionally overlaid on frozen source."""

    prepared_base = _prepare_files(base_files) if base_files else ()
    return _candidate_archive(_overlay_files(prepared_base, _prepare_files(files)))


def export_candidate(
    *,
    project_root: Path,
    platform: str,
    handle: str,
    attempt: Literal[1, 2],
    terminal_validation_disposition: str,
    manifest_digest: str,
    change_set_digest: str,
    files: Sequence[tuple[str, str]],
    base_files: Sequence[tuple[str, str]] = (),
) -> CandidateExportResult:
    """Publish exact changed bytes plus a deterministic usable-project archive.

    Replaying an identical export is idempotent.  Existing content is never
    replaced: a different byte, unsafe path component, special file, symlink,
    or non-private export artifact fails closed with :class:`PolicyViolation`.
    The function does not read or mutate migration source files.
    """

    _validate_identity(
        platform=platform,
        handle=handle,
        attempt=attempt,
        validation_disposition=terminal_validation_disposition,
        manifest_digest=manifest_digest,
        change_set_digest=change_set_digest,
    )
    prepared = _prepare_files(files)
    prepared_base = _prepare_files(base_files) if base_files else ()
    archived_files = _overlay_files(prepared_base, prepared)
    inventory = tuple(
        CandidateFileDigest(path=path, size_bytes=len(payload), sha256=_digest(payload))
        for path, payload in prepared
    )
    inventory_value = [entry.model_dump(mode="json") for entry in inventory]
    candidate_digest = _digest(canonical_json_bytes(inventory_value))
    archive = _candidate_archive(archived_files)
    archive_sha256 = _digest(archive)
    archive_kind: Literal["candidate_changes", "source_plus_candidate_overlay"] = (
        "source_plus_candidate_overlay" if prepared_base else "candidate_changes"
    )

    export_root = f"output/{platform}-{handle}/attempt-{attempt}"
    candidate_path = f"{export_root}/candidate"
    archive_path = f"{export_root}/candidate.zip"
    receipt_path = f"{export_root}/export.json"
    ready = terminal_validation_disposition == _READY_DISPOSITION
    receipt = _CandidateExportReceipt(
        platform=platform,
        handle=handle,
        attempt=attempt,
        export_root=export_root,
        candidate_path=candidate_path,
        archive_path=archive_path,
        receipt_path=receipt_path,
        validation_disposition=terminal_validation_disposition,
        ready_for_human_review=ready,
        manifest_digest=manifest_digest,
        change_set_digest=change_set_digest,
        changed_paths=tuple(entry.path for entry in inventory),
        file_count=len(inventory),
        files=inventory,
        candidate_digest=candidate_digest,
        archive_kind=archive_kind,
        archive_file_count=len(archived_files),
        archive_sha256=archive_sha256,
    )
    receipt_payload = canonical_json_bytes(receipt) + b"\n"
    receipt_sha256 = _digest(receipt_payload)

    root_fd = _open_project_root(project_root)
    descriptors = [root_fd]
    try:
        for component in (
            "output",
            f"{platform}-{handle}",
            f"attempt-{attempt}",
        ):
            descriptors.append(
                _open_or_create_private_directory(descriptors[-1], component, component)
            )
        attempt_fd = descriptors[-1]
        candidate_fd = _open_or_create_private_directory(
            attempt_fd,
            "candidate",
            "candidate directory",
        )
        descriptors.append(candidate_fd)

        # Detect every conflict before publishing any missing file. Without this
        # preflight, a replay with an additional candidate path could mutate the
        # candidate directory and only then discover that the immutable archive
        # or receipt belonged to a different export.
        _preflight_immutable_file(attempt_fd, "candidate.zip", archive)
        _preflight_immutable_file(attempt_fd, "export.json", receipt_payload)
        _preflight_candidate_inventory(candidate_fd, prepared)

        for path, payload in prepared:
            _publish_candidate_file(candidate_fd, path, payload)
        _verify_candidate_inventory(candidate_fd, prepared)

        _publish_immutable_file(attempt_fd, "candidate.zip", archive)
        _publish_immutable_file(attempt_fd, "export.json", receipt_payload)
        _verify_export_root(attempt_fd, archive, receipt_payload)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

    return CandidateExportResult(
        platform=platform,
        handle=handle,
        attempt=attempt,
        export_root=export_root,
        candidate_path=candidate_path,
        archive_path=archive_path,
        receipt_path=receipt_path,
        file_count=len(inventory),
        files=inventory,
        candidate_digest=candidate_digest,
        archive_kind=archive_kind,
        archive_file_count=len(archived_files),
        archive_sha256=archive_sha256,
        receipt_sha256=receipt_sha256,
        validation_disposition=terminal_validation_disposition,
        ready_for_human_review=ready,
    )


def _validate_identity(
    *,
    platform: str,
    handle: str,
    attempt: object,
    validation_disposition: str,
    manifest_digest: str,
    change_set_digest: str,
) -> None:
    if not isinstance(platform, str) or _PLATFORM_PATTERN.fullmatch(platform) is None:
        raise ValueError("platform must be a lowercase filesystem-safe slug")
    if not isinstance(handle, str) or _HANDLE_PATTERN.fullmatch(handle) is None:
        raise ValueError("handle must contain exactly 24 lowercase hexadecimal characters")
    if type(attempt) is not int or attempt not in {1, 2}:
        raise ValueError("attempt must be exactly 1 or 2")
    if (
        not isinstance(validation_disposition, str)
        or _DISPOSITION_PATTERN.fullmatch(validation_disposition) is None
    ):
        raise ValueError("terminal validation disposition must be a lowercase identifier")
    for role, value in (
        ("manifest digest", manifest_digest),
        ("change-set digest", change_set_digest),
    ):
        if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{role} must be a prefixed SHA-256 digest")


def _prepare_files(files: Sequence[tuple[str, str]]) -> tuple[tuple[str, bytes], ...]:
    prepared: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for pair in files:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise TypeError("each candidate file must be an exact (path, content) tuple")
        path, content = pair
        if not isinstance(path, str) or not isinstance(content, str):
            raise TypeError("candidate paths and contents must be strings")
        normalized = validate_relative_path(path)
        if normalized != path:
            raise ValueError("candidate paths must already be canonical POSIX relative paths")
        if path in seen:
            raise ValueError(f"candidate file path is duplicated: {path}")
        seen.add(path)
        assert_no_high_confidence_secrets(
            content,
            boundary="candidate content",
        )
        try:
            payload = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError(f"candidate content is not valid UTF-8 text: {path}") from exc
        prepared.append((path, payload))
    if not prepared:
        raise ValueError("candidate export requires at least one file")

    ordered = tuple(sorted(prepared, key=lambda item: item[0]))
    paths = [path for path, _payload in ordered]
    for index, path in enumerate(paths):
        prefix = f"{path}/"
        if any(other.startswith(prefix) for other in paths[index + 1 :]):
            raise ValueError(f"candidate file conflicts with a descendant path: {path}")
    return ordered


def _overlay_files(
    base_files: tuple[tuple[str, bytes], ...],
    candidate_files: tuple[tuple[str, bytes], ...],
) -> tuple[tuple[str, bytes], ...]:
    """Overlay candidate bytes on source bytes without inventing extra artifacts."""

    merged = dict(base_files)
    merged.update(candidate_files)
    ordered = tuple(sorted(merged.items()))
    paths = [path for path, _payload in ordered]
    for index, path in enumerate(paths):
        prefix = f"{path}/"
        if any(other.startswith(prefix) for other in paths[index + 1 :]):
            raise ValueError(f"archive file conflicts with a descendant path: {path}")
    return ordered


def _candidate_archive(files: tuple[tuple[str, bytes], ...]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as bundle:
        for path, payload in files:
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            bundle.writestr(info, payload)
    return buffer.getvalue()


def _open_project_root(project_root: Path) -> int:
    try:
        root = Path(project_root).absolute()
        expected = root.lstat()
    except (FileNotFoundError, OSError, TypeError) as exc:
        raise PolicyViolation("project root is unavailable") from exc
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
        raise PolicyViolation("project root must be a non-symlink directory")
    try:
        descriptor = os.open(root, _directory_flags())
    except OSError as exc:
        raise PolicyViolation("project root could not be opened safely") from exc
    actual = os.fstat(descriptor)
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(descriptor)
        raise PolicyViolation("project root changed while being opened")
    return descriptor


def _open_or_create_private_directory(parent_fd: int, name: str, role: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise PolicyViolation(f"{role} could not be created safely") from exc
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise PolicyViolation(f"{role} is not a safe directory") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        os.close(descriptor)
        raise PolicyViolation(f"{role} must be an owner-private directory")
    return descriptor


def _publish_candidate_file(root_fd: int, path: str, payload: bytes) -> None:
    parts = path.split("/")
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child_fd = _open_or_create_private_directory(
                parent_fd,
                part,
                f"candidate parent for {path}",
            )
            os.close(parent_fd)
            parent_fd = child_fd
        _publish_immutable_file(parent_fd, parts[-1], payload)
    finally:
        os.close(parent_fd)


def _publish_immutable_file(parent_fd: int, name: str, payload: bytes) -> None:
    try:
        existing = _read_private_regular_file(parent_fd, name)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing != payload:
            raise PolicyViolation(f"immutable export file already has different bytes: {name}")
        return

    temporary = f".candidate-export-{uuid4().hex}.tmp"
    try:
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise PolicyViolation("candidate export staging file could not be created") from exc
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
            ):
                raise PolicyViolation("candidate export staging file is not private")
        finally:
            os.close(descriptor)
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            concurrent = _read_private_regular_file(parent_fd, name)
            if concurrent != payload:
                raise PolicyViolation(
                    f"immutable export file was concurrently created with different bytes: {name}"
                ) from None
        except OSError as exc:
            raise PolicyViolation(f"immutable export file could not be published: {name}") from exc
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
    if _read_private_regular_file(parent_fd, name) != payload:
        raise PolicyViolation(f"immutable export file failed read-back verification: {name}")


def _preflight_immutable_file(parent_fd: int, name: str, payload: bytes) -> None:
    """Reject an existing conflicting immutable file without changing the export tree."""

    try:
        existing = _read_private_regular_file(parent_fd, name)
    except FileNotFoundError:
        return
    if existing != payload:
        raise PolicyViolation(f"immutable export file already has different bytes: {name}")


def _read_private_regular_file(parent_fd: int, name: str) -> bytes:
    expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(expected.st_mode)
        or stat.S_IMODE(expected.st_mode) != 0o600
        or expected.st_uid != os.geteuid()
        or expected.st_nlink != 1
    ):
        raise PolicyViolation(f"export path is not an owner-private regular file: {name}")
    try:
        descriptor = os.open(name, os.O_RDONLY | _no_follow_flag(), dir_fd=parent_fd)
    except OSError as exc:
        raise PolicyViolation(f"export file could not be opened safely: {name}") from exc
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise PolicyViolation(f"export file changed while being opened: {name}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _verify_candidate_inventory(
    candidate_fd: int,
    expected_files: tuple[tuple[str, bytes], ...],
) -> None:
    actual_files: dict[str, bytes] = {}
    actual_directories: set[str] = set()
    _scan_candidate_directory(candidate_fd, "", actual_files, actual_directories)
    expected = dict(expected_files)
    expected_directories: set[str] = set()
    for path in expected:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            expected_directories.add("/".join(parts[:depth]))
    if actual_directories != expected_directories or set(actual_files) != set(expected):
        raise PolicyViolation("candidate directory inventory differs from the requested export")
    for path, payload in expected.items():
        if actual_files[path] != payload:
            raise PolicyViolation(f"candidate file failed exact-byte verification: {path}")


def _preflight_candidate_inventory(
    candidate_fd: int,
    expected_files: tuple[tuple[str, bytes], ...],
) -> None:
    """Require all existing candidate entries to be a matching subset of the requested export."""

    actual_files: dict[str, bytes] = {}
    actual_directories: set[str] = set()
    _scan_candidate_directory(candidate_fd, "", actual_files, actual_directories)
    expected = dict(expected_files)
    expected_directories: set[str] = set()
    for path in expected:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            expected_directories.add("/".join(parts[:depth]))
    unexpected_directories = actual_directories - expected_directories
    unexpected_files = set(actual_files) - set(expected)
    if unexpected_directories or unexpected_files:
        raise PolicyViolation("candidate directory contains entries outside the requested export")
    for path, payload in actual_files.items():
        if payload != expected[path]:
            raise PolicyViolation(f"candidate file already has different bytes: {path}")


def _scan_candidate_directory(
    directory_fd: int,
    prefix: str,
    files: dict[str, bytes],
    directories: set[str],
) -> None:
    for name in sorted(os.listdir(directory_fd)):
        path = f"{prefix}/{name}" if prefix else name
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.geteuid():
                raise PolicyViolation(f"candidate directory is not owner-private: {path}")
            try:
                child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
            except OSError as exc:
                raise PolicyViolation(
                    f"candidate directory could not be opened safely: {path}"
                ) from exc
            directories.add(path)
            try:
                _scan_candidate_directory(child_fd, path, files, directories)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            files[path] = _read_private_regular_file(directory_fd, name)
        elif stat.S_ISLNK(metadata.st_mode):
            raise PolicyViolation(f"candidate path uses a symlink: {path}")
        else:
            raise PolicyViolation(f"candidate path is not a regular file: {path}")


def _verify_export_root(attempt_fd: int, archive: bytes, receipt: bytes) -> None:
    if set(os.listdir(attempt_fd)) != {"candidate", "candidate.zip", "export.json"}:
        raise PolicyViolation("export root contains unexpected entries")
    candidate = os.stat("candidate", dir_fd=attempt_fd, follow_symlinks=False)
    if not stat.S_ISDIR(candidate.st_mode) or stat.S_IMODE(candidate.st_mode) != 0o700:
        raise PolicyViolation("candidate export path is not a private directory")
    if _read_private_regular_file(attempt_fd, "candidate.zip") != archive:
        raise PolicyViolation("candidate archive failed exact-byte verification")
    if _read_private_regular_file(attempt_fd, "export.json") != receipt:
        raise PolicyViolation("candidate receipt failed exact-byte verification")


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:  # pragma: no cover - defensive operating-system guard
            raise PolicyViolation("candidate export could not be written completely")
        remaining = remaining[written:]


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _no_follow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as exc:  # pragma: no cover - supported target is POSIX
        raise RuntimeError("secure candidate export requires O_NOFOLLOW") from exc


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    except AttributeError as exc:  # pragma: no cover - supported target is POSIX
        raise RuntimeError("secure candidate export requires O_DIRECTORY and O_NOFOLLOW") from exc
