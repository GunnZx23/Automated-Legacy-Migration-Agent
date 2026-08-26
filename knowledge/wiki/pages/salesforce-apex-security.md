# Salesforce Apex security for LWC services

For code compiled at API 67.0, database operations default to user mode and a
class without a sharing keyword defaults to `with sharing`. Keep the fixture's
explicit `public with sharing` and `WITH USER_MODE`: they make the intended
record-sharing, object, and field authorization visible to reviewers and avoid
relying on version defaults. `with sharing` alone is not a CRUD/FLS check.

Expose only required `public static` methods with `@AuraEnabled`. Use
`cacheable=true` only for methods that read data and do not mutate it. Bind
values in static SOQL. Return controlled errors without stack traces, raw SOQL,
or secrets. Users also require Apex class access through a profile or permission
set; an LWC bundle does not grant that permission itself.

Keep LWC code compatible with Lightning Web Security: prefer plain objects to
`Map` for Apex serialization, do not mutate objects received across component
boundaries, and avoid manual DOM/HTML injection. CSP blocks inline scripts and
unknown resource origins; third-party libraries belong in reviewed static
resources, and required external origins need narrowly scoped trusted URLs.

Portable Apex unit tests prove functional behavior. Restricted-user acceptance
in the target sandbox is separate evidence because org sharing, profiles, and
field permissions vary.
