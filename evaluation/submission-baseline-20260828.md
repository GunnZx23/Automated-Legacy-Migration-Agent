# Submission Baseline Receipt — 2026-08-28

## Purpose

This receipt freezes the evidence state before fresh first-class Claude runs and formal capstone evaluation. It separates verified implementation facts from historical, wrapper-based, recorded-double, exploratory, and unavailable evidence.

It does not declare the capstone submission-ready.

## Repository baseline

| Field | Value |
| --- | --- |
| Repository | `Automated-Legacy-Migration-Agent` |
| Worktree | Local repository root (host path omitted from publishable evidence) |
| Branch | `main` |
| User-created recovery commit | `cd078321a4bf59cc38b3f30635022664b5dd989f` |
| Commit subject | `claude wiring` |
| Commit author | `GunnZx23 <27504659+GunnZx23@users.noreply.github.com>` |
| Remote | `https://github.com/GunnZx23/Automated-Legacy-Migration-Agent.git` |
| Remote state at baseline | Local `main` was one commit ahead of `origin/main` |
| Publication | Not performed by this task |

The user-created commit is the recovery point. No history rewrite, commit, push, deployment, or file deletion is authorized by this receipt.

## Source-of-truth boundary

The implementation target is `CAPSTONE_SUBMISSION_PLAN.md`, derived from the six final non-draft DOCX deliverables. `CAPSTONE_COMPLETION_PLAN.md` is retained as historical planning context where it differs from the source-derived submission plan.

The canonical system remains:

- exactly three reasoning agents: Architect, Engineer, Validator;
- deterministic LangGraph orchestration, not a fourth agent;
- bounded Perceive, Plan, Act, Reflect reasoning, with Tree of Thought rejected;
- typed manifests, changes, reports, and decision requests;
- curated Wiki and separate repository dependency graph;
- controller-owned validation, one bounded targeted retry, durable human gates;
- no automatic commit, push, or deployment;
- behavior/contract validation rather than golden-output comparison.

## First-class Claude implementation status

The normal CLI path now wires the provider directly:

`legacy-migration-agent ui --claude-model ...` → CLI approval gates → UI server → `AgentUiService` → `build_claude_cli_model_clients` → `ClaudeCliStructuredModelClient` → Architect/Engineer/Validator typed calls.

The intended persisted identity is:

- `provider = claude-cli`
- `live_invocation = true`
- `execution_boundary = remote_provider_managed`
- `store_false_sent = false`
- `model_revision = null`
- a SHA-256 runtime-identity digest bound to the model alias, Claude CLI version, and authentication provider

The runtime identity does not attest to a provider model-weight revision. Loopback browser transport does not make remote inference local, and the adapter does not claim provider-side zero retention.

At this baseline, no successful normal UI run persists all of those first-class values. Fresh evidence is required.

## Existing execution evidence

