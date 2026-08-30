"""Serve the Agent UI in a deterministic "record mode" for browser E2E drives.

This boots the same production UI server (``build_ui_server`` / ``serve_ui``)
but swaps the loopback Ollama client for the recorded test double that the
pytest suite uses (``tests/ui_test_doubles.py``), so a full migration can be
driven end-to-end through a real browser with no live model and no network
access. The Engineer/Architect/Validator model output is frozen; the local
validation toolchain (Jest, sfdx-scanner, etc.) still runs for real.

This file is intentionally the only new file needed to make the recorded
double reachable outside pytest: it adds ``tests/`` to ``sys.path`` at run
time (replicating pytest's own "rootdir insertion" import behavior for a
``tests/`` directory with no ``__init__.py``) instead of editing any existing
file under ``src/`` or ``tests/``.

Usage:
    uv run python tooling/e2e/record_mode_serve.py --port 8899
    uv run python tooling/e2e/record_mode_serve.py --scenario-id salesforce-vf-to-lwc --port 8899

Env var fallbacks (used only when the matching CLI flag is omitted):
    RECORD_MODE_PORT, RECORD_MODE_SCENARIO_ID, RECORD_MODE_PROJECT_ROOT,
    RECORD_MODE_MODEL_ID, RECORD_MODE_TIMEOUT_SECONDS
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"

# The recorded model doubles live under tests/ as plain top-level modules
# (e.g. `ui_test_doubles.py`, `salesforce_candidate_factory.py`) with no
# package __init__.py. pytest makes `from ui_test_doubles import ...` work by
# inserting that directory onto sys.path itself; we replicate exactly that
# here so the same doubles are importable from a standalone script.
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

_SUPPORTED_SCENARIO_IDS = (
    "salesforce-vf-to-lwc",
    "case-management-console",
    "mulesoft-mule3-to-mule4",
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(os.environ.get("RECORD_MODE_PROJECT_ROOT", str(_REPO_ROOT))),
        help="Project root passed to build_ui_server (default: repo root).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("RECORD_MODE_PORT", "8899")),
        help="Loopback port to serve on; 0 asks the OS for a free port.",
    )
    parser.add_argument(
        "--scenario-id",
        default=os.environ.get("RECORD_MODE_SCENARIO_ID", "salesforce-vf-to-lwc"),
        choices=_SUPPORTED_SCENARIO_IDS,
        help="Scenario the recorded double is expected to drive (informational; "
        "the double already supports every scenario id above).",
    )
    parser.add_argument(
        "--model-id",
        default=os.environ.get("RECORD_MODE_MODEL_ID", "recorded-e2e-model"),
        help="Synthetic model id presented by the UI (never called live).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.environ.get("RECORD_MODE_TIMEOUT_SECONDS", "180.0")),
        help="Timeout the double must be constructed with (must match the "
        "runtime's DEFAULT_OLLAMA_TIMEOUT_SECONDS unless overridden).",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the default system browser once the server is ready.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    project_root = args.project_root.resolve()

    # Import the recorded double lazily, after sys.path is patched above.
    from ui_test_doubles import LOCAL_MODEL_REVISION, make_ollama_client_test_double

    _RecordedDouble = make_ollama_client_test_double(project_root)

    class _BrowsableOllamaDouble(_RecordedDouble):  # type: ignore[misc, valid-type]
        """Add the direct inventory probe the UI's /api/readiness check calls.

        The recorded double only implements the ``parse``/``bind_model_revision``
        seam used during a run. The UI readiness endpoint (AgentUiService.
        runtime_readiness) additionally reaches into
        ``models.architect._resolve_model_revision(...)`` directly, outside any
        run, to decide whether the browser's Send/Start-migration controls may
        be enabled. Without this, the browser flow stays disabled even though
        the recorded double is otherwise fully wired.
        """

        def _resolve_model_revision(self, *, timeout_seconds: float) -> str:
            del timeout_seconds
            return LOCAL_MODEL_REVISION

    # Import the production application module and monkeypatch the exact
    # seam the UI resolves models through:
    #   AgentUiService._local_models -> build_local_ollama_model_clients
    #   -> legacy_migration_agent.application.agent_run.OllamaStructuredModelClient
    import legacy_migration_agent.application.agent_run as agent_run_module

    agent_run_module.OllamaStructuredModelClient = _BrowsableOllamaDouble

    from legacy_migration_agent.ui.server import serve_ui

    print(
        f"[record_mode_serve] scenario_id={args.scenario_id!r} "
        f"model_id={args.model_id!r} project_root={project_root} "
        "(recorded double active; no live model, no network)",
        flush=True,
    )
    serve_ui(
        project_root,
        port=args.port,
        open_browser=args.open_browser,
        ollama_model_id=args.model_id,
        ollama_timeout_seconds=args.timeout_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
