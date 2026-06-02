"""
Session history store backed by SQLite.

Stores every dictation in ~/.bolo/sessions.db with:
- raw transcript (what STT returned)
- final text (after corrections/cleanup)
- context (app name + surrounding text when dictating)
- timing and source info
- success/error status

Provides:
- persist/query from the running app
- CLI for browsing/searching/exporting history
"""

import datetime
import json
import os
import sqlite3
import sys
import time


DB_PATH = os.path.expanduser("~/.bolo/sessions.db")
SCHEMA_VERSION = 1


class SessionStore:
    """Persist dictation sessions to SQLite."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._ensure_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_db(self):
        """Create schema if it does not exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    raw_text TEXT NOT NULL DEFAULT '',
                    final_text TEXT NOT NULL DEFAULT '',
                    word_count INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER,
                    latency_ms INTEGER,
                    stt_provider TEXT,
                    source_app TEXT,
                    context_text TEXT,
                    success INTEGER NOT NULL DEFAULT 1,
                    error_msg TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_timestamp
                ON sessions(timestamp DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                )
            """)
            existing = conn.execute(
                "SELECT version FROM schema_version LIMIT 1").fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (SCHEMA_VERSION,))
            conn.commit()
        finally:
            conn.close()

    def persist(self, *, raw_text: str, final_text: str, word_count: int = 0,
                duration_ms: int = None, latency_ms: int = None,
                stt_provider: str = None, source_app: str = "",
                context_text: str = "", success: bool = True, error_msg: str = ""):
        """Write a completed dictation session."""
        conn = self._connect()
        try:
            conn.execute("""
                INSERT INTO sessions
                    (timestamp, raw_text, final_text, word_count,
                     duration_ms, latency_ms, stt_provider,
                     source_app, context_text, success, error_msg)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.datetime.utcnow().isoformat() + "Z",
                raw_text,
                final_text,
                word_count,
                duration_ms,
                latency_ms,
                stt_provider,
                source_app,
                context_text[:2000] if context_text else "",
                1 if success else 0,
                error_msg[:500] if error_msg else "",
            ))
            conn.commit()
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        finally:
            conn.close()

    def recent(self, limit: int = 20, offset: int = 0) -> list:
        """Get most recent sessions."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def search(self, query: str, limit: int = 20) -> list:
        """Full-text search across raw_text and final_text."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM sessions
                   WHERE raw_text LIKE ? OR final_text LIKE ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (f"%{query}%", f"%{query}%", limit)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def stats(self) -> dict:
        """Aggregate stats over all sessions."""
        conn = self._connect()
        try:
            total = conn.execute(
                "SELECT COUNT(*) as cnt FROM sessions").fetchone()["cnt"]
            if not total:
                return {"total_sessions": 0}
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(word_count) as total_words,
                    AVG(word_count) as avg_words,
                    AVG(latency_ms) as avg_latency_ms,
                    MAX(timestamp) as last_used,
                    MIN(timestamp) as first_used
                FROM sessions
            """).fetchone()
            return dict(row)
        finally:
            conn.close()

    def delete_older_than(self, days: int) -> int:
        """Delete sessions older than N days. Returns count deleted."""
        conn = self._connect()
        try:
            cutoff = (datetime.datetime.utcnow() -
                      datetime.timedelta(days=days)).isoformat() + "Z"
            cur = conn.execute(
                "DELETE FROM sessions WHERE timestamp < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _format_session(s: dict) -> str:
    ts = s["timestamp"][:19].replace("T", " ")
    source = s["source_app"] or "?"
    text = s["final_text"] or s["raw_text"] or "(empty)"
    if len(text) > 80:
        text = text[:77] + "..."
    status = "✓" if s["success"] else "✗"
    return f"{ts}  {status}  [{source}]  {text}"


def cli():
    """Entry point for `python3 session_store.py <command>`."""
    if len(sys.argv) < 2:
        print("Usage: python3 session_store.py <recent|search|stats|clean>")
        sys.exit(1)

    cmd = sys.argv[1]
    store = SessionStore()

    if cmd == "recent":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        for s in store.recent(limit=limit):
            print(_format_session(s))

    elif cmd == "search":
        query = " ".join(sys.argv[2:])
        if not query:
            print("Usage: python3 session_store.py search <term>")
            sys.exit(1)
        results = store.search(query)
        if not results:
            print("No results found.")
        for s in results:
            print(_format_session(s))

    elif cmd == "stats":
        st = store.stats()
        if st.get("total_sessions", 0) == 0:
            print("No sessions yet.")
        else:
            print(f"Total sessions:   {st['total']}")
            print(f"Total words:      {st['total_words']}")
            print(f"Avg words/session: {st['avg_words']:.1f}")
            print(f"Avg latency:      {st['avg_latency_ms']:.0f}ms")
            print(f"Last used:        {st['last_used'][:19]}")
            print(f"First used:       {st['first_used'][:19]}")

    elif cmd == "clean":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
        deleted = store.delete_older_than(days)
        print(f"Deleted {deleted} sessions older than {days} days.")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
