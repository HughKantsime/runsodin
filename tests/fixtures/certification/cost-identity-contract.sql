-- Reviewable SQLite contract prototype only. Not wired into bootstrap/migrations.
-- No backfill or live application invocation.
CREATE TABLE cost_identities (
 identity_uid TEXT PRIMARY KEY NOT NULL,
 resource_kind TEXT NOT NULL CHECK(resource_kind IN ('order','item','job','spool','consumable','attempt','schedule')),
 resource_id INTEGER NOT NULL,
 owner_scope TEXT NOT NULL CHECK(owner_scope IN ('global','org')),
 owner_key INTEGER NOT NULL,
 CHECK((owner_scope='global' AND owner_key=0) OR (owner_scope='org' AND owner_key>0)),
 UNIQUE(identity_uid,resource_kind,owner_scope,owner_key)
);
-- Live resource columns are nullable unique UUIDs, filled only by a locked
-- allocation transaction. The registry retains history, with no source FKs.
CREATE TABLE cost_order_memberships (
 child_uid TEXT NOT NULL,
 child_kind TEXT NOT NULL CHECK(child_kind IN ('item','schedule','job')),
 order_uid TEXT NOT NULL,
 order_kind TEXT NOT NULL DEFAULT 'order' CHECK(order_kind='order'),
 owner_scope TEXT NOT NULL,
 owner_key INTEGER NOT NULL,
 PRIMARY KEY(child_uid,child_kind),
 UNIQUE(child_uid,child_kind,order_uid,owner_scope,owner_key),
 FOREIGN KEY(child_uid,child_kind,owner_scope,owner_key) REFERENCES cost_identities(identity_uid,resource_kind,owner_scope,owner_key) ON DELETE RESTRICT,
 FOREIGN KEY(order_uid,order_kind,owner_scope,owner_key) REFERENCES cost_identities(identity_uid,resource_kind,owner_scope,owner_key) ON DELETE RESTRICT
);
CREATE TABLE cost_job_bindings (
 job_uid TEXT PRIMARY KEY NOT NULL,
 job_kind TEXT NOT NULL DEFAULT 'job' CHECK(job_kind='job'),
 order_uid TEXT NOT NULL,
 item_uid TEXT NOT NULL,
 item_kind TEXT NOT NULL DEFAULT 'item' CHECK(item_kind='item'),
 owner_scope TEXT NOT NULL,
 owner_key INTEGER NOT NULL,
 UNIQUE(job_uid,order_uid,item_uid,owner_scope,owner_key),
 FOREIGN KEY(job_uid,job_kind,order_uid,owner_scope,owner_key) REFERENCES cost_order_memberships(child_uid,child_kind,order_uid,owner_scope,owner_key) ON DELETE RESTRICT,
 FOREIGN KEY(item_uid,item_kind,order_uid,owner_scope,owner_key) REFERENCES cost_order_memberships(child_uid,child_kind,order_uid,owner_scope,owner_key) ON DELETE RESTRICT
);
CREATE TABLE cost_attempt_bindings (
 attempt_uid TEXT PRIMARY KEY NOT NULL,
 attempt_kind TEXT NOT NULL DEFAULT 'attempt' CHECK(attempt_kind='attempt'),
 job_uid TEXT NOT NULL,
 order_uid TEXT NOT NULL,
 item_uid TEXT NOT NULL,
 owner_scope TEXT NOT NULL,
 owner_key INTEGER NOT NULL,
 UNIQUE(attempt_uid,owner_scope,owner_key),
 FOREIGN KEY(attempt_uid,attempt_kind,owner_scope,owner_key) REFERENCES cost_identities(identity_uid,resource_kind,owner_scope,owner_key) ON DELETE RESTRICT,
 FOREIGN KEY(job_uid,order_uid,item_uid,owner_scope,owner_key) REFERENCES cost_job_bindings(job_uid,order_uid,item_uid,owner_scope,owner_key) ON DELETE RESTRICT
);
CREATE TABLE cost_obligations (
 obligation_uid TEXT PRIMARY KEY NOT NULL,
 order_uid TEXT NOT NULL,
 order_kind TEXT NOT NULL DEFAULT 'order' CHECK(order_kind='order'),
 item_uid TEXT NOT NULL,
 item_kind TEXT NOT NULL DEFAULT 'item' CHECK(item_kind='item'),
 owner_scope TEXT NOT NULL,
 owner_key INTEGER NOT NULL,
 source_uid TEXT NOT NULL,
 source_kind TEXT NOT NULL DEFAULT 'schedule' CHECK(source_kind='schedule'),
 allocation_key TEXT NOT NULL,
 obligation_kind TEXT NOT NULL CHECK(obligation_kind IN ('printed_pieces','purchased_units','explicit_no_production')),
 required_quantity TEXT NOT NULL,
 evidence_state TEXT NOT NULL CHECK(evidence_state IN ('recorded','unresolved')),
 evidence_reason TEXT,
 CHECK(length(required_quantity) BETWEEN 1 AND 32 AND required_quantity NOT GLOB '*[^0-9.]*' AND (
  (obligation_kind='explicit_no_production' AND required_quantity='0') OR
  (obligation_kind='printed_pieces' AND length(required_quantity)<=18 AND required_quantity NOT GLOB '*.*' AND substr(required_quantity,1,1) BETWEEN '1' AND '9') OR
  (obligation_kind='purchased_units' AND required_quantity!='0' AND (
   (instr(required_quantity,'.')=0 AND length(required_quantity)<=18 AND substr(required_quantity,1,1) BETWEEN '1' AND '9') OR
   (instr(required_quantity,'.') BETWEEN 2 AND 19 AND substr(required_quantity,instr(required_quantity,'.')+1) NOT GLOB '*.*' AND length(substr(required_quantity,instr(required_quantity,'.')+1)) BETWEEN 1 AND 12 AND substr(required_quantity,-1) BETWEEN '1' AND '9' AND (substr(required_quantity,1,1) BETWEEN '1' AND '9' OR substr(required_quantity,1,2)='0.'))
  ))
 )),
 CHECK((evidence_state='recorded' AND evidence_reason IS NULL) OR (evidence_state='unresolved' AND evidence_reason IS NOT NULL AND length(trim(evidence_reason)) BETWEEN 1 AND 200)),
 FOREIGN KEY(item_uid,item_kind,order_uid,owner_scope,owner_key) REFERENCES cost_order_memberships(child_uid,child_kind,order_uid,owner_scope,owner_key) ON DELETE RESTRICT,
 FOREIGN KEY(source_uid,source_kind,order_uid,owner_scope,owner_key) REFERENCES cost_order_memberships(child_uid,child_kind,order_uid,owner_scope,owner_key) ON DELETE RESTRICT,
 UNIQUE(source_uid,allocation_key),
 UNIQUE(obligation_uid,owner_scope,owner_key),
 FOREIGN KEY(order_uid,order_kind,owner_scope,owner_key) REFERENCES cost_identities(identity_uid,resource_kind,owner_scope,owner_key) ON DELETE RESTRICT,
 FOREIGN KEY(item_uid,item_kind,owner_scope,owner_key) REFERENCES cost_identities(identity_uid,resource_kind,owner_scope,owner_key) ON DELETE RESTRICT,
 FOREIGN KEY(source_uid,source_kind,owner_scope,owner_key) REFERENCES cost_identities(identity_uid,resource_kind,owner_scope,owner_key) ON DELETE RESTRICT
);
CREATE TABLE cost_attempt_participation (
 attempt_uid TEXT NOT NULL,
 attempt_kind TEXT NOT NULL DEFAULT 'attempt' CHECK(attempt_kind='attempt'),
 obligation_uid TEXT NOT NULL,
 owner_scope TEXT NOT NULL,
 owner_key INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('started','observed','unresolved')),
 reason TEXT,
 CHECK((state IN ('started','observed') AND reason IS NULL) OR (state='unresolved' AND reason IS NOT NULL AND length(trim(reason)) BETWEEN 1 AND 200)),
 PRIMARY KEY(attempt_uid,obligation_uid),
 FOREIGN KEY(attempt_uid,owner_scope,owner_key) REFERENCES cost_attempt_bindings(attempt_uid,owner_scope,owner_key) ON DELETE RESTRICT,
 FOREIGN KEY(obligation_uid,owner_scope,owner_key) REFERENCES cost_obligations(obligation_uid,owner_scope,owner_key) ON DELETE RESTRICT,
 FOREIGN KEY(attempt_uid,attempt_kind,owner_scope,owner_key) REFERENCES cost_identities(identity_uid,resource_kind,owner_scope,owner_key) ON DELETE RESTRICT
);
CREATE TABLE finance_currency_settings (
 owner_scope TEXT NOT NULL CHECK(owner_scope IN ('global','org')),
 owner_key INTEGER NOT NULL,
 currency TEXT CHECK(currency IS NULL OR currency IN ('USD','EUR','GBP','CAD','AUD','JPY','CHF','NZD')),
 CHECK((owner_scope='global' AND owner_key=0) OR (owner_scope='org' AND owner_key>0)),
 PRIMARY KEY(owner_scope,owner_key)
);
-- Observation anchors are append-only. Corrections use separate revision rows.
CREATE TRIGGER cost_identities_no_update BEFORE UPDATE ON cost_identities
BEGIN SELECT RAISE(ABORT,'economic identity is immutable'); END;
CREATE TRIGGER cost_identities_no_delete BEFORE DELETE ON cost_identities
BEGIN SELECT RAISE(ABORT,'economic identity history is retained'); END;
CREATE TRIGGER cost_obligations_no_update BEFORE UPDATE ON cost_obligations
BEGIN SELECT RAISE(ABORT,'cost obligation is immutable'); END;
CREATE TRIGGER cost_obligations_no_delete BEFORE DELETE ON cost_obligations
BEGIN SELECT RAISE(ABORT,'cost obligation history is retained'); END;

