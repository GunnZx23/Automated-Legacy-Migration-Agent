# Salesforce Apex security for LWC services

Project correction rule `apex_public_interface_annotation_mismatch`: the
generated service must be `public with sharing class
AccountContactExplorerController` and expose exactly these two LWC-callable
interfaces:

```apex
@AuraEnabled(cacheable=true)
public static List<Account> getAccounts()
@AuraEnabled(cacheable=true)
public static List<Contact> getContacts(Id accountId)
```

Place the annotation directly on each method. Both methods are read-only, and
every SOQL statement must use `WITH USER_MODE`, static bind variables, bounded
ordering, and a limit. These exact names and signatures are this project's
candidate contract; the underlying `@AuraEnabled static` exposure and user-mode
security behavior are Salesforce platform features.

Keep both explicit `with sharing` and `WITH USER_MODE`. The first enforces the
class's record-sharing behavior; by itself it does not enforce object- and
field-level permissions. User-mode database operations enforce sharing, CRUD,
and field-level security. Writing both makes the intended boundary visible and
does not depend on changing API defaults.

Expose only required `public static` methods with `@AuraEnabled`. Use
`cacheable=true` only for methods that read data and do not mutate it. In this
project's generated service, each query method must translate query failures to
a new `AuraHandledException` with a short safe literal message. Never return
`Exception.getMessage()`, stack traces, raw SOQL, record data, or secrets;
catch/helper layout and exact safe wording remain candidate-owned. Users also
require Apex class access; an LWC bundle does not grant that permission itself.
For this bounded fixture, update only the approved
`AccountContactExplorerUser` permission set to add the new controller while
preserving its legacy controller, Visualforce page, and read-only object and
field access. Do not create a second permission set or modify a profile.

Project correction rule `apex_controlled_query_error_missing` applies only to
the generated service class. Repair each query method's safe exception
translation without changing its public signature, query contract, or any
unrelated artifact.

Keep LWC code compatible with Lightning Web Security: prefer plain objects to
`Map` for Apex serialization, do not mutate objects received across component
boundaries, and avoid manual DOM/HTML injection. CSP blocks inline scripts and
unknown resource origins; third-party libraries belong in reviewed static
resources, and required external origins need narrowly scoped trusted URLs.

Portable Apex unit tests prove functional behavior. Restricted-user acceptance
in the target sandbox is separate evidence because org sharing, profiles, and
field permissions vary.
