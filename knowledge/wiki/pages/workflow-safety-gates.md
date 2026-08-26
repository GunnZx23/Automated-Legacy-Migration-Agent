# Migration workflow safety gates

The Architect produces a versioned manifest bound to one request and base
revision. The Engineer may change only approved paths in an isolated workspace.
The deterministic controller, not the Validator LLM, owns the predeclared,
allowlisted checks. It runs those checks first and stores immutable, redacted
receipts. Only then does the Validator LLM assess that immutable evidence and
emit a disposition; it cannot select, alter, or rerun commands. Required checks
that are missing, unavailable, nonterminal, or failed cannot produce a
ready-for-review disposition.

Human approval is exact: one decision is bound to one request, artifact digest,
action, and reviewer. It does not authorize a different action or a later
artifact. Commit, push, pull request, sandbox mutation, deployment, destructive
change, and publication remain explicit human gates. Retries are bounded and
exhaustion becomes a visible outcome.
