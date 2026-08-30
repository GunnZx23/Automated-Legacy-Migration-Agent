"""Validated, versioned Markdown definitions for the three migration agents.

The Markdown body is the model's system prompt.  YAML front matter is a
machine-checked capability declaration, not a source of runtime authority: the
Python role classes still expose only their narrow methods.  Loading fails
closed on inventory drift, links, oversized prompts, malformed metadata, or a
prompt that claims another role's identity.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import ConfigDict, Field, field_validator

from legacy_migration_agent.contracts import StrictModel

MAX_AGENT_FILE_BYTES = 64 * 1024
MIN_AGENT_PROMPT_CHARS = 1_200
MAX_AGENT_PROMPT_CHARS = 60_000


class AgentDefinitionError(ValueError):
    """Raised when the on-disk agent registry is not safe and complete."""


class AgentRole(StrEnum):
    ARCHITECT = "architect"
    ENGINEER = "engineer"
    VALIDATOR = "validator"


class AgentPermissions(StrictModel):
    """Declared capabilities; exact role policy is checked during loading."""

    repository_read: bool
    isolated_workspace_write: bool
    command_execution: bool
    network_access: bool
    human_gate_override: bool


class AgentModelBehavior(StrictModel):
    structured_output: Literal[True]
    private_chain_of_thought: Literal[False]
    native_tools: tuple[()] = ()
    structured_actions: tuple[str, ...] = Field(min_length=1, max_length=3)
    max_response_chars: int = Field(ge=1_000, le=250_000)

    @field_validator("structured_actions")
    @classmethod
    def validate_structured_actions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("structured agent actions must be unique")
        if any(
            not value
            or len(value) > 120
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for character in value
            )
            for value in values
        ):
            raise ValueError("structured agent actions must use bounded public identifiers")
        return values


class AgentDefinitionHeader(StrictModel):
    """Strict YAML front matter shared by every role definition."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )

    schema_version: Literal["1.0"]
    role: AgentRole
    version: str = Field(
        min_length=3,
        max_length=80,
        pattern=r"^(architect|engineer|validator)/v[1-9][0-9]*$",
    )
    permissions: AgentPermissions
    input_contracts: tuple[str, ...] = Field(min_length=1, max_length=8)
    output_contract: str = Field(min_length=1, max_length=120)
    model_behavior: AgentModelBehavior

    @field_validator("input_contracts")
    @classmethod
    def validate_input_contracts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("input contracts must be unique")
        return values


class AgentDefinition(StrictModel):
    """One validated role prompt and its immutable identity metadata."""

    header: AgentDefinitionHeader
    relative_path: str
    prompt: str = Field(min_length=MIN_AGENT_PROMPT_CHARS, max_length=MAX_AGENT_PROMPT_CHARS)
    definition_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def role(self) -> AgentRole:
        return self.header.role

    @property
    def version(self) -> str:
        return self.header.version

    @property
    def system_prompt(self) -> str:
        """Return the exact Markdown prompt after its front matter."""

        return self.prompt


_FILENAMES: Mapping[AgentRole, str] = MappingProxyType(
    {
        AgentRole.ARCHITECT: "architect.md",
        AgentRole.ENGINEER: "engineer.md",
        AgentRole.VALIDATOR: "validator.md",
    }
)

_PERMISSIONS: Mapping[AgentRole, AgentPermissions] = MappingProxyType(
    {
        AgentRole.ARCHITECT: AgentPermissions(
            repository_read=True,
            isolated_workspace_write=False,
            command_execution=False,
            network_access=False,
            human_gate_override=False,
        ),
        AgentRole.ENGINEER: AgentPermissions(
            repository_read=True,
            isolated_workspace_write=True,
            command_execution=False,
            network_access=False,
            human_gate_override=False,
        ),
        AgentRole.VALIDATOR: AgentPermissions(
            repository_read=True,
            isolated_workspace_write=False,
            command_execution=False,
            network_access=False,
            human_gate_override=False,
        ),
    }
)

_INPUT_CONTRACTS: Mapping[AgentRole, tuple[str, ...]] = MappingProxyType(
    {
        AgentRole.ARCHITECT: ("ArchitectModelContext", "ArchitectConversationContext"),
        AgentRole.ENGINEER: (
            "EngineerWorkspaceContext",
            "EngineerCorrectionProviderContext",
        ),
        AgentRole.VALIDATOR: ("ValidatorEvidenceContext",),
    }
)

