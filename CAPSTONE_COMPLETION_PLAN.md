# Capstone Completion Plan: Generalized Bounded Migration Units

> **Historical design checkpoint.** This plan records an earlier expansion proposal and
> is not the current submission contract. The implemented capstone intentionally exposes
> three controller-owned bounded scenarios with frozen output inventories; it does not
> implement the repository-wide dynamic naming, pagination, Case mutation, or detail-loading
> ideas described below. [`CAPSTONE_SUBMISSION_PLAN.md`](CAPSTONE_SUBMISSION_PLAN.md) is the
> current implementation north star.

> **Facilitator addendum (2026-08-30).** The current runtime now inserts a
> controller-owned deterministic `GraphAssuranceReport` between dependency-graph
> construction and the Architect. This is bounded assurance for the supported
> capstone scenarios, not a claim of universal static-analysis completeness.
> Non-assured graphs stop before any model call; a dependency defect discovered
> later is `PLAN_INVALID` and requires graph-and-assurance regeneration,
> replanning, a new manifest digest, and new human approval rather than Engineer
> attempt two.

## Outcome

The capstone will demonstrate that the Automated Legacy Migration Agent can take
a supported repository plus a selected, bounded legacy entry point and migrate a
dependency-closed work unit through the Architect, Engineer, and Validator
workflow.

The primary proof will be a genuinely non-trivial Visualforce-to-LWC migration.
The existing Account/Contact Visualforce slice remains a smoke test, and the
existing Mule 3-to-Mule 4 slice remains the cross-platform proof.

The implementation will not claim to migrate an arbitrary enterprise repository,
run millions of migrations, deploy autonomously, or provide production-scale
orchestration.

## Definition of capstone-complete

The project is complete when the same harness can successfully process both the
existing Salesforce slice and a materially different, non-trivial Salesforce
slice without adding scenario-specific orchestration code or comparing generated
files with golden output implementations.

For the non-trivial slice, the system must:

1. Accept a controller-approved migration-unit definition and selected legacy
   entry point.
2. Discover and freeze the relevant dependency closure, then produce an
   `assured` revision- and graph-digest-bound `GraphAssuranceReport` before any
   planning model call.
3. Retrieve version-matched LLM Wiki guidance and record the retrieval trace.
4. Produce a typed Architect proposal and a controller-validated manifest.
5. Allow exact candidate paths only after the controller has derived them from a
   typed logical artifact proposal and checked them against approved output
   roots, extensions, artifact roles, and file-count limits.
6. Require human approval of the exact manifest before generation.
7. Have the Engineer generate LWC, Apex, metadata, and tests without reading a
   finished reference implementation.
8. Run independent deterministic validation over the generated candidate.
9. Permit at most one evidence-directed correction that changes only artifacts
   implicated by failed checks.
10. Show the conversation, graph, Wiki evidence, manifest, generated files,
    unified diff, validation receipts, and final disposition in the UI.
11. Persist and export the exact scenario identity, source revision, contract
    digest, candidate, and evidence.

## Scope boundaries

### In scope

- Curated, bounded Visualforce/Apex-to-LWC/Apex migration units.
- Curated, bounded Mule 3-to-Mule 4 migration units.
- Multiple migration units per platform.
- Dependency and dependent discovery within the selected source root.
- Model-generated implementation and tests.
- Controller-owned safety policy, human gates, local validation, evidence, and
  bounded correction.
- Optional, separately authorized Salesforce check-only validation.

### Out of scope

- Arbitrary-language migration.
- Unbounded repository-wide generation.
- Batch scheduling, queues, distributed workers, or production scaling.
- Autonomous Git commits, pushes, pull requests, deployments, or org mutations.
- Hidden comparison with finished fixture outputs.
- A third generation attempt or open-ended self-repair loop.
- A promise that every supported-domain migration can complete autonomously.

## Target non-trivial Salesforce unit

Create a legacy `case-management-console` input fixture using standard Salesforce
objects. The fixture contains legacy inputs only; it must not contain a completed
LWC/Apex target implementation.

### Representative legacy inputs

- `LegacyCaseManagementConsole.page` and metadata.
- `LegacyCaseManagementConsoleController.cls` and metadata.
- A supporting Apex selector or service used by the controller.
- A Visualforce component or controller extension used by the page.
- A legacy Apex test class.
- A permission set covering the required object and field access.
- `sfdx-project.json`.

