from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from salesforce_candidate_factory import salesforce_candidate_outputs

from legacy_migration_agent.core.workspace import (
    IsolatedWorkspace,
    WorkspaceViolation,
    content_revision,
    snapshot_tree,
)


def write(path: Path, content: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def test_actual_diff_is_exact_and_source_remains_byte_identical(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write(source / "modified.txt", "old\n")
    write(source / "deleted.txt", "retire me\n")
    write(source / "unchanged.txt", "same\n")
    original = snapshot_tree(source)

    with IsolatedWorkspace(
        source,
        ("modified.txt", "deleted.txt", "nested/added.txt"),
        temp_parent=tmp_path,
    ) as workspace:
        workspace.write_text("modified.txt", "updated\n")
        workspace.delete("deleted.txt")
        workspace.write_text("nested/added.txt", "new\n")
        changes = workspace.audit_changes()

        assert changes.added_paths == ("nested/added.txt",)
        assert changes.modified_paths == ("modified.txt",)
        assert changes.deleted_paths == ("deleted.txt",)
        assert changes.changed_paths == (
            "deleted.txt",
            "modified.txt",
            "nested/added.txt",
        )
        assert changes.unified_diff == (
            "diff --git a/deleted.txt b/deleted.txt\n"
            "deleted file mode 100644\n"
            "--- a/deleted.txt\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-retire me\n"
            "diff --git a/modified.txt b/modified.txt\n"
            "--- a/modified.txt\n"
            "+++ b/modified.txt\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+updated\n"
            "diff --git a/nested/added.txt b/nested/added.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/nested/added.txt\n"
            "@@ -0,0 +1 @@\n"
            "+new\n"
        )
        workspace.assert_source_unchanged()
        assert snapshot_tree(source) == original

    assert snapshot_tree(source) == original


def test_audit_uses_disk_and_rejects_unapproved_actual_change(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write(source / "approved.txt", "base\n")
    with IsolatedWorkspace(source, ("approved.txt",), temp_parent=tmp_path) as workspace:
        # Simulate a generated tool bypassing the public write method.
        write(workspace.root / "rogue.txt", "hidden from caller metadata\n")
        with pytest.raises(WorkspaceViolation, match="outside the manifest: rogue.txt"):
            workspace.audit_changes()


def test_mutation_methods_require_an_exact_approved_path(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write(source / "directory" / "existing.txt", "base\n")
    with IsolatedWorkspace(source, ("directory",), temp_parent=tmp_path) as workspace:
        with pytest.raises(WorkspaceViolation, match="not exactly manifest-approved"):
            workspace.write_text("directory/new.txt", "no\n")
        with pytest.raises(WorkspaceViolation, match="invalid repository-relative path"):
            workspace.write_text("../escape.txt", "no\n")
        assert not (tmp_path / "escape.txt").exists()


def test_symlinks_are_rejected_in_source_and_workspace(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    write(outside, "outside\n")

    source_with_link = tmp_path / "source-with-link"
    source_with_link.mkdir()
    (source_with_link / "link.txt").symlink_to(outside)
    with pytest.raises(WorkspaceViolation, match="symlinks are not allowed"):
        IsolatedWorkspace(source_with_link, ("link.txt",), temp_parent=tmp_path)

    source = tmp_path / "source"
    source.mkdir()
    with IsolatedWorkspace(source, ("tamper.txt",), temp_parent=tmp_path) as workspace:
        (workspace.root / "tamper.txt").symlink_to(outside)
        with pytest.raises(WorkspaceViolation, match="symlinks are not allowed"):
            workspace.audit_changes()


def test_binary_writes_and_direct_changes_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with IsolatedWorkspace(
        source,
        ("write.dat", "direct.dat"),
        temp_parent=tmp_path,
    ) as workspace:
        with pytest.raises(WorkspaceViolation, match="binary content"):
            workspace.write_bytes("write.dat", b"text\x00binary")
        (workspace.root / "direct.dat").write_bytes(b"\xff\xfe")
        with pytest.raises(WorkspaceViolation, match="not valid UTF-8"):
            workspace.audit_changes()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is unavailable")
def test_unsupported_special_files_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "pipe")
    with pytest.raises(WorkspaceViolation, match="unsupported special file"):
        IsolatedWorkspace(source, ("pipe",), temp_parent=tmp_path)


def test_revision_and_diff_are_deterministic_across_absolute_locations(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    write(first / "nested" / "value.txt", "base\n")
    write(second / "nested" / "value.txt", "base\n")
    assert content_revision(first) == content_revision(second)

    diffs: list[str] = []
    revisions: list[str] = []
    for source in (first, second):
        with IsolatedWorkspace(
            source,
            ("nested/value.txt", "added.txt"),
            temp_parent=tmp_path,
        ) as workspace:
            workspace.write_text("nested/value.txt", "changed without newline")
            workspace.write_text("added.txt", "added\n")
            changes = workspace.audit_changes()
            diffs.append(changes.unified_diff)
            revisions.append(changes.after_revision)
    assert diffs[0] == diffs[1]
    assert revisions[0] == revisions[1]
    assert "\\ No newline at end of file" in diffs[0]


def test_rollback_restores_base_and_cleanup_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write(source / "base.txt", "base\n")
    workspace = IsolatedWorkspace(
        source,
        ("base.txt", "new.txt"),
        temp_parent=tmp_path,
    )
    root = workspace.root
    workspace.write_text("base.txt", "changed\n")
    workspace.write_text("new.txt", "new\n")
    workspace.rollback()
    changes = workspace.audit_changes()
    assert changes.changed_paths == ()
    assert changes.unified_diff == ""
    assert snapshot_tree(root) == snapshot_tree(source)
    workspace.cleanup()
    workspace.cleanup()
    assert workspace.closed
    assert not root.exists()
    with pytest.raises(WorkspaceViolation, match="already closed"):
        workspace.audit_changes()


def test_expected_revision_and_source_root_symlink_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write(source / "base.txt", "base\n")
    with pytest.raises(WorkspaceViolation, match="expected revision"):
        IsolatedWorkspace(
            source,
            ("base.txt",),
            temp_parent=tmp_path,
            expected_revision="sha256:" + "0" * 64,
        )

    source_link = tmp_path / "source-link"
    source_link.symlink_to(source, target_is_directory=True)
    with pytest.raises(WorkspaceViolation, match="source root cannot be a symlink"):
        IsolatedWorkspace(source_link, ("base.txt",), temp_parent=tmp_path)


def test_source_mutation_is_detected_and_never_hidden_by_workspace_cleanup(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write(source / "base.txt", "base\n")
    workspace = IsolatedWorkspace(source, ("base.txt",), temp_parent=tmp_path)
    try:
        write(source / "base.txt", "externally changed\n")
        with pytest.raises(WorkspaceViolation, match="source tree changed"):
            workspace.assert_source_unchanged()
    finally:
        workspace.cleanup()
    assert not workspace.root.exists()
    assert (source / "base.txt").read_text(encoding="utf-8") == "externally changed\n"


def test_synthetic_candidate_outputs_create_an_additive_migration(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "salesforce" / "account-contact-explorer"
    source = fixture / "input"
    outputs = salesforce_candidate_outputs()
    controller = "force-app/main/default/classes/AccountContactExplorerController.cls"
    component = "force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js"
    permission_set = (
        "force-app/main/default/permissionsets/AccountContactExplorerUser.permissionset-meta.xml"
    )
    source_before = snapshot_tree(source)

    with IsolatedWorkspace(
        source,
        (controller, component, permission_set),
        temp_parent=tmp_path,
        expected_revision=source_before.revision,
    ) as workspace:
        for path in (controller, component, permission_set):
            workspace.write_bytes(path, outputs[path])
        changes = workspace.audit_changes()
        assert changes.added_paths == (controller, component)
        assert changes.modified_paths == (permission_set,)
        assert changes.deleted_paths == ()
        assert changes.changed_paths == tuple(sorted((controller, component, permission_set)))
        assert "public with sharing class AccountContactExplorerController" in (
            workspace.root / controller
        ).read_text(encoding="utf-8")

    assert snapshot_tree(source) == source_before


def test_replaced_workspace_root_symlink_fails_closed_without_target_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "outside-target"
    source.mkdir()
    target.mkdir()
    write(source / "approved.txt", "source\n")
    write(target / "approved.txt", "outside\n")
    workspace = IsolatedWorkspace(source, ("approved.txt", "new.txt"), temp_parent=tmp_path)
    root = workspace.root
    shutil.rmtree(root)
    root.symlink_to(target, target_is_directory=True)
    try:
        operations = (
            lambda: workspace.write_text("approved.txt", "attack\n"),
            lambda: workspace.delete("approved.txt"),
            workspace.audit_changes,
            workspace.rollback,
        )
        for operation in operations:
            with pytest.raises(WorkspaceViolation, match="workspace root cannot be a symlink"):
                operation()
        assert (target / "approved.txt").read_text(encoding="utf-8") == "outside\n"
        assert not (target / "new.txt").exists()
    finally:
        workspace.cleanup()
    assert target.is_dir()
    assert (target / "approved.txt").read_text(encoding="utf-8") == "outside\n"


def test_replaced_workspace_root_directory_fails_identity_check(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write(source / "approved.txt", "source\n")
    workspace = IsolatedWorkspace(source, ("approved.txt",), temp_parent=tmp_path)
    root = workspace.root
    displaced = root.parent / "displaced-original"
    root.rename(displaced)
    root.mkdir()
    write(root / "approved.txt", "replacement\n")
    try:
        operations = (
            lambda: workspace.write_text("approved.txt", "attack\n"),
            lambda: workspace.delete("approved.txt"),
            workspace.audit_changes,
            workspace.rollback,
        )
        for operation in operations:
            with pytest.raises(WorkspaceViolation, match="workspace root identity changed"):
                operation()
        assert (root / "approved.txt").read_text(encoding="utf-8") == "replacement\n"
    finally:
        workspace.cleanup()


def test_same_content_source_directory_replacement_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    write(source / "base.txt", "same bytes\n")
    workspace = IsolatedWorkspace(source, ("base.txt",), temp_parent=tmp_path)
    original_source = tmp_path / "original-source"
    source.rename(original_source)
    source.mkdir()
    write(source / "base.txt", "same bytes\n")
    try:
        assert content_revision(source) == workspace.base_revision
        with pytest.raises(WorkspaceViolation, match="source identity changed"):
            workspace.assert_source_unchanged()
    finally:
        workspace.cleanup()
