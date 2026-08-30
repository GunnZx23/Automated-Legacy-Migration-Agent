# Invalid benchmark pilot — 2026-08-30

The first execution-anchored benchmark-v2 campaign is preserved for audit but
is excluded from scoring and submission claims.

- Archive: `output/benchmark-v2-measured-campaign-20260830.tar.gz`
- Archive SHA-256: `a7d15b41dbab1be18a924457a30ddd636730cfe8ce9514a44f60efae408936f5`
- Anchor ID: `benchmark-v2-final-anchor-1`
- Anchor digest: `sha256:1df7fcb3f647213df9d09a1a13f7f162fb273c660d196588e5af3de83762322c`
- Terminal cells: 18 of 18
- Comparison eligible: no

## Root cause

All nine `full-agent-no-wiki` cells produced a provider-schema-valid Architect
response and then stopped at the controller-owned policy boundary. The output
contract required at least one Wiki citation. The no-Wiki controller supplied a
single synthetic `benchmark-no-wiki-control` marker and required that exact ID
in `cited_wiki_pages`, while also forbidding the marker in semantic-decision or
risk evidence. The common live prompt did not explain that administrative-only
citation rule and instead described all hits as curated Wiki guidance.

Scripted tests passed because their fixtures directly authored the hidden valid
shape. They proved the controller branch was technically passable, but did not
prove that a live model had been given the rule. Nine identical failures across
three cases make the two benchmark arms incomparable.

The failure sanitizer retained only `policy_rejected`; no rejected Architect
output or accepted model-call record was persisted, so the exact violated
subclause cannot be recovered from the archive. That evidence gap is also being
addressed before the replacement campaign.

## Corrective boundary

The common Architect and Engineer contracts now state the no-Wiki marker
semantics without changing prompts between arms. The controller continues to
reject marker use as migration guidance and does not normalize model output.
Because agent definitions and runtime bytes changed, the replacement campaign
must use a new execution anchor and rerun all 18 matched cells. Old and new
cells will not be mixed. Independent output review, receipts, aggregation, and
any bounded Wiki comparison begin only after that full rerun.
