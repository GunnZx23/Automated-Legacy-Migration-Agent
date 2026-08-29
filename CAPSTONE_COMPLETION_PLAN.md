# Capstone Completion Plan: Generalized Bounded Migration Units

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
2. Discover and freeze the relevant dependency closure.
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

### 9. Replace the duplicated benchmark labels with real cases

The current evaluation matrix labels simple, medium, and complex cases while
reusing one source root per platform. Replace those labels with distinct source
snapshots or genuinely distinct entry points.

Minimum credible corpus:

- Salesforce simple: existing Account/Contact explorer.
- Salesforce medium: new Case Management console.
- Salesforce complex safety case: a distinct Case variant containing a shared
  public contract or security-sensitive dependency that must produce
  `DecisionRequest`.
- Mule simple: a small, distinct DataWeave/subflow unit.
- Mule medium: existing Customer Status API.
- Mule complex safety case: a distinct shared API-contract or security-sensitive
  variant that must produce `DecisionRequest`.

Complex safety variants need not be full successful migrations; their purpose is
to prove intervention behavior. Each case must have a distinct source snapshot
digest and expected dependency/decision evidence.

Likely files:

- `src/legacy_migration_agent/evaluation.py`
- `evaluation/`
- `fixtures/`
- `tests/test_evaluation.py`
- `tests/test_graph_evaluation.py`

Acceptance gate:

- Evaluation rejects duplicate source identity masquerading as different
  complexity cases.
- Metrics are reported by platform and complexity.
- Authorization violations remain zero, ready-state precision remains 100%, and
  high-risk intervention recall remains 100% in the committed evaluation set.

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
7. Run one live `qwen3.8:latest` end-to-end migration of the new unit, targeting
   first-pass success; if correction is offered, confirm it is evidence-directed
   and file-scoped.
8. If separately authorized, run check-only Salesforce validation against the
   authenticated dev org and persist the terminal job receipt.
9. Run the benchmark comparison and repeated nondeterministic trials only after
   the live workflow is stable.

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
- Benchmark results separated by platform and complexity.

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

## Completion status (2026-08-28)

All nine design changes are implemented and the harness now resolves three
scenario units — `salesforce-vf-to-lwc`, `case-management-console`, and
`mulesoft-mule3-to-mule4` — through one shared recipe, analyzer, validator
factory, and set of Architect/Engineer/Validator prompts. Status against the
stop condition:

- **Existing Account/Contact migration still works** — verified (full suite
  green; recorded, live, and org evidence unchanged).
- **Non-trivial Case migration works through the same shared workflow** —
  verified deterministically: the recorded-model workflow test and a
  browser-driven run both reach `ready_for_human_review` with all seven required
  Salesforce local checks passing on the real Jest/sandbox toolchain, and the
  candidate is accepted and exportable.
- **No shared runtime or agent prompt requires either fixture's
  class/component names** — verified (design change 6; unit specifics flow
  through typed runtime context and per-unit registries keyed by unit ID that
  fail closed on unknown units).
- **Candidate validated by behavior, not golden code** — verified (per-unit
  controller-owned Jest suites: nine behaviors for Account/Contact, twelve for
  Case Management).
- **Scenario identity and safety authority bound across UI, persistence,
  correction, export, and review** — verified (per-unit launch contract,
  presets, runtime scopes, closure/profile specs).
- **Cross-platform Mule slice remains working** — verified.
- **Honest, reproducible evidence** — verified. Full Python suite: 1328 passed,
  0 failed. Browser E2E driver: `tooling/e2e/case_browser_e2e.md`.

### Verification-sequence outcome

Steps 1–6 pass. **Step 7** (live `qwen3.8:latest` end-to-end for the new unit)
was attempted and is an honest **negative**: the live Architect (conversation
and ~303 s manifest proposal) succeeded, but the Engineer's eleven-file output
failed the typed `EngineerModelOutcome` contract with one schema validation
error, so the harness fail-closed (`controlled_failure`, non-retry-eligible, no
candidate written, validation never run). At temperature zero this outcome is
deterministic, so a plain retry would not change it. The shared Engineer prompt
was deliberately **not** tuned toward the Case answer — doing so would violate
design change 6 and risk golden-output leakage. The Account/Contact unit remains
the only unit with a successful live migration and external platform evidence;
the Case unit demonstrates the harness's generalization and fail-closed contract
rather than live model success. **Steps 8–9** (Case org check-only; statistical
benchmark) remain out of scope / `not_performed` and are not claimed.

### Known caveat carried forward

The regex-based controller-test null-coverage assertion (`_check_controller_test`)
matches a call whose entire argument list is exactly `null`, so the synthetic
Apex test double calls one-argument `getCases(null)` even though the real Case
service method is two-argument. This mirrors the Account/Contact `getContacts(null)`
precedent and is acceptable within scope — local checks are regex-based and org
deployment is explicitly out of scope — but it would need addressing before any
real Case org-deployment claim.

Not performed by policy: no git commit, push, pull request, deployment, or org
mutation was made by this work; those remain human-owned actions.
