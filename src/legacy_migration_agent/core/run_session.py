"""Oracle-isolated local session storage for model-backed migration runs.

The session owns filesystem routing and evidence binding only.  It deliberately
has no fixture, template, golden-output, or oracle lookup.  Portable evidence
contains repository-relative paths and content digests; absolute routing and
mutable runtime state remain in memory or beneath the excluded ``state``,
``workspaces``, and ``scratch`` directories.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.contracts import (
    Identifier,
    Sha256Digest,
    StrictModel,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import (
    ArtifactStore,
    artifact_digest,
    canonical_json_bytes,
)
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.redaction import (
    REDACTED,
    SecretRedactor,
    high_confidence_secret_findings,
)
from legacy_migration_agent.core.workspace import TreeSnapshot, snapshot_tree

RUN_SESSION_SCHEMA_VERSION: Literal["1.0"] = "1.0"
RUNTIME_STATE_PATHS = (
    "scratch/",
    "state/",
    "state/checkpoints.sqlite3",
    "workspaces/",
)
_ORACLE_SEGMENTS = frozenset({"expected", "golden", "oracle"})
_API_KEY_TOKEN = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_ABSOLUTE_ANYWHERE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>]*")
_WINDOWS_UNC_ANYWHERE = re.compile(
    r"(?<![A-Za-z0-9._~@%+\\/\-])\\\\"
    r"[^\\/\s\"'<>]+[\\/][^\s\"'<>]+"
)
_MARKDOWN_WINDOWS_DRIVE_ABSOLUTE_ANYWHERE = re.compile(
    r"(?<![A-Za-z0-9])`[A-Za-z]:`[\\/]`",
    re.IGNORECASE,
)
_MARKDOWN_WINDOWS_UNC_ABSOLUTE_ANYWHERE = re.compile(
    r"(?<![A-Za-z0-9])`\\\\[^\\`\s/]+`[\\/]`",
    re.IGNORECASE,
)
_QUOTED_FORWARD_UNC_ANYWHERE = re.compile(r"(?P<quote>[\"'])//[^/\\\s\"'<>]+/[^\s\"'<>]+")
_WINDOWS_ROOT_RELATIVE_ANYWHERE = re.compile(
    r"(?<![A-Za-z0-9._~@%+\\/\-])\\(?!\\)"
    r"(?:\$Recycle\.Bin|Documents and Settings|PerfLogs|ProgramData|"
    r"Program Files(?: \(x86\))?|Recovery|Temp|Users|Windows)"
    r"(?=[\\/]|$|[\s\"'<>;,\)\]}`])",
    re.IGNORECASE,
)
_STRUCTURED_POSIX_ABSOLUTE_ANYWHERE = re.compile(r"(?<![A-Za-z0-9._~@%+\-/<])/(?:[^\s\"'<>`]+)")
_OPAQUE_POSIX_ABSOLUTE_ANYWHERE = re.compile(r"(?<![A-Za-z0-9._~@%+*\-/<])/(?:[^\s\"'<>`]+)")
_NON_FILE_URI = re.compile(
    r"\b(?!file:)[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+",
    re.IGNORECASE,
)
_FILE_URI = re.compile(r"\bfile:/", re.IGNORECASE)
_QUOTED_URI_ROOT = re.compile(r"(?P<quote>[\"'])/(?P=quote)")
_ASSIGNED_URI_ROOT = re.compile(r"[=:]\s*/(?=$|[\s,;)])")
_MARKDOWN_INLINE_CODE_ATOM = r"`[A-Za-z@][A-Za-z0-9_@ .(),=\-]{0,159}`"
_MARKDOWN_INLINE_CODE_ALTERNATIVES = re.compile(
    rf"{_MARKDOWN_INLINE_CODE_ATOM}(?:/{_MARKDOWN_INLINE_CODE_ATOM})+"
)
_LOCAL_FILESYSTEM_ROOT_NAME = (
    r"(?:(?:"
    r"Applications|Library|System|Users|Volumes|bin|dev|etc|mnt|nix|opt|private|"
    r"proc|root|sbin|secrets|snap|srv|sys|tmp|usr|var|workspace"
    r")(?=/|$|[\s\"'<>;,\.\)\]}:!?`*_])|home(?=/))"
)
_LOCAL_FILESYSTEM_ROOT = re.compile(
    rf"(?<![A-Za-z0-9._~@%+:\-/<])/{_LOCAL_FILESYSTEM_ROOT_NAME}",
    re.IGNORECASE,
)
_MARKDOWN_UNDERSCORE_LOCAL_FILESYSTEM_ROOT = re.compile(
    rf"(?<![A-Za-z0-9._~@%+\-/<])_+/{_LOCAL_FILESYSTEM_ROOT_NAME}",
    re.IGNORECASE,
)
_SOURCE_BEARING_TEXT_FIELDS = frozenset({"content", "selected_content", "unified_diff"})
_OPAQUE_TEXT_FIELDS = frozenset(
    {
        "assumptions",
        "canonical_description",
        "concerns",
        "content",
        "description",
        "implementation_contract",
        "public_concerns",
        "reason",
        "recommendation",
        "required_implementation_contract",
        "selected_content",
        "summary",
        "unified_diff",
        "unresolved_questions",
    }
)
_PORTABLE_ROUTE_SEGMENT = re.compile(r"^[A-Za-z0-9_{}@:+,=.~-]+$")
_VISUALFORCE_PAGE_ROUTE_SEGMENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_PORTABLE_ROUTE_ROOTS = (
    "/api",
    "/force-app",
    "/lightning",
    "/resource",
    "/services/data",
)
_PORTABLE_EXACT_ROUTES = frozenset({"/customers/{customerId}/status"})
_TRAILING_ROUTE_PUNCTUATION = ".,;:)]!?"
_SECRET_KEYS = frozenset(
    {
        "access_token",
        "apikey",
        "api_key",
        "auth_token",
        "authorization",
        "bearer_token",
        "client_secret",
        "credential",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "sfdx_auth_url",
    }
)
_RUNTIME_EVIDENCE_ROOTS = frozenset({"scratch", "state", "workspaces"})
_RUNTIME_MODEL_OPERATION_FILE = re.compile(
    r"^(?:"
    r"engineer-invocation-lease-attempt-[12]|"
    r"engineer-attempt-[12]|"
    r"engineer-correction-attempt-2|"
    r"correction-wiki-attempt-2|"
    r"report-attempt-[12]|"
    r"validator-invocation-lease-attempt-[12]|"
    r"validator-attempt-[12]"
    r")\.json$"
)

PortableEvidenceCategory = Literal[
    "absolute_project_or_source_path",
    "local_absolute_path",
]
PortableEvidenceFieldClass = Literal["narrative", "source_bearing", "structured"]


class PortableEvidencePolicyViolation(PolicyViolation):
    """Portable-evidence rejection with content-free diagnostic metadata."""

    def __init__(
        self,
        message: str,
        *,
        evidence_category: PortableEvidenceCategory,
        field_class: PortableEvidenceFieldClass,
    ) -> None:
        self.evidence_category = evidence_category
        self.field_class = field_class
        super().__init__(message)


_RUNTIME_MODEL_CONTROL_FILE = re.compile(r"^correction-request-attempt-[12]\.json$")
_RUNTIME_MODEL_OPERATION_ROOT = "inflight-model-runs"
_ROLE_INVOCATION_LEASE_FILE = re.compile(
    r"^(?:engineer|validator)-invocation-lease-attempt-[12]\.json$"
)


class AgentDefinitionDigests(StrictModel):
    """Exact prompt-definition identities for the only three runtime agents."""

    architect: Sha256Digest
    engineer: Sha256Digest
    validator: Sha256Digest


class AgentRunContext(StrictModel):
    """Immutable, portable identity and replay boundary for one agent run."""

    schema_version: Literal["1.0"] = RUN_SESSION_SCHEMA_VERSION
    run_id: Identifier
    thread_id: Identifier
    slice_id: Identifier
    run_directory: str
    source_root: str
    source_revision: Sha256Digest
    request_digest: Sha256Digest
    agent_definition_digests: AgentDefinitionDigests
    provider_id: str = Field(min_length=1, max_length=160)
    model_id: str = Field(min_length=1, max_length=300)
    portable_evidence_root: Literal["evidence"] = "evidence"
    excluded_runtime_state: tuple[str, ...] = RUNTIME_STATE_PATHS

    @field_validator("run_directory", "source_root")
    @classmethod
    def validate_repository_relative_directory(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("source_root")
    @classmethod
    def prohibit_oracle_source_segments(cls, value: str) -> str:
        _reject_oracle_segments(value, role="source root")
        return value

    @field_validator("provider_id", "model_id")
    @classmethod
    def require_redacted_public_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("provider and model IDs cannot contain control characters")
        if _looks_absolute(normalized):
            raise ValueError("provider and model IDs cannot contain absolute paths")
        if _sanitize_public_identifier(normalized) != normalized:
            raise ValueError("provider and model IDs must be redacted before persistence")
        return normalized

    @model_validator(mode="after")
    def require_runtime_exclusion_contract(self) -> AgentRunContext:
        if self.excluded_runtime_state != RUNTIME_STATE_PATHS:
            raise ValueError("runtime-state exclusions cannot be changed")
        if self.run_id == self.thread_id:
            raise ValueError("run_id and thread_id must be distinct stable identifiers")
        return self


class _RuntimeBinding(StrictModel):
    schema_version: Literal["1.0"] = RUN_SESSION_SCHEMA_VERSION
    context_digest: Sha256Digest


class _RuntimeEvidenceAnchor(StrictModel):
    """Non-portable immutable digest anchor for rewritable evidence chains."""

    schema_version: Literal["1.0"] = RUN_SESSION_SCHEMA_VERSION
    anchor_kind: Identifier
    context_digest: Sha256Digest
    payload_digest: Sha256Digest


class _LifecycleArtifact(StrictModel):
    path: str
    canonical_sha256: Sha256Digest

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class _LifecycleIndex(StrictModel):
    schema_version: Literal["1.0"] = RUN_SESSION_SCHEMA_VERSION
    index_kind: Identifier
    slice_id: Identifier
    context_digest: Sha256Digest
    artifacts: tuple[_LifecycleArtifact, ...]
    excluded_runtime_state: tuple[str, ...] = RUNTIME_STATE_PATHS

    @model_validator(mode="after")
    def validate_inventory(self) -> _LifecycleIndex:
        paths = tuple(artifact.path for artifact in self.artifacts)
        if paths != tuple(sorted(paths)):
            raise ValueError("lifecycle artifact paths must be sorted")
        if len(paths) != len(set(paths)):
            raise ValueError("lifecycle artifact paths must be unique")
        if self.excluded_runtime_state != RUNTIME_STATE_PATHS:
            raise ValueError("runtime-state exclusions cannot be changed")
        return self


class _PortableArtifactStore(ArtifactStore):
    """Artifact store that enforces the portable-evidence boundary."""

    def __init__(self, root: Path, *, forbidden_absolute_paths: tuple[Path, ...]):
        super().__init__(root)
        self._forbidden_absolute_paths = tuple(str(path) for path in forbidden_absolute_paths)

    def write_json(self, relative_path: str, value: Any) -> Path:
        normalized = validate_relative_path(relative_path)
        if normalized.split("/", 1)[0].casefold() in _RUNTIME_EVIDENCE_ROOTS:
            raise PolicyViolation("runtime state cannot be written as portable evidence")
        self._assert_portable(value)
        return super().write_json(normalized, value)

    def read_json(self, relative_path: str) -> Any:
        value = super().read_json(relative_path)
        self._assert_portable(value)
        return value

    def _assert_portable(self, value: Any) -> None:
        normalized = json.loads(canonical_json_bytes(value).decode("utf-8"))
        _assert_portable_evidence_value(normalized, self._forbidden_absolute_paths)


class _WorkflowArtifactStore(ArtifactStore):
    """Route only in-flight model artifacts outside portable evidence.

    Attempt-scoped Engineer/Validator artifacts are written after the latest
    portable lifecycle has frozen its exact artifact inventory.  Keeping this
    bounded in-flight transaction beneath ``state/`` prevents a provider claim,
    role result, or deterministic report from silently widening that inventory.
    At the next controller-owned terminal boundary the immutable bytes are
    promoted into portable evidence and covered by its lifecycle index.
    """

    def __init__(
        self,
        portable_store: _PortableArtifactStore,
        *,
        runtime_state_root: Path,
    ):
        super().__init__(portable_store.root)
        self._portable_store = portable_store
        self._runtime_store = ArtifactStore(runtime_state_root)
        self._route_inflight_model_artifacts = False

    def enable_inflight_model_routing(self) -> None:
        """Irreversibly enable runtime routing for the bound model workflow."""

        self._route_inflight_model_artifacts = True

    def write_json(self, relative_path: str, value: Any) -> Path:
        normalized = validate_relative_path(relative_path)
        self._assert_portable(value)
        if self._route_inflight_model_artifacts and _is_runtime_model_operation_path(normalized):
            return self._runtime_store.write_json(
                f"{_RUNTIME_MODEL_OPERATION_ROOT}/{normalized}",
                value,
            )
        return self._portable_store.write_json(normalized, value)

    def read_json(self, relative_path: str) -> Any:
        normalized = validate_relative_path(relative_path)
        if self._route_inflight_model_artifacts and _is_runtime_model_operation_path(normalized):
            runtime_value: Any | None = None
            portable_value: Any | None = None
            try:
                runtime_value = self._runtime_store.read_json(
                    f"{_RUNTIME_MODEL_OPERATION_ROOT}/{normalized}"
                )
            except FileNotFoundError:
                pass
            try:
                portable_value = self._portable_store.read_json(normalized)
            except FileNotFoundError:
                pass
            if runtime_value is None and portable_value is None:
                raise FileNotFoundError(normalized)
            if (
                runtime_value is not None
                and portable_value is not None
                and artifact_digest(runtime_value) != artifact_digest(portable_value)
            ):
                raise PolicyViolation(
                    "runtime model-operation artifact differs from promoted portable evidence"
                )
            value = runtime_value if runtime_value is not None else portable_value
        else:
            value = self._portable_store.read_json(normalized)
        self._assert_portable(value)
        return value

    def promote_model_operation_artifacts(self) -> tuple[str, ...]:
        """Copy an in-flight transaction into the next exact lifecycle.

        Runtime copies remain immutable so an interrupted call can be
        recognized before another dispatch.  The identical promoted bytes let
        the lifecycle index permanently cover every completed operation input
        and output.
        """

        promoted: list[str] = []
        for path in self.model_operation_artifact_paths():
            value = self._runtime_store.read_json(f"{_RUNTIME_MODEL_OPERATION_ROOT}/{path}")
            self._assert_portable(value)
            self._portable_store.write_json(path, value)
            promoted.append(path)
        return tuple(promoted)

    def model_operation_artifact_paths(self) -> tuple[str, ...]:
        """Return the exact safe inventory of the in-flight transaction."""

        operation_root = self._runtime_store.root / _RUNTIME_MODEL_OPERATION_ROOT
        try:
            metadata = operation_root.lstat()
        except FileNotFoundError:
            return ()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PolicyViolation("runtime model-operation root is unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PolicyViolation("runtime model-operation root must use mode 0700")

        paths = _portable_json_paths(operation_root)
        for path in paths:
            if not _is_runtime_model_operation_path(path):
                raise PolicyViolation("runtime model-operation inventory is invalid")
        return paths

    def role_invocation_lease_paths(self) -> tuple[str, ...]:
        """Return only durable provider tombstones from the in-flight inventory."""

        return tuple(
            path
            for path in self.model_operation_artifact_paths()
            if _is_role_invocation_lease_path(path)
        )

    def is_model_operation_artifact_promoted(self, relative_path: str) -> bool:
        """Return whether one runtime artifact has an identical portable copy."""

        normalized = validate_relative_path(relative_path)
        if not _is_runtime_model_operation_path(normalized):
            raise PolicyViolation("path is not a runtime model-operation artifact")
        try:
            portable = self._portable_store.read_json(normalized)
        except FileNotFoundError:
            return False
        runtime = self._runtime_store.read_json(f"{_RUNTIME_MODEL_OPERATION_ROOT}/{normalized}")
        if artifact_digest(runtime) != artifact_digest(portable):
            raise PolicyViolation(
                "runtime model-operation artifact differs from promoted portable evidence"
            )
        return True

    def _assert_portable(self, value: Any) -> None:
        self._portable_store._assert_portable(value)


def _is_runtime_model_operation_path(relative_path: str) -> bool:
    """Recognize only bounded attempt-scoped workflow artifacts."""

    parts = relative_path.split("/")
    model_artifact = (
        len(parts) == 3
        and parts[0] == "model-runs"
        and _IDENTIFIER.fullmatch(parts[1]) is not None
        and _RUNTIME_MODEL_OPERATION_FILE.fullmatch(parts[2]) is not None
    )
    control_artifact = (
        len(parts) == 2
        and parts[0] == "control"
        and _RUNTIME_MODEL_CONTROL_FILE.fullmatch(parts[1]) is not None
    )
    return model_artifact or control_artifact


def _is_role_invocation_lease_path(relative_path: str) -> bool:
    parts = relative_path.split("/")
    return (
        len(parts) == 3
        and parts[0] == "model-runs"
        and _is_runtime_model_operation_path(relative_path)
        and (_ROLE_INVOCATION_LEASE_FILE.fullmatch(parts[2]) is not None)
    )


@dataclass(frozen=True)
class AgentRunSession:
    """Resolved local services for one oracle-isolated agent run."""

    context: AgentRunContext
    project_root: Path
    run_dir: Path
    source_root: Path
    state_dir: Path
    runtime_anchors_dir: Path
    evidence_dir: Path
    workspaces_dir: Path
    scratch_dir: Path
    checkpoint_path: Path
    store: _WorkflowArtifactStore
    _directory_identities: tuple[tuple[str, int, int], ...]

    @classmethod
    def initialize(
        cls,
        project_root: Path,
        run_dir: Path,
        *,
        run_id: str,
        thread_id: str,
        slice_id: str,
        source_root: str,
        request_digest: str,
        agent_definition_digests: AgentDefinitionDigests | Mapping[str, str],
        provider_id: str,
        model_id: str,
    ) -> AgentRunSession:
        """Create a new run and bind it to current source and agent bytes."""

        root = _safe_root(project_root, "project root")
        normalized_source = validate_relative_path(source_root)
        _reject_oracle_segments(normalized_source, role="source root")
        source = _safe_descendant_directory(root, normalized_source, "source root")
        _preflight_source_tree(source)
        source_snapshot = snapshot_tree(source)
        _reject_oracle_snapshot(source_snapshot)

        run_relative = _requested_relative_path(root, run_dir, "run directory")
        if _is_within(run_relative, normalized_source):
            raise PolicyViolation("run directory cannot be created inside the source root")
        digests = AgentDefinitionDigests.model_validate(agent_definition_digests)
        context = AgentRunContext(
            run_id=run_id,
            thread_id=thread_id,
            slice_id=slice_id,
            run_directory=run_relative,
            source_root=normalized_source,
            source_revision=source_snapshot.revision,
            request_digest=request_digest,
            agent_definition_digests=digests,
            provider_id=_sanitize_public_identifier(provider_id),
            model_id=_sanitize_public_identifier(model_id),
        )
        _assert_portable_context(context, root, source)

        destination = _create_new_directory_tree(root, run_relative)
        state = _new_child_directory(destination, "state")
        runtime_anchors = _new_child_directory(state, "anchors")
        evidence = _new_child_directory(destination, "evidence")
        workspaces = _new_child_directory(destination, "workspaces")
        scratch = _new_child_directory(destination, "scratch")
        portable_store = _PortableArtifactStore(
            evidence,
            forbidden_absolute_paths=(root, source),
        )
        store = _WorkflowArtifactStore(
            portable_store,
            runtime_state_root=state,
        )
        store.write_json("run-context.json", context)
        context_digest = artifact_digest(context)
        _write_exclusive_json(
            state / "runtime.json",
            _RuntimeBinding(context_digest=context_digest).model_dump(mode="json"),
        )
        checkpoint = state / "checkpoints.sqlite3"
        _create_private_file(checkpoint)

        session = cls(
            context=context,
            project_root=root,
            run_dir=destination,
            source_root=source,
            state_dir=state,
            runtime_anchors_dir=runtime_anchors,
            evidence_dir=evidence,
            workspaces_dir=workspaces,
            scratch_dir=scratch,
            checkpoint_path=checkpoint,
            store=store,
            _directory_identities=_capture_directory_identities(
                destination,
                state,
                runtime_anchors,
                evidence,
                workspaces,
                scratch,
            ),
        )
        session.write_index("initialized", ("run-context.json",))
        session.verify_index("initialized", exact=True)
        session.verify_source_revision()
        return session

    @classmethod
    def load(cls, project_root: Path, run_dir: Path) -> AgentRunSession:
        """Load a run only after filesystem, context, index, and source checks."""

        return cls._load(project_root, run_dir, verify_current_source=True)

    @classmethod
    def load_historical_evidence(
        cls,
        project_root: Path,
        run_dir: Path,
    ) -> AgentRunSession:
        """Load immutable run evidence without consulting current source bytes.

        This deliberately narrow path supports historical lifecycle
        classification after the repository has evolved.  It retains the same
        private-directory, runtime-binding, portable-context, initialized-index,
        and evidence-portability checks as :meth:`load`; only the mutable source
        tree revision comparison is omitted.  Callers that inspect, resume, or
        retry a run must continue to use :meth:`load`.
        """

        return cls._load(project_root, run_dir, verify_current_source=False)

    @classmethod
    def _load(
        cls,
        project_root: Path,
        run_dir: Path,
        *,
        verify_current_source: bool,
    ) -> AgentRunSession:
        """Load common session evidence with an explicit source-check policy."""

        root = _safe_root(project_root, "project root")
        run_relative = _requested_relative_path(root, run_dir, "run directory")
        destination = _safe_descendant_directory(root, run_relative, "run directory")
        state = _safe_child_directory(destination, "state")
        runtime_anchors = _safe_child_directory(state, "anchors")
        evidence = _safe_child_directory(destination, "evidence")
        workspaces = _safe_child_directory(destination, "workspaces")
        scratch = _safe_child_directory(destination, "scratch")
        for path, role in (
            (destination, "run directory"),
            (state, "state directory"),
            (runtime_anchors, "runtime anchors directory"),
            (evidence, "evidence directory"),
            (workspaces, "workspaces directory"),
            (scratch, "scratch directory"),
        ):
            _require_private_directory(path, role)
        checkpoint = state / "checkpoints.sqlite3"
        _require_private_regular_file(checkpoint, "checkpoint database")

        runtime = _load_runtime_binding(state / "runtime.json")
        bootstrap_store = ArtifactStore(evidence)
        try:
            context = AgentRunContext.model_validate(bootstrap_store.read_json("run-context.json"))
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("portable run context is malformed") from exc
        if context.run_directory != run_relative:
            raise PolicyViolation("portable run context belongs to a different run directory")
        if artifact_digest(context) != runtime.context_digest:
            raise PolicyViolation("portable run context digest does not match runtime state")

        source = _safe_descendant_directory(root, context.source_root, "source root")
        _assert_portable_context(context, root, source)
        portable_store = _PortableArtifactStore(
            evidence,
            forbidden_absolute_paths=(root, source),
        )
        store = _WorkflowArtifactStore(
            portable_store,
            runtime_state_root=state,
        )
        store.read_json("run-context.json")
        session = cls(
            context=context,
            project_root=root,
            run_dir=destination,
            source_root=source,
            state_dir=state,
            runtime_anchors_dir=runtime_anchors,
            evidence_dir=evidence,
            workspaces_dir=workspaces,
            scratch_dir=scratch,
            checkpoint_path=checkpoint,
            store=store,
            _directory_identities=_capture_directory_identities(
                destination,
                state,
                runtime_anchors,
                evidence,
                workspaces,
                scratch,
            ),
        )
        session.verify_index("initialized", exact=False)
        if verify_current_source:
            session.verify_source_revision()
        return session

    def verify_source_revision(self) -> None:
        """Reject a run when source bytes or inventory no longer match context."""

        self._verify_layout()
        _preflight_source_tree(self.source_root)
        current = snapshot_tree(self.source_root)
        _reject_oracle_snapshot(current)
        if current.revision != self.context.source_revision:
            raise PolicyViolation("source content revision changed after run initialization")

    def validate_portable_evidence(self, value: Any) -> None:
        """Validate evidence confidentiality before another store persists it."""

        self._verify_context_binding()
        normalized = json.loads(canonical_json_bytes(value).decode("utf-8"))
        _assert_portable_evidence_value(
            normalized,
            (str(self.project_root), str(self.source_root)),
        )

    def promote_model_operation_artifacts(self) -> tuple[str, ...]:
        """Bind the in-flight model transaction into the next lifecycle."""

        self._verify_context_binding()
        return self.store.promote_model_operation_artifacts()

    def role_invocation_lease_paths(self) -> tuple[str, ...]:
        """Return the bounded runtime provider-claim inventory."""

        self._verify_context_binding()
        return self.store.role_invocation_lease_paths()

    def model_operation_artifact_paths(self) -> tuple[str, ...]:
        """Return the bounded in-flight transaction inventory."""

        self._verify_context_binding()
        return self.store.model_operation_artifact_paths()

    def is_model_operation_artifact_promoted(self, relative_path: str) -> bool:
        """Verify whether runtime and portable copies are identically bound."""

        self._verify_context_binding()
        return self.store.is_model_operation_artifact_promoted(relative_path)

    def enable_inflight_model_routing(self) -> None:
        """Bind this loaded session's store to the model-operation transaction."""

        self._verify_context_binding()
        self.store.enable_inflight_model_routing()

    def has_runtime_anchor(self, kind: str) -> bool:
        """Return whether an independently stored runtime anchor exists safely."""

        self._verify_context_binding()
        path = self._runtime_anchor_path(kind)
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        anchor = _load_runtime_evidence_anchor(path)
        self._validate_runtime_anchor(anchor, kind)
        return True

    def bind_runtime_anchor(self, kind: str, value: Any) -> None:
        """Create or idempotently verify one immutable non-portable digest anchor."""

        self._verify_context_binding()
        normalized_kind = _validate_index_kind(kind)
        expected = _RuntimeEvidenceAnchor(
            anchor_kind=normalized_kind,
            context_digest=artifact_digest(self.context),
            payload_digest=artifact_digest(value),
        )
        path = self._runtime_anchor_path(normalized_kind)
        try:
            _write_exclusive_json(path, expected.model_dump(mode="json"))
        except FileExistsError:
            existing = _load_runtime_evidence_anchor(path)
            if existing != expected:
                raise PolicyViolation(
                    f"immutable runtime anchor differs from current evidence: {normalized_kind}"
                ) from None
        self.verify_runtime_anchor(normalized_kind, value)

    def verify_runtime_anchor(self, kind: str, value: Any) -> None:
        """Verify evidence against its independent non-portable runtime anchor."""

        self._verify_context_binding()
        normalized_kind = _validate_index_kind(kind)
        anchor = _load_runtime_evidence_anchor(self._runtime_anchor_path(normalized_kind))
        self._validate_runtime_anchor(anchor, normalized_kind)
        if anchor.payload_digest != artifact_digest(value):
            raise PolicyViolation(f"runtime evidence anchor digest mismatch: {normalized_kind}")

    def _runtime_anchor_path(self, kind: str) -> Path:
        normalized_kind = _validate_index_kind(kind)
        return self.runtime_anchors_dir / f"{normalized_kind}.json"

    def _validate_runtime_anchor(
        self,
        anchor: _RuntimeEvidenceAnchor,
        kind: str,
    ) -> None:
        normalized_kind = _validate_index_kind(kind)
        if anchor.anchor_kind != normalized_kind:
            raise PolicyViolation("runtime evidence anchor has the wrong kind")
        if anchor.context_digest != artifact_digest(self.context):
            raise PolicyViolation("runtime evidence anchor context digest mismatch")

    def write_index(
        self,
        kind: str,
        paths: Iterable[str] | None = None,
    ) -> Path:
        """Freeze a sorted digest inventory at one named lifecycle boundary."""

        self._verify_context_binding()
        normalized_kind = _validate_index_kind(kind)
        index_path = f"indexes/{normalized_kind}.json"
        if paths is None:
            normalized_paths = tuple(
                path for path in _portable_json_paths(self.evidence_dir) if path != index_path
            )
        else:
            requested = tuple(validate_relative_path(path) for path in paths)
            if len(requested) != len(set(requested)):
                raise PolicyViolation("lifecycle index paths must be unique")
            normalized_paths = tuple(sorted(requested))
        if index_path in normalized_paths:
            raise PolicyViolation("a lifecycle index cannot include itself")

        artifacts = tuple(
            _LifecycleArtifact(
                path=path,
                canonical_sha256=artifact_digest(self.store.read_json(path)),
            )
            for path in normalized_paths
        )
        index = _LifecycleIndex(
            index_kind=normalized_kind,
            slice_id=self.context.slice_id,
            context_digest=artifact_digest(self.context),
            artifacts=artifacts,
        )
        return self.store.write_json(index_path, index)

    def verify_index(self, kind: str, *, exact: bool = True) -> None:
        """Verify a lifecycle inventory and, optionally, the current artifact set."""

        self._verify_context_binding()
        normalized_kind = _validate_index_kind(kind)
        index_path = f"indexes/{normalized_kind}.json"
        try:
            index = _LifecycleIndex.model_validate(self.store.read_json(index_path))
        except (TypeError, ValueError) as exc:
            raise PolicyViolation(f"{normalized_kind} lifecycle index is malformed") from exc
        if index.index_kind != normalized_kind:
            raise PolicyViolation(f"{normalized_kind} lifecycle index has the wrong kind")
        if index.slice_id != self.context.slice_id:
            raise PolicyViolation(f"{normalized_kind} lifecycle index belongs to another slice")
        if index.context_digest != artifact_digest(self.context):
            raise PolicyViolation(f"{normalized_kind} lifecycle index context digest mismatch")

        indexed_paths = tuple(artifact.path for artifact in index.artifacts)
        if index_path in indexed_paths:
            raise PolicyViolation(f"{normalized_kind} lifecycle index includes itself")
        if exact:
            current_paths = tuple(
                path for path in _portable_json_paths(self.evidence_dir) if path != index_path
            )
            if indexed_paths != current_paths:
                raise PolicyViolation(
                    f"{normalized_kind} lifecycle index does not contain the exact artifact set"
                )
        for artifact in index.artifacts:
            try:
                current_digest = artifact_digest(self.store.read_json(artifact.path))
            except FileNotFoundError as exc:
                raise PolicyViolation(
                    f"{normalized_kind} lifecycle artifact is missing: {artifact.path}"
                ) from exc
            if current_digest != artifact.canonical_sha256:
                raise PolicyViolation(
                    f"{normalized_kind} lifecycle artifact digest mismatch: {artifact.path}"
                )

    def _verify_context_binding(self) -> None:
        self._verify_layout()
        runtime = _load_runtime_binding(self.state_dir / "runtime.json")
        try:
            evidence_context = AgentRunContext.model_validate(
                self.store.read_json("run-context.json")
            )
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("portable run context is malformed") from exc
        if evidence_context != self.context:
            raise PolicyViolation("portable run context changed after session loading")
        if artifact_digest(evidence_context) != runtime.context_digest:
            raise PolicyViolation("portable run context digest does not match runtime state")

    def _verify_layout(self) -> None:
        for raw_path, device, inode in self._directory_identities:
            path = Path(raw_path)
            metadata = _require_private_directory(path, "run session directory")
            if (metadata.st_dev, metadata.st_ino) != (device, inode):
                raise PolicyViolation("run session directory identity changed")
        _require_private_regular_file(self.checkpoint_path, "checkpoint database")


