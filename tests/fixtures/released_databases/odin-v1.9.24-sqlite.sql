-- SQLite database written by the published O.D.I.N. v1.9.24 release image on first boot.
-- Image: ghcr.io/hughkantsime/odin:v1.9.24@sha256:4ca8b96f8e0d41f3a8f046f9d33f678ec91f0a021188488661a368d59cb26e75
--        (linux/arm64 manifest sha256:615e91b03067ea582087887a78197eca5d25f0426e0ce563e0ccf204c8aa683a)
-- Produced by: docker run -v <empty dir>:/data <image>; wait for /health; docker stop; then
--              python3 -c "import sqlite3; print(chr(10).join(sqlite3.connect('odin.db').iterdump()))"
-- Schema and migration ledger only (no rows exist in any other table).
BEGIN TRANSACTION;
CREATE TABLE active_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_jti VARCHAR(64) UNIQUE NOT NULL,
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE alert_preferences (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	alert_type VARCHAR(19) NOT NULL, 
	in_app BOOLEAN, 
	browser_push BOOLEAN, 
	email BOOLEAN, 
	threshold_value FLOAT, 
	PRIMARY KEY (id)
);
CREATE TABLE alerts (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	alert_type VARCHAR(19) NOT NULL, 
	severity VARCHAR(8) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	message TEXT, 
	is_read BOOLEAN, 
	is_dismissed BOOLEAN, 
	printer_id INTEGER, 
	job_id INTEGER, 
	spool_id INTEGER, 
	metadata_json JSON, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id), 
	FOREIGN KEY(printer_id) REFERENCES printers (id) ON DELETE CASCADE, 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	FOREIGN KEY(spool_id) REFERENCES spools (id) ON DELETE SET NULL
);
CREATE TABLE ams_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    ams_unit INTEGER NOT NULL,
    humidity REAL,
    temperature REAL,
    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name VARCHAR(100) NOT NULL,
    token_hash VARCHAR(200) NOT NULL,
    token_prefix VARCHAR(10) NOT NULL,
    scopes TEXT DEFAULT '[]',
    expires_at DATETIME,
    last_used_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE audit_logs (
	id INTEGER NOT NULL, 
	timestamp DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	action VARCHAR(50) NOT NULL, 
	entity_type VARCHAR(50), 
	entity_id INTEGER, 
	details JSON, 
	ip_address VARCHAR(45), 
	PRIMARY KEY (id)
);
CREATE TABLE biometric_tokens (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	device_id VARCHAR(36) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	is_revoked BOOLEAN NOT NULL, 
	expires_at DATETIME NOT NULL, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	last_used_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_biometric_user_device UNIQUE (user_id, device_id), 
	UNIQUE (token_hash)
);
CREATE TABLE branding (
	id INTEGER NOT NULL, 
	app_name VARCHAR(100), 
	app_subtitle VARCHAR(200), 
	primary_color VARCHAR(7), 
	accent_color VARCHAR(7), 
	sidebar_bg VARCHAR(7), 
	sidebar_border VARCHAR(7), 
	sidebar_text VARCHAR(7), 
	sidebar_active_bg VARCHAR(7), 
	sidebar_active_text VARCHAR(7), 
	content_bg VARCHAR(7), 
	card_bg VARCHAR(7), 
	card_border VARCHAR(7), 
	text_primary VARCHAR(7), 
	text_secondary VARCHAR(7), 
	text_muted VARCHAR(7), 
	input_bg VARCHAR(7), 
	input_border VARCHAR(7), 
	font_display VARCHAR(200), 
	font_body VARCHAR(200), 
	font_mono VARCHAR(200), 
	logo_url VARCHAR(500), 
	favicon_url VARCHAR(500), 
	footer_text VARCHAR(500), 
	support_url VARCHAR(500), 
	updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id)
);
CREATE TABLE classroom_connections (
    org_id INTEGER PRIMARY KEY REFERENCES groups(id),
    client_id TEXT,
    client_secret_encrypted TEXT,
    allowed_domains TEXT,
    account_subject TEXT,
    account_email TEXT,
    granted_scopes TEXT,
    refresh_token_encrypted TEXT,
    access_token_encrypted TEXT,
    access_token_expires_at TEXT,
    state TEXT NOT NULL DEFAULT 'not_connected',
    last_success_at TEXT,
    last_error_code TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE classroom_course_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES groups(id),
    provider_course_id TEXT NOT NULL,
    cost_center_id INTEGER NOT NULL REFERENCES education_cost_centers(id),
    course_name TEXT NOT NULL,
    course_section TEXT,
    course_state TEXT,
    last_imported_at TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(org_id, provider_course_id),
    UNIQUE(org_id, cost_center_id)
);
CREATE TABLE classroom_oauth_states (
    state TEXT PRIMARY KEY,
    org_id INTEGER NOT NULL REFERENCES groups(id),
    admin_id INTEGER NOT NULL REFERENCES users(id),
    code_verifier_encrypted TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE classroom_roster_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER NOT NULL REFERENCES groups(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    provider_user_id TEXT NOT NULL,
    normalized_email TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',
    last_seen_at TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(org_id, provider_user_id),
    UNIQUE(org_id, normalized_email)
);
CREATE TABLE consumable_usage (
	id INTEGER NOT NULL, 
	consumable_id INTEGER NOT NULL, 
	order_id INTEGER, 
	quantity_used FLOAT NOT NULL, 
	used_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	notes VARCHAR(255), 
	PRIMARY KEY (id), 
	FOREIGN KEY(consumable_id) REFERENCES consumables (id) ON DELETE CASCADE, 
	FOREIGN KEY(order_id) REFERENCES orders (id) ON DELETE SET NULL
);
CREATE TABLE consumables (
	id INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	sku VARCHAR(50), 
	unit VARCHAR(20), 
	cost_per_unit FLOAT, 
	current_stock FLOAT, 
	min_stock FLOAT, 
	vendor VARCHAR(200), 
	notes TEXT, 
	status VARCHAR(20), 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id)
);
CREATE TABLE drying_logs (
	id INTEGER NOT NULL, 
	spool_id INTEGER NOT NULL, 
	dried_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	duration_hours FLOAT NOT NULL, 
	temp_c FLOAT, 
	method VARCHAR(50), 
	notes TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(spool_id) REFERENCES spools (id) ON DELETE CASCADE
);
CREATE TABLE education_audit_events (
            event_id VARCHAR(64) PRIMARY KEY,
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
            actor_kind VARCHAR(32) NOT NULL,
            actor_id VARCHAR(100) NOT NULL,
            action VARCHAR(64) NOT NULL,
            command_id VARCHAR(64) NOT NULL,
            request_hash VARCHAR(128) NOT NULL,
            resource_type VARCHAR(64) NOT NULL,
            resource_id VARCHAR(100) NOT NULL,
            cost_center_id INTEGER,
            lifecycle_revision INTEGER,
            details_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (event_id, org_id),
            UNIQUE (org_id, actor_kind, actor_id, action, command_id),
            FOREIGN KEY (cost_center_id, org_id) REFERENCES education_cost_centers(id, org_id) ON DELETE RESTRICT
        );
