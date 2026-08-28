import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "salesforce" / "account-contact-explorer"
INPUT = FIXTURE_ROOT / "input"
METADATA_NAMESPACE = "http://soap.sforce.com/2006/04/metadata"


def contract() -> dict[str, object]:
    value = yaml.safe_load((FIXTURE_ROOT / "fixture.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_fixture_declares_only_legacy_input() -> None:
    fixture = contract()

    assert "expected" not in fixture
    assert not (FIXTURE_ROOT / "expected").exists()
    source = fixture["source"]
    assert isinstance(source, dict)
    assert source["root"] == "input"
    for key in ("entrypoint", "controller", "unit_test", "permission_set"):
        path = source[key]
        assert isinstance(path, str)
        assert (INPUT / path).is_file()


def test_source_api_version_is_consistent() -> None:
    sfdx = json.loads((INPUT / "sfdx-project.json").read_text(encoding="utf-8"))
    assert sfdx["sourceApiVersion"] == "67.0"

    versioned_roots = {"ApexClass", "ApexPage"}
    for path in INPUT.rglob("*-meta.xml"):
        root = ET.parse(path).getroot()
        local_name = root.tag.rsplit("}", 1)[-1]
        if local_name in versioned_roots:
            assert root.findtext(f"{{{METADATA_NAMESPACE}}}apiVersion") == "67.0", path


def test_legacy_controller_is_read_only_bounded_and_explicitly_secure() -> None:
    fixture = contract()
    source = fixture["source"]
    assert isinstance(source, dict)
    controller = source["controller"]
    assert isinstance(controller, str)
    apex = (INPUT / controller).read_text(encoding="utf-8")

    assert "public with sharing class" in apex
    assert apex.count("WITH USER_MODE") == 2
    assert "without sharing" not in apex
    assert "WITH SYSTEM_MODE" not in apex
    assert "Database.query" not in apex
    assert not re.search(r"(?im)^\s*(insert|update|upsert|delete|undelete|merge)\b", apex)
    assert "LIMIT :MAX_ACCOUNTS" in apex
    assert "LIMIT :MAX_CONTACTS" in apex


def test_source_permission_set_is_read_only_and_legacy_scoped() -> None:
    fixture = contract()
    source = fixture["source"]
    assert isinstance(source, dict)
    permission_set = source["permission_set"]
    assert isinstance(permission_set, str)
    root = ET.parse(INPUT / permission_set).getroot()
    namespace = {"m": METADATA_NAMESPACE}

    for permission in root.findall("m:objectPermissions", namespace):
        assert permission.findtext("m:allowRead", namespaces=namespace) == "true"
        for operation in ("allowCreate", "allowEdit", "allowDelete", "modifyAllRecords"):
            assert permission.findtext(f"m:{operation}", namespaces=namespace) == "false"
    classes = {
        access.findtext("m:apexClass", namespaces=namespace)
        for access in root.findall("m:classAccesses", namespace)
    }
    assert classes == {"LegacyAccountContactExplorerController"}


def test_input_uses_only_public_synthetic_names() -> None:
    legacy_test = (
        INPUT / "force-app/main/default/classes/LegacyAcctContactExplorerCtrlTest.cls"
    ).read_text(encoding="utf-8")

    for value in ("Skynet", "Weyland-Yutani", "Grace", "Hopper", "Ada", "Lovelace"):
        assert f"'{value}'" in legacy_test


def test_behavior_security_and_human_gate_remain_scenario_requirements() -> None:
    fixture = contract()
    behavior = fixture["behavior"]
    security = fixture["security"]
    decision = fixture["decision_required"]
    validation = fixture["validation"]
    assert isinstance(behavior, dict)
    assert isinstance(security, dict)
    assert isinstance(decision, dict)
    assert isinstance(validation, dict)

    accounts = behavior["accounts"]
    contacts = behavior["contacts"]
    interaction = behavior["interaction"]
    assert isinstance(accounts, dict)
    assert isinstance(contacts, dict)
    assert isinstance(interaction, dict)
    assert accounts["maximum"] == 50
    assert contacts["maximum"] == 100
    assert contacts["fields"] == ["FirstName", "LastName", "Email", "Phone"]
    assert all(interaction.values())
    assert security["sharing_model"] == "with_sharing"
    assert security["query_access_mode"] == "user_mode"
    assert security["dml_allowed"] is False
    assert security["external_calls"] is False
    assert security["secrets"] is False
    assert decision["action"] == "destructive_change"
    assert validation["final_state"] == "source_fixture_ready_for_model_generation"