def _sanitize_public_identifier(value: str) -> str:
    normalized = value.strip()
    redacted = SecretRedactor().redact(normalized).text
    return _API_KEY_TOKEN.sub(REDACTED, redacted)


def _looks_absolute(value: str) -> bool:
    return value.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE.match(value) is not None


def _reject_oracle_segments(relative_path: str, *, role: str) -> None:
    if any(part.casefold() in _ORACLE_SEGMENTS for part in relative_path.split("/")):
        raise PolicyViolation(f"{role} cannot contain expected, golden, or oracle segments")


def _reject_oracle_snapshot(snapshot: TreeSnapshot) -> None:
    paths = (*snapshot.directories, *(entry.path for entry in snapshot.entries))
    for path in paths:
        if any(part.casefold() in _ORACLE_SEGMENTS for part in path.split("/")):
            raise PolicyViolation("source tree contains expected, golden, or oracle content")


def _assert_portable_context(context: AgentRunContext, root: Path, source: Path) -> None:
    payload = canonical_json_bytes(context).decode("utf-8")
    forbidden = (str(root), str(source))
    if any(value in payload for value in forbidden):
        raise PolicyViolation("portable run context cannot contain absolute routing paths")
    if _sanitize_public_identifier(context.provider_id) != context.provider_id:
        raise PolicyViolation("portable provider ID contains unredacted credentials")
    if _sanitize_public_identifier(context.model_id) != context.model_id:
        raise PolicyViolation("portable model ID contains unredacted credentials")


