"""Synthetic Salesforce candidate construction for test doubles.

The production fixture retains only legacy input. Tests that need a model-like
candidate construct one in memory here; product code never imports this module,
and validators never compare generated output with these bytes.
"""

from __future__ import annotations

from textwrap import dedent

from legacy_migration_agent.platforms.local_checks import (
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
                    <members>LegacyAccountContactExplorerControllerTest</members>
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

                @AuraEnabled(cacheable=true)
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
                @IsTest
                static void exercisesPublicReadMethods() {
                    Account sampleAccount = new Account(Name = 'Synthetic Candidate Account');
                    insert sampleAccount;

                    Test.startTest();
                    List<Account> visibleAccounts =
                        AccountContactExplorerController.getAccounts();
                    List<Contact> visibleContacts =
                        AccountContactExplorerController.getContacts(sampleAccount.Id);
                    Test.stopTest();

                    Assert.isNotNull(visibleAccounts);
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
