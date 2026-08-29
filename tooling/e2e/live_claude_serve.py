"""Serve the Agent UI with a **live Claude** model provider for the demo.

This boots the same production UI server (``serve_ui``) but swaps the loopback
Ollama client for :class:`ClaudeCliStructuredModelClient`, so a full migration
can be driven end-to-end through the real browser UI against a live,
Bedrock-backed Claude model. The Architect / Engineer / Validator outputs are
generated live; the local validation toolchain (Jest, sfdx-scanner, etc.) still
runs for real.

Run it under a debugger (see ``.vscode/launch.json``) to set breakpoints in the
UI service, the agent run pipeline, or ``ClaudeCliStructuredModelClient.parse``
and watch a live generation step by step.

Honesty note: the UI's provider badge and the durable model-call records will
still read as the ``ollama`` / ``local_loopback`` runtime, because this client
is injected at that seam unchanged. The ``claude`` CLI actually reaches a remote
model — the label describes the injection seam, not the network path. See
``claude_cli_client.py`` for the full rationale.

Usage:
    python tooling/e2e/live_claude_serve.py --port 8899
    python tooling/e2e/live_claude_serve.py --model-id claude-sonnet-5 --open-browser

Env var fallbacks (used only when the matching CLI flag is omitted):
    LIVE_CLAUDE_PORT, LIVE_CLAUDE_MODEL_ID, LIVE_CLAUDE_PROJECT_ROOT,
    LIVE_CLAUDE_TIMEOUT_SECONDS
    LIVE_CLAUDE_CLI_PATH — explicit path to the ``claude`` executable.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_E2E_DIR = Path(__file__).resolve().parent

# The live Claude client lives beside this script as a plain top-level module
# (no package __init__.py under tooling/e2e/), so make it importable directly.
if str(_E2E_DIR) not in sys.path:
    sys.path.insert(0, str(_E2E_DIR))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(os.environ.get("LIVE_CLAUDE_PROJECT_ROOT", str(_REPO_ROOT))),
        help="Project root passed to serve_ui (default: repo root).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LIVE_CLAUDE_PORT", "8899")),
        help="Loopback port to serve on; 0 asks the OS for a free port.",
    )
    parser.add_argument(
        "--model-id",
        default=os.environ.get("LIVE_CLAUDE_MODEL_ID", "claude-sonnet-5"),
        help="Claude model id passed to the claude CLI (default: claude-sonnet-5).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.environ.get("LIVE_CLAUDE_TIMEOUT_SECONDS", "240.0")),
        help="Per-call deadline for a live claude generation (default: 240s).",
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

    from claude_cli_client import ClaudeCliStructuredModelClient

    # Monkeypatch the exact seam the UI resolves models through:
    #   AgentUiService._local_models -> build_local_ollama_model_clients
    #   -> legacy_migration_agent.application.agent_run.OllamaStructuredModelClient
    # The UI passes timeout_seconds=<serve_ui ollama_timeout_seconds>, i.e. the
    # same --timeout-seconds value below, straight through to the client.
    import legacy_migration_agent.application.agent_run as agent_run_module

    agent_run_module.OllamaStructuredModelClient = ClaudeCliStructuredModelClient

    from legacy_migration_agent.ui.server import serve_ui

    print(
        f"[live_claude_serve] LIVE Claude provider active · model_id={args.model_id!r} "
        f"timeout={args.timeout_seconds}s project_root={project_root}",
        flush=True,
    )
    print(
        "[live_claude_serve] NOTE: the UI badge / model-call records read as "
        "'ollama'/'local_loopback' (the injection seam); the claude CLI reaches a "
        "remote Bedrock-backed model. This is a demo hook, not a production boundary.",
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
