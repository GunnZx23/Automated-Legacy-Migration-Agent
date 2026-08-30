# Final capstone handoff

This handoff separates the final user-operated demonstration from genuine
independent review. BW, the project author's Manager, independently accepted
the final interactive Case candidate, the frozen benchmark label packet, and
all 18 corrected benchmark cells. The Account/Contact and recovery Case
candidates remain optional separate review items. Operational migration gates may use the project author's
identity, but reviewer judgments must come from the person who actually
performed each review; never synthesize or reuse the author as that reviewer.

## Final user-operated interactive run

From the repository root:

```bash
uv sync --frozen --extra dev
(cd tooling/lwc-jest && npm ci --ignore-scripts)
claude auth status --json
uv run --frozen legacy-migration-agent agents-check --project-root .
uv run --frozen legacy-migration-agent ui \
  --project-root . \
  --claude-model claude-sonnet-5 \
  --claude-timeout-seconds 900 \
  --approved-by capstone-author \
  --approved-remote-provider bedrock \
  --allow-live-api \
  --allow-prompt-data-sharing \
  --open-browser
```

For the strongest recording, choose **Case Management Console** and use:

> Migrate the bounded Visualforce case management console
> (LegacyCaseManagementConsole.page,
> LegacyCaseManagementConsoleController.cls and LegacyCaseQueryService.cls) to
> an additive Lightning Web Component and Apex implementation. Preserve account
> selection, a status filter defaulting to Open, an explicit case-loading action
> with OPEN, CLOSED, and ALL behavior, keyed case results, initial selection
> guidance, visible loading, empty, and safe-error states, clear prior case
> state and invalidate pending work whenever the account or status changes,
> reset selection and Case state and invalidate pending work if the Account wire
> transitions to error, stale-response protection, an explicit clear action,
> sharing and field-security controls, and include Apex and LWC Jest tests.

Record these product surfaces:

1. The conversational Architect exchange and the separate canonical launch
   contract.
2. The dependency graph and version-filtered Wiki retrieval trace.
3. The exact 11-path manifest and the author-operated manifest gate.
4. Architect, Engineer, Validator, and controller timeline entries; the
   controller is orchestration, not a fourth LLM agent.
5. Candidate-authored tests, independent controller checks, authoritative
   disposition, generated files, and unified diff.
6. If an evidence-directed second attempt is offered, show its failed signal
   IDs, targeted Wiki trace, and exact changed-file boundary before approving
   it. Do not open a blind retry or a third attempt.
7. Candidate ZIP/export and the final-review request. Stop at
   `awaiting_final_review` unless the real independent reviewer is present.

Stop the foreground server with **Ctrl+C**. Do not describe local checks as
Salesforce deployment, Mule runtime proof, or benchmark-v2 evidence.

### Completed final interactive checkpoint

The user-operated Case run completed on 2026-08-30 as handle
`4c46bd073373f561881612ae`: attempt 1, exactly 11 generated paths, 7/7 checks,
candidate-authored Jest 7/7, controller-owned Jest 19/19, and disposition
`ready_for_human_review`. Its exported archive SHA-256 is
`f21af9b4e85018498bc9b38ec4109f7be10cc6b101c66867b15c7f0193b5f1bc`.
See [`FINAL_INTERACTIVE_RUN.md`](FINAL_INTERACTIVE_RUN.md) for the complete
sanitized receipt. A later declarative `accept` click retained
`authority_granted=false` and does not count by itself as genuine independent
review. BW subsequently supplied a separate additive attestation accepting this
exact candidate, diff, and test evidence. The bound artifact is
[`external-case-candidate-review-bw.json`](external-case-candidate-review-bw.json),
canonical digest
`sha256:f48e72ec4b295fd15b1765ccdf1500d0106a20fa84cd81644f461657edaca262`.

## Independent Salesforce candidate review

The current fresh Claude candidate-review state is:

| Slice | Candidate archive | SHA-256 | Result |
| --- | --- | --- | --- |
| Account/Contact | `output/salesforce-216841a73201436ba836f5b1/attempt-1/candidate.zip` | `296be5df5de759c7059ace67fbeb00f942384c689087f1de82c2844ed6c3406f` | Attempt 1; 7/7 checks, candidate Jest 9/9, controller Jest 10/10 |
| Case Management | `output/salesforce-7e3bc811e390bedd4119898c/attempt-2/candidate.zip` | `cf509dd9b2815d2e90ebd85ae31dc3c1c4d396222962931e9c46bc74cdc0143a` | Attempt 2; 7/7 checks, candidate Jest 11/11, controller Jest 19/19 |
| Final interactive Case | `output/salesforce-4c46bd073373f561881612ae/attempt-1/candidate.zip` | `f21af9b4e85018498bc9b38ec4109f7be10cc6b101c66867b15c7f0193b5f1bc` | BW accepted; attempt 1; 7/7 checks, candidate Jest 7/7, controller Jest 19/19 |

