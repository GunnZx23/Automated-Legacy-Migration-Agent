# Corrected benchmark v2 independent output review

This packet supports a genuine independent human review of all 18 outputs from
the corrected Wiki/no-Wiki campaign. It is a navigation aid, not a scorecard.
The controller's terminal disposition and deterministic checks are evidence for
the reviewer; they are not human acceptance, semantic-conformance, Wiki-support,
or escaped-defect judgments.

BW's earlier approval applies to the frozen dependency-label subject and to a
separate interactive Case candidate. It does **not** constitute per-cell review
of this campaign. Every output-review field remains pending until a reviewer
returns all 18 decisions in `REVIEWER_RESPONSE_TEMPLATE.md`.

## Evidence identity

| Item | Bound value |
| --- | --- |
| Registry | `legacy-migration-benchmark-v2` |
| Protocol declaration digest | `sha256:9ad200c6ec7b0f3d442a6e945ddb96b2e63cce45f2468b1dacaa9543c09635ac` |
| Registry digest | `sha256:16f4516d28990dd9542defa534cac2aa1073d99b7fb2e5e52b548f279e75642a` |
| Corrected execution anchor | `benchmark-v2-corrected-anchor-2` |
| Corrected anchor digest | `sha256:6b65847d2b5a0d792fff878bb213b111e82b336063cf4d2700a6149bd1d3c0d8` |
| Runtime identity digest | `sha256:d038f0f2ce95607ad01fd51889385c35226577e30d02fa622bef44ce9b302a6c` |
| Reviewed dependency-label subject | `sha256:fa76b2b5b9f8a9f4dea0637e7c3a1f6d9cddce6fa08ff7fbc4695631ffd29183` |
| Dependency-label review evidence | `sha256:b718d6b3c130d1318f27b9911ec223cde650a39b19af39927ab590ccf3aba5c3` |
| Raw campaign archive | `output/benchmark-v2-corrected-campaign-20260830.tar.gz` |
| Raw archive SHA-256 | `f6a2e2ac0672a7631c0b6331e41a896574933c8704e2eb7707222ee5eeae1336` |
| Raw archive size | `3,447,749` bytes |
| Machine navigation view | `review-materials/machine-cell-summary.json` |

All 18 terminal directories were loaded without invoking a provider through
`load_verified_benchmark_run_bundle`. The machine summary records the verified
run-evidence digest, protocol binding, every completed validation attempt,
change-set and report digests, changed paths, check counts, terminal failure
metadata, evidence-completeness flags, and raw-archive members. It deliberately
contains no human acceptance, semantic, Wiki-support, or defect values.

## Packet layout

After extracting the review packet, use these directories:

```text
review-materials/       this guide, response template, machine summary, checksums
campaign-evidence/      corrected execution anchor and portable evidence per cell
protocol/               frozen benchmark declarations, labels, and source edges
fixtures/               the three bounded synthetic legacy inputs
knowledge/wiki/         curated Wiki content available only to Wiki-arm model calls
agents/                 frozen Architect, Engineer, and Validator definitions
tooling-context/         bounded deterministic-test and Mule authority context
```

The separate raw campaign archive remains the authoritative complete capture.
The packet intentionally excludes `.git`, `.venv`, `node_modules`, provider
credentials, output directories, caches, scratch workspaces, and the campaign
driver. `EVIDENCE_SHA256SUMS.txt` binds the selected inputs and review materials;
the packet archive itself is bound separately by its `.sha256` sidecar.

## How to inspect one cell

For cell `<cell-id>`, start with its entry in `machine-cell-summary.json`, then
open the following packet paths:

```text
campaign-evidence/<cell-id>/evidence/request.json
campaign-evidence/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/architect.json
campaign-evidence/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/dependency-graph.json
campaign-evidence/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/wiki-trace.json
campaign-evidence/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/engineer-attempt-1.json
campaign-evidence/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/report-attempt-1.json
campaign-evidence/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/validator-attempt-1.json
```

The generated source is retained in each Engineer artifact under
`change_set.unified_diff`, with its inventory under `change_set.changed_paths`.
Deterministic check outcomes and diagnostics are in the corresponding report.

### Attempt-2 navigation

When `completed_attempts` includes `2`, inspect both attempts. Attempt 2 is a
bounded correction, not an independent replacement run:

```text
campaign-evidence/<cell-id>/evidence/control/correction-request-attempt-1.json
campaign-evidence/<cell-id>/evidence/control/correction-approval-attempt-2.json
campaign-evidence/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/correction-wiki-attempt-2.json
campaign-evidence/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/engineer-correction-attempt-2.json
campaign-evidence/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/engineer-attempt-2.json
campaign-evidence/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/report-attempt-2.json
campaign-evidence/<cell-id>/evidence/model-runs/benchmark-v2-request-<cell-id>/validator-attempt-2.json
```

