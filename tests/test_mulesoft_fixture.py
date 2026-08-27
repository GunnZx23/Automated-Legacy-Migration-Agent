from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from mulesoft_candidate_factory import build_mulesoft_candidate, mulesoft_target_outputs

from legacy_migration_agent.core.workspace import content_revision
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    CANDIDATE_FILES,
    MAX_MUNIT_RESPONSE_TIMEOUT_MS,
    MAX_YAML_DEPTH,
    MAX_YAML_ENTRIES,
    MAX_YAML_KEY_LENGTH,
    MAX_YAML_PATH_LENGTH,
    MULE3_APP,
    MULE4_APP,
    MULE4_ARTIFACT,
    MULE4_DATAWEAVE,
    MULE4_POM,
    MULE4_PROPERTIES,
    MULE4_TEST,
    MULESOFT_IMPLEMENTATION_CONTRACT,
    SOURCE_FILES,
    STATIC_CHECKS,
    MuleSoftLocalCheckCode,
    MuleSoftLocalCheckFailure,
    _flatten_configuration,
    check_mulesoft_candidate,
    main,
)

ROOT = Path(__file__).parents[1] / "fixtures" / "mulesoft" / "customer-status-api"
INPUT = ROOT / "input"


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
    build_mulesoft_candidate(source, candidate)
    return source, candidate


def replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def write_alternate_candidate(candidate: Path) -> None:
    (candidate / MULE4_APP).write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<mule xmlns="http://www.mulesoft.org/schema/mule/core"
      xmlns:ee="http://www.mulesoft.org/schema/mule/ee/core"
      xmlns:http="http://www.mulesoft.org/schema/mule/http">
    <configuration-properties file="application.yaml"/>
    <http:listener-config name="loopback-customer-api" basePath="/api/">
        <http:listener-connection host="#[p('http.host')]" port="8081"/>
    </http:listener-config>
    <sub-flow name="compose-generated-status">
        <ee:transform>
            <ee:message>
                <ee:set-payload resource="dw/customer-status-response.dwl"/>
            </ee:message>
        </ee:transform>
    </sub-flow>
    <sub-flow name="candidate-helper">
        <logger message="candidate helper"/>
    </sub-flow>
    <flow name="serve-generated-status">
        <http:listener config-ref="loopback-customer-api"
                       allowedMethods=" get "
                       path="/customers/{customerId}/status"/>
        <set-variable variableName="requestedCustomer"
                      value="#[attributes.uriParams.customerId]"/>
        <flow-ref name="compose-generated-status"/>
    </flow>
</mule>
""",
        encoding="utf-8",
    )
    (candidate / MULE4_DATAWEAVE).write_text(
        """%dw 2.0
output application/json skipNullOn="everywhere"
var requested = vars.requestedCustomer
---
{
  source: "synthetic-" ++ "fixture",
  customerId: requested,
  status: ["ACTIVE"][0]
}
""",
        encoding="utf-8",
    )
    (candidate / MULE4_TEST).write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<mule xmlns="http://www.mulesoft.org/schema/mule/core"
      xmlns:munit="http://www.mulesoft.org/schema/mule/munit"
      xmlns:munit-tools="http://www.mulesoft.org/schema/mule/munit-tools">
    <munit:config name="engineer-generated-suite"/>
    <munit:test name="source-can-be-derived" description="candidate-authored provenance check">
        <munit:behavior>
            <munit:set-event>
                <munit:variables>
                    <munit:variable key="requestedCustomer" value='#["ALT-200"]'/>
                </munit:variables>
            </munit:set-event>
        </munit:behavior>
        <munit:execution>
            <flow-ref name="compose-generated-status"/>
        </munit:execution>
        <munit:validation>
            <munit-tools:assert-that expression="#[payload.source]"
                                     is="#[MunitTools::notNullValue()]"
                                     message="generated source is present"/>
            <munit-tools:assert-that expression="#[payload.customerId]"
                                     is='#[MunitTools::equalTo("ALT-200")]'/>
        </munit:validation>
    </munit:test>
    <munit:test name="status-is-produced">
        <munit:execution>
            <flow-ref name="compose-generated-status"/>
        </munit:execution>
        <munit:validation>
            <munit-tools:assert-that expression="#[payload.status]"
                                     is='#[MunitTools::equalTo("ACTIVE")]'
                                     message="candidate status assertion"/>
        </munit:validation>
    </munit:test>
</mule>
""",
        encoding="utf-8",
    )
    pom_path = candidate / MULE4_POM
    pom = pom_path.read_text(encoding="utf-8")
    for property_reference, pinned_value in (
        ("${app.runtime}", "4.9.20"),
        ("${mule.maven.plugin.version}", "4.10.1"),
        ("${munit.version}", "3.7.3"),
        ("${http.connector.version}", "1.12.0"),
    ):
        pom = pom.replace(property_reference, pinned_value)
    pom_path.write_text(pom, encoding="utf-8")


