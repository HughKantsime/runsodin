"""
O.D.I.N. — Core database layer.

Provides the SQLAlchemy engine, session factory, declarative base,
and the FastAPI get_db dependency.

Extracted from deps.py as part of the modular architecture refactor.
Old import path (from deps import get_db) continues to work via re-exports in deps.py.
"""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.config import settings
from models import Base  # Single Base instance shared across all models

engine = create_engine(
    settings.database_url,
    echo=settings.debug,
    poolclass=NullPool,
    connect_args={"check_same_thread": False},
)
with engine.connect() as conn:
    conn.execute(text("PRAGMA journal_mode=WAL"))
    conn.execute(text("PRAGMA busy_timeout=5000"))

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Dependency for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
