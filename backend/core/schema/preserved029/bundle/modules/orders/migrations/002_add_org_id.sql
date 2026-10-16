-- Add org_id to orders and products for multi-tenant scoping.
-- Existing rows get NULL (visible to all orgs).
ALTER TABLE orders ADD COLUMN org_id INTEGER;
ALTER TABLE products ADD COLUMN org_id INTEGER;
