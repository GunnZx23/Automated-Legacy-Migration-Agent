import { afterEach, describe, expect, it, jest } from '@jest/globals';
import { createElement } from 'lwc';
import AccountContactExplorer from 'c/accountContactExplorer';
import getAccounts from '@salesforce/apex/AccountContactExplorerController.getAccounts';
import getContacts from '@salesforce/apex/AccountContactExplorerController.getContacts';
import ACCOUNTS from './data/accounts.json';
import CONTACTS from './data/contacts.json';
jest.mock(
    '@salesforce/apex/AccountContactExplorerController.getAccounts',
    () => {
        const { createApexTestWireAdapter } = require('@salesforce/sfdx-lwc-jest');
        return { __esModule: true, default: createApexTestWireAdapter(jest.fn()) };
    },
    { virtual: true }
);

jest.mock(
    '@salesforce/apex/AccountContactExplorerController.getContacts',
    () => ({ __esModule: true, default: jest.fn() }),
    { virtual: true }
);

async function flushPromises() {
    await Promise.resolve();
    await Promise.resolve();
}

function createDeferredPromise() {
    let resolve;
    let reject;
    const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return { promise, resolve, reject };
}

function createComponent() {
    const element = createElement('c-account-contact-explorer', {
        is: AccountContactExplorer
    });
    document.body.appendChild(element);
    return element;
}

function selectAccount(element, accountId) {
    const combobox = element.shadowRoot.querySelector('lightning-combobox');
    combobox.dispatchEvent(
        new CustomEvent('change', {
            detail: { value: accountId }
        })
    );
}

