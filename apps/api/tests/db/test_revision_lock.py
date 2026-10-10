"""Database-free tests of app/db/revision_lock.py's connection lifecycle.

A recording fake engine/connection stands in for PostgreSQL. These prove the
client-side contract the lock's guarantee depends on: the lock is taken and
released on one dedicated connection that stays checked out for the whole
protected block, the connection is committed (idle, not idle-in-transaction)
while the block runs, the unlock happens only after the block finished, and
any path that may leave the lock held discards the connection instead of
returning it to the pool. Server-side semantics (a session-level advisory
lock survives commits and is released when its session ends) are
PostgreSQL's documented behavior and are exercised against a live server by
tests/pipeline/test_pipeline_db.py::test_concurrent_analysis_of_one_revision_is_rejected.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.db import revision_lock
from app.db.revision_lock import (
    RevisionBusyError,
    lock_key,
    pg_locks_ids,
    revision_rebuild_lock,
)


@dataclass
class _Result:
    value: Any

    def scalar_one(self) -> Any:
        return self.value


@dataclass
class _Connection:
    log: list[str]
    acquire: bool = True
    fail_unlock: bool = False

    def execute(self, statement: Any, params: Any = None) -> _Result:
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            self.log.append("try_lock")
            return _Result(self.acquire)
        if "pg_advisory_unlock" in sql:
            self.log.append("unlock")
            if self.fail_unlock:
                raise RuntimeError("connection lost")
            return _Result(True)
        raise AssertionError(f"unexpected statement {sql}")

    def commit(self) -> None:
        self.log.append("commit")

    def invalidate(self) -> None:
        self.log.append("invalidate")

    def close(self) -> None:
        self.log.append("close")


@dataclass
class _Session:
    connection: _Connection
    connects: int = 0
    log: list[str] = field(default_factory=list)

    def get_bind(self) -> "_Session":
        return self

    @property
    def engine(self) -> "_Session":
        return self

    def connect(self) -> _Connection:
        self.connects += 1
        return self.connection


def _session(**kwargs: Any) -> _Session:
    log: list[str] = []
    session = _Session(connection=_Connection(log=log, **kwargs), log=log)
    return session


def test_lock_spans_the_whole_block_on_one_connection() -> None:
    db = _session()
    with revision_rebuild_lock(db, uuid.uuid4()):  # type: ignore[arg-type]
        db.log.append("work")
    assert db.connects == 1
    # Acquire, commit so the connection is idle (not idle in transaction),
    # run the protected work, then unlock and return the connection.
    assert db.log == ["try_lock", "commit", "work", "unlock", "commit", "close"]


def test_busy_revision_raises_before_any_work_and_releases_nothing() -> None:
    db = _session(acquire=False)
    with pytest.raises(RevisionBusyError):
        with revision_rebuild_lock(db, uuid.uuid4()):  # type: ignore[arg-type]
            db.log.append("work")
    assert db.log == ["try_lock", "commit", "close"]


def test_lock_is_released_after_the_block_fails() -> None:
    db = _session()
    with pytest.raises(ValueError):
        with revision_rebuild_lock(db, uuid.uuid4()):  # type: ignore[arg-type]
            db.log.append("work")
            raise ValueError("stage blew up")
    assert db.log == ["try_lock", "commit", "work", "unlock", "commit", "close"]


def test_failed_unlock_discards_the_connection_instead_of_pooling_it() -> None:
    db = _session(fail_unlock=True)
    with pytest.raises(RuntimeError):
        with revision_rebuild_lock(db, uuid.uuid4()):  # type: ignore[arg-type]
            db.log.append("work")
    # Closing an invalidated connection ends the server session, which
    # releases every session-level advisory lock it held.
    assert db.log[-2:] == ["invalidate", "close"]


def test_lock_keys_are_stable_signed_bigints_and_match_pg_locks_split() -> None:
    revision = uuid.UUID("0123456789abcdef0123456789abcdef")
    key = lock_key(revision)
    assert key == lock_key(revision)
    assert -(1 << 63) <= key < (1 << 63)
    classid, objid = pg_locks_ids(key)
    assert 0 <= classid < (1 << 32) and 0 <= objid < (1 << 32)
    assert ((classid << 32) | objid) == key & 0xFFFF_FFFF_FFFF_FFFF
    assert lock_key(uuid.uuid4()) != lock_key(uuid.uuid4())


def test_status_check_reads_pg_locks_and_never_takes_the_lock() -> None:
    seen: list[tuple[str, dict[str, int]]] = []

    class _ReadOnly:
        def execute(self, statement: Any, params: dict[str, int]) -> _Result:
            seen.append((str(statement), params))
            return _Result(False)

    revision = uuid.uuid4()
    assert revision_lock.rebuild_in_progress(_ReadOnly(), revision) is False  # type: ignore[arg-type]
    sql, params = seen[0]
    assert "pg_locks" in sql and "pg_try_advisory_lock" not in sql
    assert (params["classid"], params["objid"]) == pg_locks_ids(lock_key(revision))
