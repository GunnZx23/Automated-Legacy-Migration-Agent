"""Immutable dependency-graph snapshots with explicit cache invalidation.

One snapshot key binds the platform, captured source revision, analyzer
version, and canonical entry-path set.  A change to any of those inputs maps to
a different immutable artifact, so callers cannot accidentally reuse stale
analysis after source, scope, or parser changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.contracts import (
    Platform,
    Sha256Digest,
    StrictModel,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.graphs.graph_contracts import DependencyGraph

GRAPH_SNAPSHOT_SCHEMA_VERSION: Literal["1.0"] = "1.0"


class GraphSnapshotKey(StrictModel):
    """Complete identity of one reusable dependency-analysis result."""

    platform: Platform
    source_revision: Sha256Digest
    entry_paths: tuple[str, ...] = Field(min_length=1)
    analyzer_version: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$",
    )

    @field_validator("entry_paths")
    @classmethod
    def canonicalize_entry_set(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = {validate_relative_path(value) for value in values}
        return tuple(sorted(normalized))

    @property
    def cache_digest(self) -> Sha256Digest:
        """Return the deterministic storage identity for this exact key."""

        return artifact_digest(self)

    @classmethod
    def for_graph(
        cls,
        graph: DependencyGraph,
        *,
        analyzer_version: str,
    ) -> GraphSnapshotKey:
        """Derive a cache key from a revision-bound graph."""

        return cls(
            platform=graph.platform,
            source_revision=graph.base_revision,
            entry_paths=graph.entry_paths,
            analyzer_version=analyzer_version,
        )


class StoredGraphSnapshot(StrictModel):
    """Digest-protected graph plus the complete key used to produce it."""

    schema_version: Literal["1.0"] = GRAPH_SNAPSHOT_SCHEMA_VERSION
    key: GraphSnapshotKey
    graph_digest: Sha256Digest
    graph: DependencyGraph

    @model_validator(mode="after")
    def validate_bindings(self) -> StoredGraphSnapshot:
        if self.graph.platform is not self.key.platform:
            raise ValueError("stored graph platform does not match its snapshot key")
        if self.graph.base_revision != self.key.source_revision:
            raise ValueError("stored graph revision does not match its snapshot key")
        graph_entries = tuple(sorted(set(self.graph.entry_paths)))
        if graph_entries != self.key.entry_paths:
            raise ValueError("stored graph entry set does not match its snapshot key")
        if self.graph_digest != artifact_digest(self.graph):
            raise ValueError("stored graph digest does not match its content")
        return self


class GraphSnapshotStore:
    """Persist and retrieve immutable, fully keyed dependency graphs."""

    def __init__(self, root: Path) -> None:
        self._artifacts = ArtifactStore(root)
        self.root = self._artifacts.root

    def save(
        self,
        graph: DependencyGraph,
        *,
        analyzer_version: str,
    ) -> GraphSnapshotKey:
        """Save one graph idempotently and reject key reuse with new content."""

        key = GraphSnapshotKey.for_graph(graph, analyzer_version=analyzer_version)
        snapshot = StoredGraphSnapshot(
            key=key,
            graph_digest=artifact_digest(graph),
            graph=graph,
        )
        self._artifacts.write_json(self.relative_path(key), snapshot)
        return key

    def load(self, key: GraphSnapshotKey) -> DependencyGraph | None:
        """Load the exact keyed graph, returning ``None`` only for a cache miss."""

        try:
            payload = self._artifacts.read_json(self.relative_path(key))
        except FileNotFoundError:
            return None
        snapshot = StoredGraphSnapshot.model_validate(payload)
        if snapshot.key != key:
            raise ValueError("stored graph snapshot was found under the wrong cache key")
        return snapshot.graph

    @staticmethod
    def relative_path(key: GraphSnapshotKey) -> str:
        """Return the deterministic artifact path for a snapshot key."""

        digest = key.cache_digest.removeprefix("sha256:")
        return f"snapshots/v1/{key.platform.value}/{digest}.json"


__all__ = [
    "GRAPH_SNAPSHOT_SCHEMA_VERSION",
    "GraphSnapshotKey",
    "GraphSnapshotStore",
    "StoredGraphSnapshot",
]
