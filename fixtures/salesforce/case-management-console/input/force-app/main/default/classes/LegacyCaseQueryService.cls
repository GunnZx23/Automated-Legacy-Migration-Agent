public with sharing class LegacyCaseQueryService {
    public static final String STATUS_OPEN = 'OPEN';
    public static final String STATUS_CLOSED = 'CLOSED';
    public static final String STATUS_ALL = 'ALL';

    @TestVisible
    private static final Integer MAX_CASES = 100;

    public class CaseQueryException extends Exception {
    }

    public static List<Case> getCasesForAccount(Id accountId, String statusFilter) {
        if (accountId == null) {
            return new List<Case>();
        }

        try {
            if (statusFilter == STATUS_CLOSED) {
                return [
                    SELECT Id, CaseNumber, Subject, Status, Priority, ContactId,
                           AccountId, Description, IsClosed, Contact.Name
                    FROM Case
                    WHERE AccountId = :accountId AND IsClosed = true
                    WITH USER_MODE
                    ORDER BY CaseNumber DESC
                    LIMIT :MAX_CASES
                ];
            }

            if (statusFilter == STATUS_ALL) {
                return [
                    SELECT Id, CaseNumber, Subject, Status, Priority, ContactId,
                           AccountId, Description, IsClosed, Contact.Name
                    FROM Case
                    WHERE AccountId = :accountId
                    WITH USER_MODE
                    ORDER BY CaseNumber DESC
                    LIMIT :MAX_CASES
                ];
            }

            return [
                SELECT Id, CaseNumber, Subject, Status, Priority, ContactId,
                       AccountId, Description, IsClosed, Contact.Name
                FROM Case
                WHERE AccountId = :accountId AND IsClosed = false
                WITH USER_MODE
                ORDER BY CaseNumber DESC
                LIMIT :MAX_CASES
            ];
        } catch (QueryException queryException) {
            throw new CaseQueryException('Unable to load cases right now. Please try again.');
        }
    }
}
