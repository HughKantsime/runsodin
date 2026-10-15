-- models_library/migrations/001_initial.sql
-- Canonical source for models_library domain raw-SQL tables.
-- Note: models (the model catalog) is managed by SQLAlchemy ORM. See modules/models_library/models.py.
-- model_revisions tracks version history for each model file.
-- Depends on: models_library ORM (models table), core (users table).

CREATE TABLE IF NOT EXISTS model_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id INTEGER NOT NULL REFERENCES models(id),
    revision_number INTEGER NOT NULL DEFAULT 1,
    file_path TEXT,
    changelog TEXT,
    uploaded_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(model_id, revision_number)
);
