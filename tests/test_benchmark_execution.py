from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from legacy_migration_agent.benchmark_execution import (
    BenchmarkExecutionAnchor,
    build_benchmark_execution_anchor,
    load_verified_benchmark_execution_anchor,
    verify_benchmark_execution_anchor,
    write_benchmark_execution_anchor,
)
from legacy_migration_agent.core.policies import PolicyViolation

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_IDENTITY = "sha256:" + "a" * 64
OTHER_RUNTIME_IDENTITY = "sha256:" + "b" * 64
CREATED_AT = datetime(2026, 8, 29, 7, 30, tzinfo=UTC)


def _run_git(project: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(project), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"),
    )


def _copy_runtime_project(destination: Path) -> None:
    for relative in (
        "src/legacy_migration_agent",
        "agents",
        "knowledge/wiki",
        "evaluation/benchmark-v2",
        "fixtures",
        "tooling/mulesoft-runtime",
    ):
        source = PROJECT_ROOT.joinpath(*relative.split("/"))
        _copy_tree(source, destination.joinpath(*relative.split("/")))

    lwc_source = PROJECT_ROOT / "tooling/lwc-jest"
    lwc_destination = destination / "tooling/lwc-jest"
    _copy_tree(lwc_source, lwc_destination)
    for relative in (
        "evaluation/mulesoft-customer-status-api-source-edges.json",
        "evaluation/salesforce-account-contact-explorer-source-edges.json",
        "pyproject.toml",
        "uv.lock",
    ):
        source = PROJECT_ROOT.joinpath(*relative.split("/"))
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _copy_runtime_project(root)
    _run_git(root, "init", "--quiet")
    _run_git(root, "config", "user.name", "Benchmark Test")
    _run_git(root, "config", "user.email", "benchmark@example.invalid")
    _run_git(root, "config", "commit.gpgsign", "false")
    _run_git(root, "add", "--all")
    _run_git(root, "commit", "--quiet", "-m", "frozen benchmark inputs")
    return root


def _build(
    project: Path, *, anchor_id: str = "benchmark-v2-pre-run-01"
) -> BenchmarkExecutionAnchor:
    return build_benchmark_execution_anchor(
        project,
        runtime_identity_digest=RUNTIME_IDENTITY,
        created_at=CREATED_AT,
        anchor_id=anchor_id,
    )


def _append(path: Path, value: str = "\n# drift\n") -> None:
    path.write_text(path.read_text(encoding="utf-8") + value, encoding="utf-8")


def test_build_binds_complete_sorted_runtime_authority_without_clean_worktree(
    project: Path,
) -> None:
    unrelated = project / ".runs/untracked-result.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("{}\n", encoding="utf-8")

    anchor = _build(project)

    assert anchor.provider_id == "claude-cli"
    assert anchor.model_id == "claude-sonnet-5"
    assert anchor.execution_boundary == "remote_provider_managed"
    assert anchor.runtime_identity_digest == RUNTIME_IDENTITY
    assert anchor.git_head_commit_oid == _run_git(project, "rev-parse", "HEAD^{commit}")
    assert anchor.git_head_tree_oid == _run_git(project, "rev-parse", "HEAD^{tree}")
    paths = tuple(entry.path for entry in anchor.runtime_inventory)
    assert paths == tuple(sorted(paths))
    assert {
        "src/legacy_migration_agent",
        "agents",
        "knowledge/wiki",
        "evaluation/benchmark-v2/declaration.json",
        "evaluation/benchmark-v2/registry.json",
        "fixtures/mulesoft/customer-status-api/input",
        "fixtures/salesforce/account-contact-explorer/input",
        "fixtures/salesforce/case-management-console/input",
        "tooling/lwc-jest/package-lock.json",
        "tooling/mulesoft-runtime/behavior-contract.json",
        "pyproject.toml",
        "uv.lock",
    }.issubset(paths)
    assert ".runs/untracked-result.json" not in paths
    assert anchor.wiki_tree_revision == next(
        entry.revision for entry in anchor.runtime_inventory if entry.path == "knowledge/wiki"
    )
    assert tuple(item.path for item in anchor.lockfile_digests) == (
        "tooling/lwc-jest/package-lock.json",
        "uv.lock",
    )
    assert anchor.anchor_digest.startswith("sha256:")
    assert len(anchor.anchor_digest) == 71


