"""Shared contracts and bounds for model-backed migration roles."""

from __future__ import annotations

import hashlib

from pydantic import ConfigDict, Field, field_validator, model_validator

from legacy_migration_agent.contracts import (
    Sha256Digest,
    StrictModel,
    validate_relative_path,
)
from legacy_migration_agent.core.policies import PolicyViolation

MAX_SOURCE_FILE_CHARS = 32_000
MAX_SOURCE_CONTEXT_CHARS = 256_000
MAX_UPDATE_FILE_CHARS = 180_000
MAX_UPDATE_CONTEXT_CHARS = 220_000
MAX_CONTEXT_FILES = 64


class AgentRuntimeError(PolicyViolation):
    """Raised when a model proposal violates a deterministic role boundary."""


class SourceFileEvidence(StrictModel):
    """Digest-bound UTF-8 source supplied as model evidence."""

    # Source bytes are evidence. Unlike descriptive contract strings, leading
    # and trailing whitespace (including the final newline) is significant.
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    path: str
    sha256: Sha256Digest
    content: str = Field(max_length=MAX_SOURCE_FILE_CHARS)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("content")
    @classmethod
    def validate_text_content(cls, value: str) -> str:
        if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
            raise ValueError("source file evidence contains a binary control character")
        return value

    @model_validator(mode="after")
    def validate_digest(self) -> SourceFileEvidence:
        expected = f"sha256:{hashlib.sha256(self.content.encode('utf-8')).hexdigest()}"
        if self.sha256 != expected:
            raise ValueError("source file digest does not match its content")
        return self