CREATE TABLE education_commands (
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
            actor_kind VARCHAR(32) NOT NULL,
            actor_id VARCHAR(100) NOT NULL,
            action VARCHAR(64) NOT NULL,
            command_id VARCHAR(64) NOT NULL,
            request_hash VARCHAR(128) NOT NULL,
            state VARCHAR(16) NOT NULL CHECK (state IN ('pending','complete')),
            result_json TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME,
            PRIMARY KEY (org_id, actor_kind, actor_id, action, command_id),
            CHECK ((state='pending' AND result_json IS NULL AND completed_at IS NULL) OR
                   (state='complete' AND result_json IS NOT NULL AND completed_at IS NOT NULL))
        );
CREATE TABLE education_cost_center_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL,
            cost_center_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role VARCHAR(16) NOT NULL CHECK (role IN ('student','manager')),
            state VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (state IN ('active','revoked')),
            granted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            granted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revoked_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            revoked_at DATETIME,
            UNIQUE (cost_center_id, user_id, role),
            FOREIGN KEY (cost_center_id, org_id) REFERENCES education_cost_centers(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (user_id, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        );
CREATE TABLE education_cost_center_printers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL,
            cost_center_id INTEGER NOT NULL,
            printer_id INTEGER NOT NULL,
            state VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (state IN ('active','revoked')),
            granted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            granted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revoked_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
            revoked_at DATETIME,
            UNIQUE (cost_center_id, printer_id),
            FOREIGN KEY (cost_center_id, org_id) REFERENCES education_cost_centers(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (printer_id, org_id) REFERENCES printers(id, org_id) ON DELETE RESTRICT
        );
CREATE TABLE education_cost_centers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
            name_key VARCHAR(200) NOT NULL,
            code_key VARCHAR(100) NOT NULL,
            display_name VARCHAR(200) NOT NULL,
            code VARCHAR(100) NOT NULL,
            description TEXT,
            state VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (state IN ('active','archived')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (id, org_id),
            UNIQUE (org_id, name_key),
            UNIQUE (org_id, code_key)
        );
CREATE TABLE education_monitor_claims (
                claim_id VARCHAR(36) PRIMARY KEY,
                org_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                job_id INTEGER NOT NULL,
                printer_id INTEGER NOT NULL,
                authority_revision INTEGER NOT NULL CHECK (authority_revision >= 1),
                token_digest VARCHAR(64) NOT NULL,
                state VARCHAR(16) NOT NULL CHECK (state IN ('reserved','awaiting','running')),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (submission_id, org_id)
                    REFERENCES education_submissions(id, org_id) ON DELETE RESTRICT,
                FOREIGN KEY (job_id, org_id)
                    REFERENCES jobs(id, charged_to_org_id) ON DELETE RESTRICT,
                FOREIGN KEY (printer_id, org_id)
                    REFERENCES printers(id, org_id) ON DELETE RESTRICT
            );
CREATE TABLE education_notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id VARCHAR(64) NOT NULL,
            org_id INTEGER NOT NULL,
            recipient_user_id INTEGER NOT NULL,
            state VARCHAR(24) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','leased','delivered','suppressed','dead_letter')),
            available_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            lease_owner VARCHAR(100),
            lease_expires_at DATETIME,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            last_error VARCHAR(500),
            delivered_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (event_id, recipient_user_id),
            FOREIGN KEY (event_id, org_id) REFERENCES education_audit_events(event_id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (recipient_user_id, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        );
CREATE TABLE education_rate_counters (
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            scope_kind VARCHAR(16) NOT NULL CHECK (scope_kind IN ('user','tenant')),
            scope_id INTEGER NOT NULL,
            user_id INTEGER,
            bucket_kind VARCHAR(16) NOT NULL CHECK (bucket_kind IN ('hour','day')),
            bucket_start DATETIME NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (org_id, scope_kind, scope_id, bucket_kind, bucket_start),
            CHECK ((scope_kind='tenant' AND scope_id=org_id AND user_id IS NULL) OR
                   (scope_kind='user' AND user_id=scope_id)),
            FOREIGN KEY (user_id, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        );
CREATE TABLE education_storage_accounts (
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
            scope_kind VARCHAR(16) NOT NULL CHECK (scope_kind IN ('user','tenant')),
            scope_id INTEGER NOT NULL,
            user_id INTEGER,
            reserved_bytes BIGINT NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
            accounted_bytes BIGINT NOT NULL DEFAULT 0 CHECK (accounted_bytes >= 0),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (org_id, scope_kind, scope_id),
            CHECK ((scope_kind='tenant' AND scope_id=org_id AND user_id IS NULL) OR
                   (scope_kind='user' AND user_id=scope_id)),
            FOREIGN KEY (user_id, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        );
CREATE TABLE education_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL,
            operation_id VARCHAR(64) NOT NULL UNIQUE,
            job_id INTEGER NOT NULL UNIQUE,
            print_file_id INTEGER NOT NULL UNIQUE,
            model_id INTEGER NOT NULL,
            cost_center_id INTEGER NOT NULL,
            submitted_by INTEGER NOT NULL,
            approved_printer_id INTEGER,
            approved_by INTEGER,
            status VARCHAR(24) NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted','pending','scheduled','printing','completed','failed','rejected','cancelled')),
            lifecycle_revision INTEGER NOT NULL DEFAULT 1 CHECK (lifecycle_revision >= 1),
            compatibility_engine_version VARCHAR(40),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (operation_id, org_id) REFERENCES education_upload_operations(operation_id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (cost_center_id, org_id) REFERENCES education_cost_centers(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (submitted_by, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT,
            FOREIGN KEY (job_id, org_id) REFERENCES jobs(id, charged_to_org_id) ON DELETE RESTRICT,
            FOREIGN KEY (print_file_id, org_id) REFERENCES print_files(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (model_id, org_id) REFERENCES models(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (approved_printer_id, org_id) REFERENCES printers(id, org_id) ON DELETE RESTRICT,
            FOREIGN KEY (approved_by, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        );
CREATE TABLE education_upload_operations (
            operation_id VARCHAR(64) PRIMARY KEY,
            org_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE RESTRICT,
            user_id INTEGER NOT NULL,
            state VARCHAR(32) NOT NULL CHECK (state IN ('reserved','streaming','staged','renamed','committed','aborted','needs_reconciliation','purge_pending','purged')),
            reserved_bytes BIGINT NOT NULL DEFAULT 0 CHECK (reserved_bytes >= 0),
            accounted_bytes BIGINT NOT NULL DEFAULT 0 CHECK (accounted_bytes >= 0),
            expected_bytes BIGINT,
            expected_hash VARCHAR(128),
            staging_path TEXT,
            final_path TEXT,
            cleanup_path TEXT,
            reservation_released_at DATETIME,
            purge_requested_at DATETIME,
            purged_at DATETIME,
            lease_owner VARCHAR(100),
            lease_expires_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (operation_id, org_id),
            FOREIGN KEY (user_id, org_id) REFERENCES users(id, group_id) ON DELETE RESTRICT
        );
CREATE TABLE filament_library (
	id INTEGER NOT NULL, 
	brand VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	material VARCHAR, 
	color_hex VARCHAR(6), 
	cost_per_gram FLOAT, 
	is_custom BOOLEAN, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id)
);
CREATE TABLE filament_slots (
	id INTEGER NOT NULL, 
	printer_id INTEGER NOT NULL, 
	slot_number INTEGER NOT NULL, 
	filament_type VARCHAR(11), 
	color VARCHAR(50), 
	color_hex VARCHAR(7), 
	spoolman_spool_id INTEGER, 
	assigned_spool_id INTEGER, 
	spool_confirmed BOOLEAN, 
	loaded_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id), 
	FOREIGN KEY(printer_id) REFERENCES printers (id) ON DELETE CASCADE, 
	FOREIGN KEY(assigned_spool_id) REFERENCES spools (id) ON DELETE SET NULL
);
CREATE TABLE groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    owner_id INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_org BOOLEAN DEFAULT 0,
    branding_json TEXT,
    settings_json TEXT
);
CREATE TABLE hms_error_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    message TEXT,
    severity TEXT DEFAULT 'warning',
    source TEXT DEFAULT 'bambu_hms',
    occurred_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE idempotency_keys (
    key TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    auth_fingerprint TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending',
    response_status INTEGER NOT NULL DEFAULT 0,
    response_body TEXT NOT NULL DEFAULT '',
    response_media_type TEXT NOT NULL DEFAULT 'application/json',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (key, user_id)
);
CREATE TABLE jobs (
	id INTEGER NOT NULL, 
	model_id INTEGER, 
	model_revision_id INTEGER, 
	item_name VARCHAR(200) NOT NULL, 
	quantity INTEGER, 
	status VARCHAR(9), 
	priority INTEGER, 
	printer_id INTEGER, 
	scheduled_start DATETIME, 
	scheduled_end DATETIME, 
	actual_start DATETIME, 
	actual_end DATETIME, 
	duration_hours FLOAT, 
	colors_required VARCHAR(500), 
	filament_type VARCHAR(11), 
	match_score INTEGER, 
	is_locked BOOLEAN, 
	hold BOOLEAN, 
	notes TEXT, 
	estimated_cost FLOAT, 
	suggested_price FLOAT, 
	order_item_id INTEGER, 
	quantity_on_bed INTEGER, 
	due_date DATETIME, 
	submitted_by INTEGER, 
	approved_by INTEGER, 
	approved_at DATETIME, 
	rejected_reason TEXT, 
	fail_reason VARCHAR(100), 
	fail_notes TEXT, 
	charged_to_user_id INTEGER, 
	charged_to_org_id INTEGER, 
	is_favorite BOOLEAN, 
	required_tags JSON, 
	target_type VARCHAR(20), 
	target_filter VARCHAR(100), 
	queue_position INTEGER, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id), 
	FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE SET NULL, 
	FOREIGN KEY(printer_id) REFERENCES printers (id) ON DELETE SET NULL, 
	FOREIGN KEY(order_item_id) REFERENCES order_items (id) ON DELETE SET NULL
);
CREATE TABLE login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    username TEXT NOT NULL,
    attempted_at REAL NOT NULL,
    success INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE maintenance_logs (
	id INTEGER NOT NULL, 
	printer_id INTEGER NOT NULL, 
	task_id INTEGER, 
	task_name VARCHAR(200) NOT NULL, 
	performed_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	performed_by VARCHAR(100), 
	notes TEXT, 
	cost FLOAT, 
	downtime_minutes INTEGER, 
	print_hours_at_service FLOAT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(printer_id) REFERENCES printers (id) ON DELETE CASCADE, 
	FOREIGN KEY(task_id) REFERENCES maintenance_tasks (id) ON DELETE SET NULL
);
CREATE TABLE maintenance_tasks (
	id INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	description TEXT, 
	printer_model_filter VARCHAR(100), 
	interval_print_hours FLOAT, 
	interval_days INTEGER, 
	estimated_cost FLOAT, 
	estimated_downtime_min INTEGER, 
	is_active BOOLEAN, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id)
);
CREATE TABLE model_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL REFERENCES models(id),
    revision_number INTEGER NOT NULL DEFAULT 1,
    file_path TEXT,
    changelog TEXT,
    uploaded_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(model_id, revision_number)
);
CREATE TABLE models (
	id INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	build_time_hours FLOAT, 
	default_filament_type VARCHAR(11), 
	color_requirements JSON, 
	category VARCHAR(100), 
	thumbnail_url VARCHAR(500), 
	thumbnail_b64 TEXT, 
	print_file_id INTEGER, 
	notes TEXT, 
	cost_per_item FLOAT, 
	units_per_bed INTEGER, 
	quantity_per_bed INTEGER, 
	markup_percent FLOAT, 
	is_favorite BOOLEAN, 
	org_id INTEGER, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id)
);
CREATE TABLE nozzle_lifecycle (
	id INTEGER NOT NULL, 
	printer_id INTEGER NOT NULL, 
	nozzle_type VARCHAR(20), 
	nozzle_diameter FLOAT, 
	installed_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	removed_at DATETIME, 
	print_hours_accumulated FLOAT, 
	print_count INTEGER, 
	notes TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(printer_id) REFERENCES printers (id) ON DELETE CASCADE
);
CREATE TABLE odin_schema_migrations (
	migration_id VARCHAR(255) NOT NULL, 
	checksum VARCHAR(64) NOT NULL, 
	dialect VARCHAR(32) NOT NULL, 
	applied_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL, 
	PRIMARY KEY (migration_id)
);
INSERT INTO "odin_schema_migrations" VALUES('core/migrations/001_initial.sql','43e82ad07a87855aad78b583cf04a64fb9e8ab0c15bcaa51db44c66cd3d271eb','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('core/migrations/002_quiet_hours_digest_sends.sql','01db7b15a96ebc8485e0c720da44359ecf8be8ae99c51584c0320eb9afa77cc1','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('core/migrations/003_quiet_hours_org_digest_sends.sql','6cb56bddb984e3a1043924449068b6eed64dfd48549d2fa267164d0e50bc13ac','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('core/migrations/004_drop_setup_token_add_delivery_status.sql','edb13c649e36350247f3232e006da03e9ccc4ecebafa0a89c925b5925723fcd7','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('core/migrations/005_idempotency_keys.sql','d50f1e1accc3b91e4bc4cdfa195e733232f90844b2f2309a2ef25425656576ab','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('core/migrations/006_idempotency_keys_upgrade.sql','497f48802efccdcb536cec9f81d72ac422c569765027ba11a165b90229378837','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('core/migrations/007_websocket_events.sql','79a98de03ab550a17c1f11446c7d932dee323fdd66e87d3268cf216942782ea8','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/archives/migrations/001_initial.sql','19011172cfdbe42268e95f4303a16610588d8ebab652c58b29f1cc9ef3422b58','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/inventory/migrations/001_initial.sql','260dea18a426e3c6fbd38a034c64a6c321746b6d6df07b994c00c529d6963a6d','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/jobs/migrations/001_initial.sql','8584aca97bb1b8104cdd17f04c7b69935938a5be5792897e5ec8470a4298eb24','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/models_library/migrations/001_initial.sql','e20376d09c6314ddcb32845a39214bd99629991c1676c9a733ade3930491aaff','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/notifications/migrations/001_initial.sql','f95c2819213b3e342158c3f84d669d4491e15ea24a3426b3f0e7ebb80a248ea1','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/orders/migrations/001_initial.sql','8152ca1bb4d3ec7e031ecc2550a4f6385da2c005271ef597643195bffc6b68e7','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/orders/migrations/002_add_org_id.sql','91a25e5394ae981dd57fc547ba778a30ebb91c329a4d95815408b95fbb3c74c1','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/organizations/migrations/001_initial.sql','b35d61972fcb381f6ea3ce6b92193eed80f60817e8d9279e9f869b76a2dd2273','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/organizations/migrations/002_oidc_provider_policy.sql','19bfc273da6fe0923c142ec72c2ad73ea82d88230bccd91ec3cc24fdd8d240e3','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/printers/migrations/001_initial.sql','42684d365e7aebd960fe6f1f787c94494e48de0be0d71cc7834884040177edcc','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/push/migrations/001_initial.sql','95d0849549385486b4a6524e47de980f6cad4fb208abfd0677aa1be2169dca4c','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/reporting/migrations/001_initial.sql','d698a2eb65e35b2bb2d505962da55e0c8109e4e4dc8e49bc7f58a943593bd54a','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/system/migrations/001_initial.sql','304d941ff105d8fc49fcb60549d7a8e0b61eb129036d7d7674f913a039ae6a77','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/vision/migrations/001_initial.sql','8a1fb12c8600dbae6b74572e17407081adda6f9a30cad11cd30ebcafdc52fe78','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('python:001-legacy-columns','5fd102e1eda891937c2f8ccfed7f86e543b0fba33166402eeb26431693f445b7','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('python:002-education-tenant-integrity','a14ec2f537fa368e63e1e0410557d2b2aab15a0dc9663c3f2dbc46ba026c4f68','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('modules/organizations/migrations/003_google_classroom.sql','d67716a70d46074589dd893ecad143b25df81ec6b82382a62f0b18fc666faa9a','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('python:003-education-monitor-claims','94ba22755ec4f2db07265ddbacef906657f7888858a30ae6a1fb7e1e597310dc','sqlite','2026-10-15 09:27:42');
INSERT INTO "odin_schema_migrations" VALUES('orm-baseline:1','36d66151e15f6a2534ddc98e5f3bf4049a9796570197e32d227e868ab93cab0b','sqlite','2026-10-15 09:27:42');
CREATE TABLE oidc_auth_codes (
    code TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE oidc_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT DEFAULT 'SSO Login',
    client_id TEXT,
    client_secret_encrypted TEXT,
    tenant_id TEXT,
    discovery_url TEXT,
    scopes TEXT DEFAULT 'openid profile email',
    auto_create_users BOOLEAN DEFAULT 0,
    default_role TEXT DEFAULT 'viewer',
    is_enabled BOOLEAN DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
, provider_type TEXT NOT NULL DEFAULT 'microsoft', allowed_domains TEXT, default_group_id INTEGER);
CREATE TABLE oidc_pending_states (
    state TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
, nonce TEXT NOT NULL DEFAULT '');
CREATE TABLE order_items (
	id INTEGER NOT NULL, 
	order_id INTEGER NOT NULL, 
	product_id INTEGER NOT NULL, 
	quantity INTEGER, 
	unit_price FLOAT, 
	fulfilled_quantity INTEGER, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id), 
	FOREIGN KEY(order_id) REFERENCES orders (id) ON DELETE CASCADE, 
	FOREIGN KEY(product_id) REFERENCES products (id)
);
CREATE TABLE orders (
	id INTEGER NOT NULL, 
	order_number VARCHAR(100), 
	platform VARCHAR(50), 
	customer_name VARCHAR(200), 
	customer_email VARCHAR(200), 
	status VARCHAR(11), 
	revenue FLOAT, 
	platform_fees FLOAT, 
	payment_fees FLOAT, 
	shipping_charged FLOAT, 
	shipping_cost FLOAT, 
	labor_minutes INTEGER, 
	notes TEXT, 
	order_date DATETIME, 
	shipped_date DATETIME, 
	tracking_number VARCHAR(100), 
	org_id INTEGER, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id)
);
CREATE TABLE password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    expires_at DATETIME NOT NULL,
    used INTEGER DEFAULT 0
);
CREATE TABLE print_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    print_job_id INTEGER,
    printer_id INTEGER,
    user_id INTEGER,
    print_name TEXT,
    status TEXT,
    started_at DATETIME,
    completed_at DATETIME,
    actual_duration_seconds INTEGER,
    filament_used_grams REAL,
    cost_estimate REAL,
    thumbnail_b64 TEXT,
    file_path TEXT,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    tags TEXT DEFAULT '',
    plate_count INTEGER DEFAULT 1,
    plate_thumbnails TEXT,
    print_file_id INTEGER,
    project_id INTEGER,
    energy_kwh REAL,
    energy_cost REAL,
    consumption_json TEXT,
    file_hash TEXT
);
CREATE TABLE print_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT,
    original_filename TEXT,
    project_name TEXT,
    print_time_seconds REAL,
    total_weight_grams REAL,
    layer_count INTEGER,
    layer_height REAL,
    nozzle_diameter REAL,
    printer_model TEXT,
    supports_used BOOLEAN DEFAULT 0,
    bed_type TEXT,
    filaments_json TEXT,
    thumbnail_b64 TEXT,
    mesh_data TEXT,
    model_id INTEGER,
    job_id INTEGER,
    uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    stored_path TEXT,
    filament_weight_grams REAL,
    bed_x_mm REAL,
    bed_y_mm REAL,
    compatible_api_types TEXT,
    file_hash TEXT,
    plate_count INTEGER DEFAULT 1
, org_id INTEGER, created_by INTEGER, storage_bytes BIGINT NOT NULL DEFAULT 0, blob_state VARCHAR(24) NOT NULL DEFAULT 'present', compatibility_facts_json TEXT);
CREATE TABLE print_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL REFERENCES printers(id),
    job_id TEXT,
    filename TEXT,
    job_name TEXT,
    started_at DATETIME NOT NULL,
    ended_at DATETIME,
    status TEXT DEFAULT 'running',
    progress_percent REAL DEFAULT 0,
    remaining_minutes REAL,
    total_layers INTEGER,
    current_layer INTEGER,
    bed_temp_target REAL,
    nozzle_temp_target REAL,
    filament_slots TEXT,
    error_code TEXT,
    scheduled_job_id INTEGER,
    created_at DATETIME DEFAULT (datetime('now')),
    model_revision_id INTEGER
);
CREATE TABLE print_presets (
	id INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	model_id INTEGER, 
	item_name VARCHAR(200), 
	quantity INTEGER, 
	priority INTEGER, 
	duration_hours FLOAT, 
	colors_required VARCHAR(500), 
	filament_type VARCHAR(11), 
	required_tags JSON, 
	notes TEXT, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id), 
	UNIQUE (name), 
	FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE SET NULL
);
CREATE TABLE printer_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by INTEGER REFERENCES users(id),
    printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
    org_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    description TEXT,
    slicer TEXT NOT NULL,
    category TEXT NOT NULL,
    file_format TEXT NOT NULL DEFAULT 'json',
    filament_type TEXT,
    raw_content TEXT NOT NULL,
    is_shared INTEGER DEFAULT 1,
    is_default INTEGER DEFAULT 0,
    tags TEXT,
    last_applied_at DATETIME,
    last_applied_printer_id INTEGER REFERENCES printers(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE printer_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    bed_temp REAL,
    nozzle_temp REAL,
    bed_target REAL,
    nozzle_target REAL,
    fan_speed INTEGER,
    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE printers (
	id INTEGER NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	nickname VARCHAR(100), 
	model VARCHAR(100), 
	slot_count INTEGER, 
	is_active BOOLEAN, 
	display_order INTEGER, 
	camera_url VARCHAR, 
	camera_enabled BOOLEAN, 
	api_type VARCHAR(50), 
	api_host VARCHAR(255), 
	api_key VARCHAR(255), 
	is_favorite BOOLEAN, 
	last_seen DATETIME, 
	bed_temp FLOAT, 
	bed_target_temp FLOAT, 
	nozzle_temp FLOAT, 
	nozzle_target_temp FLOAT, 
	gcode_state VARCHAR(20), 
	print_stage VARCHAR(50), 
	hms_errors TEXT, 
	lights_on BOOLEAN, 
	lights_toggled_at DATETIME, 
	nozzle_type VARCHAR(20), 
	nozzle_diameter FLOAT, 
	fan_speed INTEGER, 
	bed_x_mm FLOAT, 
	bed_y_mm FLOAT, 
	plug_type VARCHAR(20), 
	plug_host VARCHAR(255), 
	plug_entity_id VARCHAR(255), 
	plug_auth_token TEXT, 
	plug_auto_on BOOLEAN, 
	plug_auto_off BOOLEAN, 
	plug_cooldown_minutes INTEGER, 
	plug_power_state BOOLEAN, 
	plug_energy_kwh FLOAT, 
	total_print_hours FLOAT, 
	total_print_count INTEGER, 
	hours_since_maintenance FLOAT, 
	prints_since_maintenance INTEGER, 
	last_error_code VARCHAR(50), 
	last_error_message TEXT, 
	last_error_at DATETIME, 
	camera_discovered BOOLEAN, 
	tags JSON, 
	timelapse_enabled BOOLEAN, 
	machine_type VARCHAR(20), 
	org_id INTEGER, 
	shared BOOLEAN, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id), 
	UNIQUE (name)
);
CREATE TABLE product_components (
	id INTEGER NOT NULL, 
	product_id INTEGER NOT NULL, 
	model_id INTEGER NOT NULL, 
	quantity_needed INTEGER, 
	notes TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE CASCADE, 
	FOREIGN KEY(model_id) REFERENCES models (id) ON DELETE CASCADE
);
CREATE TABLE product_consumables (
	id INTEGER NOT NULL, 
	product_id INTEGER NOT NULL, 
	consumable_id INTEGER NOT NULL, 
	quantity_per_product FLOAT, 
	notes TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE CASCADE, 
	FOREIGN KEY(consumable_id) REFERENCES consumables (id) ON DELETE CASCADE
);
CREATE TABLE products (
	id INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	sku VARCHAR(50), 
	price FLOAT, 
	description TEXT, 
	org_id INTEGER, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id)
);
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by INTEGER,
    org_id INTEGER,
    name TEXT NOT NULL,
    description TEXT,
    color TEXT DEFAULT '#6366f1',
    status TEXT DEFAULT 'active',
    expected_parts INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE push_devices (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	device_id VARCHAR(36) NOT NULL, 
	platform VARCHAR(20) NOT NULL, 
	token TEXT NOT NULL, 
	live_activity_token TEXT, 
	preferences_json TEXT, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	last_seen_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_push_device_user_device UNIQUE (user_id, device_id)
);
CREATE TABLE push_subscriptions (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	endpoint TEXT NOT NULL, 
	p256dh_key TEXT NOT NULL, 
	auth_key TEXT NOT NULL, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id)
);
CREATE TABLE quiet_hours_digest_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    org_id INTEGER,
    window_ended_at TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, delivery_status TEXT DEFAULT 'pending',
    UNIQUE(user_id, window_ended_at)
);
CREATE TABLE quiet_hours_org_digest_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER,
    window_ended_at TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, delivery_status TEXT DEFAULT 'pending',
    UNIQUE(org_id, window_ended_at)
);
CREATE TABLE quota_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    period_key VARCHAR(20) NOT NULL,
    grams_used REAL DEFAULT 0,
    hours_used REAL DEFAULT 0,
    jobs_used INTEGER DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, period_key)
);
CREATE TABLE report_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) NOT NULL,
    report_type VARCHAR(50) NOT NULL,
    frequency VARCHAR(20) NOT NULL DEFAULT 'weekly',
    recipients TEXT NOT NULL,
    filters TEXT DEFAULT '{}',
    is_active BOOLEAN DEFAULT 1,
    next_run_at DATETIME,
    last_run_at DATETIME,
    created_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE scheduler_runs (
	id INTEGER NOT NULL, 
	run_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	total_jobs INTEGER, 
	scheduled_count INTEGER, 
	skipped_count INTEGER, 
	setup_blocks INTEGER, 
	avg_match_score FLOAT, 
	avg_job_duration FLOAT, 
	notes TEXT, 
	PRIMARY KEY (id)
);
CREATE TABLE spool_usage (
	id INTEGER NOT NULL, 
	spool_id INTEGER NOT NULL, 
	job_id INTEGER, 
	weight_used_g FLOAT NOT NULL, 
	used_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	notes VARCHAR(255), 
	PRIMARY KEY (id), 
	FOREIGN KEY(spool_id) REFERENCES spools (id) ON DELETE CASCADE, 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE SET NULL
);
CREATE TABLE spools (
	id INTEGER NOT NULL, 
	filament_id INTEGER NOT NULL, 
	qr_code VARCHAR(50), 
	rfid_tag VARCHAR(32), 
	color_hex VARCHAR(6), 
	initial_weight_g FLOAT, 
	remaining_weight_g FLOAT, 
	spool_weight_g FLOAT, 
	price FLOAT, 
	purchase_date DATETIME, 
	vendor VARCHAR(100), 
	lot_number VARCHAR(50), 
	status VARCHAR(8), 
	location_printer_id INTEGER, 
	location_slot INTEGER, 
	storage_location VARCHAR(100), 
	notes TEXT, 
	org_id INTEGER, 
	pa_profile VARCHAR(50), 
	low_stock_threshold_g INTEGER, 
	spoolman_spool_id INTEGER, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id), 
	FOREIGN KEY(filament_id) REFERENCES filament_library (id), 
	FOREIGN KEY(location_printer_id) REFERENCES printers (id) ON DELETE SET NULL
);
CREATE TABLE system_config (
	"key" VARCHAR(100) NOT NULL, 
	value JSON NOT NULL, 
	updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY ("key")
);
CREATE TABLE timelapses (
	id INTEGER NOT NULL, 
	printer_id INTEGER NOT NULL, 
	print_job_id INTEGER, 
	filename VARCHAR(255) NOT NULL, 
	frame_count INTEGER, 
	duration_seconds FLOAT, 
	file_size_mb FLOAT, 
	status VARCHAR(20), 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(printer_id) REFERENCES printers (id) ON DELETE CASCADE
);
CREATE TABLE token_blacklist (
    jti VARCHAR(64) PRIMARY KEY,
    expires_at DATETIME NOT NULL
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(100) UNIQUE NOT NULL,
    email VARCHAR(200),
    password_hash VARCHAR(200) NOT NULL,
    role VARCHAR(20) DEFAULT 'viewer',
    is_active BOOLEAN DEFAULT 1,
    last_login DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    oidc_subject VARCHAR(200),
    oidc_provider VARCHAR(50),
    mfa_enabled BOOLEAN DEFAULT 0,
    mfa_secret TEXT,
    quota_grams REAL,
    quota_hours REAL,
    quota_jobs INTEGER,
    quota_period VARCHAR(20) DEFAULT 'monthly',
    theme_json TEXT,
    group_id INTEGER
, oidc_issuer VARCHAR(500));
CREATE TABLE vision_detections (
	id INTEGER NOT NULL, 
	printer_id INTEGER NOT NULL, 
	print_job_id INTEGER, 
	detection_type TEXT NOT NULL, 
	confidence FLOAT NOT NULL, 
	status TEXT, 
	frame_path TEXT, 
	bbox_json TEXT, 
	metadata_json TEXT, 
	reviewed_by INTEGER, 
	reviewed_at DATETIME, 
	created_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id), 
	FOREIGN KEY(printer_id) REFERENCES printers (id) ON DELETE CASCADE
);
CREATE TABLE vision_models (
	id INTEGER NOT NULL, 
	name TEXT NOT NULL, 
	detection_type TEXT NOT NULL, 
	filename TEXT NOT NULL, 
	version TEXT, 
	input_size INTEGER, 
	is_active INTEGER, 
	metadata_json TEXT, 
	uploaded_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (id)
);
CREATE TABLE vision_settings (
	printer_id INTEGER NOT NULL, 
	enabled INTEGER, 
	spaghetti_enabled INTEGER, 
	spaghetti_threshold FLOAT, 
	first_layer_enabled INTEGER, 
	first_layer_threshold FLOAT, 
	detachment_enabled INTEGER, 
	detachment_threshold FLOAT, 
	build_plate_empty_enabled INTEGER, 
	build_plate_empty_threshold FLOAT, 
	auto_pause INTEGER, 
	capture_interval_sec INTEGER, 
	collect_training_data INTEGER, 
	updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), 
	PRIMARY KEY (printer_id), 
	FOREIGN KEY(printer_id) REFERENCES printers (id) ON DELETE CASCADE
);
CREATE TABLE webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    webhook_type TEXT DEFAULT 'generic',
    alert_types TEXT,
    is_enabled BOOLEAN DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ws_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX ix_filament_library_id ON filament_library (id);
