"""Synthetic Salesforce candidate construction for test doubles.

The production fixture retains only legacy input. Tests that need a model-like
candidate construct one in memory here; product code never imports this module,
and validators never compare generated output with these bytes.
"""

from __future__ import annotations

from textwrap import dedent

from legacy_migration_agent.platforms.local_checks import (
    CASE_AGENT_OUTPUT_PATHS,
    CASE_CONTROLLER_METADATA_PATH,
    CASE_CONTROLLER_PATH,
    CASE_CONTROLLER_TEST_METADATA_PATH,
    CASE_CONTROLLER_TEST_PATH,
    CASE_LWC_CSS_PATH,
    CASE_LWC_HTML_PATH,
    CASE_LWC_JAVASCRIPT_PATH,
    CASE_LWC_METADATA_PATH,
    CASE_LWC_TEST_PATH,
    CASE_MANIFEST_PATH,
    CASE_PERMISSION_SET_PATH,
    CONTROLLER_METADATA_PATH,
    CONTROLLER_PATH,
    CONTROLLER_TEST_METADATA_PATH,
    CONTROLLER_TEST_PATH,
    LWC_CSS_PATH,
    LWC_HTML_PATH,
    LWC_JAVASCRIPT_PATH,
    LWC_METADATA_PATH,
    LWC_TEST_PATH,
    MANIFEST_PATH,
    PERMISSION_SET_PATH,
    SALESFORCE_AGENT_OUTPUT_PATHS,
)


def _source(value: str) -> bytes:
    return dedent(value).lstrip("\n").encode("utf-8")


