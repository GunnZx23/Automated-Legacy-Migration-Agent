# Final user-operated interactive run — 2026-08-30

This receipt records the final browser-operated Case Management Console run.
It is product-path evidence, not a benchmark-v2 cell, Salesforce deployment
receipt, production-readiness claim, or substitute for independent review.
Raw `.runs/` and `output/` artifacts remain intentionally ignored; the hashes
below bind this sanitized checkpoint to those local artifacts.

## Interaction and launch

| Field | Value |
| --- | --- |
| Conversation | `b8a8831f1f0053cb90817446` |
| Conversation exchanges | 2 successful Architect exchanges |
| Selected slice | `case-management-console` |
| Run handle | `4c46bd073373f561881612ae` |
| Run ID | `ui-run-4c46bd073373f561881612ae` |
| Thread ID | `ui-thread-4c46bd073373f561881612ae` |
| Provider and model | `claude-cli` / `claude-sonnet-5` |
| Approved remote provider | authenticated Bedrock session |
| Launch requested | `2026-08-30T07:42:40.393847Z` |
| Launch-contract digest | `sha256:b829f1468617350b1f393378b66a33fa18fdf46a0f5ad22d15fef03cb746cd25` |

The first conversational exchange used the Account/Contact slice and the
second switched to the nontrivial Case Management slice. Both completed with
the Architect ready to launch. The operator then launched the Case request and
approved its exact 11-path manifest. No model or controller action widened the
manifest.

## Attempt-1 outcome

The Architect, Engineer, and Validator all completed successfully. The
Engineer returned exactly 11 approved generated paths with no missing or extra
files. The controller completed the run on attempt 1 with disposition
`ready_for_human_review`; no correction request or second attempt was opened.

| Controller-owned check | Result | Evidence summary |
| --- | --- | --- |
| `salesforce-candidate-contract` | Passed | 11 files and 6 required user-visible states |
| `salesforce-dependency-closure` | Passed | 23 nodes, 52 edges, 0 warnings |
| `salesforce-toolchain-contract` | Passed | Pinned `@salesforce/sfdx-lwc-jest@7.9.0` contract |
| `salesforce-jest-sandbox-probe` | Passed | All 9 authority checks passed |
| `salesforce-lwc-jest` | Passed | Candidate-authored suite: 7/7 tests |
| `salesforce-lwc-controller-jest` | Passed | Independent controller suite: 19/19 tests |
| `salesforce-workspace-fingerprint` | Passed | Frozen input workspace remained unchanged |

The authoritative validation report is
`report-salesforce-27e491e09f8b01c14e982745`, completed at
`2026-08-30T07:49:11.239805Z`, with 7 passed, 0 failed, and 0 unavailable
checks. Its canonical evidence digest is
`sha256:659e716ecf65fa5a86e13175eb452348be531a7cc65f10945ecf74599de1ecf0`.

## Candidate export

| Field | Value |
| --- | --- |
| Generated-file count | 11 |
| Candidate digest | `sha256:649476ba710e37077ed0d5a2ff74e1b21a2008e152165c632e59446256929d63` |
| Change-set digest | `sha256:65a155e57d6ea2f993ddd5abe34224474dd311b89bce3bed6129a56a63e0f1b0` |
| Candidate archive | `output/salesforce-4c46bd073373f561881612ae/attempt-1/candidate.zip` |
| Candidate archive SHA-256 | `f21af9b4e85018498bc9b38ec4109f7be10cc6b101c66867b15c7f0193b5f1bc` |
| Archive contents | 21 source-plus-candidate-overlay files |

Raw artifact bindings:

- export receipt SHA-256:
  `7de5af15511e4e7caf65be0451d156a28783f8c893f614bd1ada2179acc167c1`;
- validation report SHA-256:
  `299c0938b1f5ae4543c826bf7521249c36403956c73887db64fbd9526640aef7`;
- final-review request SHA-256:
  `635999732bd4014a49ded5024e28aa7807b7795974f411f9b79f17700b45eff9`;
- conversation launch receipt SHA-256:
  `5857e42121aa8f832e04883090beca049fb319b8958f553f703401226ee8c173`.

## Independent-review boundary

The in-run final-review request was created for the declarative label
`independent-reviewer`, with status `awaiting_final_review`, identity assurance
`declarative_unverified`, and `authority_granted=false`. The UI subsequently
recorded an `accept` selection under that same unauthenticated label with an
empty comment, still with `authority_granted=false`. That click demonstrates
the UI decision surface only and is not independently authenticated.

BW later supplied a separate, additive external attestation after reviewing
this exact generated Case candidate, unified diff, and test evidence. BW's
reviewer-supplied decision was `Approved`. That attestation binds this run ID,
final-review request digest, change-set digest, validation-report digest, and
candidate-archive digest in
[`external-case-candidate-review-bw.json`](external-case-candidate-review-bw.json),
canonical digest
`sha256:f48e72ec4b295fd15b1765ccdf1500d0106a20fa84cd81644f461657edaca262`.
The loader verifies those bindings but does not authenticate BW's identity or
timestamp. The attestation retains `authority_granted=false` and records no
external action.

No source mutation, deployment, publication, commit, push, or external action
was authorized or performed by this run.
