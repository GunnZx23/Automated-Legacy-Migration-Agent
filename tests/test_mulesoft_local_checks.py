from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mulesoft_candidate_factory import build_mulesoft_candidate

from legacy_migration_agent.platforms.mulesoft_local_checks import (
    DW1,
    MULE4_APP,
    MULE4_ARTIFACT,
    MULE4_DATAWEAVE,
    MULE4_POM,
    MULE4_TEST,
    MuleSoftLocalCheckCode,
    MuleSoftLocalCheckFailure,
    check_mulesoft_candidate,
)

REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE_INPUT = REPOSITORY / "fixtures/mulesoft/customer-status-api/input"


def _candidate_roots(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    candidate = tmp_path / "candidate"
    shutil.copytree(FIXTURE_INPUT, source)
    build_mulesoft_candidate(source, candidate)
    return source, candidate


def _replace(path: Path, old: str, new: str) -> None:
    value = path.read_text(encoding="utf-8")
    assert old in value
    path.write_text(value.replace(old, new, 1), encoding="utf-8")


def _expect_failure(
    source: Path,
    candidate: Path,
    code: MuleSoftLocalCheckCode,
) -> MuleSoftLocalCheckFailure:
    with pytest.raises(MuleSoftLocalCheckFailure) as caught:
        check_mulesoft_candidate(candidate, source)
    assert caught.value.code is code
    return caught.value


def test_pom_accepts_candidate_owned_direct_standalone_project_coordinates(
    tmp_path: Path,
) -> None:
    source, candidate = _candidate_roots(tmp_path)
    pom = candidate / MULE4_POM
    _replace(pom, "<groupId>example.synthetic</groupId>", "<groupId>org.example.capstone</groupId>")
    _replace(
        pom,
        "<artifactId>customer-status-api-mule4</artifactId>",
        "<artifactId>bounded-status-migration</artifactId>",
    )
    _replace(pom, "<version>1.0.0-SNAPSHOT</version>", "<version>2.1.0-RC1</version>")

    assert check_mulesoft_candidate(candidate, source).passed is True


@pytest.mark.parametrize(
    "indirection",
    (
        "parent",
        "dependency-management",
        "plugin-management",
        "profile",
        "duplicate-model-version",
        "duplicate-group-id",
        "duplicate-artifact-id",
        "duplicate-project-version",
        "property-project-version",
    ),
)
def test_pom_rejects_inherited_managed_profiled_or_ambiguous_project_model(
    tmp_path: Path,
    indirection: str,
) -> None:
    source, candidate = _candidate_roots(tmp_path)
    pom = candidate / MULE4_POM
    value = pom.read_text(encoding="utf-8")
    if indirection == "parent":
        value = value.replace(
            "    <groupId>example.synthetic</groupId>\n",
            "",
            1,
        ).replace(
            "    <modelVersion>4.0.0</modelVersion>",
            """    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.example.parent</groupId>
        <artifactId>managed-parent</artifactId>
        <version>1.0.0</version>
    </parent>""",
            1,
        )
    elif indirection == "dependency-management":
        value = value.replace(
            "    <dependencies>",
            """    <dependencyManagement>
        <dependencies>
            <dependency>
                <groupId>org.mule.connectors</groupId>
                <artifactId>mule-http-connector</artifactId>
                <version>1.12.0</version>
            </dependency>
        </dependencies>
    </dependencyManagement>

    <dependencies>""",
            1,
        )
    elif indirection == "plugin-management":
        value = value.replace(
            "        <plugins>",
            """        <pluginManagement>
            <plugins/>
        </pluginManagement>
        <plugins>""",
            1,
        )
    elif indirection == "profile":
        value = value.replace(
            "</project>",
            """    <profiles>
        <profile>
            <id>candidate-managed-build</id>
        </profile>
    </profiles>
</project>""",
            1,
        )
    elif indirection == "property-project-version":
        value = value.replace(
            "<version>1.0.0-SNAPSHOT</version>",
            "<version>${revision}</version>",
            1,
        )
    else:
        element = {
            "duplicate-model-version": "<modelVersion>4.0.0</modelVersion>",
            "duplicate-group-id": "<groupId>example.synthetic</groupId>",
            "duplicate-artifact-id": "<artifactId>customer-status-api-mule4</artifactId>",
            "duplicate-project-version": "<version>1.0.0-SNAPSHOT</version>",
        }[indirection]
        value = value.replace(element, f"{element}\n    {element}", 1)
    pom.write_text(value, encoding="utf-8")

    failure = _expect_failure(source, candidate, MuleSoftLocalCheckCode.POM_CONTRACT)

    assert failure.artifact == MULE4_POM


def test_dataweave_accepts_typed_uri_params_without_a_controller_named_variable(
    tmp_path: Path,
) -> None:
    source, candidate = _candidate_roots(tmp_path)
    app = candidate / MULE4_APP
    _replace(
        app,
        """        <set-variable variableName="customerId"
                      value="#[attributes.uriParams.customerId as String]"/>
""",
        "",
    )
    _replace(
        candidate / MULE4_DATAWEAVE,
        "customerId: vars.customerId",
        'customerId: attributes["uriParams"]["customerId"] as String',
    )

    assert check_mulesoft_candidate(candidate, source).passed is True


@pytest.mark.parametrize(
    "legacy_fragment",
    (
        '<logger legacyValue="#[flowVars.customerId]"/>',
        "<logger><![CDATA[#[sessionVars.customerId]]]></logger>",
        '<flow-ref name="build-customer-status-response"/>#[inboundProperties.customerId]',
        "<logger><![CDATA[%dw 1.0\n%output application/json\n---\n{}]]></logger>",
        f'<dw:transform-message xmlns:dw="{DW1}"/>',
    ),
)
def test_mule4_xml_rejects_mule3_or_dataweave1_in_attributes_text_and_tails(
    tmp_path: Path,
    legacy_fragment: str,
) -> None:
    source, candidate = _candidate_roots(tmp_path)
    app = candidate / MULE4_APP
    value = app.read_text(encoding="utf-8")
    if legacy_fragment.startswith('<flow-ref name="build'):
        value = value.replace(
            '<flow-ref name="build-customer-status-response"/>',
            legacy_fragment,
            1,
        )
    else:
        value = value.replace(
            '<flow-ref name="build-customer-status-response"/>',
            legacy_fragment + '\n        <flow-ref name="build-customer-status-response"/>',
            1,
        )
    app.write_text(value, encoding="utf-8")

    failure = _expect_failure(source, candidate, MuleSoftLocalCheckCode.MULE4_CONTRACT)

    assert failure.artifact == MULE4_APP


def test_candidate_munit_xml_rejects_embedded_mule3_expression_text(tmp_path: Path) -> None:
    source, candidate = _candidate_roots(tmp_path)
    _replace(
        candidate / MULE4_TEST,
        "Builds a synthetic ACTIVE customer response",
        "#[outboundProperties.status]",
    )

    failure = _expect_failure(source, candidate, MuleSoftLocalCheckCode.MUNIT_CONTRACT)

    assert failure.artifact == MULE4_TEST


def test_legacy_term_in_plain_xml_prose_is_not_misclassified_as_an_expression(
    tmp_path: Path,
) -> None:
    source, candidate = _candidate_roots(tmp_path)
    _replace(
        candidate / MULE4_APP,
        '<flow-ref name="build-customer-status-response"/>',
        '<logger message="flowVars were migrated to a typed Mule 4 value"/>\n'
        '        <flow-ref name="build-customer-status-response"/>',
    )

    assert check_mulesoft_candidate(candidate, source).passed is True


@pytest.mark.parametrize(
    "invalid_json",
    (
        "duplicate",
        "NaN",
        "Infinity",
        "-Infinity",
    ),
)
def test_mule_artifact_json_rejects_duplicates_and_nonfinite_numbers(
    tmp_path: Path,
    invalid_json: str,
) -> None:
    source, candidate = _candidate_roots(tmp_path)
    artifact = candidate / MULE4_ARTIFACT
    value = artifact.read_text(encoding="utf-8")
    if invalid_json == "duplicate":
        value = value.replace(
            '"minMuleVersion": "4.9.20",',
            '"minMuleVersion": "4.9.20",\n  "minMuleVersion": "4.9.20",',
            1,
        )
    else:
        value = value.replace("{", f'{{\n  "nonfiniteMetadata": {invalid_json},', 1)
    artifact.write_text(value, encoding="utf-8")

    failure = _expect_failure(source, candidate, MuleSoftLocalCheckCode.MALFORMED_JSON)

    assert failure.artifact == MULE4_ARTIFACT
