# Benchmark v2 independent output review

> **Superseded — do not review or score this packet.** It describes the first
> 18-cell campaign, which is preserved as an invalid methodology pilot. The
> no-Wiki Architect prompt omitted a controller rule required after provider
> schema validation. No rubric, receipt, aggregate metric, or Wiki-benefit claim
> may be derived from these outputs. A replacement packet will bind the complete
> corrected rerun under a new execution anchor.

This packet is for the independent human review of the 18 outputs from the
predeclared Wiki/no-Wiki benchmark. It is deliberately separate from BW's
earlier approval of the frozen dependency-label packet and the final
interactive Case candidate. Those approvals are complete, but they do not
constitute review of these later benchmark outputs.

## Evidence identity

- Registry: `legacy-migration-benchmark-v2`
- Registry digest: `sha256:960bf1bfa81caa217003fbee649d9a3ded655ec005e2e7107f6672477a7a201d`
- Execution anchor: `benchmark-v2-final-anchor-1`
- Anchor digest: `sha256:1df7fcb3f647213df9d09a1a13f7f162fb273c660d196588e5af3de83762322c`
- Runtime identity: `sha256:13df100ad1b1bdbab85cda66288c0b8f5b51a198fbd65b6a3476550fa19685a2`
- Reviewed label subject: `sha256:fa76b2b5b9f8a9f4dea0637e7c3a1f6d9cddce6fa08ff7fbc4695631ffd29183`
- Raw campaign archive: `output/benchmark-v2-measured-campaign-20260830.tar.gz`
- Raw archive SHA-256: `a7d15b41dbab1be18a924457a30ddd636730cfe8ce9514a44f60efae408936f5`
- Machine summary: `evaluation/benchmark-v2/output-review/machine-cell-summary.json`

The archive contains 18 terminal run directories, 756 files, and the
execution anchor. The machine summary is a derived navigation aid; the raw run
evidence remains authoritative. Any protocol README retained from the frozen
execution snapshot describes the state when that snapshot was made; use this
post-run guide and the machine summary for the current campaign status.

## What to inspect

For a cell named `<cell-id>`, inspect the following members when present:

```text
benchmark-v2/<cell-id>/evidence/request.json
benchmark-v2/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/architect.json
benchmark-v2/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/dependency-graph.json
benchmark-v2/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/engineer-attempt-1.json
benchmark-v2/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/validator-attempt-1.json
benchmark-v2/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/report-attempt-1.json
benchmark-v2/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/wiki-trace.json
```

Only five cells produced candidate diffs: Mule Wiki repetitions 1-2 and
Account/Contact Wiki repetitions 1-3. Their complete generated source is in
`engineer-attempt-1.json` under `change_set.unified_diff`; their candidate
inventory is under `change_set.changed_paths`. The associated test outcomes
are in `report-attempt-1.json`. The three complex Case Wiki cells stopped at
the predeclared decision boundary before generation. Nine no-Wiki cells failed
the strict Architect contract, and Mule Wiki repetition 3 ended in a provider
failure.

## Scoring rules

Provide one decision for each of the 18 cells. Do not infer a human judgment
from the controller's terminal status.

1. **Acceptance** — `accepted`, `rejected`, or `not_applicable`. Use
   `not_applicable` when no candidate is eligible for acceptance. If the
   evidence is genuinely insufficient even to decide applicability, mark the
   judgment unavailable and explain why.
2. **Semantic conformance** — `true` when the generated candidate or safe
   intervention preserves the frozen scenario contract; `false` when it does
   not. A correct decision-required stop can be semantically conformant even
   though it intentionally produced no code. Use `unavailable` only when the
   retained evidence is insufficient to judge.
3. **Wiki support** — for Wiki cells, count distinct output assertions that
   depend on retrieved Wiki guidance. The denominator is the number reviewed;
   the numerator is the number accurately supported by the cited Wiki content
   and correctly applied. The denominator must be positive. For every no-Wiki
   cell this field is fixed as unavailable because retrieval was disabled.
4. **Escaped defects** — list defects visible to the reviewer but not prevented
   by the workflow. An assessed candidate with no defects is an available empty
   list, not unavailable. If no candidate or sufficient evidence exists, mark
   this field unavailable and explain why. Each defect needs a unique ID,
   impact (`low`, `medium`, `high`, or `critical`), and a short description.

All available judgments will be cryptographically bound to that cell's exact
`run_evidence_digest`. The reviewer does not need to copy digests into the
response; the ingestion step supplies and verifies those machine-owned fields.

## Observed terminal outcomes

| Review key | Case and arm | Observed result | Candidate diff |
|---|---|---|---|
| `M-W1` | Mule, Wiki, r1 | `environment_unavailable` | yes |
| `M-W2` | Mule, Wiki, r2 | `environment_unavailable` | yes |
| `M-W3` | Mule, Wiki, r3 | `controlled_failure` (`provider_unavailable`) | no |
| `M-N1` | Mule, no-Wiki, r1 | `controlled_failure` (`policy_rejected`) | no |
| `M-N2` | Mule, no-Wiki, r2 | `controlled_failure` (`policy_rejected`) | no |
| `M-N3` | Mule, no-Wiki, r3 | `controlled_failure` (`policy_rejected`) | no |
| `A-W1` | Account/Contact, Wiki, r1 | `ready_for_human_review` | yes |
| `A-W2` | Account/Contact, Wiki, r2 | `ready_for_human_review` | yes |
| `A-W3` | Account/Contact, Wiki, r3 | `ready_for_human_review` | yes |
| `A-N1` | Account/Contact, no-Wiki, r1 | `controlled_failure` (`policy_rejected`) | no |
| `A-N2` | Account/Contact, no-Wiki, r2 | `controlled_failure` (`policy_rejected`) | no |
| `A-N3` | Account/Contact, no-Wiki, r3 | `controlled_failure` (`policy_rejected`) | no |
| `C-W1` | Complex Case, Wiki, r1 | `decision_required` | no, intentional stop |
| `C-W2` | Complex Case, Wiki, r2 | `decision_required` | no, intentional stop |
| `C-W3` | Complex Case, Wiki, r3 | `decision_required` | no, intentional stop |
| `C-N1` | Complex Case, no-Wiki, r1 | `controlled_failure` (`policy_rejected`) | no |
| `C-N2` | Complex Case, no-Wiki, r2 | `controlled_failure` (`policy_rejected`) | no |
| `C-N3` | Complex Case, no-Wiki, r3 | `controlled_failure` (`policy_rejected`) | no |

The review-key-to-cell-ID mapping is in `REVIEWER_RESPONSE_TEMPLATE.md`; exact
run digests are in `machine-cell-summary.json`.

## Claim boundary

The campaign's model phase is complete and preserved. Until all 18 genuine
rubrics are ingested and the resulting receipts verify, the project must not
claim aggregate acceptance, semantic-conformance, Wiki-benefit, escaped-defect,
latency, first-pass, repair, or benchmark exit-gate results.
