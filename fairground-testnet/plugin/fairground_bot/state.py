from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Any, Iterator
import uuid

from .validation import normalize_address


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class StoreError(RuntimeError):
    pass


class CycleState(StrEnum):
    PREFLIGHT = "PREFLIGHT"
    FUNDS_CHECK = "FUNDS_CHECK"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    ENTRY_ARMED = "ENTRY_ARMED"
    ENTRY_TX_PENDING = "ENTRY_TX_PENDING"
    ENTRY_DISCOVERY = "ENTRY_DISCOVERY"
    ENTRY_PENDING = "ENTRY_PENDING"
    ENTRY_PARTIAL = "ENTRY_PARTIAL"
    ENTRY_CANCEL_PENDING = "ENTRY_CANCEL_PENDING"
    POSITION_VERIFY = "POSITION_VERIFY"
    HOLDING = "HOLDING"
    EXIT_ARMED = "EXIT_ARMED"
    EXIT_TX_PENDING = "EXIT_TX_PENDING"
    EXIT_DISCOVERY = "EXIT_DISCOVERY"
    EXIT_PENDING = "EXIT_PENDING"
    EXIT_PARTIAL = "EXIT_PARTIAL"
    EXIT_CANCEL_PENDING = "EXIT_CANCEL_PENDING"
    RESIDUAL_RECONCILE = "RESIDUAL_RECONCILE"
    FLAT_CONFIRM = "FLAT_CONFIRM"
    TX_UNKNOWN = "TX_UNKNOWN"
    PAUSED = "PAUSED"
    BLOCKED_FUNDS = "BLOCKED_FUNDS"
    DUST_BLOCKED = "DUST_BLOCKED"
    QUARANTINED = "QUARANTINED"
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"


TERMINAL_STATES = {CycleState.COMPLETE, CycleState.ABORTED}


# A cycle may be discarded without a signer only while the durable ledger can
# still prove that no entry transaction landed.  PAUSED and QUARANTINED are
# included because they can be reached before entry; their action/runtime
# evidence is checked separately below.  DUST_BLOCKED is deliberately absent:
# dust is evidence of post-entry exposure even when a remote read looks flat.
_PRE_ENTRY_RESOLUTION_STATES = {
    CycleState.PREFLIGHT,
    CycleState.FUNDS_CHECK,
    CycleState.APPROVAL_PENDING,
    CycleState.ENTRY_ARMED,
    CycleState.ENTRY_TX_PENDING,
    CycleState.BLOCKED_FUNDS,
    CycleState.PAUSED,
    CycleState.QUARANTINED,
}
_UNRESOLVED_ACTION_STATUSES = {"SIGNED", "BROADCAST", "UNKNOWN"}
_SAFE_APPROVAL_STATUSES = {
    "PREPARED",
    "NOT_BROADCAST",
    "REVERTED",
    "CONFIRMED",
}
_SAFE_ENTRY_STATUSES = {"PREPARED", "NOT_BROADCAST", "REVERTED"}
_POST_ENTRY_RUNTIME_KEYS = {
    "close_gas_action",
    "close_gas_balance_wei",
    "close_gas_limit",
    "close_gas_required_wei",
    "close_gas_shortfall_wei",
    "close_max_fee_per_gas_wei",
    "entry_deadline",
    "entry_filled",
    "entry_last_progress_at",
    "entry_order_id",
    "exit_deadline",
    "exit_filled",
    "exit_last_progress_at",
    "exit_oracle_price",
    "exit_oracle_timestamp",
    "exit_order_id",
    "exit_threshold_units",
    "hold_deadline",
    "position_id",
    "position_size",
    "post_entry_flat_observed_at",
    "post_entry_indexing_grace_seconds",
    "reduce_size_units",
}
_PRE_ENTRY_RESUME_STATES = {
    CycleState.PREFLIGHT,
    CycleState.FUNDS_CHECK,
    CycleState.APPROVAL_PENDING,
    CycleState.ENTRY_ARMED,
    CycleState.ENTRY_TX_PENDING,
    CycleState.BLOCKED_FUNDS,
}