def _assert_portable_evidence_value(
    value: Any,
    forbidden_absolute_paths: tuple[str, ...],
    *,
    field_name: str | None = None,
) -> None:
    if field_name in _SECRET_KEYS:
        if value is None:
            return
        if not isinstance(value, str) or REDACTED not in value:
            raise PolicyViolation("portable evidence contains an unredacted credential field")
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = str(key).casefold().replace("-", "_")
            _assert_portable_evidence_value(
                child,
                forbidden_absolute_paths,
                field_name=normalized_key,
            )
        return
    if isinstance(value, list):
        for child in value:
            _assert_portable_evidence_value(
                child,
                forbidden_absolute_paths,
                field_name=field_name,
            )
        return
    if not isinstance(value, str):
        return
    secret_findings = high_confidence_secret_findings(value)
    if any(finding.kind == "api-key-token" for finding in secret_findings):
        raise PolicyViolation("portable evidence contains an API-key-shaped token")
    if secret_findings:
        raise PolicyViolation("portable evidence contains an unredacted credential")
    if any(path in value for path in forbidden_absolute_paths):
        raise PortableEvidencePolicyViolation(
            "portable evidence contains an absolute project or source path",
            evidence_category="absolute_project_or_source_path",
            field_class=_portable_evidence_field_class(field_name),
        )
    if _contains_local_absolute_path(value, field_name=field_name):
        raise PortableEvidencePolicyViolation(
            "portable evidence contains a local absolute path",
            evidence_category="local_absolute_path",
            field_class=_portable_evidence_field_class(field_name),
        )


