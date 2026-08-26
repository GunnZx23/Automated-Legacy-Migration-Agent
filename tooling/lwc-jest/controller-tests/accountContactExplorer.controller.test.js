import { afterEach, describe, expect, it, jest } from '@jest/globals';
import { createElement } from 'lwc';
import AccountContactExplorer from 'c/accountContactExplorer';
import getAccounts from '@salesforce/apex/AccountContactExplorerController.getAccounts';
import getContacts from '@salesforce/apex/AccountContactExplorerController.getContacts';

jest.mock(
    '@salesforce/apex/AccountContactExplorerController.getAccounts',
    () => {
        const { createApexTestWireAdapter } = require('@salesforce/sfdx-lwc-jest');
        return {
            __esModule: true,
            default: createApexTestWireAdapter(jest.fn())
        };
    },
    { virtual: true }
);

jest.mock(
    '@salesforce/apex/AccountContactExplorerController.getContacts',
    () => ({
        __esModule: true,
        default: jest.fn()
    }),
    { virtual: true }
);

const ACCOUNTS = Object.freeze([
    Object.freeze({ Id: '001000000000000001', Name: 'Skynet' }),
    Object.freeze({ Id: '001000000000000002', Name: 'Weyland-Yutani' })
]);

const CONTACTS = Object.freeze([
    Object.freeze({
        Id: '003000000000000002',
        FirstName: 'Grace',
        LastName: 'Hopper',
        Email: 'grace.controller@example.invalid',
        Phone: '415-555-0102'
    }),
    Object.freeze({
        Id: '003000000000000001',
        FirstName: 'Ada',
        LastName: 'Lovelace',
        Email: 'ada.controller@example.invalid',
        Phone: '415-555-0101'
    })
]);

function flushPromises() {
    return Promise.resolve();
}

function deferredPromise() {
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
    element.shadowRoot.querySelector('lightning-combobox').dispatchEvent(
        new CustomEvent('change', {
            detail: { value: accountId }
        })
    );
}

function loadContacts(element) {
    element.shadowRoot.querySelector('lightning-button').click();
}

function alertText(element) {
    return element.shadowRoot.querySelector('[role="alert"]')?.textContent ?? '';
}