| Evidence | Result | Classification | Submission-safe claim |
| --- | --- | --- | --- |
| `.runs/agent-ui/41d54b2fa066cd719903945d/` | Account/Contact attempt 1, 7/7, ready for human review, accepted | Claude compatibility wrapper persisted as Ollama/local/non-live | A legacy wrapper-driven candidate passed all local checks. It is not first-class Claude provenance and has no Claude org evidence. |
| `.runs/agent-ui/cff6cdea682e17bfe232ec76/` | Account/Contact attempt 1 at 6/7; one-file attempt-2 repair reached 7/7 and was accepted | Strong bounded-correction behavior, but wrapper metadata is inaccurate | A diagnostic-driven, one-file Apex repair recovered locally. Do not count it as a first-class Claude benchmark run. |
| `.runs/agent-ui/3bc3ebec1e3e6400e11d4952/` | Case attempt 1, 7/7, accepted; candidate Jest 10/10 and controller Jest 12/12 | Recorded model double with synthetic runtime identity and telemetry | Product-path/harness evidence only, not live model-quality evidence. |
| `.runs/agent-ui/5638bb5b626f5d3b94c1a974/` | Second Case attempt 1, 7/7, accepted; candidate Jest 10/10 and controller Jest 12/12 | Recorded model double | Repeated product-path/harness evidence only. |
| `evaluation/model-comparison-20260828/case-results/claude-cli-claude-sonnet-5/80766d31fb5b-result.json` | Direct Claude Engineer output was typed and locally validated; 4/7, recoverable failure | Exploratory Engineer-only Case generation | Direct Claude was invoked and failed closed. This is not a three-agent UI E2E run. |
| `evaluation/model-comparison-20260828/case-results/claude-cli-claude-sonnet-5/a929245e0b87-result.json` | Direct Claude Engineer output was typed; candidate Jest passed, overall 5/7, recoverable failure | Second exploratory Engineer-only Case generation | A second independent Case generation improved one gate but remained non-ready. |
| `.../72e9ecc419ba-correction-result.json` | Correction stopped because a Case diagnostic did not bind a prior candidate path | Controller-side historical defect | No second Claude invocation occurred. |
| `.../939cd98e6ae1-correction-result.json` | Correction stopped because exact Wiki excerpts exceeded the bounded content limit | Controller-side historical defect | No second Claude invocation occurred. |
| `.runs/agent-ui/f3110155b560b735a7c1eda8/` | Mule Architect output was schema-valid but rejected by controller policy; no candidate | Fail-closed wrapper-path evidence | No successful Mule migration, local candidate validation, or MUnit claim is supported. |
| `evaluation/platform-validation/salesforce-capstone-dev-qwen38-run-18d5d840.json` | Historical Account/Contact Salesforce check-only validation succeeded for the exact Qwen candidate | Valid historical environment receipt | The exact Qwen candidate passed that scoped org check. The result cannot be transferred to a Claude candidate. |

## Evidence portability

The main runtime evidence locations are ignored from normal publication:

- `.runs/`
- `output/`
- the exploratory Case result/log/runtime directories under `evaluation/model-comparison-20260828/`

Therefore, a final public submission must promote selected sanitized receipts into a tracked evaluation-evidence location. Ignored local state is not sufficient support for a public README or PDF claim.

## Formal evaluation status

- `evaluation/benchmark-v1/registry.json` declares 72 cells, but the cases reuse only two physical fixture roots.
- `evaluation/results.json` contains 72 `not_performed` cells.
- At this baseline, the evaluation code constrained the formal result path to unperformed placeholders rather than measured outcomes. The later benchmark-v2 checkpoints below replace that implementation boundary without rewriting the historical v1 artifacts.
- Existing dependency label sets are not expert-reviewed and cannot clear the dependency-recall exit gate.
- The model comparison is useful exploratory provider-selection evidence, not a cross-platform, multi-agent benchmark.
- Mule runtime authority is disabled, so Maven/MUnit is currently unavailable.

At the baseline, the required replacement was a measured, receipt-derived benchmark. Benchmark v2 now implements that execution and aggregation boundary; live cells, independent label review, human rubrics, and tracked results remain outstanding.

## Baseline gaps, in priority order

1. Capture a fresh normal UI Account/Contact run with truthful first-class Claude role records.
2. Exercise the Case path after the controller-side correction fixes, then capture a fresh normal UI Case run.
3. Capture a reproducible Mule candidate/static-validation run or keep the runtime layer explicitly unavailable.
4. Execute the predeclared benchmark-v2 corpus with reviewed labels, repeated Wiki/no-Wiki runs, independent human rubrics, and derived metrics.
5. Promote sanitized immutable run receipts into tracked evaluation evidence.
6. Update README and course-template artifacts only after the evidence is frozen.
7. Run the complete final quality and browser E2E gates.

## Post-baseline controller repairs

The first implementation checkpoint after `cd078321...` made four bounded corrections:

1. `AgentUiService` now chooses the Claude adapter's provider-specific default timeout instead of inheriting the Ollama default when no explicit service timeout is supplied.
2. A regression test freezes the exact 15-signal failure set observed in the direct Claude Case results and verifies that:
   - every repair signal binds an approved Case controller, test, HTML, or JavaScript path;
   - all directives remain code-owned and path-bounded;
   - exact Wiki evidence covers every signal within the three-page retrieval bound.