def _portable_evidence_field_class(field_name: str | None) -> PortableEvidenceFieldClass:
    if field_name in _SOURCE_BEARING_TEXT_FIELDS:
        return "source_bearing"
    if field_name in _OPAQUE_TEXT_FIELDS:
        return "narrative"
    return "structured"


def _contains_local_absolute_path(value: str, *, field_name: str | None) -> bool:
    inspected = value
    if field_name == "unified_diff":
        # New-file and deleted-file unified diffs use /dev/null as a format
        # sentinel, not as local routing authority.  Remove only the two exact
        # header lines; body occurrences, suffixes, and all other fields remain
        # subject to the ordinary absolute-path prohibition.
        inspected_lines: list[str] = []
        for line in value.split("\n"):
            if line in {"--- /dev/null", "+++ /dev/null"}:
                inspected_lines.append("")
            elif line.startswith("+") and not line.startswith("+++ "):
                inspected_lines.append(line[1:])
            elif line.startswith("-") and not line.startswith("--- "):
                inspected_lines.append(line[1:])
            else:
                inspected_lines.append(line)
        inspected = "\n".join(inspected_lines)
    if _FILE_URI.search(inspected) is not None:
        return True
    if any(
        pattern.search(inspected) is not None
        for pattern in (
            _WINDOWS_ABSOLUTE_ANYWHERE,
            _WINDOWS_UNC_ANYWHERE,
            _MARKDOWN_WINDOWS_DRIVE_ABSOLUTE_ANYWHERE,
            _MARKDOWN_WINDOWS_UNC_ABSOLUTE_ANYWHERE,
            _WINDOWS_ROOT_RELATIVE_ANYWHERE,
            _QUOTED_FORWARD_UNC_ANYWHERE,
        )
    ):
        return True
    without_remote_urls = _NON_FILE_URI.sub("", inspected)
    if field_name in _OPAQUE_TEXT_FIELDS:
        # Models often render bounded alternatives as Markdown inline-code
        # atoms (for example, ``OPEN``/``CLOSED``/``ALL``).  The separator
        # slash is prose rather than a route.  Neutralize only separators in a
        # complete sequence of conservative identifier/expression atoms; a
        # slash inside an atom, a path-shaped atom, or any unbalanced span is
        # still inspected by the ordinary fail-closed path rules below.
        without_remote_urls = _MARKDOWN_INLINE_CODE_ALTERNATIVES.sub(
            lambda match: match.group(0).replace("`/`", "` and `"),
            without_remote_urls,
        )
    if (
        _LOCAL_FILESYSTEM_ROOT.search(without_remote_urls) is not None
        or _MARKDOWN_UNDERSCORE_LOCAL_FILESYSTEM_ROOT.search(without_remote_urls) is not None
    ):
        return True
    normalized = without_remote_urls.strip()
    if field_name in _OPAQUE_TEXT_FIELDS:
        if field_name in _SOURCE_BEARING_TEXT_FIELDS:
            # Source text legitimately contains language syntax that starts
            # with a slash: Apex/JavaScript comments, CSS block comments,
            # JavaScript regular-expression literals, division, and XML end
            # tags.  For these fields, retain the unconditional URI,
            # Windows/UNC, exact project/source, and credential checks above,
            # then reject only high-confidence local POSIX roots.  Narrative
            # fields below keep the stricter route-aware token policy.
            return False
        # Source, curated prose, and diffs can legitimately contain only the
        # exact portable route families used by the bounded Salesforce and
        # Mule slices.  Every other POSIX absolute token remains forbidden.
        if (
            normalized == "/"
            or _QUOTED_URI_ROOT.search(without_remote_urls) is not None
            or _ASSIGNED_URI_ROOT.search(without_remote_urls) is not None
        ):
            return True
        return any(
            not _is_portable_route_token(match.group(0))
            for match in _OPAQUE_POSIX_ABSOLUTE_ANYWHERE.finditer(without_remote_urls)
        )
    return (
        _looks_absolute(normalized)
        or _STRUCTURED_POSIX_ABSOLUTE_ANYWHERE.search(without_remote_urls) is not None
    )


