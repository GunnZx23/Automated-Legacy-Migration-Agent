# Benchmark v2 independent label review packet

This packet asks an instructor, classmate, or colleague with Salesforce or
MuleSoft migration experience to independently review the benchmark labels
before any benchmark-v2 model call runs. It is not an approval of generated
code, a deployment, or a claim that the benchmark has been executed.

## Frozen review subject

- Registry: `legacy-migration-benchmark-v2`
- Review method: `independent_human_review`
- Impact definition: `migration-dependency-impact-v1`
- Review-subject digest:
  `sha256:fa76b2b5b9f8a9f4dea0637e7c3a1f6d9cddce6fa08ff7fbc4695631ffd29183`
- Labels: 65 total; 51 high impact and 14 lower impact
- Planned evaluation after acceptance: 3 cases × 2 Wiki configurations × 3
  repetitions = 18 live cells

If any substantive label changes, this digest must be recomputed and the
corrected subject must be reviewed again. Do not sign evidence for an obsolete
digest.

## Cases to review

| Case | Complexity | Labels | Predeclared expected disposition |
| --- | --- | ---: | --- |
| MuleSoft Customer Status API | Simple | 10 | `environment_unavailable` because no attested Mule runtime or MUnit authority is present |
| Salesforce Account/Contact Explorer | Medium | 22 | `ready_for_human_review` |
| Salesforce Case Management Console | Complex, safety-sensitive | 33 | `decision_required` |

These are bounded synthetic cases. Acceptance supports claims about these
fixtures and this protocol only; it does not establish production, repository-
scale, provider-wide, Salesforce-wide, or MuleSoft-wide performance.

## Review inventory

Review these protocol artifacts:

- `evaluation/benchmark-v2/registry.json`
- `evaluation/benchmark-v2/dependency-labels.json`
- `evaluation/benchmark-v2/source-snapshots.json`
- `evaluation/benchmark-v2/risk-seed.json`
- `evaluation/mulesoft-customer-status-api-source-edges.json`
- `evaluation/salesforce-account-contact-explorer-source-edges.json`
- `evaluation/benchmark-v2/salesforce-case-management-console-source-edges.json`

Compare them with the three fixture contracts and immutable inputs:

- `fixtures/mulesoft/customer-status-api/fixture.yaml` and its `input/` tree
- `fixtures/salesforce/account-contact-explorer/fixture.yaml` and its `input/`
  tree
- `fixtures/salesforce/case-management-console/fixture.yaml` and its `input/`
  tree

## Required checklist

The reviewer should independently confirm or correct all of the following:

1. Each case's complexity and expected terminal disposition are reasonable.
2. Every source edge has exactly one benchmark dependency label and every
   benchmark label maps to real source evidence.
3. Dependency identity and high-impact classification follow
   `migration-dependency-impact-v1`, rather than optimizing for a desired score.
4. The Case risk seed covers exactly the four predeclared intervention reasons:
   `destructive_legacy_deletion`, `sharing_boundary_weakening`,
   `object_field_security_weakening`, and `permission_scope_expansion`.
5. No label grants destructive migration, source mutation, Git, deployment, or
   publication authority.
6. The Mule case truthfully preserves the missing-runtime boundary instead of
   treating static generation as MUnit or runtime proof.

## Reviewer response

Return the following facts to the project owner. Do not use a shared project
identity as the reviewer identity.

- Reviewer ID or name:
- Domain or relevant expertise:
- Reviewed at, including timezone:
- Decision: `accepted` or `corrections_required`
- Corrections, if any:
- Reviewer-authored attestation:

After genuine acceptance, the project owner will encode these facts in
`evaluation/benchmark-v2/label-review-evidence.json`, bind its digest to the
protocol, verify the reviewed gate, and only then create the execution anchor.
Each of the 18 terminal runs will still require a separately bound independent
human rubric before aggregate benchmark results can be claimed.

## Provider-free verification

Before review, the following command must show `labels_reviewed: false`, no
execution anchor, 18 `not_started` cells, and `next_action: review_labels`:

```bash
uv run --frozen legacy-migration-agent evaluation-benchmark-v2-status \
  --project-root .
```

The exact review-subject digest can be reproduced without invoking a model:

```bash
uv run --frozen python -c "from pathlib import Path; from legacy_migration_agent.benchmark_protocol import load_verified_benchmark_protocol; print(load_verified_benchmark_protocol(Path('.')).label_review_subject_digest)"
```
