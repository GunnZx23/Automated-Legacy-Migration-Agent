from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from legacy_migration_agent.core import run_session as run_session_module
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.run_session import (
    RUNTIME_STATE_PATHS,
    AgentDefinitionDigests,
    AgentRunSession,
)

REQUEST_DIGEST = "sha256:" + "1" * 64
AGENT_DIGESTS = AgentDefinitionDigests(
    architect="sha256:" + "a" * 64,
    engineer="sha256:" + "b" * 64,
    validator="sha256:" + "c" * 64,
)


def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    source = project / "source"
    source.mkdir(parents=True)
    (source / "Legacy.cls").write_text("public class Legacy {}\n", encoding="utf-8")
    return project, source


def _initialize(
    project: Path,
    *,
    run_dir: Path = Path(".runs/run-1"),
    run_id: str = "run-1",
    thread_id: str = "thread-run-1",
    source_root: str = "source",
    slice_id: str = "salesforce-vf-to-lwc",
    provider_id: str = "offline-test",
    model_id: str = "structured-agent/v1",
) -> AgentRunSession:
    return AgentRunSession.initialize(
        project,
        run_dir,
        run_id=run_id,
        thread_id=thread_id,
        slice_id=slice_id,
        source_root=source_root,
        request_digest=REQUEST_DIGEST,
        agent_definition_digests=AGENT_DIGESTS,
        provider_id=provider_id,
        model_id=model_id,
    )