def _is_portable_route_token(value: str) -> bool:
    token = value.rstrip(_TRAILING_ROUTE_PUNCTUATION)
    if token in _PORTABLE_EXACT_ROUTES:
        return True
    if token == "/apex":
        return True
    if token.startswith("/apex/"):
        page_name = token[len("/apex/") :]
        return _VISUALFORCE_PAGE_ROUTE_SEGMENT.fullmatch(page_name) is not None
    root = next(
        (
            candidate
            for candidate in _PORTABLE_ROUTE_ROOTS
            if token == candidate or token.startswith(f"{candidate}/")
        ),
        None,
    )
    if root is None:
        return False
    if token == root:
        return True
    segments = token[len(root) + 1 :].split("/")
    return bool(segments) and all(
        segment not in {"", ".", ".."}
        and "%" not in segment
        and "\\" not in segment
        and _PORTABLE_ROUTE_SEGMENT.fullmatch(segment) is not None
        for segment in segments
    )


def _preflight_source_tree(root: Path) -> None:
    """Reject forbidden names and unsafe objects before snapshotting file bytes."""

    if any(part.casefold() in _ORACLE_SEGMENTS for part in root.parts):
        raise PolicyViolation("source path contains expected, golden, or oracle content")
    _preflight_source_directory(root, root)


