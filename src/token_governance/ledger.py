from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    Action,
    Confidence,
    GovernanceResult,
    ReasonCode,
    Risk,
    SourceKind,
    Strategy,
)


SCHEMA_VERSION = 2
DEFAULT_BUSY_TIMEOUT_MS = 1000
MAX_BUSY_TIMEOUT_MS = 30_000
_STORAGE_SUFFIXES = ("", "-journal", "-wal", "-shm")
_V1_COLUMNS = {
    "receipt_id",
    "source",
    "content_type",
    "action",
    "risk",
    "original_text",
    "governed_text",
    "original_hash",
    "token_before",
    "token_after",
    "policy",
    "notes",
    "created_at",
}
_V2_RECEIPT_COLUMNS = _V1_COLUMNS | {
    "strategy",
    "confidence",
    "reason_code",
    "protected_fact_count",
    "preservation_check",
    "delivery_state",
    "is_legacy",
}
_V2_EVENT_COLUMNS = {
    "event_id",
    "created_at",
    "source_kind",
    "action",
    "risk",
    "reason_code",
    "token_count",
    "receipt_id",
}
_V2_INDEXES = {
    "idx_receipts_created_at": "receipts",
    "idx_receipts_delivery_state": "receipts",
    "idx_events_created_at": "governance_events",
    "idx_events_receipt_id": "governance_events",
}
_V2_INDEX_COLUMNS = {
    "idx_receipts_created_at": ("created_at",),
    "idx_receipts_delivery_state": ("is_legacy", "delivery_state"),
    "idx_events_created_at": ("created_at",),
    "idx_events_receipt_id": ("receipt_id",),
}
_V2_INDEX_SQL = {
    "idx_receipts_created_at": (
        "CREATE INDEX idx_receipts_created_at ON receipts(created_at)"
    ),
    "idx_receipts_delivery_state": (
        "CREATE INDEX idx_receipts_delivery_state "
        "ON receipts(is_legacy, delivery_state)"
    ),
    "idx_events_created_at": (
        "CREATE INDEX idx_events_created_at ON governance_events(created_at)"
    ),
    "idx_events_receipt_id": (
        "CREATE INDEX idx_events_receipt_id ON governance_events(receipt_id)"
    ),
}


class LedgerIntegrityError(RuntimeError):
    code = "ledger_integrity_failed"

    def __init__(self) -> None:
        super().__init__("Receipt integrity check failed")


class LegacyWriteDisabledError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Legacy ledger writes are disabled; use store_prepared")


class LedgerSecurityError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Unable to secure ledger storage")


