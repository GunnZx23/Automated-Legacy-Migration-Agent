from pathlib import Path

import pytest

import legacy_migration_agent.core.integrity as integrity_module
from legacy_migration_agent.core.integrity import (
    ArtifactStore,
    artifact_digest,
    canonical_json_bytes,
)
from legacy_migration_agent.core.policies import PolicyViolation


def test_digest_is_independent_of_mapping_order():
    left = {"request_id": "request-1", "paths": ["a", "b"]}
    right = {"paths": ["a", "b"], "request_id": "request-1"}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert artifact_digest(left) == artifact_digest(right)
    assert artifact_digest(left).startswith("sha256:")


def test_store_is_idempotent_but_immutable(tmp_path: Path):
    store = ArtifactStore(tmp_path / "evidence")
    first = store.write_json("runs/run-1/manifest.json", {"manifest_id": "manifest-1"})
    second = store.write_json("runs/run-1/manifest.json", {"manifest_id": "manifest-1"})
    assert first == second
    assert store.read_json("runs/run-1/manifest.json") == {"manifest_id": "manifest-1"}
    with pytest.raises(PolicyViolation, match="different content"):
        store.write_json("runs/run-1/manifest.json", {"manifest_id": "tampered"})


def test_store_reopens_parent_safely_after_concurrent_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ArtifactStore(tmp_path / "evidence")
    original_mkdir = integrity_module.os.mkdir
    raced = False

    def concurrent_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal raced
        if path == "shared" and not raced:
            raced = True
            original_mkdir(path, mode=mode, dir_fd=dir_fd)
            raise FileExistsError(path)
        return original_mkdir(path, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(integrity_module.os, "mkdir", concurrent_mkdir)
    store.write_json("shared/nested/artifact.json", {"safe": True})

    assert raced
    assert store.read_json("shared/nested/artifact.json") == {"safe": True}


def test_store_rejects_symlink_escape(tmp_path: Path):
    store = ArtifactStore(tmp_path / "evidence")
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PolicyViolation, match="escapes store root"):
        store.write_json("escape/new/manifest.json", {"safe": False})
    assert not (outside / "new").exists()


def test_store_rejects_symlink_destination(tmp_path: Path):
    store = ArtifactStore(tmp_path / "evidence")
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (store.root / "receipt.json").symlink_to(outside)
    with pytest.raises(PolicyViolation, match="safe regular file"):
        store.write_json("receipt.json", {"safe": False})


def test_store_rejects_traversal(tmp_path: Path):
    store = ArtifactStore(tmp_path / "evidence")
    with pytest.raises(ValueError, match="parent-directory"):
        store.write_json("../manifest.json", {"safe": False})