_OUTPUT_CONTRACTS: Mapping[AgentRole, str] = MappingProxyType(
    {
        AgentRole.ARCHITECT: "ArchitectManifestProposal|ArchitectConversationReply",
        AgentRole.ENGINEER: "EngineerModelOutcome",
        AgentRole.VALIDATOR: "ValidatorModelAdvisory",
    }
)

_VERSIONS: Mapping[AgentRole, str] = MappingProxyType(
    {
        AgentRole.ARCHITECT: "architect/v17",
        AgentRole.ENGINEER: "engineer/v30",
        AgentRole.VALIDATOR: "validator/v5",
    }
)

_STRUCTURED_ACTIONS: Mapping[AgentRole, tuple[str, ...]] = MappingProxyType(
    {
        AgentRole.ARCHITECT: (
            "dependency_graph.select_node_ids",
            "llm_wiki.select_page_ids",
            "migration_plan.propose_semantics",
        ),
        AgentRole.ENGINEER: ("candidate.propose_file_updates",),
        AgentRole.VALIDATOR: ("validation.review_evidence",),
    }
)


class AgentRegistry:
    """An exact three-role registry with no fallback or implicit prompt."""

    def __init__(self, definitions: Mapping[AgentRole, AgentDefinition]) -> None:
        if set(definitions) != set(AgentRole):
            missing = sorted(role.value for role in set(AgentRole) - set(definitions))
            extra = sorted(str(role) for role in set(definitions) - set(AgentRole))
            details = []
            if missing:
                details.append("missing roles: " + ", ".join(missing))
            if extra:
                details.append("extra roles: " + ", ".join(extra))
            raise AgentDefinitionError("invalid agent registry (" + "; ".join(details) + ")")
        self._definitions = MappingProxyType(dict(definitions))

    def get(self, role: AgentRole | str) -> AgentDefinition:
        try:
            normalized = role if isinstance(role, AgentRole) else AgentRole(role)
            return self._definitions[normalized]
        except (KeyError, ValueError) as exc:
            raise AgentDefinitionError(f"unknown agent role: {role}") from exc

    @property
    def definitions(self) -> tuple[AgentDefinition, ...]:
        return tuple(self._definitions[role] for role in AgentRole)


def load_agent_registry(root: Path | str) -> AgentRegistry:
    """Load exactly Architect, Engineer, and Validator definitions from disk."""

    registry_root = Path(root)
    try:
        root_metadata = registry_root.lstat()
    except FileNotFoundError as exc:
        raise AgentDefinitionError(
            f"agent definition root does not exist: {registry_root}"
        ) from exc
    if stat.S_ISLNK(root_metadata.st_mode):
        raise AgentDefinitionError("agent definition root cannot be a symlink")
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise AgentDefinitionError("agent definition root must be a directory")
    registry_root = registry_root.resolve(strict=True)

    with os.scandir(registry_root) as entries:
        names = tuple(sorted(entry.name for entry in entries))
    expected_names = tuple(sorted(_FILENAMES.values()))
    if names != expected_names:
        missing = sorted(set(expected_names) - set(names))
        extra = sorted(set(names) - set(expected_names))
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unexpected: " + ", ".join(extra))
        raise AgentDefinitionError(
            "agent definition inventory must contain exactly three Markdown files ("
            + "; ".join(details)
            + ")"
        )

    loaded: dict[AgentRole, AgentDefinition] = {}
    for expected_role, filename in _FILENAMES.items():
        definition = _load_definition(registry_root, filename, expected_role)
        if definition.role in loaded:
            raise AgentDefinitionError(f"duplicate agent role: {definition.role.value}")
        loaded[definition.role] = definition
    return AgentRegistry(loaded)


