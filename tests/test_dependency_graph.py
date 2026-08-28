from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from salesforce_candidate_factory import salesforce_candidate_outputs

import legacy_migration_agent.graphs.dependency_graph as dependency_graph_module
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.workspace import content_revision, snapshot_tree
from legacy_migration_agent.graphs.dependency_graph import (
    DependencyGraph,
    EdgeKind,
    NodeKind,
    WarningCode,
    build_salesforce_dependency_graph,
)
from legacy_migration_agent.graphs.graph_contracts import NodeKind as CommonNodeKind

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "salesforce" / "account-contact-explorer"
)


def _edge_exists(graph, source_name: str, target_name: str, kind: EdgeKind) -> bool:
    nodes = {node.node_id: node for node in graph.nodes}
    return any(
        nodes[edge.source_id].name == source_name
        and nodes[edge.target_id].name == target_name
        and edge.kind is kind
        and edge.resolved
        for edge in graph.edges
    )


def _write(root: Path, relative_path: str, content: str) -> Path:
    destination = root.joinpath(*relative_path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def test_public_imports_and_current_graph_contract_round_trip() -> None:
    source_root = FIXTURE_ROOT / "input"
    generated = build_salesforce_dependency_graph(
        source_root,
        ("force-app/main/default/pages/LegacyAccountContactExplorer.page",),
        content_revision(source_root),
    )
    payload = generated.model_dump(mode="json")

    graph = DependencyGraph.model_validate(payload)

    assert CommonNodeKind is NodeKind
    assert graph.model_dump(mode="json") == payload


def test_input_fixture_builds_evidence_bearing_legacy_graph() -> None:
    source_root = FIXTURE_ROOT / "input"
    graph = build_salesforce_dependency_graph(
        source_root,
        ("force-app/main/default/pages/LegacyAccountContactExplorer.page",),
        content_revision(source_root),
    )

    assert graph.base_revision == content_revision(source_root)
    assert graph.has_unresolved is False
    assert graph.warnings == ()
    assert _edge_exists(
        graph,
        "LegacyAccountContactExplorer",
        "LegacyAccountContactExplorerController",
        EdgeKind.VF_CONTROLLER,
    )
    assert _edge_exists(
        graph,
        "LegacyAcctContactExplorerCtrlTest",
        "LegacyAccountContactExplorerController",
        EdgeKind.APEX_CLASS_REFERENCE,
    )
    assert _edge_exists(
        graph,
        "LegacyAccountContactExplorerController",
        "Account",
        EdgeKind.SOQL_OBJECT,
    )
    assert _edge_exists(
        graph,
        "LegacyAccountContactExplorerController",
        "Contact",
        EdgeKind.SOQL_OBJECT,
    )
    assert _edge_exists(
        graph,
        "AccountContactExplorerUser",
        "LegacyAccountContactExplorerController",
        EdgeKind.PERMISSION_CLASS_ACCESS,
    )
    assert _edge_exists(
        graph,
        "AccountContactExplorerUser",
        "LegacyAccountContactExplorer",
        EdgeKind.PERMISSION_PAGE_ACCESS,
    )
    assert _edge_exists(
        graph,
        "AccountContactExplorerUser",
        "Contact.Email",
        EdgeKind.PERMISSION_FIELD_ACCESS,
    )

    controller = graph.node(NodeKind.APEX_CLASS, "LegacyAccountContactExplorerController")
    assert controller is not None
    assert controller.metadata_paths == (
        "force-app/main/default/classes/LegacyAccountContactExplorerController.cls",
        "force-app/main/default/classes/LegacyAccountContactExplorerController.cls-meta.xml",
    )
    assert all(edge.provenance for edge in graph.edges)
    assert all(digest.sha256 and len(digest.sha256) == 64 for digest in graph.source_digests)


def test_synthetic_candidate_links_lwc_test_and_permission_to_target_controller(
    tmp_path: Path,
) -> None:
    entry = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    source_root = tmp_path / "candidate"
    shutil.copytree(
        FIXTURE_ROOT / "input",
        source_root,
    )
    for relative_path, content in salesforce_candidate_outputs().items():
        destination = source_root.joinpath(*relative_path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    graph = build_salesforce_dependency_graph(
        source_root,
        (entry,),
        content_revision(source_root),
    )

    assert graph.has_unresolved is False
    assert _edge_exists(
        graph,
        "accountContactExplorer",
        "AccountContactExplorerController",
        EdgeKind.LWC_APEX_IMPORT,
    )
    lwc_imports = {edge.symbol for edge in graph.edges if edge.kind is EdgeKind.LWC_APEX_IMPORT}
    assert lwc_imports == {
        "AccountContactExplorerController.getAccounts",
        "AccountContactExplorerController.getContacts",
    }
    assert _edge_exists(
        graph,
        "AccountContactExplorerControllerTest",
        "AccountContactExplorerController",
        EdgeKind.APEX_CLASS_REFERENCE,
    )
    assert _edge_exists(
        graph,
        "AccountContactExplorerUser",
        "AccountContactExplorerController",
        EdgeKind.PERMISSION_CLASS_ACCESS,
    )
    assert _edge_exists(
        graph,
        "AccountContactExplorerController",
        "Account",
        EdgeKind.SOQL_OBJECT,
    )
    assert _edge_exists(
        graph,
        "AccountContactExplorerController",
        "Contact",
        EdgeKind.SOQL_OBJECT,
    )

    lwc = graph.node(NodeKind.LWC_COMPONENT, "accountContactExplorer")
    assert lwc is not None
    assert entry in lwc.metadata_paths
    assert (
        "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js-meta.xml"
    ) in lwc.metadata_paths


def test_standard_user_constructor_and_profile_query_resolve_as_schema(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    entry = _write(
        root,
        "force-app/main/default/classes/GeneratedControllerTest.cls",
        """
@IsTest
private class GeneratedControllerTest {
    @IsTest
    static void createsBoundedUser() {
        Profile selectedProfile = [SELECT Id, Name FROM Profile LIMIT 1];
        User selectedUser = new User(
            Alias = 'bounded',
            Email = 'bounded@example.invalid',
            LastName = 'Bounded',
            ProfileId = selectedProfile.Id,
            Username = 'bounded@example.invalid'
        );
        Assert.isNotNull(selectedUser);
    }
}
""",
    )

    graph = build_salesforce_dependency_graph(
        root,
        (entry.relative_to(root).as_posix(),),
        content_revision(root),
    )

    assert graph.has_unresolved is False
    assert graph.warnings == ()
    assert graph.node(NodeKind.SCHEMA_OBJECT, "Profile") is not None
    assert graph.node(NodeKind.SCHEMA_FIELD, "Profile.Id") is not None
    assert graph.node(NodeKind.SCHEMA_FIELD, "Profile.Name") is not None
    assert graph.node(NodeKind.UNRESOLVED, "User") is None


def test_graph_is_deterministic_and_revision_bound() -> None:
    source_root = FIXTURE_ROOT / "input"
    revision = content_revision(source_root)
    args = (
        source_root,
        ("force-app/main/default/pages/LegacyAccountContactExplorer.page",),
        revision,
    )
    first = build_salesforce_dependency_graph(*args)
    second = build_salesforce_dependency_graph(*args)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.source_digests == second.source_digests
    with pytest.raises(PolicyViolation, match="base_revision is stale"):
        build_salesforce_dependency_graph(
            args[0],
            args[1],
            "sha256:" + "0" * 64,
        )


def test_nested_soql_fields_are_owned_by_their_query_scope(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    entry = _write(
        root,
        "force-app/main/default/classes/NestedQueryController.cls",
        """
public class NestedQueryController {
    public static List<Account> load() {
        return [
            SELECT Id, Name,
                (SELECT Id, Email FROM Contacts WHERE Email != null)
            FROM Account
            WHERE Name != null
        ];
    }
}
""",
    )

    graph = build_salesforce_dependency_graph(
        root,
        (entry.relative_to(root).as_posix(),),
        content_revision(root),
    )

    field_symbols = {edge.symbol for edge in graph.edges if edge.kind is EdgeKind.SOQL_FIELD}
    assert graph.has_unresolved is False
    assert graph.warnings == ()
    assert field_symbols == {
        "Account.Id",
        "Account.Name",
        "Contact.Email",
        "Contact.Id",
    }
    assert _edge_exists(
        graph,
        "NestedQueryController",
        "Contact.Email",
        EdgeKind.SOQL_FIELD,
    )
    assert _edge_exists(
        graph,
        "NestedQueryController",
        "Contact.Id",
        EdgeKind.SOQL_FIELD,
    )


def test_unknown_child_relationship_query_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    entry = _write(
        root,
        "force-app/main/default/classes/UnknownChildQueryController.cls",
        """
public class UnknownChildQueryController {
    public static List<Account> load() {
        return [SELECT Id, (SELECT Id FROM UnknownChildren) FROM Account];
    }
}
""",
    )

    graph = build_salesforce_dependency_graph(
        root,
        (entry.relative_to(root).as_posix(),),
        content_revision(root),
    )

    assert graph.has_unresolved is True
    assert graph.node(NodeKind.UNRESOLVED, "UnknownChildren") is not None
    assert any(
        warning.code is WarningCode.UNRESOLVED_REFERENCE and "UnknownChildren" in warning.message
        for warning in graph.warnings
    )
    assert {edge.symbol for edge in graph.edges if edge.kind is EdgeKind.SOQL_FIELD} == {
        "Account.Id"
    }


def test_ambiguous_nested_soql_scope_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    entry = _write(
        root,
        "force-app/main/default/classes/AmbiguousQueryController.cls",
        """
public class AmbiguousQueryController {
    public static List<Account> load() {
        return [
            SELECT Id, (SELECT Id FROM Contacts FROM Account)
            FROM Account
        ];
    }
}
""",
    )

    graph = build_salesforce_dependency_graph(
        root,
        (entry.relative_to(root).as_posix(),),
        content_revision(root),
    )

    assert graph.has_unresolved is True
    assert any(
        warning.code is WarningCode.MALFORMED_SOURCE and "multiple FROM targets" in warning.message
        for warning in graph.warnings
    )
    assert graph.node(NodeKind.SCHEMA_FIELD, "Contact.Id") is None
    assert {edge.symbol for edge in graph.edges if edge.kind is EdgeKind.SOQL_FIELD} == {
        "Account.Id"
    }


def test_static_soql_opaque_field_expansion_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    entry = _write(
        root,
        "force-app/main/default/classes/FieldExpansionController.cls",
        """
public class FieldExpansionController {
    public static List<Account> load() {
        return [SELECT FIELDS(ALL) FROM Account];
    }
}
""",
    )

    graph = build_salesforce_dependency_graph(
        root,
        (entry.relative_to(root).as_posix(),),
        content_revision(root),
    )

    assert graph.has_unresolved is True
    assert any(
        warning.code is WarningCode.DYNAMIC_REFERENCE and "FIELDS() expansion" in warning.message
        for warning in graph.warnings
    )
    assert not any(edge.kind is EdgeKind.SOQL_FIELD for edge in graph.edges)
    assert _edge_exists(
        graph,
        "FieldExpansionController",
        "Account",
        EdgeKind.SOQL_OBJECT,
    )


def test_graph_parses_one_captured_snapshot_not_later_live_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    controller = _write(
        root,
        "force-app/main/default/classes/StableController.cls",
        "public class StableController {}",
    )
    page = _write(
        root,
        "force-app/main/default/pages/Stable.page",
        '<apex:page controller="StableController"></apex:page>',
    )
    captured = snapshot_tree(root)
    calls = 0

    def capture_then_mutate(candidate: Path | str):
        nonlocal calls
        assert Path(candidate).resolve() == root.resolve()
        calls += 1
        page.write_text('<apex:page controller="ChangedController"></apex:page>', encoding="utf-8")
        controller.write_text("public class ChangedController {}", encoding="utf-8")
        return captured

    monkeypatch.setattr(dependency_graph_module, "snapshot_tree", capture_then_mutate)
    graph = build_salesforce_dependency_graph(
        root,
        (page.relative_to(root).as_posix(),),
        captured.revision,
    )

    assert calls == 1
    assert graph.node(NodeKind.APEX_CLASS, "StableController") is not None
    assert graph.node(NodeKind.UNRESOLVED, "ChangedController") is None
    assert graph.base_revision == captured.revision


def test_dynamic_and_missing_references_are_never_silently_ignored(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    page = _write(
        root,
        "force-app/main/default/pages/Unsafe.page",
        '<apex:page controller="MissingController"></apex:page>',
    )
    dynamic = _write(
        root,
        "force-app/main/default/classes/DynamicController.cls",
        """
public class DynamicController {
    public static void inspect(String objectName, String queryText) {
        Object instance = Type.forName(objectName).newInstance();
        List<SObject> records = Database.query(queryText);
        MissingHelper helper = new MissingHelper();
    }
}
""",
    )

    graph = build_salesforce_dependency_graph(
        root,
        (
            page.relative_to(root).as_posix(),
            dynamic.relative_to(root).as_posix(),
        ),
        content_revision(root),
    )

    assert graph.has_unresolved is True
    warning_codes = {warning.code for warning in graph.warnings}
    assert WarningCode.DYNAMIC_SOQL in warning_codes
    assert WarningCode.DYNAMIC_TYPE in warning_codes
    assert WarningCode.UNRESOLVED_REFERENCE in warning_codes
    assert graph.node(NodeKind.UNRESOLVED, "MissingController") is not None
    assert graph.node(NodeKind.UNRESOLVED, "MissingHelper") is not None
    dynamic_edges = [edge for edge in graph.edges if edge.kind is EdgeKind.DYNAMIC_REFERENCE]
    assert len(dynamic_edges) == 2
    assert all(edge.resolved is False for edge in dynamic_edges)


def test_lwc_missing_method_is_an_unresolved_edge(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    _write(
        root,
        "force-app/main/default/classes/TargetController.cls",
        "public class TargetController { public static void existing() {} }",
    )
    entry = _write(
        root,
        "force-app/main/default/lwc/example/example.js",
        "import missing from '@salesforce/apex/TargetController.missing';",
    )

    graph = build_salesforce_dependency_graph(
        root,
        (entry.relative_to(root).as_posix(),),
        content_revision(root),
    )

    edge = next(edge for edge in graph.edges if edge.kind is EdgeKind.LWC_APEX_IMPORT)
    assert edge.symbol == "TargetController.missing"
    assert edge.resolved is False
    assert graph.has_unresolved is True
    assert any("method was not found" in warning.message for warning in graph.warnings)


@pytest.mark.parametrize(
    "entry",
    ("../outside.cls", "force-app/../../outside.cls", "/absolute.cls"),
)
def test_entry_paths_reject_traversal_and_absolute_paths(tmp_path: Path, entry: str) -> None:
    root = tmp_path / "repository"
    root.mkdir()

    with pytest.raises(ValueError, match="paths"):
        build_salesforce_dependency_graph(root, (entry,), content_revision(root))


def test_supported_symlink_escaping_repository_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    outside = _write(
        tmp_path,
        "outside.cls",
        "public class Outside {}",
    )
    link = root / "force-app/main/default/classes/Escape.cls"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError as exc:  # pragma: no cover - platform-specific fallback
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(PolicyViolation, match="symlink"):
        build_salesforce_dependency_graph(
            root,
            ("force-app/main/default/classes/Escape.cls",),
            "sha256:" + "0" * 64,
        )


def test_unsupported_existing_entry_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    readme = _write(root, "README.md", "not Salesforce metadata")

    with pytest.raises(ValueError, match="unsupported Salesforce metadata"):
        build_salesforce_dependency_graph(
            root,
            (readme.relative_to(root).as_posix(),),
            content_revision(root),
        )
