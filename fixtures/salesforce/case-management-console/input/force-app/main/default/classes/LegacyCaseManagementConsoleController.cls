public with sharing class LegacyCaseManagementConsoleController {
    @TestVisible
    private static final Integer MAX_ACCOUNTS = 50;

    public Id selectedAccountId { get; set; }
    public String statusFilter { get; set; }
    public List<Case> cases { get; private set; }
    public Boolean hasSearched { get; private set; }

    public LegacyCaseManagementConsoleController() {
        cases = new List<Case>();
        hasSearched = false;
        statusFilter = LegacyCaseQueryService.STATUS_OPEN;
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

    public List<SelectOption> getStatusFilterOptions() {
        return new List<SelectOption>{
            new SelectOption(LegacyCaseQueryService.STATUS_OPEN, 'Open'),
            new SelectOption(LegacyCaseQueryService.STATUS_CLOSED, 'Closed'),
            new SelectOption(LegacyCaseQueryService.STATUS_ALL, 'All')
        };
    }

    public PageReference loadCases() {
        cases = new List<Case>();
        hasSearched = false;

        if (selectedAccountId == null) {
            ApexPages.addMessage(new ApexPages.Message(
                ApexPages.Severity.WARNING,
                'Select an account before loading cases.'
            ));
            return null;
        }

        try {
            cases = LegacyCaseQueryService.getCasesForAccount(selectedAccountId, statusFilter);
            hasSearched = true;
        } catch (LegacyCaseQueryService.CaseQueryException queryException) {
            cases = new List<Case>();
            hasSearched = false;
            ApexPages.addMessage(new ApexPages.Message(
                ApexPages.Severity.ERROR,
                queryException.getMessage()
            ));
        }

        return null;
    }

    public PageReference clearSelection() {
        selectedAccountId = null;
        statusFilter = LegacyCaseQueryService.STATUS_OPEN;
        cases = new List<Case>();
        hasSearched = false;
        return null;
    }
}
