from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from legacy_migration_agent.core.workspace import content_revision
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    CANDIDATE_FILES,
    MULE3_APP,
    MULE4_APP,
    MULE4_ARTIFACT,
    MULE4_DATAWEAVE,
    MULE4_POM,
    MULE4_PROPERTIES,
    MULE4_TEST,
    SOURCE_FILES,
    MuleSoftLocalCheckCode,
    MuleSoftLocalCheckFailure,
    check_mulesoft_candidate,
    main,
)

ROOT = Path(__file__).parents[1] / "fixtures" / "mulesoft" / "customer-status-api"
INPUT = ROOT / "input"
EXPECTED = ROOT / "expected"


def load_contract() -> dict:
    return yaml.safe_load((ROOT / "fixture.yaml").read_text(encoding="utf-8"))


def relative_files(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    )


def isolated_roots(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source-tree"
    candidate = tmp_path / "candidate-tree"
    shutil.copytree(INPUT, source)
    shutil.copytree(EXPECTED, candidate)
    assert "expected" not in source.parts
    assert "expected" not in candidate.parts
    return source, candidate


def replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def expect_failure(
    candidate: Path,
    source: Path,
    code: MuleSoftLocalCheckCode,
) -> MuleSoftLocalCheckFailure:
    with pytest.raises(MuleSoftLocalCheckFailure) as caught:
        check_mulesoft_candidate(candidate, source)
    assert caught.value.code is code
    assert str(candidate) not in str(caught.value)
    assert str(source) not in str(caught.value)
    return caught.value


def test_contract_declares_public_additive_scope_and_exact_inventory() -> None:
    contract = load_contract()
    assert contract["platform"] == "mulesoft"
    assert contract["public_synthetic"] is True
    assert contract["migration_kind"] == "mule3_to_mule4_side_by_side"
    assert contract["source"]["runtime"] == "3.9.5"
    assert contract["source"]["java"] == "8"
    assert contract["source"]["dataweave"] == "1.0"
    assert contract["target"]["runtime"] == "4.9.20"
    assert contract["target"]["release_channel"] == "LTS"
    assert contract["target"]["java"] == "17"
    assert contract["target"]["dataweave"] == "2.0"
    assert contract["target"]["munit"] == "3.7.3"

    assert relative_files(INPUT) == SOURCE_FILES
    assert relative_files(EXPECTED) == CANDIDATE_FILES
    assert tuple(sorted(contract["expected"]["preserved_files"])) == SOURCE_FILES
    assert tuple(sorted(contract["expected"]["added_files"])) == tuple(sorted(CANDIDATE_FILES[3:]))
    assert contract["expected"]["modified_files"] == []
    assert contract["expected"]["deleted_files"] == []


def test_static_candidate_contract_accepts_the_reviewed_oracle_without_runtime_claim() -> None:
    result = check_mulesoft_candidate(EXPECTED, INPUT)

    assert result.passed is True
    assert result.inventory_files == 9
    assert result.preserved_source_files == 3
    assert result.mule3_runtime == "3.9.5"
    assert result.mule4_runtime == "4.9.20"
    assert result.java == "17"
    assert result.dataweave == "2.0"
    assert result.munit == "3.7.3"
    assert result.maven_executed is False
    assert result.munit_executed is False
    assert result.deployment_claim is False


def test_static_candidate_contract_runs_without_an_expected_directory(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)

    result = check_mulesoft_candidate(candidate, source)

    assert result.source_revision == content_revision(source)
    assert result.candidate_revision == content_revision(candidate)
    assert not any(path.name == "expected" for path in tmp_path.rglob("*"))


def test_module_cli_uses_current_candidate_and_explicit_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, candidate = isolated_roots(tmp_path)
    monkeypatch.chdir(candidate)

    assert main(["--source-root", str(source)]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["passed"] is True
    assert result["maven_executed"] is False
    assert result["munit_executed"] is False
    assert result["deployment_claim"] is False


def test_module_cli_failure_is_bounded_and_does_not_echo_secret_or_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, candidate = isolated_roots(tmp_path)
    secret = "cli-secret-must-not-appear"
    path = candidate / MULE4_DATAWEAVE
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n// access_token={secret}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(candidate)

    assert main(["--source-root", str(source)]) == 1

    captured = capsys.readouterr()
    failure = json.loads(captured.err)
    assert captured.out == ""
    assert failure["code"] == "secret_material"
    assert failure["passed"] is False
    assert failure["maven_executed"] is False
    assert failure["munit_executed"] is False
    assert failure["deployment_claim"] is False
    assert secret not in captured.err
    assert str(tmp_path) not in captured.err


def test_missing_inventory_fails_closed(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    (candidate / MULE4_DATAWEAVE).unlink()

    expect_failure(candidate, source, MuleSoftLocalCheckCode.INVENTORY_MISMATCH)


def test_legacy_mule3_byte_drift_fails_before_domain_validation(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    (candidate / MULE3_APP).write_text("changed\n", encoding="utf-8")

    expect_failure(candidate, source, MuleSoftLocalCheckCode.SOURCE_DRIFT)


def test_unsafe_root_or_internal_symlink_fails_closed(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    target = candidate / MULE4_DATAWEAVE
    target.unlink()
    target.symlink_to(source / MULE3_APP)

    expect_failure(candidate, source, MuleSoftLocalCheckCode.UNSAFE_TREE)

    alias = tmp_path / "candidate-alias"
    alias.symlink_to(candidate, target_is_directory=True)
    expect_failure(alias, source, MuleSoftLocalCheckCode.UNSAFE_TREE)


@pytest.mark.parametrize("declaration", ("<!DOCTYPE mule []>", "<!ENTITY secret 'x'>"))
def test_dtd_and_entity_declarations_fail_before_xml_parsing(
    tmp_path: Path,
    declaration: str,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    path = candidate / MULE4_APP
    path.write_text(declaration + path.read_text(encoding="utf-8"), encoding="utf-8")

    expect_failure(candidate, source, MuleSoftLocalCheckCode.UNSAFE_XML)


def test_malformed_xml_yaml_and_json_have_controlled_failure_codes(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path / "xml")
    (candidate / MULE4_APP).write_text("<mule>", encoding="utf-8")
    expect_failure(candidate, source, MuleSoftLocalCheckCode.MALFORMED_XML)

    source, candidate = isolated_roots(tmp_path / "yaml")
    (candidate / MULE4_PROPERTIES).write_text("http: [\n", encoding="utf-8")
    expect_failure(candidate, source, MuleSoftLocalCheckCode.MALFORMED_YAML)

    source, candidate = isolated_roots(tmp_path / "json")
    (candidate / MULE4_ARTIFACT).write_text("{", encoding="utf-8")
    expect_failure(candidate, source, MuleSoftLocalCheckCode.MALFORMED_JSON)


def test_mule3_contract_is_checked_independently_of_byte_preservation(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    for root in (source, candidate):
        replace(root / MULE3_APP, "%dw 1.0", "%dw 2.0")

    expect_failure(candidate, source, MuleSoftLocalCheckCode.MULE3_CONTRACT)


def test_mule4_and_dataweave_contract_regressions_fail_closed(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path / "mule")
    replace(
        candidate / MULE4_APP,
        "attributes.uriParams.customerId as String",
        "inboundProperties.customerId",
    )
    expect_failure(candidate, source, MuleSoftLocalCheckCode.MULE4_CONTRACT)

    source, candidate = isolated_roots(tmp_path / "dataweave")
    replace(candidate / MULE4_DATAWEAVE, "%dw 2.0", "%dw 1.0")
    expect_failure(candidate, source, MuleSoftLocalCheckCode.DATAWEAVE_CONTRACT)


def test_munit_pom_artifact_and_version_regressions_fail_closed(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path / "munit")
    replace(candidate / MULE4_TEST, "MunitTools::equalTo", "MunitTools::notEqualTo")
    expect_failure(candidate, source, MuleSoftLocalCheckCode.MUNIT_CONTRACT)

    source, candidate = isolated_roots(tmp_path / "pom")
    replace(candidate / MULE4_POM, "<app.runtime>4.9.20", "<app.runtime>4.9.19")
    expect_failure(candidate, source, MuleSoftLocalCheckCode.VERSION_MISMATCH)

    source, candidate = isolated_roots(tmp_path / "artifact")
    artifact = json.loads((candidate / MULE4_ARTIFACT).read_text(encoding="utf-8"))
    artifact["requiredProduct"] = "MULE"
    (candidate / MULE4_ARTIFACT).write_text(json.dumps(artifact), encoding="utf-8")
    expect_failure(candidate, source, MuleSoftLocalCheckCode.ARTIFACT_CONTRACT)


@pytest.mark.parametrize(
    ("original", "adversarial"),
    (
        (
            "MunitTools::equalTo(&quot;ACTIVE&quot;)",
            "MunitTools::equalTo(&quot;INACTIVE&quot;)",
        ),
        (
            "MunitTools::equalTo(&quot;ACTIVE&quot;)",
            "MunitTools::equalTo(payload.status)",
        ),
        (
            '<flow-ref name="build-customer-status-response"/>',
            '<set-payload value="#[{customerId: vars.customerId, status: '
            '&quot;ACTIVE&quot;, source: &quot;synthetic-fixture&quot;}]"/>',
        ),
    ),
)
def test_candidate_munit_cannot_change_expected_values_or_trivialize_execution(
    tmp_path: Path,
    original: str,
    adversarial: str,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    replace(candidate / MULE4_TEST, original, adversarial)

    expect_failure(candidate, source, MuleSoftLocalCheckCode.MUNIT_CONTRACT)


def test_secret_and_outbound_connector_failures_are_sanitized(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path / "secret")
    secret = "do-not-expose-this-value"
    path = candidate / MULE4_DATAWEAVE
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n// client_secret={secret}\n",
        encoding="utf-8",
    )
    failure = expect_failure(candidate, source, MuleSoftLocalCheckCode.SECRET_MATERIAL)
    assert secret not in str(failure)

    source, candidate = isolated_roots(tmp_path / "outbound")
    replace(
        candidate / MULE4_APP,
        '<flow-ref name="build-customer-status-response"/>',
        '<flow-ref name="build-customer-status-response"/>\n'
        '        <http:request method="GET" url="https://example.invalid"/>',
    )
    expect_failure(candidate, source, MuleSoftLocalCheckCode.OUTBOUND_CONNECTOR)


def test_contract_uses_only_official_mulesoft_documentation_references() -> None:
    references = load_contract()["official_references"]
    assert len(references) >= 5
    assert all(reference.startswith("https://docs.mulesoft.com/") for reference in references)