3. Claude readiness now reports the remote model's availability as unmeasured until a successful role call. It still enables the approved workflow after verifying the Claude CLI, authentication, and runtime identity. Local Ollama readiness continues to require an observed installed model.
4. Both VS Code live-Claude launch profiles now invoke the first-class `legacy_migration_agent.cli` path. The historical compatibility scripts remain present but are no longer used by either live profile.

The provider-neutral failure guidance assertion was also updated because the same UI path now supports both Ollama and Claude.

## Verification performed at this checkpoint

| Verification | Result |
| --- | --- |
| Claude adapter, CLI, OpenAI boundary, schema, frontend, and UI-service focused suite after readiness repair | 130 passed |
| Correction and Wiki targeted suite | 117 passed, 82 deselected |
| Exact provider-default and Case correction regressions after formatting | 4 passed |
| Provider-specific readiness and Case correction spot checks | 7 passed |
| VS Code first-class debugger launch contract | 1 passed |
| Ruff lint on the three post-baseline files | Passed |
| Ruff format check on the three post-baseline files | Passed |
| Whitespace error check | Passed |

These results prove the focused controller contracts only. They do not replace a live first-class Claude E2E run, a full repository test suite, Salesforce org validation, Mule runtime validation, or formal benchmark evidence.

## Measured-evaluation implementation checkpoint

The second implementation checkpoint adds the evidence contracts and execution seam needed to replace the historical 72-cell placeholder without rewriting it:

- `legacy_migration_agent.measured_evaluation` defines a strict 3-case × 2-configuration × 3-repetition registry, immutable cell receipts, independent human rubrics, deterministic aggregation, and tamper verification.
- `evaluation/benchmark-v2/` predeclares three genuinely distinct source roots: Mule Customer Status, Salesforce Account/Contact, and Salesforce Case Management.
- The two configurations use the same Claude provider/model, three agent definitions, dependency graph, controller checks, approval boundaries, prompts, and bounded retry policy. They differ only in whether curated Wiki content is supplied.
- The benchmark-only no-Wiki arm persists a digest-bound controller marker, supplies no curated Wiki page content to agents, and retains targeted retry using only controller-derived diagnostic identifiers rather than retrieved guidance. Preflight still verifies the frozen Wiki tree for cross-arm drift.
- Normal UI, CLI, and agent-run starts cannot select the ablation. Benchmark start and incomplete-start recovery require the exact immutable arm binding, and cross-arm replay fails closed.
- Mule remains predeclared as `environment_unavailable` while immutable Maven/MUnit runtime authority is absent. That outcome is not promoted to a runtime pass.

The dependency labels currently cover 66 observed source-graph edges, but all three case labels remain `initial_label_set`. Of those labels, 52 are initially high impact and 14 are supporting-test evidence classified low impact; the Mule case contributes 10 labels (seven production-impact and three supporting MUnit-evidence labels). Reviewer identity and evidence are intentionally absent, so dependency-recall, high-impact-dependency, and seeded-intervention gates remain not evaluated. The 18 model-bearing runs have not been performed, and no benchmark result is claimed.

| Verification | Result |
| --- | --- |
| Measured-evaluation contracts and benchmark-v2 artifact bindings | 18 passed |
| No-Wiki retrieval, parity, correction, run binding, and schema compatibility focused suite | 399 passed |
| Highest-risk no-Wiki parity and retry slice | 5 passed |
| Ruff lint and format checks for the evaluation/no-Wiki change scope | Passed |
| v1 public-schema changes | None |
| Whitespace error check | Passed |

The complex Case risk-seed seam is now implemented. Both Wiki arms receive the same digest-bound, non-authorizing model projection, while the expected disposition and scoring oracle stay controller-side. The persisted Architect expansion receipt distinguishes a complete model-authored intervention from a controller-authored safety stop. Either path terminates as `decision_required` after one Architect call; Engineer, Validator, candidate generation, validation reports, and external actions remain unreachable. Offline test doubles exercise both recognition and omission paths. The seed has not been sent to a live provider, and no v2 benchmark cell is claimed as measured.

