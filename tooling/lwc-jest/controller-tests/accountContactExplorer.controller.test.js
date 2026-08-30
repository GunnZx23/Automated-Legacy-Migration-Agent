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

async function flushPromises() {
    await Promise.resolve();
    await Promise.resolve();
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

function renderedElements(element) {
    return Array.from(element.shadowRoot.querySelectorAll('*'));
}

function isSemanticallyVisible(node) {
    for (let current = node; current?.nodeType === 1; current = current.parentElement) {
        if (
            current.hidden === true ||
            current.hasAttribute?.('hidden') ||
            String(current.getAttribute?.('aria-hidden') ?? '').toLowerCase() === 'true'
        ) {
            return false;
        }
        const inlineStyle = String(current.getAttribute?.('style') ?? '');
        if (
            /(?:^|;)\s*display\s*:\s*none\b/i.test(inlineStyle) ||
            /(?:^|;)\s*visibility\s*:\s*hidden\b/i.test(inlineStyle)
        ) {
            return false;
        }
        const computed = getComputedStyle(current);
        if (computed.display === 'none' || computed.visibility === 'hidden') {
            return false;
        }
    }
    return true;
}

function uniqueVisibleElement(element, selector, label) {
    const candidates = Array.from(element.shadowRoot.querySelectorAll(selector));
    if (candidates.length !== 1) {
        throw new Error(`${label} must have exactly one rendered semantic hook`);
    }
    if (!isSemanticallyVisible(candidates[0])) {
        throw new Error(`${label} semantic hook is hidden`);
    }
    return candidates[0];
}

function visibleTextContent(node) {
    return Array.from(node?.childNodes ?? [])
        .map((child) => {
            if (child.nodeType === 3) {
                return child.textContent ?? '';
            }
            return child.nodeType === 1 && isSemanticallyVisible(child)
                ? visibleTextContent(child)
                : '';
        })
        .join(' ');
}

function isInteractiveAccountSelector(node) {
    const tagName = String(node?.tagName ?? '').toLowerCase();
    const role = String(node?.getAttribute?.('role') ?? '').toLowerCase();
    return (
        ['lightning-combobox', 'lightning-radio-group', 'select'].includes(tagName) ||
        ['combobox', 'listbox', 'radiogroup'].includes(role)
    );
}

function accountSelector(element) {
    const selector = uniqueVisibleElement(
        element,
        '[data-role="account-selector"]',
        'account selector'
    );
    if (!isInteractiveAccountSelector(selector)) {
        throw new Error('account selector semantic hook is not interactive');
    }
    return selector;
}

function loadControl(element) {
    return uniqueVisibleElement(
        element,
        '[data-role="load-contacts"]',
        'load control'
    );
}

function accountOptionNodes(element) {
    const selector = accountSelector(element);
    const propertyOptions = selector?.options;
    if (propertyOptions !== undefined && propertyOptions !== null) {
        return Array.from(propertyOptions);
    }
    return Array.from(selector?.querySelectorAll?.('option, [role="option"]') ?? []);
}

function accountOptionLabel(option) {
    return String(
        option.label ?? option.getAttribute?.('aria-label') ?? option.textContent ?? ''
    ).trim();
}

function controlIsDisabled(control) {
    return (
        control?.disabled === true ||
        String(control?.getAttribute?.('aria-disabled') ?? '').toLowerCase() === 'true'
    );
}

function selectAccount(element, accountId) {
    const selector = accountSelector(element);
    const ariaOptions = accountOptionNodes(element).filter(
        (option) => option.getAttribute?.('role') === 'option'
    );
    if (ariaOptions.length > 0) {
        const account = ACCOUNTS.find((candidate) => candidate.Id === accountId);
        const ariaOption = ariaOptions.find((option) => {
            const label = accountOptionLabel(option);
            return account === undefined
                ? !ACCOUNTS.some((candidate) => label.includes(candidate.Name))
                : label.includes(account.Name);
        });
        if (ariaOption === undefined) {
            throw new Error('accessible account option is unavailable');
        }
        ariaOption.click();
        return;
    }
    selector.value = accountId;
    selector.dispatchEvent(
        new CustomEvent('change', {
            bubbles: true,
            composed: true,
            detail: { value: accountId }
        })
    );
}

function loadContacts(element) {
    loadControl(element).click();
}

function alertText(element) {
    return renderedElements(element)
        .filter(
            (node) =>
                node.getAttribute?.('role') === 'alert' && isSemanticallyVisible(node)
        )
        .map((node) => node.textContent ?? '')
        .join(' ')
        .trim();
}

function loadingIndicator(element) {
    const candidates = Array.from(
        element.shadowRoot.querySelectorAll('[data-state="loading"]')
    ).filter((node) => isSemanticallyVisible(node));
    if (candidates.length > 1) {
        throw new Error('loading state has duplicate visible semantic hooks');
    }
    return candidates[0];
}

function accountOptions(element) {
    return accountOptionNodes(element).map((option) => accountOptionLabel(option));
}

function contactResult(element) {
    const candidates = Array.from(
        element.shadowRoot.querySelectorAll('[data-role="contact-results"]')
    ).filter((node) => isSemanticallyVisible(node));
    if (candidates.length > 1) {
        throw new Error('contact results have duplicate visible semantic hooks');
    }
    return candidates[0];
}

function structuredContactRows(element) {
    const result = contactResult(element);
    if (result === undefined) {
        return [];
    }
    const requiredFields = ['FirstName', 'LastName', 'Email', 'Phone'];
    return [result, ...Array.from(result.querySelectorAll?.('*') ?? [])].flatMap(
        (node) => {
            const tagName = String(node.tagName ?? '').toLowerCase();
            const role = String(node.getAttribute?.('role') ?? '').toLowerCase();
            const isStructuredTable =
                tagName === 'lightning-datatable' ||
                tagName === 'table' ||
                role === 'grid' ||
                role === 'table';
            const columns = Array.isArray(node.columns) ? node.columns : [];
            const visibleFields = new Set(
                columns
                    .map((column) => column?.fieldName)
                    .filter((fieldName) => typeof fieldName === 'string')
            );
            const rows = Array.isArray(node.data) ? node.data : [];
            const isUsableStructuredTable =
                isStructuredTable &&
                isSemanticallyVisible(node) &&
                rows.length > 0 &&
                requiredFields.every((field) => visibleFields.has(field));
            if (tagName === 'lightning-datatable' && isUsableStructuredTable) {
                const keyField = String(
                    node.keyField ?? node.getAttribute?.('key-field') ?? ''
                ).trim();
                if (keyField === '') {
                    throw new Error('lightning-datatable must declare a key-field');
                }
                const keys = rows.map((row) => row?.[keyField]);
                if (
                    keys.some((key) => {
                        return (
                            key === undefined ||
                            key === null ||
                            String(key).trim() === ''
                        );
                    })
                ) {
                    throw new Error(
                        'every lightning-datatable row must retain its key-field value'
                    );
                }
                const uniqueKeys = new Set(
                    keys.map((key) => `${typeof key}:${String(key)}`)
                );
                if (uniqueKeys.size !== keys.length) {
                    throw new Error('lightning-datatable key-field values must be unique');
                }
            }
            return isUsableStructuredTable ? rows : [];
        }
    );
}

function contactVisible(element, contact) {
    const visibleFields = ['FirstName', 'LastName', 'Email', 'Phone'];
    const normalizeField = (field, value) => {
        const text = String(value ?? '').trim();
        if (field === 'Email') {
            return text.toLowerCase();
        }
        if (field === 'Phone') {
            return text.replace(/[^0-9]/g, '');
        }
        return text;
    };
    if (
        structuredContactRows(element).some((row) =>
            visibleFields.every((field) =>
                normalizeField(field, row[field]).includes(
                    normalizeField(field, contact[field])
                )
            )
        )
    ) {
        return true;
    }
    const result = contactResult(element);
    if (result === undefined) {
        return false;
    }
    const accessibleRows = Array.from(
        result.querySelectorAll?.(
            '[role="row"], tr, [role="article"], article, [role="listitem"], li'
        ) ?? []
    ).filter((node) => isSemanticallyVisible(node));
    const visibleContainers = accessibleRows.length > 0 ? accessibleRows : [result];
    return visibleContainers.some((container) => {
        const text = visibleTextContent(container);
        return visibleFields.every((field) =>
            normalizeField(field, text).includes(normalizeField(field, contact[field]))
        );
    });
}

function hasContactResults(element) {
    return (
        structuredContactRows(element).length > 0 ||
        CONTACTS.some((contact) => contactVisible(element, contact))
    );
}

function emptyStateVisible(element) {
    return Array.from(element.shadowRoot.querySelectorAll('[data-state="empty"]')).some(
        (node) => isSemanticallyVisible(node)
    );
}

function arrangeAccountsSuccess(accounts = ACCOUNTS) {
    getAccounts.mockResolvedValue(accounts);
}

function arrangeAccountsFailure(error) {
    getAccounts.mockRejectedValue(error);
}

describe('controller-owned account contact explorer behavior', () => {
    afterEach(() => {
        while (document.body.firstChild) {
            document.body.removeChild(document.body.firstChild);
        }
        jest.clearAllMocks();
        getAccounts.mockReset();
        getContacts.mockReset();
    });

    it('controller: renders account options from the wire adapter', async () => {
        arrangeAccountsSuccess();
        const element = createComponent();

        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        const options = accountOptions(element);
        expect(
            options.some(
                (label) => !ACCOUNTS.some((account) => label.includes(account.Name))
            )
        ).toBe(true);
        for (const account of ACCOUNTS) {
            expect(options.some((label) => label.includes(account.Name))).toBe(true);
        }
    });

    it('controller: renders a safe account-wire failure', async () => {
        const technicalError = new Error('SELECT Id FROM Account');
        arrangeAccountsFailure(technicalError);
        const element = createComponent();

        getAccounts.error(technicalError);
        await flushPromises();

        expect(alertText(element)).not.toBe('');
        expect(alertText(element)).not.toContain('SELECT Id FROM Account');
    });

    it('controller: enables Load only after account selection', async () => {
        arrangeAccountsSuccess();
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        const button = loadControl(element);
        expect(controlIsDisabled(button)).toBe(true);

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        expect(controlIsDisabled(button)).toBe(false);
    });

    it('controller: invokes contacts only after the Load action', async () => {
        arrangeAccountsSuccess();
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
        expect(CONTACTS.every((contact) => contactVisible(element, contact))).toBe(true);
    });

    it('controller: exposes loading state while contacts are pending', async () => {
        arrangeAccountsSuccess();
        const pending = deferredPromise();
        getContacts.mockReturnValue(pending.promise);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        loadContacts(element);
        await flushPromises();

        expect(loadingIndicator(element)).toBeDefined();
        expect(contactResult(element)).toBeUndefined();

        pending.resolve(CONTACTS);
        await flushPromises();
        await flushPromises();

        expect(loadingIndicator(element)).toBeUndefined();
    });

    it('controller: ignores a response made stale by account change', async () => {
        arrangeAccountsSuccess();
        const firstRequest = deferredPromise();
        getContacts.mockReturnValueOnce(firstRequest.promise);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        loadContacts(element);
        await flushPromises();

        selectAccount(element, ACCOUNTS[1].Id);
        await flushPromises();

        firstRequest.resolve([{ ...CONTACTS[0], FirstName: 'Stale' }]);
        await flushPromises();
        await flushPromises();

        expect(getContacts).toHaveBeenCalledTimes(1);
        expect(hasContactResults(element)).toBe(false);
        expect(contactResult(element)).toBeUndefined();
        expect(element.shadowRoot.textContent ?? '').not.toContain('Stale');
        expect(loadingIndicator(element)).toBeUndefined();
    });

    it('controller: resets completed and error state on nonblank account change', async () => {
        arrangeAccountsSuccess();
        getContacts
            .mockResolvedValueOnce(CONTACTS)
            .mockResolvedValueOnce([])
            .mockRejectedValueOnce(new Error('SELECT Id FROM Contact'));
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        loadContacts(element);
        await flushPromises();
        await flushPromises();
        expect(hasContactResults(element)).toBe(true);

        selectAccount(element, ACCOUNTS[1].Id);
        await flushPromises();
        expect(hasContactResults(element)).toBe(false);
        expect(contactResult(element)).toBeUndefined();
        expect(emptyStateVisible(element)).toBe(false);
        expect(loadingIndicator(element)).toBeUndefined();
        expect(alertText(element)).toBe('');
        expect(getContacts).toHaveBeenCalledTimes(1);

        loadContacts(element);
        await flushPromises();
        await flushPromises();
        expect(emptyStateVisible(element)).toBe(true);

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        expect(emptyStateVisible(element)).toBe(false);
        expect(contactResult(element)).toBeUndefined();
        expect(alertText(element)).toBe('');
        expect(getContacts).toHaveBeenCalledTimes(2);

        loadContacts(element);
        await flushPromises();
        await flushPromises();
        expect(alertText(element)).not.toBe('');
        expect(alertText(element)).not.toContain('SELECT Id FROM Contact');

        selectAccount(element, ACCOUNTS[1].Id);
        await flushPromises();
        expect(hasContactResults(element)).toBe(false);
        expect(contactResult(element)).toBeUndefined();
        expect(emptyStateVisible(element)).toBe(false);
        expect(loadingIndicator(element)).toBeUndefined();
        expect(alertText(element)).toBe('');
        expect(getContacts).toHaveBeenCalledTimes(3);
    });

    it('controller: clears results and disables Load for blank selection', async () => {
        arrangeAccountsSuccess();
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

        expect(controlIsDisabled(loadControl(element))).toBe(true);
        expect(hasContactResults(element)).toBe(false);
        expect(contactResult(element)).toBeUndefined();
        expect(loadingIndicator(element)).toBeUndefined();
        expect(emptyStateVisible(element)).toBe(false);
        expect(alertText(element)).not.toBe('');
    });

    it('controller: renders empty state only after an empty success', async () => {
        arrangeAccountsSuccess();
        getContacts.mockResolvedValue([]);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        expect(emptyStateVisible(element)).toBe(false);
        loadContacts(element);
        await flushPromises();
        await flushPromises();

        expect(emptyStateVisible(element)).toBe(true);
        expect(hasContactResults(element)).toBe(false);
        expect(contactResult(element)).toBeUndefined();
    });

    it('controller: renders a safe contacts failure', async () => {
        arrangeAccountsSuccess();
        getContacts.mockRejectedValue(new Error('SELECT Id FROM Contact'));
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        loadContacts(element);
        await flushPromises();
        await flushPromises();

        expect(alertText(element)).not.toBe('');
        expect(alertText(element)).not.toContain('SELECT Id FROM Contact');
        expect(hasContactResults(element)).toBe(false);
        expect(contactResult(element)).toBeUndefined();
    });
});
