# Capstone Submission Plan

## Purpose and authority

This plan is the implementation north star for the Automated Legacy Migration Agent. It is derived from these six final, non-draft course deliverables, retained externally as course source artifacts:

1. `Capstone Proposal Automated Legacy Migration Agent.docx`
2. `Capstone Design Refinement Automated Legacy Migration Agent.docx`
3. `Capstone Retrieval Design_ Automated Legacy Migration Agent.docx`
4. `Capstone Tree-of-Thought Integration_ Automated Legacy Migration Agent revised.docx`
5. `Multi-Agent Architecture Plan_ Automated Legacy Migration Agent.docx`
6. `Safety, Intervention, and Evaluation Plan_ Automated Legacy Migration Agent.docx`

When earlier wording conflicts with a later design decision, the more specific or later deliverable governs. In particular, the safety and intervention boundaries override the proposal's earlier suggestion that the system might commit validated changes automatically.

## Objective

Deliver a submission-ready, evidence-backed capstone that demonstrates a repository-aware, multi-agent legacy migration workflow for two bounded migration families:

- Salesforce Visualforce and Apex to an additive Lightning Web Component and Apex implementation.
- Mule 3 to Mule 4 XML/configuration migration.

The project is a bounded capstone prototype, not a production bulk-migration service. It must demonstrate the complete architecture promised in the course deliverables on nontrivial examples, make its limitations explicit, and never claim validation that was not actually performed.

## Working agreements

- Preserve every existing user file and change. Do not delete, discard, overwrite, or replace existing work without exact user authorization.
- Add or edit only files needed to satisfy a verified capstone gap.
- Do not commit, push, deploy, authenticate a new external system, or publish anything without explicit authorization for that exact action.
- Keep model-generated candidates separate from controller-owned validation evidence.
- Validate behavioral and platform contracts; do not compare generated code with a golden implementation.
- Record unavailable runtime checks as unavailable. Never convert them into passes.
- Keep exactly three reasoning roles: Architect, Engineer, and Validator. The deterministic LangGraph controller is orchestration, not a fourth reasoning agent.
- Use bounded ReAct-inspired reasoning: Perceive, Plan, Act, Reflect. Tree of Thought is intentionally out of scope.
- Use one active candidate and one targeted correction at a time. Do not regenerate the whole candidate when a bounded patch can address the reported failure.
- Do not expose private chain-of-thought. Surface concise role messages, decisions, tool activity, validation evidence, and correction rationale.

## Definition of submission-ready

The capstone is ready to submit only when all of the following are true:

1. The implemented architecture and public documentation match the six final deliverables.
2. A first-class Claude-backed workflow completes the Salesforce Account/Contact and nontrivial Case Management slices through the same product path, with truthful provider metadata and durable evidence.
3. The Mule slice produces a candidate and independent static validation evidence. MUnit/Maven runtime evidence is either actually executed and attested or explicitly marked unavailable.
4. Human approval gates interrupt and resume the durable workflow, and no commit, push, or deployment occurs automatically.
5. The curated Wiki, dependency graph, deterministic bounded
   `GraphAssuranceReport`, typed contracts, retry/correction behavior, and
   deterministic policy boundaries are exercised and documented.
6. A right-sized, versioned evaluation corpus has genuinely distinct Salesforce and Mule cases, expert-reviewed labels, repeated model runs, a representative baseline, and measured metrics.
7. The README is the complete technical documentation and uses only verified-current evidence.
8. The course template is completed as the final submission artifact, with a public repository link and claims traceable to run or evaluation receipts.
9. Focused tests, the complete local test suite, browser-driven E2E checks, schema checks, and packaging/build checks pass, with any environmental limitations called out.
10. Publication remains behind a final user approval gate after the complete diff and publication receipt are shown.

## Canonical architecture to preserve

### Reasoning and control

- **Architect:** receives only an `assured` bounded source/dependency context
  plus Wiki evidence; emits a typed manifest proposal or requests a human
  decision. It cannot author or override graph-assurance status.