Additional focused evidence at this checkpoint:

| Verification | Result |
| --- | --- |
| Benchmark-v2 declaration, binding, inert-risk projection, and artifact checks | 14 passed |
| Complex-risk model evaluation and replay checks | 4 passed |
| Both Wiki-arm terminal workflow checks for the complex risk cell | 2 passed |
| No-Wiki start, recovery, cross-binding, and bounded-retry checks | 5 passed |
| Public schema, agent definition, and full model-agent suite after Architect v10 | 119 passed |
| v1 public-schema changes | None |

These are local contract and offline-double results. They do not count toward the 18 live model-bearing cells or replace independent label and candidate review.

## Benchmark execution-evidence checkpoint

The next provider-free checkpoint closes the gap between a declared benchmark
and editable result JSON:

- a pre-run `BenchmarkExecutionAnchor` binds Git commit/tree and an enumerated
  inventory of declared source, protocol, package, Wiki, agent-definition,
  controller-tooling, and lockfile bytes, plus provider/model configuration and
  the authenticated Claude runtime identity;
- benchmark start fails closed without that anchor or when any bound input
  drifts;
- a verified run-bundle loader reconstructs zero, one, or two actual attempts,
  checks each persisted role-call boundary, and verifies controller tool
  receipts without trusting a status counter;
- the cell-receipt extractor derives disposition, attempts, model/tool usage,
  dependency detections, intervention evidence, and authorization outcomes from
  the verified run, while accepting human judgment only through a separately
  cross-bound rubric; and
- the corpus loader requires exactly 18 unique run/rubric routes, re-extracts
  every receipt, computes the aggregate metrics, and applies the predeclared
  gates.

Both arms verify the frozen Wiki tree during preflight. Only the Wiki arm loads
curated page content for agent use; the no-Wiki arm receives none. No live
anchor, rubric, receipt, aggregate result, or model-quality claim has been
created at this checkpoint.

The anchor's `created_at` is caller-supplied, and no anchor digest has yet been
externally published. This checkpoint therefore does not claim independent
proof that an anchor existed before a live cell.

## Provider-free final-quality checkpoint (2026-08-29)

The complete provider-free repository suite passed from the then-current
forward tree:

