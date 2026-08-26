import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "salesforce" / "account-contact-explorer"
INPUT = FIXTURE_ROOT / "input"
EXPECTED = FIXTURE_ROOT / "expected"
METADATA_NAMESPACE = "http://soap.sforce.com/2006/04/metadata"


def contract():
    return yaml.safe_load((FIXTURE_ROOT / "fixture.yaml").read_text(encoding="utf-8"))


def test_declared_fixture_paths_exist():
    fixture = contract()
    for key in ("entrypoint", "controller", "unit_test", "permission_set"):
        assert (INPUT / fixture["source"][key]).exists()
    for key in ("component", "controller", "unit_test", "jest_test", "permission_set"):
        assert (EXPECTED / fixture["expected"][key]).exists()
    assert (EXPECTED / fixture["expected"]["package_manifest"]).exists()
    for category in ("preserved_legacy_files", "added_files", "modified_files"):
        for path in fixture["expected"][category]:
            assert (EXPECTED / path).exists(), path


def test_legacy_artifacts_are_preserved_byte_for_byte():
    for relative_path in contract()["expected"]["preserved_legacy_files"]:
        assert (INPUT / relative_path).read_bytes() == (EXPECTED / relative_path).read_bytes()


def test_api_version_is_consistent_for_version_bearing_metadata():
    for project_root in (INPUT, EXPECTED):
        sfdx = json.loads((project_root / "sfdx-project.json").read_text(encoding="utf-8"))
        assert sfdx["sourceApiVersion"] == "67.0"

    package = ET.parse(EXPECTED / "manifest" / "package.xml").getroot()
    assert package.findtext(f"{{{METADATA_NAMESPACE}}}version") == "67.0"

    versioned_roots = {"ApexClass", "ApexPage", "LightningComponentBundle"}
    for path in FIXTURE_ROOT.rglob("*-meta.xml"):
        root = ET.parse(path).getroot()
        local_name = root.tag.rsplit("}", 1)[-1]
        if local_name in versioned_roots:
            assert root.findtext(f"{{{METADATA_NAMESPACE}}}apiVersion") == "67.0", path


def test_controllers_are_read_only_bounded_and_explicitly_secure():
    for project_root in (INPUT, EXPECTED):
        for path in project_root.glob("force-app/main/default/classes/*Controller.cls"):
            source = path.read_text(encoding="utf-8")
            assert "public with sharing class" in source
            assert source.count("WITH USER_MODE") == 2
            assert "without sharing" not in source
            assert "WITH SYSTEM_MODE" not in source
            assert "Database.query" not in source
            assert not re.search(r"(?im)^\s*(insert|update|upsert|delete|undelete|merge)\b", source)
            assert "LIMIT :MAX_ACCOUNTS" in source
            assert "LIMIT :MAX_CONTACTS" in source


def test_permission_set_is_read_only_and_grants_exact_controller_access():
    permission_path = (
        EXPECTED
        / "force-app/main/default/permissionsets/AccountContactExplorerUser.permissionset-meta.xml"
    )
    root = ET.parse(permission_path).getroot()
    namespace = {"m": METADATA_NAMESPACE}
    for permission in root.findall("m:objectPermissions", namespace):
        assert permission.findtext("m:allowRead", namespaces=namespace) == "true"
        for operation in ("allowCreate", "allowEdit", "allowDelete", "modifyAllRecords"):
            assert permission.findtext(f"m:{operation}", namespaces=namespace) == "false"
    classes = {
        access.findtext("m:apexClass", namespaces=namespace)
        for access in root.findall("m:classAccesses", namespace)
    }
    assert classes == {
        "LegacyAccountContactExplorerController",
        "AccountContactExplorerController",
    }


def test_lwc_exposure_and_retirement_remain_bounded():
    metadata = ET.parse(
        EXPECTED
        / "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js-meta.xml"
    ).getroot()
    targets = {
        target.text
        for target in metadata.findall(
            f"{{{METADATA_NAMESPACE}}}targets/{{{METADATA_NAMESPACE}}}target"
        )
    }
    assert targets == {"lightning__AppPage", "lightning__Tab"}
    fixture = contract()
    assert fixture["expected"]["destructive_changes"] == []
    assert fixture["expected"]["retirement_candidates"]
    assert fixture["decision_required"]["action"] == "destructive_change"


def test_fun_names_are_canonical_across_legacy_and_lwc_fixtures():
    data_root = EXPECTED / "force-app/main/default/lwc/accountContactExplorer/__tests__/data"
    accounts = json.loads((data_root / "accounts.json").read_text(encoding="utf-8"))
    contacts = json.loads((data_root / "contacts.json").read_text(encoding="utf-8"))

    assert [record["Name"] for record in accounts] == ["Skynet", "Weyland-Yutani"]
    assert [(record["LastName"], record["FirstName"]) for record in contacts] == [
        ("Hopper", "Grace"),
        ("Lovelace", "Ada"),
    ]
    legacy_test = (
        INPUT / "force-app/main/default/classes/LegacyAccountContactExplorerControllerTest.cls"
    ).read_text(encoding="utf-8")
    for value in ("Skynet", "Weyland-Yutani", "Grace", "Hopper", "Ada", "Lovelace"):
        assert f"'{value}'" in legacy_test

    jest_test = (
        EXPECTED
        / "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    ).read_text(encoding="utf-8")
    assert "label: ACCOUNTS[0].Name" in jest_test
    assert "label: ACCOUNTS[1].Name" in jest_test


def test_expected_sources_bind_stale_response_and_query_cap_claims():
    fixture = contract()
    assert (
        fixture["behavior"]["interaction"]["stale_response_ignored_after_selection_change"] is True
    )

    component = (
        EXPECTED / "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    ).read_text(encoding="utf-8")
    assert component.count("this.loadRequestGeneration += 1") == 2
    assert component.count("this.isCurrentRequest(accountId, requestGeneration)") == 3

    jest_source = (EXPECTED / fixture["expected"]["jest_test"]).read_text(encoding="utf-8")
    assert "createApexTestWireAdapter(jest.fn())" in jest_source
    assert "getContacts.mockReset()" in jest_source
    assert "firstRequest.resolve([" in jest_source
    assert "secondRequest.resolve(CONTACTS)" in jest_source
    assert "toHaveBeenNthCalledWith(1" in jest_source
    assert "toHaveBeenNthCalledWith(2" in jest_source
    assert "expect(datatable.data[0].FirstName).not.toBe('Stale');" in jest_source
    assert ".textContent).not.toBe('Stale')" not in jest_source

    apex_test = (EXPECTED / fixture["expected"]["unit_test"]).read_text(encoding="utf-8")
    assert "AccountContactExplorerController.MAX_ACCOUNTS + 2" in apex_test
    assert "AccountContactExplorerController.MAX_CONTACTS + 2" in apex_test
    assert "System.assert(!returnedIds.contains(foreignContact.Id))" in apex_test
    assert "System.assertEquals('Alpha Tie', contacts[0].LastName)" in apex_test
    assert "System.assertEquals('Ada', contacts[0].FirstName)" in apex_test
    assert "System.assertEquals('Zoe', contacts[1].FirstName)" in apex_test
