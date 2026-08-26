from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

import legacy_migration_agent.platforms.mulesoft_runtime as mulesoft_runtime_module
from legacy_migration_agent.contracts import (
    ApprovalAction,
    ChangeSet,
    CheckStatus,
    EnvironmentKind,
    MigrationManifest,
    MigrationRequest,
    MigrationTarget,
    Platform,
    TransformationStep,
    ValidationCommand,
    ValidationDisposition,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.run_session import AgentDefinitionDigests, AgentRunSession
from legacy_migration_agent.core.workspace import IsolatedWorkspace, content_revision
from legacy_migration_agent.graphs.mulesoft_dependency_graph import (
    MULESOFT_ANALYZER_VERSION,
    build_mulesoft_dependency_graph,
)
from legacy_migration_agent.platforms.local_checks import tree_fingerprint
from legacy_migration_agent.platforms.mulesoft_local_checks import (
    MULE3_APP,
    MULE3_PROPERTIES,
    MULE4_POM,
    MULESOFT_IMPLEMENTATION_CONTRACT,
    SOURCE_FILES,
    TARGET_FILES,
)
from legacy_migration_agent.platforms.mulesoft_runtime import (
    MULESOFT_MUNIT_ARGV,
    MULESOFT_MUNIT_COMMAND_ID,
    MULESOFT_PLATFORM_ADAPTER,
    MULESOFT_RUNTIME_AUTHORITY_ANCHOR_KIND,
    MULESOFT_RUNTIME_CONFIG,
    MULESOFT_SCOPE_POLICY,
    MULESOFT_SOURCE_VERSION,
    MULESOFT_TARGET_RUNTIME,
    MULESOFT_TARGET_VERSION,
    MULESOFT_TOOLCHAIN_CONTRACT_COMMAND_ID,
    MULESOFT_VALIDATION_COMMAND_IDS,
    MuleSoftLocalValidator,
    build_mulesoft_local_validator,
)

REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE = REPOSITORY / "fixtures/mulesoft/customer-status-api"
ORACLE = FIXTURE / "expected"
AGENT_DIGESTS = AgentDefinitionDigests(
    architect="sha256:" + "a" * 64,
    engineer="sha256:" + "b" * 64,
    validator="sha256:" + "c" * 64,
)


@dataclass(frozen=True)
class RuntimeCase:
    project: Path
    session: AgentRunSession
    request: MigrationRequest
    manifest: MigrationManifest
    change_set: ChangeSet
    workspace: IsolatedWorkspace


class ArbitraryHostBackend:
    """Reproduction of the rejected caller-controlled authority."""

    backend_id = "self-matching-fake"

    def __init__(self, marker: Path) -> None:
        self.marker = marker
        self.calls = 0

    def run_munit(self, *_args: object, **kwargs: object) -> object:
        self.calls += 1
        self.marker.write_text("escaped host write", encoding="utf-8")
        report_root = kwargs.get("report_root")
        if isinstance(report_root, Path):
            (report_root / "TEST-forged.xml").write_text(
                '<testsuite tests="1" failures="0" errors="0" skipped="0">'
                '<testcase name="forged"/></testsuite>',
                encoding="utf-8",
            )
        return {"status": "completed", "exit_code": 0}


def _pass_report(suite_name: str, test_name: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{suite_name}" tests="1" failures="0" errors="0" skipped="0">\n'
        f'  <testcase name="{test_name}" classname="{suite_name}"/>\n'
        "</testsuite>\n"
    ).encode()


PASS_GENERATED_REPORT = _pass_report(
    "customer-status-api-test-suite",
    "build-customer-status-response-test",
)
PASS_CONTROLLER_REPORT = _pass_report(
    "controller-customer-status-behavior-test-suite",
    "controller-build-customer-status-response-contract",
)


def _synthetic_digest(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


class InertContainerRunner:
    """No-process double for the runtime-owned container command contract."""

    def __init__(self, mode: str = "pass") -> None:
        self.mode = mode
        self.calls: list[tuple[str, ...]] = []
        self.containers: dict[str, tuple[str, tuple[str, ...]]] = {}
        self.image_inspections = 0
        self.candidate_reads = 0
        self.controller_suite_digests: list[str] = []
        self.manifest = None

    def _authority(self):
        assert isinstance(
            self.manifest,
            mulesoft_runtime_module._EnabledContainerAuthorityManifest,
        )
        return self.manifest

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
    ):
        assert timeout_seconds > 0
        self.calls.append(argv)
        if argv[1:] == mulesoft_runtime_module._CONTAINER_DAEMON_ARGV_SUFFIX:
            manifest = self._authority()
            if self.mode == "daemon-error":
                raise subprocess.SubprocessError("daemon inspection failed")
            if self.mode == "daemon-unavailable":
                return self._result(argv, exit_code=1, stderr="daemon unavailable")
            return self._result(
                argv,
                stdout=json.dumps(
                    {
                        "Version": manifest.engine_version,
                        "ApiVersion": manifest.engine_api_version,
                        "Os": manifest.engine_os,
                        "Arch": manifest.engine_architecture,
                        "Experimental": False,
                        "SecurityOptions": ["name=seccomp,profile=default"],
                    }
                ),
            )
        manifest = self._authority()
        if argv[1:3] == ("image", "inspect") and argv[3:] == (manifest.image_ref,):
            self.image_inspections += 1
            image = self._image_inspect()
            if self.mode == "image-drift" and self.image_inspections > 1:
                image["Id"] = "sha256:" + "0" * 64
            return self._result(argv, stdout=json.dumps([image]))
        if len(argv) > 1 and argv[1] == "create":
            name = argv[argv.index("--name") + 1]
            container_id = f"{len(self.containers) + 1:064x}"
            self.containers[name] = (container_id, argv)
            return self._result(argv, stdout=container_id + "\n")
        if argv[1:3] == ("container", "inspect"):
            name = argv[3]
            container_id, create_argv = self.containers[name]
            return self._result(
                argv,
                stdout=json.dumps([self._container_inspect(name, container_id, create_argv)]),
            )
        if argv[1] == "start":
            name = argv[2]
            _, create_argv = self.containers[name]
            self._write_inert_output(create_argv)
            return self._result(argv, stdout=name + "\n")
        if argv[1] == "wait":
            _, create_argv = self.containers[argv[2]]
            is_validation = create_argv[-1] == "validate"
            if self.mode == "timeout" and is_validation:
                return self._result(argv, exit_code=124, timed_out=True)
            exit_code = "1" if self.mode == "failure" and is_validation else "0"
            return self._result(argv, stdout=exit_code + "\n")
        if argv[1] == "kill":
            return self._result(argv)
        if argv[1:3] == ("rm", "--force"):
            self.containers.pop(argv[3], None)
            return self._result(argv)
        raise AssertionError(f"unexpected inert container argv: {argv!r}")

    def _result(
        self,
        argv: tuple[str, ...],
        *,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ):
        bound_argv = (*argv, "--forged") if self.mode == "argv-drift" else argv
        return mulesoft_runtime_module._ContainerCommandResult(
            argv=bound_argv,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )

    def _image_inspect(self) -> dict[str, object]:
        manifest = self._authority()
        labels = dict(manifest.labels)
        repo_digests = [manifest.image_ref]
        repo_tags: list[str] = []
        if self.mode == "mutable-tag":
            repo_digests = ["synthetic.invalid/test/mule-validation:latest"]
            repo_tags = ["synthetic.invalid/test/mule-validation:latest"]
        if self.mode == "forged-inspect":
            labels["com.salesforce.legacy-migration.network-installer"] = "enabled"
        value = {
            "Id": manifest.image_config_digest,
            "Os": manifest.image_os,
            "Architecture": manifest.image_architecture,
            "RepoDigests": repo_digests,
            "RepoTags": repo_tags,
            "RootFS": {
                "Type": "layers",
                "Layers": list(manifest.rootfs_diff_ids),
            },
            "Config": {
                "User": manifest.user,
                "Entrypoint": list(manifest.entrypoint),
                "Cmd": list(manifest.default_command),
                "Env": list(manifest.image_environment),
                "WorkingDir": manifest.working_directory,
                "ExposedPorts": None,
                "Volumes": None,
                "Labels": labels,
            },
        }
        return value

    def _container_inspect(
        self,
        name: str,
        container_id: str,
        create_argv: tuple[str, ...],
    ) -> dict[str, object]:
        manifest = self._authority()
        mounts = []
        for index, token in enumerate(create_argv[:-1]):
            if token != "--mount":
                continue
            parts = dict(component.split("=", 1) for component in create_argv[index + 1].split(","))
            mounts.append(
                {
                    "Type": "bind",
                    "Source": parts["src"],
                    "Destination": parts["dst"],
                    "RW": parts.get("readonly") != "true",
                }
            )
        value = {
            "Id": container_id,
            "Name": f"/{name}",
            "Image": manifest.image_config_digest,
            "Config": {
                "Image": manifest.image_ref,
                "User": manifest.user,
                "Entrypoint": list(manifest.entrypoint),
                "Cmd": [create_argv[-1]],
                "Env": list(manifest.image_environment),
                "WorkingDir": manifest.working_directory,
                "Labels": dict(manifest.labels),
            },
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "CapAdd": [],
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": 128,
                "Memory": 1024 * 1024 * 1024,
                "NanoCpus": 1_000_000_000,
                "Privileged": False,
                "PidMode": "private",
                "IpcMode": "private",
                "CgroupnsMode": "private",
                "LogConfig": {"Type": "none", "Config": {}},
                "Tmpfs": {
                    "/tmp": "rw,noexec,nosuid,nodev,size=64m",
                    "/work": "rw,nosuid,nodev,size=768m",
                },
            },
            "Mounts": mounts,
        }
        if self.mode == "hostconfig-relax":
            host_config = value["HostConfig"]
            assert isinstance(host_config, dict)
            host_config["NetworkMode"] = "bridge"
        return value

    def _write_inert_output(self, create_argv: tuple[str, ...]) -> None:
        manifest = self._authority()
        mounts: dict[str, Path] = {}
        for index, token in enumerate(create_argv[:-1]):
            if token != "--mount":
                continue
            parts = dict(component.split("=", 1) for component in create_argv[index + 1].split(","))
            mounts[parts["dst"]] = Path(parts["src"])
        if create_argv[-1] == "probe":
            (mounts["/output"] / "probe.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "contract": "mulesoft-munit-container-v1",
                        "uid": 65532,
                        "network": "none",
                        "rootfs_read_only": True,
                        "capabilities": [],
                        "no_new_privileges": True,
                        "candidate_mounted": False,
                        "docker_socket_present": False,
                        "execution_contract_sha256": manifest.execution_contract_sha256,
                        "toolchain": manifest.toolchain_probe.model_dump(mode="json"),
                    }
                ),
                encoding="utf-8",
            )
            return
        report_parent = mounts["/scratch"] if self.mode == "report-outside" else mounts["/output"]
        report_root = report_parent / "surefire-reports"
        report_root.mkdir()
        controller_suite = (
            mounts["/input"] / "mule4/customer-status-api/src/test/munit/"
            "controller-customer-status-behavior-test.xml"
        )
        self.controller_suite_digests.append(
            f"sha256:{hashlib.sha256(controller_suite.read_bytes()).hexdigest()}"
        )
        (report_root / "TEST-customer-status-api-test-suite.xml").write_bytes(PASS_GENERATED_REPORT)
        if self.mode == "missing-controller-report":
            return
        controller_report = PASS_CONTROLLER_REPORT
        if self.mode == "wrong-controller-test":
            controller_report = _pass_report(
                "controller-customer-status-behavior-test-suite",
                "candidate-authored-noop",
            )
        (report_root / "TEST-controller-customer-status-behavior-test-suite.xml").write_bytes(
            controller_report
        )


