"""Guard: production code never imports a test-only passing-candidate double.

``tests/salesforce_candidate_factory.py`` builds a byte-perfect passing candidate
in memory, and ``tooling/e2e/record_mode_serve.py`` replays canned responses with
no live model. Both exist only for tests and offline record/replay. If any module
under ``src/`` imported one of them, a canned green candidate could reach a real
run and mislabel it as a genuine model pass. This test fails closed on that
regression by parsing every production module's imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).parents[1] / "src"
FORBIDDEN_MODULE_SUBSTRINGS = ("salesforce_candidate_factory", "record_mode_serve")


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize(
    "source_file",
    sorted(SRC_ROOT.rglob("*.py")),
    ids=lambda path: str(path.relative_to(SRC_ROOT.parent)),
)
def test_production_code_never_imports_candidate_doubles(source_file: Path) -> None:
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    offending = tuple(
        module
        for module in sorted(_imported_modules(tree))
        if any(token in module for token in FORBIDDEN_MODULE_SUBSTRINGS)
    )
    assert not offending, (
        f"{source_file} imports a test-only candidate double: {', '.join(offending)}"
    )
