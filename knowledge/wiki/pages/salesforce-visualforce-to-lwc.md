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
dependent load must remain an imperative, non-cacheable Apex call because the
user explicitly starts it with **Load** and expects a server refresh. Declare
that explicit read with bare `@AuraEnabled` or `@AuraEnabled(cacheable=false)`,
never `cacheable=true`.

Project correction rule `lwc_template_binding_invalid`: the pinned compiler
supports complex template expressions; this is a maintainability rule, not a
compiler limit. Keep `data-role` and `data-state` hooks literal or simply bound;
move nontrivial presentation logic to JavaScript getters.

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
- `controller_jest_account_error`: render a fixed, safe Account error; never
  copy untrusted `error.message`, `error.body.message`, query text, or other
  payload details into visible DOM.
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
- `controller_jest_account_change_reset`: changing from one nonblank Account to
  another immediately clears completed Contacts, loaded/empty state, and any
  prior Contact error before the next explicit Load. It also invalidates the
  pending request token, so the former Account's late response cannot reappear
  under the new selection.
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

If results are rendered with a native table instead of `lightning-datatable`,
give the table an accessible name with a visible caption or an appropriate
`aria-label`. The semantic test hooks do not replace an accessible name.

Use `@AuraEnabled(cacheable=true)` for the wired initial read. Keep the explicit
dependent read non-cacheable with bare `@AuraEnabled` or
`@AuraEnabled(cacheable=false)`. Pass an object whose JavaScript property names
match the Apex parameters. Preserve the explicit Load interaction unless the
approved manifest changes it. Deleting the legacy page or controller remains a
separate destructive decision after consumer discovery.