def test_initialize_and_load_portable_oracle_isolated_session(tmp_path: Path) -> None:
    project, source = _project(tmp_path)
    secret = "provider-secret-value"
    session = _initialize(project, provider_id=f"api_key={secret}")

    assert session.context.provider_id == "api_key=[REDACTED]"
    assert session.context.run_id == "run-1"
    assert session.context.thread_id == "thread-run-1"
    assert session.context.source_root == "source"
    assert session.context.run_directory == ".runs/run-1"
    assert session.context.agent_definition_digests == AGENT_DIGESTS
    assert stat.S_IMODE(session.checkpoint_path.stat().st_mode) == 0o600
    assert session.checkpoint_path.read_bytes() == b""
    for directory in (
        session.run_dir,
        session.state_dir,
        session.runtime_anchors_dir,
        session.evidence_dir,
        session.workspaces_dir,
        session.scratch_dir,
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    context_text = (session.evidence_dir / "run-context.json").read_text(encoding="utf-8")
    runtime_text = (session.state_dir / "runtime.json").read_text(encoding="utf-8")
    assert secret not in context_text
    assert str(project) not in context_text
    assert str(source) not in context_text
    assert str(project) not in runtime_text
    assert str(source) not in runtime_text
    assert not hasattr(session, "fixture_root")
    assert not hasattr(session, "template_root")
    assert not hasattr(session, "expected_root")
    assert not hasattr(session, "golden_root")

    initialized = session.store.read_json("indexes/initialized.json")
    assert initialized["excluded_runtime_state"] == list(RUNTIME_STATE_PATHS)
    assert [entry["path"] for entry in initialized["artifacts"]] == ["run-context.json"]
    assert all(
        not entry["path"].startswith(("state/", "scratch/", "workspaces/"))
        for entry in initialized["artifacts"]
    )

    loaded = AgentRunSession.load(project, session.run_dir)
    assert loaded.context == session.context
    assert loaded.source_root == source.resolve()
    loaded.verify_source_revision()
    loaded.verify_index("initialized", exact=True)


def test_runtime_anchor_is_state_only_immutable_and_reloaded(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    payload = {"graph_key": REQUEST_DIGEST, "chain_digest": "sha256:" + "d" * 64}

    session.bind_runtime_anchor("graph-cache", payload)
    session.bind_runtime_anchor("graph-cache", payload)
    session.verify_runtime_anchor("graph-cache", payload)
    anchor = session.runtime_anchors_dir / "graph-cache.json"
    assert stat.S_IMODE(anchor.stat().st_mode) == 0o600
    assert not tuple(session.evidence_dir.rglob("*anchor*"))

    loaded = AgentRunSession.load(project, session.run_dir)
    assert loaded.has_runtime_anchor("graph-cache")
    loaded.verify_runtime_anchor("graph-cache", payload)
    with pytest.raises(PolicyViolation, match="runtime evidence anchor digest mismatch"):
        loaded.verify_runtime_anchor("graph-cache", {"changed": True})
    with pytest.raises(PolicyViolation, match="immutable runtime anchor differs"):
        loaded.bind_runtime_anchor("graph-cache", {"changed": True})


def test_exclusive_json_is_invisible_until_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "anchor.json"
    write_started = threading.Event()
    release_write = threading.Event()
    errors: list[BaseException] = []
    original_write = os.write

    def delayed_write(descriptor: int, payload: bytes | memoryview) -> int:
        write_started.set()
        if not release_write.wait(timeout=10):
            raise TimeoutError("test did not release the staged JSON write")
        return original_write(descriptor, payload)

    monkeypatch.setattr(run_session_module.os, "write", delayed_write)

    def publish() -> None:
        try:
            run_session_module._write_exclusive_json(target, {"ready": True})
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    thread = threading.Thread(target=publish)
    thread.start()
    assert write_started.wait(timeout=10)
    try:
        assert not target.exists()
    finally:
        release_write.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert json.loads(target.read_text(encoding="utf-8")) == {"ready": True}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_concurrent_identical_runtime_anchor_publication_is_idempotent(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    loaded = AgentRunSession.load(project, session.run_dir)
    payload = {"graph_key": REQUEST_DIGEST, "chain_digest": "sha256:" + "d" * 64}
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def bind(bound_session: AgentRunSession) -> None:
        try:
            barrier.wait(timeout=10)
            bound_session.bind_runtime_anchor("graph-cache", payload)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = (
        threading.Thread(target=bind, args=(session,)),
        threading.Thread(target=bind, args=(loaded,)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    session.verify_runtime_anchor("graph-cache", payload)
    assert tuple(session.runtime_anchors_dir.iterdir()) == (
        session.runtime_anchors_dir / "graph-cache.json",
    )


@pytest.mark.parametrize("reserved", ("expected", "GOLDEN", "Oracle"))
def test_source_root_rejects_oracle_path_segments(tmp_path: Path, reserved: str) -> None:
    project = tmp_path / "project"
    source = project / "sources" / reserved
    source.mkdir(parents=True)
    (source / "answer.txt").write_text("oracle bytes", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="expected, golden, or oracle"):
        _initialize(project, source_root=f"sources/{reserved}")


def test_source_tree_rejects_nested_oracle_content(tmp_path: Path) -> None:
    project, source = _project(tmp_path)
    hidden_oracle = source / "nested" / "golden"
    hidden_oracle.mkdir(parents=True)
    (hidden_oracle / "answer.txt").write_text("answer", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="source tree contains"):
        _initialize(project)


def test_source_oracle_is_rejected_before_regular_file_bytes_are_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, source = _project(tmp_path)
    oracle = source / "nested" / "oracle"
    oracle.mkdir(parents=True)
    (oracle / "answer.txt").write_text("answer", encoding="utf-8")
    actual_open = os.open
    forbidden_opens: list[str] = []

    def guarded_open(path, *args, **kwargs):
        rendered = os.fspath(path)
        parts = Path(rendered).parts if isinstance(rendered, (str, bytes)) else ()
        if any(str(part).casefold() == "oracle" for part in parts):
            forbidden_opens.append(str(rendered))
            raise AssertionError("oracle bytes must never be opened")
        return actual_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    with pytest.raises(PolicyViolation, match="source tree contains"):
        _initialize(project)
    assert forbidden_opens == []


def test_slice_id_is_bounded_before_run_directory_creation(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)

    with pytest.raises(ValueError, match="160 characters|String should match pattern"):
        _initialize(project, slice_id="s" * 161)

    assert not (project / ".runs").exists()


def test_run_and_thread_identifiers_are_distinct_immutable_and_reloaded(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(
        project,
        run_id="stable-run-id",
        thread_id="stable-thread-id",
    )

    with pytest.raises(ValidationError, match="frozen"):
        session.context.run_id = "rewritten"  # type: ignore[misc]
    loaded = AgentRunSession.load(project, session.run_dir)
    assert loaded.context.run_id == "stable-run-id"
    assert loaded.context.thread_id == "stable-thread-id"

    other_project, _ = _project(tmp_path / "other")
    with pytest.raises(ValueError, match="distinct"):
        _initialize(
            other_project,
            run_id="same-identifier",
            thread_id="same-identifier",
        )
    assert not (other_project / ".runs").exists()


def test_source_rejects_escape_symlink_and_special_file(tmp_path: Path) -> None:
    project, source = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Outside.cls").write_text("public class Outside {}", encoding="utf-8")

    with pytest.raises((ValueError, PolicyViolation), match="paths|project-relative"):
        _initialize(project, source_root="../outside")

    linked = project / "linked-source"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PolicyViolation, match="symlink"):
        _initialize(project, source_root="linked-source", run_dir=Path(".runs/link"))

    source_link = source / "linked.cls"
    source_link.symlink_to(outside / "Outside.cls")
    with pytest.raises(PolicyViolation, match="symlinks"):
        _initialize(project, run_dir=Path(".runs/source-link"))
    source_link.unlink()

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    os.mkfifo(source / "unsafe.pipe")
    with pytest.raises(PolicyViolation, match="special file"):
        _initialize(project, run_dir=Path(".runs/source-special"))


def test_run_directory_rejects_escape_reuse_symlink_and_source_nesting(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(PolicyViolation, match="inside the project root"):
        _initialize(project, run_dir=outside / "run")

    session = _initialize(project)
    with pytest.raises(PolicyViolation, match="must not already exist"):
        _initialize(project)
    assert session.run_dir.is_dir()

    linked_parent = project / "linked-runs"
    linked_parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PolicyViolation, match="unsafe component"):
        _initialize(project, run_dir=Path("linked-runs/run"))
    assert not (outside / "run").exists()

    with pytest.raises(PolicyViolation, match="inside the source root"):
        _initialize(project, run_dir=Path("source/.runs/run"))


def test_load_rejects_context_tamper_and_stale_source(tmp_path: Path) -> None:
    project, source = _project(tmp_path)
    context_session = _initialize(project, run_dir=Path(".runs/context-tamper"))
    context_path = context_session.evidence_dir / "run-context.json"
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    payload["slice_id"] = "changed-slice"
    context_path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(context_path, 0o600)

    with pytest.raises(PolicyViolation, match="context digest"):
        AgentRunSession.load(project, context_session.run_dir)

    stale_session = _initialize(project, run_dir=Path(".runs/stale-source"))
    (source / "Legacy.cls").write_text("public class Changed {}\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="source content revision changed"):
        AgentRunSession.load(project, stale_session.run_dir)


def test_load_rejects_unsafe_checkpoint_and_runtime_directories(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    permissive = _initialize(project, run_dir=Path(".runs/permissive-checkpoint"))
    os.chmod(permissive.checkpoint_path, 0o644)
    with pytest.raises(PolicyViolation, match="mode 0600"):
        AgentRunSession.load(project, permissive.run_dir)

    linked = _initialize(project, run_dir=Path(".runs/linked-checkpoint"))
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"")
    linked.checkpoint_path.unlink()
    linked.checkpoint_path.symlink_to(outside)
    with pytest.raises(PolicyViolation, match="regular non-symlink"):
        AgentRunSession.load(project, linked.run_dir)

    swapped = _initialize(project, run_dir=Path(".runs/linked-scratch"))
    outside_directory = tmp_path / "outside-scratch"
    outside_directory.mkdir()
    swapped.scratch_dir.rmdir()
    swapped.scratch_dir.symlink_to(outside_directory, target_is_directory=True)
    with pytest.raises(PolicyViolation, match="symlink"):
        AgentRunSession.load(project, swapped.run_dir)

    malformed = _initialize(project, run_dir=Path(".runs/malformed-runtime"))
    runtime_path = malformed.state_dir / "runtime.json"
    runtime_path.write_text("{}\n", encoding="utf-8")
    os.chmod(runtime_path, 0o600)
    with pytest.raises(PolicyViolation, match="runtime binding is malformed"):
        AgentRunSession.load(project, malformed.run_dir)


def test_load_and_recurring_verification_require_private_directory_modes(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    os.chmod(session.evidence_dir, 0o777)

    with pytest.raises(PolicyViolation, match="evidence directory must use mode 0700"):
        AgentRunSession.load(project, session.run_dir)
    with pytest.raises(PolicyViolation, match="run session directory must use mode 0700"):
        session.verify_index("initialized", exact=True)


def test_lifecycle_indexes_are_immutable_exact_and_runtime_free(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    session.store.write_json("model-runs/architect.json", {"output_digest": REQUEST_DIGEST})

    session.verify_index("initialized", exact=False)
    with pytest.raises(PolicyViolation, match="exact artifact set"):
        session.verify_index("initialized", exact=True)

    session.write_index("completed")
    session.verify_index("completed", exact=True)
    completed = session.store.read_json("indexes/completed.json")
    indexed_paths = tuple(entry["path"] for entry in completed["artifacts"])
    assert indexed_paths == (
        "indexes/initialized.json",
        "model-runs/architect.json",
        "run-context.json",
    )
    assert completed["excluded_runtime_state"] == list(RUNTIME_STATE_PATHS)

    artifact_path = session.evidence_dir / "model-runs/architect.json"
    artifact_path.write_text('{"output_digest":"sha256:' + "f" * 64 + '"}\n', encoding="utf-8")
    os.chmod(artifact_path, 0o600)
    with pytest.raises(PolicyViolation, match="artifact digest mismatch"):
        session.verify_index("completed", exact=True)


def test_lifecycle_index_rejects_duplicates_and_self_reference(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="unique"):
        session.write_index("duplicate", ("run-context.json", "run-context.json"))
    with pytest.raises(PolicyViolation, match="cannot include itself"):
        session.write_index("self", ("indexes/self.json",))


def test_portable_store_rejects_credentials_absolute_paths_and_runtime_state(
    tmp_path: Path,
) -> None:
    project, source = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="credential"):
        session.store.write_json("unsafe-secret.json", {"api_key": "do-not-store-this"})
    for key in ("secret", "credential", "auth_token", "sfdx_auth_url"):
        with pytest.raises(PolicyViolation, match="credential"):
            session.store.write_json(
                f"unsafe-{key}.json",
                {key: "do-not-store-this"},
            )
    for index, token in enumerate(
        (
            "ghp_abcdefghijklmnopqrstuvwxyz1234567890AB",
            "github_pat_11AA0_this_is_a_long_fine_grained_token_value",
            "xoxb" + "-123456789012-abcdefghijklmnop",
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_value_12345",
        )
    ):
        with pytest.raises(PolicyViolation, match="credential"):
            session.store.write_json(
                f"unsafe-token-shape-{index}.json",
                {"content": f"pasted value {token}"},
            )
    with pytest.raises(PolicyViolation, match="absolute project or source path"):
        session.store.write_json("unsafe-path.json", {"source": str(source.resolve())})
    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json("unsafe-posix-path.json", {"source": "/etc/passwd"})
    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json(
            "unsafe-windows-path.json",
            {"source": r"C:\Users\alice\secret.txt"},
        )
    with pytest.raises(PolicyViolation, match="runtime state"):
        session.store.write_json("state/checkpoint.json", {"safe": False})

    session.store.write_json(
        "safe-citations.json",
        {
            "url": "https://developer.salesforce.com/docs/platform/lwc/guide",
            "citation_id": "wiki:salesforce-security-section-2",
        },
    )
    session.store.write_json(
        "safe-request-token.json",
        {
            "token": "requestGeneration",
            "content": (
                "const token = ++this.requestGeneration;\n"
                "const accessToken = response.accessToken;\n"
                "Authorization: Bearer ${secure::token}\n"
                "api_key: ${secure::sk-service-key}\n"
            ),
        },
    )

    assert not (session.evidence_dir / "unsafe-secret.json").exists()
    assert not (session.evidence_dir / "unsafe-path.json").exists()
    assert not (session.evidence_dir / "unsafe-posix-path.json").exists()
    assert not (session.evidence_dir / "unsafe-windows-path.json").exists()
    assert (session.evidence_dir / "safe-citations.json").is_file()
    assert (session.evidence_dir / "safe-request-token.json").is_file()
    assert not (session.evidence_dir / "state").exists()


def test_portable_store_allows_repository_paths_with_underscored_segments(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    repository_path = (
        "force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js"
    )

    session.store.write_json("repository-path.json", {"approved_paths": [repository_path]})

    assert session.store.read_json("repository-path.json") == {"approved_paths": [repository_path]}
    source_text = "  export default class Example {}\n"
    session.store.write_json("source-text.json", {"content": source_text})
    assert session.store.read_json("source-text.json") == {"content": source_text}
    with pytest.raises(PolicyViolation, match="unredacted credential"):
        session.store.write_json(
            "secret-source-text.json",
            {"content": "  api_key=do-not-store-this\n"},
        )


def test_portable_store_persists_historical_engineer_source_syntax(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    engineer_like_artifact = {
        "model_outcome": {
            "result": {
                "kind": "file_plan",
                "file_plan": {
                    "updates": [
                        {
                            "path": "force-app/main/default/classes/ExampleController.cls",
                            "content": (
                                "/** Bounded additive controller. */\n"
                                "public with sharing class ExampleController {\n"
                                "    // Return a safe, stable response.\n"
                                "    public static String value() { return 'ok'; }\n"
                                "}\n"
                            ),
                        },
                        {
                            "path": "force-app/main/default/lwc/example/example.js",
                            "content": (
                                "import { LightningElement } from 'lwc';\n"
                                "// Keep validation local to the component.\n"
                                "const STATUS = /^(?:OPEN|CLOSED|ALL)$/i;\n"
                                "const WORDS = /[A-Za-z]+(?:-[A-Za-z]+)*/g;\n"
                                "export default class Example extends LightningElement {\n"
                                "    isStatus(value) { return STATUS.test(value); }\n"
                                "    words(value) { return value.match(WORDS); }\n"
                                "}\n"
                            ),
                        },
                        {
                            "path": "force-app/main/default/lwc/example/example.css",
                            "content": (
                                "/* Keep the generated component readable. */\n"
                                ".container { inline-size: 100%; }\n"
                            ),
                        },
                    ],
                    "assumptions": ["All output paths are repository-relative."],
                },
            }
        }
    }

    session.store.write_json("engineer-source-syntax.json", engineer_like_artifact)

    assert session.store.read_json("engineer-source-syntax.json") == engineer_like_artifact


@pytest.mark.parametrize("field_name", ("content", "selected_content", "unified_diff"))
def test_all_source_bearing_fields_allow_language_slash_syntax(
    tmp_path: Path,
    field_name: str,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    source_text = (
        "/** Explain the bounded implementation. */\n"
        "// Preserve the request-generation guard.\n"
        "const SAFE_STATUS = /^(?:OPEN|CLOSED|ALL)$/i;\n"
        "const ratio = numerator / denominator;\n"
        "</template>\n"
    )
    stored_text = source_text
    if field_name == "unified_diff":
        stored_text = (
            "--- a/force-app/main/default/lwc/example/example.js\n"
            "+++ b/force-app/main/default/lwc/example/example.js\n"
            + "".join(f"+{line}\n" for line in source_text.rstrip("\n").split("\n"))
        )

    session.store.write_json(
        f"source-bearing-{field_name}.json",
        {field_name: stored_text},
    )

    assert session.store.read_json(f"source-bearing-{field_name}.json") == {field_name: stored_text}


@pytest.mark.parametrize("field_name", ("content", "selected_content", "unified_diff"))
@pytest.mark.parametrize(
    "source_text",
    (
        '<a href="/data-role">role</a>\n',
        '<a href="/data-state">state</a>\n',
        "</data>\n",
        r"const complete = /^[\s\S]*$/;" + "\n",
        r"const newline = '\n'; const tab = '\t';" + "\n",
        "//setup/mocks\n",
        "const ratio = total /data.length;\n",
        'const socket = "wss://example.test/events";\n',
        'const resource = "salesforce+asset://resource/banner";\n',
        '<flow name="get:/users/{userId}:api-config"/>\n',
        "const routes = ['/data/accounts', '/run/{jobId}', '/media/banner.svg', '/home'];\n",
    ),
)
def test_source_bearing_fields_allow_non_path_slash_syntax_and_public_routes(
    tmp_path: Path,
    field_name: str,
    source_text: str,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    stored_text = source_text
    if field_name == "unified_diff":
        stored_text = (
            "--- a/generated/source.txt\n"
            "+++ b/generated/source.txt\n"
            f"+{source_text.rstrip(chr(10))}"
        )

    session.store.write_json(
        f"portable-source-{field_name}.json",
        {field_name: stored_text},
    )

    assert session.store.read_json(f"portable-source-{field_name}.json") == {
        field_name: stored_text
    }


def test_portable_store_allows_non_file_uri_schemes_in_structured_fields(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    value = {
        "event_endpoint": "wss://example.test/events",
        "resource_locator": "salesforce+asset://resource/banner",
        "local_looking_uri": "custom://host/tmp/candidate",
        "repository_assignment": "value=force-app/main/default/classes/Example.cls",
    }

    session.store.write_json("portable-uri-schemes.json", value)

    assert session.store.read_json("portable-uri-schemes.json") == value


@pytest.mark.parametrize(
    "source_text",
    (
        "// Read /private/tmp/candidate.txt before continuing.\n",
        "/* Never copy /etc/passwd into generated output. */\n",
        'const localHome = "/home/alice/candidate.js";\n',
        'const localSecret = "/secrets/candidate.txt";\n',
        'const localSystem = "/System/Library/private.bundle";\n',
        'const localPath = "/workspace/private/candidate.js";\n',
        "Read /tmp.\n",
        "Read /tmp!\n",
        "Read `/tmp`.\n",
        'const mixedCaseTmp = "/TMP/private/file";\n',
        'const mixedCaseUsers = "/users/alice/file";\n',
        'const mixedCaseEtc = "/ETC/passwd";\n',
        'const mixedCasePrivate = "/pRiVaTe/tmp/x";\n',
        'const mixedCaseHome = "/HOME/alice/x";\n',
        'const singleSlashFileUri = "file:/tmp/candidate.js";\n',
        'const localUri = "file:///tmp/candidate.js";\n',
        'const forwardUncPath = "//server/share/candidate.js";\n',
        r'const windowsPath = "C:\Users\alice\candidate.js";' + "\n",
        r'const uncPath = "\\server\share\candidate.js";' + "\n",
        r'const rootRelativePath = "\Users\alice\candidate.js";' + "\n",
        "const assignedPath=/private/tmp/candidate.js;\n",
    ),
)
@pytest.mark.parametrize("field_name", ("content", "selected_content", "unified_diff"))
def test_portable_store_rejects_local_paths_embedded_in_source_syntax(
    tmp_path: Path,
    source_text: str,
    field_name: str,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json(
            "unsafe-engineer-source.json",
            {field_name: source_text},
        )


@pytest.mark.parametrize("field_name", ("content", "selected_content", "unified_diff"))
def test_portable_store_rejects_exact_project_path_inside_source_comment(
    tmp_path: Path,
    field_name: str,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="absolute project or source path"):
        session.store.write_json(
            "unsafe-project-path-source.json",
            {field_name: f"// Inspect generated evidence under {project}.\n"},
        )


def test_portable_path_rejection_exposes_only_fixed_diagnostic_metadata(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(run_session_module.PortableEvidencePolicyViolation) as captured:
        session.store.write_json(
            "unsafe-diagnostic-source.json",
            {"content": "read /etc/passwd"},
        )

    assert captured.value.evidence_category == "local_absolute_path"
    assert captured.value.field_class == "source_bearing"


def test_portable_store_allows_only_exact_dev_null_unified_diff_headers(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    accepted = (
        "diff --git a/generated/file.txt b/generated/file.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/generated/file.txt\n"
        "@@ -0,0 +1 @@\n"
        "+portable content"
    )
    session.store.write_json("new-file-diff.json", {"unified_diff": accepted})
    deleted = "--- a/generated/file.txt\n+++ /dev/null\n-old content"
    session.store.write_json("deleted-file-diff.json", {"unified_diff": deleted})

    near_misses = (
        "--- /dev/null/child",
        "---  /dev/null",
        "prefix --- /dev/null",
        "--- /dev/null suffix",
        "--- a/file.txt\n+++ b/file.txt\n+/dev/null",
    )
    for index, value in enumerate(near_misses):
        with pytest.raises(PolicyViolation, match="local absolute path"):
            session.store.write_json(
                f"unsafe-diff-{index}.json",
                {"unified_diff": value},
            )
    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json("unsafe-other-field.json", {"source": "--- /dev/null"})


@pytest.mark.parametrize(
    "field_name",
    (
        "assumptions",
        "canonical_description",
        "concerns",
        "content",
        "description",
        "implementation_contract",
        "public_concerns",
        "reason",
        "recommendation",
        "required_implementation_contract",
        "selected_content",
        "summary",
        "unresolved_questions",
    ),
)
def test_opaque_portable_text_allows_routes_but_rejects_local_filesystem_paths(
    tmp_path: Path,
    field_name: str,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    route_text = (
        "Repository target /force-app/main/default/lwc/caseConsole is additive.\n"
        '<http:listener-config basePath="/api"/>\n'
        '<http:listener path="/customers/{customerId}/status"/>\n'
    )
    session.store.write_json(f"safe-route-{field_name}.json", {field_name: route_text})

    unsafe_values = (
        "read /etc/passwd",
        "read **/tmp/capstone.txt**",
        "read _/tmp/capstone.txt_",
        "read ___/tmp/capstone.txt___",
        "write /private/tmp/capstone.txt",
        r"read C:\Users\alice\secret.txt",
        "read file:///etc/passwd",
    )
    for index, value in enumerate(unsafe_values):
        with pytest.raises(PolicyViolation, match="local absolute path"):
            session.store.write_json(
                f"unsafe-{field_name}-{index}.json",
                {field_name: value},
            )


@pytest.mark.parametrize(
    "filesystem_root",
    (
        "Applications",
        "bin",
        "media",
        "mnt",
        "nix",
        "proc",
        "run",
        "sbin",
        "snap",
        "srv",
        "sys",
    ),
)
def test_opaque_portable_text_rejects_additional_local_filesystem_roots(
    tmp_path: Path,
    filesystem_root: str,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json(
            f"unsafe-root-{filesystem_root.casefold()}.json",
            {"summary": f"Read /{filesystem_root}/private/evidence.txt"},
        )


@pytest.mark.parametrize(
    "unsafe_value",
    (
        "read /workspace/private/evidence.txt",
        "path=/workspace/private/evidence.txt",
        "read /data/private/evidence.txt",
        "value=/data/private/evidence.txt",
        "read /local/private/evidence.txt",
        "read /api/../etc/passwd",
        "read /api/%2e%2e/etc/passwd",
        "read /force-app/../../Users/private/evidence.txt",
        "read /api//status",
        r"read /api\..\etc\passwd",
        "read /apiary/status",
        "read /force-appx/main/default",
        r"read \\server\share\evidence.txt",
        r"read \Users\alice\evidence.txt",
        "read //server/share/evidence.txt",
        "read file:///api",
        "read file:/tmp/private",
        'use basePath="/"',
        "use basePath=/",
        "use basePath = /",
    ),
)
def test_opaque_portable_text_rejects_non_allowlisted_and_unsafe_paths(
    tmp_path: Path,
    unsafe_value: str,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json("unsafe-opaque-path.json", {"summary": unsafe_value})


def test_structured_path_fields_remain_strict_when_narrative_fields_allow_routes(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    session.store.write_json("safe-route-summary.json", {"summary": "Call GET /api/status."})
    session.store.write_json(
        "safe-relative-glob-summary.json",
        {"summary": "Run Jest for **/__tests__/** files."},
    )
    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json("unsafe-structured-path.json", {"path": "/api/status"})


def test_narrative_markdown_inline_code_alternatives_are_not_paths(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    summaries = [
        "Preserve `OPEN`/`CLOSED`/`ALL` behavior.",
        "Call `getAccounts()`/`getCases(accountId, statusFilter)` explicitly.",
        "Enforce `with sharing`/`WITH USER_MODE`.",
    ]

    session.store.write_json("safe-inline-code-alternatives.json", {"summary": summaries})

    assert session.store.read_json("safe-inline-code-alternatives.json") == {"summary": summaries}


@pytest.mark.parametrize(
    "unsafe_value",
    (
        "Do not reinterpret `safe`/`/tmp/private.txt` as prose.",
        "Do not reinterpret `safe`/`/api/../etc/passwd` as prose.",
        "Do not reinterpret `safe`/`file:///tmp/private.txt` as prose.",
        r"Do not reinterpret `safe`/`C:\Users\alice\private.txt` as prose.",
        "Do not reinterpret `C:`/`Users`/`alice`/`private.txt` as prose.",
        r"Do not reinterpret `C:`\`Users`\`alice`\`private.txt` as prose.",
        r"Do not reinterpret `\\server`/`share`/`private.txt` as prose.",
        r"Do not reinterpret `\Users`/`alice`/`private.txt` as prose.",
        r"Do not reinterpret `safe`/`\\server\share\private.txt` as prose.",
    ),
)
def test_narrative_markdown_inline_code_near_misses_remain_rejected(
    tmp_path: Path,
    unsafe_value: str,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json("unsafe-inline-code-alternative.json", {"summary": unsafe_value})


@pytest.mark.parametrize("field_name", ("command", "locator", "path"))
@pytest.mark.parametrize("unsafe_value", ("value=/tmp/x", "value:/tmp/x"))
def test_structured_fields_reject_embedded_absolute_path_assignments(
    tmp_path: Path,
    field_name: str,
    unsafe_value: str,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json(
            "unsafe-structured-assignment.json",
            {field_name: unsafe_value},
        )


def test_assumptions_allow_bounded_salesforce_public_route_families(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    assumptions = [
        "Retain the Visualforce route root /apex.",
        "Preserve the legacy Visualforce route /apex/LegacyCaseManagementConsole.",
        "Preserve `/apex/LegacyCaseManagementConsole`!",
        "Preserve /apex/LegacyCaseManagementConsole?",
        "Query /services/data/v67.0/query through the authorized Salesforce session.",
        "Navigate to /lightning/n/AccountContactExplorer.",
        "Load the static asset at /resource/accountContactExplorerStyles.",
    ]

    session.store.write_json("salesforce-route-assumptions.json", {"assumptions": assumptions})

    assert session.store.read_json("salesforce-route-assumptions.json") == {
        "assumptions": assumptions
    }


@pytest.mark.parametrize(
    "unsafe_route",
    (
        "/apex/../private",
        "/apex/%2e%2e/private",
        "/apex//LegacyCaseManagementConsole",
        "/apex/LegacyCaseManagementConsole/Nested",
        "/apex/LegacyCaseManagementConsole?redirect=/tmp/private",
        "/apexx/LegacyCaseManagementConsole",
        r"/apex\..\private",
        "/services/data/../private",
        "/services/data/%2e%2e/private",
        "/lightning//Account",
        r"/resource\..\private",
    ),
)
def test_salesforce_public_route_families_reject_unsafe_segments(
    tmp_path: Path,
    unsafe_route: str,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json(
            "unsafe-salesforce-route-assumption.json",
            {"assumptions": [f"Use {unsafe_route}."]},
        )


def test_salesforce_public_route_is_still_rejected_in_structured_path_field(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json(
            "unsafe-structured-salesforce-route.json",
            {"path": "/services/data/v67.0/query"},
        )

    with pytest.raises(PolicyViolation, match="local absolute path"):
        session.store.write_json(
            "unsafe-structured-visualforce-route.json",
            {"path": "/apex/LegacyCaseManagementConsole"},
        )


@pytest.mark.parametrize(
    "field_name",
    ("concerns", "public_concerns", "reason", "recommendation", "summary", "unresolved_questions"),
)
def test_role_narrative_fields_still_reject_secrets(
    tmp_path: Path,
    field_name: str,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="unredacted credential"):
        session.store.write_json(
            f"unsafe-secret-{field_name}.json",
            {field_name: "api_key=do-not-store-this"},
        )


def test_role_narrative_fields_reject_the_exact_project_path(tmp_path: Path) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)

    with pytest.raises(PolicyViolation, match="absolute project or source path"):
        session.store.write_json(
            "unsafe-project-path-summary.json",
            {"summary": f"Read evidence from {project}."},
        )


def test_unified_diff_allows_routes_but_rejects_local_filesystem_paths(
    tmp_path: Path,
) -> None:
    project, _ = _project(tmp_path)
    session = _initialize(project)
    route_diff = (
        "--- a/app.xml\n"
        "+++ b/app.xml\n"
        "+Repository target /force-app/main/default/lwc/caseConsole is additive.\n"
        '+<http:listener-config basePath="/api"/>\n'
        '+<http:listener path="/customers/{customerId}/status"/>'
    )
    session.store.write_json("safe-route-diff.json", {"unified_diff": route_diff})

    unsafe_values = (
        "+read /etc/passwd",
        "+write /private/tmp/capstone.txt",
        "+read C:\\Users\\alice\\secret.txt",
        "+read file:///etc/passwd",
        "+/dev/null",
    )
    for index, value in enumerate(unsafe_values):
        with pytest.raises(PolicyViolation, match="local absolute path"):
            session.store.write_json(
                f"unsafe-route-diff-{index}.json",
                {"unified_diff": value},
            )
