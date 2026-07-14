from __future__ import annotations

import multiprocessing
import os
import sqlite3
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

import token_governance.ledger as ledger_module
from token_governance.contracts import (
    Action,
    Confidence,
    GovernanceResult,
    ReasonCode,
    Risk,
    SourceKind,
    Strategy,
    VerificationResult,
)
from token_governance.ledger import ContextLedger


FIXTURE = Path(__file__).parent / "fixtures" / "ledger_v1.sql"


def make_result(receipt_id: str, *, token_before: int = 20, token_after: int = 5):
    verification = VerificationResult(
        ok=True,
        protected_fact_count=2,
        missing_fact_count=0,
        reason_code=ReasonCode.PRESERVATION_PASSED,
    )
    return GovernanceResult(
        action=Action.TRANSFORM,
        content="governed output",
        risk=Risk.LOW,
        reason_code=ReasonCode.TRANSFORMED,
        strategy=Strategy.REPETITIVE_LOG,
        confidence=Confidence.HIGH,
        preservation_check=verification,
        token_before=token_before,
        token_after=token_after,
        tokens_saved=token_before - token_after,
        receipt_id=receipt_id,
    )


def materialize_v1(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(FIXTURE.read_text(encoding="utf-8"))


def table_columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def logical_snapshot(path: Path):
    with sqlite3.connect(path) as conn:
        return {
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "schema": conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall(),
            "receipts": conn.execute(
                "SELECT * FROM receipts ORDER BY receipt_id"
            ).fetchall(),
        }


def rewrite_table_sql(path: Path, table: str, old: str, new: str) -> None:
    with sqlite3.connect(path) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()[0]
        assert old in sql
        schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute("PRAGMA writable_schema = ON")
        conn.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
            (sql.replace(old, new), table),
        )
        conn.execute(f"PRAGMA schema_version = {schema_version + 1}")
        conn.execute("PRAGMA writable_schema = OFF")


def process_record_event(path: str, index: int) -> None:
    ContextLedger(path, busy_timeout_ms=2000).record_event(
        SourceKind.CLI,
        Action.PASSTHROUGH,
        Risk.LOW,
        ReasonCode.NO_MATCHING_STRATEGY,
        index,
        None,
    )


def test_new_database_has_v2_schema_checks_indexes_and_private_mode(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ContextLedger(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"receipts", "governance_events"} <= tables
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert {
            "idx_receipts_created_at",
            "idx_receipts_delivery_state",
            "idx_events_created_at",
            "idx_events_receipt_id",
        } <= indexes
        event_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'governance_events'"
        ).fetchone()[0]
        assert "CHECK" in event_sql

    assert {
        "strategy",
        "confidence",
        "reason_code",
        "protected_fact_count",
        "preservation_check",
        "delivery_state",
        "is_legacy",
    } <= table_columns(path, "receipts")
    assert table_columns(path, "governance_events") == {
        "event_id",
        "created_at",
        "source_kind",
        "action",
        "risk",
        "reason_code",
        "token_count",
        "receipt_id",
    }
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "damage",
    [
        "missing_table",
        "missing_column",
        "missing_index",
        "wrong_index",
        "missing_fk",
        "missing_check",
        "changed_enum_check",
        "event_created_at_nullable",
        "event_enum_check",
        "event_token_check",
        "partial_index",
        "unique_index",
    ],
)
def test_existing_v2_schema_is_validated_before_business_use(tmp_path, damage):
    path = tmp_path / "ledger.sqlite"
    ContextLedger(path)
    if damage == "missing_table":
        with sqlite3.connect(path) as conn:
            conn.execute("DROP TABLE governance_events")
    elif damage == "missing_column":
        rewrite_table_sql(path, "receipts", "strategy", "strategy_missing")
    elif damage == "missing_index":
        with sqlite3.connect(path) as conn:
            conn.execute("DROP INDEX idx_events_receipt_id")
    elif damage == "wrong_index":
        with sqlite3.connect(path) as conn:
            conn.execute("DROP INDEX idx_events_receipt_id")
            conn.execute(
                "CREATE INDEX idx_events_receipt_id ON governance_events(token_count)"
            )
    elif damage == "missing_fk":
        rewrite_table_sql(
            path,
            "governance_events",
            "FOREIGN KEY (receipt_id) REFERENCES receipts(receipt_id)\n"
            "                    ON DELETE CASCADE",
            "CHECK (receipt_id IS NULL OR typeof(receipt_id) = 'text')",
        )
    elif damage == "missing_check":
        rewrite_table_sql(
            path,
            "receipts",
            "token_after < token_before",
            "token_after <= token_before",
        )
    elif damage == "changed_enum_check":
        rewrite_table_sql(
            path,
            "receipts",
            "'repetitive_log'",
            "'unregistered_strategy'",
        )
    elif damage == "event_created_at_nullable":
        rewrite_table_sql(
            path,
            "governance_events",
            "created_at TEXT NOT NULL",
            "created_at BLOB",
        )
    elif damage == "event_enum_check":
        rewrite_table_sql(
            path,
            "governance_events",
            "'claude_hook'",
            "'unregistered_source'",
        )
    elif damage == "event_token_check":
        rewrite_table_sql(
            path,
            "governance_events",
            "token_count >= 0",
            "token_count >= -1",
        )
    elif damage == "partial_index":
        with sqlite3.connect(path) as conn:
            conn.execute("DROP INDEX idx_events_receipt_id")
            conn.execute(
                "CREATE INDEX idx_events_receipt_id "
                "ON governance_events(receipt_id) WHERE risk = 'high'"
            )
    else:
        with sqlite3.connect(path) as conn:
            conn.execute("DROP INDEX idx_events_receipt_id")
            conn.execute(
                "CREATE UNIQUE INDEX idx_events_receipt_id "
                "ON governance_events(receipt_id)"
            )

    with pytest.raises(RuntimeError, match="^Invalid ledger schema v2$"):
        ContextLedger(path)


