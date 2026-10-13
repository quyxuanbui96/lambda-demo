-- Rollback deletes workflow data; take a backup before running.

DROP TABLE managed_file;

DROP TABLE transfer_job;

DROP TABLE dispatch_state;

DROP TABLE source_file_version;

DROP TABLE operation_request;

DROP TABLE audit_log;

DROP TABLE acquisition_job;