def test_python_bytecode_cache_is_not_source_authority(project: Path) -> None:
    anchor = _build(project)
    cache = project / "src/legacy_migration_agent/__pycache__/generated.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"derived interpreter output")

    assert verify_benchmark_execution_anchor(project, anchor) == anchor


def test_write_is_immutable_idempotent_and_load_verifies(project: Path) -> None:
    anchor = _build(project)
    destination = project / ".runs/benchmark-v2/execution-anchor.json"

    assert write_benchmark_execution_anchor(destination, anchor) == destination
    assert write_benchmark_execution_anchor(destination, anchor) == destination
    loaded = load_verified_benchmark_execution_anchor(
        project,
        destination,
        expected_runtime_identity_digest=RUNTIME_IDENTITY,
    )

    assert loaded == anchor
    assert loaded.anchor_digest == anchor.anchor_digest
    parsed = json.loads(destination.read_text(encoding="utf-8"))
    assert parsed["anchor_id"] == anchor.anchor_id
    assert destination.stat().st_mode & 0o777 == 0o600

    different = anchor.model_copy(update={"anchor_id": "benchmark-v2-pre-run-02"})
    with pytest.raises(PolicyViolation, match="already exists"):
        write_benchmark_execution_anchor(destination, different)
    assert load_verified_benchmark_execution_anchor(project, destination) == anchor


def test_load_rejects_duplicate_json_keys_before_model_validation(project: Path) -> None:
    destination = project / ".runs/duplicate-anchor.json"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b'{"schema_version":"1.0","schema_version":"1.0"}\n')

    with pytest.raises(PolicyViolation, match="duplicate JSON key 'schema_version'"):
        load_verified_benchmark_execution_anchor(project, destination)


@pytest.mark.parametrize(
    "relative_path",
    (
        "fixtures/mulesoft/customer-status-api/input/legacy-mule3/customer-status-api/"
        "src/main/app/mule-app.properties",
        "knowledge/wiki/pages/salesforce-visualforce-to-lwc.md",
        "src/legacy_migration_agent/benchmark_execution.py",
        "tooling/lwc-jest/package-lock.json",
        "uv.lock",
    ),
    ids=("source", "wiki", "code", "npm-lock", "python-lock"),
)
def test_verify_rejects_runtime_content_drift(project: Path, relative_path: str) -> None:
    anchor = _build(project)
    _append(project.joinpath(*relative_path.split("/")))

    with pytest.raises(PolicyViolation):
        verify_benchmark_execution_anchor(
            project,
            anchor,
            expected_runtime_identity_digest=RUNTIME_IDENTITY,
        )


def test_verify_allows_later_commit_outside_runtime_inventory(
    project: Path,
) -> None:
    anchor = _build(project)
    unrelated = project / "NON_RUNTIME_NOTE.md"
    unrelated.write_text("changes only the Git tree\n", encoding="utf-8")
    _run_git(project, "add", "NON_RUNTIME_NOTE.md")
    _run_git(project, "commit", "--quiet", "-m", "change Git identity")

    assert _run_git(project, "rev-parse", "HEAD^{commit}") != anchor.git_head_commit_oid
    assert _run_git(project, "rev-parse", "HEAD^{tree}") != anchor.git_head_tree_oid
    assert verify_benchmark_execution_anchor(project, anchor) == anchor