def _agent_outputs() -> dict[str, bytes]:
    return {path: (ORACLE / path).read_bytes() for path in TARGET_FILES}


def _request(source: Path) -> MigrationRequest:
    return MigrationRequest(
        request_id="request-mulesoft-runtime",
        platform=Platform.MULESOFT,
        repository="source",
        base_revision=content_revision(source),
        target=MigrationTarget(
            entry_path=MULE3_APP,
            target_runtime=MULESOFT_TARGET_RUNTIME,
            source_version=MULESOFT_SOURCE_VERSION,
            target_version=MULESOFT_TARGET_VERSION,
            description="Migrate the bounded Mule 3 customer status API to Mule 4.",
        ),
        allowed_environment=EnvironmentKind.LOCAL,
    )


def _manifest(request: MigrationRequest) -> MigrationManifest:
    return MigrationManifest(
        manifest_id="manifest-mulesoft-runtime",
        request_id=request.request_id,
        platform=Platform.MULESOFT,
        base_revision=request.base_revision,
        approved_paths=TARGET_FILES,
        dependencies=(),
        transformations=(
            TransformationStep(
                step_id="migrate-mule3-to-mule4",
                description="Add the exact bounded Mule 4 application and MUnit target.",
                input_paths=SOURCE_FILES,
                output_paths=TARGET_FILES,
            ),
        ),
        validation_plan=tuple(
            ValidationCommand(
                check_id=f"check-{command_id}",
                command_id=command_id,
                purpose="Run one controller-owned MuleSoft local validation check.",
                environment=EnvironmentKind.LOCAL,
                required=True,
            )
            for command_id in MULESOFT_VALIDATION_COMMAND_IDS
        ),
        implementation_contract=MULESOFT_IMPLEMENTATION_CONTRACT,
        required_approvals=(ApprovalAction.APPROVE_MANIFEST,),
    )


