"""Deterministic secret redaction for tool output and public artifacts."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from functools import partial
from pathlib import Path

from pydantic import BaseModel, Field

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
    ("sfdx-auth-url", re.compile(r"force://[^\s'\"]+")),
    (
        "credential-assignment",
        re.compile(
            r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|bearer[_-]?token|"
            r"client[_-]?secret|credential|password|private[_-]?key|refresh[_-]?token|"
            r"secret|sfdx[_-]?auth[_-]?url|token)"
            r"\s*[:=]\s*)['\"]?[^\s,'\";]+"
        ),
    ),
    (
        "github-token",
        re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"),
    ),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("api-key-token", re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
)

_DIRECT_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], str | None], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        None,
    ),
    (
        "authorization-header",
        re.compile(
            r"(?i)authorization\s*:\s*(?:bearer|basic)\s+"
            r"(?P<credential>[^\s,;]+)"
        ),
        "credential",
    ),
    (
        "sfdx-auth-url",
        re.compile(r"force://[^\s'\"]+"),
        None,
    ),
    (
        "github-token",
        re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"),
        None,
    ),
    (
        "slack-token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        None,
    ),
    (
        "aws-access-key",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        None,
    ),
    (
        "api-key-token",
        re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b"),
        None,
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        None,
    ),
)

_CREDENTIAL_KEY_INNER = (
    r"api[_-]?key|access[_-]?token|auth[_-]?token|authorization|bearer[_-]?token|"
    r"client[_-]?secret|credential|password|passwd|private[_-]?key|"
    r"refresh[_-]?token|secret|sfdx[_-]?auth[_-]?url"
)
_CREDENTIAL_KEY = rf"(?:{_CREDENTIAL_KEY_INNER})"
_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"(?ix)(?<![A-Za-z0-9_])['\"]?{_CREDENTIAL_KEY}['\"]?\s*"
    r"(?::|=(?!=))\s*(?:"
    r"\"(?P<double>(?:\\.|[^\"\\])*)\"|"
    r"'(?P<single>(?:\\.|[^'\\])*)'|"
    r"(?P<placeholder>\$\{[^}\r\n]+\}|\#\[[^\]\r\n]+\]|\{\{[^}\r\n]+\}\}|"
    r"\[REDACTED\])|"
    r"(?P<bare>[^\s,;]+)"
    r")"
)
_REQUEST_CREDENTIAL_KEYS = frozenset(
    {
        "apikey",
        "accesstoken",
        "authtoken",
        "bearertoken",
        "clientsecret",
        "credential",
        "password",
        "passwd",
        "privatekey",
        "refreshtoken",
        "secret",
        "sfdxauthurl",
        "token",
    }
)
_CREDENTIAL_XML_ELEMENT = re.compile(
    rf"(?is)<(?P<tag>{_CREDENTIAL_KEY})(?:\s[^>]*)?>"
    r"(?P<value>[^<]*)"
    r"</(?P=tag)\s*>"
)
_PLACEHOLDER_VALUE = re.compile(
    r"(?ix)(?:"
    r"\$\{[^}\r\n]+\}|"
    r"\#\[[^\]\r\n]+\]|"
    r"\{\{[^}\r\n]+\}\}|"
    r"<[^<>\r\n]*(?:placeholder|redacted|secret-ref)[^<>\r\n]*>"
    r")"
)
_REFERENCE_VALUE = re.compile(
    r"(?x)(?:"
    r"(?:this|self|response|request|result|config|settings|vars|attributes|payload|"
    r"env|process\.env|os\.environ)(?:\??\.[A-Za-z_$][A-Za-z0-9_$]*|"
    r"\[['\"][^'\"]+['\"]\])+|"
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:\??\.[A-Za-z_$][A-Za-z0-9_$]*)+|"
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\s*\([^\r\n]*\)|"
    r"[A-Za-z_$][A-Za-z0-9_$]*"
    r")"
)
_SAFE_SENTINELS = frozenset(
    {
        "",
        "***",
        "[redacted]",
        "changeme",
        "change-me",
        "example",
        "none",
        "null",
        "redacted",
        "undefined",
    }
)
_REQUEST_PROPERTY_REFERENCE = re.compile(
    r"(?x)(?:\+\+|--)?[A-Za-z_$][A-Za-z0-9_$]*(?:"
    r"\??\.[A-Za-z_$][A-Za-z0-9_$]*|"
    r"\[\s*(?:['\"][^'\"\r\n]+['\"]|[A-Za-z_$][A-Za-z0-9_$]*|[0-9]+)\s*\]"
    r")+"
)
_REQUEST_SEMANTIC_REFERENCE = re.compile(
    r"(?ix)(?:request(?:_?generation|_?token)|current_?request)[A-Za-z0-9_]*"
)
_REQUEST_LOOKUP_CALL = re.compile(
    r"(?ix)(?:"
    r"(?:os\.getenv|os\.environ\.get|env\.get|config\.get|settings\.get|vars\.get)"
    r"\(\s*['\"][A-Za-z_][A-Za-z0-9_.:]*['\"]\s*\)|"
    r"get(?:password|token|access_?token|auth_?token|credential|secret|api_?key|"
    r"private_?key|refresh_?token)\(\s*\)"
    r")"
)


@dataclass(frozen=True, slots=True)
class _SecretSpan:
    kind: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _RequestCredentialRhs:
    value: str
    quoted: bool


def high_confidence_secret_findings(text: str) -> tuple[RedactionFinding, ...]:
    """Report only credential shapes safe enough to reject generated code for.

    The scanner deliberately does not treat words such as ``token`` as secrets.
    Sensitive assignments are findings only when their right-hand side is a
    literal, not a property/identifier reference, function call, environment
    lookup, or configuration placeholder.
    """

    if not isinstance(text, str):
        raise TypeError("secret scanning requires text")
    counts = Counter(span.kind for span in _secret_spans(text))
    return tuple(RedactionFinding(kind=kind, count=count) for kind, count in sorted(counts.items()))


def redact_high_confidence_secrets(text: str) -> RedactionResult:
    """Redact high-confidence secret values from non-authoritative public prose."""

    if not isinstance(text, str):
        raise TypeError("secret redaction requires text")
    spans = _secret_spans(text)
    redacted = text
    for span in reversed(spans):
        redacted = f"{redacted[: span.start]}{REDACTED}{redacted[span.end :]}"
    return RedactionResult(
        text=redacted,
        findings=high_confidence_secret_findings(text),
    )


def assert_no_high_confidence_secrets(
    value: object,
    *,
    boundary: str = "value",
) -> None:
    """Reject a nested value without echoing the matched credential material."""

    for text in _iter_text_values(value):
        findings = high_confidence_secret_findings(text)
        if findings:
            raise PolicyViolation(
                f"{boundary} contains forbidden secret-shaped material ({findings[0].kind})"
            )


def assert_no_request_secrets(
    value: object,
    *,
    boundary: str = "migration request",
) -> None:
    """Reject credentials in request prose without rejecting ordinary code references.

    Request fields are persisted and may be sent to a model, so this boundary is
    intentionally stricter than generated-code scanning. Credential assignments
    accept only placeholders/sentinels, property or index references, narrowly
    defined environment/config getters, and versioned request-token identifiers.
    Quoted right-hand sides are literals unless they are placeholders/sentinels.
    """

    assert_no_high_confidence_secrets(value, boundary=boundary)
    for text in _iter_text_values(value):
        for rhs in _iter_request_authorization_rhs(text):
            if not _is_safe_request_credential_rhs(rhs.value, quoted=rhs.quoted):
                raise PolicyViolation(f"{boundary} contains forbidden secret-shaped material")
        for rhs in _iter_request_credential_assignment_rhs(text):
            if not _is_safe_request_credential_rhs(rhs.value, quoted=rhs.quoted):
                raise PolicyViolation(f"{boundary} contains forbidden secret-shaped material")


def _iter_request_credential_assignment_rhs(text: str) -> Iterable[_RequestCredentialRhs]:
    """Lex credential assignments without rewriting comments or unrelated prose.

    Only a terminated C-style block comment in the narrow gap between a
    credential key and its operator is treated as whitespace.  This closes
    comment-obfuscation bypasses while preserving comments everywhere else.
    """

    index = 0
    while index < len(text):
        key = _read_request_key(text, index)
        if key is None:
            index += 1
            continue
        normalized_key, key_end = key
        index = max(index + 1, key_end)
        if normalized_key == "authorization" or normalized_key not in _REQUEST_CREDENTIAL_KEYS:
            continue
        operator_index = _skip_request_key_operator_gap(text, key_end)
        if not _is_exact_request_assignment_operator(text, operator_index):
            continue
        rhs = _read_request_credential_rhs(text, operator_index + 1)
        if rhs is not None:
            yield rhs


def _iter_request_authorization_rhs(text: str) -> Iterable[_RequestCredentialRhs]:
    """Lex Authorization headers independently from credential assignments."""

    index = 0
    while index < len(text):
        key = _read_request_key(text, index)
        if key is None:
            index += 1
            continue
        normalized_key, key_end = key
        index = max(index + 1, key_end)
        if normalized_key != "authorization":
            continue
        colon_index = _skip_request_key_operator_gap(text, key_end)
        if colon_index >= len(text) or text[colon_index] != ":":
            continue
        if colon_index + 1 < len(text) and text[colon_index + 1] in ":=":
            continue
        scheme_start = _skip_request_whitespace(text, colon_index + 1)
        outer_quote: str | None = None
        if scheme_start < len(text) and text[scheme_start] in {'"', "'"}:
            outer_quote = text[scheme_start]
            scheme_start = _skip_request_whitespace(text, scheme_start + 1)
        scheme_end = scheme_start
        while scheme_end < len(text) and text[scheme_end].isalpha():
            scheme_end += 1
        if text[scheme_start:scheme_end].casefold() not in {"basic", "bearer"}:
            continue
        if scheme_end == scheme_start or (
            scheme_end < len(text) and not text[scheme_end].isspace()
        ):
            continue
        rhs = _read_request_authorization_credential_rhs(text, scheme_end, outer_quote)
        if rhs is not None:
            yield rhs


def _read_request_key(text: str, start: int) -> tuple[str, int] | None:
    if start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        return None
    character = text[start]
    if character in {'"', "'"}:
        end = text.find(character, start + 1, min(len(text), start + 64))
        if end < 0:
            return None
        raw_key = text[start + 1 : end]
        key_end = end + 1
    elif character.isalpha() or character == "_":
        key_end = start + 1
        while key_end < len(text) and (text[key_end].isalnum() or text[key_end] in "_-"):
            key_end += 1
        raw_key = text[start:key_end]
    else:
        return None
    if key_end < len(text) and (text[key_end].isalnum() or text[key_end] == "_"):
        return None
    return raw_key.casefold().replace("_", "").replace("-", ""), key_end


def _skip_request_key_operator_gap(text: str, start: int) -> int:
    index = start
    while True:
        index = _skip_request_whitespace(text, index)
        if not text.startswith("/*", index):
            return index
        comment_end = text.find("*/", index + 2)
        if comment_end < 0:
            return len(text)
        index = comment_end + 2


def _skip_request_whitespace(text: str, start: int) -> int:
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _is_exact_request_assignment_operator(text: str, index: int) -> bool:
    if index >= len(text) or text[index] not in ":=":
        return False
    if index + 1 >= len(text):
        return True
    following = text[index + 1]
    if text[index] == "=":
        return following not in "=>"
    return following not in ":="


def _read_request_credential_rhs(text: str, start: int) -> _RequestCredentialRhs | None:
    index = _skip_request_whitespace(text, start)
    if index >= len(text):
        return None
    if text[index] in {'"', "'"}:
        quote = text[index]
        value_start = index + 1
        index = value_start
        while index < len(text):
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == quote:
                return _RequestCredentialRhs(text[value_start:index], quoted=True)
            index += 1
        return _RequestCredentialRhs(text[value_start:], quoted=True)
    if text[index] == "(":
        value_end = _read_balanced_request_parentheses(text, index)
        if value_end is None:
            value_end = len(text)
        tail_end = value_end
        while tail_end < len(text) and not text[tail_end].isspace() and text[tail_end] not in ",;":
            tail_end += 1
        return _RequestCredentialRhs(text[index:tail_end], quoted=False)
    value_end = index
    while value_end < len(text) and not text[value_end].isspace() and text[value_end] not in ",;":
        value_end += 1
    if value_end == index:
        return None
    return _RequestCredentialRhs(text[index:value_end], quoted=False)


def _read_request_authorization_credential_rhs(
    text: str,
    start: int,
    outer_quote: str | None,
) -> _RequestCredentialRhs | None:
    if outer_quote is None:
        return _read_request_credential_rhs(text, start)
    value_start = _skip_request_whitespace(text, start)
    index = value_start
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == outer_quote:
            value = text[value_start:index].strip()
            return _RequestCredentialRhs(value, quoted=False) if value else None
        index += 1
    value = text[value_start:].strip()
    return _RequestCredentialRhs(value, quoted=False) if value else None


def _read_balanced_request_parentheses(text: str, start: int) -> int | None:
    depth = 0
    quote: str | None = None
    index = start
    while index < len(text):
        character = text[index]
        if quote is not None:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _secret_spans(text: str) -> tuple[_SecretSpan, ...]:
    candidates: list[_SecretSpan] = []
    placeholder_spans = tuple(match.span() for match in _PLACEHOLDER_VALUE.finditer(text))
    for kind, pattern, secret_group in _DIRECT_SECRET_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(secret_group) if secret_group is not None else match.span()
            if any(
                parent_start <= start and end <= parent_end
                for parent_start, parent_end in placeholder_spans
            ):
                continue
            if (
                kind == "authorization-header"
                and secret_group is not None
                and _is_authorization_reference(match.group(secret_group))
            ):
                continue
            candidates.append(_SecretSpan(kind=kind, start=start, end=end))

    for match in _CREDENTIAL_ASSIGNMENT.finditer(text):
        group = next(
            name
            for name in ("double", "single", "placeholder", "bare")
            if match.group(name) is not None
        )
        candidate = match.group(group)
        classified_candidate = (
            _normalize_bare_credential_rhs(candidate) if group == "bare" else candidate
        )
        if _is_literal_credential_value(classified_candidate):
            start, end = match.span(group)
            candidates.append(
                _SecretSpan(kind="credential-literal-assignment", start=start, end=end)
            )

    for match in _CREDENTIAL_XML_ELEMENT.finditer(text):
        candidate = match.group("value").strip()
        if _is_literal_credential_value(candidate):
            raw_start, raw_end = match.span("value")
            leading = len(match.group("value")) - len(match.group("value").lstrip())
            trailing = len(match.group("value")) - len(match.group("value").rstrip())
            candidates.append(
                _SecretSpan(
                    kind="credential-literal-assignment",
                    start=raw_start + leading,
                    end=raw_end - trailing,
                )
            )

    accepted: list[_SecretSpan] = []
    for candidate in sorted(candidates, key=lambda span: (span.start, -(span.end - span.start))):
        if candidate.start == candidate.end:
            continue
        if any(
            candidate.start < existing.end and existing.start < candidate.end
            for existing in accepted
        ):
            continue
        accepted.append(candidate)
    return tuple(sorted(accepted, key=lambda span: (span.start, span.end, span.kind)))


def _is_literal_credential_value(value: str) -> bool:
    normalized = value.strip()
    if normalized.casefold() in _SAFE_SENTINELS:
        return False
    if _PLACEHOLDER_VALUE.fullmatch(normalized) is not None:
        return False
    if _REFERENCE_VALUE.fullmatch(normalized) is not None:
        return False
    return bool(normalized)


def _is_authorization_reference(value: str) -> bool:
    normalized = _normalize_bare_credential_rhs(value)
    if normalized.casefold() in _SAFE_SENTINELS:
        return True
    if _PLACEHOLDER_VALUE.fullmatch(normalized) is not None:
        return True
    if _REQUEST_SEMANTIC_REFERENCE.fullmatch(normalized) is not None:
        return True
    if any(character in normalized for character in ".(["):
        return _REFERENCE_VALUE.fullmatch(normalized) is not None
    return bool(
        re.fullmatch(
            r"(?i)[A-Za-z_$][A-Za-z0-9_$]*"
            r"(?:token|credential|authorization|value|ref|reference|variable)",
            normalized,
        )
        or normalized.casefold() in {"token", "credential", "authorization"}
    )


def _is_safe_request_credential_rhs(value: str, *, quoted: bool) -> bool:
    normalized = value.strip()
    if not quoted:
        normalized = _normalize_bare_credential_rhs(normalized)
    if normalized.casefold() in _SAFE_SENTINELS:
        return True
    if _PLACEHOLDER_VALUE.fullmatch(normalized) is not None:
        return True
    if quoted:
        return False
    normalized = _unwrap_one_request_parenthesized_reference(normalized)
    return any(
        pattern.fullmatch(normalized) is not None
        for pattern in (
            _REQUEST_PROPERTY_REFERENCE,
            _REQUEST_SEMANTIC_REFERENCE,
            _REQUEST_LOOKUP_CALL,
        )
    )


def _normalize_bare_credential_rhs(value: str) -> str:
    """Remove prose delimiters that are not part of an unquoted expression."""

    normalized = value.strip()
    while normalized.endswith((".", "!", "?", "`")):
        normalized = normalized[:-1].rstrip()
    return normalized


def _unwrap_one_request_parenthesized_reference(value: str) -> str:
    """Allow one balanced prose/code grouping around an otherwise safe reference."""

    if not value.startswith("("):
        return value
    end = _read_balanced_request_parentheses(value, 0)
    if end != len(value):
        return value
    return value[1:-1].strip()


def _iter_text_values(value: object) -> Iterable[str]:
    if isinstance(value, BaseModel):
        yield from _iter_text_values(value.model_dump(mode="python"))
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, (bytes, bytearray)):
        yield bytes(value).decode("utf-8", errors="ignore")
        return
    if isinstance(value, BaseException):
        yield from _iter_text_values(value.args)
        return
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _iter_text_values(child)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for child in value:
            yield from _iter_text_values(child)
        return
    if isinstance(value, (set, frozenset)):
        for child in value:
            yield from _iter_text_values(child)
        return
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            yield from _iter_text_values(getattr(value, field.name))


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
            redacted, count = pattern.subn(partial(_replacement, kind), redacted)
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