describe('c-account-contact-explorer', () => {
    afterEach(() => {
        while (document.body.firstChild) {
            document.body.removeChild(document.body.firstChild);
        }
        jest.clearAllMocks();
        getContacts.mockReset();
    });

    it('renders a blank option followed by wired accounts', async () => {
        const element = createComponent();

        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        const combobox = element.shadowRoot.querySelector('lightning-combobox');
        expect(combobox.options).toEqual([
            { label: '-- Select an account --', value: '' },
            { label: ACCOUNTS[0].Name, value: ACCOUNTS[0].Id },
            { label: ACCOUNTS[1].Name, value: ACCOUNTS[1].Id }
        ]);
    });

    it('renders a controlled account wire error', async () => {
        const element = createComponent();

        getAccounts.error(new Error('SELECT Id FROM Account'));
        await flushPromises();

        expect(element.shadowRoot.querySelector('.error').textContent).toContain(
            'Accounts could not be loaded.'
        );
        expect(element.shadowRoot.querySelector('.error').textContent).not.toContain(
            'SELECT Id FROM Account'
        );
        expect(element.shadowRoot.querySelector('lightning-combobox').options).toEqual([
            { label: '-- Select an account --', value: '' }
        ]);
    });

    it('keeps Load disabled until an account is selected', async () => {
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        const button = element.shadowRoot.querySelector('lightning-button');
        expect(button.disabled).toBe(true);

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        expect(button.disabled).toBe(false);
    });

    it('loads contacts only after the explicit button click', async () => {
        getContacts.mockResolvedValue(CONTACTS);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        expect(getContacts).not.toHaveBeenCalled();

        element.shadowRoot.querySelector('lightning-button').click();
        await flushPromises();
        await flushPromises();

        expect(getContacts).toHaveBeenCalledTimes(1);
        expect(getContacts).toHaveBeenCalledWith({ accountId: ACCOUNTS[0].Id });
        const datatable = element.shadowRoot.querySelector('lightning-datatable');
        expect(datatable.data).toEqual(CONTACTS);
        expect(datatable.data).not.toBe(CONTACTS);
        expect(datatable.columns).toHaveLength(4);
    });

    it('shows loading state and disables Load while contacts are pending', async () => {
        let resolveContacts;
        getContacts.mockReturnValue(
            new Promise((resolve) => {
                resolveContacts = resolve;
            })
        );
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        const button = element.shadowRoot.querySelector('lightning-button');
        button.click();
        await flushPromises();

        const spinner = element.shadowRoot.querySelector('lightning-spinner');
        expect(spinner).not.toBeNull();
        expect(spinner.alternativeText).toBe('Loading contacts');
        expect(button.disabled).toBe(true);

        resolveContacts(CONTACTS);
        await flushPromises();
        await flushPromises();

        expect(element.shadowRoot.querySelector('lightning-spinner')).toBeNull();
        expect(button.disabled).toBe(false);
    });

    it('ignores a stale response after the selected account changes', async () => {
        const firstRequest = createDeferredPromise();
        const secondRequest = createDeferredPromise();
        getContacts
            .mockReturnValueOnce(firstRequest.promise)
            .mockReturnValueOnce(secondRequest.promise);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        element.shadowRoot.querySelector('lightning-button').click();
        await flushPromises();

        selectAccount(element, ACCOUNTS[1].Id);
        await flushPromises();
        element.shadowRoot.querySelector('lightning-button').click();
        await flushPromises();

        secondRequest.resolve(CONTACTS);
        await flushPromises();
        await flushPromises();
        expect(element.shadowRoot.querySelector('lightning-datatable').data).toEqual(
            CONTACTS
        );

        firstRequest.resolve([
            {
                ...CONTACTS[0],
                FirstName: 'Stale'
            }
        ]);
        await flushPromises();
        await flushPromises();

        expect(getContacts).toHaveBeenNthCalledWith(1, {
            accountId: ACCOUNTS[0].Id
        });
        expect(getContacts).toHaveBeenNthCalledWith(2, {
            accountId: ACCOUNTS[1].Id
        });
        const datatable = element.shadowRoot.querySelector('lightning-datatable');
        expect(datatable.data).toEqual(CONTACTS);
        expect(datatable.data[0].FirstName).not.toBe('Stale');
    });

    it('warns and disables Load when the selection is cleared', async () => {
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        selectAccount(element, '');
        await flushPromises();

        expect(element.shadowRoot.querySelector('lightning-button').disabled).toBe(true);
        expect(element.shadowRoot.querySelector('.warning').textContent).toContain(
            'Select an account before loading contacts.'
        );
        expect(getContacts).not.toHaveBeenCalled();
    });

    it('renders an empty state after a successful empty result', async () => {
        getContacts.mockResolvedValue([]);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        element.shadowRoot.querySelector('lightning-button').click();
        await flushPromises();
        await flushPromises();

        expect(element.shadowRoot.querySelector('.empty-state').textContent).toContain(
            'No contacts were found for the selected account.'
        );
        expect(element.shadowRoot.querySelector('lightning-datatable')).toBeNull();
    });

    it('renders a controlled contacts error', async () => {
        getContacts.mockRejectedValue({ body: { message: 'SELECT Id FROM Contact' } });
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        element.shadowRoot.querySelector('lightning-button').click();
        await flushPromises();
        await flushPromises();

        expect(element.shadowRoot.querySelector('.error').textContent).toContain(
            'Contacts could not be loaded.'
        );
        expect(element.shadowRoot.querySelector('.error').textContent).not.toContain(
            'SELECT Id FROM Contact'
        );
        expect(element.shadowRoot.querySelector('lightning-datatable')).toBeNull();
    });

    it('uses a safe fallback instead of exposing a generic technical error', async () => {
        getContacts.mockRejectedValue({
            message: 'SELECT Id FROM Contact failed at stack frame 42',
            stack: 'internal stack details'
        });
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        element.shadowRoot.querySelector('lightning-button').click();
        await flushPromises();
        await flushPromises();

        const errorText = element.shadowRoot.querySelector('.error').textContent;
        expect(errorText).toContain('Contacts could not be loaded.');
        expect(errorText).not.toContain('SELECT Id FROM Contact');
        expect(errorText).not.toContain('stack frame');
    });
});