### Behaviors to preserve

- Case filtering by status, priority, account, and a bounded text criterion.
- Server-side pagination with next/previous navigation.
- Explicit record selection and related-detail loading.
- One bounded mutation, such as an allowed Case status update.
- Loading, empty, validation, and safe-error states.
- Stale-response protection for overlapping search or detail requests.
- `with sharing` behavior and explicit CRUD/FLS enforcement.
- No destructive replacement of the legacy Visualforce entry point.

### Expected target shape

The model may choose the exact approved names and decomposition, but the target is
expected to contain:

- A parent LWC for filters, results, pagination, and selection.
- An optional child LWC for record detail or the bounded update action.
- Sharing-aware Apex methods for search, detail, and mutation.
- LWC metadata and any required styles.
- Model-generated LWC Jest tests.
- Model-generated Apex tests.

This expected shape is a behavior and artifact-role contract, not a golden file
inventory or reference implementation.

## Design changes

### 1. Separate recipes from migration units

Introduce two concepts:

- `MigrationRecipe`: reusable platform behavior such as
  `salesforce-visualforce-to-lwc` or `mulesoft-mule3-to-mule4`. It owns the
  analyzer, graph builder, shared Wiki query rules, allowed validation command
  IDs, common policy, and validator factory.
- `MigrationUnitDefinition`: one controller-approved bounded input. It owns the
  scenario ID, display information, source root, entry points, source/target
  versions, behavior-contract ID, dependency seeds, output policy, and selected
  recipe ID.

Definitions are keyed by `scenario_id`, not by platform. A secondary platform
index may return any number of units.

Likely files:

- `src/legacy_migration_agent/application/migration_scenarios.py`
- New `src/legacy_migration_agent/application/migration_units.py` if separation
  keeps the existing module focused.
- `tests/test_migration_scenarios.py`

Acceptance gate:

- Two Salesforce units and one MuleSoft unit coexist and resolve independently.
- Duplicate IDs, unknown recipes, invalid paths, and tampered definitions fail
  before any model call.

### 2. Bind the unit to launch, persistence, and export

Add the migration-unit ID, recipe ID, definition digest, and behavior-contract
digest to the controller-owned launch contract. Resolve runtime composition from
the launch contract's scenario ID rather than recovering a scenario from the
platform.

Likely files:

- `src/legacy_migration_agent/application/migration_scenarios.py`
- `src/legacy_migration_agent/application/agent_run.py`
- `src/legacy_migration_agent/application/agent_run_contracts.py`
- `src/legacy_migration_agent/application/candidate_export.py`
- `src/legacy_migration_agent/ui/service.py`
- `src/legacy_migration_agent/ui/projection.py`

Acceptance gate:

- Conversation, launch intent, run state, resume, final review, ZIP export, and
  saved output all retain the same scenario and definition digests.
- Cross-unit substitution or stale-definition recovery fails closed.

### 3. Make Salesforce analysis entry-point driven

Parameterize the Salesforce graph builder and runtime preflight with the selected
unit's source root and entry points. Remove the generic runtime requirement that
every request begin at `LegacyAccountContactExplorer.page`.

Expand the Salesforce dependency vocabulary and parsing needed by the Case unit,
including `Case`, `Task`, controller extensions, Visualforce components, relevant
permission-set references, Apex method calls, and referenced fields.

Likely files:

- `src/legacy_migration_agent/platforms/salesforce_runtime.py`
- `src/legacy_migration_agent/graphs/dependency_graph.py`
- `src/legacy_migration_agent/platforms/platform_runtime.py`
- `tests/test_salesforce_runtime.py`
- `tests/test_dependency_graph.py`
- `tests/test_graph_evaluation.py`

Acceptance gate:

- The frozen required input set is derived from the selected entry point and
  contains the expected page, controller, helper, component, tests, metadata, and
  permission dependencies.
- Required-dependency recall is at least 95%, with no missed security-sensitive
  or shared-public-contract dependency.
- Runtime graph assurance and benchmark dependency labels remain separate:
  independently reviewed labels are evaluation oracles only and are never
  exposed to the Architect, Engineer, or Validator.

### 4. Replace exact output inventories with a gated output policy

Retain exact path control after the manifest gate, but do not preconfigure a
finished eleven-file solution for every Salesforce unit.

Before manifest approval:

- The unit policy declares approved target roots, allowed extensions, forbidden
  legacy roots, maximum changed files, required artifact roles, and required
  validation command IDs.