def _ensure_posix_private_file(path: Path, *, create: bool = True) -> None:
    descriptor = None
    try:
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for component in absolute.parent.parts[1:]:
            current /= component
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise LedgerSecurityError()
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        if create:
            flags |= os.O_CREAT
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LedgerSecurityError()
        os.fchmod(descriptor, 0o600)
    except FileNotFoundError:
        if create:
            raise LedgerSecurityError()
    except LedgerSecurityError:
        raise
    except OSError as exc:
        raise LedgerSecurityError() from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _windows_current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    token_query = 0x0008
    token_user_class = 1
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    )

    class SidAndAttributes(ctypes.Structure):
        _fields_ = (("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD))

    class TokenUser(ctypes.Structure):
        _fields_ = (("user", SidAndAttributes),)

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token, token_user_class, None, 0, ctypes.byref(required)
        )
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user_class,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents.user.sid
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return str(sid_text.value)
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


def _ensure_windows_private_file(path: Path, *, create: bool = True) -> None:
    import ctypes
    from ctypes import wintypes

    handle = None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.GetFileAttributesW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetFileAttributesW.restype = wintypes.DWORD
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for component in absolute.parent.parts[1:]:
            current /= component
            attributes = kernel32.GetFileAttributesW(str(current))
            if attributes == 0xFFFFFFFF:
                error = ctypes.get_last_error()
                if error in (2, 3):
                    continue
                raise ctypes.WinError(error)
            if attributes & 0x00000400:
                raise LedgerSecurityError()

        sid = _windows_current_user_sid()
        security_descriptor = ctypes.c_void_p()
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        )
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"D:P(A;;FA;;;{sid})",
            1,
            ctypes.byref(security_descriptor),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        class SecurityAttributes(ctypes.Structure):
            _fields_ = (
                ("length", wintypes.DWORD),
                ("security_descriptor", ctypes.c_void_p),
                ("inherit_handle", wintypes.BOOL),
            )

        attributes = SecurityAttributes(
            ctypes.sizeof(SecurityAttributes), security_descriptor, False
        )
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(SecurityAttributes),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(
            str(path),
            0x80000000 | 0x40000000 | 0x00040000,
            0x00000001 | 0x00000002 | 0x00000004,
            ctypes.byref(attributes),
            4 if create else 3,
            0x00000080 | 0x00200000,
            None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle == invalid_handle:
            handle = None
            error = ctypes.get_last_error()
            if not create and error in (2, 3):
                return
            raise ctypes.WinError(error)

        class FileAttributeTagInfo(ctypes.Structure):
            _fields_ = (
                ("file_attributes", wintypes.DWORD),
                ("reparse_tag", wintypes.DWORD),
            )

        file_info = FileAttributeTagInfo()
        kernel32.GetFileInformationByHandleEx.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        if not kernel32.GetFileInformationByHandleEx(
            handle,
            9,
            ctypes.byref(file_info),
            ctypes.sizeof(file_info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        disallowed_attributes = 0x00000400 | 0x00000010 | 0x00000040
        if file_info.file_attributes & disallowed_attributes:
            raise LedgerSecurityError()

        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        advapi32.GetSecurityDescriptorDacl.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        )
        if not advapi32.GetSecurityDescriptorDacl(
            security_descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present:
            raise ctypes.WinError(ctypes.get_last_error())
        advapi32.SetSecurityInfo.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        advapi32.SetSecurityInfo.restype = wintypes.DWORD
        result = advapi32.SetSecurityInfo(
            handle,
            1,
            0x00000004 | 0x80000000,
            None,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise OSError(result, "SetSecurityInfo failed")
    except Exception as exc:
        raise LedgerSecurityError() from exc
    finally:
        if handle is not None and "kernel32" in locals():
            kernel32.CloseHandle(handle)
        if "security_descriptor" in locals() and security_descriptor:
            kernel32.LocalFree(security_descriptor)


class ContextLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ):
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or not 0 <= busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS
        ):
            raise TypeError("busy_timeout_ms must be an integer from 0 through 30000")
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_private_storage()
        self._init_db()

    def _storage_paths(self) -> tuple[Path, ...]:
        return tuple(Path(f"{self.path}{suffix}") for suffix in _STORAGE_SUFFIXES)

    def _prepare_private_storage(self) -> None:
        secure = (
            _ensure_windows_private_file if os.name == "nt" else _ensure_posix_private_file
        )
        try:
            secure(self.path)
        except LedgerSecurityError:
            raise
        except Exception as exc:
            raise LedgerSecurityError() from exc

    def _secure_existing_storage(self) -> None:
        if os.name != "nt":
            return
        try:
            for path in self._storage_paths():
                _ensure_windows_private_file(path, create=False)
        except LedgerSecurityError:
            raise
        except Exception as exc:
            raise LedgerSecurityError() from exc

    def _connect(self) -> sqlite3.Connection:
        self._prepare_private_storage()
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA schema_version").fetchone()
            try:
                self._secure_existing_storage()
            except LedgerSecurityError:
                raise
            except BaseException as exc:
                raise LedgerSecurityError() from exc
        except BaseException:
            try:
                conn.close()
            except BaseException:
                pass
            raise
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _begin(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        self._secure_existing_storage()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if version == SCHEMA_VERSION:
                self._validate_v2(conn)
                return
            if version > SCHEMA_VERSION:
                raise RuntimeError("Ledger schema is newer than this application")
            if "receipts" not in tables:
                if version not in (0, 1):
                    raise RuntimeError("Unsupported ledger schema version")
                self._create_v2(conn)
                return
            columns = self._table_columns(conn, "receipts")
            if version in (0, 1) and columns == _V1_COLUMNS:
                self._migrate_v1(conn)
                return
            raise RuntimeError("Unsupported or incomplete ledger schema")

    def _validate_v2(self, conn: sqlite3.Connection) -> None:
        try:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {"receipts", "governance_events"} <= tables:
                raise ValueError
            if self._table_columns(conn, "receipts") != _V2_RECEIPT_COLUMNS:
                raise ValueError
            if self._table_columns(conn, "governance_events") != _V2_EVENT_COLUMNS:
                raise ValueError
            indexes = {
                str(row[0]): str(row[1])
                for row in conn.execute(
                    "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'"
                )
                if str(row[0]) in _V2_INDEXES
            }
            if indexes != _V2_INDEXES:
                raise ValueError
            for index_name, expected_columns in _V2_INDEX_COLUMNS.items():
                actual_columns = tuple(
                    str(row[2])
                    for row in conn.execute(f"PRAGMA index_info({index_name})")
                )
                if actual_columns != expected_columns:
                    raise ValueError
            index_sql = {
                str(row[0]): " ".join(str(row[1]).lower().split())
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
                )
                if str(row[0]) in _V2_INDEX_SQL and isinstance(row[1], str)
            }
            expected_index_sql = {
                name: " ".join(sql.lower().split())
                for name, sql in _V2_INDEX_SQL.items()
            }
            if index_sql != expected_index_sql:
                raise ValueError
            foreign_keys = conn.execute(
                "PRAGMA foreign_key_list(governance_events)"
            ).fetchall()
            actual_foreign_keys = tuple(tuple(row) for row in foreign_keys)
            expected_foreign_keys = (
                (
                    0,
                    0,
                    "receipts",
                    "receipt_id",
                    "receipt_id",
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
            )
            if actual_foreign_keys != expected_foreign_keys:
                raise ValueError
            receipt_sql = self._normalized_table_sql(conn, "receipts")
            event_sql = self._normalized_table_sql(conn, "governance_events")
            expected_receipt_sql = " ".join(
                self._receipts_table_sql().lower().split()
            )
            expected_event_sql = " ".join(
                self._events_table_sql().lower().split()
            )
            receipt_fragments = (
                "is_legacy = 1 or",
                "action = 'transform'",
                "delivery_state in ('prepared', 'emitted')",
                "token_after < token_before",
            )
            event_fragments = (
                "check (source_kind in",
                "check (action in",
                "check (risk in",
                "check (reason_code in",
                "check (typeof(token_count) = 'integer' and token_count >= 0)",
            )
            if receipt_sql != expected_receipt_sql or not all(
                fragment in receipt_sql for fragment in receipt_fragments
            ):
                raise ValueError
            if event_sql != expected_event_sql or not all(
                fragment in event_sql for fragment in event_fragments
            ):
                raise ValueError
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise RuntimeError("Invalid ledger schema v2") from exc

    @staticmethod
    def _normalized_table_sql(conn: sqlite3.Connection, table: str) -> str:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            raise ValueError
        return " ".join(str(row[0]).lower().split())

    def _create_v2(self, conn: sqlite3.Connection) -> None:
        self._begin(conn)
        try:
            conn.execute(self._receipts_table_sql())
            self._create_events_and_indexes(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        self._begin(conn)
        try:
            conn.execute("ALTER TABLE receipts RENAME TO receipts_v1")
            conn.execute(self._receipts_table_sql())
            conn.execute(
                """
                INSERT INTO receipts (
                    receipt_id, source, content_type, action, risk,
                    original_text, governed_text, original_hash,
                    token_before, token_after, policy, notes, created_at,
                    strategy, confidence, reason_code, protected_fact_count,
                    preservation_check, delivery_state, is_legacy
                )
                SELECT
                    receipt_id, source, content_type, action, risk,
                    original_text, governed_text, original_hash,
                    token_before, token_after, policy, notes, created_at,
                    NULL, NULL, NULL, NULL, NULL, NULL, 1
                FROM receipts_v1
                """
            )
            conn.execute("DROP TABLE receipts_v1")
            self._create_events_and_indexes(conn)
            self._migration_checkpoint()
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _migration_checkpoint(self) -> None:
        """Internal fault-injection seam for migration rollback verification."""

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _receipts_table_sql() -> str:
        strategies = ContextLedger._enum_sql(
            strategy for strategy in Strategy if strategy is not Strategy.PASSTHROUGH
        )
        confidences = ContextLedger._enum_sql(
            confidence
            for confidence in Confidence
            if confidence is not Confidence.UNAVAILABLE
        )
        return f"""
            CREATE TABLE receipts (
                receipt_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                content_type TEXT NOT NULL,
                action TEXT NOT NULL,
                risk TEXT NOT NULL,
                original_text TEXT NOT NULL,
                governed_text TEXT NOT NULL,
                original_hash TEXT NOT NULL,
                token_before INTEGER NOT NULL CHECK (token_before >= 0),
                token_after INTEGER NOT NULL CHECK (token_after >= 0),
                policy TEXT NOT NULL,
                notes TEXT NOT NULL,
                created_at TEXT NOT NULL,
                strategy TEXT,
                confidence TEXT,
                reason_code TEXT,
                protected_fact_count INTEGER,
                preservation_check TEXT,
                delivery_state TEXT CHECK (delivery_state IS NULL OR delivery_state IN ('prepared', 'emitted')),
                is_legacy INTEGER NOT NULL DEFAULT 0 CHECK (is_legacy IN (0, 1)),
                CHECK (
                    is_legacy = 1 OR (
                        action = 'transform'
                        AND strategy IN ({strategies})
                        AND confidence IN ({confidences})
                        AND reason_code = 'transformed'
                        AND protected_fact_count >= 0
                        AND preservation_check IS NOT NULL
                        AND delivery_state IN ('prepared', 'emitted')
                        AND token_after < token_before
                    )
                )
            )
        """

    @classmethod
    def _events_table_sql(cls) -> str:
        source_kinds = cls._enum_sql(SourceKind)
        actions = cls._enum_sql(Action)
        risks = cls._enum_sql(Risk)
        reasons = cls._enum_sql(ReasonCode)
        return f"""
            CREATE TABLE governance_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source_kind TEXT NOT NULL CHECK (source_kind IN ({source_kinds})),
                action TEXT NOT NULL CHECK (action IN ({actions})),
                risk TEXT NOT NULL CHECK (risk IN ({risks})),
                reason_code TEXT NOT NULL CHECK (reason_code IN ({reasons})),
                token_count INTEGER NOT NULL
                    CHECK (typeof(token_count) = 'integer' AND token_count >= 0),
                receipt_id TEXT,
                FOREIGN KEY (receipt_id) REFERENCES receipts(receipt_id)
                    ON DELETE CASCADE
            )
        """

    @classmethod
    def _create_events_and_indexes(cls, conn: sqlite3.Connection) -> None:
        conn.execute(cls._events_table_sql())
        for sql in _V2_INDEX_SQL.values():
            conn.execute(sql)

    @staticmethod
    def _enum_sql(values: Any) -> str:
        return ", ".join(f"'{value.value}'" for value in values)

    @staticmethod
    def allocate_receipt_id() -> str:
        return f"tgl_{uuid.uuid4().hex}"

    def store_prepared(
        self,
        receipt_id: str,
        original_text: str,
        result: GovernanceResult,
    ) -> None:
        if not isinstance(receipt_id, str) or not receipt_id:
            raise TypeError("receipt_id must be a non-empty string")
        if not isinstance(original_text, str):
            raise TypeError("original_text must be a string")
        if not isinstance(result, GovernanceResult):
            raise TypeError("result must be a GovernanceResult")
        if result.receipt_id != receipt_id:
            raise ValueError("receipt_id must match the governance result")
        if result.action is not Action.TRANSFORM:
            raise ValueError("only transformed results may be stored")
        verification = result.preservation_check
        if verification is None or not verification.ok:
            raise ValueError("only verified transformations may be stored")

        preservation = json.dumps(
            {
                "ok": verification.ok,
                "protected_fact_count": verification.protected_fact_count,
                "missing_fact_count": verification.missing_fact_count,
                "reason_code": verification.reason_code.value,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        original_hash = self._hash(original_text)
        with self._connection() as conn:
            self._begin(conn)
            try:
                conn.execute(
                    """
                    INSERT INTO receipts (
                        receipt_id, source, content_type, action, risk,
                        original_text, governed_text, original_hash,
                        token_before, token_after, policy, notes, created_at,
                        strategy, confidence, reason_code,
                        protected_fact_count, preservation_check,
                        delivery_state, is_legacy
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        receipt_id,
                        "v2_engine",
                        result.strategy.value,
                        result.action.value,
                        result.risk.value,
                        original_text,
                        result.content,
                        original_hash,
                        result.token_before,
                        result.token_after,
                        result.strategy.value,
                        "[]",
                        self._utc_now_text(),
                        result.strategy.value,
                        result.confidence.value,
                        result.reason_code.value,
                        verification.protected_fact_count,
                        preservation,
                        "prepared",
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def mark_emitted(self, receipt_id: str) -> None:
        with self._connection() as conn:
            self._begin(conn)
            try:
                cursor = conn.execute(
                    """
                    UPDATE receipts
                    SET delivery_state = 'emitted'
                    WHERE receipt_id = ? AND is_legacy = 0
                      AND delivery_state = 'prepared'
                    """,
                    (receipt_id,),
                )
                if cursor.rowcount == 0:
                    row = conn.execute(
                        "SELECT is_legacy, delivery_state FROM receipts WHERE receipt_id = ?",
                        (receipt_id,),
                    ).fetchone()
                    if row is None:
                        raise KeyError(f"Receipt not found: {receipt_id}")
                    if bool(row["is_legacy"]):
                        raise ValueError("legacy receipts have no delivery lifecycle")
                    if row["delivery_state"] != "emitted":
                        raise ValueError("receipt is not prepared")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def record_event(
        self,
        source_kind: SourceKind,
        action: Action,
        risk: Risk,
        reason_code: ReasonCode,
        token_count: int,
        receipt_id: str | None,
    ) -> None:
        self._require_enum("source_kind", source_kind, SourceKind)
        self._require_enum("action", action, Action)
        self._require_enum("risk", risk, Risk)
        self._require_enum("reason_code", reason_code, ReasonCode)
        if isinstance(token_count, bool) or not isinstance(token_count, int) or token_count < 0:
            raise TypeError("token_count must be a non-negative integer")
        if receipt_id is not None and not isinstance(receipt_id, str):
            raise TypeError("receipt_id must be a string or None")
        with self._connection() as conn:
            self._begin(conn)
            try:
                conn.execute(
                    """
                    INSERT INTO governance_events (
                        created_at, source_kind, action, risk,
                        reason_code, token_count, receipt_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._utc_now_text(),
                        source_kind.value,
                        action.value,
                        risk.value,
                        reason_code.value,
                        token_count,
                        receipt_id,
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _require_enum(name: str, value: object, enum_type: type[Any]) -> None:
        if not isinstance(value, enum_type):
            raise TypeError(f"{name} must be a {enum_type.__name__}")

    def record(
        self,
        *,
        source: str,
        content_type: str,
        action: str,
        risk: str,
        original_text: str,
        governed_text: str,
        token_before: int,
        token_after: int,
        policy: str,
        notes: list[str],
    ) -> str:
        raise LegacyWriteDisabledError()

    def retrieve_original(self, receipt_id: str) -> str:
        row = self._get(receipt_id)
        original = str(row["original_text"])
        if not self._hash_matches(original, str(row["original_hash"])):
            raise LedgerIntegrityError()
        return original

    def explain_receipt(self, receipt_id: str) -> dict[str, Any]:
        row = self._get(receipt_id)
        return {
            "receipt_id": row["receipt_id"],
            "source": row["source"],
            "content_type": row["content_type"],
            "action": row["action"],
            "risk": row["risk"],
            "original_hash": row["original_hash"],
            "token_before": row["token_before"],
            "token_after": row["token_after"],
            "tokens_saved": row["token_before"] - row["token_after"],
            "policy": row["policy"],
            "notes": json.loads(row["notes"]),
            "created_at": row["created_at"],
            "strategy": row["strategy"],
            "confidence": row["confidence"],
            "reason_code": row["reason_code"],
            "protected_fact_count": row["protected_fact_count"],
            "preservation_check": (
                json.loads(row["preservation_check"])
                if row["preservation_check"] is not None
                else None
            ),
            "delivery_state": row["delivery_state"],
            "is_legacy": bool(row["is_legacy"]),
        }

    def stats(self) -> dict[str, Any]:
        with self._connection() as conn:
            emitted = self._aggregate(
                conn,
                "is_legacy = 0 AND action = 'transform' AND delivery_state = 'emitted'",
            )
            prepared = self._aggregate(
                conn,
                "is_legacy = 0 AND action = 'transform' AND delivery_state = 'prepared'",
            )
            legacy = self._aggregate(conn, "is_legacy = 1")
        return {"emitted": emitted, "prepared": prepared, "legacy": legacy}

    @staticmethod
    def _aggregate(conn: sqlite3.Connection, where: str) -> dict[str, int]:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS receipt_count,
                   COALESCE(SUM(token_before), 0) AS token_before,
                   COALESCE(SUM(token_after), 0) AS token_after
            FROM receipts WHERE {where}
            """
        ).fetchone()
        before = int(row["token_before"])
        after = int(row["token_after"])
        return {
            "receipt_count": int(row["receipt_count"]),
            "token_before": before,
            "token_after": after,
            "candidate_tokens_saved": before - after,
        }

    def savings(self) -> dict[str, Any]:
        with self._connection() as conn:
            primary = self._aggregate(
                conn,
                "is_legacy = 0 AND action = 'transform' AND delivery_state = 'emitted'",
            )
            by_type = conn.execute(
                """
                SELECT content_type, COUNT(*) AS receipt_count,
                       COALESCE(SUM(token_before), 0) AS token_before,
                       COALESCE(SUM(token_after), 0) AS token_after
                FROM receipts
                WHERE is_legacy = 0 AND action = 'transform'
                  AND delivery_state = 'emitted'
                GROUP BY content_type
                ORDER BY token_before DESC
                """
            ).fetchall()
        stats = self.stats()
        warnings = (
            ["legacy_receipts_present"]
            if stats["legacy"]["receipt_count"] > 0
            else []
        )
        return {
            "receipt_count": primary["receipt_count"],
            "token_before": primary["token_before"],
            "token_after": primary["token_after"],
            "tokens_saved": primary["candidate_tokens_saved"],
            "scope": "emitted_transforms",
            "savings_kind": "governed_candidate",
            "prepared": stats["prepared"],
            "legacy": stats["legacy"],
            "warnings": warnings,
            "by_content_type": [
                {
                    "content_type": row["content_type"],
                    "receipt_count": int(row["receipt_count"]),
                    "token_before": int(row["token_before"]),
                    "token_after": int(row["token_after"]),
                    "tokens_saved": int(row["token_before"]) - int(row["token_after"]),
                }
                for row in by_type
            ],
        }

    def risks(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT receipt_id, source_kind, action, risk, reason_code,
                       token_count, created_at
                FROM governance_events
                WHERE risk != 'low'
                ORDER BY created_at DESC, event_id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def prune(
        self,
        *,
        cutoff: datetime | None = None,
        retention_days: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, int]:
        cutoff_value = self._resolve_cutoff(cutoff, retention_days, now)
        cutoff_text = self._datetime_text(cutoff_value)
        with self._connection() as conn:
            self._begin(conn)
            try:
                before_receipts = int(
                    conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
                )
                before_events = int(
                    conn.execute("SELECT COUNT(*) FROM governance_events").fetchone()[0]
                )
                conn.execute(
                    "DELETE FROM receipts WHERE julianday(created_at) < julianday(?)",
                    (cutoff_text,),
                )
                conn.execute(
                    """
                    DELETE FROM governance_events
                    WHERE receipt_id IS NULL
                      AND julianday(created_at) < julianday(?)
                    """,
                    (cutoff_text,),
                )
                after_receipts = int(
                    conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
                )
                after_events = int(
                    conn.execute("SELECT COUNT(*) FROM governance_events").fetchone()[0]
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return {
            "receipts_deleted": before_receipts - after_receipts,
            "events_deleted": before_events - after_events,
        }

    def purge_legacy(self, *, confirm: bool = False) -> dict[str, int]:
        if confirm is not True:
            raise ValueError("explicit confirmation required to purge legacy receipts")
        with self._connection() as conn:
            self._begin(conn)
            try:
                before_events = int(
                    conn.execute("SELECT COUNT(*) FROM governance_events").fetchone()[0]
                )
                cursor = conn.execute("DELETE FROM receipts WHERE is_legacy = 1")
                after_events = int(
                    conn.execute("SELECT COUNT(*) FROM governance_events").fetchone()[0]
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return {
            "receipts_deleted": cursor.rowcount,
            "events_deleted": before_events - after_events,
        }

    @classmethod
    def _resolve_cutoff(
        cls,
        cutoff: datetime | None,
        retention_days: int | None,
        now: datetime | None,
    ) -> datetime:
        if cutoff is not None:
            if retention_days is not None or now is not None:
                raise ValueError("cutoff cannot be combined with retention_days or now")
            return cls._require_utc(cutoff, "cutoff")
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or not 1 <= retention_days <= 3650
        ):
            raise ValueError("retention_days must be between 1 and 3650")
        current = cls._require_utc(now or datetime.now(timezone.utc), "now")
        return current - timedelta(days=retention_days)

    @staticmethod
    def _require_utc(value: datetime, name: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError(f"{name} must be a timezone-aware UTC datetime")
        if value.utcoffset() != timedelta(0):
            raise ValueError(f"{name} must be a UTC datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _datetime_text(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def _utc_now_text(cls) -> str:
        return cls._datetime_text(datetime.now(timezone.utc))

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _hash_matches(cls, value: str, expected: str) -> bool:
        import hmac

        return hmac.compare_digest(cls._hash(value), expected)

    def _get(self, receipt_id: str) -> sqlite3.Row:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Receipt not found: {receipt_id}")
        return row