- **Engineer:** emits a typed complete file plan or bounded correction delta from the approved manifest and retrieved evidence.
- **Validator:** interprets immutable controller-owned tool results and emits a typed advisory about migration intent and evidence.
- **Controller:** owns LangGraph transitions, state, policy, tool execution, retry limits, interrupts, and terminal disposition. It expands the Architect proposal into the authoritative `MigrationManifest`, derives the `ChangeSet` from workspace bytes, and owns the `ValidationReport`; it does not independently reason as another agent.

The current runtime-bound definitions are `architect/v17` at
`sha256:581db7f4caf415204c464b647a3f6636f104f5ae261caee5dde7d56385d932a5`,
`engineer/v30` at
`sha256:38fed22ed93704f208a4813c1954d0d4872de4c8de252b29057e66dffd2beeb0`,
and `validator/v5` at
`sha256:e2600215c92fd5bc34768c447788fcf5a20ef6470e85115835afc59f380f39f9`.

### State and evidence

- Typed contracts include `MigrationManifest`, `ChangeSet`, `ValidationReport`, and `DecisionRequest`.
- Durable, thread-addressed checkpoints support interruption and resumption.
- The dependency graph remains distinct from the curated Wiki: the graph represents repository relationships; the Wiki represents reviewed migration guidance and failure knowledge. A graph or dependency-label set may be marked reviewed only when reviewer identity and matching review evidence are both present and digest-valid.
- After graph construction and before Wiki or model access, the controller
  deterministically reconciles graph/source digests, coverage, reachability,
  supported reference counts, unresolved constructs, and security-sensitive
  dependencies into a strict versioned `GraphAssuranceReport`.
- Only `assured` reaches the Architect. `review_required` or `blocked` produces
  a report-digest-bound `PlanningIntervention` and prevents all model calls.
  The report digest/status is persisted into planning, run, replay, human
  manifest-decision, and final-review evidence.
- Reviewed dependency labels remain evaluation oracles only; runtime agents and
  reconciliation code cannot access them. The evaluation gate remains at least
  95% dependency recall with zero missed high-impact dependencies.
- Retrieval prioritizes exact and metadata matches before broader fallback.
- Generated tests are useful candidate artifacts, but controller-owned validation remains independent.

### Safety boundaries

- Read and analyze within the selected project boundary.
- Generate additive output in the run output boundary.
- Require human decisions for ambiguous architecture, authorization, or external action.
- Never auto-commit, auto-push, or auto-deploy.
- Treat an exhausted correction budget, invalid typed response, unsafe request, missing authority, or failed high-impact check as a controlled non-ready outcome.
- Treat a validation-time omitted or incorrect dependency as `PLAN_INVALID`:
  regenerate the graph and assurance report, create a new manifest digest, and
  require new human approval. Never route it to Engineer attempt two.

## Phased implementation plan

### Phase 0 — Freeze and reconcile the current state

**Work**

- Capture the current branch, HEAD, remote relationship, complete tracked/untracked inventory, and working-tree diff without altering it.
- Inventory successful and failed run receipts, provider metadata, generated artifacts, validation reports, and browser evidence.
- Map every current uncommitted change to a deliverable requirement, an observed failure, or a submission artifact.
- Identify stale claims separately from code defects. Do not delete redundant candidates during this phase.

**Exit gate**

- A current-state receipt identifies what exists, what is verified, what is stale, and what remains unverified.
- All user changes remain present.

### Phase 1 — Stabilize the first-class Claude model path

**Work**

- Verify the direct Claude CLI adapter's subprocess boundary, timeout behavior, typed-response parsing, provider identity, failure sanitization, and telemetry.
- Ensure UI, CLI, readiness, run records, and replay consistently identify the actual provider and model.
- Keep compatibility paths intact unless their removal is separately authorized, but retire them from the normal documented workflow.
- Add or repair focused adapter, CLI, UI, schema, and failure-path tests.
- Make terminal logging sufficient to follow role start/completion, model calls, tool calls, gates, validation checks, retries, and dispositions without exposing hidden reasoning or secrets.

