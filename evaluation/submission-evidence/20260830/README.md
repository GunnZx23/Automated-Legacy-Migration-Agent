# Submission evidence checkpoint — 2026-08-30

This directory is the tracked, sanitized receipt for the final capstone tree. The
machine-readable source is [`submission-receipt.json`](submission-receipt.json).
Raw `.runs/` and `output/` content stays intentionally untracked; the receipt
binds those local artifacts by SHA-256 without publishing provider transcripts,
temporary workspaces, or disposable runtime state.

## Current-tree gates

| Gate | Result |
| --- | --- |
| Pytest | 2,111 passed in 604.41 seconds; zero failures or skips with ephemeral loopback binding authorized |
| Ruff format | 126 source/test Python files already formatted |
| Ruff lint | Passed |
| mypy | 70 source files, no issues |
| Agent registry | Exactly three valid agents |
| Package build | Source distribution and wheel built |
| Git diff integrity | Passed |

The pinned LWC Jest install completed. `npm audit` reports 61 low-severity
findings in the local developer/test dependency tree, with zero moderate, high,
or critical findings. The direct pinned package is
`@salesforce/sfdx-lwc-jest@7.9.0`, and npm reports no direct fix. This is
recorded as a toolchain limitation, not silently rewritten with an incompatible
dependency update.

## Publication hygiene

The intended publishable tree contains no tracked `.runs/`, `output/`, `.env*`,
virtual-environment, `node_modules`, or Playwright-session state. The final
source distribution and wheel contain zero such paths. Credential-pattern and
host-path scans found no matches outside deliberately synthetic
redaction/fail-closed tests, 22 relative Markdown links resolved, and the
deletion audit found zero deleted worktree paths. User-specific paths in the
planning receipt and browser runbook were replaced with portable descriptions.
The receipt does not embed the source-distribution digest because the receipt
is itself included in that archive; doing so would create a circular hash. The
wheel digest remains noncircular and recorded.
No commit, push, deployment, or publication action was performed.

## Final course-template report

The supplied ten-answer template was filled as a six-page A4 AcroForm at
`output/pdf/Final Capstone Report Planning - Automated Legacy Migration Agent -
2026-08-30 interactive-final.pdf`. All 10 canonical fields and widget values
passed programmatic readback, every widget has a non-empty appearance stream,
and all six rendered pages passed visual inspection with complete multiline
answers. The PDF SHA-256 is
`9d2bafed494d11c2b2a65a63ab54c7d4d4be354373e3cc0883f62bd384a10e72`.
Its exact answer source is
[`final-report-answers-interactive.json`](final-report-answers-interactive.json),
SHA-256
`b3e4df8d4ed9b6c7a43b51b9951f2199fd1effa08d75caee55966ccb73b80a18`.
The PDF remains an ignored course-upload artifact; this tracked receipt binds
it without publishing the raw run state or earlier render iterations.

The preserved `2026-08-30 final.pdf` is superseded and must not be submitted:
although its canonical field values read back, several widget appearances
displayed only the first line. The `interactive-final.pdf` artifact fixes that
presentation defect without changing the source template.

The remaining operator steps are collected in
[`FINAL_HANDOFF.md`](FINAL_HANDOFF.md). The completed browser-operated Case run
is bound separately in
[`FINAL_INTERACTIVE_RUN.md`](FINAL_INTERACTIVE_RUN.md). The handoff identifies
the exact Case prompt, the reviewable candidate archives and hashes, the
already-bound final-review requests, BW's separate Case and complete 18-cell
review evidence, without treating an unauthenticated UI click as reviewer
identity.

## Fresh Claude product-path evidence

| Slice | Attempt | Generated files | Authoritative result | Test/check detail |
| --- | ---: | ---: | --- | --- |
| Salesforce Account/Contact | 1 | 11 | `ready_for_human_review` | 7/7 checks; candidate Jest 9/9; controller Jest 10/10 |
| Salesforce Case Management recovery evidence | 2 | 11 | `ready_for_human_review` | Attempt 1 found 2 of 19 controller behaviors failing; attempt 2 changed only HTML and JavaScript, then 7/7 checks, candidate Jest 11/11, controller Jest 19/19 |
| Salesforce Case Management final interactive run | 1 | 11 | `ready_for_human_review` | 7/7 checks on attempt 1; candidate Jest 7/7; controller Jest 19/19; no retry |
| Mule customer status | 1 | 6 | `environment_unavailable` | 3 static/controller checks passed; pinned toolchain and MUnit execution remained unavailable; no retry was opened |

These are ordinary UI product runs, not benchmark-v2 cells. BW independently
accepted the final interactive Case candidate through the separate additive
attestation; the two older retained Salesforce candidates still lack genuine
independent-review evidence. The in-run declarative `accept` click remains
identity-unverified and `authority_granted=false`; BW's later evidence is the
review claim. No fresh Claude org compilation, deployment, publication,
Maven/MUnit execution, Mule startup, or Anypoint action is claimed.

## Benchmark-v2 boundary

The predeclared comparison is 3 cases × 2 configurations × 3 repetitions = 18
cells. BW independently accepted the frozen 65-label subject (51 high impact
and 14 low impact) and reviewed the complete corrected 18-cell campaign,
including both attempts where present. The corrected raw archive SHA-256 is
`f6a2e2ac0672a7631c0b6331e41a896574933c8704e2eb7707222ee5eeae1336`; the
review-packet SHA-256 is
`425fadd39e12b62226041f1a0bb8d95e100c1dd1ae5fc1846ec8b736e4232bae`.
All 18 cells were accepted and semantically conformant, with no escaped defects.
The aggregate records 390/390 dependency recall, zero missed high-impact
dependencies, zero authorization violations, and 13/18 expected-outcome
conformance. Its quality gate remains false because expected-outcome
conformance missed its threshold and Wiki attribution and Mule runtime evidence
are unavailable. The historical campaign predates `GraphAssuranceReport`; it is
evaluation evidence, not proof of the current assurance runtime or Wiki benefit.

## Reproduction commands

```bash
uv lock --check
uv sync --frozen --extra dev
(cd tooling/lwc-jest && npm ci --ignore-scripts)
uv run --frozen ruff format --check src tests
uv run --frozen ruff check src tests
uv run --frozen mypy
uv run --frozen pytest
uv run --frozen legacy-migration-agent agents-check --project-root .
uv build
git diff --check
```

The UI-server tests bind ephemeral loopback ports. A host or sandbox that denies
all socket binding must grant loopback authority for those 45 tests; that is an
environment boundary, not a reason to skip or weaken the tests.
