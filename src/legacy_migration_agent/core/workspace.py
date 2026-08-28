"""Disposable, manifest-scoped filesystem workspaces.

This module is the trust boundary between generated migration content and a
repository supplied by a user.  It deliberately does not use Git or caller-
reported change metadata: the source tree is copied from an immutable byte
snapshot and the final change set is derived by rescanning the disposable
copy.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from legacy_migration_agent.contracts import validate_relative_path
from legacy_migration_agent.core.policies import PolicyViolation


class WorkspaceViolation(PolicyViolation):
    """Raised when a filesystem operation crosses the workspace boundary."""


@dataclass(frozen=True)
class SnapshotEntry:
    """One immutable regular file in a filesystem snapshot."""

    path: str
    content: bytes
    mode: int


@dataclass(frozen=True)
class TreeSnapshot:
    """A deterministic snapshot of regular files and directory structure."""

    entries: tuple[SnapshotEntry, ...]
    directories: tuple[str, ...]
    revision: str

    def by_path(self) -> dict[str, SnapshotEntry]:
        """Return a fresh path-indexed view without exposing mutable state."""

        return {entry.path: entry for entry in self.entries}


@dataclass(frozen=True)
class WorkspaceChanges:
    """Actual changes observed between the immutable base and workspace disk."""

    before_revision: str
    after_revision: str
    added_paths: tuple[str, ...]
    modified_paths: tuple[str, ...]
    deleted_paths: tuple[str, ...]
    unified_diff: str

    @property
    def changed_paths(self) -> tuple[str, ...]:
        """Return every changed path in deterministic repository order."""

        return tuple(sorted((*self.added_paths, *self.modified_paths, *self.deleted_paths)))


def snapshot_tree(root: Path | str) -> TreeSnapshot:
    """Capture a tree without following symlinks or accepting special files.

    The content revision is intentionally independent of absolute location,
    timestamps, ownership, and file mode.  Each relative path and byte string
    is length-framed before hashing, preventing concatenation ambiguity.
    """

    safe_root = _validated_root(root, role="snapshot")
    entries: list[SnapshotEntry] = []
    directories: list[str] = []
    _scan_directory(safe_root, safe_root, entries, directories)
    ordered_entries = tuple(sorted(entries, key=lambda entry: entry.path))
    ordered_directories = tuple(sorted(directories))
    return TreeSnapshot(
        entries=ordered_entries,
        directories=ordered_directories,
        revision=_content_revision(ordered_entries),
    )


def content_revision(root: Path | str) -> str:
    """Return the deterministic ``sha256:`` content revision for ``root``."""

    return snapshot_tree(root).revision


class IsolatedWorkspace:
    """A disposable copy whose mutations are limited to exact approved paths.

    Construction captures the source once, materializes that captured state in
    a private temporary directory, and verifies that the source did not change
    during the copy.  The caller may use the explicit mutation methods, but the
    final :meth:`audit_changes` always trusts a fresh disk scan instead.
    """

    def __init__(
        self,
        source_root: Path | str,
        approved_paths: Iterable[str],
        *,
        temp_parent: Path | str | None = None,
        expected_revision: str | None = None,
    ) -> None:
        self.source_root = _validated_root(source_root, role="source")
        self._source_identity = _directory_identity(self.source_root, role="source")
        normalized = tuple(_normalize_path(path) for path in approved_paths)
        if not normalized:
            raise WorkspaceViolation("at least one approved path is required")
        if len(normalized) != len(set(normalized)):
            raise WorkspaceViolation("approved paths must be unique")
        self.approved_paths = frozenset(normalized)

        parent: Path | None = None
        if temp_parent is not None:
            parent = _validated_root(temp_parent, role="temporary parent")

        self._base_snapshot = snapshot_tree(self.source_root)
        if expected_revision is not None and expected_revision != self._base_snapshot.revision:
            raise WorkspaceViolation("source content revision does not match the expected revision")

        self._container = Path(
            tempfile.mkdtemp(
                prefix="legacy-migration-workspace-",
                dir=str(parent) if parent is not None else None,
            )
        )
        self._container_identity = _directory_identity(
            self._container,
            role="workspace container",
        )
        self.root = self._container / "repository"
        self._closed = False
        try:
            self.root.mkdir(mode=0o700)
            self._root_identity = _directory_identity(
                self.root,
                role="workspace root",
            )
            _materialize_snapshot(self._base_snapshot, self.root)
            self.assert_source_unchanged()
            if snapshot_tree(self.root) != self._base_snapshot:
                raise WorkspaceViolation("isolated copy does not match the source snapshot")
        except BaseException:
            self.cleanup()
            raise

    @property
    def base_snapshot(self) -> TreeSnapshot:
        return self._base_snapshot

    @property
    def base_revision(self) -> str:
        return self._base_snapshot.revision

    @property
    def closed(self) -> bool:
        return self._closed

    def write_text(self, relative_path: str, content: str) -> Path:
        """Write UTF-8 text to one exact manifest-approved path."""

        if not isinstance(content, str):
            raise TypeError("write_text content must be a string")
        return self.write_bytes(relative_path, content.encode("utf-8"))

    def write_bytes(self, relative_path: str, content: bytes) -> Path:
        """Write textual UTF-8 bytes and reject binary payloads."""

        if not isinstance(content, bytes):
            raise TypeError("write_bytes content must be bytes")
        _decode_text(content, relative_path)
        destination = self._approved_destination(relative_path)
        try:
            mode = stat.S_IMODE(destination.lstat().st_mode)
        except FileNotFoundError:
            mode = 0o644
        _write_regular_file(destination, content, mode=mode)
        return destination

    def delete(self, relative_path: str) -> None:
        """Delete one exact approved regular file without following links."""

        destination = self._approved_destination(relative_path, may_be_missing=True)
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceViolation(f"refusing to delete symlink: {relative_path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceViolation(f"refusing to delete non-regular file: {relative_path}")
        destination.unlink()

    def audit_changes(self) -> WorkspaceChanges:
        """Derive and approve the actual disk delta against the base snapshot."""

        self._require_open()
        after = snapshot_tree(self.root)
        before_by_path = self._base_snapshot.by_path()
        after_by_path = after.by_path()
        before_paths = set(before_by_path)
        after_paths = set(after_by_path)
        added = tuple(sorted(after_paths - before_paths))
        deleted = tuple(sorted(before_paths - after_paths))
        modified = tuple(
            sorted(
                path
                for path in before_paths & after_paths
                if (
                    before_by_path[path].content != after_by_path[path].content
                    or before_by_path[path].mode != after_by_path[path].mode
                )
            )
        )
        changed = set((*added, *modified, *deleted))
        unapproved = tuple(sorted(changed - self.approved_paths))
        if unapproved:
            raise WorkspaceViolation(
                "actual workspace changes are outside the manifest: " + ", ".join(unapproved)
            )

        for path in sorted(changed):
            if path in before_by_path:
                _decode_text(before_by_path[path].content, path)
            if path in after_by_path:
                _decode_text(after_by_path[path].content, path)

        return WorkspaceChanges(
            before_revision=self._base_snapshot.revision,
            after_revision=after.revision,
            added_paths=added,
            modified_paths=modified,
            deleted_paths=deleted,
            unified_diff=_unified_diff(
                before_by_path,
                after_by_path,
                tuple(sorted(changed)),
            ),
        )

    def assert_source_unchanged(self) -> None:
        """Prove that paths, bytes, modes, and directories still match source."""

        self._require_open()
        _require_directory_identity(
            self.source_root,
            self._source_identity,
            role="source",
        )
        current = snapshot_tree(self.source_root)
        if current != self._base_snapshot:
            raise WorkspaceViolation("source tree changed after workspace creation")

    def rollback(self) -> None:
        """Discard every workspace change and restore the captured base."""

        self._require_open()
        _remove_tree(self.root)
        self.root.mkdir(mode=0o700)
        self._root_identity = _directory_identity(
            self.root,
            role="workspace root",
        )
        _materialize_snapshot(self._base_snapshot, self.root)
        if snapshot_tree(self.root) != self._base_snapshot:
            raise WorkspaceViolation("rollback failed to restore the base snapshot")

    def cleanup(self) -> None:
        """Remove the private temporary workspace; safe to call repeatedly."""

        if self._closed:
            return
        _require_directory_identity(
            self._container,
            self._container_identity,
            role="workspace container",
        )
        _remove_tree(self._container)
        self._closed = True

    def __enter__(self) -> IsolatedWorkspace:
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        integrity_error: BaseException | None = None
        try:
            self.assert_source_unchanged()
        except BaseException as caught:
            integrity_error = caught
        finally:
            self.cleanup()
        if exc_type is None and integrity_error is not None:
            raise integrity_error
        return False

    def _approved_destination(
        self,
        relative_path: str,
        *,
        may_be_missing: bool = False,
    ) -> Path:
        self._require_open()
        normalized = _normalize_path(relative_path)
        if normalized not in self.approved_paths:
            raise WorkspaceViolation(f"path is not exactly manifest-approved: {normalized}")
        destination = _confined_path(
            self.root,
            normalized,
            require_file=False,
            permit_missing_leaf=True,
        )
        if not may_be_missing and destination.exists():
            metadata = destination.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise WorkspaceViolation(f"destination is not a regular file: {normalized}")
        return destination

    def _require_open(self) -> None:
        if self._closed:
            raise WorkspaceViolation("workspace is already closed")
        _require_directory_identity(
            self._container,
            self._container_identity,
            role="workspace container",
        )
        _require_directory_identity(
            self.root,
            self._root_identity,
            role="workspace root",
        )


def _normalize_path(raw_path: str) -> str:
    try:
        return validate_relative_path(raw_path)
    except (TypeError, ValueError) as exc:
        raise WorkspaceViolation(f"invalid repository-relative path: {raw_path!r}") from exc


def _validated_root(raw_root: Path | str, *, role: str) -> Path:
    root = Path(raw_root)
    try:
        metadata = root.lstat()
    except FileNotFoundError as exc:
        raise WorkspaceViolation(f"{role} root does not exist: {root}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise WorkspaceViolation(f"{role} root cannot be a symlink: {root}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceViolation(f"{role} root must be a directory: {root}")
    return root.resolve(strict=True)


def _directory_identity(path: Path, *, role: str) -> tuple[int, int]:
    """Return a non-symlink directory identity without resolving the leaf."""

    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise WorkspaceViolation(f"{role} does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise WorkspaceViolation(f"{role} cannot be a symlink: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceViolation(f"{role} must be a directory: {path}")
    return metadata.st_dev, metadata.st_ino


def _require_directory_identity(
    path: Path,
    expected: tuple[int, int],
    *,
    role: str,
) -> None:
    """Fail closed when a pinned directory was removed, linked, or replaced."""

    actual = _directory_identity(path, role=role)
    if actual != expected:
        raise WorkspaceViolation(f"{role} identity changed after workspace creation")


def _scan_directory(
    root: Path,
    directory: Path,
    entries: list[SnapshotEntry],
    directories: list[str],
) -> None:
    with os.scandir(directory) as children:
        ordered = sorted(children, key=lambda child: child.name)
    for child in ordered:
        relative = child.path.removeprefix(f"{root}{os.sep}").replace(os.sep, "/")
        metadata = child.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceViolation(f"symlinks are not allowed in a workspace tree: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(relative)
            _scan_directory(root, Path(child.path), entries, directories)
            continue
        if stat.S_ISREG(metadata.st_mode):
            entries.append(_read_entry(Path(child.path), relative))
            continue
        raise WorkspaceViolation(f"unsupported special file in workspace tree: {relative}")


def _read_entry(path: Path, relative_path: str) -> SnapshotEntry:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceViolation(f"cannot safely open regular file: {relative_path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceViolation(f"unsupported special file in workspace tree: {relative_path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return SnapshotEntry(
            path=relative_path,
            content=b"".join(chunks),
            mode=stat.S_IMODE(metadata.st_mode),
        )
    finally:
        os.close(descriptor)


def _content_revision(entries: tuple[SnapshotEntry, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(b"legacy-migration-content-revision-v1\0")
    for entry in entries:
        encoded_path = entry.path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(entry.content).to_bytes(8, "big"))
        digest.update(entry.content)
    return f"sha256:{digest.hexdigest()}"


def _materialize_snapshot(snapshot: TreeSnapshot, destination: Path) -> None:
    for relative in snapshot.directories:
        directory = destination.joinpath(*relative.split("/"))
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    for entry in snapshot.entries:
        path = destination.joinpath(*entry.path.split("/"))
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _write_regular_file(path, entry.content, mode=entry.mode)


def _confined_path(
    root: Path,
    relative_path: str,
    *,
    require_file: bool,
    permit_missing_leaf: bool = False,
) -> Path:
    normalized = _normalize_path(relative_path)
    candidate = root.joinpath(*normalized.split("/"))
    current = root
    parts = normalized.split("/")
    for part in parts[:-1]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if require_file:
                raise WorkspaceViolation(f"source path does not exist: {normalized}") from None
            current.mkdir(mode=0o755)
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceViolation(f"path contains a symlink: {normalized}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkspaceViolation(f"path parent is not a directory: {normalized}")

    try:
        candidate.relative_to(root)
    except ValueError as exc:  # defensive; lexical validation already rejects traversal
        raise WorkspaceViolation(f"path escapes its supplied root: {normalized}") from exc

    try:
        leaf = candidate.lstat()
    except FileNotFoundError:
        if require_file or not permit_missing_leaf:
            raise WorkspaceViolation(
                f"path does not exist below its supplied root: {normalized}"
            ) from None
        return candidate
    if stat.S_ISLNK(leaf.st_mode):
        raise WorkspaceViolation(f"path resolves through a symlink: {normalized}")
    if require_file and not stat.S_ISREG(leaf.st_mode):
        raise WorkspaceViolation(f"source path is not a regular file: {normalized}")
    return candidate


def _write_regular_file(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, mode & 0o777)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceViolation(f"destination is not a regular file: {path.name}")
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode & 0o777)
    except OSError as exc:
        raise WorkspaceViolation(f"cannot safely write regular file: {path.name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _decode_text(content: bytes, relative_path: str) -> str:
    if b"\x00" in content:
        raise WorkspaceViolation(f"binary content is not allowed for changed path: {relative_path}")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceViolation(f"changed path is not valid UTF-8 text: {relative_path}") from exc


def _unified_diff(
    before: Mapping[str, SnapshotEntry],
    after: Mapping[str, SnapshotEntry],
    changed_paths: tuple[str, ...],
) -> str:
    chunks: list[str] = []
    for path in changed_paths:
        old = before.get(path)
        new = after.get(path)
        chunks.append(f"diff --git a/{path} b/{path}\n")
        if old is None:
            if new is None:
                raise WorkspaceViolation(f"changed path has no before or after state: {path}")
            chunks.append(f"new file mode {_git_mode(new.mode)}\n")
        elif new is None:
            chunks.append(f"deleted file mode {_git_mode(old.mode)}\n")
        elif old.mode != new.mode:
            chunks.append(f"old mode {_git_mode(old.mode)}\n")
            chunks.append(f"new mode {_git_mode(new.mode)}\n")

        old_text = "" if old is None else _decode_text(old.content, path)
        new_text = "" if new is None else _decode_text(new.content, path)
        if old_text == new_text:
            continue
        from_name = "/dev/null" if old is None else f"a/{path}"
        to_name = "/dev/null" if new is None else f"b/{path}"
        generated = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=from_name,
            tofile=to_name,
            lineterm="\n",
        )
        for line in generated:
            chunks.append(line)
            if not line.endswith("\n"):
                chunks.append("\n\\ No newline at end of file\n")
    return "".join(chunks)


def _git_mode(mode: int) -> str:
    """Render the regular-file mode used by Git-compatible diff headers."""

    return f"{stat.S_IFREG | (mode & 0o777):06o}"


def _remove_tree(path: Path) -> None:
    """Remove only the explicitly owned temporary tree, never following links."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        path.unlink()
        return
    shutil.rmtree(path)