def write_loopback_http_munit(
    candidate: Path,
    *,
    host: str = "127.0.0.1",
    port: str = "8081",
    protocol: str | None = None,
    method: str = "GET",
    path: str = "/api/customers/CANDIDATE-200/status",
    config_base_path: str | None = None,
    config_response_timeout: str | None = None,
    response_timeout: str | None = None,
    target: str | None = None,
    target_value: str | None = None,
    extra_request_attribute: str | None = None,
) -> None:
    config_attributes = "".join(
        attribute
        for attribute in (
            f' basePath="{config_base_path}"' if config_base_path is not None else "",
            (
                f' responseTimeout="{config_response_timeout}"'
                if config_response_timeout is not None
                else ""
            ),
        )
    )
    connection_attributes = f' protocol="{protocol}"' if protocol is not None else ""
    request_attributes = "".join(
        attribute
        for attribute in (
            f' responseTimeout="{response_timeout}"' if response_timeout is not None else "",
            f' target="{target}"' if target is not None else "",
            f' targetValue="{target_value}"' if target_value is not None else "",
            f" {extra_request_attribute}" if extra_request_attribute is not None else "",
        )
    )
    assertion_expression = f"#[vars.{target}.status]" if target is not None else "#[payload.status]"
    (candidate / MULE4_TEST).write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<mule xmlns="http://www.mulesoft.org/schema/mule/core"
      xmlns:http="http://www.mulesoft.org/schema/mule/http"
      xmlns:munit="http://www.mulesoft.org/schema/mule/munit"
      xmlns:munit-tools="http://www.mulesoft.org/schema/mule/munit-tools">
    <http:request-config name="candidate-loopback-request"{config_attributes}>
        <http:request-connection host="{host}" port="{port}"{connection_attributes}/>
    </http:request-config>
    <munit:config name="candidate-http-suite"/>
    <munit:test name="public-status-is-observable">
        <munit:enable-flow-sources>
            <munit:enable-flow-source value="customer-status-api-flow"/>
        </munit:enable-flow-sources>
        <munit:execution>
            <http:request method="{method}"
                          path="{path}"
                          config-ref="candidate-loopback-request"{request_attributes}/>
        </munit:execution>
        <munit:validation>
            <munit-tools:assert-that expression="{assertion_expression}"
                                     is='#[MunitTools::equalTo("ACTIVE")]'/>
        </munit:validation>
    </munit:test>