Use the final completed attempt for the candidate decision, but inspect attempt
1 and the correction evidence when judging the repair. If an attempt-2 provider
or policy boundary stopped before a complete report, the machine summary marks
the missing evidence explicitly; do not invent it.

## Scoring rules

Return one decision for every cell. Do not infer a judgment from the observed
controller result.

1. **Acceptance** — `accepted`, `rejected`, or `not_applicable`. Use
   `not_applicable` when no candidate is eligible for acceptance. If retained
   evidence cannot establish applicability, use `unavailable: <reason>`.
2. **Semantic conformance** — `true` when the generated candidate or safe
   intervention preserves the frozen scenario contract; `false` when it does
   not. A correct `decision_required` stop can be conformant without generating
   code. Use `unavailable: <reason>` only when evidence is insufficient.
3. **Wiki support** — for Wiki cells, report `numerator/denominator`, where the
   denominator is the positive number of output assertions reviewed for
   dependence on retrieved Wiki guidance and the numerator is the number both
   accurately supported and correctly applied. For no-Wiki cells, use
   `unavailable: Wiki retrieval was disabled by this configuration.`
4. **Escaped defects** — use `none`, a semicolon-separated list of
   `unique ID / low/medium/high/critical / description` entries, or
   `unavailable: <reason>`. Use `/` within a table cell; do not use the Markdown
   table delimiter `|` in an entry. An assessed candidate with no defects is
   `none`, not unavailable. Do not count a declared environment limitation as a
   code defect unless the candidate itself mishandles that boundary.

## Machine-observed terminal outcomes

`P/F/U` below means passed/failed/unavailable deterministic checks. It is not a
human score.

| Key | Cell | Terminal disposition | Completed report evidence | Candidate diff evidence |
| --- | --- | --- | --- | --- |
| M-W1 | Mule, Wiki, r1 | `environment_unavailable` | a1 `3/0/2` | a1 |
| M-W2 | Mule, Wiki, r2 | `recoverable_failure` | a1 `2/1/2`; a2 `2/1/2` | a1, a2 |
| M-W3 | Mule, Wiki, r3 | `recoverable_failure` | a1 `1/2/2`; a2 `2/1/2` | a1, a2 |
| M-N1 | Mule, no-Wiki, r1 | `controlled_failure` (`correction_signal_coverage_missing`) | a1 `1/2/2`; attempt 2 incomplete | a1 |
| M-N2 | Mule, no-Wiki, r2 | `environment_unavailable` | a1 `3/0/2` | a1 |
| M-N3 | Mule, no-Wiki, r3 | `recoverable_failure` | a1 `1/2/2`; a2 `1/2/2` | a1, a2 |
| A-W1 | Account/Contact, Wiki, r1 | `ready_for_human_review` | a1 `7/0/0` | a1 |
| A-W2 | Account/Contact, Wiki, r2 | `ready_for_human_review` | a1 `7/0/0` | a1 |
| A-W3 | Account/Contact, Wiki, r3 | `recoverable_failure` | a1 `6/1/0`; a2 `6/1/0` | a1, a2 |
| A-N1 | Account/Contact, no-Wiki, r1 | `ready_for_human_review` | a1 `7/0/0` | a1 |
| A-N2 | Account/Contact, no-Wiki, r2 | `ready_for_human_review` | a1 `6/1/0`; a2 `7/0/0` | a1, a2 |
| A-N3 | Account/Contact, no-Wiki, r3 | `ready_for_human_review` | a1 `6/1/0`; a2 `7/0/0` | a1, a2 |
| C-W1 | Complex Case, Wiki, r1 | `decision_required` | no Engineer/Validator attempt by design | none |
| C-W2 | Complex Case, Wiki, r2 | `decision_required` | no Engineer/Validator attempt by design | none |
| C-W3 | Complex Case, Wiki, r3 | `decision_required` | no Engineer/Validator attempt by design | none |
| C-N1 | Complex Case, no-Wiki, r1 | `decision_required` | no Engineer/Validator attempt by design | none |
| C-N2 | Complex Case, no-Wiki, r2 | `decision_required` | no Engineer/Validator attempt by design | none |
| C-N3 | Complex Case, no-Wiki, r3 | `decision_required` | no Engineer/Validator attempt by design | none |

Mule's two unavailable checks are the declared local Mule Maven/MUnit runtime
boundary. `environment_unavailable` therefore is not Mule runtime success.
Likewise, a deterministic pass does not substitute for the independent semantic
review requested here.

## Claim boundary

Until all 18 genuine per-cell decisions are returned, encoded without changing
their meaning, bound to the exact `run_evidence_digest` values, and verified by
the receipt/corpus pipeline, no aggregate acceptance, semantic-conformance,
Wiki-benefit, escaped-defect, latency, first-pass, repair, or benchmark exit-gate
claim may be made from this campaign.
