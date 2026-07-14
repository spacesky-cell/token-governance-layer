CREATE TABLE receipts (
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
);

INSERT INTO receipts (
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
    notes,
    created_at
) VALUES (
    'tgl_legacy_fixture',
    'synthetic_fixture',
    'log',
    'summarize',
    'low',
    'synthetic legacy payload',
    'synthetic governed payload',
    '808cd2cb440f987c57d354c3043ef5d9d77566654271aa61afefaf356e7c12b6',
    24,
    8,
    'balanced',
    '["synthetic fixture only"]',
    '2025-01-01 00:00:00'
);