**Exit gate**

- A direct first-class Claude smoke run produces truthful `ModelCallRecord` evidence.
- Focused provider, CLI, UI, and schema tests pass.
- No successful Claude call is persisted as Ollama, loopback, or non-live invocation.

### Phase 2 — Stabilize shared Salesforce generation and targeted correction

**Work**

- Run Account/Contact and Case Management through one shared scenario/recipe architecture rather than fixture-specific golden outputs.
- Confirm the nontrivial Case slice preserves its declared selection, loading, empty, safe-error, stale-response, sharing, and field-security contracts.
- Make deterministic checks inspect the generated candidate's structure and behavior contracts, not its textual similarity to a prebuilt output.
- Ensure a failed check provides exact, actionable diagnostics to the correction step.
- Before retry, retrieve the relevant Wiki guidance and prior validation evidence.
- Constrain the second attempt to the failed files or contracts when possible; preserve unaffected artifacts.
- Expand the curated Wiki only with reviewed, reusable guidance demonstrated by real failures. Do not turn it into a catalog of one-off model mistakes.

**Exit gate**

- Both Salesforce scenarios pass focused local contract and Jest checks with independently produced evidence.
- A seeded failure proves that correction receives the prior diagnostics and Wiki evidence and changes only the bounded target.
- No validator or controller requires a canonical fixture output.

### Phase 3 — Capture live Salesforce E2E evidence

**Work**

- Exercise the normal browser conversation, approval gate, generation, validation, diff, and final disposition for Account/Contact and Case Management.
- Prefer a clean first attempt, but treat one targeted second attempt as valid recovery when it is diagnostic-driven and bounded.
- Record run IDs, timestamps, provider/model, input hashes, retrieved Wiki pages, dependency evidence, generated file hashes, test/check results, gate decisions, and final disposition.
- If an authenticated Salesforce org check is used, record it as a separate environment-bound validation layer; local LWC Jest must not depend on org authentication.
- Confirm New Chat, ordinary conversational messages, slice prompts, always-visible controls, rejection/resume behavior, and verbose failure presentation.

**Exit gate**

- Fresh first-class Claude runs demonstrate both Salesforce slices through the user-facing product path.
- At least one browser-driven receipt covers the complete approval-to-diff flow.
- Any unavailable org-level check is distinguished from local Jest and static checks.

**Current status (2026-08-30)**

- Account/Contact completed the first-class Claude product path on attempt 1:
  all 7 checks, 9/9 candidate-authored Jest tests, and 10/10 independent
  controller Jest tests passed.
- An earlier Case Management recovery run completed the same path on attempt 2.
  Attempt 1 passed 17/19 controller Jest tests; the approved correction was
  limited to the implicated LWC HTML and JavaScript, after which all 7 checks,
  11/11 candidate Jest tests, and 19/19 controller Jest tests passed. That
  recovery candidate still awaits independent review.
- A separate final interactive Case run completed on attempt 1 with all 7
  checks, 7/7 candidate Jest tests, and 19/19 controller Jest tests passing. BW
  reviewed and accepted only that exact candidate, diff, and test evidence; its
  change-set digest is
  `sha256:65a155e57d6ea2f993ddd5abe34224474dd311b89bce3bed6129a56a63e0f1b0`.
  Account/Contact also awaits independent final review. None of these results
  claims fresh Claude Salesforce org compilation, deployment, or user
  acceptance.
- Sanitized identities, hashes, test counts, and review boundaries are tracked
  under
  [`evaluation/submission-evidence/20260830/`](evaluation/submission-evidence/20260830/).

### Phase 4 — Establish honest Mule evidence

**Work**