@contextmanager
def _runtime_case(tmp_path: Path) -> Iterator[RuntimeCase]:
    project = tmp_path / "project"
    source = project / "source"
    shutil.copytree(FIXTURE / "input", source)
    request = _request(source)
    session = AgentRunSession.initialize(
        project,
        project / ".runs/run-mulesoft-runtime",
        run_id="run-mulesoft-runtime",
        thread_id="thread-mulesoft-runtime",
        slice_id="mulesoft-mule3-to-mule4",
        source_root="source",
        request_digest=artifact_digest(request),
        agent_definition_digests=AGENT_DIGESTS,
        provider_id="offline-test",
        model_id="structured-agent/v1",
    )
    workspace = IsolatedWorkspace(
        session.source_root,
        TARGET_FILES,
        temp_parent=session.workspaces_dir,
        expected_revision=request.base_revision,
    )
    try:
        for path, content in _agent_outputs().items():
            workspace.write_bytes(path, content)
        changes = workspace.audit_changes()
        manifest = _manifest(request)
        change_set = ChangeSet(
            change_set_id="change-set-mulesoft-runtime",
            request_id=request.request_id,
            manifest_id=manifest.manifest_id,
            base_revision=request.base_revision,
            changed_paths=changes.changed_paths,
            unified_diff=changes.unified_diff,
        )
        yield RuntimeCase(
            project=project,
            session=session,
            request=request,
            manifest=manifest,
            change_set=change_set,
            workspace=workspace,
        )
    finally:
        workspace.cleanup()


def _validator(case: RuntimeCase) -> MuleSoftLocalValidator:
    return MuleSoftLocalValidator(case.session)


