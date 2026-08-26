from pathlib import Path

import pytest

from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.redaction import REDACTED, SecretRedactor


def test_redacts_supported_credentials_without_echoing_values():
    explicit = "fixture-secret-123456"
    source = "\n".join(
        (
            "Authorization: Bearer token-value-123456",
            "SFDX_AUTH_URL=force://client:refresh-token@login.example.test",
            "client_secret='client-secret-value'",
            explicit,
        )
    )

    result = SecretRedactor((explicit,)).redact(source)

    assert result.changed is True
    assert result.text.count(REDACTED) == 4
    for secret_fragment in (
        "token-value-123456",
        "refresh-token",
        "client-secret-value",
        explicit,
    ):
        assert secret_fragment not in result.text


def test_does_not_redact_normal_salesforce_source():
    source = "public with sharing class Example { @AuraEnabled public static void run() {} }"
    result = SecretRedactor().redact(source)
    assert result.text == source
    assert result.findings == ()


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
