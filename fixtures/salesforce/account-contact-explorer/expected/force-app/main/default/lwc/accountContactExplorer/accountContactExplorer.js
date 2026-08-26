import { LightningElement, wire } from 'lwc';
import getAccounts from '@salesforce/apex/AccountContactExplorerController.getAccounts';
import getContacts from '@salesforce/apex/AccountContactExplorerController.getContacts';

const BLANK_ACCOUNT_OPTION = Object.freeze({
    label: '-- Select an account --',
    value: ''
});

const CONTACT_COLUMNS = Object.freeze([
    { label: 'First Name', fieldName: 'FirstName', type: 'text' },
    { label: 'Last Name', fieldName: 'LastName', type: 'text' },
    { label: 'Email', fieldName: 'Email', type: 'email' },
    { label: 'Phone', fieldName: 'Phone', type: 'phone' }
]);

export default class AccountContactExplorer extends LightningElement {
    accountOptions = [BLANK_ACCOUNT_OPTION];
    selectedAccountId = '';
    contacts = [];
    columns = CONTACT_COLUMNS;
    isLoading = false;
    hasLoaded = false;
    loadRequestGeneration = 0;
    warningMessage;
    errorMessage;

    @wire(getAccounts)
    wiredAccounts({ data, error }) {
        if (data) {
            this.accountOptions = [
                BLANK_ACCOUNT_OPTION,
                ...data.map((accountRecord) => ({
                    label: accountRecord.Name,
                    value: accountRecord.Id
                }))
            ];
            this.errorMessage = undefined;
        } else if (error) {
            this.accountOptions = [BLANK_ACCOUNT_OPTION];
            this.errorMessage = 'Accounts could not be loaded.';
        }
    }

    handleAccountChange(event) {
        this.loadRequestGeneration += 1;
        this.selectedAccountId = event.detail.value;
        this.contacts = [];
        this.isLoading = false;
        this.hasLoaded = false;
        this.errorMessage = undefined;
        this.warningMessage = this.selectedAccountId
            ? undefined
            : 'Select an account before loading contacts.';
    }

    async handleLoad() {
        if (!this.selectedAccountId) {
            this.warningMessage = 'Select an account before loading contacts.';
            return;
        }

        const accountId = this.selectedAccountId;
        this.loadRequestGeneration += 1;
        const requestGeneration = this.loadRequestGeneration;

        this.isLoading = true;
        this.hasLoaded = false;
        this.contacts = [];
        this.warningMessage = undefined;
        this.errorMessage = undefined;

        try {
            const result = await getContacts({ accountId });
            if (!this.isCurrentRequest(accountId, requestGeneration)) {
                return;
            }
            this.contacts = (result ?? []).map((contactRecord) => ({
                ...contactRecord
            }));
            this.hasLoaded = true;
        } catch (error) {
            if (!this.isCurrentRequest(accountId, requestGeneration)) {
                return;
            }
            this.errorMessage = 'Contacts could not be loaded.';
        } finally {
            if (this.isCurrentRequest(accountId, requestGeneration)) {
                this.isLoading = false;
            }
        }
    }

    isCurrentRequest(accountId, requestGeneration) {
        return (
            accountId === this.selectedAccountId &&
            requestGeneration === this.loadRequestGeneration
        );
    }

    get isLoadDisabled() {
        return !this.selectedAccountId || this.isLoading;
    }

    get hasContacts() {
        return this.contacts.length > 0;
    }

    get showEmptyState() {
        return this.hasLoaded && !this.hasContacts && !this.errorMessage;
    }
}
