"""Deprecated false-provenance Claude/Ollama server shim.

This historical executable is retained for traceability but intentionally
fails closed before parsing runtime options, importing a provider client, or
starting a server. It previously presented remote Claude calls as
``ollama`` / ``local_loopback``, which cannot produce truthful evidence. Use
``legacy-migration-agent ui --claude-model ...`` instead.

Invoking this file with any options exits nonzero with the replacement command.
The argument parser remains importable only so historical imports do not break.
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
    del argv
    print(
        "[live_claude_serve] Disabled: this historical Claude/Ollama compatibility "
        "shim would record false provider provenance. Use the truthful first-class "
        "command `legacy-migration-agent ui --claude-model ...` instead.",
        file=sys.stderr,
        flush=True,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
