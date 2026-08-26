"""Deterministic secret redaction for tool output and public artifacts."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from functools import partial
from pathlib import Path

from pydantic import Field

from legacy_migration_agent.contracts import StrictModel
from legacy_migration_agent.core.policies import PolicyViolation, ensure_paths_within_repository

REDACTED = "[REDACTED]"


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        "authorization-header",
        re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+"),
    ),
    (
        "sfdx-auth-url",
        re.compile(r"force://[^\s'\"]+"),
    ),
    (
        "credential-assignment",
        re.compile(
            r"(?i)(\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)"
            r"\s*[:=]\s*)['\"]?[^\s,'\";]+"
        ),
    ),
)


class RedactionFinding(StrictModel):
    kind: str = Field(min_length=1, max_length=160)
    count: int = Field(ge=1)


class RedactionResult(StrictModel):
    text: str
    findings: tuple[RedactionFinding, ...]

    @property
    def changed(self) -> bool:
        return bool(self.findings)


class FileSecretFinding(StrictModel):
    path: str
    kind: str
    line: int = Field(ge=1)


class SecretRedactor:
    """Redact known credential shapes plus caller-supplied exact values."""

    def __init__(self, explicit_secrets: Iterable[str] = ()):
        secrets = tuple(dict.fromkeys(secret for secret in explicit_secrets if secret))
        self._explicit_secrets = tuple(sorted(secrets, key=len, reverse=True))

    def redact(self, text: str) -> RedactionResult:
        redacted = text
        counts: Counter[str] = Counter()
        for secret in self._explicit_secrets:
            occurrences = redacted.count(secret)
            if occurrences:
                redacted = redacted.replace(secret, REDACTED)
                counts["explicit-secret"] += occurrences
        for kind, pattern in SECRET_PATTERNS:
            redacted, count = pattern.subn(
                partial(_replacement, kind),
                redacted,
            )
            counts[kind] += count
        return RedactionResult(
            text=redacted,
            findings=tuple(
                RedactionFinding(kind=kind, count=count)
                for kind, count in sorted(counts.items())
                if count
            ),
        )

    def scan_files(
        self,
        repository_root: Path,
        relative_paths: Iterable[str],
    ) -> tuple[FileSecretFinding, ...]:
        findings: list[FileSecretFinding] = []
        paths = ensure_paths_within_repository(repository_root, relative_paths)
        root = repository_root.resolve(strict=True)
        for path in paths:
            if not path.exists() or not path.is_file() or path.is_symlink():
                raise PolicyViolation(
                    f"secret scan requires an existing regular file: {path.relative_to(root)}"
                )
            payload = path.read_bytes()
            if b"\x00" in payload:
                raise PolicyViolation(
                    f"secret scan does not accept binary files: {path.relative_to(root)}"
                )
            text = payload.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                result = self.redact(line)
                for finding in result.findings:
                    findings.append(
                        FileSecretFinding(
                            path=path.relative_to(root).as_posix(),
                            kind=finding.kind,
                            line=line_number,
                        )
                    )
        return tuple(findings)


def _replacement(kind: str, match: re.Match[str]) -> str:
    if kind in {"authorization-header", "credential-assignment"}:
        return f"{match.group(1)}{REDACTED}"
    return REDACTED