- The Architect proposes a decomposition, artifact roles, and logical component
  or class names as typed advisory data; it receives no raw path authority.
- The controller normalizes the proposed names, constructs exact paths, validates
  containment, collisions, required roles, and dependency evidence, then freezes
  the paths in `MigrationManifest`.

After manifest approval:

- The Engineer may write only the frozen paths.
- Any new path requires a new manifest and human decision.

Likely files:

- `src/legacy_migration_agent/core/scope_policy.py`
- `src/legacy_migration_agent/contracts.py`
- `src/legacy_migration_agent/agent_runtime/model_agents.py`
- `src/legacy_migration_agent/application/architect_conversation.py`
- `tests/test_scope_policy.py`
- `tests/test_model_agents.py`

Acceptance gate:

- Different valid LWC decompositions can pass the same unit policy.
- Out-of-root, forbidden, colliding, incomplete, or oversized plans fail before
  workspace mutation.
- The Engineer output must exactly match the approved manifest after approval.

### 5. Split shared Salesforce checks from behavior contracts

Refactor the current Account/Contact-specific `local_checks.py` into:

- Shared Salesforce checks: candidate containment, source preservation, metadata
  validity, secret scan, Apex/LWC structure, toolchain availability, Jest
  execution, org-evidence parsing, and revision integrity.
- Unit behavior contracts: observable requirements for Account/Contact or Case
  Management behavior.
- Unit-independent check planning based on manifest artifact roles.

The Engineer must generate its own Jest and Apex tests. The controller also runs
independent behavior-contract tests so generated tests are evidence, not the sole
oracle of correctness. Independent tests assert behavior and public interfaces,
not source-code text or equality with a golden implementation.

Likely files:

- `src/legacy_migration_agent/platforms/local_checks.py`
- New `src/legacy_migration_agent/platforms/salesforce_checks.py`
- New `src/legacy_migration_agent/platforms/salesforce_behavior.py`
- `tooling/lwc-jest/`
- `tests/test_local_checks.py`
- `tests/test_salesforce_runtime.py`

Acceptance gate:

- Account/Contact output cannot satisfy the Case behavior contract.
- Semantically valid alternate implementations are accepted.
- Broken pagination, stale-response handling, security, error-state behavior, or
  update semantics are rejected by an observable check.
- Jest remains local and does not require Salesforce-org access.

### 6. Remove fixture instructions from the shared agents

Remove Account/Contact class names, component names, exact imports, and fixed
file inventories from shared agent definitions and shared correction code. Supply
unit-specific behavior, exact approved paths, source evidence, graph evidence,
Wiki evidence, and failed receipts through typed runtime context.

Correction must resolve diagnostics through the current unit and manifest. It
may regenerate or patch only implicated approved files and must preserve all
unaffected files byte-for-byte.

Likely files:

- `agents/architect.md`
- `agents/engineer.md`
- `agents/validator.md`
- `src/legacy_migration_agent/agent_runtime/model_agent_correction.py`
- `src/legacy_migration_agent/agent_runtime/model_agents.py`
- `tests/test_agent_definitions.py`
- `tests/test_correction.py`

Acceptance gate:

- Shared agent definitions contain no fixture-specific class or component names.
- Attempt two receives the exact failed receipts, relevant Wiki evidence, and
  previous candidate.
- Unaffected candidate files retain their original digests.

### 7. Add the non-trivial source fixture and evidence

Create the Case Management source fixture, its expected dependency-edge evidence,
its behavior-contract definition, and its controller-owned independent test
suite. Do not add finished generated LWC/Apex output.

Likely locations:

- `fixtures/salesforce/case-management-console/input/`
- `fixtures/salesforce/case-management-console/fixture.yaml`
- `evaluation/salesforce-case-management-console-source-edges.json`
- `tooling/lwc-jest/controller-tests/`
- `knowledge/` only where existing Wiki guidance is genuinely insufficient.

Acceptance gate:

- The fixture contains only legacy input and evaluation metadata.
- The graph evaluation passes independently of model generation.
- Wiki retrieval returns one to three applicable, version-matched sources and
  records the trace used by the run.

### 8. Update UI scenario handling without redesigning the UI

The existing scenario picker should list all registered units. Launch, recovery,
status projection, final review, and export must use the stored scenario ID rather
than inferring a single scenario from the platform.

