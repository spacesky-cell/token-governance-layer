from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any


class ContextLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    action TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    original_text TEXT NOT NULL,
                    governed_text TEXT NOT NULL,
                    original_hash TEXT NOT NULL,
                    token_before INTEGER NOT NULL,
                    token_after INTEGER NOT NULL,
                    policy TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

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
        receipt_id = f"tgl_{uuid.uuid4().hex}"
        original_hash = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO receipts (
                    receipt_id, source, content_type, action, risk,
                    original_text, governed_text, original_hash,
                    token_before, token_after, policy, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    source,
                    content_type,
                    action,
                    risk,
                    original_text,
                    governed_text,
                    original_hash,
                    token_before,
                    token_after,
                    policy,
                    json.dumps(notes, ensure_ascii=False),
                ),
            )
        return receipt_id

    def retrieve_original(self, receipt_id: str) -> str:
        row = self._get(receipt_id)
        return str(row["original_text"])

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
        }

    def savings(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS receipt_count,
                    COALESCE(SUM(token_before), 0) AS token_before,
                    COALESCE(SUM(token_after), 0) AS token_after
                FROM receipts
                """
            ).fetchone()
            by_type = conn.execute(
                """
                SELECT
                    content_type,
                    COUNT(*) AS receipt_count,
                    COALESCE(SUM(token_before), 0) AS token_before,
                    COALESCE(SUM(token_after), 0) AS token_after
                FROM receipts
                GROUP BY content_type
                ORDER BY token_before DESC
                """
            ).fetchall()

        total_before = int(row["token_before"])
        total_after = int(row["token_after"])
        return {
            "receipt_count": int(row["receipt_count"]),
            "token_before": total_before,
            "token_after": total_after,
            "tokens_saved": total_before - total_after,
            "by_content_type": [
                {
                    "content_type": item["content_type"],
                    "receipt_count": int(item["receipt_count"]),
                    "token_before": int(item["token_before"]),
                    "token_after": int(item["token_after"]),
                    "tokens_saved": int(item["token_before"]) - int(item["token_after"]),
                }
                for item in by_type
            ],
        }

    def risks(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT receipt_id, source, content_type, action, risk, notes, created_at
                FROM receipts
                WHERE risk != 'low'
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [
            {
                "receipt_id": row["receipt_id"],
                "source": row["source"],
                "content_type": row["content_type"],
                "action": row["action"],
                "risk": row["risk"],
                "notes": json.loads(row["notes"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _get(self, receipt_id: str) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Receipt not found: {receipt_id}")
        return row
