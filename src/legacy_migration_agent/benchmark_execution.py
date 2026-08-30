"""Immutable pre-run authority for the measured 18-cell benchmark.

The execution anchor binds the verified benchmark protocol to creation-time Git
provenance and the exact runtime-influencing working-tree bytes.  A dirty
worktree is allowed: ``runtime_content_revision`` is the drift authority for
enumerated runtime content, while the original commit and tree OIDs remain
independently reportable provenance.  Verification requires those original Git
objects to remain available and related, but it does not require the repository
to remain at the original HEAD.  A later commit therefore does not invalidate an
anchor when every enumerated runtime byte is unchanged.

Building and verifying an anchor are read-only operations.  Writing is explicit,
immutable, and rejects symlinks in every destination component.  The anchor's
``created_at`` value is caller supplied; it is not a trusted timestamp.  Proving
that an anchor existed before execution therefore requires external publication
of its digest (for example, a timestamp service or a separately published commit)
before the first benchmark cell is launched.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter, field_validator, model_validator

from legacy_migration_agent.benchmark_protocol import (
    BENCHMARK_DECLARATION_PATH,
    BENCHMARK_REGISTRY_PATH,
    VerifiedBenchmarkProtocol,
    load_verified_benchmark_protocol,
)
from legacy_migration_agent.contracts import (
    Identifier,
    Revision,
    Sha256Digest,
    StrictModel,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest, canonical_json_bytes
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.workspace import content_revision

_GitObjectId = Annotated[str, Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")]
_EXPECTED_RUNTIME_IDENTITY = TypeAdapter(Sha256Digest)

_BASE_RUNTIME_PATHS: dict[str, Literal["file", "tree"]] = {
    "src/legacy_migration_agent": "tree",
    "agents": "tree",
    "knowledge/wiki": "tree",
    "pyproject.toml": "file",
    "uv.lock": "file",
}

# These controller-owned files influence deterministic validation outcomes but
# live outside the Python package.  node_modules is intentionally represented by
# the pinned lockfile and the code-owned installed-tree fingerprint, not scanned:
# npm installations contain symlinks and are environment evidence, not source.
_RUNTIME_TOOLING_PATHS: dict[str, Literal["file", "tree"]] = {
    "tooling/lwc-jest/package.json": "file",
    "tooling/lwc-jest/package-lock.json": "file",
    "tooling/lwc-jest/jest.config.js": "file",
    "tooling/lwc-jest/jest.setup.js": "file",
    "tooling/lwc-jest/controller-tests/accountContactExplorer.controller.test.js": "file",
    "tooling/lwc-jest/controller-tests/caseManagementConsole.controller.test.js": "file",
    "tooling/mulesoft-runtime/authority.json": "file",
    "tooling/mulesoft-runtime/behavior-contract.json": "file",
    "tooling/mulesoft-runtime/controller-tests/customer-status-behavior-test.xml": "file",
}

_LOCKFILE_PATHS = (
    "tooling/lwc-jest/package-lock.json",
    "uv.lock",
)


class RuntimeInfluenceEntry(StrictModel):
    """One path and its exact file or tree content identity."""

    path: str
    kind: Literal["file", "tree"]
    revision: Sha256Digest

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class LockfileDigest(StrictModel):
    """One dependency lock included in the runtime inventory."""

    path: str
    digest: Sha256Digest

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class BenchmarkExecutionAnchor(StrictModel):
    """Strict authority binding runtime bytes to creation-time Git provenance."""

    schema_version: Literal["1.0"] = "1.0"
    anchor_id: Identifier
    created_at: datetime
    protocol_declaration_digest: Sha256Digest
    benchmark_registry_digest: Sha256Digest
    supporting_artifact_paths: tuple[str, ...] = Field(min_length=1, max_length=128)
    supporting_artifact_graph_digest: Sha256Digest
    git_head_commit_oid: _GitObjectId
    git_head_tree_oid: _GitObjectId
    runtime_inventory: tuple[RuntimeInfluenceEntry, ...] = Field(
        min_length=1,
        max_length=256,
    )
    runtime_content_revision: Sha256Digest
    wiki_tree_revision: Revision
    agent_definitions_digest: Sha256Digest
    lockfile_digests: tuple[LockfileDigest, ...] = Field(min_length=2, max_length=16)
    provider_id: Literal["claude-cli"] = "claude-cli"
    model_id: Literal["claude-sonnet-5"] = "claude-sonnet-5"
    execution_boundary: Literal["remote_provider_managed"] = "remote_provider_managed"
    runtime_identity_digest: Sha256Digest

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("benchmark execution anchor created_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("supporting_artifact_paths")
    @classmethod
    def validate_supporting_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_relative_path(value) for value in values)
        if normalized != tuple(sorted(normalized)) or len(normalized) != len(set(normalized)):
            raise ValueError("supporting artifact paths must be unique and sorted")
        return normalized

    @model_validator(mode="after")
    def validate_internal_bindings(self) -> BenchmarkExecutionAnchor:
        inventory_paths = tuple(entry.path for entry in self.runtime_inventory)
        if inventory_paths != tuple(sorted(inventory_paths)):
            raise ValueError("runtime influence inventory must be sorted")
        if len(inventory_paths) != len(set(inventory_paths)):
            raise ValueError("runtime influence inventory paths must be unique")
        if self.runtime_content_revision != _inventory_revision(self.runtime_inventory):
            raise ValueError("runtime content revision does not match its explicit inventory")

        entries_by_path = {entry.path: entry for entry in self.runtime_inventory}
        if any(path not in entries_by_path for path in self.supporting_artifact_paths):
            raise ValueError("supporting artifact path is absent from the runtime inventory")
        supporting_entries = tuple(entries_by_path[path] for path in self.supporting_artifact_paths)
        if self.supporting_artifact_graph_digest != _supporting_graph_digest(supporting_entries):
            raise ValueError("supporting artifact graph digest does not match its inventory")

        lock_paths = tuple(item.path for item in self.lockfile_digests)
        if lock_paths != tuple(sorted(lock_paths)) or len(lock_paths) != len(set(lock_paths)):
            raise ValueError("lockfile digests must be unique and sorted")
        for item in self.lockfile_digests:
            entry = entries_by_path.get(item.path)
            if entry is None or entry.kind != "file" or entry.revision != item.digest:
                raise ValueError("lockfile digest does not match the runtime inventory")

        wiki_entry = entries_by_path.get("knowledge/wiki")
        if (
            wiki_entry is None
            or wiki_entry.kind != "tree"
            or wiki_entry.revision != self.wiki_tree_revision
        ):
            raise ValueError("complete Wiki revision does not match the runtime inventory")
        return self

    @property
    def anchor_digest(self) -> Sha256Digest:
        """Return the canonical digest suitable for external publication."""

        return artifact_digest(self)


@dataclass(frozen=True, slots=True)
class _CapturedExecutionState:
    protocol_declaration_digest: Sha256Digest
    benchmark_registry_digest: Sha256Digest
    supporting_artifact_paths: tuple[str, ...]
    supporting_artifact_graph_digest: Sha256Digest
    git_head_commit_oid: str
    git_head_tree_oid: str
    runtime_inventory: tuple[RuntimeInfluenceEntry, ...]
    runtime_content_revision: Sha256Digest
    wiki_tree_revision: Revision
    agent_definitions_digest: Sha256Digest
    lockfile_digests: tuple[LockfileDigest, ...]


def build_benchmark_execution_anchor(
    project_root: Path,
    *,
    runtime_identity_digest: str,
    created_at: datetime,
    anchor_id: str,
) -> BenchmarkExecutionAnchor:
    """Build an anchor without mutating the repository or invoking a model."""

    root = _validated_project_root(project_root)
    state = _capture_execution_state(root)
    try:
        return BenchmarkExecutionAnchor(
            anchor_id=anchor_id,
            created_at=created_at,
            protocol_declaration_digest=state.protocol_declaration_digest,
            benchmark_registry_digest=state.benchmark_registry_digest,
            supporting_artifact_paths=state.supporting_artifact_paths,
            supporting_artifact_graph_digest=state.supporting_artifact_graph_digest,
            git_head_commit_oid=state.git_head_commit_oid,
            git_head_tree_oid=state.git_head_tree_oid,
            runtime_inventory=state.runtime_inventory,
            runtime_content_revision=state.runtime_content_revision,
            wiki_tree_revision=state.wiki_tree_revision,
            agent_definitions_digest=state.agent_definitions_digest,
            lockfile_digests=state.lockfile_digests,
            runtime_identity_digest=runtime_identity_digest,
        )
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("benchmark execution anchor inputs are invalid") from exc


def verify_benchmark_execution_anchor(
    project_root: Path,
    anchor: BenchmarkExecutionAnchor,
    *,
    expected_runtime_identity_digest: str | None = None,
) -> BenchmarkExecutionAnchor:
    """Verify current runtime bytes and the anchor's original Git provenance.

    ``expected_runtime_identity_digest`` should always be supplied by the live
    caller immediately before a cell is launched.  When omitted, this function
    can validate the stored identity's syntax and every local binding, but it
    cannot infer an authenticated remote Claude session identity.

    The stored commit and tree are creation-time provenance.  Their objects must
    still exist and the commit must reference the stored tree, but current HEAD
    may advance when the enumerated runtime inventory remains byte-identical.
    """

    frozen = BenchmarkExecutionAnchor.model_validate(anchor.model_dump(mode="python"))
    _verify_expected_runtime_identity(frozen, expected_runtime_identity_digest)
    root = _validated_project_root(project_root)
    git_provenance = (frozen.git_head_commit_oid, frozen.git_head_tree_oid)
    _verify_original_git_provenance(root, *git_provenance)
    state = _capture_execution_state(root, frozen_git_provenance=git_provenance)
    _verify_original_git_provenance(root, *git_provenance)
    comparisons = {
        "protocol declaration": (
            frozen.protocol_declaration_digest,
            state.protocol_declaration_digest,
        ),
        "benchmark registry": (frozen.benchmark_registry_digest, state.benchmark_registry_digest),
        "supporting artifact paths": (
            frozen.supporting_artifact_paths,
            state.supporting_artifact_paths,
        ),
        "supporting artifact graph": (
            frozen.supporting_artifact_graph_digest,
            state.supporting_artifact_graph_digest,
        ),
        "runtime inventory": (frozen.runtime_inventory, state.runtime_inventory),
        "runtime content": (frozen.runtime_content_revision, state.runtime_content_revision),
        "complete Wiki tree": (frozen.wiki_tree_revision, state.wiki_tree_revision),
        "agent definitions": (
            frozen.agent_definitions_digest,
            state.agent_definitions_digest,
        ),
        "lockfiles": (frozen.lockfile_digests, state.lockfile_digests),
    }
    for label, (expected, observed) in comparisons.items():
        if expected != observed:
            raise PolicyViolation(f"benchmark execution anchor {label} drifted")
    return frozen


def write_benchmark_execution_anchor(
    path: Path,
    anchor: BenchmarkExecutionAnchor,
) -> Path:
    """Write one immutable canonical JSON anchor without following symlinks."""

    frozen = BenchmarkExecutionAnchor.model_validate(anchor.model_dump(mode="python"))
    destination = _absolute_lexical_path(path)
    if destination.suffix != ".json":
        raise PolicyViolation("benchmark execution anchor destination must end in .json")
    repository_root = _discover_containing_git_root(destination)
    if repository_root is not None:
        _reject_inventory_destination(repository_root, destination, frozen.runtime_inventory)
    payload = canonical_json_bytes(frozen) + b"\n"
    _write_immutable_absolute_file(destination, payload)
    return destination


def load_verified_benchmark_execution_anchor(
    project_root: Path,
    path: Path,
    *,
    expected_runtime_identity_digest: str | None = None,
) -> BenchmarkExecutionAnchor:
    """Safely load strict JSON and verify it against the current execution state."""

    root = _validated_project_root(project_root)
    destination = _absolute_lexical_path(path, base=root)
    raw = _read_absolute_regular_file(destination)
    payload = _decode_strict_json(raw, destination)
    try:
        anchor = BenchmarkExecutionAnchor.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("benchmark execution anchor is malformed or tampered") from exc
    try:
        destination.relative_to(root)
    except ValueError:
        pass
    else:
        _reject_inventory_destination(root, destination, anchor.runtime_inventory)
    return verify_benchmark_execution_anchor(
        root,
        anchor,
        expected_runtime_identity_digest=expected_runtime_identity_digest,
    )


def load_strict_benchmark_json(path: Path) -> Any:
    """Load one bounded benchmark-side JSON artifact without following symlinks."""

    destination = _absolute_lexical_path(path)
    return _decode_strict_json(
        _read_absolute_regular_file(destination),
        destination,
    )


def _capture_execution_state(
    root: Path,
    *,
    frozen_git_provenance: tuple[str, str] | None = None,
) -> _CapturedExecutionState:
    """Capture twice and fail if creation Git or any relevant byte changes.

    Anchor creation reads current Git twice so a moving HEAD cannot produce a
    mixed anchor.  Later verification supplies the anchor's already-validated
    creation-time provenance because current HEAD is not runtime authority.
    """

    first_git = frozen_git_provenance or _git_identity(root)
    first_protocol = load_verified_benchmark_protocol(root)
    first = _capture_once(root, first_protocol, first_git)

    second_git = frozen_git_provenance or _git_identity(root)
    second_protocol = load_verified_benchmark_protocol(root)
    second = _capture_once(root, second_protocol, second_git)
    if first != second:
        raise PolicyViolation("benchmark execution inputs changed while the anchor was captured")
    return second


def _capture_once(
    root: Path,
    protocol: VerifiedBenchmarkProtocol,
    git_identity: tuple[str, str],
) -> _CapturedExecutionState:
    path_kinds, supporting_paths = _runtime_path_kinds(protocol)
    inventory = tuple(_inventory_entry(root, path, path_kinds[path]) for path in sorted(path_kinds))
    entries_by_path = {entry.path: entry for entry in inventory}
    supporting_entries = tuple(entries_by_path[path] for path in supporting_paths)
    lockfiles = tuple(
        LockfileDigest(path=path, digest=entries_by_path[path].revision)
        for path in sorted(_LOCKFILE_PATHS)
    )
    agent_definition_digests = {
        configuration.agent_definitions_digest for configuration in protocol.registry.configurations
    }
    if len(agent_definition_digests) != 1:
        raise PolicyViolation("benchmark configurations disagree on agent definitions")
    return _CapturedExecutionState(
        protocol_declaration_digest=protocol.declaration_digest,
        benchmark_registry_digest=protocol.registry_digest,
        supporting_artifact_paths=supporting_paths,
        supporting_artifact_graph_digest=_supporting_graph_digest(supporting_entries),
        git_head_commit_oid=git_identity[0],
        git_head_tree_oid=git_identity[1],
        runtime_inventory=inventory,
        runtime_content_revision=_inventory_revision(inventory),
        wiki_tree_revision=protocol.wiki_tree_revision,
        agent_definitions_digest=next(iter(agent_definition_digests)),
        lockfile_digests=lockfiles,
    )


def _runtime_path_kinds(
    protocol: VerifiedBenchmarkProtocol,
) -> tuple[dict[str, Literal["file", "tree"]], tuple[str, ...]]:
    path_kinds = {**_BASE_RUNTIME_PATHS, **_RUNTIME_TOOLING_PATHS}
    declaration = protocol.declaration
    protocol_files = {
        BENCHMARK_DECLARATION_PATH,
        BENCHMARK_REGISTRY_PATH,
        declaration.source_snapshots_path,
        declaration.runtime_bindings_path,
        declaration.dependency_labels_path,
        declaration.risk_seed_path,
    }
    declared_trees: set[str] = set()
    for source in protocol.source_snapshots.cases:
        declared_trees.add(source.source_root)
        protocol_files.add(source.fixture_contract_path)
        protocol_files.add(source.source_edge_labels_path)
    for item in protocol.dependency_labels.cases:
        protocol_files.add(item.source_edge_labels_path)
    if protocol.dependency_labels.review_evidence_path is not None:
        protocol_files.add(protocol.dependency_labels.review_evidence_path)
    for condition in protocol.runtime_bindings.environment_conditions:
        protocol_files.add(condition.evidence_path)

    for path in sorted(protocol_files):
        _add_path_kind(path_kinds, path, "file")
    for path in sorted(declared_trees):
        _add_path_kind(path_kinds, path, "tree")

    supporting_paths = tuple(
        sorted(
            {
                "src/legacy_migration_agent",
                "agents",
                "knowledge/wiki",
                *protocol_files,
                *declared_trees,
                *_RUNTIME_TOOLING_PATHS,
            }
        )
    )
    return path_kinds, supporting_paths


def _add_path_kind(
    values: dict[str, Literal["file", "tree"]],
    path: str,
    kind: Literal["file", "tree"],
) -> None:
    normalized = validate_relative_path(path)
    existing = values.get(normalized)
    if existing is not None and existing != kind:
        raise PolicyViolation("runtime influence path is declared as both file and tree")
    values[normalized] = kind


def _inventory_entry(
    root: Path,
    relative_path: str,
    kind: Literal["file", "tree"],
) -> RuntimeInfluenceEntry:
    _validate_relative_components(root, relative_path, expected_kind=kind)
    if relative_path == "src/legacy_migration_agent":
        revision = _source_tree_revision(root.joinpath(*relative_path.split("/")))
    elif kind == "tree":
        revision = content_revision(root.joinpath(*relative_path.split("/")))
    else:
        revision = _bytes_digest(_read_relative_regular_file(root, relative_path))
    return RuntimeInfluenceEntry(path=relative_path, kind=kind, revision=revision)


def _source_tree_revision(source_root: Path) -> Sha256Digest:
    """Hash source bytes while excluding derived Python bytecode caches.

    ``__pycache__`` and ``*.py[co]`` are interpreter outputs rather than source
    authority.  Including them would let an otherwise read-only import drift the
    anchor between cells.  Symlinks at scanned levels are still rejected before
    any exclusion is applied.
    """

    files: list[tuple[str, bytes]] = []

    def scan(directory: Path, prefix: str) -> None:
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda child: child.name)
        except OSError as exc:
            raise PolicyViolation("benchmark source tree could not be scanned safely") from exc
        for child in children:
            relative = f"{prefix}/{child.name}" if prefix else child.name
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise PolicyViolation("benchmark source tree entry could not be inspected") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise PolicyViolation(f"runtime influence path contains a symlink: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                if child.name != "__pycache__":
                    scan(Path(child.path), relative)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise PolicyViolation(f"runtime influence path is not a regular file: {relative}")
            if child.name.endswith((".pyc", ".pyo")):
                continue
            files.append((relative, _read_absolute_regular_file(Path(child.path))))

    scan(source_root, "")
    digest = hashlib.sha256()
    digest.update(b"legacy-migration-benchmark-source-tree-v1\0")
    for relative, payload in files:
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return f"sha256:{digest.hexdigest()}"


def _inventory_revision(entries: tuple[RuntimeInfluenceEntry, ...]) -> Sha256Digest:
    return artifact_digest(
        {
            "schema_version": "benchmark-runtime-inventory/v1",
            "entries": [entry.model_dump(mode="json") for entry in entries],
        }
    )


def _supporting_graph_digest(entries: tuple[RuntimeInfluenceEntry, ...]) -> Sha256Digest:
    return artifact_digest(
        {
            "schema_version": "benchmark-supporting-artifact-graph/v1",
            "artifacts": [entry.model_dump(mode="json") for entry in entries],
        }
    )


def _validated_project_root(project_root: Path) -> Path:
    root = Path(project_root)
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise PolicyViolation("benchmark project root does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation("benchmark project root must be a non-symlink directory")
    return root.resolve(strict=True)


def _validate_relative_components(
    root: Path,
    relative_path: str,
    *,
    expected_kind: Literal["file", "tree"],
) -> None:
    normalized = validate_relative_path(relative_path)
    current = root
    parts = normalized.split("/")
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise PolicyViolation(f"runtime influence path is missing: {normalized}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PolicyViolation(f"runtime influence path contains a symlink: {normalized}")
        is_leaf = index == len(parts) - 1
        if not is_leaf and not stat.S_ISDIR(metadata.st_mode):
            raise PolicyViolation(f"runtime influence path contains a non-directory: {normalized}")
    if expected_kind == "file" and not stat.S_ISREG(metadata.st_mode):
        raise PolicyViolation(f"runtime influence path is not a regular file: {normalized}")
    if expected_kind == "tree" and not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation(f"runtime influence path is not a directory: {normalized}")


def _git_identity(root: Path) -> tuple[str, str]:
    commit = _git_rev_parse(root, "HEAD^{commit}")
    tree = _git_rev_parse(root, "HEAD^{tree}")
    return commit, tree


def _git_rev_parse(root: Path, expression: str) -> str:
    try:
        completed = subprocess.run(
            ("git", "--no-optional-locks", "-C", str(root), "rev-parse", "--verify", expression),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PolicyViolation("benchmark Git identity could not be read") from exc
    value = completed.stdout.strip()
    if completed.returncode != 0 or not _is_git_object_id(value):
        raise PolicyViolation("benchmark project must have a valid Git HEAD")
    return value


def _is_git_object_id(value: str) -> bool:
    return len(value) in {40, 64} and all(character in "0123456789abcdef" for character in value)


def _verify_original_git_provenance(root: Path, commit_oid: str, tree_oid: str) -> None:
    """Fail closed unless both original objects exist and retain their relation."""

    resolved_commit = _resolve_original_git_object(root, commit_oid, "commit")
    resolved_tree = _resolve_original_git_object(root, tree_oid, "tree")
    commit_tree = _resolve_original_git_object(root, commit_oid, "tree")
    if resolved_commit != commit_oid or resolved_tree != tree_oid or commit_tree != tree_oid:
        raise PolicyViolation(
            "benchmark execution anchor Git provenance is inconsistent: "
            "original commit does not reference original tree"
        )


def _resolve_original_git_object(
    root: Path, oid: str, object_type: Literal["commit", "tree"]
) -> str:
    """Resolve a typed, stored Git object without consulting current HEAD."""

    try:
        completed = subprocess.run(
            (
                "git",
                "--no-optional-locks",
                "-C",
                str(root),
                "rev-parse",
                "--verify",
                f"{oid}^{{{object_type}}}",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PolicyViolation(
            f"benchmark execution anchor original Git {object_type} object could not be verified"
        ) from exc
    resolved = completed.stdout.strip()
    if completed.returncode != 0 or not _is_git_object_id(resolved):
        raise PolicyViolation(
            f"benchmark execution anchor original Git {object_type} object "
            "is unavailable or corrupt"
        )
    return resolved


def _verify_expected_runtime_identity(
    anchor: BenchmarkExecutionAnchor,
    expected_runtime_identity_digest: str | None,
) -> None:
    if expected_runtime_identity_digest is None:
        return
    try:
        expected = _EXPECTED_RUNTIME_IDENTITY.validate_python(expected_runtime_identity_digest)
    except ValueError as exc:
        raise PolicyViolation("expected Claude runtime identity digest is invalid") from exc
    if anchor.runtime_identity_digest != expected:
        raise PolicyViolation("benchmark execution anchor Claude runtime identity differs")


def _reject_inventory_destination(
    project_root: Path,
    destination: Path,
    inventory: tuple[RuntimeInfluenceEntry, ...],
) -> None:
    try:
        relative = destination.relative_to(project_root).as_posix()
    except ValueError:
        return
    for entry in inventory:
        if (
            relative == entry.path
            or relative.startswith(f"{entry.path}/")
            or entry.path.startswith(f"{relative}/")
        ):
            raise PolicyViolation(
                "benchmark results and anchor paths must be outside the runtime inventory"
            )


def _discover_containing_git_root(destination: Path) -> Path | None:
    ancestor = destination.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        return None
    try:
        completed = subprocess.run(
            (
                "git",
                "--no-optional-locks",
                "-C",
                str(ancestor),
                "rev-parse",
                "--show-toplevel",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    if not value:
        return None
    root = Path(value)
    try:
        metadata = root.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation("containing Git root is not a safe directory")
    return root.resolve(strict=True)


def _read_relative_regular_file(root: Path, relative_path: str) -> bytes:
    normalized = validate_relative_path(relative_path)
    parts = normalized.split("/")
    with _open_relative_parent(root, parts[:-1], create=False) as parent_fd:
        return _read_regular_file_at(parent_fd, parts[-1], normalized)


def _read_absolute_regular_file(path: Path) -> bytes:
    root, parts = _absolute_root_and_parts(path)
    with _open_relative_parent(root, parts[:-1], create=False) as parent_fd:
        return _read_regular_file_at(parent_fd, parts[-1], str(path))


def _read_regular_file_at(parent_fd: int, leaf: str, display_path: str) -> bytes:
    try:
        descriptor = os.open(leaf, os.O_RDONLY | _no_follow_flag(), dir_fd=parent_fd)
    except OSError as exc:
        raise PolicyViolation(f"artifact is not a safe regular file: {display_path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PolicyViolation(f"artifact is not a regular file: {display_path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_immutable_absolute_file(path: Path, payload: bytes) -> None:
    root, parts = _absolute_root_and_parts(path)
    with _open_relative_parent(root, parts[:-1], create=True) as parent_fd:
        leaf = parts[-1]
        try:
            existing = _read_regular_file_at(parent_fd, leaf, str(path))
        except PolicyViolation:
            try:
                os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            else:
                raise
        if existing is not None:
            if existing == payload:
                return
            raise PolicyViolation("immutable benchmark execution anchor already exists")

        temporary = f".{leaf}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
            0o600,
            dir_fd=parent_fd,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(
                    temporary,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                concurrent = _read_regular_file_at(parent_fd, leaf, str(path))
                if concurrent != payload:
                    raise PolicyViolation(
                        "immutable benchmark execution anchor was concurrently replaced"
                    ) from None
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


@contextmanager
def _open_relative_parent(
    root: Path,
    parts: list[str],
    *,
    create: bool,
) -> Iterator[int]:
    flags = os.O_RDONLY | _directory_flag() | _no_follow_flag()
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, flags))
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=descriptors[-1])
            except FileNotFoundError:
                if not create:
                    raise PolicyViolation("artifact parent directory does not exist") from None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
                except FileExistsError:
                    pass
                try:
                    child = os.open(part, flags, dir_fd=descriptors[-1])
                except OSError as exc:
                    raise PolicyViolation(
                        "artifact parent is a symlink or unsafe directory"
                    ) from exc
            except OSError as exc:
                raise PolicyViolation("artifact parent is a symlink or unsafe directory") from exc
            descriptors.append(child)
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _absolute_lexical_path(path: Path, *, base: Path | None = None) -> Path:
    value = Path(path)
    if not value.is_absolute():
        value = (base if base is not None else Path.cwd()) / value
    return Path(os.path.abspath(value))


def _absolute_root_and_parts(path: Path) -> tuple[Path, list[str]]:
    if not path.is_absolute() or not path.name:
        raise PolicyViolation("artifact path must identify an absolute file")
    root = Path(path.anchor)
    parts = list(path.parts[1:])
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise PolicyViolation("artifact path is invalid")
    return root, parts


def _decode_strict_json(raw: bytes, path: Path) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise PolicyViolation(
                    f"benchmark execution anchor contains duplicate JSON key {key!r}"
                )
            value[key] = item
        return value

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except PolicyViolation:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation(f"benchmark execution anchor is not valid JSON: {path}") from exc


def _bytes_digest(value: bytes) -> Sha256Digest:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _no_follow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as exc:  # pragma: no cover - POSIX target contract
        raise RuntimeError("benchmark anchor storage requires O_NOFOLLOW") from exc


def _directory_flag() -> int:
    try:
        return os.O_DIRECTORY
    except AttributeError as exc:  # pragma: no cover - POSIX target contract
        raise RuntimeError("benchmark anchor storage requires O_DIRECTORY") from exc


__all__ = [
    "BenchmarkExecutionAnchor",
    "LockfileDigest",
    "RuntimeInfluenceEntry",
    "build_benchmark_execution_anchor",
    "load_strict_benchmark_json",
    "load_verified_benchmark_execution_anchor",
    "verify_benchmark_execution_anchor",
    "write_benchmark_execution_anchor",
]