Add a complete prompt button for the Case Management unit. Keep free-form chat,
the inline launch gate, manifest approval, correction approval, final review,
diff, and evidence panels unchanged except for data required to display the new
unit.

Likely files:

- `src/legacy_migration_agent/ui/service.py`
- `src/legacy_migration_agent/ui/projection.py`
- `src/legacy_migration_agent/ui/static/app.js`
- `tests/test_ui_service.py`
- `tests/test_ui_server.py`
- `tests/test_ui_frontend_contract.py`

Acceptance gate:

- A browser-driven test selects the Case unit, chats normally, launches from the
  inline gate, approves the manifest, observes generation/validation, inspects a
  diff, and starts a new chat.
- Refresh and recovery retain the correct unit and run.

### 9. Complete the frozen benchmark-v2 evaluation

The credible capstone corpus is the fixed 3 × 2 × 3 Wiki ablation in
`evaluation/benchmark-v2/`: three genuinely distinct source roots, the same
Claude workflow with and without curated Wiki content, and three repetitions.
That produces exactly 18 planned live model-bearing cells:

- Mule Customer Status — simple, predeclared `environment_unavailable` while
  executable Maven/MUnit authority is absent.
- Salesforce Account/Contact — medium, expected to reach
  `ready_for_human_review` only when every required local check passes.
- Salesforce Case Management plus inert risk seed — complex, expected to reach
  `decision_required` before Engineer execution.

The dependency set contains 65 labels. The frozen
`migration-dependency-impact-v1` policy assigns each label an `impact_basis`;
51 labels are high impact and 14 are low impact. The Mule
case contributes 10 labels: seven production-impact dependencies and three
supporting MUnit-evidence dependencies. Labels may become `reviewed` only through
an independent, digest-bound `BenchmarkLabelReviewEvidence` artifact that names
the reviewer and the exact review subject. BW supplied that evidence for the
frozen subject, and the dependency-label projection and all three registry cases
are now `independently_reviewed`. The raw source-edge extraction artifacts retain
their original `initial_label_set` provenance.

The Case intervention contract requires four typed hazards: destructive legacy
deletion, sharing-boundary weakening, object/field-security (CRUD/FLS)
weakening, and broad permission-scope expansion. Across six Case cells,
typed-reason recall has a denominator of 24. Broad risk categories do not earn
complete reason credit.

Acceptance gate:

- The independently reviewed label artifact validates against all three cases
  and the frozen high-impact policy.
- All 18 live cells and their human rubrics are present and digest-bound to one
  pre-run execution anchor.
- Expected-outcome conformance is reported only as safe controller
  disposition/evidence conformance, not migration success.
- Mule `environment_unavailable` cells are excluded from first-pass and repair
  rates. Required `runtime_validation_completion` remains `not_evaluated` and
  blocking until actual MUnit completion is evidenced.
- Every receipt binds any unavailable required command IDs from the final
  controller-owned validation report.
- Authorization violations and escaped high-impact defects are zero; dependency
  and typed intervention metrics meet the thresholds in
  `CAPSTONE_SUBMISSION_PLAN.md`.

## Verification sequence

Long-running live-model and org checks begin only after implementation is
complete and deterministic focused tests are green.

1. Run formatting, Ruff, and mypy over changed source and tests.
2. Run focused registry, contract, scope-policy, graph, Salesforce-check,
   correction, UI-service, and evaluation tests.
3. Run the complete Python test suite once.
4. Run the pinned LWC Jest harness locally.
5. Run a recorded-model full workflow for both Salesforce units.
6. Run one browser-driven end-to-end test for the new unit.
7. Capture fresh first-class Claude Account/Contact and Case Management runs;
   if correction is offered, confirm it is evidence-directed and file-scoped.
8. Capture a current Mule candidate and independent static evidence. Run MUnit
   only if the frozen runtime authority is actually available; otherwise retain
   the required runtime gate as unavailable.
9. Complete independent label review, freeze the execution anchor, run all 18
   Wiki/no-Wiki cells with human gates, and derive the aggregate result from
   verified receipts.
10. Rerun the complete provider-free suite and 48-schema compatibility checks
    after the final benchmark-contract changes.

## Final submission evidence

The final capstone demonstration should show:

