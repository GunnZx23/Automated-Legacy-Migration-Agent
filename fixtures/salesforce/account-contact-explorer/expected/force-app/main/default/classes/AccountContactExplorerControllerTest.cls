@IsTest
private class AccountContactExplorerControllerTest {
    private static final String TARGET_ACCOUNT_NAME = 'Synthetic Account 000';
    private static final String FOREIGN_ACCOUNT_NAME = 'Synthetic Account 050';
    private static final String EMPTY_ACCOUNT_NAME = 'Synthetic Account 051';

    private static String paddedNumber(Integer numberValue) {
        if (numberValue < 10) {
            return '00' + String.valueOf(numberValue);
        }
        if (numberValue < 100) {
            return '0' + String.valueOf(numberValue);
        }
        return String.valueOf(numberValue);
    }

    @TestSetup
    static void createRecords() {
        List<Account> accounts = new List<Account>();
        for (
            Integer index = 0;
            index < AccountContactExplorerController.MAX_ACCOUNTS + 2;
            index++
        ) {
            accounts.add(new Account(Name = 'Synthetic Account ' + paddedNumber(index)));
        }
        insert accounts;

        Set<String> requiredAccountNames = new Set<String>{
            TARGET_ACCOUNT_NAME,
            FOREIGN_ACCOUNT_NAME
        };
        Map<String, Id> accountIdsByName = new Map<String, Id>();
        for (Account accountRecord : [
            SELECT Id, Name
            FROM Account
            WHERE Name IN :requiredAccountNames
        ]) {
            accountIdsByName.put(accountRecord.Name, accountRecord.Id);
        }

        List<Contact> contacts = new List<Contact>{
            new Contact(
                AccountId = accountIdsByName.get(TARGET_ACCOUNT_NAME),
                FirstName = 'Zoe',
                LastName = 'Alpha Tie'
            ),
            new Contact(
                AccountId = accountIdsByName.get(TARGET_ACCOUNT_NAME),
                FirstName = 'Ada',
                LastName = 'Alpha Tie'
            )
        };
        for (Integer index = 0; index < 98; index++) {
            contacts.add(new Contact(
                AccountId = accountIdsByName.get(TARGET_ACCOUNT_NAME),
                FirstName = 'Member',
                LastName = 'Synthetic ' + paddedNumber(index)
            ));
        }
        for (Integer index = 0; index < 2; index++) {
            contacts.add(new Contact(
                AccountId = accountIdsByName.get(TARGET_ACCOUNT_NAME),
                FirstName = 'Overflow',
                LastName = 'Zulu Overflow ' + paddedNumber(index)
            ));
        }
        contacts.add(new Contact(
            AccountId = accountIdsByName.get(FOREIGN_ACCOUNT_NAME),
            FirstName = 'Foreign',
            LastName = '000 Foreign Account'
        ));
        insert contacts;
    }

    @IsTest
    static void returnsAccountsInNameOrder() {
        Integer totalAccounts = [SELECT COUNT() FROM Account];

        Test.startTest();
        List<Account> accounts = AccountContactExplorerController.getAccounts();
        Test.stopTest();

        System.assertEquals(
            AccountContactExplorerController.MAX_ACCOUNTS + 2,
            totalAccounts
        );
        System.assertEquals(AccountContactExplorerController.MAX_ACCOUNTS, accounts.size());
        System.assertEquals('Synthetic Account 000', accounts[0].Name);
        System.assertEquals('Synthetic Account 049', accounts[49].Name);
        for (Integer index = 0; index < accounts.size(); index++) {
            System.assertEquals(
                'Synthetic Account ' + paddedNumber(index),
                accounts[index].Name
            );
        }
    }

    @IsTest
    static void returnsContactsInLastAndFirstNameOrder() {
        Account accountRecord = [
            SELECT Id
            FROM Account
            WHERE Name = :TARGET_ACCOUNT_NAME
            LIMIT 1
        ];
        Contact foreignContact = [
            SELECT Id
            FROM Contact
            WHERE Account.Name = :FOREIGN_ACCOUNT_NAME
            LIMIT 1
        ];
        Integer totalTargetContacts = [
            SELECT COUNT()
            FROM Contact
            WHERE AccountId = :accountRecord.Id
        ];

        Test.startTest();
        List<Contact> contacts =
            AccountContactExplorerController.getContacts(accountRecord.Id);
        Test.stopTest();

        Map<Id, Contact> returnedContactsById = new Map<Id, Contact>(contacts);
        Set<Id> returnedIds = returnedContactsById.keySet();
        System.assertEquals(
            AccountContactExplorerController.MAX_CONTACTS + 2,
            totalTargetContacts
        );
        System.assertEquals(AccountContactExplorerController.MAX_CONTACTS, contacts.size());
        System.assert(!returnedIds.contains(foreignContact.Id));
        System.assertEquals('Alpha Tie', contacts[0].LastName);
        System.assertEquals('Ada', contacts[0].FirstName);
        System.assertEquals('Alpha Tie', contacts[1].LastName);
        System.assertEquals('Zoe', contacts[1].FirstName);
        System.assertEquals('Synthetic 097', contacts[99].LastName);
    }

    @IsTest
    static void returnsEmptyListForBlankSelection() {
        Test.startTest();
        List<Contact> contacts = AccountContactExplorerController.getContacts(null);
        Test.stopTest();

        System.assertEquals(0, contacts.size());
    }

    @IsTest
    static void returnsEmptyListWhenAccountHasNoContacts() {
        Account accountRecord = [
            SELECT Id
            FROM Account
            WHERE Name = :EMPTY_ACCOUNT_NAME
            LIMIT 1
        ];

        Test.startTest();
        List<Contact> contacts =
            AccountContactExplorerController.getContacts(accountRecord.Id);
        Test.stopTest();

        System.assertEquals(0, contacts.size());
    }
}
