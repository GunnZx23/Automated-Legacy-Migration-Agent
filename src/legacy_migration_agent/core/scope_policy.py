"""Digest-bound, platform-specific scope policy for model-authored manifests.

The Architect may propose a migration plan, but it does not define the write or
validation boundary.  A caller supplies this immutable adapter before model
invocation.  Deterministic validation then proves that every proposed output,
check, and approval remains inside that predeclared boundary.
"""

from __future__ import annotations

from collections import Counter

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.contracts import (
    ApprovalAction,
    Identifier,
    MigrationManifest,
    MigrationRequest,
    Platform,
    Sha256Digest,
    StrictModel,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation


def _unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return values


def _is_path_or_descendant(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


class MigrationScopePolicy(StrictModel):
    """Caller-owned authority boundary for one class of migration manifest.

    ``required_source_input_paths`` is the exact immutable source set that the
    Architect must account for across its transformation plan. A source may be
    cited by more than one step, including when an existing source file is also
    an approved same-path update.

    Exact output paths and root/extension pairs are alternatives and may be
    combined.  A path allowed by either mechanism is still rejected when it is
    equal to, or below, a forbidden path.  This lets a broad fixture or source
    root explicitly exclude legacy inputs and reviewed golden-output trees.
    """

    policy_id: Identifier
    platform: Platform
    required_source_input_paths: tuple[str, ...] = Field(min_length=1)
    approved_output_roots: tuple[str, ...] = ()
    approved_output_extensions: tuple[str, ...] = ()
    approved_output_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    allowed_validation_command_ids: tuple[Identifier, ...] = Field(min_length=1)
    required_validation_command_ids: tuple[Identifier, ...] = Field(min_length=1)
    required_implementation_contract: tuple[str, ...] = Field(default=(), max_length=64)
    max_changed_files: int = Field(ge=1, le=256)
    required_approval_actions: tuple[ApprovalAction, ...] = ()

    @field_validator(
        "required_source_input_paths",
        "approved_output_roots",
        "approved_output_paths",
        "forbidden_paths",
    )
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_relative_path(value) for value in values)
        return _unique(normalized, "scope policy paths")

    @field_validator("approved_output_extensions")
    @classmethod
    def validate_extensions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("approved output extensions must be unique")
        for value in values:
            if (
                not value.startswith(".")
                or value in {".", ".."}
                or "/" in value
                or "\\" in value
                or value != value.strip()
            ):
                raise ValueError(
                    "approved output extensions must be dot-prefixed filename suffixes"
                )
        return values

    @field_validator("allowed_validation_command_ids", "required_validation_command_ids")
    @classmethod
    def validate_command_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(values, "scope policy validation command IDs")

    @field_validator("required_approval_actions")
    @classmethod
    def validate_approval_actions(
        cls,
        values: tuple[ApprovalAction, ...],
    ) -> tuple[ApprovalAction, ...]:
        if len(values) != len(set(values)):
            raise ValueError("scope policy approval actions must be unique")
        return values

    @field_validator("required_implementation_contract")
    @classmethod
    def validate_implementation_contract(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError(
                "scope policy implementation-contract entries must contain 1 to 2000 characters"
            )
        return _unique(values, "scope policy implementation-contract entries")

    @model_validator(mode="after")
    def validate_policy_shape(self) -> MigrationScopePolicy:
        if not self.approved_output_paths and not self.approved_output_roots:
            raise ValueError("scope policy requires exact output paths or output roots")
        if bool(self.approved_output_roots) != bool(self.approved_output_extensions):
            raise ValueError("approved output roots and extensions must be declared together")
        unknown_required = sorted(
            set(self.required_validation_command_ids) - set(self.allowed_validation_command_ids)
        )
        if unknown_required:
            raise ValueError(
                "required validation commands must be allowed: " + ", ".join(unknown_required)
            )
        contradictory = sorted(
            path
            for path in self.approved_output_paths
            if any(_is_path_or_descendant(path, forbidden) for forbidden in self.forbidden_paths)
        )
        if contradictory:
            raise ValueError(
                "exact approved output paths cannot also be forbidden: " + ", ".join(contradictory)
            )
        return self

    def allows_output_path(self, path: str) -> bool:
        """Return whether one already-normalized path is inside this policy."""

        if any(_is_path_or_descendant(path, forbidden) for forbidden in self.forbidden_paths):
            return False
        if path in self.approved_output_paths:
            return True
        return any(
            path.startswith(root + "/")
            and any(path.endswith(extension) for extension in self.approved_output_extensions)
            for root in self.approved_output_roots
        )


class PlatformAdapter(StrictModel):
    """Typed platform binding for one immutable migration scope policy."""

    adapter_id: Identifier
    platform: Platform
    scope_policy: MigrationScopePolicy
    scope_policy_digest: Sha256Digest

    @model_validator(mode="after")
    def validate_policy_binding(self) -> PlatformAdapter:
        if self.scope_policy.platform is not self.platform:
            raise ValueError("platform adapter and scope policy platforms do not match")
        if self.scope_policy_digest != artifact_digest(self.scope_policy):
            raise ValueError("scope policy digest does not match its content")
        return self

    @classmethod
    def bind(
        cls,
        *,
        adapter_id: str,
        policy: MigrationScopePolicy,
    ) -> PlatformAdapter:
        """Bind an adapter to the canonical bytes of an explicit policy."""

        return cls(
            adapter_id=adapter_id,
            platform=policy.platform,
            scope_policy=policy,
            scope_policy_digest=artifact_digest(policy),
        )

    def validate_request(self, request: MigrationRequest) -> None:
        if request.platform is not self.platform:
            raise PolicyViolation("platform adapter does not match the migration request")

    def validate_manifest(
        self,
        manifest: MigrationManifest,
        request: MigrationRequest,
    ) -> None:
        """Reject a model proposal outside the caller-owned policy boundary."""

        self.validate_request(request)
        policy = self.scope_policy
        if manifest.platform is not self.platform:
            raise PolicyViolation("manifest platform does not match the platform adapter")

        if len(manifest.approved_paths) > policy.max_changed_files:
            raise PolicyViolation(
                "manifest exceeds scope policy max_changed_files "
                f"({len(manifest.approved_paths)} > {policy.max_changed_files})"
            )
        disallowed_paths = sorted(
            path for path in manifest.approved_paths if not policy.allows_output_path(path)
        )
        if disallowed_paths:
            raise PolicyViolation(
                "manifest contains output paths outside the scope policy: "
                + ", ".join(disallowed_paths)
            )
        if (
            policy.approved_output_paths
            and not policy.approved_output_roots
            and manifest.approved_paths != policy.approved_output_paths
        ):
            raise PolicyViolation(
                "manifest approved paths do not match the caller-owned output boundary"
            )

        validate_manifest_transformation_scope(
            manifest,
            required_source_input_paths=policy.required_source_input_paths,
            approved_output_paths=manifest.approved_paths,
        )

        planned_command_ids = {check.command_id for check in manifest.validation_plan}
        unknown_commands = sorted(planned_command_ids - set(policy.allowed_validation_command_ids))
        if unknown_commands:
            raise PolicyViolation(
                "manifest contains validation commands outside the scope policy: "
                + ", ".join(unknown_commands)
            )
        missing_required = sorted(set(policy.required_validation_command_ids) - planned_command_ids)
        if missing_required:
            raise PolicyViolation(
                "manifest omits required validation commands: " + ", ".join(missing_required)
            )
        optional_required = sorted(
            command_id
            for command_id in policy.required_validation_command_ids
            if not any(
                check.command_id == command_id and check.required
                for check in manifest.validation_plan
            )
        )
        if optional_required:
            raise PolicyViolation(
                "scope-required validation commands must be marked required: "
                + ", ".join(optional_required)
            )
        if (
            set(policy.allowed_validation_command_ids)
            == set(policy.required_validation_command_ids)
            and tuple(check.command_id for check in manifest.validation_plan)
            != policy.required_validation_command_ids
        ):
            raise PolicyViolation(
                "manifest validation command order does not match the caller-owned contract"
            )

        if manifest.implementation_contract != policy.required_implementation_contract:
            raise PolicyViolation(
                "manifest implementation contract does not match the caller-owned contract"
            )

        missing_approvals = sorted(
            action.value
            for action in set(policy.required_approval_actions) - set(manifest.required_approvals)
        )
        if missing_approvals:
            raise PolicyViolation(
                "manifest omits scope-required approval actions: " + ", ".join(missing_approvals)
            )


def validate_manifest_transformation_scope(
    manifest: MigrationManifest,
    *,
    required_source_input_paths: tuple[str, ...],
    approved_output_paths: tuple[str, ...],
) -> None:
    """Enforce the aggregate caller-owned transformation boundary.

    Plans may split work into multiple reviewable steps, but they may not invent
    inputs, omit source evidence, reuse an output, or chain one generated output
    into another step. Source reuse across steps is intentionally allowed.
    """

    transformations = manifest.transformations
    if not transformations:
        raise PolicyViolation("manifest must contain at least one transformation")

    step_ids = tuple(step.step_id for step in transformations)
    duplicate_step_ids = sorted(
        step_id for step_id, count in Counter(step_ids).items() if count > 1
    )
    if duplicate_step_ids:
        raise PolicyViolation(
            "manifest transformation step IDs must be unique: " + ", ".join(duplicate_step_ids)
        )

    expected_sources = set(required_source_input_paths)
    expected_outputs = set(approved_output_paths)
    if set(manifest.approved_paths) != expected_outputs:
        raise PolicyViolation(
            "manifest approved paths do not match the caller-owned output boundary"
        )

    input_union: set[str] = set()
    output_owners: Counter[str] = Counter()
    for step in transformations:
        if not step.input_paths:
            raise PolicyViolation(
                f"manifest transformation {step.step_id} must declare at least one input"
            )
        if not step.output_paths:
            raise PolicyViolation(
                f"manifest transformation {step.step_id} must declare at least one output"
            )

        unknown_inputs = sorted(set(step.input_paths) - expected_sources)
        if unknown_inputs:
            raise PolicyViolation(
                f"manifest transformation {step.step_id} contains inputs outside the "
                "caller-owned source boundary: " + ", ".join(unknown_inputs)
            )
        unknown_outputs = sorted(set(step.output_paths) - expected_outputs)
        if unknown_outputs:
            raise PolicyViolation(
                f"manifest transformation {step.step_id} contains outputs outside the "
                "approved manifest boundary: " + ", ".join(unknown_outputs)
            )

        input_union.update(step.input_paths)
        output_owners.update(step.output_paths)

    missing_sources = sorted(expected_sources - input_union)
    if missing_sources:
        raise PolicyViolation(
            "manifest transformations omit required source inputs: " + ", ".join(missing_sources)
        )
    missing_outputs = sorted(expected_outputs - set(output_owners))
    if missing_outputs:
        raise PolicyViolation(
            "manifest transformations do not own every approved output: "
            + ", ".join(missing_outputs)
        )
    duplicate_outputs = sorted(path for path, count in output_owners.items() if count != 1)
    if duplicate_outputs:
        raise PolicyViolation(
            "each approved output must be owned by exactly one transformation: "
            + ", ".join(duplicate_outputs)
        )


__all__ = [
    "MigrationScopePolicy",
    "PlatformAdapter",
    "validate_manifest_transformation_scope",
]