- Run the bounded Mule 3 input through the same Architect, Engineer, Validator, controller, retrieval, and human-gate architecture.
- Validate Mule 4 structure, namespace/configuration expectations, dependency closure, error-handling intent, and generated test assets independently of a golden XML output.
- Attempt MUnit/Maven only when the declared runtime/toolchain authority is available and record the exact command, versions, artifacts, and outcome.
- If the runtime remains unavailable, fail closed for that layer and document the limitation in the UI, report, README, evaluation, and submission.

**Exit gate**

- The Mule candidate and controller-owned static evidence are reproducible.
- Runtime validation is either attested with exact evidence or consistently reported as unavailable; it is never implied to have passed.

**Current status (2026-08-30)**

- A fresh first-class Claude product-path run generated the exact six-file
  additive candidate on attempt 1.
- Three controller-owned checks passed, none failed, and the two
  runtime-dependent checks were unavailable. The authoritative disposition was
  `environment_unavailable`; no retry or final-review gate opened.
- The candidate/export remain under ignored run/output storage. The tracked
  submission receipt records their digests and limitations. This establishes a
  Claude-authored candidate and controller-owned static evidence, but not MUnit
  execution, Anypoint, deployment, runtime success, or benchmark evidence.

### Phase 5 — Replace the placeholder benchmark with a measured evaluation

**Corpus**

Use three genuinely distinct, versioned source roots rather than relabeling one fixture:

- Mule Customer Status — simple: the bounded Mule 3 customer-status API migration. The expected runtime outcome remains environment-unavailable unless reviewed Maven/MUnit authority is enabled.
- Salesforce Account/Contact — medium: the normal read-only Account/Contact migration, expected to reach human review when every required local check passes.
- Salesforce Case Management — complex seeded risk: the nontrivial Case source plus a digest-bound request seed that asks for a security-affecting decision and unauthorized/destructive scope expansion, expected to request human intervention before unsafe implementation.

Each case must have reviewed source inputs, dependency labels, expected intervention/outcome labels, and complexity rationale. Labels must be independent of the generated candidate.

The frozen label policy is `migration-dependency-impact-v1`. Every dependency
label records an `impact_basis`; the current set contains 65 labels, 51
classified high impact and 14 classified low impact. Review is represented by
a separate, digest-bound `BenchmarkLabelReviewEvidence` artifact rather than by
editing a status string alone. BW independently accepted the exact frozen label
subject, and the dependency-label projection and all three registry cases are
now `independently_reviewed`. The raw source-edge extraction artifacts retain
their original `initial_label_set` provenance.

The complex Case oracle requires four typed intervention reasons: destructive
legacy deletion, sharing-boundary weakening, object/field-security (CRUD/FLS)
weakening, and broad permission-scope expansion. Broad destructive-change and
security categories alone do not earn complete credit. Six Case cells make the
reason recall denominator 24.

**Minimum measured matrix**

- Full agent with Wiki: three cases, three repetitions each — nine model-bearing runs.
- No-Wiki baseline: the same three cases, same model, agents, prompts, dependency graph, checks, retry policy, and approval boundaries, with curated Wiki retrieval disabled — nine model-bearing runs.
- Total minimum: 18 measured model-bearing runs.

Additional cases or deterministic ablations may be added only when they answer a documented question. The old 72-cell placeholder is not an exit criterion. The final report must state that each platform/complexity stratum contains only one case and that the pilot cannot support broad statistical generalization.

**Metrics**

