"""Command-line interface for the capstone's local, human-gated workflow."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import cast

from legacy_migration_agent.cli_parser import build_parser

CommandHandler = Callable[[argparse.Namespace], int]

__all__ = ["build_parser", "main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Parse and execute one local capstone command."""

    args = build_parser().parse_args(argv)
    handler = cast(CommandHandler, args.command_handler)
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
