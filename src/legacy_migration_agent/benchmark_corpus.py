"""Provider-free extraction and verification of the complete benchmark-v2 corpus."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.benchmark_execution import load_strict_benchmark_json
from legacy_migration_agent.benchmark_protocol import load_verified_benchmark_protocol
from legacy_migration_agent.benchmark_receipts import extract_evaluation_cell_receipt
from legacy_migration_agent.contracts import Identifier, StrictModel, validate_relative_path
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.measured_evaluation import (
    PLANNED_CELLS,
    EvaluationCellReceipt,
    MeasuredEvaluationVerification,
    MetricSummary,
    aggregate_measured_evaluation,
    verify_measured_evaluation,
)


class BenchmarkCorpusRunReference(StrictModel):
    """Routing-only reference; it cannot supply a case, arm, or result."""

    run_dir: str
    rubric_path: str
    run_id: Identifier
    thread_id: Identifier

    @field_validator("run_dir", "rubric_path")
    @classmethod
    def validate_artifact_path(cls, value: str) -> str:
        return validate_relative_path(value)


class BenchmarkCorpusManifest(StrictModel):
    """Exact 18-run inventory used by the official receipt extractor."""

    schema_version: Literal["2.0"] = "2.0"
    registry_id: Identifier
    execution_anchor_path: str
    runs: tuple[BenchmarkCorpusRunReference, ...] = Field(
        min_length=PLANNED_CELLS,
        max_length=PLANNED_CELLS,
    )

    @field_validator("execution_anchor_path")
    @classmethod
    def validate_anchor_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @model_validator(mode="after")
    def validate_unique_routing(self) -> BenchmarkCorpusManifest:
        for values, label in (
            (tuple(item.run_id for item in self.runs), "run IDs"),
            (tuple(item.thread_id for item in self.runs), "thread IDs"),
            (tuple(item.run_dir for item in self.runs), "run directories"),
            (tuple(item.rubric_path for item in self.runs), "rubric paths"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"benchmark corpus {label} must be unique")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedBenchmarkCorpus:
    manifest: BenchmarkCorpusManifest
    receipts: tuple[EvaluationCellReceipt, ...]
    summary: MetricSummary
    verification: MeasuredEvaluationVerification


def load_verified_benchmark_corpus(
    project_root: Path,
    manifest_path: Path,
) -> VerifiedBenchmarkCorpus:
    """Extract all 18 receipts, aggregate them, and recompute verification."""

    try:
        payload = load_strict_benchmark_json(manifest_path)
        manifest = BenchmarkCorpusManifest.model_validate_json(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )
    except PolicyViolation:
        raise
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("benchmark corpus manifest is malformed") from exc

    root = Path(project_root).resolve(strict=True)
    protocol = load_verified_benchmark_protocol(root)
    if manifest.registry_id != protocol.registry.registry_id:
        raise PolicyViolation("benchmark corpus selects another registry")
    anchor_path = root / manifest.execution_anchor_path
    receipts = tuple(
        extract_evaluation_cell_receipt(
            root,
            root / item.run_dir,
            root / item.rubric_path,
            anchor_path,
            run_id=item.run_id,
            thread_id=item.thread_id,
        )
        for item in manifest.runs
    )
    try:
        summary = aggregate_measured_evaluation(protocol.registry, receipts)
        verification = verify_measured_evaluation(
            protocol.registry,
            receipts,
            summary,
        )
    except (TypeError, ValueError) as exc:
        raise PolicyViolation("benchmark corpus does not form the declared matrix") from exc
    return VerifiedBenchmarkCorpus(
        manifest=manifest,
        receipts=receipts,
        summary=summary,
        verification=verification,
    )


__all__ = [
    "BenchmarkCorpusManifest",
    "BenchmarkCorpusRunReference",
    "VerifiedBenchmarkCorpus",
    "load_verified_benchmark_corpus",
]