CREATE INDEX ix_alert_preferences_user_id ON alert_preferences (user_id);
CREATE INDEX ix_push_subscriptions_user_id ON push_subscriptions (user_id);
CREATE INDEX ix_push_devices_user_id ON push_devices (user_id);
CREATE INDEX ix_push_devices_device_id ON push_devices (device_id);
CREATE INDEX ix_biometric_tokens_user_id ON biometric_tokens (user_id);
CREATE INDEX ix_biometric_tokens_device_id ON biometric_tokens (device_id);
CREATE UNIQUE INDEX ix_spools_rfid_tag ON spools (rfid_tag);
CREATE UNIQUE INDEX ix_spools_qr_code ON spools (qr_code);
CREATE INDEX ix_alerts_is_read ON alerts (is_read);
CREATE INDEX ix_alerts_created_at ON alerts (created_at);
CREATE INDEX ix_alerts_user_id ON alerts (user_id);
CREATE INDEX idx_login_attempts_ip ON login_attempts(ip);
CREATE INDEX idx_login_attempts_username ON login_attempts(username);
CREATE INDEX idx_login_attempts_at ON login_attempts(attempted_at);
CREATE INDEX idx_qhds_window_ended_at
    ON quiet_hours_digest_sends(window_ended_at);
CREATE INDEX idx_qhds_org_id
    ON quiet_hours_digest_sends(org_id);
