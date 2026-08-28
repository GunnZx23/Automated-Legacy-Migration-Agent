from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from legacy_migration_agent.application.candidate_export import (
    build_candidate_archive,
    export_candidate,
)
from legacy_migration_agent.core.policies import PolicyViolation

HANDLE = "0123456789abcdef01234567"
MANIFEST_DIGEST = "sha256:" + "a" * 64
CHANGE_SET_DIGEST = "sha256:" + "b" * 64
FILES = (
    (
        "force-app/main/default/lwc/accountExplorer/accountExplorer.js",
        "export default class AccountExplorer {}\n",
    ),
    (
        "force-app/main/default/lwc/accountExplorer/accountExplorer.html",
        "<template><p>Generated</p></template>\n",
    ),
)


def _export(
    project_root: Path,
    *,
    attempt: int = 1,
    files: tuple[tuple[str, str], ...] = FILES,
    disposition: str = "ready_for_human_review",
    manifest_digest: str = MANIFEST_DIGEST,
    base_files: tuple[tuple[str, str], ...] = (),
):
    return export_candidate(
        project_root=project_root,
        platform="salesforce",
        handle=HANDLE,
        attempt=attempt,  # type: ignore[arg-type]
        terminal_validation_disposition=disposition,
        manifest_digest=manifest_digest,
        change_set_digest=CHANGE_SET_DIGEST,
        files=files,
        base_files=base_files,
    )


def _assert_private_tree(root: Path) -> None:
    for path in root.rglob("*"):
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_dir():
            assert mode == 0o700, path
        else:
            assert mode == 0o600, path


def test_export_writes_exact_candidate_archive_and_sanitized_receipt(tmp_path: Path) -> None:
    source = tmp_path / "legacy.page"
    source.write_text("<apex:page>unchanged</apex:page>\n", encoding="utf-8")
    source_before = source.read_bytes()

    result = _export(tmp_path)

    assert result.schema_version == "1.0"
    assert result.platform == "salesforce"
    assert result.handle == HANDLE
    assert result.attempt == 1
    assert result.export_root == f"output/salesforce-{HANDLE}/attempt-1"
    assert result.candidate_path == f"{result.export_root}/candidate"
    assert result.archive_path == f"{result.export_root}/candidate.zip"
    assert result.receipt_path == f"{result.export_root}/export.json"
    assert result.file_count == 2
    assert result.archive_kind == "candidate_changes"
    assert result.archive_file_count == 2
    assert result.validation_disposition == "ready_for_human_review"
    assert result.ready_for_human_review is True
    assert tuple(entry.path for entry in result.files) == tuple(sorted(path for path, _ in FILES))

    candidate_root = tmp_path / result.candidate_path
    for path, content in FILES:
        assert (candidate_root / path).read_bytes() == content.encode("utf-8")

    archive_path = tmp_path / result.archive_path
    assert (
        result.archive_sha256 == "sha256:" + hashlib.sha256(archive_path.read_bytes()).hexdigest()
    )
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == sorted(path for path, _content in FILES)
        for path, content in FILES:
            assert archive.read(path) == content.encode("utf-8")
            info = archive.getinfo(path)
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert stat.S_IMODE(info.external_attr >> 16) == 0o600

    receipt_path = tmp_path / result.receipt_path
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert receipt["changed_paths"] == sorted(path for path, _content in FILES)
    assert receipt["candidate_digest"] == result.candidate_digest
    assert receipt["archive_sha256"] == result.archive_sha256
    assert receipt["archive_kind"] == "candidate_changes"
    assert receipt["archive_file_count"] == 2
    assert receipt["manifest_digest"] == MANIFEST_DIGEST
    assert receipt["change_set_digest"] == CHANGE_SET_DIGEST
    assert "prompt" not in receipt
    assert str(tmp_path).encode() not in receipt_bytes
    assert result.receipt_sha256 == "sha256:" + hashlib.sha256(receipt_bytes).hexdigest()
    assert source.read_bytes() == source_before
    _assert_private_tree(tmp_path / "output")


