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
current reference workflow records the correction outcome for human review; it
does not autonomously execute a retry, replan, commit, or deployment.
