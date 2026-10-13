-- Initial workflow schema. Run explicitly with the DB/Infra team.

CREATE TABLE acquisition_job (
	id VARCHAR(36) NOT NULL,
	owner VARCHAR(128) NOT NULL,
	organization_id VARCHAR(128) NOT NULL,
	status VARCHAR(16) NOT NULL,
	version INTEGER NOT NULL,
	payload JSON NOT NULL,
	lease_until VARCHAR(40),
	PRIMARY KEY (id),
	CHECK (status IN ('QUEUED','RUNNING','READY','FAILED')),
	CHECK (version >= 0)
);

CREATE TABLE audit_log (
	id VARCHAR(36) NOT NULL,
	created_at VARCHAR(40) NOT NULL,
	action VARCHAR(64) NOT NULL,
	user_id VARCHAR(128) NOT NULL,
	organization_id VARCHAR(128) NOT NULL,
	resource_id VARCHAR(36) NOT NULL,
	request_id VARCHAR(36) NOT NULL,
	result VARCHAR(32) NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE operation_request (
	id SERIAL NOT NULL,
	owner VARCHAR(128) NOT NULL,
	action VARCHAR(16) NOT NULL,
	operation_key VARCHAR(36) NOT NULL,
	payload_hash VARCHAR(64) NOT NULL,
	resource_id VARCHAR(36) NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_operation_key UNIQUE (owner, action, operation_key)
);

CREATE TABLE source_file_version (
	id VARCHAR(36) NOT NULL,
	organization_id VARCHAR(128) NOT NULL,
	participant_code VARCHAR(64) NOT NULL,
	insurance_year INTEGER NOT NULL,
	business_type VARCHAR(128) NOT NULL,
	source_system VARCHAR(8) NOT NULL,
	bucket VARCHAR(63) NOT NULL,
	object_key VARCHAR(1024) NOT NULL,
	version_id VARCHAR(256) NOT NULL,
	captured_at VARCHAR(40) NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_source_version UNIQUE (organization_id, participant_code, insurance_year, business_type, source_system, bucket, object_key, version_id)
);

CREATE TABLE dispatch_state (
	acquisition_id VARCHAR(36) NOT NULL,
	status VARCHAR(16) NOT NULL,
	payload JSON NOT NULL,
	next_attempt_at VARCHAR(40) NOT NULL,
	PRIMARY KEY (acquisition_id),
	CHECK (status IN ('PENDING','SENT')),
	FOREIGN KEY(acquisition_id) REFERENCES acquisition_job (id)
);

CREATE TABLE transfer_job (
	id VARCHAR(36) NOT NULL,
	acquisition_id VARCHAR(36) NOT NULL,
	owner VARCHAR(128) NOT NULL,
	organization_id VARCHAR(128) NOT NULL,
	status VARCHAR(16) NOT NULL,
	version INTEGER NOT NULL,
	payload JSON NOT NULL,
	cleanup_at VARCHAR(40),
	PRIMARY KEY (id),
	CHECK (status IN ('PREPARED','TRANSFERRED','FAILED')),
	CHECK (version >= 0),
	FOREIGN KEY(acquisition_id) REFERENCES acquisition_job (id)
);

CREATE TABLE managed_file (
	id VARCHAR(36) NOT NULL,
	acquisition_id VARCHAR(36) NOT NULL,
	transfer_id VARCHAR(36),
	organization_id VARCHAR(128) NOT NULL,
	status VARCHAR(16) NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (id),
	CHECK (status IN ('RESERVED','AVAILABLE','FAILED')),
	FOREIGN KEY(acquisition_id) REFERENCES acquisition_job (id),
	FOREIGN KEY(transfer_id) REFERENCES transfer_job (id)
);

CREATE INDEX ix_acquisition_scope ON acquisition_job (organization_id, owner);

CREATE INDEX ix_audit_resource ON audit_log (resource_id, created_at);

CREATE INDEX ix_source_scope ON source_file_version (organization_id, participant_code, insurance_year, business_type);

CREATE INDEX ix_dispatch_status ON dispatch_state (status);

CREATE INDEX ix_transfer_scope ON transfer_job (organization_id, owner);
