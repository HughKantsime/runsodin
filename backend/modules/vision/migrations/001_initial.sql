-- vision/migrations/001_initial.sql
-- Canonical source for vision domain tables.
-- vision_detections, vision_settings, vision_models are dual-defined:
-- SQLAlchemy ORM is the canonical source. These tables are created by
-- Base.metadata.create_all. The raw SQL in entrypoint.sh is removed.
-- See modules/vision/models.py for the authoritative schema.

-- Table managed by SQLAlchemy ORM in modules/vision/models.py
-- vision_detections

-- Table managed by SQLAlchemy ORM in modules/vision/models.py
-- vision_settings

-- Table managed by SQLAlchemy ORM in modules/vision/models.py
-- vision_models