def _load_definition(root: Path, filename: str, expected_role: AgentRole) -> AgentDefinition:
    path = root / filename
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise AgentDefinitionError(f"agent definition cannot be a symlink: {filename}")
    if not stat.S_ISREG(metadata.st_mode):
        raise AgentDefinitionError(f"agent definition must be a regular file: {filename}")
    if metadata.st_size > MAX_AGENT_FILE_BYTES:
        raise AgentDefinitionError(f"agent definition exceeds byte limit: {filename}")
    payload = path.read_bytes()
    if len(payload) != metadata.st_size:
        raise AgentDefinitionError(f"agent definition changed while being read: {filename}")
    if b"\x00" in payload:
        raise AgentDefinitionError(f"agent definition contains NUL bytes: {filename}")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentDefinitionError(f"agent definition is not UTF-8: {filename}") from exc
    header_text, prompt = _split_front_matter(text, filename)
    try:
        raw_header = yaml.safe_load(header_text)
    except yaml.YAMLError as exc:
        raise AgentDefinitionError(f"malformed YAML front matter: {filename}") from exc
    if not isinstance(raw_header, dict):
        raise AgentDefinitionError(f"front matter must be a mapping: {filename}")
    try:
        header = AgentDefinitionHeader.model_validate(raw_header)
    except ValueError as exc:
        raise AgentDefinitionError(f"invalid agent front matter in {filename}: {exc}") from exc
    _validate_role_contract(header, expected_role, filename)
    _validate_prompt(prompt, expected_role, filename)
    return AgentDefinition(
        header=header,
        relative_path=filename,
        prompt=prompt,
        definition_digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
    )


def _split_front_matter(text: str, filename: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise AgentDefinitionError(
            f"agent definition must start with YAML front matter: {filename}"
        )
    closing = text.find("\n---\n", 4)
    if closing < 0:
        raise AgentDefinitionError(
            f"agent definition has no closing front matter delimiter: {filename}"
        )
    header_text = text[4:closing]
    prompt = text[closing + len("\n---\n") :].strip()
    if not header_text.strip() or not prompt:
        raise AgentDefinitionError(
            f"agent definition front matter and prompt must be non-empty: {filename}"
        )
    return header_text, prompt


def _validate_role_contract(
    header: AgentDefinitionHeader,
    expected_role: AgentRole,
    filename: str,
) -> None:
    if header.role is not expected_role:
        raise AgentDefinitionError(
            f"filename/role mismatch in {filename}: expected {expected_role.value}"
        )
    if header.version != _VERSIONS[expected_role]:
        raise AgentDefinitionError(f"unsupported agent definition version in {filename}")
    if header.permissions != _PERMISSIONS[expected_role]:
        raise AgentDefinitionError(f"unsafe or incorrect permissions in {filename}")
    if header.input_contracts != _INPUT_CONTRACTS[expected_role]:
        raise AgentDefinitionError(f"incorrect input contract in {filename}")
    if header.output_contract != _OUTPUT_CONTRACTS[expected_role]:
        raise AgentDefinitionError(f"incorrect output contract in {filename}")
    if header.model_behavior.native_tools:
        raise AgentDefinitionError(f"native model tools are not supported in {filename}")
    if header.model_behavior.structured_actions != _STRUCTURED_ACTIONS[expected_role]:
        raise AgentDefinitionError(f"incorrect structured action contract in {filename}")


def _validate_prompt(prompt: str, role: AgentRole, filename: str) -> None:
    if not MIN_AGENT_PROMPT_CHARS <= len(prompt) <= MAX_AGENT_PROMPT_CHARS:
        raise AgentDefinitionError(f"agent prompt is outside the character bounds: {filename}")
    own_identity = f"Identity: You are the {role.value.title()} agent."
    if prompt.count(own_identity) != 1:
        raise AgentDefinitionError(
            f"agent prompt must declare exactly one matching identity: {filename}"
        )
    for other in AgentRole:
        if other is role:
            continue
        drift_identity = f"Identity: You are the {other.value.title()} agent."
        if drift_identity in prompt:
            raise AgentDefinitionError(f"agent prompt contains role drift in {filename}")
    required_domain_terms = ("Salesforce", "Visualforce", "LWC", "Mule 3", "Mule 4")
    missing_terms = tuple(term for term in required_domain_terms if term not in prompt)
    if missing_terms:
        raise AgentDefinitionError(
            f"agent prompt is missing domain guidance in {filename}: {', '.join(missing_terms)}"
        )
