public with sharing class LegacyAccountContactExplorerController {
    @TestVisible
    private static final Integer MAX_ACCOUNTS = 50;

    @TestVisible
    private static final Integer MAX_CONTACTS = 100;

    public Id selectedAccountId { get; set; }
    public List<Contact> contacts { get; private set; }

    public LegacyAccountContactExplorerController() {
        contacts = new List<Contact>();
    }

    public List<SelectOption> getAccountOptions() {
        List<SelectOption> options = new List<SelectOption>{
            new SelectOption('', '-- Select an account --')
        };

        for (Account accountRecord : [
            SELECT Id, Name
            FROM Account
            WITH USER_MODE
            ORDER BY Name
            LIMIT :MAX_ACCOUNTS
        ]) {
            options.add(new SelectOption(accountRecord.Id, accountRecord.Name));
        }

        return options;
    }

    public PageReference loadContacts() {
        contacts = new List<Contact>();

        if (selectedAccountId == null) {
            ApexPages.addMessage(new ApexPages.Message(
                ApexPages.Severity.WARNING,
                'Select an account before loading contacts.'
            ));
            return null;
        }

        contacts = [
            SELECT Id, FirstName, LastName, Email, Phone
            FROM Contact
            WHERE AccountId = :selectedAccountId
            WITH USER_MODE
            ORDER BY LastName, FirstName
            LIMIT :MAX_CONTACTS
        ];
        return null;
    }
}
