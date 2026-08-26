from __future__ import annotations

from collections.abc import Callable

import pytest

from legacy_migration_agent.contracts import (
    EnvironmentKind,
    MigrationManifest,
    MigrationRequest,
    MigrationTarget,
    Platform,
    TransformationStep,
    ValidationCommand,
)
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.scope_policy import MigrationScopePolicy, PlatformAdapter

SOURCE_A = "legacy/source-a.txt"
SOURCE_B = "legacy/source-b.txt"
OUTPUT_A = "generated/target-a.txt"
OUTPUT_B = "generated/target-b.txt"
IMPLEMENTATION_CONTRACT = (
    "Preserve both legacy source files while creating only the approved additive outputs.",
    "Keep validation local and make no deployment or runtime-success claim.",
)
VALIDATION_COMMANDS = ("bounded-contract", "bounded-behavior")


def _request() -> MigrationRequest:
    return MigrationRequest(
        request_id="request-multi-step-scope",
        platform=Platform.SALESFORCE,
        repository="source",
        base_revision="revision-1234567",
        target=MigrationTarget(
            entry_path=SOURCE_A,
            target_runtime="bounded-test-runtime",
            source_version="source-v1",
            target_version="target-v1",
            description="Exercise the aggregate transformation scope policy.",
        ),
        allowed_environment=EnvironmentKind.LOCAL,
    )


def _manifest() -> MigrationManifest:
    request = _request()
    return MigrationManifest(
        manifest_id="manifest-multi-step-scope",
        request_id=request.request_id,
        platform=request.platform,
        base_revision=request.base_revision,
        approved_paths=(OUTPUT_A, OUTPUT_B),
        dependencies=(),
        transformations=(
            TransformationStep(
                step_id="create-target-a",
                description="Create the first bounded target.",
                input_paths=(SOURCE_A, SOURCE_B),
                output_paths=(OUTPUT_A,),
            ),
            TransformationStep(
                step_id="create-target-b",
                description="Create the second bounded target while reusing source evidence.",
                input_paths=(SOURCE_B,),
                output_paths=(OUTPUT_B,),
            ),
        ),
        validation_plan=tuple(
            ValidationCommand(
                check_id=f"check-{command_id}",
                command_id=command_id,
                purpose="Run one caller-owned local check.",
            )
            for command_id in VALIDATION_COMMANDS
        ),
        implementation_contract=IMPLEMENTATION_CONTRACT,
    )


def _adapter() -> PlatformAdapter:
    return PlatformAdapter.bind(
        adapter_id="multi-step-scope-adapter",
        policy=MigrationScopePolicy(
            policy_id="multi-step-scope-policy",
            platform=Platform.SALESFORCE,
            required_source_input_paths=(SOURCE_A, SOURCE_B),
            approved_output_paths=(OUTPUT_A, OUTPUT_B),
            allowed_validation_command_ids=VALIDATION_COMMANDS,
            required_validation_command_ids=VALIDATION_COMMANDS,
            required_implementation_contract=IMPLEMENTATION_CONTRACT,
            max_changed_files=2,
        ),
    )


def test_scope_policy_accepts_multiple_steps_and_source_reuse() -> None:
    _adapter().validate_manifest(_manifest(), _request())


@pytest.mark.parametrize(
    "implementation_contract",
    (
        (),
        (
            "Preserve both legacy source files while creating only the approved additive "
            "outputs, with an unreviewed mutation.",
            IMPLEMENTATION_CONTRACT[1],
        ),
        tuple(reversed(IMPLEMENTATION_CONTRACT)),
    ),
    ids=("omitted", "mutated", "reordered"),
)
def test_scope_policy_rejects_nonexact_implementation_contract(
    implementation_contract: tuple[str, ...],
) -> None:
    manifest = _manifest().model_copy(update={"implementation_contract": implementation_contract})

    with pytest.raises(PolicyViolation, match="caller-owned contract"):
        _adapter().validate_manifest(manifest, _request())


def test_scope_policy_rejects_exact_output_path_reordering() -> None:
    manifest = _manifest().model_copy(
        update={"approved_paths": tuple(reversed(_manifest().approved_paths))}
    )

    with pytest.raises(PolicyViolation, match="caller-owned output boundary"):
        _adapter().validate_manifest(manifest, _request())


def test_scope_policy_rejects_required_validation_command_reordering() -> None:
    manifest = _manifest().model_copy(
        update={"validation_plan": tuple(reversed(_manifest().validation_plan))}
    )

    with pytest.raises(PolicyViolation, match="validation command order"):
        _adapter().validate_manifest(manifest, _request())


def _without_transformations(manifest: MigrationManifest) -> MigrationManifest:
    return manifest.model_copy(update={"transformations": ()})


def _with_duplicate_step_id(manifest: MigrationManifest) -> MigrationManifest:
    second = manifest.transformations[1].model_copy(update={"step_id": "create-target-a"})
    return manifest.model_copy(update={"transformations": (manifest.transformations[0], second)})


def _with_empty_inputs(manifest: MigrationManifest) -> MigrationManifest:
    first = manifest.transformations[0].model_copy(update={"input_paths": ()})
    return manifest.model_copy(update={"transformations": (first, manifest.transformations[1])})


def _with_empty_outputs(manifest: MigrationManifest) -> MigrationManifest:
    first = manifest.transformations[0].model_copy(update={"output_paths": ()})
    return manifest.model_copy(update={"transformations": (first, manifest.transformations[1])})


def _with_missing_source(manifest: MigrationManifest) -> MigrationManifest:
    first = manifest.transformations[0].model_copy(update={"input_paths": (SOURCE_A,)})
    second = manifest.transformations[1].model_copy(update={"input_paths": (SOURCE_A,)})
    return manifest.model_copy(update={"transformations": (first, second)})


def _with_generated_output_chaining(manifest: MigrationManifest) -> MigrationManifest:
    second = manifest.transformations[1].model_copy(update={"input_paths": (SOURCE_B, OUTPUT_A)})
    return manifest.model_copy(update={"transformations": (manifest.transformations[0], second)})


def _with_missing_output(manifest: MigrationManifest) -> MigrationManifest:
    return manifest.model_copy(update={"transformations": (manifest.transformations[0],)})


def _with_duplicate_output_owner(manifest: MigrationManifest) -> MigrationManifest:
    second = manifest.transformations[1].model_copy(update={"output_paths": (OUTPUT_A, OUTPUT_B)})
    return manifest.model_copy(update={"transformations": (manifest.transformations[0], second)})


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (_without_transformations, "at least one transformation"),
        (_with_duplicate_step_id, "step IDs must be unique"),
        (_with_empty_inputs, "must declare at least one input"),
        (_with_empty_outputs, "must declare at least one output"),
        (_with_missing_source, "omit required source inputs"),
        (_with_generated_output_chaining, "outside the caller-owned source boundary"),
        (_with_missing_output, "do not own every approved output"),
        (_with_duplicate_output_owner, "owned by exactly one transformation"),
    ),
)
def test_scope_policy_rejects_unsafe_aggregate_transformation_plans(
    mutate: Callable[[MigrationManifest], MigrationManifest],
    message: str,
) -> None:
    with pytest.raises(PolicyViolation, match=message):
        _adapter().validate_manifest(mutate(_manifest()), _request())