def test_real_v1_fixture_migrates_transactionally_and_remains_retrievable(tmp_path):
    path = tmp_path / "ledger.sqlite"
    materialize_v1(path)

    ledger = ContextLedger(path)

    assert ledger.retrieve_original("tgl_legacy_fixture") == "synthetic legacy payload"
    explanation = ledger.explain_receipt("tgl_legacy_fixture")
    assert explanation["is_legacy"] is True
    assert explanation["delivery_state"] is None
    assert ledger.stats()["legacy"]["receipt_count"] == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_failed_v1_migration_rolls_back_all_logical_changes(tmp_path):
    path = tmp_path / "ledger.sqlite"
    materialize_v1(path)
    before = logical_snapshot(path)

    class FailingLedger(ContextLedger):
        def _migration_checkpoint(self) -> None:
            raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        FailingLedger(path)

    assert logical_snapshot(path) == before


def test_migrated_database_enforces_v2_transformed_only_receipt_constraint(tmp_path):
    path = tmp_path / "ledger.sqlite"
    materialize_v1(path)
    ContextLedger(path)

    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO receipts (
                    receipt_id, source, content_type, action, risk,
                    original_text, governed_text, original_hash,
                    token_before, token_after, policy, notes, created_at,
                    delivery_state, is_legacy
                ) VALUES (
                    'tgl_invalid_v2', 'sql', 'text', 'passthrough', 'low',
                    'raw', 'raw',
                    'd7439bee24773b29a0f6e47b3f11a3c17e322fc2f1f69c9b5e06ce414b5d4c48',
                    1, 1, 'passthrough', '[]', '2026-01-01T00:00:00Z',
                    'emitted', 0
                )
                """
            )


def test_prepared_receipt_requires_verified_transformation_and_becomes_emitted(tmp_path):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    receipt_id = ledger.allocate_receipt_id()
    result = make_result(receipt_id)

    ledger.store_prepared(receipt_id, "original output", result)
    prepared = ledger.stats()
    assert prepared["emitted"]["receipt_count"] == 0
    assert prepared["prepared"]["receipt_count"] == 1
    assert prepared["prepared"]["candidate_tokens_saved"] == 15
    assert ledger.retrieve_original(receipt_id) == "original output"

    ledger.mark_emitted(receipt_id)
    emitted = ledger.stats()
    assert emitted["emitted"]["receipt_count"] == 1
    assert emitted["emitted"]["candidate_tokens_saved"] == 15
    assert emitted["prepared"]["receipt_count"] == 0
    assert ledger.savings()["receipt_count"] == 1
    assert ledger.savings()["tokens_saved"] == 15


def test_store_prepared_rejects_receipt_id_mismatch(tmp_path):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    result = make_result("tgl_expected")

    with pytest.raises(ValueError, match="receipt_id"):
        ledger.store_prepared("tgl_other", "original", result)

    assert ledger.stats()["prepared"]["receipt_count"] == 0


def test_retrieve_detects_hash_tampering_without_echoing_payload(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = ContextLedger(path)
    receipt_id = ledger.allocate_receipt_id()
    ledger.store_prepared(receipt_id, "private original", make_result(receipt_id))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE receipts SET original_text = ? WHERE receipt_id = ?",
            ("tampered private original", receipt_id),
        )

    with pytest.raises(ledger_module.LedgerIntegrityError) as captured:
        ledger.retrieve_original(receipt_id)

    assert captured.value.code == "ledger_integrity_failed"
    assert str(captured.value) == "Receipt integrity check failed"
    assert "private" not in str(captured.value)


def test_governance_events_accept_only_closed_typed_metadata(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = ContextLedger(path)
    ledger.record_event(
        SourceKind.CLAUDE_HOOK,
        Action.PASSTHROUGH,
        Risk.HIGH,
        ReasonCode.SECRET_DETECTED,
        12,
        None,
    )

    with pytest.raises(TypeError, match="source_kind"):
        ledger.record_event(
            "claude_hook",  # type: ignore[arg-type]
            Action.PASSTHROUGH,
            Risk.HIGH,
            ReasonCode.SECRET_DETECTED,
            12,
            None,
        )
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO governance_events "
                "(created_at, source_kind, action, risk, reason_code, token_count, receipt_id) "
                "VALUES ('2026-01-01T00:00:00Z', 'invalid', 'passthrough', "
                "'low', 'transformed', 1, NULL)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO governance_events "
                "(created_at, source_kind, action, risk, reason_code, token_count, receipt_id) "
                "VALUES ('2026-01-01T00:00:00Z', 'cli', 'passthrough', "
                "'low', 'no_matching_strategy', -1, NULL)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO governance_events "
                "(created_at, source_kind, action, risk, reason_code, token_count, receipt_id) "
                "VALUES ('2026-01-01T00:00:00Z', 'cli', 'transform', "
                "'low', 'transformed', 1, 'tgl_missing')"
            )

    risks = ledger.risks()
    assert risks == [
        {
            "receipt_id": None,
            "source_kind": "claude_hook",
            "action": "passthrough",
            "risk": "high",
            "reason_code": "secret_detected",
            "token_count": 12,
            "created_at": risks[0]["created_at"],
        }
    ]


def test_prune_removes_expired_receipts_and_linked_or_unlinked_events(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = ContextLedger(path)
    old_id = ledger.allocate_receipt_id()
    fresh_id = ledger.allocate_receipt_id()
    ledger.store_prepared(old_id, "old original", make_result(old_id))
    ledger.store_prepared(fresh_id, "fresh original", make_result(fresh_id))
    ledger.record_event(
        SourceKind.CLI,
        Action.TRANSFORM,
        Risk.LOW,
        ReasonCode.TRANSFORMED,
        20,
        old_id,
    )
    ledger.record_event(
        SourceKind.CLI,
        Action.PASSTHROUGH,
        Risk.MEDIUM,
        ReasonCode.NO_MATCHING_STRATEGY,
        10,
        None,
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE receipts SET created_at = '2025-01-01T00:00:00Z' WHERE receipt_id = ?",
            (old_id,),
        )
        conn.execute(
            "UPDATE governance_events SET created_at = '2025-01-01T00:00:00Z'"
        )

    report = ledger.prune(cutoff=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert report == {"receipts_deleted": 1, "events_deleted": 2}
    with pytest.raises(KeyError):
        ledger.retrieve_original(old_id)
    assert ledger.retrieve_original(fresh_id) == "fresh original"


def test_prune_can_derive_utc_cutoff_from_retention_days(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = ContextLedger(path)
    receipt_id = ledger.allocate_receipt_id()
    ledger.store_prepared(receipt_id, "old original", make_result(receipt_id))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE receipts SET created_at = '2026-01-01T00:00:00Z' WHERE receipt_id = ?",
            (receipt_id,),
        )

    report = ledger.prune(
        retention_days=30,
        now=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    assert report["receipts_deleted"] == 1


def test_purge_legacy_requires_explicit_confirmation_and_preserves_v2(tmp_path):
    path = tmp_path / "ledger.sqlite"
    materialize_v1(path)
    ledger = ContextLedger(path)
    current_id = ledger.allocate_receipt_id()
    ledger.store_prepared(current_id, "current", make_result(current_id))
    ledger.record_event(
        SourceKind.CLI,
        Action.TRANSFORM,
        Risk.LOW,
        ReasonCode.TRANSFORMED,
        24,
        "tgl_legacy_fixture",
    )

    with pytest.raises(ValueError, match="explicit confirmation required"):
        ledger.purge_legacy()

    assert ledger.purge_legacy(confirm=True) == {
        "receipts_deleted": 1,
        "events_deleted": 1,
    }
    assert ledger.retrieve_original(current_id) == "current"


def test_record_legacy_write_is_disabled_without_adding_a_row(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = ContextLedger(path)

    with pytest.raises(
        ledger_module.LegacyWriteDisabledError,
        match="^Legacy ledger writes are disabled; use store_prepared$",
    ):
        ledger.record(
            source="compatibility_test",
            content_type="log",
            action="summarize",
            risk="low",
            original_text="legacy original",
            governed_text="legacy governed",
            token_before=10,
            token_after=2,
            policy="balanced",
            notes=["synthetic"],
        )

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_savings_top_level_counts_only_emitted_transforms_with_legacy_separate(tmp_path):
    path = tmp_path / "ledger.sqlite"
    materialize_v1(path)
    ledger = ContextLedger(path)
    emitted_id = ledger.allocate_receipt_id()
    prepared_id = ledger.allocate_receipt_id()
    ledger.store_prepared(emitted_id, "emitted original", make_result(emitted_id))
    ledger.mark_emitted(emitted_id)
    ledger.store_prepared(prepared_id, "prepared original", make_result(prepared_id))

    savings = ledger.savings()

    assert savings["scope"] == "emitted_transforms"
    assert savings["receipt_count"] == 1
    assert savings["token_before"] == 20
    assert savings["token_after"] == 5
    assert savings["tokens_saved"] == 15
    assert savings["prepared"]["receipt_count"] == 1
    assert savings["legacy"]["receipt_count"] == 1
    assert savings["warnings"] == ["legacy_receipts_present"]


def test_busy_timeout_is_bounded_without_retry_sleep(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = ContextLedger(path, busy_timeout_ms=50)
    blocker = sqlite3.connect(path, timeout=0)
    blocker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            ledger.record_event(
                SourceKind.CLI,
                Action.PASSTHROUGH,
                Risk.LOW,
                ReasonCode.NO_MATCHING_STRATEGY,
                1,
                None,
            )
    finally:
        blocker.rollback()
        blocker.close()
    assert time.monotonic() - started < 1.0


def test_busy_timeout_rejects_values_above_thirty_seconds(tmp_path):
    with pytest.raises(TypeError, match="busy_timeout_ms"):
        ContextLedger(tmp_path / "ledger.sqlite", busy_timeout_ms=30_001)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL policy")
def test_windows_security_policy_covers_database_and_sidecar_paths(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    secured: list[Path] = []

    def fake_secure(candidate: Path) -> None:
        secured.append(candidate)
        candidate.touch(exist_ok=True)

    monkeypatch.setattr(ledger_module, "_ensure_windows_private_file", fake_secure)

    ContextLedger(path)

    assert {
        path,
        Path(f"{path}-journal"),
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    } <= set(secured)


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL policy")
def test_windows_security_failure_is_fixed_and_stops_initialization(tmp_path, monkeypatch):
    def fail_security(candidate: Path) -> None:
        raise PermissionError("localized path details")

    monkeypatch.setattr(ledger_module, "_ensure_windows_private_file", fail_security)

    with pytest.raises(ledger_module.LedgerSecurityError) as captured:
        ContextLedger(tmp_path / "ledger.sqlite")

    assert str(captured.value) == "Unable to secure ledger storage"


def install_windows_handle_acl_mocks(
    monkeypatch,
    *,
    handle: int = 123,
    set_security_result: int = 0,
    file_attributes: int = 0,
    parent_attributes: int = 0,
):
    import ctypes
    from ctypes import wintypes

    events: list[tuple[str, int | None]] = []

    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    def convert_sddl(sddl, revision, descriptor, size):
        ctypes.cast(descriptor, ctypes.POINTER(ctypes.c_void_p))[0] = 1001
        events.append(("convert", None))
        return 1

    def create_file(*args):
        events.append(("create_flags", int(args[5])))
        events.append(("create", handle))
        return handle

    def get_file_attributes(path):
        events.append(("parent_attributes", parent_attributes))
        return parent_attributes

    def get_file_information(handle_value, info_class, info, size):
        attributes = ctypes.cast(
            info,
            ctypes.POINTER(wintypes.DWORD),
        )
        attributes[0] = file_attributes
        attributes[1] = 0
        events.append(("file_attributes", file_attributes))
        return 1

    def get_dacl(descriptor, present, dacl, defaulted):
        ctypes.cast(present, ctypes.POINTER(wintypes.BOOL))[0] = 1
        ctypes.cast(dacl, ctypes.POINTER(ctypes.c_void_p))[0] = 1002
        ctypes.cast(defaulted, ctypes.POINTER(wintypes.BOOL))[0] = 0
        events.append(("get_dacl", None))
        return 1

    def set_security(handle_value, *args):
        numeric_handle = getattr(handle_value, "value", handle_value)
        events.append(("set_security", int(numeric_handle)))
        return set_security_result

    def close_handle(handle_value):
        numeric_handle = getattr(handle_value, "value", handle_value)
        events.append(("close", int(numeric_handle)))
        return 1

    def local_free(pointer):
        events.append(("free", None))
        return None

    kernel32 = SimpleNamespace(
        CreateFileW=Function(create_file),
        CloseHandle=Function(close_handle),
        LocalFree=Function(local_free),
        GetFileAttributesW=Function(get_file_attributes),
        GetFileInformationByHandleEx=Function(get_file_information),
    )
    advapi32 = SimpleNamespace(
        ConvertStringSecurityDescriptorToSecurityDescriptorW=Function(convert_sddl),
        GetSecurityDescriptorDacl=Function(get_dacl),
        SetSecurityInfo=Function(set_security),
    )
    monkeypatch.setattr(ledger_module, "_windows_current_user_sid", lambda: "S-1-5-21-1")
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, use_last_error=True: kernel32 if name == "kernel32" else advapi32,
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32))
    return events


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL policy")
def test_windows_acl_is_applied_to_open_handle_before_close(tmp_path, monkeypatch):
    events = install_windows_handle_acl_mocks(monkeypatch)

    try:
        ledger_module._ensure_windows_private_file(tmp_path / "ledger.sqlite")
    except ledger_module.LedgerSecurityError:
        pass

    names = [name for name, _ in events]
    assert ("create_flags", 0x00000080 | 0x00200000) in events
    assert "file_attributes" in names
    assert names.index("set_security") < names.index("close")


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL policy")
def test_windows_acl_error_still_closes_valid_handle(tmp_path, monkeypatch):
    events = install_windows_handle_acl_mocks(
        monkeypatch,
        set_security_result=5,
    )

    with pytest.raises(ledger_module.LedgerSecurityError):
        ledger_module._ensure_windows_private_file(tmp_path / "ledger.sqlite")

    assert ("set_security", 123) in events
    assert ("close", 123) in events
    assert events.index(("set_security", 123)) < events.index(("close", 123))


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL policy")
def test_windows_invalid_handle_is_never_closed(tmp_path, monkeypatch):
    from ctypes import wintypes

    invalid = wintypes.HANDLE(-1).value
    events = install_windows_handle_acl_mocks(monkeypatch, handle=invalid)

    with pytest.raises(ledger_module.LedgerSecurityError):
        ledger_module._ensure_windows_private_file(tmp_path / "ledger.sqlite")

    assert not any(name == "close" for name, _ in events)


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point policy")
@pytest.mark.parametrize("attributes", [0x00000400, 0x00000010])
def test_windows_reparse_or_nonregular_handle_is_rejected(
    tmp_path, monkeypatch, attributes
):
    events = install_windows_handle_acl_mocks(
        monkeypatch,
        file_attributes=attributes,
    )

    with pytest.raises(ledger_module.LedgerSecurityError):
        ledger_module._ensure_windows_private_file(tmp_path / "ledger.sqlite")

    assert ("close", 123) in events
    assert not any(name == "set_security" for name, _ in events)


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point policy")
def test_windows_reparse_parent_component_is_rejected_before_open(tmp_path, monkeypatch):
    events = install_windows_handle_acl_mocks(
        monkeypatch,
        parent_attributes=0x00000400,
    )

    with pytest.raises(ledger_module.LedgerSecurityError):
        ledger_module._ensure_windows_private_file(tmp_path / "ledger.sqlite")

    assert not any(name == "create" for name, _ in events)


def test_every_connect_secures_database_and_sidecars_before_sqlite_open(
    tmp_path, monkeypatch
):
    path = tmp_path / "ledger.sqlite"
    events: list[tuple[str, Path | None]] = []
    real_connect = sqlite3.connect

    def secure(candidate: Path) -> None:
        events.append(("secure", candidate))
        candidate.touch(exist_ok=True)

    def connect(*args, **kwargs):
        events.append(("connect", None))
        return real_connect(*args, **kwargs)

    secure_name = (
        "_ensure_windows_private_file"
        if os.name == "nt"
        else "_ensure_posix_private_file"
    )
    monkeypatch.setattr(ledger_module, secure_name, secure)
    monkeypatch.setattr(ledger_module.sqlite3, "connect", connect)
    ledger = ContextLedger(path)
    events.clear()

    with ledger._connect():
        pass

    expected = list(ledger._storage_paths())
    assert events[:4] == [("secure", candidate) for candidate in expected]
    assert events[4] == ("connect", None)


def test_post_connect_security_failure_closes_connection_and_is_fixed(
    tmp_path, monkeypatch
):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    real_connect = sqlite3.connect
    tracked = None

    class TrackingConnection:
        def __init__(self):
            self.inner = real_connect(":memory:")
            self.closed = False

        @property
        def row_factory(self):
            return self.inner.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self.inner.row_factory = value

        def execute(self, *args, **kwargs):
            return self.inner.execute(*args, **kwargs)

        def close(self):
            self.closed = True
            self.inner.close()

    def connect(*args, **kwargs):
        nonlocal tracked
        tracked = TrackingConnection()
        return tracked

    def fail_security():
        raise PermissionError("host-specific detail")

    monkeypatch.setattr(ledger_module.sqlite3, "connect", connect)
    monkeypatch.setattr(ledger, "_secure_existing_storage", fail_security)

    with pytest.raises(ledger_module.LedgerSecurityError) as captured:
        ledger._connect()

    assert str(captured.value) == "Unable to secure ledger storage"
    assert tracked is not None and tracked.closed is True


@pytest.mark.parametrize("failure_stage", ["row_factory", "schema_version"])
def test_connect_initialization_failure_closes_and_preserves_original_exception(
    tmp_path, monkeypatch, failure_stage
):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")

    class InitializationConnection:
        def __init__(self):
            self.closed = False
            self.close_attempted = False
            self._row_factory = None

        @property
        def row_factory(self):
            return self._row_factory

        @row_factory.setter
        def row_factory(self, value):
            if failure_stage == "row_factory":
                raise KeyboardInterrupt("row factory setup failed")
            self._row_factory = value

        def execute(self, sql):
            if failure_stage == "schema_version" and sql == "PRAGMA schema_version":
                raise RuntimeError("schema pragma setup failed")
            return self

        def fetchone(self):
            return (0,)

        def close(self):
            self.close_attempted = True
            self.closed = True
            if failure_stage == "schema_version":
                raise OSError("close failure must not mask setup failure")

    connection = InitializationConnection()
    monkeypatch.setattr(ledger_module.sqlite3, "connect", lambda *args, **kwargs: connection)

    if failure_stage == "row_factory":
        expected = pytest.raises(KeyboardInterrupt, match="row factory setup failed")
    else:
        expected = pytest.raises(RuntimeError, match="schema pragma setup failed")
    with expected:
        ledger._connect()

    assert connection.close_attempted is True
    assert connection.closed is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_posix_database_and_active_sidecars_are_private(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = ContextLedger(path)

    with ledger._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = [
            candidate
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
            if candidate.exists()
        ]
        assert len(active) >= 2
        assert all(candidate.stat().st_mode & 0o777 == 0o600 for candidate in active)
        conn.execute("ROLLBACK")
    journal = Path(f"{path}-journal")
    ledger_module._ensure_posix_private_file(journal)
    assert journal.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX file types")
def test_posix_symlink_and_fifo_storage_are_rejected(tmp_path):
    target = tmp_path / "target"
    target.touch()
    symlink = tmp_path / "ledger-link.sqlite"
    symlink.symlink_to(target)
    fifo = tmp_path / "ledger-fifo.sqlite"
    os.mkfifo(fifo)

    for candidate in (symlink, fifo):
        with pytest.raises(ledger_module.LedgerSecurityError):
            ledger_module._ensure_posix_private_file(candidate)


@pytest.mark.skipif(os.name != "posix", reason="POSIX parent symlinks")
def test_posix_parent_symlink_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ledger_module.LedgerSecurityError):
        ledger_module._ensure_posix_private_file(linked / "ledger.sqlite")


class NonClosingConnection:
    def __init__(self, inner):
        self.inner = inner
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self.inner.__exit__(exc_type, exc, traceback)

    def close(self):
        self.closed = True
        self.inner.close()


def test_connection_context_closes_on_success_and_exception(tmp_path, monkeypatch):
    ledger = ContextLedger(tmp_path / "ledger.sqlite")
    connections: list[NonClosingConnection] = []

    def connect():
        wrapper = NonClosingConnection(sqlite3.connect(":memory:"))
        connections.append(wrapper)
        return wrapper

    monkeypatch.setattr(ledger, "_connect", connect)

    assert hasattr(ledger, "_connection")
    with ledger._connection() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="injected body failure"):
        with ledger._connection():
            raise RuntimeError("injected body failure")

    assert len(connections) == 2
    assert all(connection.closed for connection in connections)


def test_migration_failure_explicitly_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    materialize_v1(path)
    connections: list[NonClosingConnection] = []

    class FailingLedger(ContextLedger):
        def _migration_checkpoint(self) -> None:
            raise RuntimeError("injected migration failure")

    def connect(self):
        inner = sqlite3.connect(path, isolation_level=None)
        inner.row_factory = sqlite3.Row
        inner.execute("PRAGMA foreign_keys = ON")
        wrapper = NonClosingConnection(inner)
        connections.append(wrapper)
        return wrapper

    monkeypatch.setattr(FailingLedger, "_connect", connect)

    with pytest.raises(RuntimeError, match="injected migration failure"):
        FailingLedger(path)

    assert connections and all(connection.closed for connection in connections)


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-open-file semantics")
def test_windows_ledger_file_is_deletable_after_operations(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = ContextLedger(path)
    ledger.stats()
    ledger.risks()

    path.unlink()
    assert not path.exists()


def test_concurrent_thread_and_process_writes_finish_within_bound(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ContextLedger(path, busy_timeout_ms=2000)

    def thread_write(index: int) -> None:
        process_record_event(str(path), index)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(thread_write, range(8)))

    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=process_record_event, args=(str(path), index))
        for index in range(8, 12)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=5)
        assert not process.is_alive()
        assert process.exitcode == 0

    assert time.monotonic() - started < 10
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM governance_events").fetchone()[0] == 12