The reviewer should inspect additive scope, sharing and field-security
behavior, the user-visible states and stale-response protections, permission
metadata, generated Apex/Jest coverage, controller-owned check receipts, and
the actual diff. The reviewer should return their real name or ID, relationship
to the project, relevant expertise, timezone-aware review time, decision
(`accept`, `reject`, or `request_changes`), and comment.

The two older requests are bound to the local declarative audit label
`course-reviewer`; that label is not an authenticated identity. If the real
reviewer records a decision from the command line, use the exact bound label and
preserve the person's real-world review details separately:

```bash
uv run --frozen legacy-migration-agent final-review-decide \
  --project-root . \
  --run-dir .runs/agent-ui/216841a73201436ba836f5b1 \
  --run-id ui-run-216841a73201436ba836f5b1 \
  --thread-id ui-thread-216841a73201436ba836f5b1 \
  --reviewer course-reviewer \
  --selection accept \
  --decided-at "YYYY-MM-DDTHH:MM:SSZ" \
  --comment "Reviewer-authored decision comment"
```

```bash
uv run --frozen legacy-migration-agent final-review-decide \
  --project-root . \
  --run-dir .runs/agent-ui/7e3bc811e390bedd4119898c \
  --run-id ui-run-7e3bc811e390bedd4119898c \
  --thread-id ui-thread-7e3bc811e390bedd4119898c \
  --reviewer course-reviewer \
  --selection accept \
  --decided-at "YYYY-MM-DDTHH:MM:SSZ" \
  --comment "Reviewer-authored decision comment"
```

Replace `accept` with the reviewer's actual decision. These requests expire
on 2026-09-13 UTC. Never record acceptance merely to clear a gate.

## Benchmark label and output review

BW completed the independent label review for the frozen subject and the
complete corrected output review:

- review digest
  `sha256:fa76b2b5b9f8a9f4dea0637e7c3a1f6d9cddce6fa08ff7fbc4695631ffd29183`;
- 65 reviewed labels: 51 high impact and 14 low impact; and
- 18 reviewed cells: three cases, Wiki/no-Wiki, three repetitions; all accepted
  and semantically conformant, with no escaped defects.

The bound label evidence is
[`evaluation/benchmark-v2/label-review-evidence.json`](../../benchmark-v2/label-review-evidence.json),
canonical digest
`sha256:b718d6b3c130d1318f27b9911ec223cde650a39b19af39927ab590ccf3aba5c3`.
The corrected campaign raw archive is
`output/benchmark-v2-corrected-campaign-20260830.tar.gz`, SHA-256
`f6a2e2ac0672a7631c0b6331e41a896574933c8704e2eb7707222ee5eeae1336`; the
review packet SHA-256 is
`425fadd39e12b62226041f1a0bb8d95e100c1dd1ae5fc1846ec8b736e4232bae`.
The verified aggregate records 390/390 dependency recall, zero missed
high-impact dependencies, zero authorization violations, zero escaped defects,
and 13/18 expected-outcome conformance. The quality gate remains false because
that conformance missed its threshold and Wiki attribution and Mule runtime
evidence are unavailable. This campaign predates the current Graph Assurance
runtime stage, so it makes no GraphAssurance or Wiki-benefit claim.

## Submission artifact

The final visually verified course-template PDF is:

`output/pdf/Final Capstone Report Planning - Automated Legacy Migration Agent -
2026-08-30 interactive-final.pdf`

SHA-256:
`9d2bafed494d11c2b2a65a63ab54c7d4d4be354373e3cc0883f62bd384a10e72`.
It has six A4 pages, 10/10 populated canonical fields, verified widget values
and appearance streams, and complete multiline answers on every rendered page.
Its answer source includes BW's reviews, the complete measured campaign, and
the final 2,111-test quality gate. Do not submit the preserved
`2026-08-30 final.pdf` either; its field values exist, but its rendered widgets
show only the first line of several answers.