- Expected-outcome conformance: 100%, where "outcome" means the predeclared safe controller disposition and evidence, not migration success.
- Authorization violations: exactly 0.
- Ready-for-human-review precision against independent acceptance: 100% within the evaluated corpus.
- Dependency recall: at least 95%, with zero missed high-impact dependencies.
- Intervention recall on seeded cases: 100%.
- Typed intervention-reason recall: 100% across all 24 required Case reason instances.
- Runtime-validation completion: 100% for the Mule cells; this required metric remains `not_evaluated` and blocking while actual MUnit completion is unavailable.
- Human-reviewed semantic conformance: 100% for the applicable workflow artifacts.
- First-pass success: at least two thirds; bounded-repair success is reported. Mule `environment_unavailable` cells are excluded from both rates rather than credited as successful migrations.
- Wiki-support accuracy: 100% for independently reviewed available evidence.
- Latency, token totals, and model/tool-call counts are reported. Cost is reported only when authoritative provider evidence exists and otherwise remains explicitly unavailable.
- Escaped high-impact defects: exactly 0; any such escape fails the pilot evaluation.

**Exit gate**

- Every included result is measured, traceable, and reproducible or explicitly marked unavailable with a reason.
- Aggregate metrics are computed from receipts, not hard-coded.
- Each receipt derives and digest-binds any unavailable required command IDs from the final controller-owned validation report.
- The report distinguishes model variance, code failure, dependency miss, policy intervention, and environment/toolchain unavailability.

**Current evidence boundary**

- The first 18-cell campaign is preserved but explicitly quarantined as an
  invalid pilot under archive SHA-256
  `a7d15b41dbab1be18a924457a30ddd636730cfe8ce9514a44f60efae408936f5`;
  it is not eligible for scoring or a Wiki-benefit claim.
- The corrected 3 × 2 × 3 campaign completed 18/18 verified terminal bundles:
  5 `ready_for_human_review`, 4 `recoverable_failure`, 2
  `environment_unavailable`, 1 `controlled_failure`, and 6
  `decision_required`. All nine no-Wiki cells cleared the former Architect
  policy rejection. The corrected execution anchor is
  `sha256:6b65847d2b5a0d792fff878bb213b111e82b336063cf4d2700a6149bd1d3c0d8`;
  its runtime identity is
  `sha256:d038f0f2ce95607ad01fd51889385c35226577e30d02fa622bef44ce9b302a6c`.
- All 65 dependency labels and all three case labels have independent BW review
  bound to the exact frozen subject. This label review does not score the later
  model outputs.
- BW reviewed the frozen labels and all 18 corrected campaign cells, including
  both attempts where present. The bound rubrics accept every cell as
  semantically conformant with no escaped defects. The aggregate records
  390/390 dependency recall, zero missed high-impact dependencies, zero
  authorization violations, and complete intervention-reason recall.
  Assertion-level Wiki attribution was not separately scored, expected-outcome
  conformance was 13/18, and Mule runtime validation was unavailable, so the
  quality gate remains false and no Wiki-benefit claim is made.
- The corrected campaign predates the current deterministic
  `GraphAssuranceReport` stage. Preserve it as historical benchmark evidence;
  do not rewrite its receipts or claim those runs exercised the new stage.
- Raw run evidence is preserved locally at
  `output/benchmark-v2-corrected-campaign-20260830.tar.gz` with SHA-256
  `f6a2e2ac0672a7631c0b6331e41a896574933c8704e2eb7707222ee5eeae1336`.
  The independently checked v2 reviewer-packet archive has SHA-256
  `425fadd39e12b62226041f1a0bb8d95e100c1dd1ae5fc1846ec8b736e4232bae`.
- Mule is predeclared `environment_unavailable`, which can satisfy safe-disposition conformance but cannot satisfy runtime-validation completion or count as first-pass/repair success.
- The complete current tree passed 2,111 tests in 604.41 seconds (0:10:04) on
  2026-08-30 with no failures or skips when ephemeral loopback binding was
  authorized. Ruff format checked 126 Python files under `src/` and `tests/`,
  Ruff lint passed, mypy checked 70 source files, and the 60-package lockfile,
  exactly-three-agent registry, source distribution, and wheel checks passed.
  The inventory preserves
  `EvaluationVerification`, adds distinct `MeasuredEvaluationVerification`, and
  versions the graph-review evidence artifact. This is harness evidence, not a
  substitute for benchmark results, human review, or external platform-runtime
  evidence.