| Verification | Result |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -p no:cacheprovider` | 1,527 passed in 497.98 seconds |
| Ruff lint | Passed |
| mypy over the 68-file source package | Passed |
| Public-schema export and compatibility | Passed; v1 unchanged and v2 contains 45 schemas |
| Whitespace error check | Passed |

A real browser was also driven through the production UI and ordinary
controller lifecycle with the offline recorded model double. The Case
Management unit accepted a normal conversational message, showed the launch
and manifest gates, generated an 11-file candidate, ran the real local
Jest/sandbox validation toolchain, passed all seven required checks, displayed
the unified diff, and completed final review as `ready_for_human_review`.
`New chat` remained available, only the exact selected scenario was marked
pressed, and the terminal view stated that no external action was authorized.
The run handle was `a2e6283864f52997bbc22190` and the conversation identifier
was `928f8f39a6fcecaf5ce816e8`.

This browser receipt proves the UI, controller, approval, validation, diff, and
review integration. Because it used a recorded model double, it is not Claude
migration-quality evidence and is excluded from benchmark v2. No source was
sent to a remote provider during this checkpoint.

The same complete suite was first attempted inside a restricted test host and
reported 1 failure, 38 setup errors, 1,479 passes, and 9 skips because that host
denied every UI test's ephemeral `127.0.0.1` socket bind. The unchanged command
then passed outside that socket restriction as reported above. This was an
execution-environment denial, not a product-code failure.

## Post-baseline benchmark-contract hardening

The 1,527-test and 45-schema results above remain truthful frozen evidence for
the tree on which they were captured. They predate the final benchmark contract
changes below and must not be described as a complete current-tree verification
until the root suite and schema checks are rerun.

The current forward tree now implements these additional provider-free
contracts without claiming a benchmark result:

- The 66 dependency labels remain `initial_label_set`. A frozen
  `migration-dependency-impact-v1` policy assigns each label an `impact_basis`;
  52 are initially high impact and 14 are supporting-test evidence classified
  low impact.
- `BenchmarkLabelReviewEvidence` provides the independent, digest-bound review
  artifact. No review artifact or reviewer identity exists yet. A graph or
  label set cannot become `reviewed` without both.
- The Case intervention oracle requires all four typed reasons: destructive
  legacy deletion, sharing-boundary weakening, object/field-security (CRUD/FLS)
  weakening, and broad permission-scope expansion. Six Case cells produce a
  reason-recall denominator of 24; broad categories alone are insufficient.
- `expected_outcome_conformance` is explicitly safe controller
  disposition/evidence conformance, not migration success.
- Mule `environment_unavailable` cells are excluded from first-pass and
  bounded-repair rates. The required `runtime_validation_completion` metric
  remains `not_evaluated` and blocks the result until actual MUnit completion is
  evidenced.
- Cell receipts derive unavailable required command IDs from the final
  controller-owned validation report and bind those IDs to that report's
  digest.
- The current public v2 inventory contains 48 schemas, including
  `BenchmarkLabelReviewEvidence`. It preserves the historical
  `EvaluationVerification` contract and adds the distinct benchmark
  `MeasuredEvaluationVerification` contract; v1 remains unchanged.

None of the 18 live model-bearing cells has been run, all label review is still
outstanding, and no aggregate or human-review result is claimed.

The remaining submission gates are therefore evidence collection rather than
more harness architecture: independent label review; fresh first-class Claude
Account/Contact, Case, and Mule runs; the 18 benchmark cells and human rubrics;
and sanitized tracked live-result receipts. A provider-free Mule
candidate/static receipt now exists separately, with runtime/MUnit still
unavailable. The course-template PDF can truthfully report these items as
outstanding; it must be regenerated if later evidence changes any claim or
metric.

## Post-contract current-tree checkpoint (2026-08-29)

The previously outstanding provider-free/schema rerun is now complete on the
current forward tree:

| Verification | Result |
| --- | --- |
| `uv --cache-dir /private/tmp/capstone-uv-cache run --frozen pytest -q` | 1,573 passed in 466.68 seconds |
| Ruff over the complete repository | Passed |
| mypy over the 68-file source package | Passed |
| Public-schema export and compatibility | Passed; v1 preserved and v2 contains 48 schemas |
| Focused remediated security review | No remaining P0-P2; 100 focused tests passed |
| Python sdist and wheel build | Passed |

The complete test command ran outside the restricted socket sandbox only so
loopback UI transport tests could bind to `127.0.0.1`; it did not invoke Claude
or any external platform. A separate source-free readiness probe verified the
pinned Claude executable and authenticated `bedrock` provider with endpoint,
proxy, custom-CA, and unlisted child-environment values rejected or removed.
No migration source or role prompt was sent.

This closes the stale 1,527-test/45-schema caveat for harness code. It does not
close the remaining evidence gates: all 66 labels are still unreviewed, the 18
live benchmark cells and human rubrics are absent, current first-class Claude
Salesforce/Mule result evidence has not been promoted, MUnit runtime authority
remains unavailable, and no result may imply otherwise. A template-based report
has now been produced from this exact evidence boundary and explicitly records
those limitations.

A read-only inventory of the retained local run contexts found six historical
runs whose `model_id` is `claude-sonnet-5` (five Account/Contact and one Mule),
but every one persists `provider_id=ollama`. Those records predate the
first-class Claude provider binding and cannot be relabeled or promoted as
Claude evidence. No retained Case Management run identifies a first-class
Claude provider. Fresh runs under the current runtime contract are therefore
required rather than reusing the old successful candidates.

### Current-tree real-browser receipt

After the current-tree quality checkpoint, a fresh real-browser Case Management
run exercised the production UI and ordinary controller lifecycle with the
offline recorded model double. Run `b55572a1397dfc7f41404c0d` (conversation
`6f708fb80e3a3c23f7f0c728`) completed on attempt 1 with terminal disposition
`ready_for_human_review`. It generated the exact 11-file additive candidate and
passed all seven required local checks with no failed or unavailable result:

- `salesforce-candidate-contract`
- `salesforce-dependency-closure`
- `salesforce-toolchain-contract`
- `salesforce-jest-sandbox-probe`
- `salesforce-lwc-jest`
- `salesforce-lwc-controller-jest`
- `salesforce-workspace-fingerprint`

The browser then requested independent final review under the declarative local
audit labels `capstone-author` and `independent-reviewer`. Review
`final-review-85a39108baf5af04f120ba50368e0fca` remains
`awaiting_final_review`; no automated acceptance was recorded. The request
explicitly states `reviewer_identity_assurance=declarative_unverified` and
`authority_granted=false`. The terminal UI kept `New chat`, generated-code/diff
inspection, and the review-candidate export controls available. This is
provider-free harness and UI evidence only; it is not independent human
acceptance, a Claude quality result, or benchmark evidence.

### Current-tree provider-free Mule browser receipt

A fresh real-browser run exercised the production UI and ordinary controller
lifecycle for `mulesoft-mule3-to-mule4` using the offline recorded model double.
Run `402797927ff2d147468a124e` (conversation
`c4f6ee7c5b327155c82d3873`) completed on attempt 1 with terminal disposition
`environment_unavailable`. The source snapshot remained
`sha256:f0977cde8766fe3fca4287356ff11331e75cea6605942e0c96890ced9cd028b3`;
the controller built 9 graph nodes and 10 edges and retrieved the two bounded
Mule Wiki pages before expanding the exact six-path manifest.

The recorded Engineer produced all six additive Mule 4 files. The
controller-owned report had three passed checks, no failed checks, and two
unavailable checks:

- `mulesoft-candidate-contract` — passed;
- `mulesoft-dependency-closure` — passed;
- `mulesoft-toolchain-contract` — unavailable because the frozen runtime
  authority is disabled;
- `mulesoft-munit` — unavailable because no verified runtime-owned isolation
  authority exists; and
- `mulesoft-workspace-fingerprint` — passed.

The controller classified the result as `stop_environment`; it opened no retry
and no final-review gate. The candidate-only export is stored under ignored
`output/mulesoft-402797927ff2d147468a124e/attempt-1/` with these bindings:

| Artifact | Digest |
| --- | --- |
| Manifest | `sha256:da9d83e67c9188e6a3716c72d35d9be964b108792473347399d81b4ce666fe8d` |
| Candidate | `sha256:de3a0700b9b1d53dcdaff3f43bf398d96ff55f5f0c7db6ca605074aa583f886e` |
| Change set | `sha256:f45d2b6c8f08e1515717243fe570f21108c35a514413f18fc860642634733fcf` |
| Validation report file | `sha256:c15a0947d37aa271a470935b24c2366d854c534a4e5755004be5b61163602de6` |
| Candidate archive | `sha256:ca0e13207eb0ffc35a5da2aaf857d5272106f22d341e256ddc10e92e64c60030` |
| Export receipt file | `sha256:cc74a6e7be5fdf12ec9a4b423134a03d494f9c12edc652f22d40c594a040c37b` |

This proves the ordinary conversation, approval, graph/Wiki, candidate,
static-check, fail-closed disposition, and export paths. Because the role bytes
came from a recorded test double, it is not live-model or model-quality
evidence and is excluded from benchmark v2. It does not prove Maven dependency
resolution, MUnit behavior, Anypoint compatibility, deployment, or Mule runtime
success.

### Current-tree submission-hygiene receipt

A bounded secret-pattern scan found matches only in five test modules that
deliberately exercise rejection and redaction with synthetic GitHub-token,
AWS-example-key, bearer-token, and private-key markers. No non-test source,
configuration, documentation, fixture, or tracked evidence file matched the
scan. Apache-2.0 is declared by `pyproject.toml`, the complete license is present
in `LICENSE`, attribution is recorded in `ATTRIBUTIONS.md`, and the built wheel
contains its packaged license file. Generated `.runs/` and `output/` trees are
ignored, and no run state, candidate export, dependency installation, virtual
environment, cache, or Python bytecode directory is tracked. `git diff --check`
passed and the forward diff still contains no deletion.

### Current-tree course-template report receipt

The supplied six-page, ten-field course template was completed without
substituting a custom report. The dated artifact is:

`output/pdf/Final Capstone Report Planning - Automated Legacy Migration Agent -
2026-08-29.pdf`

Artifact SHA-256:
`a1c85851cc987912091960d8c9356b3592a222c43e9f90e7b68ff14e6d07cb01`.

All six rendered pages were visually inspected. The ten AcroForm answer values
were read back successfully, normal appearances render as multiline wrapped
text, and the report contains the current 1,573-test/466.68-second checkpoint,
48-schema inventory, 66-label review boundary, 18 planned live cells, and the
pending independent final-review state. It does not say that review was
accepted. It also records the provider-free Mule 3-pass/2-unavailable static
receipt without converting it into a runtime-success claim. Because `output/`
is intentionally ignored, this PDF is a separate course-upload artifact rather
than tracked repository evidence.

## Live submission checkpoint (2026-08-30)

This dated checkpoint supersedes older current-state counts in this receipt
without rewriting their historical evidence. The complete current-tree test
suite passed: **2,065 passed in 543.20 seconds**. Ruff reported all **122**
Python files already formatted and its lint check passed; mypy passed across
**68** source files. The agent-definition check confirmed exactly three valid
reasoning agents: Architect, Engineer, and Validator.

Fresh first-class Claude runs now provide the following bounded evidence:

| Slice | Result | Evidence boundary |
| --- | --- | --- |
| Salesforce Account/Contact | Attempt 1 completed with all 7/7 deterministic checks passed, candidate Jest 9/9, and controller Jest 10/10. | The candidate is awaiting genuine independent final review; no acceptance or Salesforce-org result is implied. |
| Salesforce Case Management | Attempt 1 exposed two controller-Jest failures. A bounded attempt-2 correction changed only the approved LWC HTML and JavaScript paths, then completed with all 7/7 checks passed, candidate Jest 11/11, and controller Jest 19/19. | The corrected candidate is awaiting genuine independent final review; no acceptance or Salesforce-org result is implied. |
| MuleSoft Mule 3 to Mule 4 | Attempt 1 generated the exact six additive files and finished with 3 passed, 0 failed, and 2 unavailable checks. | The terminal disposition is `environment_unavailable`. No Maven, MUnit, Anypoint, deployment, or Mule-runtime success is claimed. |

The benchmark-v2 protocol remains deliberately
`predeclared_not_executed`: 18 cells are registered, and its 65 initial labels
remain unreviewed (51 high-impact and 14 low-impact). These are an
initial label set, not benchmark results or independent-review evidence.

The sanitized machine-readable checkpoint is tracked at
`evaluation/submission-evidence/20260830/submission-receipt.json`. Raw run state
and generated candidates remain in ignored runtime/output locations rather
than being promoted as repository evidence.

The current checkpoint also fills the supplied ten-answer course template at
`output/pdf/Final Capstone Report Planning - Automated Legacy Migration Agent -
2026-08-30 final.pdf`. All 10 fields passed form readback, and all six rendered
A4 pages passed visual inspection. The tracked receipt binds the PDF and its
reviewable answer source by SHA-256 without promoting ignored output artifacts.

The remaining external steps are a genuine independent review of the
Salesforce candidates and benchmark labels, optional exact-candidate Salesforce
org evidence, optional Mule runtime/MUnit evidence in an attested environment,
and the final user-operated interactive recording. None of those pending items
is represented as complete by this checkpoint.
