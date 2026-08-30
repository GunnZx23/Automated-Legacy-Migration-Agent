"""Fail-closed contracts for the historical false-provenance Claude shims."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelConfigurationError,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_E2E_DIR = _REPO_ROOT / "tooling" / "e2e"
_TRUTHFUL_COMMAND = "legacy-migration-agent ui --claude-model ..."


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _forbid_subprocess(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    del args, kwargs
    raise AssertionError("the deprecated shim attempted to invoke a subprocess")


def test_deprecated_client_constructor_fails_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module(
        "deprecated_claude_cli_client_constructor_test",
        _E2E_DIR / "claude_cli_client.py",
    )
    monkeypatch.setattr(module.subprocess, "run", _forbid_subprocess)
    assert module.ClaudeCliStructuredModelClient.provider == "disabled-deprecated-shim"
    assert module.ClaudeCliStructuredModelClient.execution_boundary == "offline_recorded"

    with pytest.raises(ModelConfigurationError, match="false provider provenance") as exc_info:
        module.ClaudeCliStructuredModelClient(
            "claude-sonnet-5",
            approval=LiveModelApproval(
                allow_live_api=True,
                allow_prompt_data_sharing=True,
                approved_by="test-operator",
                approved_remote_provider_id="bedrock",
            ),
        )

    assert _TRUTHFUL_COMMAND in str(exc_info.value)


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        ("_resolve_model_revision", {"timeout_seconds": 1.0}),
        ("_invoke", {"system_prompt": "system", "user_prompt": "input"}),
        ("bind_model_revision", {"expected_revision": "not-a-digest"}),
        (
            "parse",
            {"system_prompt": "", "input_value": object(), "output_type": object},
        ),
    ],
)
def test_deprecated_client_methods_remain_fail_closed_when_construction_is_bypassed(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    kwargs: dict[str, object],
) -> None:
    module = _load_module(
        f"deprecated_claude_cli_client_{method_name}_test",
        _E2E_DIR / "claude_cli_client.py",
    )
    monkeypatch.setattr(module.subprocess, "run", _forbid_subprocess)
    client = object.__new__(module.ClaudeCliStructuredModelClient)

    with pytest.raises(ModelConfigurationError, match="false provider provenance") as exc_info:
        getattr(client, method_name)(**kwargs)

    assert _TRUTHFUL_COMMAND in str(exc_info.value)


def test_deprecated_server_main_never_parses_options_or_starts_server(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_module(
        "deprecated_live_claude_serve_test",
        _E2E_DIR / "live_claude_serve.py",
    )

    def forbidden_parse(argv: list[str] | None = None) -> object:
        del argv
        raise AssertionError("the deprecated server attempted to parse launch options")

    monkeypatch.setattr(module, "_parse_args", forbidden_parse)

    assert module.main(["--port", "8899", "--open-browser"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "false provider provenance" in captured.err
    assert _TRUTHFUL_COMMAND in captured.err


def test_deprecated_server_script_exits_nonzero_with_guidance() -> None:
    completed = subprocess.run(
        [sys.executable, str(_E2E_DIR / "live_claude_serve.py"), "--port", "8899"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "false provider provenance" in completed.stderr
    assert _TRUTHFUL_COMMAND in completed.stderr


def test_deprecated_client_script_exits_nonzero_with_guidance() -> None:
    completed = subprocess.run(
        [sys.executable, str(_E2E_DIR / "claude_cli_client.py")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "false provider provenance" in completed.stderr
    assert _TRUTHFUL_COMMAND in completed.stderr
