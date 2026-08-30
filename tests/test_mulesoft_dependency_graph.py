from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mulesoft_candidate_factory import build_mulesoft_candidate

import legacy_migration_agent.graphs.mulesoft_dependency_graph as mulesoft_graph_module
from legacy_migration_agent.contracts import Platform
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.workspace import content_revision, snapshot_tree
from legacy_migration_agent.graphs.graph_contracts import EdgeKind, NodeKind, WarningCode
from legacy_migration_agent.graphs.graph_store import GraphSnapshotStore
from legacy_migration_agent.graphs.mulesoft_dependency_graph import (
    MULESOFT_ANALYZER_VERSION,
    build_mulesoft_dependency_graph,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "fixtures/mulesoft/customer-status-api"
MULE3_ROOT = FIXTURE_ROOT / "input"
MULE3_APP = "legacy-mule3/customer-status-api/src/main/app/customer-status-api.xml"
MULE3_PROPERTIES = "legacy-mule3/customer-status-api/src/main/app/mule-app.properties"
MULE3_TEST = "legacy-mule3/customer-status-api/src/test/munit/customer-status-api-test.xml"
MULE4_APP = "mule4/customer-status-api/src/main/mule/customer-status-api.xml"
MULE4_TEST = "mule4/customer-status-api/src/test/munit/customer-status-api-test.xml"


def _build(root: Path, entries: tuple[str, ...]):
    return build_mulesoft_dependency_graph(root, entries, content_revision(root))


def _edge_tuples(graph) -> set[tuple[str, str, str, str | None, bool]]:
    nodes = {node.node_id: node for node in graph.nodes}
    return {
        (
            nodes[edge.source_id].name,
            edge.kind.value,
            nodes[edge.target_id].name,
            edge.symbol,
            edge.resolved,
        )
        for edge in graph.edges
    }


def _write(root: Path, relative_path: str, content: str) -> Path:
    destination = root.joinpath(*relative_path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def _mule4_root(tmp_path: Path) -> Path:
    candidate = build_mulesoft_candidate(MULE3_ROOT, tmp_path / "candidate")
    return candidate / "mule4/customer-status-api"


def test_mule3_fixture_graph_is_exact_deterministic_and_revision_bound() -> None:
    first = _build(MULE3_ROOT, (MULE3_APP,))
    second = _build(MULE3_ROOT, (MULE3_APP,))

    assert first.platform is Platform.MULESOFT
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.has_unresolved is False
    assert first.warnings == ()
    assert {(node.kind, node.name) for node in first.nodes} == {
        (NodeKind.MULE_CONFIGURATION, "customer-status-http-listener"),
        (NodeKind.MULE_FLOW, "customer-status-api-flow"),
        (NodeKind.MULE_SUBFLOW, "build-customer-status-response"),
        (NodeKind.MULE_PROPERTY, "http.host"),
        (NodeKind.MULE_PROPERTY, "http.port"),
        (NodeKind.MULE_VARIABLE, "customerId"),
        (NodeKind.MULE_VARIABLE, "responseStatus"),
        (NodeKind.MUNIT_SUITE, "customer-status-api-munit"),
        (NodeKind.MUNIT_TEST, "build-customer-status-response-test"),
    }
    assert _edge_tuples(first) == {
        (
            "customer-status-api-flow",
            "http_listener_config_reference",
            "customer-status-http-listener",
            "customer-status-http-listener",
            True,
        ),
        (
            "customer-status-api-flow",
            "mule_route_parameter_binding",
            "customerId",
            "{customerId}",
            True,
        ),
        (
            "customer-status-api-flow",
            "flow_reference",
            "build-customer-status-response",
            "build-customer-status-response",
            True,
        ),
        (
            "customer-status-http-listener",
            "property_reference",
            "http.host",
            "${http.host}",
            True,
        ),
        (
            "customer-status-http-listener",
            "property_reference",
            "http.port",
            "${http.port}",
            True,
        ),
        (
            "build-customer-status-response",
            "dataweave_variable_reference",
            "customerId",
            "flowVars.customerId",
            True,
        ),
        (
            "build-customer-status-response",
            "dataweave_variable_reference",
            "responseStatus",
            "flowVars.responseStatus",
            True,
        ),
        (
            "customer-status-api-munit",
            "munit_suite_test",
            "build-customer-status-response-test",
            "build-customer-status-response-test",
            True,
        ),
        (
            "build-customer-status-response-test",
            "munit_flow_reference",
            "build-customer-status-response",
            "build-customer-status-response",
            True,
        ),
        (
            "build-customer-status-response-test",
            "munit_variable_reference",
            "responseStatus",
            "flowVars.responseStatus",
            True,
        ),
    }
    assert {digest.path for digest in first.source_digests} == {
        MULE3_APP,
        MULE3_PROPERTIES,
        MULE3_TEST,
    }

    with pytest.raises(PolicyViolation, match="base_revision is stale"):
        build_mulesoft_dependency_graph(
            MULE3_ROOT,
            (MULE3_APP,),
            "sha256:" + "0" * 64,
        )


def test_mule3_route_lineage_and_munit_variable_reference_kinds_are_explicit() -> None:
    graph = _build(MULE3_ROOT, (MULE3_APP,))
    nodes = {node.node_id: node for node in graph.nodes}
    route_binding = next(
        edge
        for edge in graph.edges
        if edge.kind is EdgeKind.MULE_ROUTE_PARAMETER_BINDING
        and nodes[edge.source_id].name == "customer-status-api-flow"
        and nodes[edge.target_id].name == "customerId"
    )

    assert route_binding.symbol == "{customerId}"
    assert tuple((item.parser, item.line) for item in route_binding.provenance) == (
        ("mule_http_route", 18),
        ("mule_expression", 21),
    )
    assert (
        "build-customer-status-response-test",
        "munit_variable_reference",
        "responseStatus",
        "flowVars.responseStatus",
        True,
    ) in _edge_tuples(graph)
    assert not any(
        edge.kind is EdgeKind.DATAWEAVE_VARIABLE_REFERENCE
        and nodes[edge.source_id].kind is NodeKind.MUNIT_TEST
        for edge in graph.edges
    )


def test_entry_paths_are_a_canonical_set_and_snapshot_store_round_trips(
    tmp_path: Path,
) -> None:
    revision = content_revision(MULE3_ROOT)
    first = build_mulesoft_dependency_graph(
        MULE3_ROOT, (MULE3_TEST, MULE3_APP, MULE3_APP), revision
    )
    second = build_mulesoft_dependency_graph(MULE3_ROOT, (MULE3_APP, MULE3_TEST), revision)

    assert first == second
    assert first.entry_paths == tuple(sorted((MULE3_APP, MULE3_TEST)))
    store = GraphSnapshotStore(tmp_path / "graphs")
    key = store.save(first, analyzer_version=MULESOFT_ANALYZER_VERSION)
    assert key.platform is Platform.MULESOFT
    assert store.load(key) == first


def test_labeled_mule3_edges_have_full_recall_and_no_high_impact_miss() -> None:
    labels = json.loads(
        (PROJECT_ROOT / "evaluation/mulesoft-customer-status-api-source-edges.json").read_text(
            encoding="utf-8"
        )
    )["edges"]
    graph = _build(MULE3_ROOT, (MULE3_APP,))
    observed = _edge_tuples(graph)
    expected = {
        (edge["source"], edge["kind"], edge["target"], edge["symbol"], True) for edge in labels
    }
    missing = expected - observed
    recall = (len(expected) - len(missing)) / len(expected)

    assert recall >= 0.95
    assert missing == set()


def test_mule4_graph_links_resources_dataweave_variables_and_munit(tmp_path: Path) -> None:
    graph = _build(_mule4_root(tmp_path), ("src/main/mule/customer-status-api.xml",))
    edges = _edge_tuples(graph)

    assert graph.has_unresolved is False
    assert (
        "configuration-properties:application.yaml",
        "configuration_properties_reference",
        "application.yaml",
        "application.yaml",
        True,
    ) in edges
    assert (
        "build-customer-status-response",
        "dataweave_module_reference",
        "dw/customer-status-response.dwl",
        "dw/customer-status-response.dwl",
        True,
    ) in edges
    assert (
        "dw/customer-status-response.dwl",
        "dataweave_variable_reference",
        "customerId",
        "vars.customerId",
        True,
    ) in edges
    assert (
        "build-customer-status-response-test",
        "munit_flow_reference",
        "build-customer-status-response",
        "build-customer-status-response",
        True,
    ) in edges


def test_combined_snapshot_scopes_same_named_apps_and_http_only_munit_by_target_path(
    tmp_path: Path,
) -> None:
    candidate = build_mulesoft_candidate(MULE3_ROOT, tmp_path / "candidate")
    (candidate / MULE4_TEST).write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<mule xmlns="http://www.mulesoft.org/schema/mule/core"
      xmlns:http="http://www.mulesoft.org/schema/mule/http"
      xmlns:munit="http://www.mulesoft.org/schema/mule/munit"
      xmlns:munit-tools="http://www.mulesoft.org/schema/mule/munit-tools">
  <http:request-config name="candidate-loopback-request">
    <http:request-connection host="127.0.0.1" port="8081"/>
  </http:request-config>
  <munit:config name="customer-status-api-munit"/>
  <munit:test name="build-customer-status-response-test">
    <munit:enable-flow-sources>
      <munit:enable-flow-source value="customer-status-api-flow"/>
    </munit:enable-flow-sources>
    <munit:execution>
      <http:request method="GET"
                    path="/api/customers/HTTP-ONLY/status"
                    config-ref="candidate-loopback-request"/>
    </munit:execution>
    <munit:validation>
      <munit-tools:assert-that expression="#[payload.status]"
                               is='#[MunitTools::equalTo("ACTIVE")]'/>
    </munit:validation>
  </munit:test>
</mule>
""",
        encoding="utf-8",
    )

    graph = _build(candidate, (MULE4_APP,))
    target_tests = tuple(node for node in graph.nodes if node.kind is NodeKind.MUNIT_TEST)

    assert len(target_tests) == 1
    assert target_tests[0].metadata_paths == (MULE4_TEST,)
    assert target_tests[0].node_id.startswith("mule:mule4:")
    assert MULE3_TEST not in {path for node in graph.nodes for path in node.metadata_paths}
    assert (
        "build-customer-status-response-test",
        "munit_flow_reference",
        "customer-status-api-flow",
        "customer-status-api-flow",
        True,
    ) in _edge_tuples(graph)


def test_pom_graph_indexes_plugins_dependencies_and_property_versions(tmp_path: Path) -> None:
    graph = _build(_mule4_root(tmp_path), ("pom.xml",))
    edges = _edge_tuples(graph)

    assert graph.has_unresolved is False
    assert len([node for node in graph.nodes if node.kind is NodeKind.MAVEN_PLUGIN]) == 2
    assert len([node for node in graph.nodes if node.kind is NodeKind.MAVEN_DEPENDENCY]) == 3
    assert (
        "customer-status-api-mule4",
        "maven_dependency",
        "org.mule.connectors:mule-http-connector",
        "org.mule.connectors:mule-http-connector",
        True,
    ) in edges
    assert (
        "org.mule.tools.maven:mule-maven-plugin",
        "property_reference",
        "app.runtime",
        "${app.runtime}",
        True,
    ) in edges


def test_missing_dynamic_and_unsafe_resource_references_are_explicit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    entry = _write(
        root,
        "src/main/mule/unsafe.xml",
        """<?xml version="1.0" encoding="UTF-8"?>
<mule xmlns="http://www.mulesoft.org/schema/mule/core"
      xmlns:http="http://www.mulesoft.org/schema/mule/http"
      xmlns:ee="http://www.mulesoft.org/schema/mule/ee/core">
  <flow name="unsafe-flow">
    <flow-ref name="missing-subflow"/>
    <flow-ref name="#[vars.nextFlow]"/>
    <http:listener config-ref="missing-listener" path="/unsafe"/>
    <set-payload value="#[vars.missingVariable ++ '${missing.property}']"/>
    <ee:set-payload resource="../../outside.dwl"/>
  </flow>
</mule>
""",
    )

    graph = _build(root, (entry.relative_to(root).as_posix(),))
    warning_codes = {warning.code for warning in graph.warnings}
    edges = _edge_tuples(graph)

    assert graph.has_unresolved is True
    assert WarningCode.DYNAMIC_REFERENCE in warning_codes
    assert WarningCode.UNRESOLVED_REFERENCE in warning_codes
    assert any(kind == "dynamic_reference" and not resolved for _, kind, _, _, resolved in edges)
    assert any(
        kind == "flow_reference" and target == "missing-subflow" and not resolved
        for _, kind, target, _, resolved in edges
    )
    assert any(
        kind == "http_listener_config_reference" and target == "missing-listener" and not resolved
        for _, kind, target, _, resolved in edges
    )
    assert any(
        kind == "property_reference" and target == "missing.property" and not resolved
        for _, kind, target, _, resolved in edges
    )
    assert any(
        target == "../../outside.dwl" and not resolved for _, _, target, _, resolved in edges
    )


def test_dynamic_dataweave_resource_access_is_unresolved(tmp_path: Path) -> None:
    root = tmp_path / "project"
    module = _write(
        root,
        "src/main/resources/dw/dynamic.dwl",
        "%dw 2.0\noutput application/json\n---\nreadUrl(vars.resourcePath)",
    )

    graph = _build(root, (module.relative_to(root).as_posix(),))

    assert graph.has_unresolved is True
    assert any(warning.code is WarningCode.DYNAMIC_REFERENCE for warning in graph.warnings)
    assert any(edge.kind is EdgeKind.DYNAMIC_REFERENCE for edge in graph.edges)


def test_graph_uses_one_captured_snapshot_not_mutated_live_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    app = _write(
        root,
        "src/main/mule/app.xml",
        """<mule xmlns="http://www.mulesoft.org/schema/mule/core">
  <flow name="stable-flow"><flow-ref name="stable-subflow"/></flow>
  <sub-flow name="stable-subflow"/>
</mule>""",
    )
    captured = snapshot_tree(root)
    calls = 0

    def capture_then_mutate(candidate: Path | str):
        nonlocal calls
        assert Path(candidate).resolve() == root.resolve()
        calls += 1
        app.write_text(
            """<mule xmlns="http://www.mulesoft.org/schema/mule/core">
  <flow name="changed-flow"><flow-ref name="missing"/></flow>
</mule>""",
            encoding="utf-8",
        )
        return captured

    monkeypatch.setattr(mulesoft_graph_module, "snapshot_tree", capture_then_mutate)
    graph = build_mulesoft_dependency_graph(
        root,
        (app.relative_to(root).as_posix(),),
        captured.revision,
    )

    assert calls == 1
    assert graph.node(NodeKind.MULE_FLOW, "stable-flow") is not None
    assert graph.node(NodeKind.MULE_FLOW, "changed-flow") is None
    assert graph.has_unresolved is False


@pytest.mark.parametrize("entry", ("../outside.xml", "src/../../outside.xml", "/tmp/x.xml"))
def test_entry_paths_reject_traversal_and_absolute_paths(tmp_path: Path, entry: str) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with pytest.raises(ValueError, match="paths"):
        build_mulesoft_dependency_graph(root, (entry,), content_revision(root))


@pytest.mark.parametrize("declaration", ("<!DOCTYPE mule []>", "<!ENTITY x 'unsafe'>"))
def test_xml_dtd_and_entity_declarations_fail_closed(
    tmp_path: Path,
    declaration: str,
) -> None:
    root = tmp_path / "project"
    entry = _write(
        root,
        "src/main/mule/unsafe.xml",
        f"<?xml version='1.0'?>{declaration}<mule/>",
    )

    with pytest.raises(PolicyViolation, match="DTD/entity"):
        _build(root, (entry.relative_to(root).as_posix(),))


def test_symlink_is_rejected_before_mulesoft_parsing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    outside = _write(tmp_path, "outside.xml", "<mule/>")
    link = root / "src/main/mule/linked.xml"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError as exc:  # pragma: no cover - platform-specific fallback
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(PolicyViolation, match="symlink"):
        build_mulesoft_dependency_graph(root, ("src/main/mule/linked.xml",), "stale-revision")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_special_file_is_rejected_before_mulesoft_parsing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    fifo = root / "src/main/mule/pipe.xml"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)

    with pytest.raises(PolicyViolation, match="unsupported special file"):
        build_mulesoft_dependency_graph(root, ("src/main/mule/pipe.xml",), "stale-revision")


def test_malformed_xml_is_selected_as_unresolved_evidence(tmp_path: Path) -> None:
    root = tmp_path / "project"
    entry = _write(root, "src/main/mule/broken.xml", "<mule><flow></mule>")

    graph = _build(root, (entry.relative_to(root).as_posix(),))

    assert graph.has_unresolved is True
    assert graph.nodes[0].kind is NodeKind.METADATA_FILE
    assert graph.nodes[0].resolved is False
    assert graph.warnings[0].code is WarningCode.MALFORMED_SOURCE