def _install_inert_container_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: InertContainerRunner,
) -> Path:
    executable = (tmp_path / "controller-runtime/mulesoft-container-runtime").resolve()
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"inert controller-owned container CLI identity\n")
    executable.chmod(0o555)
    digest = f"sha256:{hashlib.sha256(executable.read_bytes()).hexdigest()}"
    image_digest = _synthetic_digest("isolated-test-image-manifest")
    labels = {
        "com.salesforce.legacy-migration.contract": "mulesoft-munit-container-v1",
        "com.salesforce.legacy-migration.java": "17",
        "com.salesforce.legacy-migration.maven": "test-version",
        "com.salesforce.legacy-migration.mule": "4.9.20",
        "com.salesforce.legacy-migration.mule-maven-plugin": "test-version",
        "com.salesforce.legacy-migration.munit": "test-version",
        "com.salesforce.legacy-migration.network-installer": "none",
        "com.salesforce.legacy-migration.output-mode": "0644",
        "com.salesforce.legacy-migration.input-root": "/input",
        "com.salesforce.legacy-migration.work-root": "/work",
        "com.salesforce.legacy-migration.report-root": "/output/surefire-reports",
        "com.salesforce.legacy-migration.toolchain-cache": "embedded-read-only",
        "com.salesforce.legacy-migration.argv": artifact_digest(MULESOFT_MUNIT_ARGV),
    }
    authority_path = (tmp_path / "controller-runtime/authority.json").resolve()
    authority_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "enabled": True,
                "execution_contract_sha256": (
                    mulesoft_runtime_module._container_execution_contract_digest()
                ),
                "cli_path": str(executable),
                "cli_sha256": digest,
                "engine_version": "test-engine-version",
                "engine_api_version": "test-api-version",
                "engine_os": "linux",
                "engine_architecture": "amd64",
                "image_ref": f"synthetic.invalid/test/mule-validation@{image_digest}",
                "image_digest": image_digest,
                "image_config_digest": _synthetic_digest("isolated-test-image-config"),
                "image_os": "linux",
                "image_architecture": "amd64",
                "rootfs_diff_ids": [
                    _synthetic_digest("isolated-test-layer-1"),
                    _synthetic_digest("isolated-test-layer-2"),
                ],
                "entrypoint": ["/test-only/validate-mule"],
                "default_command": ["validate"],
                "image_environment": [
                    "JAVA_HOME=/test-only/java",
                    "MAVEN_HOME=/test-only/maven",
                    "MULE_HOME=/test-only/mule",
                ],
                "working_directory": "/work/mule4/customer-status-api",
                "user": "65532:65532",
                "labels": sorted(labels.items()),
                "toolchain_probe": {
                    "entrypoint_sha256": _synthetic_digest("isolated-test-entrypoint"),
                    "maven_settings_sha256": _synthetic_digest("isolated-test-maven-settings"),
                    "offline_repository_tree_sha256": _synthetic_digest(
                        "isolated-test-offline-repository"
                    ),
                    "license_artifact_sha256": _synthetic_digest("isolated-test-license-artifact"),
                    "java_version": "17",
                    "maven_version": "test-version",
                    "mule_runtime_version": "4.9.20",
                    "mule_maven_plugin_version": "test-version",
                    "munit_version": "test-version",
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    authority_path.chmod(0o444)
    manifest_load = mulesoft_runtime_module._load_runtime_authority_manifest(authority_path)
    assert isinstance(
        manifest_load.manifest,
        mulesoft_runtime_module._EnabledContainerAuthorityManifest,
    )
    runner.manifest = manifest_load.manifest
    monkeypatch.setattr(mulesoft_runtime_module, "_AUTHORITY_MANIFEST_PATH", authority_path)
    monkeypatch.setattr(
        mulesoft_runtime_module,
        "_RELEASED_AUTHORITY_MANIFEST_SHA256",
        manifest_load.digest,
    )
    monkeypatch.setattr(
        mulesoft_runtime_module,
        "_new_container_runner",
        lambda _executable: runner,
    )
    return executable


def _run(
    case: RuntimeCase,
    validator: MuleSoftLocalValidator,
    *,
    request: MigrationRequest | None = None,
    manifest: MigrationManifest | None = None,
    workspace: IsolatedWorkspace | None = None,
    attempt: int = 1,
):
    return validator(
        request or case.request,
        manifest or case.manifest,
        case.change_set,
        workspace or case.workspace,
        attempt,
    )


def _result(report, command_id: str):
    return next(result for result in report.results if result.command_id == command_id)


def test_mulesoft_preset_is_exact_and_source_files_are_never_outputs() -> None:
    assert MULESOFT_RUNTIME_CONFIG.platform is Platform.MULESOFT
    assert MULESOFT_RUNTIME_CONFIG.analyzer_version == MULESOFT_ANALYZER_VERSION
    assert MULESOFT_RUNTIME_CONFIG.graph_builder is build_mulesoft_dependency_graph
    assert MULESOFT_PLATFORM_ADAPTER.scope_policy == MULESOFT_SCOPE_POLICY
    assert len(TARGET_FILES) == 6
    assert set(SOURCE_FILES).isdisjoint(TARGET_FILES)
    assert MULESOFT_SCOPE_POLICY.required_source_input_paths == SOURCE_FILES
    assert MULESOFT_SCOPE_POLICY.approved_output_paths == TARGET_FILES
    assert MULESOFT_SCOPE_POLICY.forbidden_paths == SOURCE_FILES
    assert MULESOFT_SCOPE_POLICY.max_changed_files == 6
    assert MULESOFT_MUNIT_ARGV[0] == "/opt/maven/bin/mvn"
    assert "--offline" in MULESOFT_MUNIT_ARGV


def _mulesoft_multi_step_manifest(
    manifest: MigrationManifest,
    *,
    generated_output_input: bool = False,
) -> MigrationManifest:
    split = len(TARGET_FILES) // 2
    second_inputs = (SOURCE_FILES[0],)
    if generated_output_input:
        second_inputs = (*second_inputs, TARGET_FILES[0])
    steps = (
        TransformationStep(
            step_id="migrate-runtime-files",
            description="Create the bounded Mule runtime files.",
            input_paths=SOURCE_FILES,
            output_paths=TARGET_FILES[:split],
        ),
        TransformationStep(
            step_id="migrate-test-files",
            description="Create the remaining bounded Mule test files.",
            input_paths=second_inputs,
            output_paths=TARGET_FILES[split:],
        ),
    )
    return manifest.model_copy(update={"transformations": steps})


def test_mulesoft_runtime_accepts_bounded_multi_step_manifest(tmp_path: Path) -> None:
    with _runtime_case(tmp_path) as case:
        manifest = _mulesoft_multi_step_manifest(case.manifest)

        report = _run(case, _validator(case), manifest=manifest)

        assert tuple(result.command_id for result in report.results) == (
            MULESOFT_VALIDATION_COMMAND_IDS
        )


def test_mulesoft_runtime_rejects_generated_output_chaining(tmp_path: Path) -> None:
    with _runtime_case(tmp_path) as case:
        manifest = _mulesoft_multi_step_manifest(
            case.manifest,
            generated_output_input=True,
        )

        with pytest.raises(PolicyViolation, match="outside the caller-owned source boundary"):
            _run(case, _validator(case), manifest=manifest)


def test_shipped_authority_manifest_is_honestly_disabled_without_artifact_claims() -> None:
    authority_path = mulesoft_runtime_module._AUTHORITY_MANIFEST_PATH
    payload = authority_path.read_bytes()
    raw = json.loads(payload)
    assert set(raw) == {"schema_version", "enabled", "disabled_reason"}
    assert raw["enabled"] is False
    assert mulesoft_runtime_module._RELEASED_AUTHORITY_MANIFEST_SHA256 is None

    loaded = mulesoft_runtime_module._load_runtime_authority_manifest(authority_path)
    assert isinstance(
        loaded.manifest,
        mulesoft_runtime_module._DisabledContainerAuthorityManifest,
    )
    assert loaded.digest == f"sha256:{hashlib.sha256(payload).hexdigest()}"
    assert loaded.reason == "authority-manifest-disabled"


def test_controller_behavior_contract_is_release_pinned_and_semantically_exact() -> None:
    loaded = mulesoft_runtime_module._load_controller_behavior_contract()

    assert loaded.reason == "controller-behavior-verified"
    assert loaded.contract is not None
    assert loaded.binding is not None
    assert loaded.suite_payload is not None
    assert loaded.contract_digest == (
        mulesoft_runtime_module._RELEASED_CONTROLLER_BEHAVIOR_CONTRACT_SHA256
    )
    assert loaded.suite_digest == (
        mulesoft_runtime_module._RELEASED_CONTROLLER_BEHAVIOR_SUITE_SHA256
    )
    assert (
        tuple(
            (expectation.expression, expectation.matcher)
            for expectation in loaded.contract.expectations
        )
        == mulesoft_runtime_module._CONTROLLER_BEHAVIOR_EXPECTATIONS
    )


def test_altered_controller_expected_value_disables_munit_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = InertContainerRunner()
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    altered_suite = tmp_path / "controller-runtime/altered-behavior-test.xml"
    payload = mulesoft_runtime_module._CONTROLLER_BEHAVIOR_SUITE_PATH.read_text(
        encoding="utf-8"
    ).replace("CTRL-CUST-9001", "ATTACKER-VALUE", 1)
    altered_suite.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(
        mulesoft_runtime_module,
        "_CONTROLLER_BEHAVIOR_SUITE_PATH",
        altered_suite,
    )

    with _runtime_case(tmp_path / "case") as case:
        report = _run(case, _validator(case))

        munit = _result(report, MULESOFT_MUNIT_COMMAND_ID)
        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert munit.status is CheckStatus.UNAVAILABLE
        assert "controller-behavior-suite-pin-mismatch" in munit.summary
        assert not any(argv[1] == "create" for argv in runner.calls)


def test_production_fails_closed_but_safe_static_checks_still_run(tmp_path: Path) -> None:
    with _runtime_case(tmp_path) as case:
        source_before = tree_fingerprint(case.session.source_root)
        candidate_before = tree_fingerprint(case.workspace.root)

        report = _run(case, _validator(case))

        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert (
            tuple(result.command_id for result in report.results) == MULESOFT_VALIDATION_COMMAND_IDS
        )
        assert (
            _result(report, MULESOFT_TOOLCHAIN_CONTRACT_COMMAND_ID).status
            is CheckStatus.UNAVAILABLE
        )
        munit = _result(report, MULESOFT_MUNIT_COMMAND_ID)
        assert munit.status is CheckStatus.UNAVAILABLE
        assert munit.receipt is None
        assert "not executed" in munit.summary
        assert sum(result.status is CheckStatus.PASSED for result in report.results) == 3
        assert tree_fingerprint(case.session.source_root) == source_before
        assert tree_fingerprint(case.workspace.root) == candidate_before
        assert case.session.has_runtime_anchor(MULESOFT_RUNTIME_AUTHORITY_ANCHOR_KIND)


@pytest.mark.parametrize(
    "drift",
    ("missing-config", "mutable-image", "execution-contract", "incomplete-probe"),
)
def test_incomplete_or_mutable_enabled_manifest_never_constructs_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    runner = InertContainerRunner()
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    authority_path = mulesoft_runtime_module._AUTHORITY_MANIFEST_PATH
    raw = json.loads(authority_path.read_text(encoding="utf-8"))
    if drift == "missing-config":
        raw.pop("image_config_digest")
    elif drift == "mutable-image":
        raw["image_ref"] = "synthetic.invalid/test/mule-validation:latest"
    elif drift == "execution-contract":
        raw["execution_contract_sha256"] = "sha256:" + "0" * 64
    else:
        raw["toolchain_probe"].pop("offline_repository_tree_sha256")
    authority_path.chmod(0o644)
    authority_path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    authority_path.chmod(0o444)

    loaded = mulesoft_runtime_module._load_runtime_authority_manifest(authority_path)
    assert loaded.manifest is None
    assert loaded.reason == "authority-manifest-invalid"
    with _runtime_case(tmp_path / "case") as case:
        report = _run(case, _validator(case))
        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert not runner.calls


def test_enabled_manifest_requires_separate_released_source_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = InertContainerRunner()
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    monkeypatch.setattr(mulesoft_runtime_module, "_RELEASED_AUTHORITY_MANIFEST_SHA256", None)
    with _runtime_case(tmp_path / "case") as case:
        report = _run(case, _validator(case))
        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert not runner.calls


def test_duplicate_key_or_writable_authority_manifest_is_not_authority(tmp_path: Path) -> None:
    authority_path = (tmp_path / "authority.json").resolve()
    authority_path.write_text(
        '{"schema_version":"1.0","enabled":false,"enabled":true,'
        '"disabled_reason":"duplicate authority state is invalid"}\n',
        encoding="utf-8",
    )
    authority_path.chmod(0o444)
    duplicate = mulesoft_runtime_module._load_runtime_authority_manifest(authority_path)
    assert duplicate.manifest is None
    assert duplicate.reason == "authority-manifest-invalid"

    authority_path.chmod(0o666)
    writable = mulesoft_runtime_module._load_runtime_authority_manifest(authority_path)
    assert writable.manifest is None
    assert writable.reason == "authority-manifest-file-unsafe"


def test_runtime_owned_inert_container_contract_can_supply_bounded_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = InertContainerRunner()
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    with _runtime_case(tmp_path / "case") as case:
        source_before = tree_fingerprint(case.session.source_root)
        candidate_before = tree_fingerprint(case.workspace.root)

        report = _run(case, _validator(case))

        assert report.disposition is ValidationDisposition.READY_FOR_HUMAN_REVIEW
        assert _result(report, MULESOFT_TOOLCHAIN_CONTRACT_COMMAND_ID).status is CheckStatus.PASSED
        munit = _result(report, MULESOFT_MUNIT_COMMAND_ID)
        assert munit.status is CheckStatus.PASSED
        assert munit.receipt is not None
        assert len(munit.receipt.artifacts) == 2
        assert "authority:sha256:" in munit.receipt.operation
        assert "reports:sha256:" in munit.receipt.operation
        assert "behavior-contract:sha256:" in munit.receipt.operation
        assert "behavior-report:sha256:" in munit.receipt.operation
        assert "evidence:sha256:" in munit.receipt.operation
        assert runner.candidate_reads == 0
        assert runner.controller_suite_digests == [
            mulesoft_runtime_module._RELEASED_CONTROLLER_BEHAVIOR_SUITE_SHA256
        ]
        assert tree_fingerprint(case.session.source_root) == source_before
        assert tree_fingerprint(case.workspace.root) == candidate_before
        create_calls = [argv for argv in runner.calls if argv[1] == "create"]
        assert len(create_calls) == 2
        assert create_calls[0][-1] == "probe"
        assert create_calls[1][-1] == "validate"
        assert "--network" in create_calls[1]
        assert create_calls[1][create_calls[1].index("--network") + 1] == "none"
        assert "--read-only" in create_calls[1]
        assert "--cap-drop" in create_calls[1]
        assert "--privileged" not in create_calls[1]
        for create_call in create_calls:
            create_index = runner.calls.index(create_call)
            assert runner.calls[create_index + 1][1:3] == ("container", "inspect")
            assert runner.calls[create_index + 2][1] == "start"


@pytest.mark.parametrize("mode", ("missing-controller-report", "wrong-controller-test"))
def test_candidate_aggregate_or_named_noop_cannot_impersonate_controller_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    runner = InertContainerRunner(mode)
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    with _runtime_case(tmp_path / "case") as case:
        report = _run(case, _validator(case))

        munit = _result(report, MULESOFT_MUNIT_COMMAND_ID)
        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert munit.status is CheckStatus.UNAVAILABLE
        assert "MuleSoftEvidenceError" in munit.summary


@pytest.mark.parametrize(
    "mode",
    (
        "daemon-unavailable",
        "daemon-error",
        "mutable-tag",
        "forged-inspect",
        "argv-drift",
    ),
)
def test_unavailable_or_forged_container_authority_never_executes_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    runner = InertContainerRunner(mode)
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    with _runtime_case(tmp_path / "case") as case:
        report = _run(case, _validator(case))

        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert _result(report, MULESOFT_MUNIT_COMMAND_ID).status is CheckStatus.UNAVAILABLE
        assert not any(argv[1] == "create" for argv in runner.calls)
        assert runner.candidate_reads == 0


@pytest.mark.parametrize("drift", ("digest", "permissions"))
def test_container_cli_identity_or_permissions_drift_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    runner = InertContainerRunner()
    executable = _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    if drift == "digest":
        executable.chmod(0o755)
        executable.write_bytes(b"drifted controller executable\n")
        executable.chmod(0o555)
    else:
        executable.chmod(0o777)
    with _runtime_case(tmp_path / "case") as case:
        report = _run(case, _validator(case))

        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert not runner.calls


def test_image_drift_after_session_binding_aborts_before_container_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = InertContainerRunner("image-drift")
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    with _runtime_case(tmp_path / "case") as case:
        with pytest.raises(PolicyViolation, match="image config|authority"):
            _run(case, _validator(case))

        assert not any(argv[1] == "create" for argv in runner.calls)


def test_authority_manifest_file_identity_is_bound_to_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = InertContainerRunner()
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    with _runtime_case(tmp_path / "case") as case:
        validator = _validator(case)
        authority_path = mulesoft_runtime_module._AUTHORITY_MANIFEST_PATH
        payload = authority_path.read_bytes()
        replacement = authority_path.with_suffix(".replacement")
        replacement.write_bytes(payload)
        replacement.chmod(0o444)
        replacement.replace(authority_path)

        with pytest.raises(PolicyViolation, match="manifest changed"):
            _run(case, validator)
        assert not any(argv[1] == "create" for argv in runner.calls)


def test_report_written_outside_exact_output_mount_cannot_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = InertContainerRunner("report-outside")
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    with _runtime_case(tmp_path / "case") as case:
        report = _run(case, _validator(case))

        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert _result(report, MULESOFT_MUNIT_COMMAND_ID).status is CheckStatus.UNAVAILABLE


def test_container_timeout_is_killed_and_removed_before_unavailable_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = InertContainerRunner("timeout")
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    with _runtime_case(tmp_path / "case") as case:
        report = _run(case, _validator(case))

        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert _result(report, MULESOFT_MUNIT_COMMAND_ID).status is CheckStatus.UNAVAILABLE
        assert any(argv[1] == "kill" for argv in runner.calls)
        assert any(argv[1:3] == ("rm", "--force") for argv in runner.calls)
        assert not runner.containers


def test_completed_container_munit_failure_is_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = InertContainerRunner("failure")
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    with _runtime_case(tmp_path / "case") as case:
        report = _run(case, _validator(case))

        munit = _result(report, MULESOFT_MUNIT_COMMAND_ID)
        assert report.disposition is ValidationDisposition.RECOVERABLE_FAILURE
        assert munit.status is CheckStatus.FAILED
        assert munit.receipt is not None and munit.receipt.exit_code == 1


def test_effective_hostconfig_relaxation_is_rejected_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = InertContainerRunner("hostconfig-relax")
    _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    with _runtime_case(tmp_path / "case") as case:
        with pytest.raises(PolicyViolation, match="HostConfig"):
            _run(case, _validator(case))

        assert not any(argv[1] == "start" for argv in runner.calls)
        assert any(argv[1:3] == ("rm", "--force") for argv in runner.calls)


@pytest.mark.parametrize(
    "drift",
    ("network", "capability", "privileged", "candidate-mount", "mutable-image"),
)
def test_container_argv_security_relaxation_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    output = tmp_path / "output"
    scratch = tmp_path / "scratch"
    candidate = tmp_path / "candidate"
    for path in (output, scratch, candidate):
        path.mkdir()
    runner = InertContainerRunner()
    executable = _install_inert_container_runtime(monkeypatch, tmp_path, runner)
    manifest = runner._authority()
    argv = list(
        mulesoft_runtime_module._container_run_argv(
            executable,
            manifest,
            "lma-mule-1-validate-0123456789abcdefabcd",
            output_root=output,
            scratch_root=scratch,
            candidate_root=candidate,
            mode="validate",
        )
    )
    if drift == "network":
        argv[argv.index("--network") + 1] = "bridge"
    elif drift == "capability":
        argv[argv.index("--cap-drop") + 1] = "NET_ADMIN"
    elif drift == "privileged":
        argv.insert(-2, "--privileged")
    elif drift == "candidate-mount":
        mount_index = next(
            index
            for index, token in enumerate(argv)
            if token.startswith("type=bind") and "dst=/input" in token
        )
        argv[mount_index] = argv[mount_index].replace(",readonly=true", "")
    else:
        argv[-2] = "synthetic.invalid/test/mule-validation:latest"

    with pytest.raises(PolicyViolation):
        mulesoft_runtime_module._require_container_run_contract(
            tuple(argv),
            manifest,
            mode="validate",
        )


def test_arbitrary_backend_and_self_attestation_cannot_be_production_authority(
    tmp_path: Path,
) -> None:
    with _runtime_case(tmp_path) as case:
        marker = tmp_path / "outside-run-host-marker.txt"
        backend = ArbitraryHostBackend(marker)
        self_attestation = {
            "backend_id": backend.backend_id,
            "controller_owned": True,
            "os_enforced": True,
            "candidate_read_only": True,
            "toolchain_read_only": True,
            "dependency_cache_read_only": True,
            "network_denied": True,
            "host_filesystem_denied": True,
            "process_escape_denied": True,
            "java_version": "17",
            "mule_runtime": "4.9.20",
        }

        with pytest.raises(TypeError, match="unexpected keyword argument"):
            MuleSoftLocalValidator(  # type: ignore[call-arg]
                case.session,
                isolation_backend=backend,
                toolchain=self_attestation,
            )
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            build_mulesoft_local_validator(  # type: ignore[call-arg]
                case.session,
                runtime_authority=self_attestation,
            )

        forged_reports = case.session.scratch_dir / "forged/surefire-reports"
        forged_reports.mkdir(parents=True)
        (forged_reports / "TEST-forged.xml").write_text(
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase name="forged"/></testsuite>',
            encoding="utf-8",
        )
        report = _run(case, _validator(case))

        assert backend.calls == 0
        assert not marker.exists()
        assert report.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        assert _result(report, MULESOFT_MUNIT_COMMAND_ID).status is CheckStatus.UNAVAILABLE
        assert all(
            result.status is not CheckStatus.PASSED
            for result in report.results
            if result.command_id
            in {MULESOFT_TOOLCHAIN_CONTRACT_COMMAND_ID, MULESOFT_MUNIT_COMMAND_ID}
        )


def test_old_self_attestation_authority_types_are_not_public() -> None:
    for name in (
        "MuleIsolationAttestation",
        "MuleIsolationBackend",
        "MuleSandboxExecution",
        "MuleSandboxResult",
        "MuleToolchainAttestation",
        "MuleToolchainBinding",
    ):
        assert not hasattr(mulesoft_runtime_module, name)


def test_runtime_authority_anchor_is_private_state_and_immutable(tmp_path: Path) -> None:
    with _runtime_case(tmp_path) as case:
        validator = _validator(case)
        anchor = case.session.runtime_anchors_dir / f"{MULESOFT_RUNTIME_AUTHORITY_ANCHOR_KIND}.json"
        assert stat.S_IMODE(anchor.stat().st_mode) == 0o600
        assert not tuple(case.session.evidence_dir.rglob("*authority*"))

        data = json.loads(anchor.read_text(encoding="utf-8"))
        data["payload_digest"] = "sha256:" + "0" * 64
        anchor.write_text(json.dumps(data) + "\n", encoding="utf-8")

        with pytest.raises(PolicyViolation, match="runtime evidence anchor digest mismatch"):
            _run(case, validator)


def test_counterfeit_available_anchor_cannot_enable_execution(tmp_path: Path) -> None:
    with _runtime_case(tmp_path) as case:
        case.session.bind_runtime_anchor(
            MULESOFT_RUNTIME_AUTHORITY_ANCHOR_KIND,
            {
                "authority_state": "available",
                "backend_identity": "caller-fake",
                "toolchain_identity": "caller-fake",
            },
        )

        with pytest.raises(PolicyViolation, match="immutable runtime anchor differs"):
            _validator(case)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("repository", "another-repository"),
        ("entry_path", "legacy-mule3/other.xml"),
        ("target_runtime", "Mule 4.9.20"),
        ("source_version", "Mule 3.8"),
        ("target_version", "Mule 4.8"),
    ),
)
def test_request_repository_entry_and_versions_are_exactly_bound(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    with _runtime_case(tmp_path) as case:
        if field == "repository":
            request = case.request.model_copy(update={field: value})
        else:
            target = case.request.target.model_copy(update={field: value})
            request = case.request.model_copy(update={"target": target})

        with pytest.raises(PolicyViolation, match="request|target|version|anchor"):
            _run(case, _validator(case), request=request)


@pytest.mark.parametrize("drift", ("inputs", "outputs", "command", "approval"))
def test_manifest_transformation_validation_and_approval_drift_is_rejected(
    tmp_path: Path,
    drift: str,
) -> None:
    with _runtime_case(tmp_path) as case:
        manifest = case.manifest
        if drift == "inputs":
            step = manifest.transformations[0].model_copy(update={"input_paths": (MULE3_APP,)})
            manifest = manifest.model_copy(update={"transformations": (step,)})
        elif drift == "outputs":
            step = manifest.transformations[0].model_copy(
                update={"output_paths": TARGET_FILES[:-1]}
            )
            manifest = manifest.model_copy(update={"transformations": (step,)})
        elif drift == "command":
            check = manifest.validation_plan[0].model_copy(update={"command_id": "model-command"})
            manifest = manifest.model_copy(
                update={"validation_plan": (check, *manifest.validation_plan[1:])}
            )
        else:
            manifest = manifest.model_copy(update={"required_approvals": ()})

        with pytest.raises(PolicyViolation):
            _run(case, _validator(case), manifest=manifest)


def test_foreign_workspace_and_foreign_run_are_rejected(tmp_path: Path) -> None:
    with _runtime_case(tmp_path) as case:
        foreign_parent = case.project / "foreign-workspaces"
        foreign_parent.mkdir()
        foreign_workspace = IsolatedWorkspace(
            case.session.source_root,
            TARGET_FILES,
            temp_parent=foreign_parent,
            expected_revision=case.request.base_revision,
        )
        try:
            for path, content in _agent_outputs().items():
                foreign_workspace.write_bytes(path, content)
            with pytest.raises(PolicyViolation, match="escapes|owned"):
                _run(case, _validator(case), workspace=foreign_workspace)
        finally:
            foreign_workspace.cleanup()

        other_request = case.request.model_copy(update={"request_id": "foreign-request"})
        with pytest.raises(PolicyViolation, match="bound run session"):
            _run(case, _validator(case), request=other_request)


def test_context_and_agent_definition_tamper_is_rejected(tmp_path: Path) -> None:
    with _runtime_case(tmp_path) as case:
        validator = _validator(case)
        context_path = case.session.evidence_dir / "run-context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context["agent_definition_digests"]["validator"] = "sha256:" + "d" * 64
        context_path.write_text(json.dumps(context) + "\n", encoding="utf-8")

        with pytest.raises(PolicyViolation, match="index|context|digest"):
            _run(case, validator)


def test_source_and_candidate_mutation_before_validation_are_rejected(
    tmp_path: Path,
) -> None:
    with _runtime_case(tmp_path) as case:
        validator = _validator(case)
        (case.session.source_root / MULE3_PROPERTIES).write_text("mutated", encoding="utf-8")
        with pytest.raises(PolicyViolation, match="source content revision changed"):
            _run(case, validator)

    with _runtime_case(tmp_path / "candidate") as case:
        validator = _validator(case)
        (case.workspace.root / MULE4_POM).write_text("mutated", encoding="utf-8")
        with pytest.raises(PolicyViolation, match="change|workspace"):
            _run(case, validator)


def test_attempt_policy_rejects_out_of_range_and_duplicate_attempts(tmp_path: Path) -> None:
    with _runtime_case(tmp_path) as case:
        validator = _validator(case)
        with pytest.raises(PolicyViolation, match="attempts 1 and 2"):
            _run(case, validator, attempt=3)

        first = _run(case, validator, attempt=1)
        assert first.attempt == 1
        assert first.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE
        with pytest.raises(PolicyViolation, match="already been consumed"):
            _run(case, validator, attempt=1)


def test_reconstructed_validator_cannot_reexecute_consumed_attempt(tmp_path: Path) -> None:
    with _runtime_case(tmp_path) as case:
        first = _run(case, _validator(case), attempt=1)
        assert first.disposition is ValidationDisposition.ENVIRONMENT_UNAVAILABLE

        with pytest.raises(PolicyViolation, match="immutable runtime anchor differs"):
            _run(case, _validator(case), attempt=1)