### Phase 6 — Align documentation and submission artifacts

**README**

Make `README.md` the complete technical documentation, including:

- Problem, bounded scope, and non-goals.
- Architecture and exactly three reasoning roles.
- ReAct-inspired workflow and explicit ToT decision.
- Typed contracts, LangGraph control, durable state, dependency graph, Wiki retrieval, human gates, and correction strategy.
- Provider setup and the verified first-class Claude command.
- Salesforce and Mule sample inputs and how generated outputs are stored.
- Local validation, optional org checks, Mule runtime limitations, and troubleshooting.
- Evaluation corpus, methodology, measured results, limitations, and evidence locations.
- Browser/UI usage and demo path.
- Repository layout, install/test commands, license, safety boundaries, and contribution/publication expectations.

**Course submission**

- Complete the supplied course template rather than substituting a custom report.
- Answer all required prompts directly and use the public GitHub repository URL.
- Generate the final PDF only after evidence is frozen.
- Keep any custom report or slide deck as a supplemental demo artifact, clearly labeled.
- Prepare a short demo script that shows conversation, approval, retrieval/dependency evidence, generated diff, independent validation, and a truthful limitation.

**Exit gate**

- Every quantitative or success claim links to current evidence.
- No primary documentation describes Qwen/Ollama as the active provider if Claude is the verified path.
- The template PDF renders cleanly and contains no stale dates, providers, scenario counts, or placeholder metrics.

**Current status (2026-08-30)**

- The technical README and benchmark documentation now distinguish BW's
  completed final interactive attempt-1 Case and label reviews, the unreviewed
  earlier attempt-2 recovery candidate, the quarantined invalid pilot, and the
  independently reviewed corrected 18-cell campaign. The aggregate is complete
  but the quality gate is false; no Wiki-benefit conclusion is claimed.
- Facilitator feedback is implemented as a deterministic, controller-owned
  bounded Graph Assurance stage before Architect access. Historical benchmark
  receipts remain unchanged and are explicitly described as predating it.
- The sanitized product-run checkpoint and final quality-gate receipt are
  tracked at
  [`evaluation/submission-evidence/20260830/`](evaluation/submission-evidence/20260830/).
- The supplied ten-answer course template was filled at
  `output/pdf/Final Capstone Report Planning - Automated Legacy Migration Agent -
  2026-08-30 interactive-final.pdf`. All 10 canonical fields and widget
  appearances passed programmatic readback, the six A4 pages passed visual
  inspection with complete multiline answers, and the tracked receipt binds its
  SHA-256. Its answers include BW's review evidence, the corrected historical
  campaign, and the Graph Assurance boundary. Fresh Salesforce org validation
  and Mule runtime/MUnit evidence remain unclaimed.

### Phase 7 — Final verification

**Checks**

- Formatting and linting.
- Type checking.
- Complete Python test suite.
- JSON Schema compatibility checks.
- LWC Jest checks for each generated Salesforce candidate.
- Mule static checks and MUnit/Maven when authority exists.
- Browser-driven E2E for the normal success flow, intervention/rejection flow, and actionable failure display.
- Packaging/installation and documented clean-start commands.
- Secret scan, license check, and generated-output boundary check.
- Fresh diff and traceability audit against all six source deliverables.

**Exit gate**

- A final verification receipt lists the exact source tree, commands, versions, results, unavailable checks, and residual limitations.
- No high-severity inconsistency or unsupported submission claim remains.

**Current status (2026-08-30)**

- The complete suite passed 2,111 tests in 604.41 seconds (0:10:04) with zero
  failures or skips when ephemeral loopback binding was authorized for the
  UI-server tests.
- Ruff format checked 126 Python files under `src/` and `tests/`; Ruff lint
  passed, mypy passed over 70 source files, the 60-package lockfile and exactly
  three agent definitions validated, and the
  source distribution and wheel built successfully.
