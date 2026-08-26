"""Canonical artifact hashing and immutable local evidence storage."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from legacy_migration_agent.contracts import validate_relative_path
from legacy_migration_agent.core.policies import PolicyViolation


def canonical_json_bytes(value: BaseModel | Any) -> bytes:
    """Serialize a model or JSON-compatible value reproducibly."""

    serializable = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(
        serializable,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def artifact_digest(value: BaseModel | Any) -> str:
    """Return a prefixed SHA-256 digest for an artifact's canonical JSON."""

    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


class ArtifactStore:
    """Write immutable JSON evidence beneath one explicitly selected root.

    Existing identical content is idempotent. Reusing a path for different
    content is rejected so a receipt or approved artifact cannot be silently
    rewritten after review.
    """

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("artifact store root must be a directory")

    def write_json(self, relative_path: str, value: BaseModel | Any) -> Path:
        normalized = validate_relative_path(relative_path)
        if not normalized.endswith(".json"):
            raise ValueError("artifact store accepts only .json destinations")
        parts = normalized.split("/")
        destination = self.root.joinpath(*parts)
        payload = canonical_json_bytes(value) + b"\n"
        with self._parent_directory(parts[:-1], create=True) as parent_fd:
            leaf = parts[-1]
            try:
                existing = _read_file_at(parent_fd, leaf)
            except FileNotFoundError:
                existing = None
            except OSError as exc:
                raise PolicyViolation(
                    f"artifact destination is not a safe regular file: {relative_path}"
                ) from exc
            if existing is not None:
                if existing == payload:
                    return destination
                raise PolicyViolation(
                    f"immutable artifact already exists with different content: {relative_path}"
                )

            temporary_name = f".{leaf}.{uuid4().hex}.tmp"
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
                0o600,
                dir_fd=parent_fd,
            )
            try:
                with os.fdopen(temporary_fd, "wb", closefd=True) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    # Linking an exclusive temporary file publishes without
                    # replacing a concurrent writer's immutable artifact.
                    os.link(
                        temporary_name,
                        leaf,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    concurrent = _read_file_at(parent_fd, leaf)
                    if concurrent != payload:
                        raise PolicyViolation(
                            "immutable artifact was concurrently created with "
                            f"different content: {relative_path}"
                        ) from None
                os.fsync(parent_fd)
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        return destination

    def read_json(self, relative_path: str) -> Any:
        normalized = validate_relative_path(relative_path)
        parts = normalized.split("/")
        try:
            with self._parent_directory(parts[:-1], create=False) as parent_fd:
                payload = _read_file_at(parent_fd, parts[-1])
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise PolicyViolation(f"artifact path is not safe: {relative_path}") from exc
        return json.loads(payload.decode("utf-8"))

    @contextmanager
    def _parent_directory(self, parts: list[str], *, create: bool) -> Iterator[int]:
        """Open path components relative to the store without following links."""

        directory_flags = os.O_RDONLY | _directory_flag() | _no_follow_flag()
        descriptors = [os.open(self.root, directory_flags)]
        try:
            for part in parts:
                try:
                    child = os.open(part, directory_flags, dir_fd=descriptors[-1])
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
                    except FileExistsError:
                        # Another writer may have created this exact parent
                        # after our failed open.  Re-open it through the same
                        # directory descriptor with O_DIRECTORY|O_NOFOLLOW;
                        # a raced symlink or non-directory therefore still
                        # fails closed in the OSError handler below.
                        pass
                    try:
                        child = os.open(part, directory_flags, dir_fd=descriptors[-1])
                    except OSError as exc:
                        raise PolicyViolation(
                            f"artifact parent escapes store root or is not a safe directory: {part}"
                        ) from exc
                except OSError as exc:
                    raise PolicyViolation(
                        f"artifact parent escapes store root or is not a safe directory: {part}"
                    ) from exc
                descriptors.append(child)
            yield descriptors[-1]
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


def _read_file_at(parent_fd: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | _no_follow_flag(), dir_fd=parent_fd)
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _no_follow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as exc:  # pragma: no cover - supported target is POSIX
        raise RuntimeError("secure artifact storage requires O_NOFOLLOW") from exc


def _directory_flag() -> int:
    try:
        return os.O_DIRECTORY
    except AttributeError as exc:  # pragma: no cover - supported target is POSIX
        raise RuntimeError("secure artifact storage requires O_DIRECTORY") from exc
