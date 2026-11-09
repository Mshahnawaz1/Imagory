"""Opening connections with the library's settings, transactions, and the UTC clock."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection with the library's standard settings.

    The connection is in autocommit mode; group writes with :func:`transaction`.
    """
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def utc_now() -> str:
    """Current UTC time as ISO 8601 text ending in ``Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