def _preflight_source_directory(root: Path, directory: Path) -> None:
    try:
        with os.scandir(directory) as children:
            entries = tuple(sorted(children, key=lambda child: child.name))
    except OSError as exc:
        raise PolicyViolation("source tree inventory could not be inspected safely") from exc
    for child in entries:
        relative = Path(child.path).relative_to(root).as_posix()
        if any(part.casefold() in _ORACLE_SEGMENTS for part in relative.split("/")):
            raise PolicyViolation("source tree contains expected, golden, or oracle content")
        try:
            child_metadata = child.stat(follow_symlinks=False)
        except OSError as exc:
            raise PolicyViolation(f"source path could not be inspected safely: {relative}") from exc
        if stat.S_ISLNK(child_metadata.st_mode):
            raise PolicyViolation(f"symlinks are not allowed in the source tree: {relative}")
        if stat.S_ISDIR(child_metadata.st_mode):
            _preflight_source_directory(root, Path(child.path))
            continue
        if stat.S_ISREG(child_metadata.st_mode):
            continue
        raise PolicyViolation(f"unsupported special file in source tree: {relative}")


def _safe_root(path: Path, role: str) -> Path:
    metadata = _directory_metadata(path, role)
    if stat.S_ISLNK(metadata.st_mode):  # pragma: no cover - covered by helper message
        raise PolicyViolation(f"{role} cannot be a symlink")
    return path.resolve(strict=True)