</mule>
""",
        encoding="utf-8",
    )


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


def test_contract_declares_a_source_only_public_migration_fixture() -> None:
    contract = load_contract()
    assert contract["platform"] == "mulesoft"
    assert contract["public_synthetic"] is True
    assert contract["migration_kind"] == "mule3_to_mule4_side_by_side"
    assert contract["source"]["runtime"] == "3.9.5"
    assert contract["source"]["java"] == "8"
    assert contract["source"]["dataweave"] == "1.0"
    assert contract["fixture_scope"] == {
        "source_only": True,
        "shipped_tree": "input",
        "candidate_tree": "not_shipped",
        "candidate_origin": "agent_generated_in_isolated_workspace",
    }
    assert "target" not in contract
    assert "expected" not in contract

    assert relative_files(INPUT) == SOURCE_FILES
    assert tuple(sorted((*SOURCE_FILES, *mulesoft_target_outputs()))) == CANDIDATE_FILES
    assert tuple(contract["validation"]["local_static"]) == STATIC_CHECKS

    implementation_contract = "\n".join(MULESOFT_IMPLEMENTATION_CONTRACT)
    assert "customerId echoes the path value" in implementation_contract
    assert "status is ACTIVE" in implementation_contract
    assert "source is synthetic-fixture" in implementation_contract
    assert "topology and expression spelling are implementation choices" in implementation_contract


def test_static_candidate_contract_accepts_a_test_built_candidate_without_runtime_claim(
    tmp_path: Path,
) -> None:
    source, candidate = isolated_roots(tmp_path)

    result = check_mulesoft_candidate(candidate, source)

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


def test_static_contract_accepts_alternate_names_topology_dataweave_and_tests(
    tmp_path: Path,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    write_alternate_candidate(candidate)

    result = check_mulesoft_candidate(candidate, source)

    assert result.passed is True
    assert result.munit_executed is False
    assert result.deployment_claim is False


def test_static_contract_accepts_equivalent_effective_listener_route_decomposition(
    tmp_path: Path,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    app_path = candidate / MULE4_APP
    replace(app_path, 'basePath="/api"', 'basePath="/"')
    replace(
        app_path,
        'path="/customers/{customerId}/status"',
        'path="/api/customers/{customerId}/status"',
    )

    assert check_mulesoft_candidate(candidate, source).passed is True


def test_candidate_munit_may_exercise_the_fixed_loopback_http_route(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    write_loopback_http_munit(candidate)

    result = check_mulesoft_candidate(candidate, source)

    assert result.passed is True
    assert result.munit_executed is False


def test_candidate_munit_accepts_bounded_loopback_capture_and_timeouts(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    properties_path = candidate / MULE4_PROPERTIES
    properties = yaml.safe_load(properties_path.read_text(encoding="utf-8"))
    properties["candidate"] = {"loopbackTimeout": 15_000}
    properties_path.write_text(yaml.safe_dump(properties, sort_keys=True), encoding="utf-8")
    write_loopback_http_munit(
        candidate,
        config_base_path="/api",
        config_response_timeout="${candidate.loopbackTimeout}",
        response_timeout="5000",
        target="candidateResponse",
        target_value="#[payload]",
        path="/customers/CANDIDATE-200/status",
    )

    assert check_mulesoft_candidate(candidate, source).passed is True


@pytest.mark.parametrize(
    "unsafe_override",
    (
        {"host": "api.example.invalid"},
        {"port": "8082"},
        {"protocol": "HTTPS"},
        {"method": "POST"},
        {"path": "/api/customers/CANDIDATE-200/profile"},
        {"target": "vars.candidateResponse"},
        {"target": "candidateResponse", "target_value": "#[vars.unbounded]"},
        {"response_timeout": "0"},
        {"response_timeout": str(MAX_MUNIT_RESPONSE_TIMEOUT_MS + 1)},
        {"response_timeout": "${missing.timeout}"},
        {"extra_request_attribute": 'url="https://api.example.invalid/status"'},
    ),
)
def test_candidate_munit_loopback_exception_does_not_allow_other_http_targets(
    tmp_path: Path,
    unsafe_override: dict[str, str],
) -> None:
    source, candidate = isolated_roots(tmp_path)
    write_loopback_http_munit(candidate, **unsafe_override)

    failure = expect_failure(candidate, source, MuleSoftLocalCheckCode.OUTBOUND_CONNECTOR)

    assert failure.artifact == MULE4_TEST


def test_static_contract_accepts_harmless_yaml_and_artifact_metadata(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    properties_path = candidate / MULE4_PROPERTIES
    properties = yaml.safe_load(properties_path.read_text(encoding="utf-8"))
    properties["http"]["port"] = 8081
    properties["http"]["idleTimeout"] = "30s"
    properties["observability"] = {"dryRun": True, "enabled": False}
    properties_path.write_text(yaml.safe_dump(properties, sort_keys=True), encoding="utf-8")
    artifact_path = candidate / MULE4_ARTIFACT
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["name"] = "generated-customer-status-api"
    artifact["redeploymentEnabled"] = True
    artifact_path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")

    assert check_mulesoft_candidate(candidate, source).passed is True


def test_static_contract_resolves_candidate_chosen_listener_property_keys(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    properties_path = candidate / MULE4_PROPERTIES
    properties_path.write_text(
        yaml.safe_dump(
            {
                "listener": {
                    "bindAddress": "127.0.0.1",
                    "bindPort": 8081,
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    app_path = candidate / MULE4_APP
    app = app_path.read_text(encoding="utf-8")
    app = app.replace("${http.host}", "${listener.bindAddress}")
    app = app.replace("${http.port}", "#[p('listener.bindPort')]")
    app_path.write_text(app, encoding="utf-8")

    assert check_mulesoft_candidate(candidate, source).passed is True


def test_static_contract_resolves_candidate_chosen_route_property_keys(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    properties_path = candidate / MULE4_PROPERTIES
    properties = yaml.safe_load(properties_path.read_text(encoding="utf-8"))
    properties["candidateRoutes"] = {
        "publicBase": "/api",
        "statusResource": "/customers/{customerId}/status",
    }
    properties_path.write_text(yaml.safe_dump(properties, sort_keys=True), encoding="utf-8")
    app_path = candidate / MULE4_APP
    replace(app_path, 'basePath="/api"', 'basePath="${candidateRoutes.publicBase}"')
    replace(
        app_path,
        'path="/customers/{customerId}/status"',
        "path=\"#[p('candidateRoutes.statusResource')]\"",
    )

    assert check_mulesoft_candidate(candidate, source).passed is True


@pytest.mark.parametrize(
    ("original", "unresolved"),
    (
        ('basePath="/api"', 'basePath="${missing.publicBase}"'),
        (
            'path="/customers/{customerId}/status"',
            "path=\"#[p('missing.statusResource')]\"",
        ),
    ),
)
def test_static_contract_rejects_unresolved_route_properties(
    tmp_path: Path,
    original: str,
    unresolved: str,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    replace(candidate / MULE4_APP, original, unresolved)

    failure = expect_failure(candidate, source, MuleSoftLocalCheckCode.MULE4_CONTRACT)

    assert failure.artifact == MULE4_APP


def test_static_contract_rejects_unsafe_property_resolved_route(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    properties_path = candidate / MULE4_PROPERTIES
    properties = yaml.safe_load(properties_path.read_text(encoding="utf-8"))
    properties["candidateRoutes"] = {"publicBase": "https://api.example.invalid"}
    properties_path.write_text(yaml.safe_dump(properties, sort_keys=True), encoding="utf-8")
    replace(
        candidate / MULE4_APP,
        'basePath="/api"',
        'basePath="${candidateRoutes.publicBase}"',
    )

    expect_failure(candidate, source, MuleSoftLocalCheckCode.MULE4_CONTRACT)


def test_static_contract_accepts_dotted_and_hyphenated_listener_property_keys(
    tmp_path: Path,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    properties_path = candidate / MULE4_PROPERTIES
    properties_path.write_text(
        '"listener.bind-address": "127.0.0.1"\n'
        '"listener.bind-port": 8081\n'
        "metadata:\n"
        "  dry-run: true\n",
        encoding="utf-8",
    )
    app_path = candidate / MULE4_APP
    app = app_path.read_text(encoding="utf-8")
    app = app.replace("${http.host}", "${listener.bind-address}")
    app = app.replace("${http.port}", "#[p('listener.bind-port')]")
    app_path.write_text(app, encoding="utf-8")

    assert check_mulesoft_candidate(candidate, source).passed is True


@pytest.mark.parametrize(
    "unsafe_yaml",
    (
        "http: &listener\n  host: 127.0.0.1\n  port: 8081\n",
        "http: *undefined-listener\n",
        "http:\n  <<: {host: 127.0.0.1}\n  port: 8081\n",
        "http:\n  host: 127.0.0.1\n  host: 0.0.0.0\n  port: 8081\n",
        "123: true\n",
        '"invalid key": true\n',
    ),
)
def test_yaml_graph_features_merge_keys_and_duplicate_keys_fail_closed(
    tmp_path: Path,
    unsafe_yaml: str,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    (candidate / MULE4_PROPERTIES).write_text(unsafe_yaml, encoding="utf-8")

    failure = expect_failure(candidate, source, MuleSoftLocalCheckCode.MALFORMED_YAML)

    assert failure.artifact == MULE4_PROPERTIES


def test_yaml_depth_entry_key_and_flattened_path_limits_fail_closed(tmp_path: Path) -> None:
    deeply_nested: dict[str, object] = {"leaf": True}
    for index in range(MAX_YAML_DEPTH + 1):
        deeply_nested = {f"level{index}": deeply_nested}

    long_path: dict[str, object] = {"value": True}
    path_segments = (MAX_YAML_PATH_LENGTH // 65) + 2
    for index in range(path_segments):
        long_path = {f"{index:02d}-" + ("k" * 61): long_path}

    bounded_inputs = {
        "depth": yaml.safe_dump(deeply_nested),
        "entries": yaml.safe_dump(
            {f"metadata{index}": True for index in range(MAX_YAML_ENTRIES + 1)}
        ),
        "key": yaml.safe_dump({"k" * (MAX_YAML_KEY_LENGTH + 1): True}),
        "path": yaml.safe_dump(long_path),
    }
    for name, unsafe_yaml in bounded_inputs.items():
        source, candidate = isolated_roots(tmp_path / name)
        (candidate / MULE4_PROPERTIES).write_text(unsafe_yaml, encoding="utf-8")

        expect_failure(candidate, source, MuleSoftLocalCheckCode.MALFORMED_YAML)


def test_flattened_key_collisions_and_mapping_cycles_fail_closed(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    (candidate / MULE4_PROPERTIES).write_text(
        'http:\n  host: 127.0.0.1\n  port: 8081\n"http.host": 127.0.0.1\n',
        encoding="utf-8",
    )
    expect_failure(candidate, source, MuleSoftLocalCheckCode.MALFORMED_YAML)

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(MuleSoftLocalCheckFailure) as caught:
        _flatten_configuration(cyclic)
    assert caught.value.code is MuleSoftLocalCheckCode.MALFORMED_YAML
    assert caught.value.artifact == MULE4_PROPERTIES


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


def test_immutable_source_is_not_revalidated_against_fixture_semantics(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path)
    for root in (source, candidate):
        replace(root / MULE3_APP, "%dw 1.0", "%dw 2.0")

    assert check_mulesoft_candidate(candidate, source).passed is True


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

    source, candidate = isolated_roots(tmp_path / "duplicate-listener")
    replace(
        candidate / MULE4_APP,
        "</mule>",
        """    <flow name="duplicate-public-route">
        <http:listener config-ref="customer-status-http-listener"
                       path="/customers/{customerId}/status"
                       allowedMethods="GET"/>
    </flow>
