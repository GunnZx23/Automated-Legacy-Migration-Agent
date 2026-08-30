# Salesforce Apex security for LWC services

Project correction rule `apex_public_interface_annotation_mismatch`: the
generated service must be a `public with sharing` Apex class whose only
LWC-callable interface is the exact set of `public static` methods named in
`manifest.implementation_contract`. Place `@AuraEnabled` directly on each of
those methods and expose no additional Aura-enabled method. Use
`@AuraEnabled(cacheable=true)` on the read consumed by `@wire`. Keep an explicit,
user-triggered dependent read non-cacheable with bare `@AuraEnabled` or
`@AuraEnabled(cacheable=false)`; `cacheable=true` would let the imperative call
reuse client-cached data instead of preserving the explicit refresh boundary.
The class name, method names, return types, parameters, and per-method cache
policy come from the approved manifest, not from this page. Update only the
approved least-privilege permission set; do not create a second permission set or
modify a profile, and do not create `User` records.
The local controller contract checks safe exception translation, and
authorized-org validation proves the org-dependent security behavior.

Read-only query methods must use `WITH USER_MODE`, static bind variables,
bounded ordering, and a limit of 1 through 200 rows. A bound limit must resolve
to a positive compile-time `Integer` constant; the exact value inside that
range is candidate-owned. The `@AuraEnabled static` exposure and user-mode
security behavior are Salesforce platform features.

Keep both explicit `with sharing` and `WITH USER_MODE`. The first enforces the
class's record-sharing behavior; by itself it does not enforce object- and
field-level permissions. User-mode database operations enforce sharing, CRUD,
and field-level security. Writing both makes the intended boundary visible and
does not depend on changing API defaults.

Expose only required `public static` methods with `@AuraEnabled`, using the
manifest's method-specific wired/cacheable or imperative/non-cacheable policy.
Each generated query method must be inside a `try` block whose matching `catch`
translates the failure to a new `AuraHandledException`. Its sole argument must
be a short, fixed, safe literal message. Never return `Exception.getMessage()`,
stack traces, raw SOQL, record data, or secrets; catch/helper layout and exact
safe wording remain candidate-owned. Users also require Apex class access; an
LWC bundle does not grant that permission itself. Update only the approved
least-privilege permission set named in the manifest to add the new controller
while preserving its existing controller, Visualforce page, and read-only object
and field access. Do not create a second permission set or modify a profile.
Portable Apex tests use synthetic data. Do not create `User` records, query
`Profile`, or use `System.runAs` to fabricate a permission failure.
The local controller contract checks safe exception translation; an authorized
org validation proves Apex execution.

Project correction rule `apex_controlled_query_error_missing` applies only to
the generated service class. Repair each query method's safe exception
translation without changing its public signature, query contract, or any
unrelated artifact.

Keep LWC code compatible with Lightning Web Security: prefer plain objects to
`Map` for Apex serialization, do not mutate objects received across component
boundaries, and avoid manual DOM/HTML injection. CSP blocks inline scripts and
unknown resource origins; third-party libraries belong in reviewed static
resources, and required external origins need narrowly scoped trusted URLs.

Restricted-user acceptance in the target sandbox is separate evidence because
org sharing, profiles, and field permissions vary.