def test_verify_rejects_tampered_git_provenance_pair(project: Path) -> None:
    anchor = _build(project)
    unrelated = project / "NON_RUNTIME_NOTE.md"
    unrelated.write_text("changes only the Git tree\n", encoding="utf-8")
    _run_git(project, "add", "NON_RUNTIME_NOTE.md")
    _run_git(project, "commit", "--quiet", "-m", "create another valid tree")

    current_tree = _run_git(project, "rev-parse", "HEAD^{tree}")
    tree_tampered = BenchmarkExecutionAnchor.model_validate(
        {**anchor.model_dump(mode="python"), "git_head_tree_oid": current_tree}
    )
    with pytest.raises(PolicyViolation, match="does not reference original tree"):
        verify_benchmark_execution_anchor(project, tree_tampered)


@pytest.mark.parametrize(
    ("field", "object_type"),
    (
        ("git_head_commit_oid", "commit"),
        ("git_head_tree_oid", "tree"),
    ),
)
def test_verify_rejects_missing_original_git_object(
    project: Path,
    field: str,
    object_type: str,
) -> None:
    anchor = _build(project)
    missing_object = "f" * 40
    tampered = BenchmarkExecutionAnchor.model_validate(
        {**anchor.model_dump(mode="python"), field: missing_object}
    )

    with pytest.raises(
        PolicyViolation,
        match=rf"original Git {object_type} object is unavailable or corrupt",
    ):
        verify_benchmark_execution_anchor(project, tampered)


def test_load_rejects_wrong_claude_runtime_identity(project: Path) -> None:
    anchor = _build(project)
    destination = project / ".runs/execution-anchor.json"
    write_benchmark_execution_anchor(destination, anchor)

    with pytest.raises(PolicyViolation, match="Claude runtime identity differs"):
        load_verified_benchmark_execution_anchor(
            project,
            destination,
            expected_runtime_identity_digest=OTHER_RUNTIME_IDENTITY,
        )


def test_verify_rejects_symlink_inside_runtime_inventory(project: Path) -> None:
    anchor = _build(project)
    page = project / "knowledge/wiki/pages/salesforce-visualforce-to-lwc.md"
    replacement = project / "replacement-wiki-page.md"
    replacement.write_text(page.read_text(encoding="utf-8"), encoding="utf-8")
    page.unlink()
    page.symlink_to(replacement)

    with pytest.raises(PolicyViolation, match="symlink"):
        verify_benchmark_execution_anchor(project, anchor)


def test_load_rejects_symlink_in_anchor_path_component(project: Path) -> None:
    anchor = _build(project)
    real = project / ".runs/real/execution-anchor.json"
    write_benchmark_execution_anchor(real, anchor)
    link = project / ".runs/link"
    link.symlink_to(real.parent, target_is_directory=True)

    with pytest.raises(PolicyViolation, match="symlink|unsafe directory"):
        load_verified_benchmark_execution_anchor(
            project,
            link / real.name,
            expected_runtime_identity_digest=RUNTIME_IDENTITY,
        )


def test_build_rejects_symlink_project_root(project: Path, tmp_path: Path) -> None:
    link = tmp_path / "project-link"
    link.symlink_to(project, target_is_directory=True)

    with pytest.raises(PolicyViolation, match="non-symlink directory"):
        _build(link)


def test_write_rejects_destination_inside_runtime_inventory(project: Path) -> None:
    anchor = _build(project)
    destination = project / "src/legacy_migration_agent/execution-anchor.json"

    with pytest.raises(PolicyViolation, match="outside the runtime inventory"):
        write_benchmark_execution_anchor(destination, anchor)
    assert not destination.exists()


def test_anchor_requires_timezone_aware_created_at(project: Path) -> None:
    with pytest.raises(PolicyViolation, match="inputs are invalid"):
        build_benchmark_execution_anchor(
            project,
            runtime_identity_digest=RUNTIME_IDENTITY,
            created_at=datetime(2026, 8, 29),
            anchor_id="benchmark-v2-pre-run-01",
        )


def test_anchor_json_permissions_are_not_affected_by_umask(
    project: Path,
) -> None:
    anchor = _build(project)
    destination = project / ".runs/private/execution-anchor.json"
    original_umask = os.umask(0)
    try:
        write_benchmark_execution_anchor(destination, anchor)
    finally:
        os.umask(original_umask)
    assert destination.stat().st_mode & 0o777 == 0o600
