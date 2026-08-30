from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from test_measured_evaluation import _receipts, _registry

import legacy_migration_agent.benchmark_corpus as benchmark_corpus_module
from legacy_migration_agent.benchmark_corpus import (
    BenchmarkCorpusManifest,
    BenchmarkCorpusRunReference,
    load_verified_benchmark_corpus,
)


def test_corpus_loader_derives_and_verifies_the_complete_18_cell_matrix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = _registry()
    receipts = _receipts(registry)
    references = tuple(
        BenchmarkCorpusRunReference(
            run_dir=f".runs/benchmark-v2/run-{index}",
            rubric_path=f"evaluation/benchmark-v2/rubrics/rubric-{index}.json",
            run_id=receipt.run_id,
            thread_id=f"thread-{index}",
        )
        for index, receipt in enumerate(receipts, start=1)
    )
    manifest = BenchmarkCorpusManifest(
        registry_id=registry.registry_id,
        execution_anchor_path="evaluation/benchmark-v2/results/execution-anchor.json",
        runs=references,
    )
    manifest_path = tmp_path / "corpus.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    pending = iter(receipts)
    monkeypatch.setattr(
        benchmark_corpus_module,
        "load_verified_benchmark_protocol",
        lambda _root: SimpleNamespace(registry=registry),
    )
    monkeypatch.setattr(
        benchmark_corpus_module,
        "extract_evaluation_cell_receipt",
        lambda *_args, **_kwargs: next(pending),
    )

    corpus = load_verified_benchmark_corpus(tmp_path, manifest_path)

    assert corpus.manifest == manifest
    assert corpus.receipts == receipts
    assert corpus.summary.verified_cells == 18
    assert corpus.verification.cross_bindings_verified is True
    assert corpus.verification.required_metrics_evaluable is False
    assert corpus.verification.safety_gate_passed is False
    assert corpus.verification.quality_gate_passed is True
    assert corpus.verification.passed is False
