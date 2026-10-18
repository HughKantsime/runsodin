"""After-commit dispatch work shares one connection; each piece keeps its own transaction."""
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import NullPool


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dispatch.db'}", poolclass=NullPool)  # as the product engine
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE events (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))
    connects = []
    event.listen(engine, "connect", lambda *_: connects.append(1))
    engine.connects = connects
    yield engine
    engine.dispose()


def _names(engine):
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(text("SELECT name FROM events ORDER BY id"))]


def test_pieces_share_one_connection_and_fail_independently(engine):
    from core.dispatch_connection import dispatch_scope, dispatch_transaction
    before = len(engine.connects)
    with dispatch_scope(engine):
        with dispatch_transaction(engine) as connection:
            connection.execute(text("INSERT INTO events (name) VALUES ('first')"))
        with pytest.raises(RuntimeError):
            with dispatch_transaction(engine) as connection:
                connection.execute(text("INSERT INTO events (name) VALUES ('failed piece')"))
                raise RuntimeError("handler failure")
        with dispatch_transaction(engine) as connection:
            connection.execute(text("INSERT INTO events (name) VALUES ('third')"))
    assert len(engine.connects) - before == 1
    assert _names(engine) == ["first", "third"]


def test_outside_a_scope_each_transaction_has_its_own_connection(engine):
    from core.dispatch_connection import dispatch_transaction
    before = len(engine.connects)
    for name in ("a", "b"):
        with dispatch_transaction(engine) as connection:
            connection.execute(text("INSERT INTO events (name) VALUES (:n)"), {"n": name})
    assert len(engine.connects) - before == 2 and _names(engine) == ["a", "b"]


def test_a_nested_scope_keeps_the_outer_connection_and_an_unused_scope_opens_none(engine):
    from core.dispatch_connection import dispatch_scope, dispatch_transaction
    before = len(engine.connects)
    with dispatch_scope(engine):
        pass
    assert len(engine.connects) == before
    with dispatch_scope(engine):
        with dispatch_scope(engine):
            with dispatch_transaction(engine) as connection:
                connection.execute(text("INSERT INTO events (name) VALUES ('inner')"))
        with dispatch_transaction(engine) as connection:
            connection.execute(text("INSERT INTO events (name) VALUES ('outer')"))
    assert len(engine.connects) - before == 1 and _names(engine) == ["inner", "outer"]
