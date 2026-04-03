-- inventory/migrations/001_initial.sql
-- Canonical source for inventory domain tables.
-- consumables, product_consumables, consumable_usage are dual-defined:
-- SQLAlchemy ORM is the canonical source. These tables are created by
-- Base.metadata.create_all. The raw SQL in entrypoint.sh is removed.
-- See modules/inventory/models.py for the authoritative schema.

-- Table managed by SQLAlchemy ORM in modules/inventory/models.py
-- consumables

-- Table managed by SQLAlchemy ORM in modules/inventory/models.py
-- product_consumables

-- Table managed by SQLAlchemy ORM in modules/inventory/models.py
-- consumable_usage
