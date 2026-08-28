import hashlib
import sys
import time
from pathlib import Path

import pytest

from legacy_migration_agent.contracts import ApprovalAction, EnvironmentKind
from legacy_migration_agent.core.execution import (
    OUTPUT_LIMIT_EXIT_CODE,
    SafeCommandRunner,
    execution_binding,
)
from legacy_migration_agent.core.policies import (
    CommandRegistry,
    CommandSpec,
    ExecutionApproval,
    PolicyViolation,
    RetryBudget,
)
from legacy_migration_agent.core.redaction import SecretRedactor

BASE_REVISION = "abcdef0"
INPUT_DIGEST = f"sha256:{'a' * 64}"


def runner(
    repository: Path,
    specs: tuple[CommandSpec, ...],
    *,
    timeout_seconds: float = 2,
    max_output_chars: int = 64,
    redactor: SecretRedactor | None = None,
) -> SafeCommandRunner:
    return SafeCommandRunner(
        CommandRegistry(specs),
        repository,
        RetryBudget(maximum_attempts=2),
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        redactor=redactor,
    )


def spec(
    command_id: str,
    code: str,
    directory: Path,
    *,
    allowed_argument_vectors: tuple[tuple[str, ...], ...] = ((),),
    sanitized_environment: tuple[tuple[str, str], ...] = (),
    artifact_paths: tuple[str, ...] = (),
    requires_approval: ApprovalAction | None = None,
) -> CommandSpec:
    return CommandSpec(
        command_id=command_id,
        argv_prefix=(sys.executable, "-c", code),
        allowed_working_directories=(directory,),
        allowed_argument_vectors=allowed_argument_vectors,
        sanitized_environment=sanitized_environment,
        artifact_paths=artifact_paths,
        requires_approval=requires_approval,
    )


def run_command(
    command_runner: SafeCommandRunner,
    command_id: str,
    repository: Path,
    **overrides,
):
    values = {
        "request_id": "request-1",
        "run_id": "run-1",
        "attempt": 1,
        "base_revision": BASE_REVISION,
        "environment": EnvironmentKind.LOCAL,
        "artifact_digest": INPUT_DIGEST,
        "working_directory": repository,
    }
    values.update(overrides)
    return command_runner.run(command_id, **values)