def salesforce_candidate_outputs() -> dict[str, bytes]:
    """Return a fresh, complete synthetic candidate without reading a golden tree."""

    apex_metadata = _source(
        """
        <?xml version="1.0" encoding="UTF-8"?>
        <ApexClass xmlns="http://soap.sforce.com/2006/04/metadata">
            <apiVersion>67.0</apiVersion>
            <status>Active</status>
        </ApexClass>
        """
    )
    outputs = {
        MANIFEST_PATH: _source(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <Package xmlns="http://soap.sforce.com/2006/04/metadata">
                <types>
                    <members>AccountContactExplorerController</members>
                    <members>AccountContactExplorerControllerTest</members>
                    <members>LegacyAccountContactExplorerController</members>
                    <members>LegacyAcctContactExplorerCtrlTest</members>
                    <name>ApexClass</name>
                </types>
                <types>
                    <members>LegacyAccountContactExplorer</members>
                    <name>ApexPage</name>
                </types>
                <types>
                    <members>accountContactExplorer</members>
                    <name>LightningComponentBundle</name>
                </types>
                <types>
                    <members>AccountContactExplorerUser</members>
                    <name>PermissionSet</name>
                </types>
                <version>67.0</version>
            </Package>
            """
        ),
        CONTROLLER_PATH: _source(
            """
            public with sharing class AccountContactExplorerController {
                @TestVisible
                private static final Integer MAX_ACCOUNTS = 50;

                @TestVisible
                private static final Integer MAX_CONTACTS = 100;

                @AuraEnabled(cacheable=true)
                public static List<Account> getAccounts() {
                    try {
                        return [
                            SELECT Id, Name
                            FROM Account
                            WITH USER_MODE
                            ORDER BY Name
                            LIMIT :MAX_ACCOUNTS
                        ];
                    } catch (QueryException queryError) {
                        throw new AuraHandledException('Accounts could not be read.');
                    }
                }

                @AuraEnabled
                public static List<Contact> getContacts(Id accountId) {
                    if (accountId == null) {
                        return new List<Contact>();
                    }

                    try {
                        return [
                            SELECT Id, FirstName, LastName, Email, Phone
                            FROM Contact
                            WHERE AccountId = :accountId
                            WITH USER_MODE
                            ORDER BY LastName, FirstName
                            LIMIT :MAX_CONTACTS
                        ];
                    } catch (QueryException queryError) {
                        throw new AuraHandledException('Contacts could not be read.');
                    }
                }
            }
            """
        ),
        CONTROLLER_METADATA_PATH: apex_metadata,
        CONTROLLER_TEST_PATH: _source(
            """
            @IsTest
            private class AccountContactExplorerControllerTest {
                @TestSetup
                static void createRecords() {
                    List<Account> accounts = new List<Account>{
                        new Account(Name = 'Synthetic Empty Account'),
                        new Account(Name = 'Synthetic Populated Account')
                    };
                    insert accounts;
                    insert new Contact(
                        AccountId = accounts[1].Id,
                        FirstName = 'Synthetic',
                        LastName = 'Contact'
                    );
                }

                @IsTest
                static void returnsAccountsAndSelectedContacts() {
                    Account populatedAccount = [
                        SELECT Id FROM Account
                        WHERE Name = 'Synthetic Populated Account'
                        LIMIT 1
                    ];
                    Test.startTest();
                    List<Account> visibleAccounts =
                        AccountContactExplorerController.getAccounts();
                    List<Contact> visibleContacts =
                        AccountContactExplorerController.getContacts(populatedAccount.Id);
                    Test.stopTest();

                    Assert.areEqual(2, visibleAccounts.size());
                    Assert.areEqual(1, visibleContacts.size());
                    Assert.areEqual('Contact', visibleContacts[0].LastName);
                }

                @IsTest
                static void returnsEmptyContactsForAccountWithoutContacts() {
                    Account emptyAccount = [
                        SELECT Id FROM Account
                        WHERE Name = 'Synthetic Empty Account'
                        LIMIT 1
                    ];
                    Test.startTest();
                    List<Contact> visibleContacts =
                        AccountContactExplorerController.getContacts(emptyAccount.Id);
                    Test.stopTest();

                    Assert.areEqual(0, visibleContacts.size());
                }

                @IsTest
                static void returnsEmptyContactsForBlankSelection() {
                    Test.startTest();
                    List<Contact> visibleContacts =
                        AccountContactExplorerController.getContacts(null);
                    Test.stopTest();

                    Assert.areEqual(0, visibleContacts.size());
                }
            }
            """
        ),
        CONTROLLER_TEST_METADATA_PATH: apex_metadata,
        LWC_HTML_PATH: _source(
            """
            <template>
                <lightning-card title="Account Contact Explorer" icon-name="standard:account">
                    <div class="slds-p-horizontal_medium slds-p-bottom_medium controls">
                        <lightning-combobox
                            data-role="account-selector"
                            name="account"
                            label="Account"
                            value={selectedAccountId}
                            options={accountOptions}
                            onchange={handleAccountChange}>
                        </lightning-combobox>
                        <lightning-button
                            data-role="load-contacts"
                            class="load-button"
                            label="Load Contacts"
                            variant="brand"
                            disabled={isLoadDisabled}
                            onclick={handleLoad}>
                        </lightning-button>
                    </div>

                    <template lwc:if={warningMessage}>
                        <div data-state="warning" class="warning" role="alert">
                            {warningMessage}
                        </div>
                    </template>

                    <template lwc:if={errorMessage}>
                        <div data-state="error" class="error" role="alert">
                            {errorMessage}
                        </div>
                    </template>

                    <template lwc:if={isLoading}>
                        <div data-state="loading" class="loading-region">
                            <lightning-spinner alternative-text="Loading contacts" size="small">
                            </lightning-spinner>
                        </div>
                    </template>

                    <template lwc:elseif={hasContacts}>
                        <lightning-datatable
                            data-role="contact-results"
                            key-field="Id"
                            data={contacts}
                            columns={columns}
                            hide-checkbox-column>
                        </lightning-datatable>
                    </template>

                    <template lwc:elseif={showEmptyState}>
                        <p data-state="empty" class="empty-state">
                            No contacts were found for the selected account.
                        </p>
                    </template>
                </lightning-card>
            </template>
            """
        ),
        LWC_JAVASCRIPT_PATH: _source(
            """
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
            """
        ),
        LWC_CSS_PATH: _source(
            """
            :host {
                display: block;
            }

            .controls {
                display: grid;
                gap: 0.75rem;
                grid-template-columns: minmax(12rem, 1fr) auto;
                align-items: end;
            }

            .loading-region {
                min-height: 4rem;
                position: relative;
            }

            .warning {
                color: var(--lwc-colorTextWarning, #8c4b02);
            }

            .empty-state {
                color: var(--lwc-colorTextWeak, #444444);
            }
            """
        ),
        LWC_METADATA_PATH: _source(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata">
                <apiVersion>67.0</apiVersion>
                <isExposed>true</isExposed>
                <masterLabel>Account Contact Explorer</masterLabel>
                <description>Read-only browser for synthetic Accounts and Contacts.</description>
                <targets>
                    <target>lightning__AppPage</target>
                    <target>lightning__Tab</target>
                </targets>
            </LightningComponentBundle>
            """
        ),
        LWC_TEST_PATH: _source(
            """
            import { afterEach, describe, expect, it, jest } from '@jest/globals';
            import { createElement } from 'lwc';
            import AccountContactExplorer from 'c/accountContactExplorer';
            import getAccounts from '@salesforce/apex/AccountContactExplorerController.getAccounts';
            import getContacts from '@salesforce/apex/AccountContactExplorerController.getContacts';

            const ACCOUNTS = [
                { Id: '001000000000001AAA', Name: 'Skynet' },
                { Id: '001000000000002AAA', Name: 'Weyland-Yutani' }
            ];
            const CONTACTS = [
                {
                    Id: '003000000000002AAA',
                    FirstName: 'Grace',
                    LastName: 'Hopper',
                    Email: 'grace@example.invalid',
                    Phone: '415-555-0102'
                },
                {
                    Id: '003000000000001AAA',
                    FirstName: 'Ada',
                    LastName: 'Lovelace',
                    Email: 'ada@example.invalid',
                    Phone: '415-555-0101'
                }
            ];

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
                element.shadowRoot.querySelector('lightning-combobox').dispatchEvent(
                    new CustomEvent('change', { detail: { value: accountId } })
                );
            }

            describe('candidate-authored account contact checks', () => {
                afterEach(() => {
                    while (document.body.firstChild) {
                        document.body.removeChild(document.body.firstChild);
                    }
                    jest.clearAllMocks();
                    getContacts.mockReset();
                });

                it('offers wired account choices', async () => {
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    expect(
                        element.shadowRoot.querySelector('lightning-combobox').options
                    ).toHaveLength(3);
                });

                it('shows a controlled account error', async () => {
                    const element = createComponent();
                    getAccounts.error(new Error('unsafe technical detail'));
                    await flushPromises();
                    expect(element.shadowRoot.querySelector('[role="alert"]')).not.toBeNull();
                });

                it('gates loading on account selection', async () => {
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    const button = element.shadowRoot.querySelector('lightning-button');
                    expect(button.disabled).toBe(true);
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    expect(button.disabled).toBe(false);
                });

                it('loads contact results after an explicit action', async () => {
                    getContacts.mockResolvedValue(CONTACTS);
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    element.shadowRoot.querySelector('lightning-button').click();
                    await flushPromises();
                    expect(getContacts).toHaveBeenCalledWith({ accountId: ACCOUNTS[0].Id });
                });

                it('exposes loading while a request is unresolved', async () => {
                    const pending = createDeferredPromise();
                    getContacts.mockReturnValue(pending.promise);
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    element.shadowRoot.querySelector('lightning-button').click();
                    await flushPromises();
                    const spinner = element.shadowRoot.querySelector('lightning-spinner');
                    expect(spinner.alternativeText).toBe('Loading contacts');
                    pending.resolve(CONTACTS);
                    await flushPromises();
                });

                it('keeps the current account response', async () => {
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
                    expect(getContacts).toHaveBeenCalledTimes(2);
                    secondRequest.resolve(CONTACTS);
                    await flushPromises();
                    firstRequest.resolve([{ ...CONTACTS[0], FirstName: 'Stale' }]);
                    await flushPromises();
                    expect(
                        element.shadowRoot.querySelector('lightning-datatable').data[0].FirstName
                    ).not.toBe('Stale');
                });

                it('warns after selection is cleared', async () => {
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    selectAccount(element, '');
                    await flushPromises();
                    expect(element.shadowRoot.querySelector('[role="alert"]')).not.toBeNull();
                });

                it('renders an empty result after an empty success', async () => {
                    getContacts.mockResolvedValue([]);
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    element.shadowRoot.querySelector('lightning-button').click();
                    await flushPromises();
                    expect(element.shadowRoot.querySelector('[data-state="empty"]')).not.toBeNull();
                });

                it('shows a controlled contacts error', async () => {
                    getContacts.mockRejectedValue(new Error('unsafe technical detail'));
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    element.shadowRoot.querySelector('lightning-button').click();
                    await flushPromises();
                    expect(element.shadowRoot.querySelector('[role="alert"]')).not.toBeNull();
                });

                it('keeps rendered technical detail hidden', async () => {
                    getContacts.mockRejectedValue(new Error('private stack detail'));
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    element.shadowRoot.querySelector('lightning-button').click();
                    await flushPromises();
                    expect(getContacts).toHaveBeenCalledTimes(1);
                    expect(element.shadowRoot.textContent).not.toContain('private stack detail');
                });
            });
            """
        ),
        PERMISSION_SET_PATH: _source(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">
                <classAccesses>
                    <apexClass>AccountContactExplorerController</apexClass>
                    <enabled>true</enabled>
                </classAccesses>
                <classAccesses>
                    <apexClass>LegacyAccountContactExplorerController</apexClass>
                    <enabled>true</enabled>
                </classAccesses>
                <description>Read-only access to both synthetic explorer implementations.</description>
                <fieldPermissions>
                    <editable>false</editable>
                    <field>Contact.Email</field>
                    <readable>true</readable>
                </fieldPermissions>
                <fieldPermissions>
                    <editable>false</editable>
                    <field>Contact.Phone</field>
                    <readable>true</readable>
                </fieldPermissions>
                <hasActivationRequired>false</hasActivationRequired>
                <label>Account Contact Explorer User</label>
                <objectPermissions>
                    <allowCreate>false</allowCreate>
                    <allowDelete>false</allowDelete>
                    <allowEdit>false</allowEdit>
                    <allowRead>true</allowRead>
                    <modifyAllRecords>false</modifyAllRecords>
                    <object>Account</object>
                    <viewAllFields>false</viewAllFields>
                    <viewAllRecords>false</viewAllRecords>
                </objectPermissions>
                <objectPermissions>
                    <allowCreate>false</allowCreate>
                    <allowDelete>false</allowDelete>
                    <allowEdit>false</allowEdit>
                    <allowRead>true</allowRead>
                    <modifyAllRecords>false</modifyAllRecords>
                    <object>Contact</object>
                    <viewAllFields>false</viewAllFields>
                    <viewAllRecords>false</viewAllRecords>
                </objectPermissions>
                <pageAccesses>
                    <apexPage>LegacyAccountContactExplorer</apexPage>
                    <enabled>true</enabled>
                </pageAccesses>
            </PermissionSet>
            """
        ),
    }
    if tuple(sorted(outputs)) != SALESFORCE_AGENT_OUTPUT_PATHS:
        raise AssertionError("synthetic Salesforce candidate inventory drifted")
    return outputs


def salesforce_candidate_text_outputs() -> dict[str, str]:
    """Return text updates suitable for structured model test doubles."""

    return {
        path: content.decode("utf-8") for path, content in salesforce_candidate_outputs().items()
    }


def case_management_candidate_outputs() -> dict[str, bytes]:
    """Return a fresh, complete synthetic Case Management Console candidate.

    Mirrors :func:`salesforce_candidate_outputs` for the ``case-management-console``
    migration unit: eleven artifacts that satisfy the controller-owned static and
    dependency-closure checks for the Case controller, LWC, Apex test, and
    least-privileged permission set.
    """

    apex_metadata = _source(
        """
        <?xml version="1.0" encoding="UTF-8"?>
        <ApexClass xmlns="http://soap.sforce.com/2006/04/metadata">
            <apiVersion>67.0</apiVersion>
            <status>Active</status>
        </ApexClass>
        """
    )
    outputs = {
        CASE_MANIFEST_PATH: _source(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <Package xmlns="http://soap.sforce.com/2006/04/metadata">
                <types>
                    <members>CaseManagementConsoleController</members>
                    <members>CaseManagementConsoleControllerTest</members>
                    <members>LegacyCaseManagementConsoleController</members>
                    <members>LegacyCaseQueryService</members>
                    <members>LegacyCaseConsoleCtrlTest</members>
                    <name>ApexClass</name>
                </types>
                <types>
                    <members>LegacyCaseManagementConsole</members>
                    <name>ApexPage</name>
                </types>
                <types>
                    <members>caseManagementConsole</members>
                    <name>LightningComponentBundle</name>
                </types>
                <types>
                    <members>CaseManagementConsoleUser</members>
                    <name>PermissionSet</name>
                </types>
                <version>67.0</version>
            </Package>
            """
        ),
        CASE_CONTROLLER_PATH: _source(
            """
            public with sharing class CaseManagementConsoleController {
                @TestVisible
                private static final Integer MAX_ACCOUNTS = 50;

                @TestVisible
                private static final Integer MAX_CASES = 100;

                @AuraEnabled(cacheable=true)
                public static List<Account> getAccounts() {
                    try {
                        return [
                            SELECT Id, Name
                            FROM Account
                            WITH USER_MODE
                            ORDER BY Name
                            LIMIT :MAX_ACCOUNTS
                        ];
                    } catch (QueryException queryError) {
                        throw new AuraHandledException('Accounts could not be read.');
                    }
                }

                @AuraEnabled
                public static List<Case> getCases(Id accountId, String statusFilter) {
                    if (accountId == null) {
                        return new List<Case>();
                    }

                    List<Boolean> closedValues = new List<Boolean>{ false };
                    if (statusFilter == 'CLOSED') {
                        closedValues = new List<Boolean>{ true };
                    } else if (statusFilter == 'ALL') {
                        closedValues = new List<Boolean>{ true, false };
                    }

                    try {
                        return [
                            SELECT Id, CaseNumber, Subject, Status, Priority, Contact.Name
                            FROM Case
                            WHERE AccountId = :accountId AND IsClosed IN :closedValues
                            WITH USER_MODE
                            ORDER BY CaseNumber DESC
                            LIMIT :MAX_CASES
                        ];
                    } catch (QueryException queryError) {
                        throw new AuraHandledException('Cases could not be read.');
                    }
                }
            }
            """
        ),
        CASE_CONTROLLER_METADATA_PATH: apex_metadata,
        CASE_CONTROLLER_TEST_PATH: _source(
            """
            @IsTest
            private class CaseManagementConsoleControllerTest {
                @TestSetup
                static void createRecords() {
                    List<Account> accounts = new List<Account>{
                        new Account(Name = 'Weyland-Yutani'),
                        new Account(Name = 'Skynet')
                    };
                    insert accounts;

                    Account skynetAccount = [
                        SELECT Id FROM Account WHERE Name = 'Skynet' LIMIT 1
                    ];

                    Contact skynetContact = new Contact(
                        AccountId = skynetAccount.Id,
                        FirstName = 'Sarah',
                        LastName = 'Connor'
                    );
                    insert skynetContact;

                    insert new List<Case>{
                        new Case(
                            AccountId = skynetAccount.Id,
                            ContactId = skynetContact.Id,
                            Subject = 'Cooling fan malfunction',
                            Status = 'New',
                            Priority = 'High'
                        ),
                        new Case(
                            AccountId = skynetAccount.Id,
                            ContactId = skynetContact.Id,
                            Subject = 'Firmware update request',
                            Status = 'Closed',
                            Priority = 'Low'
                        )
                    };
                }

                @IsTest
                static void returnsAccountsAndDefaultOpenCases() {
                    Account skynetAccount = [
                        SELECT Id FROM Account WHERE Name = 'Skynet' LIMIT 1
                    ];
                    Test.startTest();
                    List<Account> visibleAccounts =
                        CaseManagementConsoleController.getAccounts();
                    List<Case> openCases =
                        CaseManagementConsoleController.getCases(skynetAccount.Id, 'OPEN');
                    Test.stopTest();

                    Assert.areEqual(2, visibleAccounts.size());
                    Assert.areEqual(1, openCases.size());
                    Assert.areEqual('Cooling fan malfunction', openCases[0].Subject);
                }

                @IsTest
                static void filtersCasesByStatus() {
                    Account skynetAccount = [
                        SELECT Id FROM Account WHERE Name = 'Skynet' LIMIT 1
                    ];
                    Test.startTest();
                    List<Case> closedCases =
                        CaseManagementConsoleController.getCases(skynetAccount.Id, 'CLOSED');
                    List<Case> allCases =
                        CaseManagementConsoleController.getCases(skynetAccount.Id, 'ALL');
                    Test.stopTest();

                    Assert.areEqual(1, closedCases.size());
                    Assert.areEqual('Firmware update request', closedCases[0].Subject);
                    Assert.areEqual(2, allCases.size());
                    Assert.areEqual('Firmware update request', allCases[0].Subject);
                }

                @IsTest
                static void returnsEmptyCasesForBlankSelection() {
                    Test.startTest();
                    List<Case> visibleCases =
                        CaseManagementConsoleController.getCases(null, 'OPEN');
                    Test.stopTest();

                    Assert.areEqual(0, visibleCases.size());
                }
            }
            """
        ),
        CASE_CONTROLLER_TEST_METADATA_PATH: apex_metadata,
        CASE_LWC_HTML_PATH: _source(
            """
            <template>
                <lightning-card title="Case Management Console" icon-name="standard:case">
                    <div class="slds-p-horizontal_medium slds-p-bottom_medium controls">
                        <lightning-combobox
                            data-role="account-selector"
                            name="account"
                            label="Account"
                            value={selectedAccountId}
                            options={accountOptions}
                            onchange={handleAccountChange}>
                        </lightning-combobox>
                        <lightning-combobox
                            data-role="status-filter"
                            name="status"
                            label="Status"
                            value={statusFilter}
                            options={statusOptions}
                            onchange={handleStatusChange}>
                        </lightning-combobox>
                        <lightning-button
                            data-role="load-cases"
                            class="load-button"
                            label="Load Cases"
                            variant="brand"
                            disabled={isLoadDisabled}
                            onclick={handleLoad}>
                        </lightning-button>
                        <lightning-button
                            data-role="clear-selection"
                            class="clear-button"
                            label="Clear Selection"
                            onclick={handleClear}>
                        </lightning-button>
                    </div>

                    <template lwc:if={showGuidance}>
                        <p role="alert" class="guidance">
                            Select an account, choose a status, and load cases to view them.
                        </p>
                    </template>

                    <template lwc:if={warningMessage}>
                        <div data-state="warning" class="warning" role="alert">
                            {warningMessage}
                        </div>
                    </template>

                    <template lwc:if={errorMessage}>
                        <div data-state="error" class="error" role="alert">
                            {errorMessage}
                        </div>
                    </template>

                    <template lwc:if={isLoading}>
                        <div data-state="loading" class="loading-region">
                            <lightning-spinner alternative-text="Loading cases" size="small">
                            </lightning-spinner>
                        </div>
                    </template>

                    <template lwc:elseif={hasCases}>
                        <lightning-datatable
                            data-role="case-results"
                            data-state="results"
                            key-field="Id"
                            data={cases}
                            columns={columns}
                            hide-checkbox-column>
                        </lightning-datatable>
                    </template>

                    <template lwc:elseif={showEmptyState}>
                        <p data-state="empty" class="empty-state">
                            No cases were found for the selected account and status filter.
                        </p>
                    </template>
                </lightning-card>
            </template>
            """
        ),
        CASE_LWC_JAVASCRIPT_PATH: _source(
            """
            import { LightningElement, wire } from 'lwc';
            import getAccounts from '@salesforce/apex/CaseManagementConsoleController.getAccounts';
            import getCases from '@salesforce/apex/CaseManagementConsoleController.getCases';

            const BLANK_ACCOUNT_OPTION = Object.freeze({
                label: '-- Select an account --',
                value: ''
            });

            const STATUS_FILTER_OPTIONS = Object.freeze([
                { label: 'Open', value: 'OPEN' },
                { label: 'Closed', value: 'CLOSED' },
                { label: 'All', value: 'ALL' }
            ]);

            const CASE_COLUMNS = Object.freeze([
                { label: 'Case Number', fieldName: 'CaseNumber', type: 'text' },
                { label: 'Subject', fieldName: 'Subject', type: 'text' },
                { label: 'Status', fieldName: 'Status', type: 'text' },
                { label: 'Priority', fieldName: 'Priority', type: 'text' },
                { label: 'Contact', fieldName: 'ContactName', type: 'text' }
            ]);

            export default class CaseManagementConsole extends LightningElement {
                accountOptions = [BLANK_ACCOUNT_OPTION];
                statusOptions = STATUS_FILTER_OPTIONS;
                selectedAccountId = '';
                statusFilter = 'OPEN';
                cases = [];
                columns = CASE_COLUMNS;
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
                        this.loadRequestGeneration += 1;
                        this.accountOptions = [BLANK_ACCOUNT_OPTION];
                        this.selectedAccountId = '';
                        this.cases = [];
                        this.isLoading = false;
                        this.hasLoaded = false;
                        this.warningMessage = undefined;
                        this.errorMessage = 'Accounts could not be loaded.';
                    }
                }

                handleAccountChange(event) {
                    this.loadRequestGeneration += 1;
                    this.selectedAccountId = event.detail.value;
                    this.cases = [];
                    this.isLoading = false;
                    this.hasLoaded = false;
                    this.errorMessage = undefined;
                    this.warningMessage = this.selectedAccountId
                        ? undefined
                        : 'Select an account before loading cases.';
                }

                handleStatusChange(event) {
                    this.loadRequestGeneration += 1;
                    this.statusFilter = event.detail.value;
                    this.cases = [];
                    this.isLoading = false;
                    this.hasLoaded = false;
                    this.errorMessage = undefined;
                    this.warningMessage = undefined;
                }

                async handleLoad() {
                    if (!this.selectedAccountId) {
                        this.warningMessage = 'Select an account before loading cases.';
                        return;
                    }

                    const accountId = this.selectedAccountId;
                    const statusFilter = this.statusFilter;
                    this.loadRequestGeneration += 1;
                    const requestGeneration = this.loadRequestGeneration;

                    this.isLoading = true;
                    this.hasLoaded = false;
                    this.cases = [];
                    this.warningMessage = undefined;
                    this.errorMessage = undefined;

                    try {
                        const result = await getCases({ accountId, statusFilter });
                        if (!this.isCurrentRequest(accountId, requestGeneration)) {
                            return;
                        }
                        this.cases = (result ?? []).map((caseRecord) => ({
                            ...caseRecord,
                            ContactName: caseRecord.Contact ? caseRecord.Contact.Name : ''
                        }));
                        this.hasLoaded = true;
                    } catch (error) {
                        if (!this.isCurrentRequest(accountId, requestGeneration)) {
                            return;
                        }
                        this.errorMessage = 'Cases could not be loaded.';
                    } finally {
                        if (this.isCurrentRequest(accountId, requestGeneration)) {
                            this.isLoading = false;
                        }
                    }
                }

                handleClear() {
                    this.loadRequestGeneration += 1;
                    this.selectedAccountId = '';
                    this.statusFilter = 'OPEN';
                    this.cases = [];
                    this.isLoading = false;
                    this.hasLoaded = false;
                    this.errorMessage = undefined;
                    this.warningMessage = 'Select an account before loading cases.';
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

                get hasCases() {
                    return this.cases.length > 0;
                }

                get showGuidance() {
                    return (
                        !this.hasLoaded &&
                        !this.isLoading &&
                        !this.hasCases &&
                        !this.warningMessage &&
                        !this.errorMessage
                    );
                }

                get showEmptyState() {
                    return this.hasLoaded && !this.hasCases && !this.errorMessage;
                }
            }
            """
        ),
        CASE_LWC_CSS_PATH: _source(
            """
            :host {
                display: block;
            }

            .controls {
                display: grid;
                gap: 0.75rem;
                grid-template-columns: minmax(12rem, 1fr) minmax(10rem, 1fr) auto auto;
                align-items: end;
            }

            .loading-region {
                min-height: 4rem;
                position: relative;
            }

            .warning {
                color: var(--lwc-colorTextWarning, #8c4b02);
            }

            .guidance,
            .empty-state {
                color: var(--lwc-colorTextWeak, #444444);
            }
            """
        ),
        CASE_LWC_METADATA_PATH: _source(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata">
                <apiVersion>67.0</apiVersion>
                <isExposed>true</isExposed>
                <masterLabel>Case Management Console</masterLabel>
                <description>Read-only browser for synthetic Accounts and their Cases.</description>
                <targets>
                    <target>lightning__AppPage</target>
                    <target>lightning__Tab</target>
                </targets>
            </LightningComponentBundle>
            """
        ),
        CASE_LWC_TEST_PATH: _source(
            """
            import { afterEach, describe, expect, it, jest } from '@jest/globals';
            import { createElement } from 'lwc';
            import CaseManagementConsole from 'c/caseManagementConsole';
            import getAccounts from '@salesforce/apex/CaseManagementConsoleController.getAccounts';
            import getCases from '@salesforce/apex/CaseManagementConsoleController.getCases';

            const ACCOUNTS = [
                { Id: '001000000000001AAA', Name: 'Skynet' },
                { Id: '001000000000002AAA', Name: 'Weyland-Yutani' }
            ];
            const CASES = [
                {
                    Id: '500000000000002AAA',
                    CaseNumber: '00001002',
                    Subject: 'Firmware update request',
                    Status: 'Closed',
                    Priority: 'Low',
                    Contact: { Name: 'Sarah Connor' }
                },
                {
                    Id: '500000000000001AAA',
                    CaseNumber: '00001001',
                    Subject: 'Cooling fan malfunction',
                    Status: 'New',
                    Priority: 'High',
                    Contact: { Name: 'Sarah Connor' }
                }
            ];

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
                const element = createElement('c-case-management-console', {
                    is: CaseManagementConsole
                });
                document.body.appendChild(element);
                return element;
            }

            function loadButton(element) {
                return element.shadowRoot.querySelector('[data-role="load-cases"]');
            }

            function selectAccount(element, accountId) {
                element.shadowRoot
                    .querySelector('[data-role="account-selector"]')
                    .dispatchEvent(new CustomEvent('change', { detail: { value: accountId } }));
            }

            describe('candidate-authored case console checks', () => {
                afterEach(() => {
                    while (document.body.firstChild) {
                        document.body.removeChild(document.body.firstChild);
                    }
                    jest.clearAllMocks();
                    getCases.mockReset();
                });

                it('offers wired account choices', async () => {
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    expect(
                        element.shadowRoot
                            .querySelector('[data-role="account-selector"]').options
                    ).toHaveLength(3);
                });

                it('shows a controlled account error', async () => {
                    const element = createComponent();
                    getAccounts.error(new Error('unsafe technical detail'));
                    await flushPromises();
                    expect(element.shadowRoot.querySelector('[role="alert"]')).not.toBeNull();
                });

                it('gates loading on account selection', async () => {
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    expect(loadButton(element).disabled).toBe(true);
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    expect(loadButton(element).disabled).toBe(false);
                });

                it('loads case results after an explicit action', async () => {
                    getCases.mockResolvedValue(CASES);
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    loadButton(element).click();
                    await flushPromises();
                    expect(getCases).toHaveBeenCalledWith({
                        accountId: ACCOUNTS[0].Id,
                        statusFilter: 'OPEN'
                    });
                });

                it('exposes loading while a request is unresolved', async () => {
                    const pending = createDeferredPromise();
                    getCases.mockReturnValue(pending.promise);
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    loadButton(element).click();
                    await flushPromises();
                    const spinner = element.shadowRoot.querySelector('lightning-spinner');
                    expect(spinner.alternativeText).toBe('Loading cases');
                    pending.resolve(CASES);
                    await flushPromises();
                });

                it('keeps the current account response', async () => {
                    const firstRequest = createDeferredPromise();
                    const secondRequest = createDeferredPromise();
                    getCases
                        .mockReturnValueOnce(firstRequest.promise)
                        .mockReturnValueOnce(secondRequest.promise);
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    loadButton(element).click();
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[1].Id);
                    await flushPromises();
                    loadButton(element).click();
                    await flushPromises();
                    expect(getCases).toHaveBeenCalledTimes(2);
                    secondRequest.resolve(CASES);
                    await flushPromises();
                    firstRequest.resolve([{ ...CASES[0], Subject: 'Stale' }]);
                    await flushPromises();
                    expect(
                        element.shadowRoot
                            .querySelector('[data-role="case-results"]').data[0].Subject
                    ).not.toBe('Stale');
                });

                it('warns after selection is cleared', async () => {
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    selectAccount(element, '');
                    await flushPromises();
                    expect(element.shadowRoot.querySelector('[role="alert"]')).not.toBeNull();
                });

                it('renders an empty result after an empty success', async () => {
                    getCases.mockResolvedValue([]);
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    loadButton(element).click();
                    await flushPromises();
                    expect(
                        element.shadowRoot.querySelector('[data-state="empty"]')
                    ).not.toBeNull();
                });

                it('shows a controlled cases error', async () => {
                    getCases.mockRejectedValue(new Error('unsafe technical detail'));
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    loadButton(element).click();
                    await flushPromises();
                    expect(element.shadowRoot.querySelector('[role="alert"]')).not.toBeNull();
                });

                it('keeps rendered technical detail hidden', async () => {
                    getCases.mockRejectedValue(new Error('private stack detail'));
                    const element = createComponent();
                    getAccounts.emit(ACCOUNTS);
                    await flushPromises();
                    selectAccount(element, ACCOUNTS[0].Id);
                    await flushPromises();
                    loadButton(element).click();
                    await flushPromises();
                    expect(getCases).toHaveBeenCalledTimes(1);
                    expect(element.shadowRoot.textContent).not.toContain('private stack detail');
                });
            });
            """
        ),
        CASE_PERMISSION_SET_PATH: _source(
            """
            <?xml version="1.0" encoding="UTF-8"?>
            <PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">
                <classAccesses>
                    <apexClass>CaseManagementConsoleController</apexClass>
                    <enabled>true</enabled>
                </classAccesses>
                <classAccesses>
                    <apexClass>LegacyCaseManagementConsoleController</apexClass>
                    <enabled>true</enabled>
                </classAccesses>
                <classAccesses>
                    <apexClass>LegacyCaseQueryService</apexClass>
                    <enabled>true</enabled>
                </classAccesses>
                <description>Read-only access to both synthetic Case console implementations.</description>
                <fieldPermissions>
                    <editable>false</editable>
                    <field>Case.AccountId</field>
                    <readable>true</readable>
                </fieldPermissions>
                <fieldPermissions>
                    <editable>false</editable>
                    <field>Case.ContactId</field>
                    <readable>true</readable>
                </fieldPermissions>
                <fieldPermissions>
                    <editable>false</editable>
                    <field>Case.Description</field>
                    <readable>true</readable>
                </fieldPermissions>
                <fieldPermissions>
                    <editable>false</editable>
                    <field>Case.Priority</field>
                    <readable>true</readable>
                </fieldPermissions>
                <fieldPermissions>
                    <editable>false</editable>
                    <field>Case.Subject</field>
                    <readable>true</readable>
                </fieldPermissions>
                <hasActivationRequired>false</hasActivationRequired>
                <label>Case Management Console User</label>
                <objectPermissions>
                    <allowCreate>false</allowCreate>
                    <allowDelete>false</allowDelete>
                    <allowEdit>false</allowEdit>
                    <allowRead>true</allowRead>
                    <modifyAllRecords>false</modifyAllRecords>
                    <object>Account</object>
                    <viewAllFields>false</viewAllFields>
                    <viewAllRecords>false</viewAllRecords>
                </objectPermissions>
                <objectPermissions>
                    <allowCreate>false</allowCreate>
                    <allowDelete>false</allowDelete>
                    <allowEdit>false</allowEdit>
                    <allowRead>true</allowRead>
                    <modifyAllRecords>false</modifyAllRecords>
                    <object>Case</object>
                    <viewAllFields>false</viewAllFields>
                    <viewAllRecords>false</viewAllRecords>
                </objectPermissions>
                <objectPermissions>
                    <allowCreate>false</allowCreate>
                    <allowDelete>false</allowDelete>
                    <allowEdit>false</allowEdit>
                    <allowRead>true</allowRead>
                    <modifyAllRecords>false</modifyAllRecords>
                    <object>Contact</object>
                    <viewAllFields>false</viewAllFields>
                    <viewAllRecords>false</viewAllRecords>
                </objectPermissions>
                <pageAccesses>
                    <apexPage>LegacyCaseManagementConsole</apexPage>
                    <enabled>true</enabled>
                </pageAccesses>
            </PermissionSet>
            """
        ),
    }
    if tuple(sorted(outputs)) != CASE_AGENT_OUTPUT_PATHS:
        raise AssertionError("synthetic Case candidate inventory drifted")
    return outputs


def case_management_candidate_text_outputs() -> dict[str, str]:
    """Return Case candidate text updates suitable for structured model test doubles."""

    return {
        path: content.decode("utf-8")
        for path, content in case_management_candidate_outputs().items()
    }
