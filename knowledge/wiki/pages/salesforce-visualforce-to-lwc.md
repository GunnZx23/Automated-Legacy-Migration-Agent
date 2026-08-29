# Salesforce Visualforce to LWC migration

Preserve the observable behavior before modernizing implementation details. Record
the Visualforce controls, controller properties and actions, validation
messages, query limits, sort order, loading behavior, empty results, errors,
and partial-page rerenders.

Load the initial options with an `@wire` adapter bound to the read-only,
cacheable controller method named in `manifest.implementation_contract`.
Binding a `@AuraEnabled(cacheable=true)` read with `@wire` is the standard
reactive Lightning pattern, so the component reacts to the adapter's data and
error branches rather than fetching imperatively on init. The user-triggered
dependent load must remain an imperative call because the user explicitly
starts it with **Load**.

Project correction rule `lwc_template_binding_invalid`: the pinned LWC compiler
supports complex template expressions, so this is a project maintainability
convention, not a compiler restriction. Prefer standard JavaScript getters for
nontrivial presentation logic, and keep `data-role` and `data-state` hooks
literal or bound to a simple property so the public semantic test surface stays
stable.

For a combobox, a placeholder is not the required blank choice. Include a
rendered option whose value is the empty string, followed by the returned
Account choices. Bind `lightning-button.disabled` to a getter that describes
the disabled state, such as `isLoadDisabled`, and have that getter return true
for the blank selection. Do not bind a positive `canLoadContacts` getter
directly to `disabled`; that reverses the selection gate.

The project browser harness needs stable semantic hooks: put
`data-role="account-selector"` on the interactive selector and provide unique
hooks for the Load action and contact results. These hooks are a test adapter,
not a required UI design. Keep controls accessible and render guidance or
controlled errors through a nonempty `role="alert"` region.

Controller-owned signals apply to both the first candidate and a correction:

- `controller_jest_account_options`: render an empty-string option before the
  Accounts; a `lightning-combobox` placeholder is not that option.
- `controller_jest_account_error`: render a safe, nontechnical Account error.
- `controller_jest_selection_gate`: Load is disabled for blank and enabled for
  nonblank selection; do not bind a positive `canLoadContacts` getter directly.
  Bind a disabled-state getter because a positive getter reverses the gate.
- `controller_jest_explicit_load`: request Contacts only after the explicit
  action and render successful results.
- `controller_jest_loading_state`: show loading while the current request is
  pending.
- `controller_jest_stale_response`: each selection change invalidates pending
  work; a late success or failure cannot change Contacts, alerts, or loading.
  A generation counter or request token is valid.
- `controller_jest_blank_selection`: invalidate pending work, clear result
  states, disable Load, and show safe guidance when selection is cleared.
- `controller_jest_empty_state`: show empty only after a current empty success,
  never before the first explicit Load.
- `controller_jest_contacts_error`: show a safe error with no visible `contact-results` hook,
  even when its rows are empty.

Typical mappings include `apex:pageBlock` to `lightning-card`,
`apex:selectList` to `lightning-combobox`, `apex:commandButton` to
`lightning-button`, and `apex:pageBlockTable` to `lightning-datatable`. A
Visualforce `rerender` becomes a reactive DOM update; it is not an “LWC module
update.”

Keep `@AuraEnabled(cacheable=true)` only on read-only methods. Pass an object
whose JavaScript property names match the Apex parameters. Preserve the
explicit Load interaction unless the approved manifest changes it. Deleting
the legacy page or controller remains a separate destructive decision after
consumer discovery.
