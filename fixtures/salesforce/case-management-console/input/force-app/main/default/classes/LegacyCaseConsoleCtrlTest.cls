@IsTest
private class LegacyCaseConsoleCtrlTest {
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

        Contact skynetContact = new Contact(
            AccountId = skynetAccount.Id,
            FirstName = 'Sarah',
            LastName = 'Connor',
            Email = 'sarah@example.invalid'
        );
        insert skynetContact;

        insert new List<Case>{
            new Case(
                AccountId = skynetAccount.Id,
                ContactId = skynetContact.Id,
                Subject = 'Cooling fan malfunction',
                Status = 'New',
                Priority = 'High',
                Description = 'Unit reports intermittent overheating.',
                IsClosed = false
            ),
            new Case(
                AccountId = skynetAccount.Id,
                ContactId = skynetContact.Id,
                Subject = 'Firmware update request',
                Status = 'Closed',
                Priority = 'Low',
                Description = 'Customer requested the latest firmware build.',
                IsClosed = true
            )
        };
    }

    @IsTest
    static void providesBlankAndOrderedAccountOptions() {
        Test.setCurrentPage(Page.LegacyCaseManagementConsole);
        LegacyCaseManagementConsoleController controller =
            new LegacyCaseManagementConsoleController();

        Test.startTest();
        List<SelectOption> options = controller.getAccountOptions();
        Test.stopTest();

        System.assertEquals('', options[0].getValue());
        System.assertEquals('-- Select an account --', options[0].getLabel());
        System.assertEquals('Skynet', options[1].getLabel());
        System.assertEquals('Weyland-Yutani', options[2].getLabel());
    }

    @IsTest
    static void loadsOpenCasesForSelectedAccountByDefault() {
        Account accountRecord = [SELECT Id FROM Account WHERE Name = 'Skynet' LIMIT 1];
        LegacyCaseManagementConsoleController controller =
            new LegacyCaseManagementConsoleController();
        controller.selectedAccountId = accountRecord.Id;

        Test.startTest();
        PageReference result = controller.loadCases();
        Test.stopTest();

        System.assertEquals(null, result);
        System.assertEquals(true, controller.hasSearched);
        System.assertEquals(1, controller.cases.size());
        System.assertEquals('Cooling fan malfunction', controller.cases[0].Subject);
        System.assertEquals('Sarah Connor', controller.cases[0].Contact.Name);
    }

    @IsTest
    static void loadsClosedCasesWhenStatusFilterIsClosed() {
        Account accountRecord = [SELECT Id FROM Account WHERE Name = 'Skynet' LIMIT 1];
        LegacyCaseManagementConsoleController controller =
            new LegacyCaseManagementConsoleController();
        controller.selectedAccountId = accountRecord.Id;
        controller.statusFilter = LegacyCaseQueryService.STATUS_CLOSED;

        Test.startTest();
        controller.loadCases();
        Test.stopTest();

        System.assertEquals(1, controller.cases.size());
        System.assertEquals('Firmware update request', controller.cases[0].Subject);
    }

    @IsTest
    static void loadsAllCasesWhenStatusFilterIsAll() {
        Account accountRecord = [SELECT Id FROM Account WHERE Name = 'Skynet' LIMIT 1];
        LegacyCaseManagementConsoleController controller =
            new LegacyCaseManagementConsoleController();
        controller.selectedAccountId = accountRecord.Id;
        controller.statusFilter = LegacyCaseQueryService.STATUS_ALL;

        Test.startTest();
        controller.loadCases();
        Test.stopTest();

        System.assertEquals(2, controller.cases.size());
    }

    @IsTest
    static void warnsWhenSelectionIsBlank() {
        LegacyCaseManagementConsoleController controller =
            new LegacyCaseManagementConsoleController();

        Test.startTest();
        controller.loadCases();
        Test.stopTest();

        System.assertEquals(0, controller.cases.size());
        System.assertEquals(false, controller.hasSearched);
        System.assertEquals(1, ApexPages.getMessages().size());
        System.assertEquals(
            ApexPages.Severity.WARNING,
            ApexPages.getMessages()[0].getSeverity()
        );
    }

    @IsTest
    static void clearSelectionResetsStateForGuidanceMessage() {
        Account accountRecord = [SELECT Id FROM Account WHERE Name = 'Skynet' LIMIT 1];
        LegacyCaseManagementConsoleController controller =
            new LegacyCaseManagementConsoleController();
        controller.selectedAccountId = accountRecord.Id;
        controller.loadCases();

        Test.startTest();
        PageReference result = controller.clearSelection();
        Test.stopTest();

        System.assertEquals(null, result);
        System.assertEquals(null, controller.selectedAccountId);
        System.assertEquals(0, controller.cases.size());
        System.assertEquals(false, controller.hasSearched);
        System.assertEquals(LegacyCaseQueryService.STATUS_OPEN, controller.statusFilter);
    }

    @IsTest
    static void serviceReturnsEmptyListWhenAccountIdIsNull() {
        Test.startTest();
        List<Case> results = LegacyCaseQueryService.getCasesForAccount(
            null,
            LegacyCaseQueryService.STATUS_OPEN
        );
        Test.stopTest();

        System.assertEquals(0, results.size());
    }
}
