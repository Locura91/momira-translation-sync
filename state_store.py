"""
state_store.py — tracks what's already been translated, so re-running the
tool is safe (idempotent) and so the future "autonomous scan" mode (Mode B)
can tell new/changed entities apart from already-up-to-date ones.

Uses SQLite (stdlib, no install needed) in a single local file:
nbext_state.db — keep this file around; deleting it just means everything
looks "new" again on the next run.
"""

import sqlite3
import json
import hashlib
from contextlib import contextmanager
from typing import Optional, Dict, Any, List

DB_PATH = "nbext_state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS translation_state (
    entity_type TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    option_code TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL,
    translated_languages TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    PRIMARY KEY (entity_type, supplier_id, entity_id, option_code)
);
"""


@contextmanager
def _connect(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def compute_hash(fields: Dict[str, str]) -> str:
    """Deterministic hash of the English source fields, used to detect edits."""
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StateStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        with _connect(self.db_path):
            pass  # just ensures the table exists

    def get_state(self, entity_type: str, supplier_id: str, entity_id: str, option_code: str = "") -> Optional[Dict[str, Any]]:
        with _connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM translation_state WHERE entity_type=? AND supplier_id=? AND entity_id=? AND option_code=?",
                (entity_type, str(supplier_id), str(entity_id), option_code),
            ).fetchone()
            if not row:
                return None
            return {
                "source_hash": row["source_hash"],
                "translated_languages": json.loads(row["translated_languages"]),
                "last_synced_at": row["last_synced_at"],
            }

    def upsert_state(
        self,
        entity_type: str,
        supplier_id: str,
        entity_id: str,
        source_hash: str,
        translated_languages: List[str],
        option_code: str = "",
    ):
        with _connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO translation_state
                    (entity_type, supplier_id, entity_id, option_code, source_hash, translated_languages, last_synced_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(entity_type, supplier_id, entity_id, option_code)
                DO UPDATE SET source_hash=excluded.source_hash,
                              translated_languages=excluded.translated_languages,
                              last_synced_at=excluded.last_synced_at
                """,
                (entity_type, str(supplier_id), str(entity_id), option_code, source_hash, json.dumps(sorted(translated_languages))),
            )

    def languages_needed(self, entity_type, supplier_id, entity_id, source_hash, target_languages, option_code=""):
        """
        Returns the subset of target_languages that still need translating:
        - all of them, if this entity has never been synced, or its English
          source changed since the last sync (source_hash differs)
        - just the missing ones, if the source is unchanged but the target
          language list grew since the last run
        - [] if everything is already up to date
        """
        state = self.get_state(entity_type, supplier_id, entity_id, option_code)
        if state is None or state["source_hash"] != source_hash:
            return list(target_languages)
        already_done = set(state["translated_languages"])
        return [lang for lang in target_languages if lang not in already_done]
