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

function selectStatus(element, statusValue) {
    const control = statusFilterControl(element);
    const ariaOptions = comboboxOptions(control).filter(
        (option) => option.getAttribute?.('role') === 'option'
    );
    if (ariaOptions.length > 0) {
        const ariaOption = ariaOptions.find((option) => {
            return (
                optionValue(option).toUpperCase() === statusValue ||
                optionLabel(option).toUpperCase() === statusValue
            );
        });
        if (ariaOption === undefined) {
            throw new Error('accessible status option is unavailable');
        }
        ariaOption.click();
        return;
    }
    control.value = statusValue;
    control.dispatchEvent(
        new CustomEvent('change', {
            bubbles: true,
            composed: true,
            detail: { value: statusValue }
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
    const requiredFields = ['CaseNumber', 'Subject', 'Status', 'Priority'];
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
                    .map((column) => String(column?.fieldName ?? '').toLowerCase())
                    .filter((fieldName) => fieldName !== '')
            );
            const rows = Array.isArray(node.data) ? node.data : [];
            const isUsableStructuredTable =
                isStructuredTable &&
                isSemanticallyVisible(node) &&
                rows.length > 0 &&
                requiredFields.every((field) => visibleFields.has(field.toLowerCase()));
            if (tagName === 'lightning-datatable' && isUsableStructuredTable) {
                const keyField = String(
                    node.keyField ?? node.getAttribute?.('key-field') ?? ''
                ).trim();
                if (keyField === '') {
                    throw new Error('lightning-datatable must declare a key-field');
                }
                const keys = rows.map((row) => row?.[keyField]);
                if (
                    keys.some(
                        (key) =>
                            key === undefined ||
                            key === null ||
                            String(key).trim() === ''
                    )
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

function rowField(row, fieldName) {
    let current = row;
    for (const segment of fieldName.split('.')) {
        if (current === null || typeof current !== 'object') {
            return undefined;
        }
        const key = Object.keys(current).find(
            (candidate) => candidate.toLowerCase() === segment.toLowerCase()
        );
        if (key === undefined) {
            return undefined;
        }
        current = current[key];
    }
    return current;
}

function contactNameValues(row) {
    const values = [
        rowField(row, 'Contact.Name'),
        rowField(row, 'ContactName'),
        rowField(row, 'RequesterName'),
        rowField(row, 'CustomerName')
    ];
    for (const [key, value] of Object.entries(row ?? {})) {
        const semanticKey = key.toLowerCase().replace(/[^a-z0-9]/g, '');
        if (!/(?:contact|requester|customer)/.test(semanticKey)) {
            continue;
        }
        if (value !== null && typeof value === 'object') {
            values.push(rowField(value, 'Name'));
        } else {
            values.push(value);
        }
    }
    return values
        .filter((value) => value !== undefined && value !== null)
        .map((value) => String(value).trim())
        .filter((value) => value !== '');
}

function caseVisible(element, expected) {
    const normalize = (value) => String(value ?? '').trim().toLowerCase();
    const requiredFields = ['CaseNumber', 'Subject', 'Status', 'Priority'];
    if (
        structuredCaseRows(element).some(
            (row) =>
                requiredFields.every((field) =>
                    normalize(rowField(row, field)).includes(normalize(expected[field]))
                ) &&
                contactNameValues(row).some((value) =>
                    normalize(value).includes(normalize(expected.Contact.Name))
                )
        )
    ) {
        return true;
    }
    const result = caseResults(element);
    if (result === undefined) {
        return false;
    }
    const accessibleRows = Array.from(
        result.querySelectorAll?.(
            '[role="row"], tr, [role="article"], article, [role="listitem"], li, '
                + '[data-row-key], [data-record-id]'
        ) ?? []
    ).filter((node) => isSemanticallyVisible(node));
    const visibleContainers = accessibleRows.length > 0 ? accessibleRows : [result];
    return visibleContainers.some((container) => {
        const text = normalize(visibleTextContent(container));
        return (
            requiredFields.every((field) => text.includes(normalize(expected[field]))) &&
            text.includes(normalize(expected.Contact.Name))
        );
    });
}

function hasCaseResults(element) {
    return (
        structuredCaseRows(element).length > 0 ||
        CASES.some((caseRecord) => caseVisible(element, caseRecord))
    );
}

function emptyStateVisible(element) {
    return Array.from(element.shadowRoot.querySelectorAll('[data-state="empty"]')).some(
        (node) => isSemanticallyVisible(node)
    );
}

function guidanceVisible(element) {
    // Guidance markup and wording are candidate-owned. The public contract only
    // requires a visible, nonempty accessible alert before an Account is chosen.
    return alertText(element) !== '';
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

    it('controller: renders initial guidance before account selection', async () => {
        const element = createComponent();
        await flushPromises();

        expect(guidanceVisible(element)).toBe(true);
        expect(controlIsDisabled(loadControl(element))).toBe(true);
        expect(caseResults(element)).toBeUndefined();
        expect(emptyStateVisible(element)).toBe(false);
        expect(loadingIndicator(element)).toBeUndefined();
        expect(getCases).not.toHaveBeenCalled();
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

    it('controller: requests closed cases when Closed is selected', async () => {
        getCases.mockResolvedValue([]);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        selectStatus(element, 'CLOSED');
        await flushPromises();

        loadCases(element);
        await flushPromises();
        await flushPromises();

        expect(getCases).toHaveBeenCalledTimes(1);
        expect(getCases).toHaveBeenCalledWith({
            accountId: ACCOUNTS[0].Id,
            statusFilter: 'CLOSED'
        });
    });

    it('controller: requests all cases when All is selected', async () => {
        getCases.mockResolvedValue([]);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        selectStatus(element, 'ALL');
        await flushPromises();

        loadCases(element);
        await flushPromises();
        await flushPromises();

        expect(getCases).toHaveBeenCalledTimes(1);
        expect(getCases).toHaveBeenCalledWith({
            accountId: ACCOUNTS[0].Id,
            statusFilter: 'ALL'
        });
    });

    it('controller: renders scoped case results with stable keys', async () => {
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
        for (const expected of CASES) {
            expect(caseVisible(element, expected)).toBe(true);
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

    it('controller: resets completed and error state when status changes', async () => {
        getCases
            .mockResolvedValueOnce(CASES)
            .mockResolvedValueOnce([])
            .mockRejectedValueOnce(new Error('SELECT Id FROM Case'));
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        loadCases(element);
        await flushPromises();
        await flushPromises();
        expect(hasCaseResults(element)).toBe(true);

        selectStatus(element, 'CLOSED');
        await flushPromises();
        expect(hasCaseResults(element)).toBe(false);
        expect(caseResults(element)).toBeUndefined();
        expect(emptyStateVisible(element)).toBe(false);
        expect(loadingIndicator(element)).toBeUndefined();
        expect(getCases).toHaveBeenCalledTimes(1);

        loadCases(element);
        await flushPromises();
        await flushPromises();
        expect(getCases).toHaveBeenLastCalledWith({
            accountId: ACCOUNTS[0].Id,
            statusFilter: 'CLOSED'
        });
        expect(emptyStateVisible(element)).toBe(true);

        selectStatus(element, 'ALL');
        await flushPromises();
        expect(emptyStateVisible(element)).toBe(false);
        expect(caseResults(element)).toBeUndefined();
        expect(getCases).toHaveBeenCalledTimes(2);

        loadCases(element);
        await flushPromises();
        await flushPromises();
        expect(getCases).toHaveBeenLastCalledWith({
            accountId: ACCOUNTS[0].Id,
            statusFilter: 'ALL'
        });
        const caseLoadErrorText = alertText(element);
        expect(caseLoadErrorText).not.toBe('');
        expect(caseLoadErrorText).not.toContain('SELECT Id FROM Case');

        selectStatus(element, 'OPEN');
        await flushPromises();
        expect(hasCaseResults(element)).toBe(false);
        expect(caseResults(element)).toBeUndefined();
        expect(emptyStateVisible(element)).toBe(false);
        expect(loadingIndicator(element)).toBeUndefined();
        expect(alertText(element)).not.toBe(caseLoadErrorText);
        expect(alertText(element)).not.toContain('SELECT Id FROM Case');
        expect(getCases).toHaveBeenCalledTimes(3);
    });

    it('controller: ignores a response made stale by status change', async () => {
        const firstRequest = deferredPromise();
        getCases.mockReturnValueOnce(firstRequest.promise).mockResolvedValueOnce([]);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();

        loadCases(element);
        await flushPromises();
        expect(getCases).toHaveBeenCalledWith({
            accountId: ACCOUNTS[0].Id,
            statusFilter: 'OPEN'
        });
        expect(loadingIndicator(element)).toBeDefined();

        selectStatus(element, 'CLOSED');
        await flushPromises();
        expect(loadingIndicator(element)).toBeUndefined();
        expect(caseResults(element)).toBeUndefined();
        expect(getCases).toHaveBeenCalledTimes(1);

        firstRequest.resolve([
            { ...CASES[0], Subject: 'StaleStatusValue', Contact: { Name: 'Stale Contact' } }
        ]);
        await flushPromises();
        await flushPromises();
        expect(hasCaseResults(element)).toBe(false);
        expect(caseResults(element)).toBeUndefined();
        expect(element.shadowRoot.textContent ?? '').not.toContain('StaleStatusValue');
        expect(loadingIndicator(element)).toBeUndefined();

        loadCases(element);
        await flushPromises();
        await flushPromises();
        expect(getCases).toHaveBeenCalledTimes(2);
        expect(getCases).toHaveBeenLastCalledWith({
            accountId: ACCOUNTS[0].Id,
            statusFilter: 'CLOSED'
        });
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

    it('controller: clears completed case state when the account wire later fails', async () => {
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

        getAccounts.error(new Error('SELECT Id, Name FROM Account WITH USER_MODE'));
        await flushPromises();

        expect(alertText(element)).not.toBe('');
        expect(alertText(element)).not.toContain('SELECT Id, Name FROM Account');
        expect(controlIsDisabled(loadControl(element))).toBe(true);
        expect(caseResults(element)).toBeUndefined();
        expect(hasCaseResults(element)).toBe(false);
        expect(emptyStateVisible(element)).toBe(false);
        expect(loadingIndicator(element)).toBeUndefined();
    });

    it('controller: invalidates pending case work when the account wire fails', async () => {
        const lateSuccess = deferredPromise();
        const lateFailure = deferredPromise();
        getCases
            .mockReturnValueOnce(lateSuccess.promise)
            .mockReturnValueOnce(lateFailure.promise);
        const element = createComponent();
        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[0].Id);
        await flushPromises();
        loadCases(element);
        await flushPromises();

        getAccounts.error(new Error('Account wire technical detail'));
        await flushPromises();
        const successBoundaryAlert = alertText(element);
        expect(successBoundaryAlert).not.toBe('');

        lateSuccess.resolve([
            {
                ...CASES[0],
                Subject: 'LateAccountWireSuccess',
                Contact: { Name: 'Stale Contact' }
            }
        ]);
        await flushPromises();
        await flushPromises();
        expect(alertText(element)).toBe(successBoundaryAlert);
        expect(element.shadowRoot.textContent ?? '').not.toContain('LateAccountWireSuccess');
        expect(caseResults(element)).toBeUndefined();
        expect(emptyStateVisible(element)).toBe(false);
        expect(loadingIndicator(element)).toBeUndefined();
        expect(controlIsDisabled(loadControl(element))).toBe(true);

        getAccounts.emit(ACCOUNTS);
        await flushPromises();
        selectAccount(element, ACCOUNTS[1].Id);
        await flushPromises();
        loadCases(element);
        await flushPromises();

        getAccounts.error(new Error('Second account wire technical detail'));
        await flushPromises();
        const failureBoundaryAlert = alertText(element);
        expect(failureBoundaryAlert).not.toBe('');

        lateFailure.reject(new Error('LateAccountWireFailure'));
        await flushPromises();
        await flushPromises();
        expect(alertText(element)).toBe(failureBoundaryAlert);
        expect(alertText(element)).not.toContain('LateAccountWireFailure');
        expect(caseResults(element)).toBeUndefined();
        expect(emptyStateVisible(element)).toBe(false);
        expect(loadingIndicator(element)).toBeUndefined();
        expect(controlIsDisabled(loadControl(element))).toBe(true);
        expect(getCases).toHaveBeenCalledTimes(2);
    });
});
