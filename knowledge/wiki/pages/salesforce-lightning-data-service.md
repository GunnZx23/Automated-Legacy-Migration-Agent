# Salesforce Lightning Data Service record retrieval

For an LWC that only reads a supported Salesforce record, prefer Lightning
Data Service through `lightning/uiRecordApi` instead of adding an Apex method.
`getRecord` uses User Interface API, participates in the client-side record
cache, and returns data in the current user's access context. Use
`optionalFields` only when an inaccessible field may be omitted.

```js
import { LightningElement, api, wire } from 'lwc';
import { getFieldValue, getRecord } from 'lightning/uiRecordApi';
import ACCOUNT_NAME from '@salesforce/schema/Account.Name';

const FIELDS = [ACCOUNT_NAME];

export default class AccountSummary extends LightningElement {
    @api recordId;
    @wire(getRecord, { recordId: '$recordId', fields: FIELDS }) account;

    get accountName() {
        return getFieldValue(this.account.data, ACCOUNT_NAME);
    }
}
```

`$recordId` is reactive; schema imports declare dependencies. Render loading
and error states and request only behaviorally required fields.

This is not the retrieval shape of the capstone fixture. That UI waits for a
**Load** click and queries an ordered, limited Account/Contact collection by
criteria rather than reading one known record ID. `getRecord` cannot express
that list query. An LDS-backed GraphQL wire adapter could support multi-record
filtering, but changing the existing Apex service contract is a separate
redesign. This bounded migration preserves the explicit imperative call.
