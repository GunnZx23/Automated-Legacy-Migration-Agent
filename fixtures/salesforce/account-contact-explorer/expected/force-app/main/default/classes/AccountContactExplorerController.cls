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