def test_success_emits_context_bound_receipt_and_bounded_output(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    command = spec("fixture-success", "print('x' * 100)", repository)
    command_runner = runner(repository, (command,), max_output_chars=32)

    result = run_command(command_runner, "fixture-success", repository)

    binding = execution_binding(
        "request-1",
        "run-1",
        "fixture-success",
        1,
        BASE_REVISION,
        EnvironmentKind.LOCAL,
        INPUT_DIGEST,
    )
    assert result.receipt.receipt_id == f"receipt-{binding[:24]}"
    assert result.receipt.operation.endswith(binding)
    assert result.receipt.request_id == "request-1"
    assert result.receipt.run_id == "run-1"
    assert result.receipt.attempt == 1
    assert result.receipt.base_revision == BASE_REVISION
    assert result.receipt.environment is EnvironmentKind.LOCAL
    assert result.receipt.input_artifact_digest == INPUT_DIGEST
    assert result.receipt.working_directory == "."
    assert result.receipt.terminal is True
    assert result.receipt.exit_code == 0
    assert result.receipt.ended_at >= result.receipt.started_at
    assert result.receipt.stdout_digest.startswith("sha256:")
    assert len(result.stdout) == 32
    assert result.stdout.endswith("...[output truncated]")
    assert result.duration_seconds >= 0
    assert result.timed_out is False


def test_dynamic_arguments_are_denied_before_spawn(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    marker = repository / "spawned.txt"
    command = spec(
        "fixture-args",
        "from pathlib import Path; Path('spawned.txt').write_text('spawned')",
        repository,
    )
    command_runner = runner(repository, (command,))

    with pytest.raises(PolicyViolation, match="exact allowed argument"):
        run_command(
            command_runner,
            "fixture-args",
            repository,
            args=("model-generated-argument",),
        )
    assert not marker.exists()


def test_process_receives_only_the_explicit_sanitized_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setenv("HOST_SECRET", "must-not-leak")
    command = spec(
        "fixture-environment",
        "import os; print(os.getenv('SAFE_FLAG')); print(os.getenv('HOST_SECRET', 'missing'))",
        repository,
        sanitized_environment=(("SAFE_FLAG", "yes"),),
    )

    result = run_command(runner(repository, (command,)), "fixture-environment", repository)

    assert result.stdout.splitlines() == ["yes", "missing"]
    assert "must-not-leak" not in result.receipt.model_dump_json()


def test_output_is_redacted_before_return_and_digest(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    bearer_secret = "bearer-value-123"
    explicit_secret = "opaque-secret-456"
    command = spec(
        "fixture-redaction",
        (f"print('Authorization: Bearer {bearer_secret}'); print('{explicit_secret}')"),
        repository,
    )
    command_runner = runner(
        repository,
        (command,),
        redactor=SecretRedactor((explicit_secret,)),
        max_output_chars=256,
    )

    result = run_command(command_runner, "fixture-redaction", repository)

    assert result.stdout == "Authorization: Bearer [REDACTED]\n[REDACTED]\n"
    assert result.stderr == ""
    assert result.receipt.redacted is True
    serialized = result.receipt.model_dump_json()
    assert bearer_secret not in result.stdout + result.stderr + serialized
    assert explicit_secret not in result.stdout + result.stderr + serialized
    expected = hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
    assert result.receipt.stdout_digest == f"sha256:{expected}"


def test_approval_is_exact_pre_spawn_and_nonce_is_single_use(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    marker = repository / "spawned.txt"
    command = spec(
        "sandbox-validation",
        "from pathlib import Path; p=Path('spawned.txt'); p.write_text((p.read_text() if p.exists() else '') + 'run\\n')",
        repository,
        artifact_paths=("spawned.txt",),
        requires_approval=ApprovalAction.SANDBOX_VALIDATION,
    )
    command_runner = runner(repository, (command,))

    with pytest.raises(PolicyViolation, match="requires an execution approval"):
        run_command(
            command_runner,
            "sandbox-validation",
            repository,
            decision_id="decision-1",
        )
    assert not marker.exists()

    approval = ExecutionApproval(
        decision_id="decision-1",
        request_id="request-1",
        action=ApprovalAction.SANDBOX_VALIDATION,
        artifact_digest=INPUT_DIGEST,
        base_revision=BASE_REVISION,
        command_id="sandbox-validation",
        environment=EnvironmentKind.LOCAL,
        reviewer="reviewer@example.test",
        nonce="one-use-approval",
    )
    with pytest.raises(PolicyViolation, match="base_revision does not match"):
        run_command(
            command_runner,
            "sandbox-validation",
            repository,
            base_revision="fffffff",
            decision_id="decision-1",
            approval=approval,
        )
    assert not marker.exists()

    result = run_command(
        command_runner,
        "sandbox-validation",
        repository,
        decision_id="decision-1",
        approval=approval,
    )
    assert result.receipt.artifacts[0].path == "spawned.txt"
    assert marker.read_text() == "run\n"

    with pytest.raises(PolicyViolation, match="nonce has already been used"):
        run_command(
            command_runner,
            "sandbox-validation",
            repository,
            decision_id="decision-1",
            approval=approval,
        )
    assert marker.read_text() == "run\n"


def test_declared_artifact_is_hashed_only_after_execution(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    command = spec(
        "fixture-artifact",
        "from pathlib import Path; Path('report.json').write_text('{\"ok\": true}')",
        repository,
        artifact_paths=("report.json",),
    )

    result = run_command(runner(repository, (command,)), "fixture-artifact", repository)

    artifact = result.receipt.artifacts[0]
    assert artifact.path == "report.json"
    assert (
        artifact.sha256 == "sha256:6bc0da1f42f96fc37b8bd7ed20ba57606d2a0da5cda2b135c7854fbdc985b8a3"
    )
    assert artifact.size_bytes == len('{"ok": true}')


def test_missing_or_symlink_artifact_cannot_be_claimed(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    missing = spec("fixture-missing", "pass", repository, artifact_paths=("missing.txt",))
    with pytest.raises(PolicyViolation, match="not produced"):
        run_command(runner(repository, (missing,)), "fixture-missing", repository)

    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    symlink = spec(
        "fixture-symlink",
        "import os; os.symlink('../outside.txt', 'artifact.txt')",
        repository,
        artifact_paths=("artifact.txt",),
    )
    with pytest.raises(PolicyViolation, match="symlink|escapes"):
        run_command(runner(repository, (symlink,)), "fixture-symlink", repository)


def test_failure_preserves_nonzero_exit(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    command = spec(
        "fixture-failure",
        "import sys; print('no', file=sys.stderr); sys.exit(7)",
        repository,
    )

    result = run_command(runner(repository, (command,)), "fixture-failure", repository)

    assert result.receipt.exit_code == 7
    assert "no" in result.stderr


def test_timeout_is_a_terminal_failure_with_bounded_evidence(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    command = spec(
        "fixture-timeout",
        "import time; print('started', flush=True); time.sleep(2)",
        repository,
    )
    command_runner = runner(repository, (command,), timeout_seconds=0.05)

    result = run_command(command_runner, "fixture-timeout", repository)

    assert result.timed_out is True
    assert result.receipt.terminal is True
    assert result.receipt.exit_code == 124
    assert "timed out" in result.stderr
    assert result.receipt.stdout_digest.startswith("sha256:")


def test_output_limit_terminates_process_before_unbounded_capture(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    command = spec(
        "fixture-output-limit",
        "import os; chunk = b'x' * 8192; exec(\"while True:\\n    os.write(1, chunk)\")",
        repository,
    )

    result = run_command(
        runner(repository, (command,), max_output_chars=64),
        "fixture-output-limit",
        repository,
    )

    assert result.receipt.exit_code == OUTPUT_LIMIT_EXIT_CODE
    assert result.timed_out is False
    assert len(result.stdout) == 64
    assert result.stdout.endswith("...[output truncated]")
    assert "output limit" in result.stderr


def test_timeout_terminates_descendant_processes(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    child = (
        "import time; from pathlib import Path; time.sleep(0.4); "
        "Path('descendant-survived.txt').write_text('survived')"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(2)"
    )
    command = spec("fixture-descendant-timeout", parent, repository)

    result = run_command(
        runner(repository, (command,), timeout_seconds=0.05),
        "fixture-descendant-timeout",
        repository,
    )
    time.sleep(0.6)

    assert result.timed_out is True
    assert not (repository / "descendant-survived.txt").exists()


def test_unknown_command_is_rejected_before_execution(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    command_runner = runner(repository, (spec("known", "pass", repository),))

    with pytest.raises(PolicyViolation, match="unknown command_id"):
        run_command(command_runner, "invented-command", repository)


def test_working_directory_escape_is_rejected_even_if_registry_allows_it(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    command_runner = runner(repository, (spec("misconfigured", "pass", outside),))

    with pytest.raises(PolicyViolation, match="escapes repository root"):
        run_command(command_runner, "misconfigured", outside)