def test_archive_can_overlay_changes_on_frozen_source_without_expanding_candidate_delta(
    tmp_path: Path,
) -> None:
    base_files = (
        ("sfdx-project.json", '{"packageDirectories":[{"path":"force-app"}]}\n'),
        (FILES[0][0], "legacy implementation\n"),
        ("force-app/main/default/pages/Legacy.page", "<apex:page/>\n"),
    )

    result = _export(tmp_path, base_files=base_files)

    assert result.file_count == len(FILES)
    assert result.archive_kind == "source_plus_candidate_overlay"
    assert result.archive_file_count == 4
    with zipfile.ZipFile(tmp_path / result.archive_path) as archive:
        assert archive.namelist() == sorted(
            {
                "sfdx-project.json",
                "force-app/main/default/pages/Legacy.page",
                *(path for path, _content in FILES),
            }
        )
        assert archive.read(FILES[0][0]) == FILES[0][1].encode()
        assert archive.read("force-app/main/default/pages/Legacy.page") == b"<apex:page/>\n"
    assert sorted(
        path.relative_to(tmp_path / result.candidate_path).as_posix()
        for path in (tmp_path / result.candidate_path).rglob("*")
        if path.is_file()
    ) == sorted(path for path, _content in FILES)


def test_identical_replay_is_idempotent_and_does_not_replace_files(tmp_path: Path) -> None:
    first = _export(tmp_path)
    tracked = [
        tmp_path / first.archive_path,
        tmp_path / first.receipt_path,
        *(tmp_path / first.candidate_path / path for path, _content in FILES),
    ]
    identities = {path: (path.stat().st_ino, path.read_bytes()) for path in tracked}

    second = _export(tmp_path, files=tuple(reversed(FILES)))

    assert second == first
    assert {path: (path.stat().st_ino, path.read_bytes()) for path in tracked} == identities


