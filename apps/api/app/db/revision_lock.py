"""Per-revision rebuild lock shared by every entry point that deletes and
rebuilds revision-scoped derived data (the analysis pipeline and Automation
Intelligence). See docs/architecture/analysis-pipeline.md section 2.

Why it is needed: parsing, graph construction and chunking each run
`DELETE ... WHERE revision_id = :r` followed by INSERTs. Symbol rows have no
natural-key uniqueness, so two concurrent rebuilds of one revision under
READ COMMITTED can both commit a full symbol set (the second DELETE blocks
on the first transaction's locked rows, then skips them, and never sees the
rows the first one inserted). Serializing rebuilds per revision prevents
that.

How it holds: a PostgreSQL *session-level* advisory lock on a dedicated
connection checked out for the whole protected block.

- Session-level, not transaction-level: the protected stage services run on
  the caller's ORM session and commit several times; a transaction-scoped
  lock on that session would be released by the first commit.
- Dedicated connection: the ORM session may return its connection to the
  pool between commits, so the lock cannot live there. The lock connection
  stays checked out until the block exits, so the server session that owns
  the lock lives exactly as long as the protected work.
- Committed right after acquisition: the lock connection then sits idle,
  not idle-in-transaction, so idle_in_transaction_session_timeout cannot
  terminate it (and silently release the lock) mid-rebuild.
- If the unlock fails, the connection is invalidated (closed, not returned
  to the pool): ending the server session releases every session-level lock
  it holds, so a lock can never leak into a pooled connection.

The guarantee is mutual exclusion between holders of this lock. It does not
block unrelated writers; every rebuild entry point must take it.
"""

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

logger = logging.getLogger("devlens.revision_lock")


class RevisionBusyError(Exception):
    """Another request currently holds this revision's rebuild lock."""


def lock_key(revision_id: uuid.UUID) -> int:
    """Signed 64-bit key for pg_*advisory_lock(bigint), from the revision id."""
    return (revision_id.int & 0xFFFF_FFFF_FFFF_FFFF) - (1 << 63)


def pg_locks_ids(key: int) -> tuple[int, int]:
    """(classid, objid) under which pg_locks reports a bigint advisory key:
    high-order half in classid, low-order half in objid (objsubid = 1)."""
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    return unsigned >> 32, unsigned & 0xFFFF_FFFF


@contextmanager
def revision_rebuild_lock(db: Session, revision_id: uuid.UUID) -> Iterator[None]:
    """Holds the revision's rebuild lock for the duration of the block, or
    raises RevisionBusyError immediately if another holder has it."""
    key = lock_key(revision_id)
    connection = db.get_bind().engine.connect()
    # True from the moment the server may hold the lock until it is known to
    # be released; if anything fails in that window the connection is
    # discarded (closing the server session releases the lock) rather than
    # returned to the pool.
    may_hold = True
    try:
        acquired = connection.execute(select(func.pg_try_advisory_lock(key))).scalar_one()
        connection.commit()
        if not acquired:
            may_hold = False
            raise RevisionBusyError(f"revision {revision_id} is already being rebuilt")
        try:
            yield
        finally:
            connection.execute(select(func.pg_advisory_unlock(key)))
            connection.commit()
            may_hold = False
    finally:
        if may_hold:
            logger.error(
                "rebuild lock for %s not cleanly released; discarding connection", revision_id
            )
            connection.invalidate()
        connection.close()


_HELD = text(
    "SELECT EXISTS ("
    " SELECT 1 FROM pg_locks"
    " WHERE locktype = 'advisory' AND granted AND objsubid = 1"
    " AND classid::bigint = :classid AND objid::bigint = :objid"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    ")"
)


def rebuild_in_progress(db: Session, revision_id: uuid.UUID) -> bool:
    """Read-only check via pg_locks. Never takes the lock itself, so a status
    read cannot make a concurrent rebuild request fail."""
    classid, objid = pg_locks_ids(lock_key(revision_id))
    return bool(db.execute(_HELD, {"classid": classid, "objid": objid}).scalar_one())
