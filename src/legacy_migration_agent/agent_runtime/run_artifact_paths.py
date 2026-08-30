"""Canonical relative paths for one persisted model-backed agent run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ModelArtifactRole = Literal["engineer", "validator"]


@dataclass(frozen=True, slots=True)
class RunArtifactPaths:
    """Build the immutable ``ArtifactStore`` paths owned by one request."""

    request_id: str

    @property
    def root(self) -> str:
        return f"model-runs/{self.request_id}"

    def _path(self, filename: str) -> str:
        return f"{self.root}/{filename}"

    @property
    def architect(self) -> str:
        return self._path("architect.json")

    @property
    def architect_policy_rejection(self) -> str:
        return self._path("architect-policy-rejection.json")

    @property
    def architect_preflight(self) -> str:
        return self._path("architect-preflight.json")

    @property
    def architect_context(self) -> str:
        return self._path("architect-context.json")

    @property
    def dependency_graph(self) -> str:
        return self._path("dependency-graph.json")

    @property
    def graph_assurance_report(self) -> str:
        return self._path("graph-assurance-report.json")

    @property
    def wiki_trace(self) -> str:
        return self._path("wiki-trace.json")

    @property
    def engineer_correction(self) -> str:
        return self._path("engineer-correction-attempt-2.json")

    @property
    def correction_wiki(self) -> str:
        return self._path("correction-wiki-attempt-2.json")

    def role_outcome(self, role: ModelArtifactRole, attempt: int) -> str:
        return self._path(f"{role}-attempt-{attempt}.json")

    def role_invocation_lease(self, role: ModelArtifactRole, attempt: int) -> str:
        return self._path(f"{role}-invocation-lease-attempt-{attempt}.json")

    def engineer(self, attempt: int) -> str:
        return self.role_outcome("engineer", attempt)

    def engineer_invocation_lease(self, attempt: int) -> str:
        return self.role_invocation_lease("engineer", attempt)

    def report(self, attempt: int) -> str:
        return self._path(f"report-attempt-{attempt}.json")

    def validator(self, attempt: int) -> str:
        return self.role_outcome("validator", attempt)

    def validator_invocation_lease(self, attempt: int) -> str:
        return self.role_invocation_lease("validator", attempt)
