# Salesforce Visualforce to LWC migration

Preserve the observable behavior before modernizing the implementation. Record
the Visualforce components, controller properties and actions, validation
messages, query limits, sort order, empty states, and partial-page rerenders.

Typical mappings include `apex:pageBlock` to `lightning-card`,
`apex:selectList` to `lightning-combobox`, `apex:commandButton` to
`lightning-button`, and `apex:pageBlockTable` to `lightning-datatable`. A
`rerender` becomes a reactive DOM update; it is not an “LWC module update.”

The fixture loads a criteria-filtered Account/Contact list only after the user
clicks **Load**. Use an imperative Apex call for that explicit, one-shot action;
Salesforce specifically recommends imperative invocation when a button controls
when the method runs. Keep `@AuraEnabled(cacheable=true)` only if the method is
read-only. Pass a JavaScript object whose property names match the Apex
parameters.

If parameters can change while the promise is pending, capture the requested
identifier and a request generation. Apply a result, error, or loading-state
completion only when both still match the current selection, so an older slow
response cannot replace newer state.

Keep interaction semantics unless the approved manifest explicitly changes
them. For example, retain an explicit Load button when the legacy page does not
load automatically on selection. Treat deletion of the legacy page or
controller as a separate destructive decision after consumer discovery.
