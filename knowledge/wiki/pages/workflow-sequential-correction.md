# Evidence-grounded sequential planning and correction

Use one evidence-supported Architect plan for the bounded migration slice.
Each planning step names the constraints it satisfies and cites the frozen
repository graph or curated pilot Wiki trace. ReAct is research inspiration for
interleaving evidence gathering and planning, not a technical standard or a
runtime dependency. The deterministic controller requires exact coverage,
rejects duplicate or unknown constraints, and stops with
`decision_required` when a hard scope or safety violation remains.

After the Engineer and Validator complete, classify the terminal report. One
recoverable implementation failure may request a same-manifest retry. An
invalid plan requires a new manifest digest and a new human approval. An
unavailable environment or an exhausted attempt budget stops visibly. The
current workflow never authorizes a retry, replan, commit, or deployment merely
because validation failed.

A proposed second attempt is a correction of the existing candidate, not a
fresh migration. Before invoking the Engineer, derive a bounded Wiki query from
the controller-owned diagnostic IDs, retrieve the directly relevant curated
page, and record a digest-bound trace containing the query, catalog digest,
selected page and excerpt digests, prior-candidate digest, and correction-request
digest. Fail closed when retrieval returns no relevant hit; do not ask the same
model to guess from the same unchanged context.

After exact human approval, give the Engineer the prior candidate, failed check
summaries, and retrieved correction evidence. It should update only the files
needed for those diagnostics while carrying all other candidate files forward
unchanged. Require a nonempty changed-file delta against attempt one and reject
an unchanged resubmission. Bind attempt-two evidence to both candidate digests,
then rerun the required dependent checks so a narrow patch cannot hide a
regression. Tie resolution is deterministic by Wiki score and stable page ID;
ambiguous or conflicting guidance returns to human review.