ALLOWED_TRANSITIONS: dict[CycleState, set[CycleState]] = {
    CycleState.PREFLIGHT: {
        CycleState.FUNDS_CHECK,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
        CycleState.ABORTED,
    },
    CycleState.FUNDS_CHECK: {
        CycleState.APPROVAL_PENDING,
        CycleState.ENTRY_ARMED,
        CycleState.BLOCKED_FUNDS,
        CycleState.ABORTED,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.APPROVAL_PENDING: {
        CycleState.FUNDS_CHECK,
        CycleState.TX_UNKNOWN,
        CycleState.PAUSED,
        CycleState.ABORTED,
        CycleState.QUARANTINED,
    },
    CycleState.ENTRY_ARMED: {
        CycleState.ENTRY_TX_PENDING,
        CycleState.BLOCKED_FUNDS,
        CycleState.ABORTED,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.ENTRY_TX_PENDING: {
        CycleState.ENTRY_DISCOVERY,
        CycleState.TX_UNKNOWN,
        CycleState.PAUSED,
        CycleState.ABORTED,
        CycleState.QUARANTINED,
    },
    CycleState.ENTRY_DISCOVERY: {
        CycleState.ENTRY_PENDING,
        CycleState.ENTRY_PARTIAL,
        CycleState.POSITION_VERIFY,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.ENTRY_PENDING: {
        CycleState.ENTRY_PARTIAL,
        CycleState.ENTRY_CANCEL_PENDING,
        CycleState.POSITION_VERIFY,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.ENTRY_PARTIAL: {
        CycleState.ENTRY_PENDING,
        CycleState.ENTRY_CANCEL_PENDING,
        CycleState.POSITION_VERIFY,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.ENTRY_CANCEL_PENDING: {
        CycleState.POSITION_VERIFY,
        CycleState.TX_UNKNOWN,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.POSITION_VERIFY: {
        CycleState.HOLDING,
        CycleState.FLAT_CONFIRM,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.HOLDING: {
        CycleState.EXIT_ARMED,
        CycleState.FLAT_CONFIRM,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.EXIT_ARMED: {
        CycleState.EXIT_TX_PENDING,
        CycleState.EXIT_DISCOVERY,
        CycleState.RESIDUAL_RECONCILE,
        CycleState.FLAT_CONFIRM,
        CycleState.PAUSED,
        CycleState.DUST_BLOCKED,
        CycleState.QUARANTINED,
    },
    CycleState.EXIT_TX_PENDING: {
        CycleState.EXIT_DISCOVERY,
        CycleState.TX_UNKNOWN,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.EXIT_DISCOVERY: {
        CycleState.EXIT_PENDING,
        CycleState.EXIT_PARTIAL,
        CycleState.RESIDUAL_RECONCILE,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.EXIT_PENDING: {
        CycleState.EXIT_PARTIAL,
        CycleState.EXIT_CANCEL_PENDING,
        CycleState.RESIDUAL_RECONCILE,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.EXIT_PARTIAL: {
        CycleState.EXIT_PENDING,
        CycleState.EXIT_CANCEL_PENDING,
        CycleState.RESIDUAL_RECONCILE,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.EXIT_CANCEL_PENDING: {
        CycleState.RESIDUAL_RECONCILE,
        CycleState.TX_UNKNOWN,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.RESIDUAL_RECONCILE: {
        CycleState.EXIT_ARMED,
        CycleState.FLAT_CONFIRM,
        CycleState.DUST_BLOCKED,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.FLAT_CONFIRM: {
        CycleState.COMPLETE,
        CycleState.RESIDUAL_RECONCILE,
        CycleState.PAUSED,
        CycleState.QUARANTINED,
    },
    CycleState.TX_UNKNOWN: {
        CycleState.FUNDS_CHECK,
        CycleState.ENTRY_DISCOVERY,
        CycleState.POSITION_VERIFY,
        CycleState.EXIT_DISCOVERY,
        CycleState.RESIDUAL_RECONCILE,
        CycleState.PAUSED,
        CycleState.ABORTED,
        CycleState.QUARANTINED,
    },
    CycleState.PAUSED: {
        CycleState.PREFLIGHT,
        CycleState.ENTRY_PENDING,
        CycleState.ENTRY_PARTIAL,
        CycleState.POSITION_VERIFY,
        CycleState.HOLDING,
        CycleState.EXIT_ARMED,
        CycleState.EXIT_PENDING,
        CycleState.EXIT_PARTIAL,
        CycleState.RESIDUAL_RECONCILE,
        CycleState.QUARANTINED,
        CycleState.ABORTED,
    },
    CycleState.BLOCKED_FUNDS: {
        CycleState.FUNDS_CHECK,
        CycleState.ABORTED,
    },
    CycleState.DUST_BLOCKED: {
        CycleState.RESIDUAL_RECONCILE,
        CycleState.EXIT_ARMED,
        CycleState.FLAT_CONFIRM,
        CycleState.COMPLETE,
        CycleState.ABORTED,
        CycleState.QUARANTINED,
    },
    # QUARANTINED is no longer a black hole: after a confirmed write, resume may
    # re-enter discovery/verify. Pure local dead-ends still abort via
    # resolve_cycle_flat when the wallet is remotely flat.
    CycleState.QUARANTINED: {
        CycleState.ENTRY_DISCOVERY,
        CycleState.POSITION_VERIFY,
        CycleState.EXIT_DISCOVERY,
        CycleState.EXIT_ARMED,
        CycleState.RESIDUAL_RECONCILE,
        CycleState.HOLDING,
        CycleState.FLAT_CONFIRM,
        CycleState.ABORTED,
    },
    CycleState.COMPLETE: set(),
    CycleState.ABORTED: set(),
}


@dataclass(frozen=True, slots=True)
class CycleRecord:
    cycle_id: str
    account: str
    chain_id: int
    state: CycleState
    revision: int
    intent: dict[str, Any]
    runtime: dict[str, Any]
    created_at: str
    updated_at: str
    last_error: str | None


@dataclass(frozen=True, slots=True)
class ActionRecord:
    action_id: str
    cycle_id: str
    kind: str
    attempt: int
    nonce: int
    calldata_hash: str
    tx_hash: str | None
    status: str
    receipt_block: int | None
    created_at: str
    updated_at: str
    raw_transaction: bytes | None = field(repr=False, default=None)


@dataclass(frozen=True, slots=True)
class Lease:
    account: str
    owner: str
    fencing_token: int
    expires_at: float


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._validate_private_directory(parent)
        self._validate_private_artifact(self.path, create=True)
        self._initialize()

    @staticmethod
    def _validate_private_directory(path: Path) -> None:
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise StoreError(f"State directory is unavailable: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise StoreError("State directory must be a real local directory")
        if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            raise StoreError("State directory must be owned by the current user")

        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise StoreError("Cannot securely open state directory") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                raise StoreError("State directory must be a real local directory")
            if hasattr(os, "geteuid") and opened.st_uid != os.geteuid():
                raise StoreError("State directory must be owned by the current user")
            if (metadata.st_dev, metadata.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise StoreError("State directory changed while opening")
            os.fchmod(descriptor, 0o700)
        except OSError as exc:
            raise StoreError("Cannot make state directory private") from exc
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_private_artifact(
        path: Path,
        *,
        create: bool = False,
    ) -> tuple[int, int] | None:
        metadata: os.stat_result | None
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            if not create:
                return None
            metadata = None
        except OSError as exc:
            raise StoreError(f"Cannot inspect state file: {path}") from exc

        if metadata is not None:
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise StoreError("State database and sidecars must be regular files")
            if metadata.st_nlink != 1:
                raise StoreError("State database and sidecars must not be hard-linked")
            if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
                raise StoreError(
                    "State database and sidecars must be owned by the current user"
                )

        flags = os.O_RDWR
        if metadata is None:
            flags |= os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            action = "create" if metadata is None else "open"
            raise StoreError(f"Cannot securely {action} state file: {path}") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise StoreError("State database and sidecars must be regular files")
            if opened.st_nlink != 1:
                raise StoreError(
                    "State database and sidecars must not be hard-linked"
                )
            if hasattr(os, "geteuid") and opened.st_uid != os.geteuid():
                raise StoreError(
                    "State database and sidecars must be owned by the current user"
                )
            if metadata is not None and (
                metadata.st_dev,
                metadata.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise StoreError("State file changed while opening")
            os.fchmod(descriptor, 0o600)
            final = os.fstat(descriptor)
            if (
                not stat.S_ISREG(final.st_mode)
                or final.st_nlink != 1
                or (
                    hasattr(os, "geteuid")
                    and final.st_uid != os.geteuid()
                )
            ):
                raise StoreError("State file changed while securing it")
            return final.st_dev, final.st_ino
        except OSError as exc:
            raise StoreError(f"Cannot make state file private: {path}") from exc
        finally:
            os.close(descriptor)

    def _harden_artifacts(self) -> tuple[int, int]:
        self._validate_private_directory(self.path.parent)
        database_identity = self._validate_private_artifact(self.path)
        if database_identity is None:
            raise StoreError("State database disappeared")
        self._validate_private_artifact(Path(f"{self.path}-wal"))
        self._validate_private_artifact(Path(f"{self.path}-shm"))
        return database_identity

    def _connect(self) -> sqlite3.Connection:
        database_identity = self._harden_artifacts()
        try:
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            # Multi-account farm: many workers write leases/cycles concurrently.
            conn.execute("PRAGMA busy_timeout=30000")
            if self._harden_artifacts() != database_identity:
                raise StoreError("State database changed while connecting")
            return conn
        except Exception:
            if "conn" in locals():
                conn.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS cycles (
                    cycle_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    chain_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    intent_json TEXT NOT NULL,
                    runtime_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS cycles_account_updated
                    ON cycles(account, updated_at DESC);
                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL REFERENCES cycles(cycle_id),
                    kind TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    nonce INTEGER NOT NULL,
                    calldata_hash TEXT NOT NULL,
                    tx_hash TEXT,
                    raw_tx BLOB,
                    status TEXT NOT NULL,
                    receipt_block INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(cycle_id, kind, attempt),
                    UNIQUE(tx_hash)
                );
                CREATE TABLE IF NOT EXISTS leases (
                    account TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS controls (
                    account TEXT PRIMARY KEY,
                    kill_mode TEXT,
                    reason TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            action_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(actions)").fetchall()
            }
            if "raw_tx" not in action_columns:
                conn.execute("ALTER TABLE actions ADD COLUMN raw_tx BLOB")
        self._harden_artifacts()

    @staticmethod
    def _cycle(row: sqlite3.Row) -> CycleRecord:
        return CycleRecord(
            cycle_id=row["cycle_id"],
            account=row["account"],
            chain_id=row["chain_id"],
            state=CycleState(row["state"]),
            revision=row["revision"],
            intent=json.loads(row["intent_json"]),
            runtime=json.loads(row["runtime_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_error=row["last_error"],
        )

    @staticmethod
    def _action(row: sqlite3.Row) -> ActionRecord:
        return ActionRecord(
            action_id=row["action_id"],
            cycle_id=row["cycle_id"],
            kind=row["kind"],
            attempt=row["attempt"],
            nonce=row["nonce"],
            calldata_hash=row["calldata_hash"],
            tx_hash=row["tx_hash"],
            status=row["status"],
            receipt_block=row["receipt_block"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            raw_transaction=(bytes(row["raw_tx"]) if row["raw_tx"] is not None else None),
        )

    def create_cycle(
        self,
        *,
        account: str,
        chain_id: int,
        intent: dict[str, Any],
        cycle_id: str | None = None,
    ) -> CycleRecord:
        owner = normalize_address(account)
        identifier = cycle_id or str(uuid.uuid4())
        now = _utc_now()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT state FROM cycles WHERE account=? ORDER BY updated_at DESC",
                (owner,),
            ).fetchall()
            if any(CycleState(row["state"]) not in TERMINAL_STATES for row in rows):
                raise StoreError("This account already has an active cycle; resume or inspect it")
            conn.execute(
                """INSERT INTO cycles(
                    cycle_id, account, chain_id, state, revision,
                    intent_json, runtime_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, '{}', ?, ?)""",
                (
                    identifier,
                    owner,
                    int(chain_id),
                    CycleState.PREFLIGHT.value,
                    _json(intent),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM cycles WHERE cycle_id=?", (identifier,)
            ).fetchone()
        if row is None:  # pragma: no cover - SQLite invariant
            raise StoreError("Failed to create cycle")
        return self._cycle(row)

    def get_cycle(self, cycle_id: str) -> CycleRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cycles WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
        return self._cycle(row) if row is not None else None

    def get_active_cycle(self, account: str) -> CycleRecord | None:
        owner = normalize_address(account)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cycles WHERE account=? ORDER BY updated_at DESC", (owner,)
            ).fetchall()
        for row in rows:
            record = self._cycle(row)
            if record.state not in TERMINAL_STATES:
                return record
        return None

    def active_cycles(self) -> list[CycleRecord]:
        """Return every nonterminal cycle without a recency window."""

        terminal = tuple(state.value for state in TERMINAL_STATES)
        placeholders = ",".join("?" for _ in terminal)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM cycles
                    WHERE state NOT IN ({placeholders})
                    ORDER BY updated_at DESC""",
                terminal,
            ).fetchall()
        return [self._cycle(row) for row in rows]

    def latest_cycles(self, *, limit: int = 20) -> list[CycleRecord]:
        safe_limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cycles ORDER BY updated_at DESC LIMIT ?", (safe_limit,)
            ).fetchall()
        return [self._cycle(row) for row in rows]

    def transition(
        self,
        record: CycleRecord,
        new_state: CycleState,
        *,
        runtime_updates: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> CycleRecord:
        if new_state not in ALLOWED_TRANSITIONS[record.state]:
            raise StoreError(f"Illegal state transition: {record.state} -> {new_state}")
        runtime = dict(record.runtime)
        if runtime_updates:
            runtime.update(runtime_updates)
        now = _utc_now()
        with self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE cycles
                   SET state=?, revision=revision+1, runtime_json=?, updated_at=?, last_error=?
                   WHERE cycle_id=? AND state=? AND revision=?""",
                (
                    new_state.value,
                    _json(runtime),
                    now,
                    error,
                    record.cycle_id,
                    record.state.value,
                    record.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("Cycle state changed concurrently; refusing a stale transition")
            row = conn.execute(
                "SELECT * FROM cycles WHERE cycle_id=?", (record.cycle_id,)
            ).fetchone()
        return self._cycle(row)

    @staticmethod
    def _validate_action_attempt(attempt: int) -> int:
        try:
            value = int(attempt)
        except (TypeError, ValueError) as exc:
            raise StoreError("Action attempt must be a positive integer") from exc
        if value <= 0:
            raise StoreError("Action attempt must be a positive integer")
        return value

    @staticmethod
    def _assert_action_attempt_available(
        conn: sqlite3.Connection,
        *,
        cycle_id: str,
        kind: str,
        attempt: int,
    ) -> None:
        existing = conn.execute(
            """SELECT 1 FROM actions
               WHERE cycle_id=? AND kind=? AND attempt=?
               LIMIT 1""",
            (cycle_id, kind, attempt),
        ).fetchone()
        if existing is not None:
            raise StoreError(
                "Action attempt already exists for this cycle and kind"
            )

    def prepare_action(
        self,
        record: CycleRecord,
        *,
        kind: str,
        attempt: int,
        nonce: int,
        calldata_hash: str,
    ) -> ActionRecord:
        action_attempt = self._validate_action_attempt(attempt)
        action_id = str(uuid.uuid4())
        now = _utc_now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT state, revision FROM cycles WHERE cycle_id=?", (record.cycle_id,)
            ).fetchone()
            if row is None or row["state"] != record.state.value or row["revision"] != record.revision:
                raise StoreError("Cannot prepare an action from stale cycle state")
            self._assert_action_attempt_available(
                conn,
                cycle_id=record.cycle_id,
                kind=kind,
                attempt=action_attempt,
            )
            try:
                conn.execute(
                    """INSERT INTO actions(
                        action_id, cycle_id, kind, attempt, nonce, calldata_hash,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?)""",
                    (
                        action_id,
                        record.cycle_id,
                        kind,
                        action_attempt,
                        int(nonce),
                        calldata_hash,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:  # pragma: no cover - UUID/FK invariant
                raise StoreError("Cannot persist duplicate action identity") from exc
            action_row = conn.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
        return self._action(action_row)

    def arm_action(
        self,
        record: CycleRecord,
        *,
        kind: str,
        attempt: int,
        nonce: int,
        calldata_hash: str,
        pending_state: CycleState,
        runtime_updates: dict[str, Any] | None = None,
    ) -> tuple[CycleRecord, ActionRecord]:
        """Atomically persist a write intent and move the FSM to pending.

        There is no crash window in which an action exists without the cycle
        pointing at it (or vice versa).
        """

        if pending_state not in ALLOWED_TRANSITIONS[record.state]:
            raise StoreError(
                f"Illegal state transition: {record.state} -> {pending_state}"
            )
        action_attempt = self._validate_action_attempt(attempt)
        action_id = str(uuid.uuid4())
        now = _utc_now()
        runtime = dict(record.runtime)
        if runtime_updates:
            runtime.update(runtime_updates)
        runtime.update(
            {
                "action_id": action_id,
                "expected_action_kind": kind,
                "write_from_state": record.state.value,
            }
        )
        with self._transaction() as conn:
            current = conn.execute(
                "SELECT state, revision FROM cycles WHERE cycle_id=?", (record.cycle_id,)
            ).fetchone()
            if (
                current is None
                or current["state"] != record.state.value
                or current["revision"] != record.revision
            ):
                raise StoreError("Cannot arm an action from stale cycle state")
            self._assert_action_attempt_available(
                conn,
                cycle_id=record.cycle_id,
                kind=kind,
                attempt=action_attempt,
            )
            try:
                conn.execute(
                    """INSERT INTO actions(
                        action_id, cycle_id, kind, attempt, nonce, calldata_hash,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?)""",
                    (
                        action_id,
                        record.cycle_id,
                        kind,
                        action_attempt,
                        int(nonce),
                        calldata_hash,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:  # pragma: no cover - UUID/FK invariant
                raise StoreError("Cannot persist duplicate action identity") from exc
            cursor = conn.execute(
                """UPDATE cycles
                   SET state=?, revision=revision+1, runtime_json=?, updated_at=?, last_error=NULL
                   WHERE cycle_id=? AND state=? AND revision=?""",
                (
                    pending_state.value,
                    _json(runtime),
                    now,
                    record.cycle_id,
                    record.state.value,
                    record.revision,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - protected above
                raise StoreError("Failed to atomically arm the action")
            cycle_row = conn.execute(
                "SELECT * FROM cycles WHERE cycle_id=?", (record.cycle_id,)
            ).fetchone()
            action_row = conn.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
        return self._cycle(cycle_row), self._action(action_row)

    def record_signed_transaction(
        self,
        action: ActionRecord,
        tx_hash: str,
        raw_transaction: bytes,
    ) -> ActionRecord:
        if not tx_hash.startswith("0x") or len(tx_hash) != 66:
            raise StoreError("Malformed transaction hash")
        if not raw_transaction or len(raw_transaction) > 512 * 1024:
            raise StoreError("Malformed signed transaction")
        now = _utc_now()
        with self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE actions SET tx_hash=?, raw_tx=?, status='SIGNED', updated_at=?
                   WHERE action_id=? AND status='PREPARED' AND tx_hash IS NULL""",
                (tx_hash.lower(), sqlite3.Binary(raw_transaction), now, action.action_id),
            )
            if cursor.rowcount != 1:
                raise StoreError("Transaction hash was already recorded or action changed")
            row = conn.execute(
                "SELECT * FROM actions WHERE action_id=?", (action.action_id,)
            ).fetchone()
        return self._action(row)

    def record_tx_hash(self, action: ActionRecord, tx_hash: str) -> ActionRecord:
        """Compatibility helper for tests; workflow uses record_signed_transaction."""

        return self.record_signed_transaction(action, tx_hash, b"legacy-test-raw")

    def mark_action(
        self,
        action_id: str,
        status: str,
        *,
        receipt_block: int | None = None,
    ) -> ActionRecord:
        transitions = {
            "PREPARED": {"SIGNED", "NOT_BROADCAST"},
            "SIGNED": {
                "BROADCAST",
                "CONFIRMED",
                "REVERTED",
                "UNKNOWN",
                "NOT_BROADCAST",
            },
            "BROADCAST": {"CONFIRMED", "REVERTED", "UNKNOWN"},
            "UNKNOWN": {"BROADCAST", "CONFIRMED", "REVERTED", "UNKNOWN"},
            "CONFIRMED": set(),
            "REVERTED": set(),
            "NOT_BROADCAST": set(),
        }
        if status not in {"BROADCAST", "CONFIRMED", "REVERTED", "UNKNOWN", "NOT_BROADCAST"}:
            raise StoreError("Unsupported action status")
        now = _utc_now()
        with self._transaction() as conn:
            current = conn.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if current is None:
                raise StoreError("Unknown action")
            current_status = str(current["status"])
            same_status = status == current_status
            legacy_not_broadcast_material = (
                same_status
                and status == "NOT_BROADCAST"
                and (
                    current["tx_hash"] is not None
                    or current["raw_tx"] is not None
                )
            )
            if same_status and not legacy_not_broadcast_material:
                return self._action(current)
            if not same_status and status not in transitions.get(current_status, set()):
                raise StoreError(
                    f"Illegal action transition: {current_status} -> {status}"
                )
            purge_raw = status in {"CONFIRMED", "REVERTED", "NOT_BROADCAST"}
            clear_hash = status == "NOT_BROADCAST"
            cursor = conn.execute(
                """UPDATE actions
                   SET status=?, receipt_block=?, updated_at=?,
                       raw_tx=CASE WHEN ? THEN NULL ELSE raw_tx END,
                       tx_hash=CASE WHEN ? THEN NULL ELSE tx_hash END
                   WHERE action_id=? AND status=?""",
                (
                    status,
                    receipt_block,
                    now,
                    1 if purge_raw else 0,
                    1 if clear_hash else 0,
                    action_id,
                    current_status,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("Unknown action")
            row = conn.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
        return self._action(row)

    @staticmethod
    def _account_has_unresolved_actions_in(
        conn: sqlite3.Connection,
        account: str,
    ) -> bool:
        row = conn.execute(
            """SELECT 1
               FROM actions AS action
               JOIN cycles AS cycle ON cycle.cycle_id=action.cycle_id
               WHERE cycle.account=?
                 AND action.status IN ('SIGNED', 'BROADCAST', 'UNKNOWN')
               LIMIT 1""",
            (account,),
        ).fetchone()
        return row is not None

    def account_has_unresolved_actions(self, account: str) -> bool:
        """Check unresolved writes across the account's complete cycle history."""

        owner = normalize_address(account)
        with self._connect() as conn:
            return self._account_has_unresolved_actions_in(conn, owner)

    @staticmethod
    def _is_provably_pre_entry_snapshot(
        record: CycleRecord,
        actions: list[ActionRecord],
    ) -> bool:
        """Evaluate a cycle and its action journal from one database snapshot."""

        if record.state not in _PRE_ENTRY_RESOLUTION_STATES:
            return False
        if _POST_ENTRY_RUNTIME_KEYS.intersection(record.runtime):
            return False

        # PAUSED/QUARANTINED can be reached on either side of entry.  Durable
        # provenance pointing back to a post-entry state makes them unsafe even
        # if the action table is incomplete or damaged.
        provenance_keys: set[str] = set()
        for key in ("resume_state", "write_from_state"):
            raw_state = record.runtime.get(key)
            if raw_state is None:
                continue
            try:
                source_state = CycleState(str(raw_state))
            except ValueError:
                return False
            if source_state not in _PRE_ENTRY_RESUME_STATES:
                return False
            provenance_keys.add(key)
        if record.state is CycleState.PAUSED and "resume_state" not in provenance_keys:
            return False

        expected_kind = record.runtime.get("expected_action_kind")
        if expected_kind is not None and str(expected_kind) not in {
            "approval",
            "entry",
        }:
            return False
        if "exit_attempts" in record.runtime:
            try:
                if int(record.runtime["exit_attempts"]) != 0:
                    return False
            except (TypeError, ValueError):
                return False

        action_by_id = {action.action_id: action for action in actions}
        if len(action_by_id) != len(actions):  # pragma: no cover - PK invariant
            return False
        referenced_action_id = record.runtime.get("action_id")
        referenced: ActionRecord | None = None
        if referenced_action_id is not None:
            referenced = action_by_id.get(str(referenced_action_id))
            if referenced is None:
                return False
            if expected_kind is not None and referenced.kind != str(expected_kind):
                return False

        for action in actions:
            if action.status in _UNRESOLVED_ACTION_STATUSES:
                return False
            if action.status == "PREPARED" and (
                action.tx_hash is not None or action.raw_transaction is not None
            ):
                return False
            if action.kind == "approval":
                if action.status not in _SAFE_APPROVAL_STATUSES:
                    return False
                continue
            if action.kind == "entry":
                if action.status not in _SAFE_ENTRY_STATUSES:
                    return False
                continue
            # Any cancel/reduce/exit (and every unknown write kind) means this
            # is not a pre-entry-only journal, regardless of its terminal status.
            return False

        if (
            record.state is CycleState.QUARANTINED
            and not provenance_keys
            and referenced is None
            and not any(
                action.kind == "entry"
                and action.status in {"REVERTED", "NOT_BROADCAST"}
                for action in actions
            )
        ):
            # Legacy entry reverts may predate persisted provenance fields.
            # A terminal REVERTED/NOT_BROADCAST entry is still positive
            # durable evidence that no entry landed. CONFIRMED and every
            # unresolved status were already rejected above.
            return False

        if record.state is CycleState.APPROVAL_PENDING and not any(
            action.kind == "approval" for action in actions
        ):
            return False
        if record.state is CycleState.ENTRY_TX_PENDING and not any(
            action.kind == "entry" for action in actions
        ):
            return False
        return True

    def is_provably_pre_entry(self, record: CycleRecord) -> bool:
        """Return whether the current durable snapshot proves entry never landed.

        This is a read-only preview predicate.  A caller performing a state
        change must use :meth:`resolve_pre_entry_flat_cycles`, which repeats the
        proof together with lease and revision checks under ``BEGIN IMMEDIATE``.
        """

        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                row = conn.execute(
                    "SELECT * FROM cycles WHERE cycle_id=?", (record.cycle_id,)
                ).fetchone()
                if row is None:
                    return False
                current = self._cycle(row)
                if (
                    current.account != record.account
                    or current.state is not record.state
                    or current.revision != record.revision
                ):
                    return False
                if self._account_has_unresolved_actions_in(conn, current.account):
                    return False
                action_rows = conn.execute(
                    "SELECT * FROM actions WHERE cycle_id=? ORDER BY created_at",
                    (record.cycle_id,),
                ).fetchall()
                actions = [self._action(action_row) for action_row in action_rows]
                return self._is_provably_pre_entry_snapshot(current, actions)
            finally:
                conn.execute("ROLLBACK")

    def resolve_pre_entry_flat_cycles(
        self,
        expected_records: list[CycleRecord],
        leases: list[Lease],
        reason: str,
    ) -> list[CycleRecord]:
        """Atomically abort the exact globally-reviewed pre-entry active set.

        Remote flatness is a caller-side, signerless proof.  This method is the
        durable commit barrier: it validates every supplied fencing lease,
        re-checks that ``expected_records`` is the complete global nonterminal
        set at the reviewed revisions, re-evaluates the pre-entry journal, marks
        safe PREPARED writes as NOT_BROADCAST, and only then aborts every cycle.
        Any mismatch rolls the entire batch back.

        ``leases`` may be a superset of cycle accounts because vault replacement
        also fences audited old accounts that have no active cycle.
        """

        expected_by_id: dict[str, CycleRecord] = {}
        expected_accounts: set[str] = set()
        for record in expected_records:
            owner = normalize_address(record.account)
            if record.cycle_id in expected_by_id:
                raise StoreError("duplicate cycle in pre-entry resolution batch")
            if owner in expected_accounts:
                raise StoreError("duplicate active account in pre-entry resolution batch")
            if owner != record.account:
                raise StoreError("cycle account is not normalized")
            expected_by_id[record.cycle_id] = record
            expected_accounts.add(owner)

        lease_by_account: dict[str, Lease] = {}
        for lease in leases:
            owner = normalize_address(lease.account)
            if owner in lease_by_account:
                raise StoreError("duplicate account lease in pre-entry resolution batch")
            if owner != lease.account:
                raise StoreError("lease account is not normalized")
            lease_by_account[owner] = lease
        if not expected_accounts.issubset(lease_by_account):
            raise StoreError("every active cycle must have a matching account lease")

        clean_reason = reason.strip()[:240]
        resolved: list[CycleRecord] = []
        with self._transaction() as conn:
            # Take wall-clock values only after BEGIN IMMEDIATE has acquired the
            # write lock; time spent waiting for that lock must count against
            # every lease's expiry.
            now_epoch = time.time()
            now_text = _utc_now()
            # Every audited account, including an old-vault account without a
            # cycle, must remain fenced for the whole commit.
            for owner, lease in lease_by_account.items():
                lease_row = conn.execute(
                    "SELECT owner, fencing_token, expires_at FROM leases WHERE account=?",
                    (owner,),
                ).fetchone()
                if (
                    lease_row is None
                    or lease_row["owner"] != lease.owner
                    or lease_row["fencing_token"] != lease.fencing_token
                    or lease_row["expires_at"] <= now_epoch
                ):
                    raise StoreError("execution lease is stale during pre-entry resolution")
                if self._account_has_unresolved_actions_in(conn, owner):
                    raise StoreError(
                        "account journal contains an unresolved transaction"
                    )

            terminal = tuple(state.value for state in TERMINAL_STATES)
            placeholders = ",".join("?" for _ in terminal)
            active_rows = conn.execute(
                f"SELECT * FROM cycles WHERE state NOT IN ({placeholders})",
                terminal,
            ).fetchall()
            active_by_id = {
                str(row["cycle_id"]): self._cycle(row) for row in active_rows
            }
            if set(active_by_id) != set(expected_by_id):
                raise StoreError("global active cycle set changed during pre-entry resolution")

            snapshots: list[tuple[CycleRecord, list[ActionRecord]]] = []
            for record in expected_records:
                current = active_by_id[record.cycle_id]
                if (
                    current.account != record.account
                    or current.state is not record.state
                    or current.revision != record.revision
                ):
                    raise StoreError("cycle revision changed during pre-entry resolution")
                action_rows = conn.execute(
                    "SELECT * FROM actions WHERE cycle_id=? ORDER BY created_at",
                    (record.cycle_id,),
                ).fetchall()
                actions = [self._action(row) for row in action_rows]
                if not self._is_provably_pre_entry_snapshot(current, actions):
                    raise StoreError(
                        "cycle journal does not prove a safe pre-entry state"
                    )
                snapshots.append((current, actions))

            # No mutation occurs until every member of the batch has passed.
            for current, actions in snapshots:
                prepared_ids = [
                    action.action_id
                    for action in actions
                    if action.status == "PREPARED"
                ]
                if prepared_ids:
                    cursor = conn.executemany(
                        """UPDATE actions
                           SET status='NOT_BROADCAST', raw_tx=NULL, updated_at=?
                           WHERE action_id=? AND status='PREPARED'""",
                        ((now_text, action_id) for action_id in prepared_ids),
                    )
                    if cursor.rowcount != len(prepared_ids):
                        raise StoreError(
                            "action journal changed during pre-entry resolution"
                        )
                cursor = conn.execute(
                    """UPDATE cycles
                       SET state=?, revision=revision+1, updated_at=?, last_error=?
                       WHERE cycle_id=? AND state=? AND revision=?""",
                    (
                        CycleState.ABORTED.value,
                        now_text,
                        clean_reason,
                        current.cycle_id,
                        current.state.value,
                        current.revision,
                    ),
                )
                if cursor.rowcount != 1:  # pragma: no cover - write lock invariant
                    raise StoreError("cycle changed during pre-entry resolution")

            for record in expected_records:
                row = conn.execute(
                    "SELECT * FROM cycles WHERE cycle_id=?", (record.cycle_id,)
                ).fetchone()
                if row is None:  # pragma: no cover - PK invariant
                    raise StoreError("resolved cycle disappeared")
                resolved.append(self._cycle(row))
        return resolved

    def resolve_cycle_flat(self, record: CycleRecord, *, reason: str) -> CycleRecord:
        """Terminally resolve an operator-reviewed nonterminal cycle.

        The caller must independently prove the remote account is flat and no
        transaction is unresolved.  This method only supplies the atomic CAS.
        """

        allowed = {
            CycleState.BLOCKED_FUNDS,
            CycleState.DUST_BLOCKED,
            CycleState.PAUSED,
            CycleState.QUARANTINED,
        }
        if record.state not in allowed:
            raise StoreError("Only a blocked/paused/quarantined flat cycle can be resolved")
        now = _utc_now()
        with self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE cycles
                   SET state=?, revision=revision+1, updated_at=?, last_error=?
                   WHERE cycle_id=? AND state=? AND revision=?""",
                (
                    CycleState.ABORTED.value,
                    now,
                    reason.strip()[:240],
                    record.cycle_id,
                    record.state.value,
                    record.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreError("Cycle changed while resolving it")
            row = conn.execute(
                "SELECT * FROM cycles WHERE cycle_id=?", (record.cycle_id,)
            ).fetchone()
        return self._cycle(row)

    def get_action(self, action_id: str) -> ActionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM actions WHERE action_id=?", (action_id,)
            ).fetchone()
        return self._action(row) if row is not None else None

    def actions_for_cycle(self, cycle_id: str) -> list[ActionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM actions WHERE cycle_id=? ORDER BY created_at", (cycle_id,)
            ).fetchall()
        return [self._action(row) for row in rows]

    def purge_expired_leases(self) -> int:
        """Drop leases whose TTL already elapsed. Returns deleted row count."""

        now = time.time()
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM leases WHERE expires_at <= ?", (now,)
            )
            return int(cursor.rowcount or 0)

    def force_release_account_lease(self, account: str) -> bool:
        """Operator-local console: drop any lease on the account (expired or not)."""

        owner_address = normalize_address(account)
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM leases WHERE account=?", (owner_address,)
            )
            return int(cursor.rowcount or 0) > 0

    def acquire_lease(self, account: str, *, ttl_seconds: int = 300) -> Lease:
        owner_address = normalize_address(account)
        owner = str(uuid.uuid4())
        now = time.time()
        expires = now + max(10, min(ttl_seconds, 300))
        with self._transaction() as conn:
            # Always drop expired rows first so a crashed farm does not block
            # the next operator start for the full TTL window.
            conn.execute("DELETE FROM leases WHERE expires_at <= ?", (now,))
            row = conn.execute(
                "SELECT * FROM leases WHERE account=?", (owner_address,)
            ).fetchone()
            if row is not None and row["expires_at"] > now:
                raise StoreError("Another process holds the account execution lease")
            token = (int(row["fencing_token"]) + 1) if row is not None else 1
            conn.execute(
                """INSERT INTO leases(account, owner, fencing_token, expires_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account) DO UPDATE SET
                     owner=excluded.owner,
                     fencing_token=excluded.fencing_token,
                     expires_at=excluded.expires_at""",
                (owner_address, owner, token, expires),
            )
        return Lease(owner_address, owner, token, expires)

    def heartbeat(self, lease: Lease, *, ttl_seconds: int = 300) -> Lease:
        expires = time.time() + max(10, min(ttl_seconds, 300))
        with self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE leases SET expires_at=?
                   WHERE account=? AND owner=? AND fencing_token=?""",
                (expires, lease.account, lease.owner, lease.fencing_token),
            )
            if cursor.rowcount != 1:
                raise StoreError("Execution lease was lost; refusing further writes")
        return Lease(lease.account, lease.owner, lease.fencing_token, expires)

    def assert_lease(self, lease: Lease) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM leases WHERE account=?", (lease.account,)
            ).fetchone()
        if (
            row is None
            or row["owner"] != lease.owner
            or row["fencing_token"] != lease.fencing_token
            or row["expires_at"] <= time.time()
        ):
            raise StoreError("Execution lease is stale; refusing to sign or broadcast")

    def release_lease(self, lease: Lease) -> None:
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM leases WHERE account=? AND owner=? AND fencing_token=?",
                (lease.account, lease.owner, lease.fencing_token),
            )

    def set_kill(self, account: str, mode: str, reason: str) -> None:
        owner = normalize_address(account)
        if mode not in {"KILL_FLATTEN", "FREEZE_SIGNER"}:
            raise StoreError("kill mode must be KILL_FLATTEN or FREEZE_SIGNER")
        clean_reason = reason.strip()[:240]
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO controls(account, kill_mode, reason, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account) DO UPDATE SET
                     kill_mode=excluded.kill_mode,
                     reason=excluded.reason,
                     updated_at=excluded.updated_at
                   WHERE NOT (
                     controls.kill_mode='FREEZE_SIGNER'
                     AND excluded.kill_mode='KILL_FLATTEN'
                   )""",
                (owner, mode, clean_reason, _utc_now()),
            )

    def clear_kill(self, account: str) -> None:
        owner = normalize_address(account)
        with self._transaction() as conn:
            conn.execute("DELETE FROM controls WHERE account=?", (owner,))

    def clear_kill_if_matches(
        self,
        account: str,
        expected_mode: str,
        expected_updated_at: str,
    ) -> bool:
        """Clear only the exact control previously reviewed by an operator."""

        owner = normalize_address(account)
        if expected_mode not in {"KILL_FLATTEN", "FREEZE_SIGNER"}:
            raise StoreError("expected kill mode is invalid")
        with self._transaction() as conn:
            cursor = conn.execute(
                """DELETE FROM controls
                   WHERE account=? AND kill_mode=? AND updated_at=?""",
                (owner, expected_mode, str(expected_updated_at)),
            )
        return cursor.rowcount == 1

    def clear_kills_if_matches(
        self,
        expected_controls: list[tuple[str, str, str]],
        leases: list[Lease],
    ) -> bool:
        """Atomically clear an exact reviewed control set under live leases."""

        normalized: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for account, mode, updated_at in expected_controls:
            owner = normalize_address(account)
            if owner in seen:
                raise StoreError("duplicate account in control batch")
            if mode not in {"KILL_FLATTEN", "FREEZE_SIGNER"}:
                raise StoreError("expected kill mode is invalid")
            seen.add(owner)
            normalized.append((owner, mode, str(updated_at)))
        lease_by_account = {lease.account: lease for lease in leases}
        if set(lease_by_account) != seen or len(lease_by_account) != len(leases):
            raise StoreError("control batch must have one matching lease per account")

        now = time.time()
        with self._transaction() as conn:
            for owner, mode, updated_at in normalized:
                lease = lease_by_account[owner]
                lease_row = conn.execute(
                    "SELECT owner, fencing_token, expires_at FROM leases WHERE account=?",
                    (owner,),
                ).fetchone()
                if (
                    lease_row is None
                    or lease_row["owner"] != lease.owner
                    or lease_row["fencing_token"] != lease.fencing_token
                    or lease_row["expires_at"] <= now
                ):
                    return False
                control_row = conn.execute(
                    "SELECT kill_mode, updated_at FROM controls WHERE account=?",
                    (owner,),
                ).fetchone()
                if (
                    control_row is None
                    or control_row["kill_mode"] != mode
                    or control_row["updated_at"] != updated_at
                ):
                    return False
            conn.executemany(
                "DELETE FROM controls WHERE account=?",
                ((owner,) for owner, _mode, _updated_at in normalized),
            )
        return True

    def get_kill(self, account: str) -> dict[str, str] | None:
        owner = normalize_address(account)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT kill_mode, reason, updated_at FROM controls WHERE account=?",
                (owner,),
            ).fetchone()
        return dict(row) if row is not None else None
