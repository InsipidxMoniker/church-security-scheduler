CREATE TABLE admin_users (
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE CHECK(length(username) BETWEEN 1 AND 100),
 password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','scheduler')),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), credential_epoch INTEGER NOT NULL DEFAULT 1 CHECK(credential_epoch>0),
 totp_secret TEXT NOT NULL, enrolled INTEGER NOT NULL DEFAULT 0 CHECK(enrolled IN (0,1)),
 last_totp_step INTEGER NOT NULL DEFAULT -1, created_at REAL NOT NULL DEFAULT (unixepoch()), updated_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TRIGGER last_admin BEFORE UPDATE OF active,role ON admin_users
WHEN OLD.active=1 AND OLD.role='admin' AND (NEW.active=0 OR NEW.role!='admin')
AND (SELECT count(*) FROM admin_users WHERE active=1 AND role='admin')<=1
BEGIN SELECT RAISE(ABORT,'last active administrator'); END;
CREATE TRIGGER no_staff_delete BEFORE DELETE ON admin_users
BEGIN SELECT RAISE(ABORT,'deactivate staff instead'); END;
CREATE TABLE recovery_codes (
 user_id TEXT NOT NULL REFERENCES admin_users(id) ON DELETE RESTRICT,
 code_hash TEXT NOT NULL, consumed_at REAL, PRIMARY KEY(user_id,code_hash)
);
CREATE TABLE settings (
 id INTEGER PRIMARY KEY CHECK(id=1), timezone TEXT NOT NULL DEFAULT '',
 service_1_label TEXT NOT NULL DEFAULT '', service_2_label TEXT NOT NULL DEFAULT '',
 service_1_time TEXT, service_2_time TEXT,
 essentiality_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(essentiality_confirmed IN (0,1)),
 settings_revision INTEGER NOT NULL DEFAULT 1 CHECK(settings_revision>0),
 config_epoch INTEGER NOT NULL DEFAULT 1 CHECK(config_epoch>0), roster_epoch INTEGER NOT NULL DEFAULT 1 CHECK(roster_epoch>0),
 security_settings TEXT NOT NULL DEFAULT '{"pin_digits":8,"link_days":30}',
 created_at REAL NOT NULL DEFAULT (unixepoch()), updated_at REAL NOT NULL DEFAULT (unixepoch())
);
INSERT INTO settings(id) VALUES(1);
CREATE TABLE positions (
 number INTEGER PRIMARY KEY CHECK(number BETWEEN 1 AND 8),
 zone TEXT NOT NULL CHECK((number IN (1,2,3,8) AND zone='outside') OR (number IN (4,5,6,7) AND zone='inside')),
 restricted_required INTEGER NOT NULL CHECK(restricted_required=CASE WHEN number IN (1,2) THEN 1 ELSE 0 END),
 essential INTEGER NOT NULL DEFAULT 1 CHECK(essential IN (0,1))
);
INSERT INTO positions(number,zone,restricted_required) VALUES(1,'outside',1),(2,'outside',1),(3,'outside',0),(4,'inside',0),(5,'inside',0),(6,'inside',0),(7,'inside',0),(8,'outside',0);
CREATE TRIGGER fixed_positions BEFORE UPDATE OF number,zone,restricted_required ON positions
BEGIN SELECT RAISE(ABORT,'position mapping is fixed'); END;
CREATE TRIGGER no_position_delete BEFORE DELETE ON positions
BEGIN SELECT RAISE(ABORT,'position mapping is fixed'); END;
CREATE TABLE volunteers (
 id TEXT PRIMARY KEY, display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 100),
 normalized_label TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)),
 restricted_qualified INTEGER NOT NULL CHECK(restricted_qualified IN (0,1)), pin_hash TEXT,
 credential_epoch INTEGER NOT NULL DEFAULT 1 CHECK(credential_epoch>0), revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 created_at REAL NOT NULL DEFAULT (unixepoch()), updated_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE UNIQUE INDEX active_label ON volunteers(normalized_label) WHERE active=1;