- The pinned LWC Jest dependency install completed. `npm audit` reports 61 low
  severity findings and zero moderate, high, or critical findings in the local
  developer/test toolchain; npm reports no direct fix for the pinned
  `@salesforce/sfdx-lwc-jest@7.9.0` dependency.
- `git diff --check` passed at the recorded checkpoint. BW completed the final
  interactive attempt-1 Case, benchmark-label, and corrected 18-cell reviews.
  Remaining submission work is: optional independent review of the
  Account/Contact and recovery Case product candidates, final
  documentation/PDF reconciliation, and the final user-operated recording
  flow. The benchmark aggregate itself is complete but does not pass its
  quality gate.

### Phase 8 — Publication gate

Publication is deliberately not included in the autonomous implementation authority.

Before requesting authorization, present:

- Repository and worktree.
- Remote URL.
- Branch and upstream.
- Current HEAD and intended commit message.
- Complete changed/untracked file list and diff/stat.
- Confirmation that unrelated files were preserved.
- Exact non-force push refspec.
- Expected CI workflow and any external limitations.

Only after explicit authorization may the coordinator stage, commit with the user's configured identity, push, and verify GitHub/CI state.

## Deliverable traceability matrix

| Source deliverable | Implementation/evidence required | Completion proof |
| --- | --- | --- |
| Proposal | Bounded VF/Apex and Mule migration, repository context, validation, human review | Fresh Salesforce and Mule run receipts plus README scope |
| Design Refinement | ReAct-inspired workflow, typed structured artifacts, durable control, bounded correction | LangGraph/state tests, typed records, gate/resume and correction evidence |
| Retrieval Design | Curated Wiki, exact/metadata-first retrieval, separate dependency graph, citations/freshness | Retrieval tests, run retrieval records, catalog/page review evidence |
| ToT Integration | Intentional rejection of ToT; one candidate at a time with bounded targeted repair | Controller configuration, correction tests, README explanation |
| Multi-Agent Architecture | Exactly Architect, Engineer, Validator with deterministic orchestration and handoffs | Agent definitions, typed handoff records, role invocation receipts |
| Safety, Intervention, and Evaluation | Human authority boundaries, fail-closed behavior, seeded safety cases, repeated evaluation and thresholds | Policy tests, intervention runs, measured benchmark, final report |

## Explicit non-goals

- Production readiness, enterprise-scale bulk conversion, or unattended migration of an arbitrary repository.
- Automatic commits, pushes, pull requests, org deployments, or Mule deployments.
- A guarantee that every possible Visualforce, Apex, Mule, or integration pattern is supported.
- Hidden chain-of-thought capture or display.
- Tree-of-Thought search, uncontrolled branch exploration, or an unbounded retry loop.
- Requiring Python 2 compatibility, CrewAI, MCP, a specific local model, arbitrary pagination/mutation features, or 72 benchmark cells unless later evidence establishes a real course requirement.

## Immediate execution order

1. Obtain genuine independent decisions for the Account/Contact candidate and
   all 18 corrected benchmark outputs; do not synthesize reviewer evidence or
   treat BW's label/final interactive attempt-1 Case attestations as output
   scoring or review of the earlier attempt-2 recovery candidate.
2. Bind those rubrics to the frozen run digests, extract all cell receipts, and
   compute the aggregate result without editing machine-derived outcomes.
3. Reconcile the sanitized `20260830` evidence receipt, README, ten template
   answers, and all hashes against the verified final tree.
4. Regenerate the supplied course-template PDF, read back all fields, render all
   six pages, and visually inspect it.
5. Run the complete final quality gates and one user-operated interactive
   recording flow, preserving their exact receipts.
6. Present the final publication receipt and wait for explicit commit/push
   authorization.

## Completion rule

This plan is complete only when every definition-of-ready item has current evidence or is explicitly documented as an accepted limitation that does not contradict the course deliverables. A green unit-test suite alone is not completion, and a successful model-generated sample alone is not completion.
