# Capstone Submission Plan

## Purpose and authority

This plan is the implementation north star for the Automated Legacy Migration Agent. It is derived from the six final, non-draft course deliverables in `/Users/gurleen.singh/Documents/Course 2026/deliverables/`:

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
5. The curated Wiki, dependency graph, typed contracts, retry/correction behavior, and deterministic policy boundaries are exercised and documented.
6. A right-sized, versioned evaluation corpus has genuinely distinct Salesforce and Mule cases, expert-reviewed labels, repeated model runs, a representative baseline, and measured metrics.
7. The README is the complete technical documentation and uses only verified-current evidence.
8. The course template is completed as the final submission artifact, with a public repository link and claims traceable to run or evaluation receipts.
9. Focused tests, the complete local test suite, browser-driven E2E checks, schema checks, and packaging/build checks pass, with any environmental limitations called out.
10. Publication remains behind a final user approval gate after the complete diff and publication receipt are shown.

## Canonical architecture to preserve

### Reasoning and control

- **Architect:** inspects the bounded source, repository context, dependency graph, and Wiki evidence; emits a typed `MigrationManifest` or requests a human decision.
- **Engineer:** generates an additive candidate `ChangeSet` from the approved manifest and retrieved evidence.
- **Validator:** interprets controller-owned tool results, checks the migration intent, and emits a typed `ValidationReport`.
- **Controller:** owns LangGraph transitions, state, policy, tool execution, retry limits, interrupts, and terminal disposition. It does not independently reason as another agent.

### State and evidence

- Typed contracts include `MigrationManifest`, `ChangeSet`, `ValidationReport`, and `DecisionRequest`.
- Durable, thread-addressed checkpoints support interruption and resumption.
- The dependency graph remains distinct from the curated Wiki: the graph represents repository relationships; the Wiki represents reviewed migration guidance and failure knowledge.
- Retrieval prioritizes exact and metadata matches before broader fallback.
- Generated tests are useful candidate artifacts, but controller-owned validation remains independent.

### Safety boundaries

- Read and analyze within the selected project boundary.
- Generate additive output in the run output boundary.
- Require human decisions for ambiguous architecture, authorization, or external action.
- Never auto-commit, auto-push, or auto-deploy.
- Treat an exhausted correction budget, invalid typed response, unsafe request, missing authority, or failed high-impact check as a controlled non-ready outcome.

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

### Phase 4 — Establish honest Mule evidence

**Work**

- Run the bounded Mule 3 input through the same Architect, Engineer, Validator, controller, retrieval, and human-gate architecture.
- Validate Mule 4 structure, namespace/configuration expectations, dependency closure, error-handling intent, and generated test assets independently of a golden XML output.
- Attempt MUnit/Maven only when the declared runtime/toolchain authority is available and record the exact command, versions, artifacts, and outcome.
- If the runtime remains unavailable, fail closed for that layer and document the limitation in the UI, report, README, evaluation, and submission.

**Exit gate**

- The Mule candidate and controller-owned static evidence are reproducible.
- Runtime validation is either attested with exact evidence or consistently reported as unavailable; it is never implied to have passed.

### Phase 5 — Replace the placeholder benchmark with a measured evaluation

**Corpus**

Create six genuinely distinct, versioned cases rather than relabeling the same source fixture:

- Salesforce simple: Account/Contact read-only migration.
- Salesforce medium: Case Management console with multiple states and dependencies.
- Salesforce complex/safety: a distinct seeded ambiguity, contract conflict, or authorization-sensitive case expected to request human intervention.
- Mule simple: a distinct bounded subflow/configuration migration.
- Mule medium: the customer-status API slice.
- Mule complex/safety: a distinct seeded contract, dependency, or security ambiguity expected to request human intervention.

Each case must have reviewed source inputs, dependency labels, expected intervention/outcome labels, and complexity rationale. Labels must be independent of the generated candidate.

**Minimum measured matrix**

- Full agent with Wiki: six cases, two runs per case — 12 model-bearing runs.
- No-Wiki baseline: one representative medium Salesforce case and one representative medium Mule case, two runs each — four model-bearing runs.
- Total minimum: 16 measured model-bearing runs.

Additional deterministic ablations may be added only when they answer a documented question. The old 72-cell placeholder is not an exit criterion.

**Metrics**

- Authorization violations: exactly 0.
- Ready-to-merge precision: 100% within the evaluated corpus.
- Dependency recall: at least 95%, with zero missed high-impact dependencies.
- Intervention recall on seeded cases: 100%.
- Validation-report precision against expert review.
- First-pass success and bounded-repair success.
- Wiki retrieval accuracy and usefulness.
- Latency, model/tool-call count, and available cost proxy.
- Escaped high-impact defects: exactly 0; any such escape fails the pilot evaluation.

**Exit gate**

- Every included result is measured, traceable, and reproducible or explicitly marked unavailable with a reason.
- Aggregate metrics are computed from receipts, not hard-coded.
- The report distinguishes model variance, code failure, dependency miss, policy intervention, and environment/toolchain unavailability.

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

1. Preserve and inventory the current dirty working tree and all existing run evidence.
2. Reconcile the direct Claude adapter and Case Management changes against focused tests and actual failure receipts.
3. Fix the smallest shared-path defects needed for truthful first-class Claude runs.
4. Capture fresh Account/Contact and Case Management browser E2E receipts.
5. Establish the Mule evidence boundary.
6. Build and run the measured evaluation corpus.
7. Update documentation and generate the course-template PDF from frozen evidence.
8. Run the complete final verification suite.
9. Present a publication receipt and wait for explicit commit/push authorization.

## Completion rule

This plan is complete only when every definition-of-ready item has current evidence or is explicitly documented as an accepted limitation that does not contradict the course deliverables. A green unit-test suite alone is not completion, and a successful model-generated sample alone is not completion.
