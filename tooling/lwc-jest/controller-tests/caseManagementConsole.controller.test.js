import { afterEach, describe, expect, it, jest } from '@jest/globals';
import { createElement } from 'lwc';
import CaseManagementConsole from 'c/caseManagementConsole';
import getAccounts from '@salesforce/apex/CaseManagementConsoleController.getAccounts';
import getCases from '@salesforce/apex/CaseManagementConsoleController.getCases';

jest.mock(
    '@salesforce/apex/CaseManagementConsoleController.getAccounts',
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
    '@salesforce/apex/CaseManagementConsoleController.getCases',
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

const CASES = Object.freeze([
    Object.freeze({
        Id: '500000000000000001',
        CaseNumber: '00001002',
        Subject: 'Login attempts fail',
        Status: 'New',
        Priority: 'High',
        Contact: Object.freeze({ Name: 'Ada Lovelace' })
    }),
    Object.freeze({
        Id: '500000000000000002',
        CaseNumber: '00001001',
        Subject: 'Export button broken',
        Status: 'Working',
        Priority: 'Medium',
        Contact: Object.freeze({ Name: 'Grace Hopper' })
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
    const element = createElement('c-case-management-console', {
        is: CaseManagementConsole
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

function isInteractiveControl(node) {
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
    if (!isInteractiveControl(selector)) {
        throw new Error('account selector semantic hook is not interactive');
    }
    return selector;
}

function statusFilterControl(element) {
    const control = uniqueVisibleElement(
        element,
        '[data-role="status-filter"]',
        'status filter'
    );
    if (!isInteractiveControl(control)) {
        throw new Error('status filter semantic hook is not interactive');
    }
    return control;
}

function loadControl(element) {
    return uniqueVisibleElement(element, '[data-role="load-cases"]', 'load control');
}

function clearControl(element) {
    return uniqueVisibleElement(element, '[data-role="clear-selection"]', 'clear control');
}

function comboboxOptions(control) {
    const propertyOptions = control?.options;
    if (Array.isArray(propertyOptions)) {
        return propertyOptions;
    }
    return Array.from(control?.querySelectorAll?.('option, [role="option"]') ?? []);
}

function optionLabel(option) {
    return String(
        option.label ?? option.getAttribute?.('aria-label') ?? option.textContent ?? ''
    ).trim();
}

function optionValue(option) {
    if (option.value !== undefined && option.value !== null) {
        return String(option.value);
    }
    const attributeValue = option.getAttribute?.('value');
    return attributeValue === undefined || attributeValue === null
        ? ''
        : String(attributeValue);
}

function accountOptions(element) {
    return comboboxOptions(accountSelector(element));
}

function statusOptions(element) {
    return comboboxOptions(statusFilterControl(element));
}

function controlIsDisabled(control) {
    return (
        control?.disabled === true ||
        String(control?.getAttribute?.('aria-disabled') ?? '').toLowerCase() === 'true'
    );
}

function selectAccount(element, accountId) {
    const selector = accountSelector(element);
    const ariaOptions = comboboxOptions(selector).filter(
        (option) => option.getAttribute?.('role') === 'option'
    );
    if (ariaOptions.length > 0) {
        const account = ACCOUNTS.find((candidate) => candidate.Id === accountId);
        const ariaOption = ariaOptions.find((option) => {
            const label = optionLabel(option);
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

function loadCases(element) {
    loadControl(element).click();
}

function clearSelection(element) {
    clearControl(element).click();
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

function caseResults(element) {
    const candidates = Array.from(
        element.shadowRoot.querySelectorAll('[data-role="case-results"]')
    ).filter((node) => isSemanticallyVisible(node));
    if (candidates.length > 1) {
        throw new Error('case results have duplicate visible semantic hooks');
    }
    return candidates[0];
}

function structuredCaseRows(element) {
    const result = caseResults(element);
    if (result === undefined) {
        return [];
    }
    const requiredFields = ['CaseNumber', 'Subject', 'Status', 'Priority', 'ContactName'];
    const tagName = String(result.tagName ?? '').toLowerCase();
    const columns = Array.isArray(result.columns) ? result.columns : [];
    const visibleFields = new Set(
        columns
            .map((column) => column?.fieldName)
            .filter((fieldName) => typeof fieldName === 'string')
    );
    const rows = Array.isArray(result.data) ? result.data : [];
    const isUsableStructuredTable =
        tagName === 'lightning-datatable' &&
        rows.length > 0 &&
        requiredFields.every((field) => visibleFields.has(field));
    if (!isUsableStructuredTable) {
        return [];
    }
    const keyField = String(result.keyField ?? result.getAttribute?.('key-field') ?? '').trim();
    if (keyField === '') {
        throw new Error('lightning-datatable must declare a key-field');
    }
    const keys = rows.map((row) => row?.[keyField]);
    if (
        keys.some(
            (key) => key === undefined || key === null || String(key).trim() === ''
        )
    ) {
        throw new Error('every lightning-datatable row must retain its key-field value');
    }
    const uniqueKeys = new Set(keys.map((key) => `${typeof key}:${String(key)}`));
    if (uniqueKeys.size !== keys.length) {
        throw new Error('lightning-datatable key-field values must be unique');
    }
    return rows;
}

function hasCaseResults(element) {
    return structuredCaseRows(element).length > 0;
}

function emptyStateVisible(element) {
    return Array.from(element.shadowRoot.querySelectorAll('[data-state="empty"]')).some(
        (node) => isSemanticallyVisible(node)
    );
}

function guidanceVisible(element) {
    return Array.from(element.shadowRoot.querySelectorAll('[data-state="guidance"]')).some(
        (node) => isSemanticallyVisible(node)
    );
}

describe('controller-owned case management console behavior', () => {
    afterEach(() => {
        while (document.body.firstChild) {
            document.body.removeChild(document.body.firstChild);
        }
        jest.clearAllMocks();
        getAccounts.mockReset();
        getCases.mockReset();
    });

    it('controller: lists account options with a blank choice from the wire adapter', async () => {
        const element = createComponent();

        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        const options = accountOptions(element);
        expect(options.some((option) => optionValue(option) === '')).toBe(true);
        for (const account of ACCOUNTS) {
            expect(options.some((option) => optionLabel(option).includes(account.Name))).toBe(
                true
            );
        }
    });

    it('controller: defaults the status filter to Open with all choices', async () => {
        getCases.mockResolvedValue([]);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        const statusValues = statusOptions(element).map((option) => optionValue(option));
        for (const expected of ['OPEN', 'CLOSED', 'ALL']) {
            expect(statusValues).toContain(expected);
        }

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        loadCases(element);
        await flushPromises();
        await flushPromises();

        expect(getCases).toHaveBeenCalledTimes(1);
        expect(getCases).toHaveBeenCalledWith({
            accountId: ACCOUNTS[0].Id,
            statusFilter: 'OPEN'
        });
    });

    it('controller: renders scoped case results in a keyed datatable', async () => {
        getCases.mockResolvedValue(CASES);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        loadCases(element);
        await flushPromises();
        await flushPromises();

        const rows = structuredCaseRows(element);
        expect(rows).toHaveLength(CASES.length);
        for (const expected of CASES) {
            expect(
                rows.some(
                    (row) =>
                        String(row.CaseNumber) === expected.CaseNumber &&
                        String(row.ContactName) === expected.Contact.Name
                )
            ).toBe(true);
        }
    });

    it('controller: warns and issues no query for a blank account', async () => {
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, '');
        await flushPromises();

        expect(alertText(element)).not.toBe('');

        loadCases(element);
        await flushPromises();

        expect(getCases).not.toHaveBeenCalled();
        expect(hasCaseResults(element)).toBe(false);
    });

    it('controller: renders empty state only after an empty success', async () => {
        getCases.mockResolvedValue([]);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        expect(emptyStateVisible(element)).toBe(false);

        loadCases(element);
        await flushPromises();
        await flushPromises();

        expect(emptyStateVisible(element)).toBe(true);
        expect(hasCaseResults(element)).toBe(false);
        expect(caseResults(element)).toBeUndefined();
    });

    it('controller: exposes loading state while cases are pending', async () => {
        const pending = deferredPromise();
        getCases.mockReturnValue(pending.promise);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        loadCases(element);
        await flushPromises();

        expect(loadingIndicator(element)).toBeDefined();
        expect(caseResults(element)).toBeUndefined();

        pending.resolve(CASES);
        await flushPromises();
        await flushPromises();

        expect(loadingIndicator(element)).toBeUndefined();
    });

    it('controller: renders a safe case-load failure', async () => {
        getCases.mockRejectedValue(new Error('SELECT Id FROM Case WHERE AccountId = :accountId'));
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        loadCases(element);
        await flushPromises();
        await flushPromises();

        expect(alertText(element)).not.toBe('');
        expect(alertText(element)).not.toContain('SELECT Id FROM Case');
        expect(hasCaseResults(element)).toBe(false);
        expect(caseResults(element)).toBeUndefined();
    });

    it('controller: ignores a response made stale by account change', async () => {
        const firstRequest = deferredPromise();
        getCases.mockReturnValueOnce(firstRequest.promise);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        loadCases(element);
        await flushPromises();

        selectAccount(element, ACCOUNTS[1].Id);
        await flushPromises();

        firstRequest.resolve([
            { ...CASES[0], Subject: 'StaleSubjectValue', Contact: { Name: 'Stale Contact' } }
        ]);
        await flushPromises();
        await flushPromises();

        expect(getCases).toHaveBeenCalledTimes(1);
        expect(hasCaseResults(element)).toBe(false);
        expect(caseResults(element)).toBeUndefined();
        expect(element.shadowRoot.textContent ?? '').not.toContain('StaleSubjectValue');
        expect(loadingIndicator(element)).toBeUndefined();
    });

    it('controller: clears results and prompts to reselect on clear', async () => {
        getCases.mockResolvedValue(CASES);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        loadCases(element);
        await flushPromises();
        await flushPromises();

        expect(hasCaseResults(element)).toBe(true);

        clearSelection(element);
        await flushPromises();

        expect(hasCaseResults(element)).toBe(false);
        expect(caseResults(element)).toBeUndefined();
        expect(loadingIndicator(element)).toBeUndefined();
        expect(emptyStateVisible(element)).toBe(false);
        expect(controlIsDisabled(loadControl(element))).toBe(true);
        expect(alertText(element) !== '' || guidanceVisible(element)).toBe(true);
    });

    it('controller: enables Load only after account selection', async () => {
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        expect(controlIsDisabled(loadControl(element))).toBe(true);

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        expect(controlIsDisabled(loadControl(element))).toBe(false);
    });

    it('controller: requests cases only after the Load action', async () => {
        getCases.mockResolvedValue(CASES);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();

        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        expect(getCases).not.toHaveBeenCalled();

        loadCases(element);
        await flushPromises();
        await flushPromises();

        expect(getCases).toHaveBeenCalledTimes(1);
        expect(getCases).toHaveBeenCalledWith({
            accountId: ACCOUNTS[0].Id,
            statusFilter: 'OPEN'
        });
        expect(hasCaseResults(element)).toBe(true);
    });

    it('controller: renders a safe account-wire failure', async () => {
        const technicalError = new Error('SELECT Id, Name FROM Account WITH USER_MODE');
        const element = createComponent();

        getAccounts.error(technicalError);
        await flushPromises();

        expect(alertText(element)).not.toBe('');
        expect(alertText(element)).not.toContain('SELECT Id, Name FROM Account');
    });
});
