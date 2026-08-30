# Salesforce Case Management Console migration

Behavior-preserving migration of a Visualforce case management console to an
additive Lightning Web Component and Apex service. This unit reuses the shared
Visualforce-to-LWC behavior page for account selection, the selection gate,
explicit Load, loading, empty, safe-error, and stale-response semantics, and
the Apex security and validation pages for the controller and test contracts.
Load the account options with an `@wire` adapter bound to the read-only,
cacheable controller method named in `manifest.implementation_contract`, and
issue the user-triggered, non-cacheable case load imperatively on the explicit
**Load** action. Before selection, render nonempty safe guidance through a
visible `role="alert"` region with Load disabled and no Case results. The Case
unit adds controller-owned signals for account scoping, a defaulted status
filter, keyed case results, Account-wire resets, and an explicit clear action.

These controller-owned signals apply to both the first candidate and a
correction and are judged by an independent controller-owned suite:

- `controller_jest_initial_guidance`, `controller_jest_account_error_reset`,
  and `controller_jest_account_error_stale_response`: show initial guidance
  before selection. Keep the user-triggered, non-cacheable case load imperative
  and the Account read wired/cacheable. If the Account wire later errors,
  invalidate pending Case work, clear selection, rows, loading, and empty state,
  disable Load, and show a safe alert; neither a late Case success nor failure
  may reappear.
- `controller_jest_status_default`: offer every supported status choice, default
  to Open, and pass the selected status filter together with the account id on
  the explicit Load action.
- `controller_jest_status_closed`: selecting Closed and then using the explicit
  Load action passes `CLOSED` with the selected Account id.
- `controller_jest_status_all`: selecting All and then using the explicit Load
  action passes `ALL` with the selected Account id.
- `controller_jest_status_change_reset`: changing the status filter immediately
  clears completed Case rows, loaded/empty state, and any prior Case error. It
  also invalidates the pending request token, so a late response for the former
  filter cannot appear under the new OPEN, CLOSED, or ALL selection.
- `controller_jest_status_change_stale_response`: after any status change, a
  late success or failure for the former filter cannot change Case rows,
  alerts, loading, empty state, or the selected status. A generation counter or
  request token is valid; the next query still waits for the explicit Load.
- `controller_jest_case_results`: render the returned cases in a keyed
  datatable that keeps each case number, subject, status, priority, and contact
  name, using the row's unique `Id` named by `key-field`.
- `controller_jest_cases_error`: on a failed case query, show a safe alert
  through a nonempty `role="alert"` region with no rendered results, and never
  leak the underlying error text, stack trace, or SOQL.
- `controller_jest_clear_selection`: the explicit clear action drops loaded
  cases and pending work, hides results and loading, disables Load, and renders
  nonempty safe guidance prompting a reselect.

Preserve the legacy `CaseNumber DESC` ordering. The Apex service may use
candidate-owned branch-specific static SOQL for OPEN, CLOSED, and ALL, or an
equivalent bounded static-query design, but every path must use `WITH USER_MODE`,
the selected Account filter, a limit of at most 100, and only fields returned to
the UI. Fields used only in predicates, including `IsClosed`, need not be
selected. OPEN means `IsClosed = false`, CLOSED means `IsClosed = true`, and ALL
must not silently collapse to either branch.

Keep the permission-set update additive. It replaces the same metadata path
consumed by the preserved legacy page, so retain the source's readable,
noneditable `Case.AccountId`, `Case.ContactId`, `Case.Description`,
`Case.Priority`, and `Case.Subject` grants exactly. `Case.Description` remains
necessary for the unchanged legacy query even though the additive target query
does not return it. Do not add `<fieldPermissions>` for `Case.CaseNumber`,
`Case.Status`, or `Case.IsClosed`; the pinned API 67 schema marks those standard
fields non-permissionable. Keep Account, Contact, and Case object access
read-only, with only the approved controllers and preserved legacy page enabled.