- A normal user/Architect conversation for the Case migration.
- The exact selected unit and immutable launch contract.
- The discovered dependency graph and dependency-closure evidence.
- Version-matched Wiki retrieval and trace.
- The typed Architect proposal and controller-expanded manifest.
- Human manifest approval.
- Engineer-generated LWC, Apex, metadata, and tests.
- Per-file diff with no golden-output comparison.
- Local Jest and deterministic validation receipts.
- Optional Salesforce check-only receipt.
- Validator advisory and controller-owned final disposition.
- A seeded high-risk case that pauses with `DecisionRequest`.
- Benchmark results separated by case, Wiki arm, and repetition, including
  typed intervention-reason recall and the honest Mule runtime limitation.

Update `README.md` only after behavior is verified so it remains the canonical
technical description of what the repository actually supports. Update the final
capstone report and presentation from captured evidence rather than planned
claims.

## Stop condition

Stop adding architecture when all of the following are true:

- The existing Account/Contact migration still works.
- The non-trivial Case migration works through the same shared workflow.
- No shared runtime or agent prompt requires either fixture's class/component
  names.
- The generated candidate is validated by behavior rather than golden code.
- Scenario identity and safety authority remain bound across UI, persistence,
  correction, export, and review.
- The cross-platform Mule slice remains working.
- The benchmark and final submission contain honest, reproducible evidence.

Anything beyond this boundary is productionization and is not required for the
capstone submission.

## Completion status (2026-08-30)

The bounded design required by the current submission contract is implemented.
The repository-wide dynamic naming, pagination, Case mutation, and
detail-loading ideas recorded in this historical plan remain intentionally
deferred, as the warning above explains. The harness now resolves three scenario
units — `salesforce-vf-to-lwc`, `case-management-console`, and
`mulesoft-mule3-to-mule4` — through one shared recipe, analyzer, validator
factory, and set of Architect/Engineer/Validator prompts. Status against the
stop condition:

The current runtime bindings use `architect/v17` at
`sha256:581db7f4caf415204c464b647a3f6636f104f5ae261caee5dde7d56385d932a5`,
`engineer/v30` at
`sha256:38fed22ed93704f208a4813c1954d0d4872de4c8de252b29057e66dffd2beeb0`,
and `validator/v5` at
`sha256:e2600215c92fd5bc34768c447788fcf5a20ef6470e85115835afc59f380f39f9`.

- **Existing Account/Contact migration still works** — verified through a
  fresh first-class Claude product-path run on attempt 1: all seven checks,
  9/9 candidate-authored Jest tests, and 10/10 controller-owned Jest tests
  passed. The candidate reached `ready_for_human_review`; independent final
  review and any fresh Claude org validation remain separate.
- **Non-trivial Case migration works through the same shared workflow** —
  verified through the first-class Claude product path. An earlier recovery
  run's attempt 1 passed 17/19 controller Jest tests; the evidence-directed
  attempt-2 correction changed only `caseManagementConsole.html` and
  `caseManagementConsole.js`, then passed all seven checks, 11/11 candidate
  Jest tests, and 19/19 controller Jest tests. That recovery candidate still
  awaits independent review. A separate final interactive run passed all seven
  checks, 7/7 candidate Jest tests, and 19/19 controller Jest tests on attempt
  1; BW independently accepted only that exact candidate, diff, and test
  evidence. No org result is claimed for either candidate.
- **No shared runtime or agent prompt requires either fixture's
  class/component names** — verified (design change 6; unit specifics flow
  through typed runtime context and per-unit registries keyed by unit ID that
  fail closed on unknown units).
- **Candidate validated by behavior, not golden code** — verified (per-unit
  controller-owned Jest suites: ten tests for Account/Contact, nineteen for
  Case Management).
- **Scenario identity and safety authority bound across UI, persistence,
  correction, export, and review** — verified (per-unit launch contract,
  presets, runtime scopes, closure/profile specs).
- **Cross-platform Mule slice remains structurally supported** — verified at the
  source-graph and validation-contract level. A fresh first-class Claude run
  generated the exact six-file candidate on attempt 1: three checks passed,
  none failed, and the toolchain/MUnit checks were unavailable, so the
  controller stopped at `environment_unavailable` without retry. Executable
  MUnit evidence remains outstanding.
- **Honest, reproducible harness evidence** — the complete current tree passed
  2,111 tests in 604.41 seconds (0:10:04) on 2026-08-30 with no failures or
  skips when ephemeral loopback binding was authorized. Ruff format checked 126
  Python files under `src/` and `tests/`, Ruff lint passed, mypy checked 70
  source files, and the 60-package lockfile, exactly-three-agent registry,
  source distribution, and wheel checks passed. The tracked sanitized product-run checkpoint is
  [`evaluation/submission-evidence/20260830/`](evaluation/submission-evidence/20260830/).
  This is product/harness evidence, not aggregate
  benchmark or external platform validation.