</mule>""",
    )
    expect_failure(candidate, source, MuleSoftLocalCheckCode.MULE4_CONTRACT)


@pytest.mark.parametrize(
    "body",
    (
        "null",
        '{ arbitrary: "not-the-public-response" }',
        "{ customerId: null, status: null, source: null }",
    ),
)
def test_dataweave_static_check_rejects_trivial_or_unbound_response_bodies(
    tmp_path: Path,
    body: str,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    (candidate / MULE4_DATAWEAVE).write_text(
        f"%dw 2.0\noutput application/json\n---\n{body}\n",
        encoding="utf-8",
    )

    expect_failure(candidate, source, MuleSoftLocalCheckCode.DATAWEAVE_CONTRACT)


def test_munit_pom_artifact_and_version_regressions_fail_closed(tmp_path: Path) -> None:
    source, candidate = isolated_roots(tmp_path / "munit")
    test_path = candidate / MULE4_TEST
    test_path.write_text(
        test_path.read_text(encoding="utf-8").replace(
            "munit-tools:assert-that",
            "munit-tools:verify-call",
        ),
        encoding="utf-8",
    )
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
    ("original", "invalid"),
    (
        (
            '<flow-ref name="build-customer-status-response"/>',
            '<flow-ref name="missing-candidate-flow"/>',
        ),
        (
            "<munit-tools:assert-that ",
            "<munit-tools:verify-call ",
        ),
    ),
)
def test_candidate_munit_must_call_candidate_code_and_contain_an_assertion(
    tmp_path: Path,
    original: str,
    invalid: str,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    if "assert-that" in original:
        path = candidate / MULE4_TEST
        path.write_text(
            path.read_text(encoding="utf-8").replace(original, invalid),
            encoding="utf-8",
        )
    else:
        replace(candidate / MULE4_TEST, original, invalid)

    expect_failure(candidate, source, MuleSoftLocalCheckCode.MUNIT_CONTRACT)


@pytest.mark.parametrize(
    "assertion",
    (
        ('<munit-tools:assert-that expression="#[true]" is="#[MunitTools::equalTo(true)]"/>'),
        (
            '<munit-tools:assert-that expression="#[payload.status]" '
            'is="#[MunitTools::equalTo(payload.status)]"/>'
        ),
        '<munit-tools:assert-equals actual="#[payload]" expected="#[payload]"/>',
    ),
)
def test_candidate_munit_rejects_constant_and_self_comparison_assertions(
    tmp_path: Path,
    assertion: str,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    (candidate / MULE4_TEST).write_text(
        f"""<mule xmlns="http://www.mulesoft.org/schema/mule/core"
      xmlns:munit="http://www.mulesoft.org/schema/mule/munit"
      xmlns:munit-tools="http://www.mulesoft.org/schema/mule/munit-tools">
  <munit:config name="candidate-assertion-suite"/>
  <munit:test name="candidate-assertion-test">
    <munit:execution>
      <flow-ref name="build-customer-status-response"/>
    </munit:execution>
    <munit:validation>
      {assertion}
    </munit:validation>
  </munit:test>
