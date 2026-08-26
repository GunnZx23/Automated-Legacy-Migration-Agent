# Salesforce migration validation

Validate at several layers and record terminal receipts for each required
check. LWC Jest tests cover rendering, events, wire adapters, imperative Apex
mocks, loading, empty, and controlled-error states. Jest runs the component in
local isolation; it does not connect to an org, compile Apex, verify class/FLS
permissions, or prove metadata deployability. Apex tests use synthetic records
and verify filters, ordering, limits, null input, and empty results.

When an authorized sandbox is available, `sf project deploy start --dry-run`
validates and runs the selected Apex tests without saving metadata. It is still
an external org operation, distinct from the capstone's local static sandbox.
Bind it to an exact manifest, target, source revision, and test level. If the
command returns before completion, retain its job ID and use
`sf project deploy report --job-id ...` to poll the same operation. Only its
terminal success is a pass; submitted, queued, timed-out, unavailable, or local
results are not sandbox-validation success.