def _directory_metadata(path: Path, role: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PolicyViolation(f"{role} does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise PolicyViolation(f"{role} cannot be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation(f"{role} must be a directory")
    return metadata


def _require_private_directory(path: Path, role: str) -> os.stat_result:
    metadata = _directory_metadata(path, role)
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PolicyViolation(f"{role} must use mode 0700")
    return metadata


def _safe_descendant_directory(root: Path, relative_path: str, role: str) -> Path:
    normalized = validate_relative_path(relative_path)
    current = root
    for part in normalized.split("/"):
        current = current / part
        _directory_metadata(current, role)
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:  # pragma: no cover - defensive after no-follow traversal
        raise PolicyViolation(f"{role} escapes the project root") from exc
    return resolved


def _safe_child_directory(parent: Path, name: str) -> Path:
    return _safe_descendant_directory(parent, name, f"{name} directory")


def _requested_relative_path(root: Path, requested: Path, role: str) -> str:
    if requested.is_absolute():
        candidate = Path(os.path.abspath(requested))
    else:
        candidate = Path(os.path.abspath(root / requested))
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise PolicyViolation(f"{role} must be inside the project root") from exc
    try:
        return validate_relative_path(relative)
    except ValueError as exc:
        raise PolicyViolation(f"{role} must identify a project-relative child") from exc


def _create_new_directory_tree(root: Path, relative_path: str) -> Path:
    parts = validate_relative_path(relative_path).split("/")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors = [os.open(root, flags)]
    try:
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
            except FileExistsError:
                if final:
                    raise PolicyViolation("run directory must not already exist") from None
            except OSError as exc:
                raise PolicyViolation("run directory could not be created safely") from exc
            try:
                child = os.open(part, flags, dir_fd=descriptors[-1])
            except OSError as exc:
                raise PolicyViolation("run directory path contains an unsafe component") from exc
            descriptors.append(child)
        os.fsync(descriptors[-1])
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    destination = root.joinpath(*parts)
    os.chmod(destination, 0o700)
    return destination


def _new_child_directory(parent: Path, name: str) -> Path:
    destination = parent / name
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError as exc:  # pragma: no cover - parent is newly exclusive
        raise PolicyViolation(f"run child already exists: {name}") from exc
    os.chmod(destination, 0o700)
    return destination


def _create_private_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def _write_exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(value) + b"\n"
    expected_parent = _require_private_directory(path.parent, "exclusive JSON parent directory")
    parent_descriptor = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor: int | None = None
    staged_name = f".{path.name}.{uuid4().hex}.pending"
    staged_present = False
    try:
        opened_parent = os.fstat(parent_descriptor)
        if (opened_parent.st_dev, opened_parent.st_ino) != (
            expected_parent.st_dev,
            expected_parent.st_ino,
        ):
            raise PolicyViolation("exclusive JSON parent directory changed while being opened")
        descriptor = os.open(
            staged_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        staged_present = True
        os.set_inheritable(descriptor, False)
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:  # pragma: no cover - defensive OS boundary
                raise OSError("exclusive JSON payload write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None

        # Publish only complete, durable bytes. Hard-link creation is atomic and
        # retains O_EXCL semantics: a concurrent publisher receives
        # FileExistsError and can safely read the already-complete winner.
        os.link(
            staged_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        os.unlink(staged_name, dir_fd=parent_descriptor)
        staged_present = False
        os.fsync(parent_descriptor)
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            try:
                if staged_present:
                    try:
                        os.unlink(staged_name, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass
                    else:
                        os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)


def _require_private_regular_file(path: Path, role: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PolicyViolation(f"{role} does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PolicyViolation(f"{role} must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PolicyViolation(f"{role} must use mode 0600")
    return metadata


def _read_private_json(path: Path, role: str) -> Any:
    expected = _require_private_regular_file(path, role)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise PolicyViolation(f"{role} changed while being opened")
        if actual.st_size > 1024 * 1024:
            raise PolicyViolation(f"{role} exceeds the size limit")
        payload = b""
        while chunk := os.read(descriptor, 64 * 1024):
            payload += chunk
    finally:
        os.close(descriptor)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation(f"{role} is not valid UTF-8 JSON") from exc


def _load_runtime_binding(path: Path) -> _RuntimeBinding:
    try:
        return _RuntimeBinding.model_validate(_read_private_json(path, "runtime binding"))
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("runtime binding is malformed") from exc


def _load_runtime_evidence_anchor(path: Path) -> _RuntimeEvidenceAnchor:
    try:
        return _RuntimeEvidenceAnchor.model_validate(
            _read_private_json(path, "runtime evidence anchor")
        )
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("runtime evidence anchor is malformed") from exc


def _capture_directory_identities(*paths: Path) -> tuple[tuple[str, int, int], ...]:
    identities = []
    for path in paths:
        metadata = _require_private_directory(path, "run session directory")
        identities.append((str(path), metadata.st_dev, metadata.st_ino))
    return tuple(identities)


def _validate_index_kind(kind: str) -> str:
    normalized = kind.strip()
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError("lifecycle index kind must be a bounded identifier")
    return normalized


def _portable_json_paths(root: Path) -> tuple[str, ...]:
    paths: list[str] = []

    def scan(directory: Path) -> None:
        with os.scandir(directory) as children:
            ordered = sorted(children, key=lambda child: child.name)
        for child in ordered:
            metadata = child.stat(follow_symlinks=False)
            relative = Path(child.path).relative_to(root).as_posix()
            if stat.S_ISLNK(metadata.st_mode):
                raise PolicyViolation(f"portable evidence cannot contain symlinks: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                scan(Path(child.path))
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise PolicyViolation(f"portable evidence cannot contain special files: {relative}")
            if not relative.endswith(".json"):
                raise PolicyViolation(f"portable evidence must be JSON: {relative}")
            paths.append(validate_relative_path(relative))

    scan(root)
    return tuple(sorted(paths))


def _is_within(candidate: str, parent: str) -> bool:
    candidate_parts = candidate.split("/")
    parent_parts = parent.split("/")
    return candidate_parts[: len(parent_parts)] == parent_parts


__all__ = [
    "AgentDefinitionDigests",
    "AgentRunContext",
    "AgentRunSession",
    "PortableEvidencePolicyViolation",
    "RUNTIME_STATE_PATHS",
]