CREATE INDEX idx_qhods_window_ended_at
    ON quiet_hours_org_digest_sends(window_ended_at);
CREATE INDEX ix_idempotency_keys_created_at
    ON idempotency_keys(created_at);
CREATE INDEX ix_idempotency_keys_state
    ON idempotency_keys(state);
CREATE INDEX idx_ws_events_created ON ws_events(created_at);
CREATE INDEX idx_print_archives_printer ON print_archives(printer_id);
CREATE INDEX idx_print_archives_status ON print_archives(status);
CREATE INDEX idx_print_archives_created ON print_archives(created_at);
CREATE INDEX idx_print_jobs_printer ON print_jobs(printer_id);
CREATE INDEX idx_print_jobs_status ON print_jobs(status);
CREATE INDEX idx_print_jobs_started ON print_jobs(started_at);
CREATE INDEX idx_print_files_model ON print_files(model_id);
CREATE INDEX idx_print_files_job ON print_files(job_id);
CREATE INDEX idx_printer_telemetry_printer ON printer_telemetry(printer_id);
CREATE INDEX idx_printer_telemetry_recorded ON printer_telemetry(recorded_at);
CREATE INDEX idx_hms_history_printer ON hms_error_history(printer_id);
CREATE INDEX idx_hms_history_occurred ON hms_error_history(occurred_at);
CREATE INDEX idx_ams_telemetry_printer ON ams_telemetry(printer_id);
CREATE INDEX idx_ams_telemetry_recorded ON ams_telemetry(recorded_at);
CREATE INDEX idx_printer_profiles_slicer ON printer_profiles(slicer);
CREATE INDEX idx_printer_profiles_category ON printer_profiles(category);
CREATE UNIQUE INDEX uq_users_id_group ON users(id, group_id);
CREATE UNIQUE INDEX uq_printers_id_org ON printers(id, org_id);
CREATE UNIQUE INDEX uq_models_id_org ON models(id, org_id);
CREATE UNIQUE INDEX uq_jobs_id_org ON jobs(id, charged_to_org_id);
CREATE UNIQUE INDEX uq_print_files_id_org ON print_files(id, org_id);
CREATE UNIQUE INDEX uq_users_oidc_identity ON users(oidc_issuer, oidc_subject) WHERE oidc_issuer IS NOT NULL AND oidc_subject IS NOT NULL;
CREATE UNIQUE INDEX uq_education_centers_id_org ON education_cost_centers(id, org_id);
CREATE UNIQUE INDEX uq_education_centers_name ON education_cost_centers(org_id, name_key);
CREATE UNIQUE INDEX uq_education_centers_code ON education_cost_centers(org_id, code_key);
CREATE INDEX ix_education_grants_active ON education_cost_center_grants(org_id, user_id, state, role);
CREATE INDEX ix_education_printers_active ON education_cost_center_printers(org_id, printer_id, state);
CREATE INDEX ix_education_submissions_queue ON education_submissions(org_id, cost_center_id, status, created_at);
CREATE INDEX ix_education_upload_state ON education_upload_operations(state, lease_expires_at);
CREATE INDEX ix_education_outbox_claim ON education_notification_outbox(state, available_at, lease_expires_at);
CREATE TRIGGER trg_education_audit_no_update BEFORE UPDATE ON education_audit_events BEGIN SELECT RAISE(ABORT, 'education audit events are immutable'); END;
CREATE TRIGGER trg_education_audit_no_delete BEFORE DELETE ON education_audit_events BEGIN SELECT RAISE(ABORT, 'education audit events are immutable'); END;
CREATE INDEX ix_classroom_mappings_center
    ON classroom_course_mappings(cost_center_id);
CREATE INDEX ix_classroom_roster_user
    ON classroom_roster_identities(user_id);
CREATE UNIQUE INDEX uq_education_submissions_id_org ON education_submissions(id, org_id);
CREATE UNIQUE INDEX uq_education_monitor_claim_submission ON education_monitor_claims(submission_id);
CREATE UNIQUE INDEX uq_education_monitor_claim_job ON education_monitor_claims(job_id);
CREATE UNIQUE INDEX uq_education_monitor_claim_printer ON education_monitor_claims(printer_id);
CREATE UNIQUE INDEX uq_education_monitor_claim_token ON education_monitor_claims(token_digest);
CREATE INDEX ix_education_monitor_claim_state ON education_monitor_claims(state, updated_at);
DELETE FROM "sqlite_sequence";
COMMIT;