describe('controller-owned account contact explorer behavior', () => {
    afterEach(() => {
        while (document.body.firstChild) {
            document.body.removeChild(document.body.firstChild);
        }
        jest.clearAllMocks();
        getContacts.mockReset();
    });

    it('controller: renders account options from the wire adapter', async () => {
        const element = createComponent();

        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        expect(element.shadowRoot.querySelector('lightning-combobox').options).toEqual([
            { label: '-- Select an account --', value: '' },
            { label: ACCOUNTS[0].Name, value: ACCOUNTS[0].Id },
            { label: ACCOUNTS[1].Name, value: ACCOUNTS[1].Id }
        ]);
    });

    it('controller: renders a safe account-wire failure', async () => {
        const element = createComponent();

        getAccounts.error(new Error('SELECT Id FROM Account'));
        await flushPromises();

        expect(alertText(element)).toContain('Accounts could not be loaded.');
        expect(alertText(element)).not.toContain('SELECT Id FROM Account');
    });

    it('controller: enables Load only after account selection', async () => {
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        const button = element.shadowRoot.querySelector('lightning-button');
        expect(button.disabled).toBe(true);

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        expect(button.disabled).toBe(false);
    });

    it('controller: invokes contacts only after the Load action', async () => {
        getContacts.mockResolvedValue(CONTACTS);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        expect(getContacts).not.toHaveBeenCalled();

        loadContacts(element);
        await flushPromises();
        await flushPromises();

        expect(getContacts).toHaveBeenCalledTimes(1);
        expect(getContacts).toHaveBeenCalledWith({ accountId: ACCOUNTS[0].Id });
        expect(element.shadowRoot.querySelector('lightning-datatable').data).toEqual(CONTACTS);
    });

    it('controller: exposes loading state while contacts are pending', async () => {
        const pending = deferredPromise();
        getContacts.mockReturnValue(pending.promise);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        loadContacts(element);
        await flushPromises();

        const spinner = element.shadowRoot.querySelector('lightning-spinner');
        expect(spinner).not.toBeNull();
        expect(spinner.alternativeText).toBe('Loading contacts');
        expect(element.shadowRoot.querySelector('lightning-button').disabled).toBe(true);

        pending.resolve(CONTACTS);
        await flushPromises();
        await flushPromises();

        expect(element.shadowRoot.querySelector('lightning-spinner')).toBeNull();
    });

    it('controller: hides prior empty state during a new request', async () => {
        const pending = deferredPromise();
        getContacts.mockResolvedValueOnce([]).mockReturnValueOnce(pending.promise);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        loadContacts(element);
        await flushPromises();
        await flushPromises();
        expect(element.shadowRoot.querySelector('.empty-state')).not.toBeNull();

        loadContacts(element);
        await flushPromises();

        expect(element.shadowRoot.querySelector('.empty-state')).toBeNull();
        expect(element.shadowRoot.querySelector('lightning-spinner')).not.toBeNull();

        pending.resolve(CONTACTS);
        await flushPromises();
        await flushPromises();
    });

    it('controller: ignores a response made stale by account change', async () => {
        const firstRequest = deferredPromise();
        const secondRequest = deferredPromise();
        getContacts
            .mockReturnValueOnce(firstRequest.promise)
            .mockReturnValueOnce(secondRequest.promise);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        loadContacts(element);
        await flushPromises();

        selectAccount(element, ACCOUNTS[1].Id);
        await flushPromises();
        loadContacts(element);
        await flushPromises();

        secondRequest.resolve(CONTACTS);
        await flushPromises();
        await flushPromises();

        firstRequest.resolve([{ ...CONTACTS[0], FirstName: 'Stale' }]);
        await flushPromises();
        await flushPromises();

        const rendered = element.shadowRoot.querySelector('lightning-datatable').data;
        expect(rendered).toEqual(CONTACTS);
        expect(rendered[0].FirstName).not.toBe('Stale');
    });

    it('controller: ignores an older overlapping Load for the same account', async () => {
        const firstRequest = deferredPromise();
        const secondRequest = deferredPromise();
        getContacts
            .mockReturnValueOnce(firstRequest.promise)
            .mockReturnValueOnce(secondRequest.promise);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        loadContacts(element);
        loadContacts(element);
        await flushPromises();

        expect(getContacts).toHaveBeenCalledTimes(2);
        secondRequest.resolve(CONTACTS);
        await flushPromises();
        await flushPromises();

        firstRequest.resolve([{ ...CONTACTS[0], FirstName: 'Stale' }]);
        await flushPromises();
        await flushPromises();

        const rendered = element.shadowRoot.querySelector('lightning-datatable').data;
        expect(rendered).toEqual(CONTACTS);
        expect(rendered[0].FirstName).not.toBe('Stale');
    });

    it('controller: ignores an older same-account rejection while the current request is pending', async () => {
        const firstRequest = deferredPromise();
        const secondRequest = deferredPromise();
        getContacts
            .mockReturnValueOnce(firstRequest.promise)
            .mockReturnValueOnce(secondRequest.promise);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        loadContacts(element);
        loadContacts(element);
        await flushPromises();

        expect(getContacts).toHaveBeenCalledTimes(2);
        firstRequest.reject(new Error('SELECT Id FROM Contact'));
        await flushPromises();
        await flushPromises();

        expect(alertText(element)).toBe('');
        expect(element.shadowRoot.querySelector('lightning-spinner')).not.toBeNull();

        secondRequest.resolve(CONTACTS);
        await flushPromises();
        await flushPromises();

        expect(element.shadowRoot.querySelector('lightning-datatable').data).toEqual(CONTACTS);
        expect(element.shadowRoot.querySelector('lightning-spinner')).toBeNull();
    });

    it('controller: clears results and disables Load for blank selection', async () => {
        getContacts.mockResolvedValue(CONTACTS);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        loadContacts(element);
        await flushPromises();
        await flushPromises();

        selectAccount(element, '');
        await flushPromises();

        expect(element.shadowRoot.querySelector('lightning-button').disabled).toBe(true);
        expect(element.shadowRoot.querySelector('lightning-datatable')).toBeNull();
        expect(element.shadowRoot.querySelector('lightning-spinner')).toBeNull();
        expect(element.shadowRoot.querySelector('.empty-state')).toBeNull();
        expect(alertText(element)).toContain('Select an account before loading contacts.');
    });

    it('controller: renders empty state only after an empty success', async () => {
        getContacts.mockResolvedValue([]);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        expect(element.shadowRoot.querySelector('.empty-state')).toBeNull();
        loadContacts(element);
        await flushPromises();
        await flushPromises();

        expect(element.shadowRoot.querySelector('.empty-state')).not.toBeNull();
        expect(element.shadowRoot.querySelector('lightning-datatable')).toBeNull();
    });

    it('controller: renders a safe contacts failure', async () => {
        getContacts.mockRejectedValue(new Error('SELECT Id FROM Contact'));
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        loadContacts(element);
        await flushPromises();
        await flushPromises();

        expect(alertText(element)).toContain('Contacts could not be loaded.');
        expect(alertText(element)).not.toContain('SELECT Id FROM Contact');
        expect(element.shadowRoot.querySelector('lightning-datatable')).toBeNull();
    });
});