CREATE TRIGGER cost_memberships_no_update BEFORE UPDATE ON cost_order_memberships
BEGIN SELECT RAISE(ABORT,'order membership is immutable'); END;
CREATE TRIGGER cost_memberships_no_delete BEFORE DELETE ON cost_order_memberships
BEGIN SELECT RAISE(ABORT,'order membership history is retained'); END;

CREATE TRIGGER cost_participation_graph BEFORE INSERT ON cost_attempt_participation
WHEN NOT EXISTS(SELECT 1 FROM cost_attempt_bindings a JOIN cost_obligations o ON o.obligation_uid=NEW.obligation_uid WHERE a.attempt_uid=NEW.attempt_uid AND a.order_uid=o.order_uid AND a.item_uid=o.item_uid AND a.owner_scope=NEW.owner_scope AND a.owner_key=NEW.owner_key)
BEGIN SELECT RAISE(ABORT,'attempt does not belong to obligation order/item'); END;
CREATE TRIGGER cost_participation_anchors BEFORE UPDATE ON cost_attempt_participation
WHEN NEW.attempt_uid!=OLD.attempt_uid OR NEW.attempt_kind!=OLD.attempt_kind OR NEW.obligation_uid!=OLD.obligation_uid OR NEW.owner_scope!=OLD.owner_scope OR NEW.owner_key!=OLD.owner_key
BEGIN SELECT RAISE(ABORT,'participation anchors are immutable'); END;
CREATE TRIGGER cost_participation_no_delete BEFORE DELETE ON cost_attempt_participation
BEGIN SELECT RAISE(ABORT,'participation history is retained'); END;
CREATE TRIGGER cost_participation_progression BEFORE UPDATE ON cost_attempt_participation
WHEN (OLD.state='observed' AND NEW.state!='observed') OR (OLD.state='unresolved' AND NEW.state='started')
BEGIN SELECT RAISE(ABORT,'participation state cannot regress'); END;
CREATE TRIGGER cost_job_bindings_no_update BEFORE UPDATE ON cost_job_bindings
BEGIN SELECT RAISE(ABORT,'job binding is immutable'); END;
CREATE TRIGGER cost_job_bindings_no_delete BEFORE DELETE ON cost_job_bindings
BEGIN SELECT RAISE(ABORT,'job binding is retained'); END;
CREATE TRIGGER cost_attempt_bindings_no_update BEFORE UPDATE ON cost_attempt_bindings
BEGIN SELECT RAISE(ABORT,'attempt binding is immutable'); END;
CREATE TRIGGER cost_attempt_bindings_no_delete BEFORE DELETE ON cost_attempt_bindings
BEGIN SELECT RAISE(ABORT,'attempt binding is retained'); END;

