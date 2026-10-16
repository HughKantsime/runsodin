-- Durable cross-process WebSocket event queue. This table was historically
-- created from the FastAPI lifespan, which allowed an API process to mutate
-- schema after the entrypoint declared startup complete.

CREATE TABLE IF NOT EXISTS ws_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ws_events_created ON ws_events(created_at);
