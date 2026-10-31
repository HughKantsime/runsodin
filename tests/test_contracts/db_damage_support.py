"""Damage a database the way production's was on 2026-10-30, for the database safety tests."""
import sqlite3
from pathlib import Path


def _page(raw: bytes, number: int, size: int) -> bytes:
    return raw[(number - 1) * size: number * size]


def _rightmost_leaf(connection: sqlite3.Connection, raw: bytes, table: str, size: int) -> int:
    root = connection.execute("SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    assert _page(raw, root, size)[0] == 0x05, "the table needs an interior root page"
    return int.from_bytes(_page(raw, root, size)[8:12], "big")


def rewind_rightmost_leaf(database: Path, table: str, change: str) -> None:
    """Put back an older image of a live leaf after ``change``, which must keep rows in place.

    It's what a checkpoint that skips part of a transaction leaves behind: every row is readable
    and in order, but the page is out of date. Only an index that saw the change can tell.
    """
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = connection.execute("PRAGMA page_size").fetchone()[0]
    raw = database.read_bytes()
    leaf = _rightmost_leaf(connection, raw, table, size)
    image = _page(raw, leaf, size)
    connection.execute(change)
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    with database.open("r+b") as handle:
        handle.seek((leaf - 1) * size)
        handle.write(image)


def overwrite_rightmost_leaf(database: Path, victim: str, source: str) -> None:
    """Overwrite ``victim``'s rightmost leaf with an image of ``source``'s first leaf.

    The file stays a valid-looking database and ordinary reads and writes keep working, exactly
    what let the incident run for 21 hours. ``victim`` needs enough rows for an interior root.
    """
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = connection.execute("PRAGMA page_size").fetchone()[0]
    source_page = connection.execute("SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?", (source,)).fetchone()[0]
    raw = database.read_bytes()
    child = _rightmost_leaf(connection, raw, victim, size)
    connection.close()
    while _page(raw, source_page, size)[0] == 0x05:
        source_page = int.from_bytes(_page(raw, source_page, size)[8:12], "big")
    image = _page(raw, source_page, size)
    assert image[0] == 0x0D, "the source needs a table leaf page"
    with database.open("r+b") as handle:
        handle.seek((child - 1) * size)
        handle.write(image)