CREATE TRIGGER cost_identities_no_replace BEFORE INSERT ON cost_identities
WHEN EXISTS(SELECT 1 FROM cost_identities WHERE identity_uid=NEW.identity_uid)
BEGIN SELECT RAISE(ABORT,'retained cost evidence cannot be replaced'); END;

CREATE TRIGGER cost_obligations_no_replace BEFORE INSERT ON cost_obligations
WHEN EXISTS(SELECT 1 FROM cost_obligations WHERE obligation_uid=NEW.obligation_uid OR (source_uid=NEW.source_uid AND allocation_key=NEW.allocation_key))
BEGIN SELECT RAISE(ABORT,'retained cost evidence cannot be replaced'); END;

CREATE TRIGGER cost_order_memberships_no_replace BEFORE INSERT ON cost_order_memberships
WHEN EXISTS(SELECT 1 FROM cost_order_memberships WHERE child_uid=NEW.child_uid AND child_kind=NEW.child_kind)
BEGIN SELECT RAISE(ABORT,'retained cost evidence cannot be replaced'); END;

CREATE TRIGGER cost_job_bindings_no_replace BEFORE INSERT ON cost_job_bindings
WHEN EXISTS(SELECT 1 FROM cost_job_bindings WHERE job_uid=NEW.job_uid)
BEGIN SELECT RAISE(ABORT,'retained cost evidence cannot be replaced'); END;

CREATE TRIGGER cost_attempt_bindings_no_replace BEFORE INSERT ON cost_attempt_bindings
WHEN EXISTS(SELECT 1 FROM cost_attempt_bindings WHERE attempt_uid=NEW.attempt_uid)
BEGIN SELECT RAISE(ABORT,'retained cost evidence cannot be replaced'); END;

CREATE TRIGGER cost_attempt_participation_no_replace BEFORE INSERT ON cost_attempt_participation
WHEN EXISTS(SELECT 1 FROM cost_attempt_participation WHERE attempt_uid=NEW.attempt_uid AND obligation_uid=NEW.obligation_uid)
BEGIN SELECT RAISE(ABORT,'retained cost evidence cannot be replaced'); END;