### Verification-sequence outcome

Steps 1–6 produced the frozen provider-free harness evidence described above.
An earlier live `qwen3.8:latest` Case attempt was an honest negative: Architect
completed, but Engineer failed the typed `EngineerModelOutcome` contract, so no
candidate or validation was produced. That historical local-model result is not
the current submission provider and is not benchmark-v2 evidence. Verification
step 7 is now complete: fresh first-class Claude Account/Contact and Case runs
produced durable tracked receipts. BW later accepted only the separate final
interactive attempt-1 Case candidate (7/7 candidate Jest tests; change-set
digest
`sha256:65a155e57d6ea2f993ddd5abe34224474dd311b89bce3bed6129a56a63e0f1b0`).
Account/Contact and the earlier attempt-2 recovery Case candidate (11/11
candidate Jest tests) remain outside that attestation and await independent
review. Step 8 is complete at the honest available boundary:
the fresh Claude Mule run produced a six-file candidate and static evidence,
then stopped at `environment_unavailable` because executable Maven/MUnit
authority remains absent. Neither product run is substituted for a benchmark
cell.

Step 9 is complete at its honest measured boundary. The first campaign remains quarantined as an
invalid pilot under archive SHA-256
`a7d15b41dbab1be18a924457a30ddd636730cfe8ce9514a44f60efae408936f5`.
BW reviewed the frozen 65-label subject (51 high and 14 low) and all 18
corrected campaign outputs. The corrected campaign completed 18/18 verified
terminal bundles: 5 `ready_for_human_review`, 4 `recoverable_failure`, 2
`environment_unavailable`, 1 `controlled_failure`, and 6 `decision_required`.
All nine no-Wiki cells cleared the former Architect policy rejection. The
campaign is bound to anchor
`sha256:6b65847d2b5a0d792fff878bb213b111e82b336063cf4d2700a6149bd1d3c0d8`
and runtime identity
`sha256:d038f0f2ce95607ad01fd51889385c35226577e30d02fa622bef44ce9b302a6c`;
its raw archive is `output/benchmark-v2-corrected-campaign-20260830.tar.gz`
with SHA-256
`f6a2e2ac0672a7631c0b6331e41a896574933c8704e2eb7707222ee5eeae1336`.
The independently checked v2 review-packet archive has SHA-256
`425fadd39e12b62226041f1a0bb8d95e100c1dd1ae5fc1846ec8b736e4232bae`.
The bound rubrics record acceptance and semantic conformance for all 18 cells,
no escaped defects, 390/390 dependency recall, and zero missed high-impact
dependencies. Assertion-level Wiki support was unavailable. Expected-outcome
conformance was 13/18 and Mule runtime validation remained unavailable, so the
measured quality gate is false and no Wiki-benefit conclusion is claimed. The
campaign predates the current `GraphAssuranceReport` runtime stage and remains
historical evaluation evidence rather than proof of assurance use. Step 10
passed at the final checkpoint:
2,111 tests in 604.41 seconds (0:10:04) plus Ruff format/lint, mypy, the 60-package
lockfile, exactly-three-agent, and package-build checks passed. The supplied
course template was filled as the six-page A4 PDF
`output/pdf/Final Capstone Report Planning - Automated Legacy Migration Agent -
2026-08-30 interactive-final.pdf`. All 10 canonical fields and widget
appearances passed form readback, every rendered page passed visual inspection,
and the tracked submission receipt binds its SHA-256. Its answers include the
completed BW Case and 18-cell reviews, the historical benchmark boundary, and
the final quality gate. No publication action has been taken.

### Salesforce environment boundary

The Case candidate factory now uses the signature-correct
`getCases(null, 'OPEN')` call, avoids assigning the read-only `Case.IsClosed`
field, and is covered by an arity-aware controller-test contract. Local static,
Apex-structure, and LWC Jest checks still do not constitute Salesforce org
compilation, deployment, or user-acceptance evidence; those environment-bound
layers remain explicitly unperformed.

Not performed by policy: no git commit, push, pull request, deployment, or org
mutation was made by this work; those remain human-owned actions.
