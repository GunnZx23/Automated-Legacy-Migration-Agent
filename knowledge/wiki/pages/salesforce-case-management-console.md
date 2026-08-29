# Salesforce Case Management Console migration

Behavior-preserving migration of a Visualforce case management console to an
additive Lightning Web Component and Apex service. This unit reuses the shared
Visualforce-to-LWC behavior page for account selection, the selection gate,
explicit Load, loading, empty, safe-error, and stale-response semantics, and
the Apex security and validation pages for the controller and test contracts.
Load the account options with an `@wire` adapter bound to the read-only,
cacheable controller method named in `manifest.implementation_contract`, and
issue the user-triggered case load imperatively on the explicit **Load**
action. It adds four controller-owned signals for account scoping, a defaulted
status filter, keyed case results, and an explicit clear action.

These controller-owned signals apply to both the first candidate and a
correction and are judged by an independent controller-owned suite:

- `controller_jest_status_default`: offer every supported status choice, default
  to Open, and pass the selected status filter together with the account id on
  the explicit Load action.
- `controller_jest_case_results`: render the returned cases in a keyed
  datatable that keeps each case number, subject, status, priority, and contact
  name, using the row's unique `Id` named by `key-field`.
- `controller_jest_cases_error`: on a failed case query, show a safe alert
  through a nonempty `role="alert"` region with no rendered results, and never
  leak the underlying error text, stack trace, or SOQL.
- `controller_jest_clear_selection`: the explicit clear action drops loaded
  cases and pending work, hides results and loading, disables Load, and renders
  nonempty safe guidance prompting a reselect.
