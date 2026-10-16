"""Archive schema comparison ignores column order only (v1.9.24 PostgreSQL upgrades).

A database upgraded in place gets ALTER TABLE ... ADD COLUMN columns at the end of the
table, while a fresh create_all declares them in model order. Restores import data with
pg_dump COPY column lists, so column position is not restore-relevant. Every other part
of a table definition stays an exact lexical match.
"""
import pytest

from modules.system.backup_service import BackupValidationError
from modules.system.postgres_canonical_executables import validate_schema_sql

FRESH = """CREATE TABLE public.jobs (
    id integer NOT NULL,
    name character varying(100) DEFAULT 'a,b'::character varying,
    nozzle_1_type character varying(20),
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT jobs_qty_check CHECK ((qty > 0))
);
ALTER TABLE ONLY public.jobs ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);
CREATE INDEX ix_jobs_name ON public.jobs USING btree (name);
"""
UPGRADED = """CREATE TABLE public.jobs (
    id integer NOT NULL,
    name character varying(100) DEFAULT 'a,b'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT jobs_qty_check CHECK ((qty > 0)),
    nozzle_1_type character varying(20)
);
CREATE INDEX ix_jobs_name ON public.jobs USING btree (name);
ALTER TABLE ONLY public.jobs ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);
"""


def test_column_order_alone_is_accepted():
    validate_schema_sql(UPGRADED, FRESH)
    validate_schema_sql(FRESH, UPGRADED)


@pytest.mark.parametrize("change", [
    ("nozzle_1_type character varying(20)", "nozzle_1_type character varying(40)"),
    ("nozzle_1_type character varying(20)", "nozzle_1_type character varying(20) NOT NULL"),
    ("DEFAULT now()", "DEFAULT CURRENT_TIMESTAMP"),
    ("DEFAULT 'a,b'::character varying", "DEFAULT 'a, b'::character varying"),
    ("CHECK ((qty > 0))", "CHECK ((qty >= 0))"),
    ("    nozzle_1_type character varying(20)\n", "    nozzle_1_type character varying(20),\n    extra integer\n"),
    (",\n    nozzle_1_type character varying(20)\n", "\n"),
    ("    nozzle_1_type character varying(20)\n", "    nozzle_1_type character varying(20),\n    nozzle_1_type character varying(20)\n"),
    ("USING btree (name)", "USING btree (name, id)"),
    ("CREATE TABLE public.jobs (", "CREATE UNLOGGED TABLE public.jobs ("),
])
def test_every_other_table_difference_is_still_refused(change):
    old, new = change
    assert old in UPGRADED
    with pytest.raises(BackupValidationError):
        validate_schema_sql(UPGRADED.replace(old, new, 1), FRESH)