CREATE TABLE service_dates (
 id TEXT PRIMARY KEY, local_date TEXT NOT NULL UNIQUE
 CHECK(length(local_date)=10 AND date(local_date,'+0 days') IS NOT NULL AND date(local_date,'+0 days')=local_date AND strftime('%w',local_date)='0'),
 timezone_snapshot TEXT NOT NULL CHECK(length(timezone_snapshot)>0),
 availability_open INTEGER NOT NULL DEFAULT 1 CHECK(availability_open IN (0,1)), first_published_at REAL,
 date_input_epoch INTEGER NOT NULL DEFAULT 1 CHECK(date_input_epoch>0), current_published_version_id TEXT,
 created_at REAL NOT NULL DEFAULT (unixepoch()), updated_at REAL NOT NULL DEFAULT (unixepoch()),
 CHECK(first_published_at IS NULL OR availability_open=0),
 FOREIGN KEY(id,current_published_version_id) REFERENCES schedule_versions(date_id,id) ON DELETE RESTRICT
);
CREATE TRIGGER date_timezone_immutable BEFORE UPDATE OF timezone_snapshot,local_date ON service_dates
WHEN NEW.timezone_snapshot!=OLD.timezone_snapshot OR NEW.local_date!=OLD.local_date
BEGIN SELECT RAISE(ABORT,'date identity and timezone are immutable'); END;
CREATE TRIGGER first_publication_immutable BEFORE UPDATE OF first_published_at ON service_dates
WHEN OLD.first_published_at IS NOT NULL AND NEW.first_published_at IS NOT OLD.first_published_at
BEGIN SELECT RAISE(ABORT,'first publication is irreversible'); END;
CREATE TABLE date_links (
 id TEXT PRIMARY KEY, date_id TEXT NOT NULL REFERENCES service_dates(id) ON DELETE RESTRICT,
 token_digest TEXT NOT NULL UNIQUE, generation INTEGER NOT NULL CHECK(generation>0),
 created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL,
 creator TEXT NOT NULL REFERENCES admin_users(id) ON DELETE RESTRICT,
 UNIQUE(date_id,id), CHECK(expires_at>created_at)
);
CREATE UNIQUE INDEX current_link ON date_links(date_id) WHERE revoked_at IS NULL;
CREATE TABLE availability (
 date_id TEXT NOT NULL REFERENCES service_dates(id) ON DELETE RESTRICT,
 volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE RESTRICT,
 service_1_available INTEGER NOT NULL CHECK(service_1_available IN (0,1)),
 service_2_available INTEGER NOT NULL CHECK(service_2_available IN (0,1)), revision INTEGER NOT NULL CHECK(revision>0),
 source TEXT NOT NULL CHECK(source IN ('volunteer','admin')), actor_id TEXT REFERENCES admin_users(id) ON DELETE RESTRICT,
 confirmation_reason TEXT, updated_at REAL NOT NULL DEFAULT (unixepoch()),
 PRIMARY KEY(date_id,volunteer_id),
 CHECK((source='volunteer' AND actor_id IS NULL AND confirmation_reason IS NULL) OR
 (source='admin' AND actor_id IS NOT NULL AND length(trim(confirmation_reason)) BETWEEN 1 AND 1000 AND confirmation_reason IS NOT NULL))
);
CREATE INDEX availability_volunteer ON availability(volunteer_id,date_id);
CREATE TABLE schedule_versions (
 id TEXT PRIMARY KEY, date_id TEXT NOT NULL REFERENCES service_dates(id) ON DELETE RESTRICT,
 status TEXT NOT NULL CHECK(status IN ('draft','published')), publication_number INTEGER,
 draft_revision INTEGER NOT NULL DEFAULT 1 CHECK(draft_revision>0), base_version_id TEXT,
 config_snapshot TEXT NOT NULL, config_epoch INTEGER NOT NULL CHECK(config_epoch>0),
 creator TEXT NOT NULL REFERENCES admin_users(id) ON DELETE RESTRICT,
 published_actor TEXT REFERENCES admin_users(id) ON DELETE RESTRICT, published_at REAL,
 input_epochs TEXT, solver_metadata TEXT, published_payload TEXT, payload_hash TEXT,
 created_at REAL NOT NULL DEFAULT (unixepoch()), updated_at REAL NOT NULL DEFAULT (unixepoch()),
 UNIQUE(date_id,id), UNIQUE(date_id,publication_number),
 FOREIGN KEY(date_id,base_version_id) REFERENCES schedule_versions(date_id,id) ON DELETE RESTRICT,
 CHECK((status='draft' AND publication_number IS NULL AND published_actor IS NULL AND published_at IS NULL AND published_payload IS NULL AND payload_hash IS NULL) OR
 (status='published' AND publication_number IS NOT NULL AND publication_number>0 AND published_actor IS NOT NULL AND published_at IS NOT NULL AND published_payload IS NOT NULL AND payload_hash IS NOT NULL))
);
CREATE UNIQUE INDEX one_draft ON schedule_versions(date_id) WHERE status='draft';
CREATE INDEX version_creator ON schedule_versions(creator);
CREATE INDEX version_publisher ON schedule_versions(published_actor);
CREATE INDEX version_base ON schedule_versions(date_id,base_version_id);
CREATE TRIGGER published_version_update BEFORE UPDATE ON schedule_versions WHEN OLD.status='published'
BEGIN SELECT RAISE(ABORT,'published version is immutable'); END;
CREATE TRIGGER published_version_delete BEFORE DELETE ON schedule_versions WHEN OLD.status='published'
BEGIN SELECT RAISE(ABORT,'published version is immutable'); END;
CREATE TRIGGER no_direct_publication BEFORE INSERT ON schedule_versions WHEN NEW.status='published'
BEGIN SELECT RAISE(ABORT,'create draft before publication'); END;
CREATE TRIGGER published_pointer BEFORE UPDATE OF current_published_version_id ON service_dates
WHEN NEW.current_published_version_id IS NOT NULL AND NOT EXISTS
(SELECT 1 FROM schedule_versions WHERE date_id=NEW.id AND id=NEW.current_published_version_id AND status='published')
BEGIN SELECT RAISE(ABORT,'pointer requires same-date publication'); END;
CREATE TABLE slots (
 version_id TEXT NOT NULL, service_number INTEGER NOT NULL CHECK(service_number IN (1,2)),
 position_number INTEGER NOT NULL REFERENCES positions(number) ON DELETE RESTRICT,
 date_id TEXT NOT NULL, volunteer_id TEXT REFERENCES volunteers(id) ON DELETE RESTRICT,
 slot_revision INTEGER NOT NULL DEFAULT 1 CHECK(slot_revision>0), locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1)),
 source TEXT NOT NULL CHECK(source IN ('manual','solver')), zone TEXT NOT NULL CHECK(zone IN ('inside','outside')),
 essential INTEGER NOT NULL CHECK(essential IN (0,1)),
 PRIMARY KEY(version_id,service_number,position_number), UNIQUE(date_id,version_id,service_number,position_number),
 FOREIGN KEY(date_id,version_id) REFERENCES schedule_versions(date_id,id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX one_person_per_service ON slots(version_id,service_number,volunteer_id) WHERE volunteer_id IS NOT NULL;
CREATE INDEX slots_person ON slots(volunteer_id);
CREATE INDEX slots_position ON slots(position_number);
CREATE TABLE rotation_approvals (
 id TEXT PRIMARY KEY, version_id TEXT NOT NULL, date_id TEXT NOT NULL,
 volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE RESTRICT,
 first_service INTEGER NOT NULL DEFAULT 1 CHECK(first_service=1), first_position INTEGER NOT NULL,
 second_service INTEGER NOT NULL DEFAULT 2 CHECK(second_service=2), second_position INTEGER NOT NULL,
 first_revision INTEGER NOT NULL CHECK(first_revision>0), second_revision INTEGER NOT NULL CHECK(second_revision>0),
 first_fingerprint TEXT NOT NULL, second_fingerprint TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 1000),
 approving_user TEXT NOT NULL REFERENCES admin_users(id) ON DELETE RESTRICT, approved_at REAL NOT NULL, invalidated_at REAL,
 FOREIGN KEY(date_id,version_id,first_service,first_position) REFERENCES slots(date_id,version_id,service_number,position_number) ON DELETE RESTRICT,
 FOREIGN KEY(date_id,version_id,second_service,second_position) REFERENCES slots(date_id,version_id,service_number,position_number) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX active_rotation ON rotation_approvals(version_id,volunteer_id) WHERE invalidated_at IS NULL;
CREATE INDEX rotation_first ON rotation_approvals(date_id,version_id,first_service,first_position);
CREATE INDEX rotation_second ON rotation_approvals(date_id,version_id,second_service,second_position);
CREATE INDEX rotation_actor ON rotation_approvals(approving_user);
CREATE INDEX rotation_person ON rotation_approvals(volunteer_id);
CREATE TABLE vacancy_approvals (
 id TEXT PRIMARY KEY, version_id TEXT NOT NULL, date_id TEXT NOT NULL, service_number INTEGER NOT NULL, position_number INTEGER NOT NULL,
 slot_revision INTEGER NOT NULL CHECK(slot_revision>0), fingerprint TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 1000),
 approving_user TEXT NOT NULL REFERENCES admin_users(id) ON DELETE RESTRICT, approved_at REAL NOT NULL, invalidated_at REAL,
 FOREIGN KEY(date_id,version_id,service_number,position_number) REFERENCES slots(date_id,version_id,service_number,position_number) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX active_vacancy ON vacancy_approvals(version_id,service_number,position_number) WHERE invalidated_at IS NULL;
CREATE INDEX vacancy_slot ON vacancy_approvals(date_id,version_id,service_number,position_number);
CREATE INDEX vacancy_actor ON vacancy_approvals(approving_user);
CREATE TRIGGER slots_insert BEFORE INSERT ON slots WHEN (SELECT status FROM schedule_versions WHERE id=NEW.version_id)='published'
BEGIN SELECT RAISE(ABORT,'published children are immutable'); END;
CREATE TRIGGER slots_update BEFORE UPDATE ON slots WHEN (SELECT status FROM schedule_versions WHERE id=OLD.version_id)='published' OR (SELECT status FROM schedule_versions WHERE id=NEW.version_id)='published'
BEGIN SELECT RAISE(ABORT,'published children are immutable'); END;
CREATE TRIGGER slots_delete BEFORE DELETE ON slots WHEN (SELECT status FROM schedule_versions WHERE id=OLD.version_id)='published'
BEGIN SELECT RAISE(ABORT,'published children are immutable'); END;
CREATE TRIGGER rotation_insert BEFORE INSERT ON rotation_approvals WHEN (SELECT status FROM schedule_versions WHERE id=NEW.version_id)='published'
BEGIN SELECT RAISE(ABORT,'published children are immutable'); END;
CREATE TRIGGER rotation_update BEFORE UPDATE ON rotation_approvals WHEN (SELECT status FROM schedule_versions WHERE id=OLD.version_id)='published' OR (SELECT status FROM schedule_versions WHERE id=NEW.version_id)='published'
BEGIN SELECT RAISE(ABORT,'published children are immutable'); END;
CREATE TRIGGER rotation_delete BEFORE DELETE ON rotation_approvals WHEN (SELECT status FROM schedule_versions WHERE id=OLD.version_id)='published'
BEGIN SELECT RAISE(ABORT,'published children are immutable'); END;
CREATE TRIGGER vacancy_insert BEFORE INSERT ON vacancy_approvals WHEN (SELECT status FROM schedule_versions WHERE id=NEW.version_id)='published'
BEGIN SELECT RAISE(ABORT,'published children are immutable'); END;
CREATE TRIGGER vacancy_update BEFORE UPDATE ON vacancy_approvals WHEN (SELECT status FROM schedule_versions WHERE id=OLD.version_id)='published' OR (SELECT status FROM schedule_versions WHERE id=NEW.version_id)='published'
BEGIN SELECT RAISE(ABORT,'published children are immutable'); END;
CREATE TRIGGER vacancy_delete BEFORE DELETE ON vacancy_approvals WHEN (SELECT status FROM schedule_versions WHERE id=OLD.version_id)='published'
BEGIN SELECT RAISE(ABORT,'published children are immutable'); END;
CREATE TRIGGER rotation_no_revival BEFORE UPDATE ON rotation_approvals WHEN OLD.invalidated_at IS NOT NULL AND NEW.invalidated_at IS NOT OLD.invalidated_at
BEGIN SELECT RAISE(ABORT,'approval invalidation is permanent'); END;
CREATE TRIGGER vacancy_no_revival BEFORE UPDATE ON vacancy_approvals WHEN OLD.invalidated_at IS NOT NULL AND NEW.invalidated_at IS NOT OLD.invalidated_at
BEGIN SELECT RAISE(ABORT,'approval invalidation is permanent'); END;
CREATE TABLE staff_sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES admin_users(id) ON DELETE RESTRICT,
 kind TEXT NOT NULL CHECK(kind IN ('pending','staff')), credential_epoch INTEGER NOT NULL,
 created_at REAL NOT NULL, last_used_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL, csrf_hash TEXT NOT NULL
);
CREATE INDEX staff_session_user ON staff_sessions(user_id);
CREATE INDEX staff_session_expiry ON staff_sessions(expires_at);
CREATE TABLE volunteer_sessions (
 token_hash TEXT PRIMARY KEY, volunteer_id TEXT NOT NULL REFERENCES volunteers(id) ON DELETE RESTRICT,
 date_id TEXT NOT NULL REFERENCES service_dates(id) ON DELETE RESTRICT, link_id TEXT NOT NULL, link_generation INTEGER NOT NULL,
 credential_epoch INTEGER NOT NULL, created_at REAL NOT NULL, last_used_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL, csrf_hash TEXT NOT NULL,
 FOREIGN KEY(date_id,link_id) REFERENCES date_links(date_id,id) ON DELETE RESTRICT
);
CREATE INDEX volunteer_session_person ON volunteer_sessions(volunteer_id);
CREATE INDEX volunteer_session_link ON volunteer_sessions(date_id,link_id);
CREATE INDEX volunteer_session_expiry ON volunteer_sessions(expires_at);
CREATE TABLE auth_throttles (
 scope_hash TEXT PRIMARY KEY, window_start REAL NOT NULL, count INTEGER NOT NULL CHECK(count>=0), blocked_until REAL NOT NULL DEFAULT 0, expires_at REAL NOT NULL
);
CREATE INDEX throttle_expiry ON auth_throttles(expires_at);
CREATE TABLE idempotency (
 actor_id TEXT NOT NULL REFERENCES admin_users(id) ON DELETE RESTRICT,
 date_id TEXT NOT NULL REFERENCES service_dates(id) ON DELETE RESTRICT, operation TEXT NOT NULL, key TEXT NOT NULL,
 request_digest TEXT NOT NULL, version_id TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(actor_id,date_id,operation,key), FOREIGN KEY(date_id,version_id) REFERENCES schedule_versions(date_id,id) ON DELETE RESTRICT
);
CREATE INDEX idempotency_version ON idempotency(date_id,version_id);
CREATE TABLE audit_events (
 id TEXT PRIMARY KEY, occurred_at REAL NOT NULL, action TEXT NOT NULL, object_id TEXT NOT NULL,
 actor_type TEXT NOT NULL CHECK(actor_type IN ('system','staff','volunteer')),
 staff_id TEXT REFERENCES admin_users(id) ON DELETE RESTRICT, volunteer_id TEXT REFERENCES volunteers(id) ON DELETE RESTRICT,
 details TEXT NOT NULL,
 CHECK((actor_type='system' AND staff_id IS NULL AND volunteer_id IS NULL) OR
 (actor_type='staff' AND staff_id IS NOT NULL AND volunteer_id IS NULL) OR
 (actor_type='volunteer' AND staff_id IS NULL AND volunteer_id IS NOT NULL))
);
CREATE INDEX audit_object ON audit_events(object_id,occurred_at);
CREATE INDEX audit_staff ON audit_events(staff_id);
CREATE INDEX audit_volunteer ON audit_events(volunteer_id);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
