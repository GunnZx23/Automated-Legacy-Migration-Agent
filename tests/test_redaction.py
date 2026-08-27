from pathlib import Path

import pytest

from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.redaction import (
    REDACTED,
    SecretRedactor,
    assert_no_high_confidence_secrets,
    assert_no_request_secrets,
    high_confidence_secret_findings,
    redact_high_confidence_secrets,
)


@pytest.mark.parametrize(
    "source",
    (
        "const token = ++this.requestGeneration;",
        "const token = (++this.requestGeneration);",
        "access_token = response.accessToken",
        'authToken = response["token"]',
        "password = config['password']",
        'client_secret = os.getenv("CLIENT_SECRET")',
        'access_token = config.get("auth.token")',
        "password = getPassword()",
        "token=requestGeneration_2026",
        "token=current_request_2",
        "token=requestToken2",
        "token=requestGeneration",
        "token=currentRequest",
        "token=requestToken",
        "Use `token=requestToken` in the stale-response guard.",
        "Authorization: Bearer requestToken",
        "Authorization: Bearer requestToken.",
        "Authorization: Bearer response.accessToken",
        "Authorization: Bearer getToken()",
        "password=${secure::database-password}",
        'password="${secure::database-password}"',
        "token=[REDACTED]",
        "token=<secret-ref>.",
        'password="changeme"',
        "if (token == randomIdentifier) continue;",
        "const mapper = token => next(token);",
        "Use a request token for stale-response protection.",
    ),
)
def test_request_secret_boundary_allows_only_nonliteral_credential_references(
    source: str,
) -> None:
    assert_no_request_secrets({"content": source})


@pytest.mark.parametrize(
    "source",
    (
        "Authorization: Bearer actual-token-value-123456",
        "Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ1Ng==",
        '"Authorization": "Bearer literal-token-value-123456"',
        "token/*comment*/=abcdefghijklmnop123456;",
        "token /* comment */ = abcdefghijklmnop123456;",
        "token=(++this.requestGeneration)abcdefghijklmnop123456;",
        '"password" /* retained locally */ : "literal-password-123456"',
        'password="response.password"',
        "token='requestToken2'",
        "password=hunter2",
        "token=randomIdentifier",
        "token=abcdefghijklmnop123456",
        "access_token=literalAccessToken123456",
        "authToken=resolveToken()",
        "password=loadPassword()",
        "credential=lookup(anything)",
        "github_pat_11AA0_this_is_a_long_fine_grained_token_value",
    ),
)
def test_request_secret_boundary_rejects_literal_and_ambiguous_credential_rhs(
    source: str,
) -> None:
    with pytest.raises(PolicyViolation, match="forbidden secret-shaped material") as caught:
        assert_no_request_secrets({"content": source})
    assert source not in str(caught.value)


def test_redacts_supported_credentials_without_echoing_values():
    explicit = "fixture-secret-123456"
    source = "\n".join(
        (
            "Authorization: Bearer token-value-123456",
            "SFDX_AUTH_URL=force://client:refresh-token@login.example.test",
            "client_secret='client-secret-value'",
            "ghp_abcdefghijklmnopqrstuvwxyz1234567890AB",
            "github_pat_11AA0_this_is_a_long_fine_grained_token_value",
            "xoxb" + "-123456789012-abcdefghijklmnop",
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_value_12345",
            "token=generic-token-value-123456",
            explicit,
        )
    )

    result = SecretRedactor((explicit,)).redact(source)

    assert result.changed is True
    assert result.text.count(REDACTED) == 10
    for secret_fragment in (
        "token-value-123456",
        "refresh-token",
        "client-secret-value",
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890AB",
        "github_pat_11AA0_this_is_a_long_fine_grained_token_value",
        "xoxb" + "-123456789012-abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9",
        "generic-token-value-123456",
        explicit,
    ):
        assert secret_fragment not in result.text


def test_does_not_redact_normal_salesforce_source():
    source = "public with sharing class Example { @AuraEnabled public static void run() {} }"
    result = SecretRedactor().redact(source)
    assert result.text == source
    assert result.findings == ()


@pytest.mark.parametrize(
    "source",
    (
        "const token = ++this.requestGeneration;",
        "if (token !== this.requestGeneration) return;",
        "const accessToken = response.accessToken;",
        "const access_token = response.token;",
        'client_secret = os.getenv("CLIENT_SECRET")',
        "password = getPassword()",
        "password: ${secure::database-password}",
        'password: "${secure::database-password}"',
        "password: #[p('secure::database-password')]",
        "Authorization: Bearer ${secure::token}",
        "Authorization: Bearer ${accessToken}",
        "Authorization: Bearer token",
        # Ambiguity policy: a language-agnostic scanner treats every valid bare
        # identifier as a reference. Structural credential fields remain
        # fail-closed at the run-session boundary.
        "password=hunter2",
        "Use a request token for stale-response protection.",
    ),
)
def test_high_confidence_detector_allows_references_and_placeholders(source: str) -> None:
    assert high_confidence_secret_findings(source) == ()
    assert redact_high_confidence_secrets(source).text == source
    assert_no_high_confidence_secrets({"content": source}, boundary="candidate")


@pytest.mark.parametrize(
    "source",
    (
        "Authorization: Bearer actual-token-value-123456",
        "Authorization: Bearer actualtokensecret123456",
        "Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ1Ng==",
        "SFDX_AUTH_URL=force://client:refresh-value@login.example.test",
        'client_secret = "literal-client-secret-123456"',
        "access_token=literal-access-token-123456",
        '"password": "literal-password-123456"',
        "<password>literal-password-123456</password>",
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890AB",
        "github_pat_11AA0_this_is_a_long_fine_grained_token_value",
        "xoxb" + "-123456789012-abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
        "sk-secret-key-123456789",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_value_12345",
        "-----BEGIN PRIVATE KEY-----\nsecret material\n-----END PRIVATE KEY-----",
    ),
)
def test_high_confidence_detector_rejects_literal_secret_shapes(source: str) -> None:
    findings = high_confidence_secret_findings(source)
    assert findings

    redacted = redact_high_confidence_secrets(source)
    assert redacted.changed is True
    assert REDACTED in redacted.text

    with pytest.raises(PolicyViolation, match="forbidden secret-shaped material") as caught:
        assert_no_high_confidence_secrets({"content": source}, boundary="candidate")
    assert source not in str(caught.value)


def test_file_scan_reports_location_without_secret_content(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "config.txt").write_text(
        "safe=true\naccess_token=token-value-123456\n",
        encoding="utf-8",
    )
    findings = SecretRedactor().scan_files(repository, ("config.txt",))
    assert len(findings) == 1
    assert findings[0].path == "config.txt"
    assert findings[0].line == 2
    assert "token-value" not in findings[0].model_dump_json()


def test_file_scan_rejects_binary_artifacts(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "binary.bin").write_bytes(b"safe\x00secret")
    with pytest.raises(PolicyViolation, match="binary"):
        SecretRedactor().scan_files(repository, ("binary.bin",))