</mule>
""",
        encoding="utf-8",
    )

    expect_failure(candidate, source, MuleSoftLocalCheckCode.MUNIT_CONTRACT)


@pytest.mark.parametrize(
    ("candidate_name", "reserved_name"),
    (
        (
            "customer-status-api-test-suite",
            "controller-customer-status-behavior-test-suite",
        ),
        (
            "build-customer-status-response-test",
            "controller-build-customer-status-response-contract",
        ),
    ),
)
def test_candidate_munit_cannot_reuse_runtime_evidence_identities(
    tmp_path: Path,
    candidate_name: str,
    reserved_name: str,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    replace(candidate / MULE4_TEST, candidate_name, reserved_name)

    expect_failure(candidate, source, MuleSoftLocalCheckCode.MUNIT_CONTRACT)


@pytest.mark.parametrize("supply_chain_drift", ("dependency", "plugin", "repository"))
def test_pom_supply_chain_allowlists_reject_unapproved_inputs(
    tmp_path: Path,
    supply_chain_drift: str,
) -> None:
    source, candidate = isolated_roots(tmp_path)
    pom = candidate / MULE4_POM
    if supply_chain_drift == "dependency":
        replace(
            pom,
            "</dependencies>",
            """    <dependency>
            <groupId>example.unapproved</groupId>
            <artifactId>extra-library</artifactId>
            <version>1.0.0</version>
        </dependency>
    </dependencies>""",
        )
    elif supply_chain_drift == "plugin":
        replace(
            pom,
            "</plugins>",
            """        <plugin>
                <groupId>example.unapproved</groupId>
                <artifactId>extra-plugin</artifactId>
                <version>1.0.0</version>
            </plugin>
        </plugins>""",
        )
    else:
        replace(
            pom,
            "https://repository.mulesoft.org/releases/",
            "https://packages.example.invalid/releases/",
        )

    expect_failure(candidate, source, MuleSoftLocalCheckCode.POM_CONTRACT)


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
