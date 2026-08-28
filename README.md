# Automated Legacy Migration Agent

[![CI](https://github.com/GunnZx23/Automated-Legacy-Migration-Agent/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/GunnZx23/Automated-Legacy-Migration-Agent/actions/workflows/ci.yml)
[![Python coverage](https://codecov.io/gh/GunnZx23/Automated-Legacy-Migration-Agent/graph/badge.svg?branch=main)](https://codecov.io/gh/GunnZx23/Automated-Legacy-Migration-Agent)
[![Python 3.11-3.13](https://img.shields.io/badge/Python-3.11--3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: Apache-2.0](https://img.shields.io/github/license/GunnZx23/Automated-Legacy-Migration-Agent)](LICENSE)

The Automated Legacy Migration Agent is a local, human-gated capstone reference
implementation that uses a real LLM to turn one bounded legacy source slice
into an isolated, reviewable migration candidate. The reusable orchestration
harness is demonstrated through two fixed synthetic scenarios; it does not
accept an arbitrary repository or claim general-purpose migration support. The
two supported scenarios are:

- Salesforce Visualforce/Apex to an additive Lightning Web Component (LWC)
  and Apex implementation; and
- Mule 3 to a separate Mule 4 application with DataWeave 2 and MUnit.

The primary interface is a conversational browser application backed by local
Ollama and `qwen3.8:latest`. The runtime has exactly three model roles:
Architect, Engineer, and Validator. A deterministic Python/LangGraph
controller coordinates them, but is not a fourth agent.

The controller owns every authority-bearing decision: scenario selection,
source and target versions, paths, the canonical migration request,
dependency analysis, Wiki retrieval, human gates, file application,
validation, retry eligibility, evidence, and terminal disposition. Model prose
is advisory unless it satisfies a typed role contract, and even typed model
output cannot approve work or expand scope.

The agent runtime produces local candidate code and evidence. It never commits,
pushes, opens a pull request, connects to a Salesforce org, calls Anypoint,
deploys, publishes, or claims production readiness. A human may separately run
an explicitly authorized check-only platform validation; the repository records
that evidence without granting the agent external authority.

## Contents

- [Quick start](#quick-start)
- [Using the application](#using-the-application)
- [Architecture and authority](#architecture-and-authority)
- [Controller-owned launch contract](#controller-owned-launch-contract)
- [End-to-end workflow and gates](#end-to-end-workflow-and-gates)
- [Salesforce migration slice](#salesforce-migration-slice)
- [MuleSoft migration slice](#mulesoft-migration-slice)
- [Validation of model-generated code](#validation-of-model-generated-code)
- [Bounded correction attempt](#bounded-correction-attempt)
- [Agentic LLM Wiki and dependency graph](#agentic-llm-wiki-and-dependency-graph)
- [Persistence, evidence, and generated output](#persistence-evidence-and-generated-output)
- [Loopback API](#loopback-api)
- [Logging and troubleshooting](#logging-and-troubleshooting)
- [CLI reference](#cli-reference)
- [Testing and schemas](#testing-and-schemas)
- [Security and limitations](#security-and-limitations)
- [Evaluation status](#evaluation-status)
- [Repository layout](#repository-layout)
- [Submission and license](#submission-and-license)

## Quick start

### Prerequisites

- Python 3.12, as pinned by `.python-version` (the package accepts Python
  3.11 through 3.13);
- [`uv`](https://docs.astral.sh/uv/);
- Node.js 22 and npm for the pinned LWC Jest harness; and
- [Ollama](https://ollama.com/) with the local `qwen3.8:latest` alias.

Install the locked Python development environment and LWC Jest dependencies:

```bash
uv sync --frozen --extra dev
(cd tooling/lwc-jest && npm ci --ignore-scripts)
```

Install or confirm the local model:

```bash
ollama pull qwen3.8:latest
ollama list
```

If Ollama is not already running, start it in another terminal:

```bash
ollama serve
```

Validate the executable agent definitions and launch the application:

```bash
uv run --frozen legacy-migration-agent agents-check --project-root .
uv run --frozen legacy-migration-agent ui \
  --project-root . \
  --ollama-model qwen3.8:latest \
  --ollama-timeout-seconds 600 \
  --open-browser
```

The server binds only to `http://127.0.0.1:8765/`. `--open-browser` opens the
system default browser. Stop the foreground server with **Ctrl+C**.

For VS Code, open **Run and Debug**, choose **Agent UI: VS Code Integrated
Browser**, and press **F5**. The checked-in `.vscode/launch.json` runs the same
Qwen command, waits for the loopback URL, and opens the integrated browser.

## Using the application

1. Choose **Visualforce to Lightning Web Component** or **Mule 3 to Mule 4**.
   The selection identifies a fixed controller-owned scenario; it is not a
   suggestion to the model.
2. Send a normal conversational message. The example buttons fill a complete
   request, but the composer also accepts free-form follow-ups.
3. Continue the Architect conversation as needed. Its public response can ask
   for clarification or summarize how the selected scenario relates to the
   conversation. That summary is advisory and cannot change the migration.
4. When the Architect reports that the conversation is ready, the UI shows the
   inline **Scenario launch gate**. It displays the canonical controller-owned
   request separately from the Architect advisory.
5. Choose **Start migration** at that gate. There is no separate start button
   beside the message composer. Continuing the chat creates a new exchange and
   invalidates the old launch token.
6. Review the Architect plan and the Controller-expanded paths, checks,
   implementation contract, and risks. Approve or reject the exact manifest.
7. On approval, the Engineer generates the candidate in an isolated workspace.
   The UI then shows the agent transcript, harness timeline, validation
   receipts, generated files, and per-file unified diff.
8. If the controller offers a bounded correction, review the exact failed
   signals and separately authorize attempt two. There is no third attempt.
9. When all required deterministic checks pass, request a final review and have
   the designated second human accept, reject, or request changes.
10. Download the candidate ZIP or save the exact persisted candidate under
    `output/` for inspection.

**New chat** remains visible except while a browser request is actively in
flight. Starting a new conversation does not delete, approve, reject, or cancel
an existing run. Refreshing the page recovers verified persisted conversation
or run state.

Messages are limited to 1–2000 characters and each conversation to 12
user/Architect exchanges. The local service allows at most 64 persisted
conversations and 16 nonterminal runs. Role-call timeout defaults to 180
seconds and may be set from 1 to 600 seconds at server startup.

The application exposes public structured outputs and lifecycle events, not
private chain-of-thought. This is deliberate: the useful debugging surface is
what each role received and returned at its contract boundary, what the
controller authorized, which check ran, and why a transition stopped.

## Architecture and authority

### The only three model roles

| Role | Current definition | Model-authored output | Boundary |
|---|---|---|---|
| Architect | `agents/architect.md` (`architect/v8`) | `ArchitectConversationReply` for intake or `ArchitectManifestProposal` for planning | Read-only; cannot choose a scenario, author exact paths/checks, launch, approve, write, execute, or widen scope |
| Engineer | `agents/engineer.md` (`engineer/v23`) | `EngineerModelOutcome`, containing a complete file plan or a decision-required intervention | No shell, network, direct filesystem, approval, Git, deployment, or success-declaration authority |
| Validator | `agents/validator.md` (`validator/v5`) | `ValidatorModelAdvisory` over immutable receipts | Cannot run checks, edit files, report runtime availability, approve, or change the deterministic disposition |

All three definitions declare strict structured output, no private
chain-of-thought, and `native_tools: []`. Their named `structured_actions` are
typed response fields, not provider tool calls.

### Deterministic controller

The controller owns:

- the fixed scenario registry and `MigrationLaunchContract`;
- source snapshots and SHA-256 revision binding;
- platform dependency graphs and curated Wiki retrieval;
- strict Pydantic parsing and policy validation;
- expansion of Architect semantics into exact manifest paths, checks,
  approvals, and implementation constraints;
- human launch, manifest, correction, and final-review gates;
- isolated workspace creation and disk-derived `ChangeSet` evidence;
- allowlisted local commands, normalized receipts, and terminal disposition;
- checkpointing, retry classification, secret scanning, redaction, and
  append-only evidence.

The controller is orchestration code, not an LLM role. It never appears in the
three-agent registry and does not generate migration code.

### Runtime model boundary

The submitted interactive path uses one server-owned
`OllamaStructuredModelClient` with `qwen3.8:latest`:

- the endpoint is fixed to loopback Ollama at `127.0.0.1:11434`;
- the browser cannot select a provider, endpoint, model, credential, source
  path, output path, command, or deployment target;
- temperature is zero, thinking and native tools are disabled, and each role
  receives a strict structural schema;
- the controller validates UTF-8, JSON shape, duplicate keys, body bounds,
  model identity, completion state, usage, and the full Pydantic contract; and
- model inventory is checked before and after generation so alias drift stops
  the call.

Provider refusal, timeout, malformed output, model drift, policy rejection, or
unavailable Ollama fails closed through a sanitized public error boundary.

## Controller-owned launch contract

Free-form English is never parsed to decide launch authority. Selecting a
scenario is the only launch-time choice. The selected scenario deterministically
produces the complete `MigrationLaunchContract`:

| Contract area | Controller-owned fields |
|---|---|
| Identity | `scenario_id`, `platform` |
| Canonical request | `canonical_description`, `target_summary` |
| Source and target | `source_root`, `entry_path`, `target_runtime`, `source_version`, `target_version` |
| Exact scope | `required_source_input_paths`, `approved_output_paths`, `scope_policy_digest` |
| Runtime implementation | `analyzer_version`, `adapter_id` |
| Knowledge boundary | `wiki_as_of`, `wiki_query`, `wiki_max_primary_hits` |

The two supported scenario IDs are:

| Scenario ID | Source | Target |
|---|---|---|
| `salesforce-vf-to-lwc` | `fixtures/salesforce/account-contact-explorer/input` | Additive LWC, sharing-aware Apex, metadata, Apex tests, and LWC Jest tests |
| `mulesoft-mule3-to-mule4` | `fixtures/mulesoft/customer-status-api/input` | Separate Mule 4.9.20/Java 17 application with DataWeave 2 and MUnit |

The controller passes the contract's `canonical_description` verbatim into
`MigrationRequest.target.description`. User messages and the Architect's
`advisory_summary` never enter that field and cannot redirect the platform,
technology, direction, version, source, or output inventory.

Each selected conversation exchange records the scenario, platform, and
launch-contract digest. The browser launch token additionally binds the
conversation ID, exchange number, selected scenario, contract digest, model
revision, and Architect output digest. An immutable launch intent is persisted
before run creation, and a matching launch receipt is persisted afterward.
The run stores the full canonical contract as `evidence/launch-contract.json`;
resume and recovery verify the full contract and digest again. A stale token,
changed conversation, altered contract, duplicate launch, or scenario drift is
rejected before another run can be authorized.

## End-to-end workflow and gates

```text
free-form conversation
        |
        v
Architect advisory (no launch authority)
        |
        v
Scenario launch gate + canonical MigrationLaunchContract
        |
        v
frozen source revision + canonical MigrationRequest
        |
        v
exact digest-bound source + dependency graph + version-filtered Wiki RetrievalTrace
        |
        v
ArchitectManifestProposal (semantic decisions and evidence selections)
        |
        v
Controller-expanded MigrationManifest
        |
        v
human manifest decision ---- reject/modify ----> controlled stop/new plan
        |
      approve
        v
Engineer attempt-one file plan + exact persisted Architect RetrievalTrace/digest
        |
        v
isolated workspace -> disk-derived ChangeSet
        |
        v
controller checks -> ValidationReport -> ValidatorModelAdvisory
        |
        +---- eligible failure -> separate correction RetrievalTrace
                                  + exact human-approved changed-file delta
        |
        v
independent final human review -> local export only
```

There are four distinct human boundaries:

1. **Scenario launch** authorizes creation of one run for the exact selected
   contract.
2. **Manifest decision** approves, rejects, or at the lower-level CLI requests
   modification of one digest-bound plan.
3. **Correction approval** authorizes only one exact second implementation
   attempt against the same manifest and base revision.
4. **Final review** records an independent accept, reject, or request-changes
   decision. It grants no external authority.

Reviewer names are local declarative audit labels, not authenticated accounts
and not additional agents. Final review requires distinct requester and
designated-reviewer labels.

The reasoning strategy is a bounded, sequential, ReAct-inspired workflow:
observe frozen evidence, propose one plan, obtain authority, act in isolation,
validate, and optionally repair once. Tree of Thought is intentionally not used
because speculative branches would make scope, evidence lineage, approval, and
cost harder to audit.

## Salesforce migration slice

The source-only fixture contains a synthetic Visualforce account/contact page,
legacy Apex controller and tests, permission metadata, and Salesforce DX
configuration. The fixed canonical request preserves account selection,
explicit contact loading, loading/empty/safe-error states, stale-response
protection, sharing and field security, plus Apex and LWC Jest coverage.

The controller pins:

- Salesforce API 67.0 for source and target;
- additive coexistence with the legacy Visualforce entry point;
- a concrete `accountContactExplorer` LWC bundle;
- sharing-aware Apex with the fixed `getAccounts()` and
  `getContacts(Id accountId)` interface and user-mode read security;
- exact LWC, Apex, permission-set, and `manifest/package.xml` output paths; and
- accessible behavior hooks for selection, explicit load, results, loading,
  empty, alert, and stale-response behavior.

The approved 11-file output inventory is:

```text
force-app/main/default/classes/AccountContactExplorerController.cls
force-app/main/default/classes/AccountContactExplorerController.cls-meta.xml
force-app/main/default/classes/AccountContactExplorerControllerTest.cls
force-app/main/default/classes/AccountContactExplorerControllerTest.cls-meta.xml
force-app/main/default/lwc/accountContactExplorer/__tests__/accountContactExplorer.test.js
force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.css
force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.html
force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js
force-app/main/default/lwc/accountContactExplorer/accountContactExplorer.js-meta.xml
force-app/main/default/permissionsets/AccountContactExplorerUser.permissionset-meta.xml
manifest/package.xml
```

The model is free to choose internal helpers, state shape, accessible markup,
test titles, inline synthetic test records, and assertion style within the
public behavior and safety contracts. Generated Apex tests are review artifacts
until a separately authorized Salesforce org validation proves that the exact
candidate compiles and runs. The current first-attempt Qwen 3.8 candidate has
that separate check-only evidence; see [Evaluation status](#evaluation-status).

## MuleSoft migration slice

The source-only fixture preserves three synthetic Mule 3 files: application
XML, properties, and legacy MUnit. The fixed scenario creates a separate
six-file Mule 4 application and does not overwrite the Mule 3 source.
Because the small fixture has no runtime descriptor, Mule 3.9.5 is an explicit
scenario assumption rather than a version inferred from those three files.

| Technology | Pinned target |
|---|---:|
| Mule runtime | 4.9.20 LTS |
| Java | 17 |
| DataWeave header | 2.0 |
| MUnit | 3.7.3 |
| Mule Maven Plugin | 4.10.1 |
| HTTP Connector | 1.12.0 |

The approved output inventory is:

```text
mule4/customer-status-api/mule-artifact.json
mule4/customer-status-api/pom.xml
mule4/customer-status-api/src/main/mule/customer-status-api.xml
mule4/customer-status-api/src/main/resources/application.yaml
mule4/customer-status-api/src/main/resources/dw/customer-status-response.dwl
mule4/customer-status-api/src/test/munit/customer-status-api-test.xml
```

The observable contract is `GET /api/customers/{customerId}/status` on
`127.0.0.1:8081` and a response containing the requested `customerId`, status
`ACTIVE`, and source `synthetic-fixture`. Flow organization, DataWeave layout,
property names, and candidate MUnit structure remain model choices within that
contract.

`tooling/mulesoft-runtime/authority.json` is deliberately disabled because no
code-reviewed, immutable Mule validation container has been built and
attested. Static validation still runs, but `mulesoft-munit` remains
unavailable and the Mule slice cannot honestly reach a runtime-pass
disposition in the checked-in configuration.

## Validation of model-generated code

There are no checked-in completed LWC or Mule 4 candidates for the runtime to
copy or compare against. The live fixture tree contains only legacy inputs.
Test-only candidate factories under `tests/` build temporary implementations
to exercise the harness; they are not model context and are not migration
answers.

The Engineer generates both code and candidate-authored tests. The controller
then evaluates that arbitrary output against scope, safety, platform,
toolchain, and observable-behavior contracts. It does **not** compare candidate
bytes, diffs, ASTs, test names, helper names, fixtures, or statement order with
a stored expected implementation.

### Evidence ownership

| Evidence | Purpose | Claim boundary |
|---|---|---|
| Candidate-authored tests | Show that the model's implementation/test pair passes its generated suite | Not independent correctness evidence |
| Static candidate checks | Verify exact paths, parseability, metadata, security, dependencies, public interfaces, and forbidden capabilities | Do not prescribe a private implementation |
| Controller-owned behavior tests | Exercise a fixed public behavior boundary unavailable to the model as output scope | Do not prove external platform deployment |
| Deterministic receipts/report | Record actual terminal commands, exit status, diagnostics, and disposition | Authoritative local result |
| Validator advisory | Critique the immutable evidence bundle | Cannot change the deterministic result |

Salesforce runs require these controller-owned check IDs, in dependency order:

1. `salesforce-candidate-contract`;
2. `salesforce-dependency-closure`;
3. `salesforce-toolchain-contract`;
4. `salesforce-jest-sandbox-probe`;
5. `salesforce-lwc-jest` for every candidate-authored Jest test;
6. `salesforce-lwc-controller-jest` for the independent nine-test behavior
   boundary; and
7. `salesforce-workspace-fingerprint`.

MuleSoft runs require:

1. `mulesoft-candidate-contract`;
2. `mulesoft-dependency-closure`;
3. `mulesoft-toolchain-contract`;
4. `mulesoft-munit`; and
5. `mulesoft-workspace-fingerprint`.

A required failed, unavailable, blocked, missing, or nonterminal check prevents
`ready_for_human_review`. A passing local Salesforce candidate is still not an
Apex compile, org test, deployment, or user-acceptance result.

The nine controller-owned LWC behaviors are account-option rendering, safe
account-load failure, the account-selection gate, explicit contact loading,
loading state, stale-response rejection after an account change, blank-selection
reset, empty results, and safe contact-load failure. They assert observable
behavior without prescribing private helper names, boolean polarity, request
tokens, same-account reload mechanics, or a particular test-source shape.

## Bounded correction attempt

Attempt two is not a fresh migration generation. It is available only when
attempt one has a terminal `recoverable_failure` containing at least one
Engineer-actionable failed check or classified diagnostic.

Before any second Engineer call, the controller freezes an
`EngineerCorrectionAuthority`. This controller-only object contains the full
`CorrectionAttemptEvidence` and the one reproducible
`EngineerCorrectionContext`. The role adapter accepts the authority object,
revalidates the nested prior report/change-set/manifest bindings, reapplies the
prior complete file plan to prove its candidate revision, and passes only the
canonical model-facing context to Qwen. A caller cannot authorize attempt two
by constructing or modifying a bare model context.

The correction boundary:

1. freezes the approved manifest, prior complete file plan, prior disk-derived
   change set, validation report, and correction-request digests;
2. removes controller environment/integrity failures from the Engineer repair
   set;
3. maps each actionable diagnostic to an exact repair signal, directive, and
   allowed subset of already approved paths;
4. retains the exact attempt-one Architect `RetrievalTrace` and digest as
   baseline planning provenance, then performs a separate correction-specific
   Wiki retrieval for the exact signal IDs and requires complete, unambiguous,
   version-valid per-signal coverage;
5. requires a separate human approval bound to the exact report, manifest,
   base revision, correction ID, and authorized attempt number;
6. gives the Engineer the complete prior candidate, bounded public summaries of
   the failed checks, and the targeted repair contract; a candidate-Jest-only
   failure can authorize only the generated Jest file, while component changes
   require an independent controller-owned behavior diagnostic; and
7. accepts only a nonempty delta containing complete content for files that
   actually changed.

The controller overlays that delta on the immutable prior plan, preserves every
unchanged file byte-for-byte, applies the combined candidate in a fresh
isolated workspace, and reruns validation. Missing or stale Wiki guidance,
unknown diagnostics, unauthorized paths, unchanged resubmissions, scope
expansion, an environment-only failure, or a third attempt fails closed.

## Agentic LLM Wiki and dependency graph

The project uses a curated Agentic LLM Wiki instead of unrestricted vector
RAG. The corpus is small, version-sensitive, and safety-sensitive, so exact,
reviewable retrieval is more useful than opaque similarity search.

`knowledge/wiki/` contains Salesforce, MuleSoft, validation, safety, and
correction guidance. `catalog.json` assigns stable page IDs, platform/runtime
scope, review state, source authority, related pages, and content digests.
Retrieval verifies catalog/page/index integrity, filters by platform, source
version, target version, and date, performs deterministic ranking, follows only
curated links, and records the selected pages in `RetrievalTrace`.

The dependency graph remains the source of repository facts:

- Salesforce analysis covers Visualforce/Apex references, tests, permission
  metadata, schema references, and dynamic constructs.
- MuleSoft analysis covers flows, subflows, configuration, property
  references, DataWeave, connector/API relationships, and MUnit.

The Architect receives the exact bounded UTF-8 source inputs from the same
immutable snapshot, plus the graph and Wiki trace. Every source file carries a
SHA-256 digest, and its paths must exactly match the controller-owned scenario
policy. The graph remains bound to those source bytes. The controller persists
the exact Architect trace and digest and passes them directly to the Engineer
on attempt one. Attempt two retains that baseline provenance and adds a separate
targeted correction trace; it does not replace the planning trace. Wiki guidance
can explain how a discovered relationship should be migrated, but cannot invent
a repository dependency or erase an unresolved edge. A model cannot promote its
own output into trusted Wiki content.

## Persistence, evidence, and generated output

The UI uses two separate durable trees under the ignored `.runs/agent-ui/`
directory:

```text
.runs/agent-ui/
├── conversations/<conversation-id>/
│   ├── header.json
│   ├── exchange-0001.json ...
│   ├── launch-intent.json          # only after Start migration begins
│   └── launch.json                 # only after the bound run is created
└── <run-handle>/
    ├── evidence/                   # portable, sanitized, digest-indexed JSON
    ├── state/                      # SQLite checkpoint, anchors, in-flight leases
    ├── workspaces/                 # isolated attempt workspaces
    └── scratch/                    # bounded runtime scratch data
```

Portable run evidence includes the full launch contract, canonical request,
run configuration/context, the bounded source content and its revision/digests,
graph and Wiki bindings, agent
definition digests, Architect expansion receipt, decisions, model-call
records, Engineer plans, disk-derived change sets, tool receipts, validation
reports, correction evidence, Validator advisory, status projections, and
final-review records when present.

Runtime state and portable evidence are intentionally separate. Private
directories, immutable digest anchors, exact lifecycle indexes, invocation
leases, and checkpoint replay detect source drift, partial writes, duplicate
provider dispatch, foreign evidence, stale approvals, and resume mismatches.
Portable evidence rejects local absolute paths, credentials, remote URLs where
not allowed, and secret-shaped values.

The source fixture remains unchanged. When the user selects **Save candidate
to output/**, the controller materializes the generated delta and a complete
source-plus-candidate project archive as:

```text
output/<platform>-<handle>/attempt-<n>/
├── candidate/<approved generated paths>  # reviewable generated delta only
├── candidate.zip                         # complete source + delta overlay
└── export.json
```

`export.json` binds the generated file inventory, archive kind and file count,
candidate/archive digests, manifest and change-set digests, attempt, and actual
validation disposition. The ZIP overlays the generated files on the exact
source snapshot bound to the run, so it is usable as a complete project without
turning legacy inputs into generated output. Export is idempotent for identical
bytes and never relabels a failed or unavailable candidate as accepted.
`.runs/` and `output/` are ignored by Git and are never used as fixed-scenario
model input.

## Loopback API

The packaged browser uses a same-origin JSON API. `GET /api/config` returns a
server-issued CSRF token; state-changing requests and candidate download must
present it. Host/origin checks, a 16 KiB request limit, duplicate-key rejection,
UTF-8 JSON enforcement, CSP, and no-store response headers protect the
loopback surface.

| Method and route | Exact purpose |
|---|---|
| `GET /api/config` | Return browser-safe model configuration and CSRF token |
| `GET /api/readiness` | Probe sanitized Ollama reachability and selected-model installation without invoking an agent |
| `GET /api/scenarios` | Return public display data and canonical request for the two fixed scenarios |
| `POST /api/conversations` | Create a conversation from `{scenario_id}`; `null` is allowed and creates no run |
| `GET /api/conversations/<id>` | Reload verified public exchanges, readiness, model receipts, and optional launch handle |
| `POST /api/conversations/<id>/messages` | Append `{message, scenario_id}` and invoke only the Architect intake operation |
| `POST /api/conversations/<id>/launch` | Consume `{launch_token}` and create exactly one contract-bound run |
| `GET /api/sessions/latest` | Recover the latest verified run, if one exists |
| `GET /api/sessions/<handle>` | Read a verified run projection |
| `POST /api/sessions/<handle>/decision` | Record `{selection, reviewer, comment}` for the exact manifest gate |
| `POST /api/sessions/<handle>/retry` | Record `{correction_id, reviewer, comment}` and run the exact eligible correction |
| `POST /api/sessions/<handle>/final-review/request` | Bind `{requester, designated_reviewer}` to an eligible candidate |
| `POST /api/sessions/<handle>/final-review/decision` | Record `{selection, reviewer, comment}` for final review |
| `POST /api/sessions/<handle>/export` | Persist the exact candidate under `output/`; body is `{}` |
| `GET /api/sessions/<handle>/candidate.zip` | Download the verified complete source-plus-candidate project archive |

A migration run can be created only by consuming a ready conversation's exact
launch token. The browser has no arbitrary-prompt run-creation route.

## Logging and troubleshooting

The UI writes lifecycle logs to stderr by default. Capture a local diagnostic
log without adding it to the repository:

```bash
uv run --frozen legacy-migration-agent ui \
  --project-root . \
  --ollama-model qwen3.8:latest \
  --ollama-timeout-seconds 600 \
  --open-browser 2>&1 | tee /tmp/legacy-migration-agent-ui.log
```

Useful events include:

- `ui.server.starting`, `ui.server.ready`, and `ui.server.stopped`;
- `ui.provider.readiness.*` and `ollama.inventory.*`;
- `ui.conversation.model.*` and `ui.conversation.launch.*`;
- `model.call.*` with the exact role and output contract;
- `workflow.operation.*`;
- `engineer.input.prepared`, `engineer.output.received`, and correction-signal
  events;
- `validation.started`, one `validation.check.completed` per check,
  prerequisite-blocked diagnostics, and `validation.completed`; and
- manifest, correction, final-review, and export events.

Logs intentionally omit prompts, generated source, diffs, private model
reasoning, credentials, raw provider bodies, local paths, and unredacted
exceptions. Failure events expose stable fields such as phase, seam, category,
reason code, check ID, and retry eligibility. Controller policy rejections also
include fixed `failure_summary` and `failure_guidance` fields; the UI displays
the same reason code, explanation, and next step without exposing raw model
output or exception text.

### Ollama is not connected

```bash
ollama list
ollama show qwen3.8:latest
curl -s http://127.0.0.1:11434/api/tags
```

If those fail, start `ollama serve` and relaunch the UI. An empty `ollama ps`
means no model is currently loaded for an active request; it does not prove the
model is uninstalled. `ollama list` and `/api/readiness` are the relevant
checks.

### Port 8765 is already in use

Stop the foreground server with **Ctrl+C**. For a detached process, identify it
before terminating it:

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
kill <PID>
```

Alternatively launch with another unprivileged port, for example `--port
8766`.

### A run stopped

Use the UI's harness timeline and candidate-check cards together with terminal
events. `failed` means the controller stopped at a sanitized boundary;
`recoverable_failure` can offer one targeted correction;
`environment_unavailable` means required tooling could not run and does not
consume an Engineer retry; `decision_required` means human scope or evidence is
needed. Never treat a blocked or unavailable prerequisite as a test pass.

### The UI reports its active-run limit

The UI admits at most 16 concurrent or unverified run directories. A verified
terminal history (`completed`, `rejected`, `decision_required`, or controlled
`failed`) does not consume that allowance, even after the checked-in source,
scenario contract, or agent prompts evolve. Capacity classification verifies
the historical run's own stored request, lifecycle index, runtime anchor, and
terminal projection; normal run inspection, resume, and retry still require the
current source and agent definitions to match. Malformed, corrupt, or genuinely
nonterminal histories continue to count toward the limit.

## CLI reference

The UI is the normal Qwen execution path. The CLI exposes provider-free
inspection, artifact creation, exact-thread lifecycle controls, evaluation,
and the lower-level gated run contracts.

| Command | Purpose |
|---|---|
| `agents-check` | Validate exactly three executable definitions and print their digests/contracts |
| `wiki-search` | Run deterministic Wiki retrieval |
| `validate-manifest` | Validate request and manifest JSON |
| `agent-request-create` | Create a canonical source-revision-bound request from `--scenario-id` |
| `agent-run-start`, `agent-run-resume`, `agent-run-retry` | Lower-level gated run lifecycle |
| `agent-run-status` | Read an exact run/thread without invoking a model |
| `agent-manifest-decision-create` | Create an exact approve/reject/modify artifact for a pending manifest |
| `agent-correction-approval-create` | Create an exact attempt-two approval for an offered correction |
| `final-review-request`, `final-review-decide`, `final-review-status` | Provider-free independent final-review lifecycle |
| `graph-evaluate` | Compare a revision-bound graph with bounded labels |
| `evaluation-verify` | Verify the formal benchmark declaration/results |
| `evaluation-pilot-run-local`, `evaluation-pilot-verify`, `evaluation-pilot-ingest-agent-run` | Maintain the two-cell pilot without fabricating model evidence |
| `export-schemas` | Intentionally refresh the current versioned JSON Schema release |
| `ui` | Start the loopback conversational application with required `--ollama-model` |

Create a provider-free request for the Salesforce scenario:

```bash
uv run --frozen legacy-migration-agent agent-request-create \
  --project-root . \
  --request-id request-salesforce-1 \
  --scenario-id salesforce-vf-to-lwc \
  --requested-at 2026-08-27T12:00:00+00:00 \
  --output .runs/requests/salesforce-1.json
```

For MuleSoft, use `--scenario-id mulesoft-mule3-to-mule4`. There are no CLI
arguments for an arbitrary platform, source root, target description, Wiki
cutoff, path inventory, adapter, or runtime version; those values come from the
selected launch contract.

Inspect exact current arguments before using a lower-level lifecycle command:

```bash
uv run --frozen legacy-migration-agent --help
uv run --frozen legacy-migration-agent agent-run-start --help
uv run --frozen legacy-migration-agent agent-run-resume --help
uv run --frozen legacy-migration-agent agent-run-retry --help
```

Status, decision creation, final review, schema export, Wiki search, and
evaluation verification do not invoke Qwen by themselves.

## Testing and schemas

Run the provider-free local quality gates:

```bash
uv lock --check
uv sync --frozen --extra dev
(cd tooling/lwc-jest && npm ci --ignore-scripts)
uv run --frozen ruff format --check src tests
uv run --frozen ruff check src tests
uv run --frozen mypy
uv run --frozen pytest
uv run --frozen legacy-migration-agent agents-check --project-root .
uv run --frozen legacy-migration-agent evaluation-verify \
  --registry evaluation/benchmark-v1/registry.json \
  --results evaluation/results.json
uv run --frozen legacy-migration-agent evaluation-pilot-verify \
  --project-root . \
  --registry evaluation/pilot-v1/registry.json \
  --snapshot-dir evaluation/pilot-v1
```

Generate the same branch-coverage report uploaded by CI:

```bash
uv run --frozen pytest \
  --cov=legacy_migration_agent \
  --cov-branch \
  --cov-report=term-missing \
  --cov-report=xml:coverage.xml
```

The CI and coverage badges at the top of this README are live repository
metrics. Python coverage measures the controller and harness under
`src/legacy_migration_agent`; it does not treat generated Apex, LWC, Jest,
Mule, or MUnit output as Python coverage. Migration quality remains represented
by the separate benchmark and platform-validation evidence below.

The test suite uses model doubles and temporary candidates to exercise
conversation binding, launch drift rejection, role contracts, graph/Wiki
retrieval, human gates, isolated workspaces, arbitrary generated candidate
validation, correction deltas, checkpoint recovery, evidence integrity,
security, CLI, and UI transport. These tests establish harness behavior, not
Qwen migration quality or external platform success.

`schemas/v1.0/` is the frozen 52-file legacy public-contract release.
`schemas/v2.0/` is the current 39-file public-contract release and includes
`ValidatorModelAdvisory`. The compatibility test protects both inventories and
requires exact current v2 schema bytes:

```bash
uv run --frozen pytest tests/test_schema_compatibility.py
```

After an intentional, reviewed current-contract change, refresh only v2 and
rerun the compatibility test:

```bash
uv run --frozen legacy-migration-agent export-schemas --output-dir schemas/v2.0
uv run --frozen pytest tests/test_schema_compatibility.py
```

Do not overwrite the published v1 baseline. A sandbox that forbids binding a
loopback TCP socket can block `tests/test_ui_server.py` with a permission error;
that environment restriction must be separated from an application assertion
failure.

## Security and limitations

| Threat or ambiguity | Controller response |
|---|---|
| Prompt injection in source, Wiki, chat, or prior output | Treat all content as untrusted evidence; fixed role prompts, strict schemas, and no native tools |
| English prose redirects the migration | Ignore prose for authority; derive launch only from the selected canonical contract |
| Traversal, symlink, or scope escape | Canonical relative paths, exact-file policy, private isolated workspaces, and disk-derived change-set comparison |
| Generated command injection | Controller-owned argv with `shell=False`; model text never becomes a command |
| Secret disclosure | Request lexer, high-confidence content scanning, filtered environment, persistence gates, and redaction |
| Source mutation | Read-only fixture selection plus before/after source fingerprints |
| Model claims its own success | Deterministic receipts and report remain authoritative; Validator is advisory |
| Replay or duplicate dispatch | Digest-bound gates, immutable launch intent/receipt, operation leases, anchors, and checkpoints |
| Model or alias substitution | Server-owned Qwen alias and pre/post Ollama inventory binding |
| Missing dependency | Source-bound platform graph, unresolved-edge checks, and validation closure |
| Golden-answer leakage | Source-only fixtures; expected/golden/oracle paths are rejected from runtime source/run routes |
| Fake correction | Exact failed diagnostics, bounded failed-test titles, targeted Wiki evidence, allowed-path delta, one approval, and two-attempt maximum |
| Browser request forgery | Loopback bind, Host/Origin validation, CSRF token, CSP, bounded strict JSON |

Known limitations:

- only two small synthetic migrations are supported;
- static analyzers cannot prove every dynamic or external dependency;
- the local workspace/sandbox is an application-level control, not a hardened
  hostile multi-user container boundary;
- local reviewer labels are not authenticated identities;
- one exact Salesforce candidate has passed a separately authorized Developer
  Edition check-only validation, but the agent does not perform org operations;
- Mule runtime/MUnit execution is disabled pending an attested runtime;
- Qwen can still produce plausible but incomplete code, which is why
  deterministic checks and final human review are mandatory; and
- no deployment, Git publication, production integration, or user acceptance
  is performed by an agent run.

See [`SECURITY.md`](SECURITY.md) for vulnerability reporting. Never put
credentials, proprietary source, personal data, or raw exploit payloads in a
public issue or submission artifact.

## Evaluation status

The repository separates harness tests, one-run pilot evidence, external
platform evidence, and a statistically meaningful benchmark.

- `evaluation/benchmark-v1/registry.json` predeclares six cases, four
  treatments, and three repetitions: 72 cells. Every cell in
  `evaluation/results.json` is currently `not_performed`.
- `evaluation/pilot-v1/` is the immutable zero-measurement pilot baseline.
  `evaluation/pilot-v1-salesforce-qwen38-20260827/` is its verified successor:
  the Salesforce cell succeeded and the MuleSoft cell remains
  `not_performed`.
- Repository tests, temporary synthetic candidates, and model doubles are not
  ingested as migration-quality results.
- The measured Salesforce pilot run used `qwen3.8:latest`, completed on attempt
  one, invoked exactly Architect, Engineer, and Validator once each, consumed
  45,629 recorded tokens, and accumulated 754,808 ms of model-call latency. It
  reached `ready_for_human_review` with all 7 required local checks passing.
- The exported project from that exact run then passed a separately authorized
  Salesforce Developer Edition check-only deployment: job
  `0Afak00000ifJ71CAE`, 7/7 metadata components, 7/7 specified Apex tests, and
  zero failures. The generated Jest file was correctly excluded from Metadata
  API and remained part of local Jest validation. The durable receipt is
  `evaluation/platform-validation/salesforce-capstone-dev-qwen38-run-18d5d840.json`.
- No Mule runtime result, deployment, human acceptance, or statistically
  controlled latency/token comparison is claimed.

### Exploratory local model comparison

These observations are model-selection evidence, not benchmark results. The
older Qwen3-Coder run used an earlier harness, so its counts are not directly
comparable with the current Qwen 3.8 run.

| Model | Salesforce slice result | Interpretation |
|---|---|---|
| `qwen3.8:latest` | Current harness: 7/7 controller checks, 10/10 candidate-authored Jest tests, 9/9 independent controller Jest tests, and the exact project passed the 7-component/7-test Salesforce check-only validation on attempt one | Current default because it has the strongest current end-to-end and platform evidence. |
| `qwen3-coder:30b` | Historical earlier-harness run: 4/7 checks; candidate Jest executed 0 tests and the controller suite failed 8/9 tests | Retained only as exploratory history; it was not rerun because the current Qwen 3.8 result already satisfies the capstone demonstration goal. |

An earlier Qwen 3.8 reproduction exposed an under-assertion in the independent
controller suite: it checked that failed contact loading left no populated
rows, but not that the `contact-results` rendering hook was absent. The
controller test and Wiki contract now require that absence in loading, empty,
guidance, stale-response, and controlled-error states. The successful measured
run above used the strengthened harness.

A pilot cell becomes measured only when an actual terminal Qwen run is
explicitly ingested with its bound source, contract, definitions, graph, Wiki,
decisions, model records, candidate, receipts, and terminal status. External
platform success still requires separate terminal platform evidence.

## Repository layout

| Path | Purpose |
|---|---|
| `agents/` | The three executable Markdown role definitions |
| `src/legacy_migration_agent/application/` | Scenario contracts, conversation/run lifecycle, export, and final review |
| `src/legacy_migration_agent/agent_runtime/` | Role adapters, model workflow, Ollama client, correction, and checkpoints |
| `src/legacy_migration_agent/core/` | Integrity, policy, scope, workspace, execution, redaction, observability, and session storage |
| `src/legacy_migration_agent/graphs/` | Salesforce/MuleSoft dependency graph construction, storage, and evaluation |
| `src/legacy_migration_agent/knowledge/` | Curated Wiki loader and deterministic retrieval |
| `src/legacy_migration_agent/platforms/` | Salesforce and MuleSoft scope, checks, runtime adapters, and normalized evidence |
| `src/legacy_migration_agent/ui/` | Loopback HTTP server, application service, and browser assets |
| `src/legacy_migration_agent/cli.py` | CLI parser and controlled lifecycle commands |
| `fixtures/` | Source-only synthetic Salesforce and Mule 3 inputs plus scenario metadata |
| `knowledge/wiki/` | Runtime Wiki catalog, index, and curated pages |
| `tooling/lwc-jest/` | Pinned independent LWC Jest toolchain and controller behavior tests |
| `tooling/mulesoft-runtime/` | Disabled runtime authority and future controller behavior contract |
| `schemas/v1.0/`, `schemas/v2.0/` | Frozen legacy and current public JSON Schema releases |
| `evaluation/` | Graph labels, formal benchmark declaration, pilot declaration, and honest current results |
| `tests/` | Harness, security, domain, model-double, CLI, and UI tests; temporary candidate factories live here |
| `docs/diagrams/` | Mermaid source and rendered supporting architecture asset |
| `.runs/` | Ignored private conversations, checkpoints, workspaces, and run evidence |
| `output/` | Ignored attempt-specific candidate exports |
| `SECURITY.md`, `ATTRIBUTIONS.md`, `LICENSE` | Governance, notices, and Apache 2.0 license |

`.venv/`, `node_modules/`, caches, `.runs/`, and `output/` are generated local
state, not submission source.

## Submission and license

Submission repository:
[github.com/GunnZx23/Automated-Legacy-Migration-Agent](https://github.com/GunnZx23/Automated-Legacy-Migration-Agent)

Before publication, run the complete provider-free local gates above, record
the separate browser/Qwen evidence honestly, inspect `git status`, and confirm that
`.runs/`, `output/`, `.env*`, credentials, model weights, `.venv/`, and
`node_modules/` are absent from the commit. A Git commit, push, or pull request
is a separate human-owned action and is never authorized by a migration run or
final-review acceptance.

Original source, documentation, diagrams, tests, and synthetic fixtures are
released under the [Apache License 2.0](LICENSE). See
[`ATTRIBUTIONS.md`](ATTRIBUTIONS.md) for third-party notices. Ollama and Qwen
weights are operator-installed and are not bundled or redistributed by this
repository; the operator remains responsible for their upstream licenses.
