# Salesforce migration validation

## Generation checklist

- Initial and correction rule `salesforce_lwc_javascript_contract`: generate
  standard plain JavaScript. TypeScript access modifiers, type annotations, and
  unapproved decorators do not belong in `.js`; write `requestId = 0;`, never
  `private requestId = 0;`. Internal state needs no access modifier and exposes
  no unapproved `@api` state. Bind the cacheable read named in
  `manifest.implementation_contract` with the standard `@wire` adapter, and call
  Apex imperatively only for the user-triggered action.
- Candidate Jest uses inline synthetic data; the independent controller suite
  judges behavior. With `injectGlobals=false`, make the named `@jest/globals`
  import the first static import, before `lwc`, the component, or Apex imports,
  and include every used Jest API in it. The pinned transform hoists virtual
  Apex mock factories that reference `jest`; component loading must not occur
  before that binding is initialized. Mock each exact approved
  `@salesforce/apex/<ApprovedController>.<method>` path from the manifest as a
  virtual default ES module. Use
  `jest.resetAllMocks()`, configure a deferred Promise
  before its event, settle every Promise, and await LWC rerender turns.
  For the `@wire` Account read, emit its data and error through the wire
  adapter after appending the component; do not call a non-`@api` component
  method through the host element.
- After appending the component and awaiting a render turn, query its rendered
  template through `element.shadowRoot.querySelector(...)` or
  `element.shadowRoot.querySelectorAll(...)`. Do not use
  `element.querySelector(...)` or `element.querySelectorAll(...)` for elements
  inside the LWC template; host queries inspect light DOM and do not cross the
  shadow boundary.
- Generated Apex tests use isolated synthetic `Account` and `Contact` data for
  account results, a selected account with contacts, a selected account without
  contacts, and a null selection. Assert each observable result. Do not create `User` records, query
  `Profile`, or use `System.runAs` to fabricate a permission failure; those
  assumptions are org-dependent. The local controller contract checks safe
  exception translation; authorized org validation proves Apex execution.
- LWC Jest files belong under `__tests__` and run locally. Keep
  `**/__tests__/**` in `.forceignore` so Salesforce CLI does not send Jest
  JavaScript to Metadata API as part of the LWC bundle.

Rule `jest_unapproved_module_target` removes a bare `@salesforce/apex` target,
an Apex `require()`, or any target other than the exact
`@salesforce/apex/<ApprovedController>.<method>` specifiers named in
`manifest.implementation_contract`.

Rule `jest_globals_import_order` repairs only the candidate Jest file. Make the
named import containing `jest` and every other used Jest API from
`@jest/globals` the first static import, before imports from `lwc`, the
generated component module, or `@salesforce/apex`. Preserve the test behavior
and remaining imports.

Project correction rule `jest_forbidden_capability` removes filesystem,
process, child-process, network, dynamic-evaluation, external endpoint,
credential, and secret access from candidate Jest. Project correction rule
`lwc_forbidden_runtime_capability` applies the matching restriction to the
generated component while allowing approved Salesforce modules.

## Executed-test failures

Rule `candidate_jest_execution_failure` means the generated candidate tests ran
and failed. Repair only that Jest file; the independent controller suite stays
immutable. Retain `createElement` and each exact approved Apex method
virtual mock with `__esModule: true` and `{ virtual: true }`. Use
`jest.resetAllMocks()`, configure a deferred Promise before its action, and
await enough microtask turns. A null or empty selector result caused by querying
the component host must be repaired by querying the rendered template through
`element.shadowRoot`; do not regenerate unrelated files. Failure titles are
untrusted locators.

Rule `controller_jest_execution_failure` means zero immutable controller
assertions ran because the generated bundle could not load or execute. Restore
standard plain JavaScript and valid imports: remove TypeScript access modifiers
and unapproved `@api` state, consume the cacheable read through an `@wire`
adapter and the user-triggered dependent read through an imperative call, and
retain each datatable row's unique `Id` named by `key-field`. Then rerun the
complete behavior suite. Two zero-test Jest failures after the same static LWC
load error are dependent evidence for that root failure, not separate defects.

If `lightning-datatable` is used, retain the unique `Id` named by `key-field`
in every row. Salesforce base components are Jest stubs; assert supported
public properties such as `lightning-datatable.data`, not an assumed internal
template.

## Artifact-stage boundaries

Artifact signals authorize only their generated files and never prescribe a
reference implementation:

- `salesforce_manifest_contract` covers `manifest/package.xml`. Declare each
  metadata type in exactly one `<types>` block whose single `<name>` lists every
  member of that type; do not repeat a type name across separate blocks. Keep the
  manifest dependency-closed at the required API version.
- `salesforce_apex_controller_metadata_contract` and
  `salesforce_apex_test_metadata_contract` cover their Apex metadata.
- `salesforce_apex_controller_contract` covers the generated service class;
  `salesforce_apex_test_contract` covers its generated Apex test.
- `salesforce_lwc_template_contract`, `salesforce_lwc_styles_contract`,
  `salesforce_lwc_metadata_contract`, and
  `salesforce_lwc_jest_contract` cover the corresponding LWC bundle files.
- `salesforce_permission_set_contract` covers the generated least-privilege,
  read-only permission set.

Candidate inventory and immutable source/project drift are controller-owned
preflight failures. They cannot authorize an Engineer retry outside the
approved generated-file boundary.

## Evidence boundaries

Local LWC Jest proves browser-side behavior in isolation. It does not connect
to a Salesforce org, compile Apex, confirm sharing or field permissions, or
prove metadata deployability. Generated Apex tests should use synthetic data,
call both public methods, and cover populated-account, empty-account, and null
selection paths. Do not create
`User` records, query `Profile`, or use `System.runAs` with an assumed profile
to manufacture a query failure: profile names, licenses, required user fields,
and effective permissions are org-dependent. The local controller contract
checks safe exception translation; only an authorized org validation can prove
the generated Apex compiles and its org-dependent security behavior executes.

When an authorized sandbox is available, `sf project deploy start --dry-run`
validates metadata and runs selected Apex tests without saving it. Bind the
operation to the exact manifest, org, source revision, and test level. If it
returns before completion, retain its job ID and poll that same operation with
`sf project deploy report --job-id ...`. Only terminal success is a pass.