def test_same_inputs_produce_identical_archives_and_receipts_across_roots(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir(mode=0o700)
    second_root.mkdir(mode=0o700)

    first = _export(first_root)
    second = _export(second_root)

    assert first == second
    assert (first_root / first.archive_path).read_bytes() == (
        second_root / second.archive_path
    ).read_bytes()
    assert (first_root / first.receipt_path).read_bytes() == (
        second_root / second.receipt_path
    ).read_bytes()


def test_different_existing_candidate_bytes_are_rejected_without_replacement(
    tmp_path: Path,
) -> None:
    first = _export(tmp_path)
    candidate = tmp_path / first.candidate_path / FILES[0][0]
    original = candidate.read_bytes()
    changed = ((FILES[0][0], "different bytes\n"), FILES[1])

    with pytest.raises(PolicyViolation, match="different bytes"):
        _export(tmp_path, files=changed)

    assert candidate.read_bytes() == original


def test_different_receipt_binding_is_rejected(tmp_path: Path) -> None:
    first = _export(tmp_path)
    receipt = tmp_path / first.receipt_path
    original = receipt.read_bytes()

    with pytest.raises(PolicyViolation, match="different bytes"):
        _export(tmp_path, manifest_digest="sha256:" + "c" * 64)

    assert receipt.read_bytes() == original


def test_conflicting_replay_with_additional_file_leaves_original_export_unchanged(
    tmp_path: Path,
) -> None:
    first_files = (("a.txt", "original\n"),)
    first = _export(tmp_path, files=first_files)
    export_root = tmp_path / first.export_root
    before = {
        path.relative_to(export_root).as_posix(): (path.stat().st_ino, path.read_bytes())
        for path in export_root.rglob("*")
        if path.is_file()
    }

    with pytest.raises(PolicyViolation, match="candidate.zip"):
        _export(
            tmp_path,
            files=(*first_files, ("b.txt", "must not be published\n")),
        )

    after = {
        path.relative_to(export_root).as_posix(): (path.stat().st_ino, path.read_bytes())
        for path in export_root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not (export_root / "candidate/b.txt").exists()
    assert _export(tmp_path, files=first_files) == first


@pytest.mark.parametrize(
    "path",
    (
        "../outside.js",
        "/absolute.js",
        "force-app\\component.js",
        " force-app/component.js",
        "force-app//component.js",
        "force-app/./component.js",
    ),
)
def test_noncanonical_and_traversal_paths_are_rejected_before_export(
    tmp_path: Path,
    path: str,
) -> None:
    with pytest.raises(ValueError):
        _export(tmp_path, files=((path, "content\n"),))

    assert not (tmp_path / "output").exists()


def test_duplicate_and_file_directory_conflicts_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicated"):
        _export(tmp_path, files=(("a.js", "one"), ("a.js", "two")))
    with pytest.raises(ValueError, match="descendant"):
        _export(tmp_path, files=(("folder", "file"), ("folder/child.js", "child")))

    assert not (tmp_path / "output").exists()


def test_symlink_parent_is_rejected_without_writing_outside(tmp_path: Path) -> None:
    candidate = _precreate_candidate_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (candidate / "force-app").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PolicyViolation, match="symlink"):
        _export(tmp_path)

    assert list(outside.iterdir()) == []


def test_special_file_destination_is_rejected_without_blocking(tmp_path: Path) -> None:
    candidate = _precreate_candidate_root(tmp_path)
    fifo = candidate / "generated.js"
    os.mkfifo(fifo, mode=0o600)

    with pytest.raises(PolicyViolation, match="regular file"):
        _export(tmp_path, files=(("generated.js", "content\n"),))


def test_nonprivate_existing_export_directory_is_rejected(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir(mode=0o755)

    with pytest.raises(PolicyViolation, match="owner-private directory"):
        _export(tmp_path)


def test_attempts_are_isolated_and_nonready_disposition_does_not_block_export(
    tmp_path: Path,
) -> None:
    first = _export(tmp_path, attempt=1)
    second_files = (("src/main/mule/generated.xml", "<mule/>\n"),)
    second = _export(
        tmp_path,
        attempt=2,
        files=second_files,
        disposition="recoverable_failure",
    )

    assert first.export_root.endswith("attempt-1")
    assert second.export_root.endswith("attempt-2")
    assert second.ready_for_human_review is False
    assert second.validation_disposition == "recoverable_failure"
    assert (tmp_path / second.candidate_path / second_files[0][0]).read_text(
        encoding="utf-8"
    ) == second_files[0][1]
    assert (tmp_path / first.receipt_path).is_file()
    assert (tmp_path / second.receipt_path).is_file()


def test_identity_and_utf8_inputs_are_strictly_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="24 lowercase"):
        export_candidate(
            project_root=tmp_path,
            platform="salesforce",
            handle="not-a-handle",
            attempt=1,
            terminal_validation_disposition="ready_for_human_review",
            manifest_digest=MANIFEST_DIGEST,
            change_set_digest=CHANGE_SET_DIGEST,
            files=FILES,
        )
    with pytest.raises(ValueError, match="valid UTF-8"):
        _export(tmp_path, files=(("generated.js", "\ud800"),))

    assert not (tmp_path / "output").exists()


def test_secret_candidate_is_rejected_before_archive_or_output(tmp_path: Path) -> None:
    secret = "literal-client-secret-123456"
    files = (("generated.js", f'const client_secret = "{secret}";\n'),)

    with pytest.raises(PolicyViolation, match="candidate content"):
        build_candidate_archive(files)
    with pytest.raises(PolicyViolation, match="candidate content"):
        _export(tmp_path, files=files)

    assert not (tmp_path / "output").exists()
    assert not any(
        secret.encode() in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )


def test_benign_request_token_code_exports_exact_bytes(tmp_path: Path) -> None:
    content = "const token = ++this.requestGeneration;\nconst accessToken = response.accessToken;\n"
    files = (("generated.js", content),)

    archive = build_candidate_archive(files)
    result = _export(tmp_path, files=files)

    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        assert bundle.read("generated.js") == content.encode()
    assert (tmp_path / result.candidate_path / "generated.js").read_text(
        encoding="utf-8"
    ) == content


def _precreate_candidate_root(project_root: Path) -> Path:
    output = project_root / "output"
    run = output / f"salesforce-{HANDLE}"
    attempt = run / "attempt-1"
    candidate = attempt / "candidate"
    output.mkdir(mode=0o700)
    run.mkdir(mode=0o700)
    attempt.mkdir(mode=0o700)
    candidate.mkdir(mode=0o700)
    return candidate
