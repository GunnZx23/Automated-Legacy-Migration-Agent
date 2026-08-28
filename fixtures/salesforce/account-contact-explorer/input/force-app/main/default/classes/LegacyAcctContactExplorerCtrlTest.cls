@IsTest
private class LegacyAcctContactExplorerCtrlTest {
    @TestSetup
    static void createRecords() {
        List<Account> accounts = new List<Account>{
            new Account(Name = 'Weyland-Yutani'),
            new Account(Name = 'Skynet')
        };
        insert accounts;

        Account skynetAccount = [
            SELECT Id
            FROM Account
            WHERE Name = 'Skynet'
            LIMIT 1
        ];
        insert new List<Contact>{
            new Contact(
                AccountId = skynetAccount.Id,
                FirstName = 'Grace',
                LastName = 'Hopper',
                Email = 'grace@example.invalid',
                Phone = '415-555-0102'
            ),
            new Contact(
                AccountId = skynetAccount.Id,
                FirstName = 'Ada',
                LastName = 'Lovelace',
                Email = 'ada@example.invalid',
                Phone = '415-555-0101'
            )
        };
    }

    @IsTest
    static void providesBlankAndOrderedAccountOptions() {
        Test.setCurrentPage(Page.LegacyAccountContactExplorer);
        LegacyAccountContactExplorerController controller =
            new LegacyAccountContactExplorerController();

        Test.startTest();
        List<SelectOption> options = controller.getAccountOptions();
        Test.stopTest();

        System.assertEquals('', options[0].getValue());
        System.assertEquals('-- Select an account --', options[0].getLabel());
        System.assertEquals('Skynet', options[1].getLabel());
        System.assertEquals('Weyland-Yutani', options[2].getLabel());
    }

    @IsTest
    static void loadsSelectedAccountContactsInNameOrder() {
        Account accountRecord = [
            SELECT Id
            FROM Account
            WHERE Name = 'Skynet'
            LIMIT 1
        ];
        LegacyAccountContactExplorerController controller =
            new LegacyAccountContactExplorerController();
        controller.selectedAccountId = accountRecord.Id;

        Test.startTest();
        PageReference result = controller.loadContacts();
        Test.stopTest();

        System.assertEquals(null, result);
        System.assertEquals(2, controller.contacts.size());
        System.assertEquals('Hopper', controller.contacts[0].LastName);
        System.assertEquals('Lovelace', controller.contacts[1].LastName);
    }

    @IsTest
    static void warnsWhenSelectionIsBlank() {
        LegacyAccountContactExplorerController controller =
            new LegacyAccountContactExplorerController();

        Test.startTest();
        controller.loadContacts();
        Test.stopTest();

        System.assertEquals(0, controller.contacts.size());
        System.assertEquals(1, ApexPages.getMessages().size());
        System.assertEquals(
            ApexPages.Severity.WARNING,
            ApexPages.getMessages()[0].getSeverity()
        );
    }
}
