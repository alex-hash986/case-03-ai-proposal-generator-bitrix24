import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Conflict(ValueError):
    pass


class MissingCycle(LookupError):
    pass


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise RuntimeError("SQLite WAL unavailable")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cycles (
                    key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                    payload TEXT NOT NULL, result TEXT
                );
                CREATE TABLE IF NOT EXISTS mock_operations (
                    key TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (key, operation)
                );
                CREATE TABLE IF NOT EXISTS bitrix_steps (
                    key TEXT NOT NULL, scope TEXT NOT NULL, step TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('started', 'done', 'retry')),
                    result TEXT,
                    PRIMARY KEY (key, step)
                );
                CREATE TABLE IF NOT EXISTS telemetry (
                    request_id TEXT PRIMARY KEY, processing_time_sec REAL NOT NULL,
                    tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL,
                    cost_rub TEXT NOT NULL, cost_complete INTEGER NOT NULL,
                    ab_variant TEXT NOT NULL, mode TEXT NOT NULL, channel TEXT NOT NULL,
                    is_after_hours INTEGER NOT NULL, need_human INTEGER NOT NULL,
                    saved_minutes REAL, outcome TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover_interrupted_cycles(self) -> None:
        # The caller holds the publisher lock for the lifetime of the service.
        with self.connection() as db:
            db.execute("DELETE FROM cycles WHERE state='pending' AND result IS NULL")
            db.execute("UPDATE cycles SET state='reconcile' WHERE state='pending'")

    def claim(self, key: str, fingerprint: str, payload: str) -> str | None:
        with self.connection() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO cycles VALUES (?, ?, 'pending', ?, NULL)",
                (key, fingerprint, payload),
            )
            if inserted.rowcount:
                return None
            row = db.execute("SELECT * FROM cycles WHERE key=?", (key,)).fetchone()
            if row["fingerprint"] != fingerprint:
                raise Conflict("Ключ уже привязан к другому контексту")
            if row["state"] == "pending":
                raise Conflict("Цикл уже выполняется")
            return str(row["result"])

    def checkpoint(self, key: str, result: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE cycles SET result=? WHERE key=? AND state='pending'", (result, key),
            )

    def finish(
        self, key: str, state: str, result: str, telemetry: tuple[Any, ...],
    ) -> None:
        with self.connection() as db:
            db.execute("UPDATE cycles SET state=?, result=? WHERE key=?", (state, result, key))
            self._log(db, telemetry)

    def fail(self, key: str, telemetry: tuple[Any, ...], *, retryable: bool) -> None:
        with self.connection() as db:
            if retryable:
                db.execute("DELETE FROM cycles WHERE key=? AND result IS NULL", (key,))
            self._log(db, telemetry)

    @staticmethod
    def _log(db: sqlite3.Connection, telemetry: tuple[Any, ...]) -> None:
        db.execute("INSERT INTO telemetry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", telemetry)

    def claim_reconcile(self, key: str) -> tuple[str, str, bool]:
        with self.connection() as db:
            claimed = db.execute(
                "UPDATE cycles SET state='pending' WHERE key=? AND state='reconcile'", (key,),
            ).rowcount
            row = db.execute("SELECT * FROM cycles WHERE key=?", (key,)).fetchone()
            if row is None:
                raise MissingCycle(key)
            if not claimed and row["state"] == "pending":
                raise Conflict("Цикл уже выполняется")
            return str(row["payload"]), str(row["result"]), bool(claimed)

    def mock_operation(self, key: str, operation: str, payload: dict[str, Any]) -> bool:
        with self.connection() as db:
            return bool(db.execute(
                "INSERT OR IGNORE INTO mock_operations VALUES (?, ?, ?)",
                (key, operation, json.dumps(payload, ensure_ascii=False)),
            ).rowcount)

    def bitrix_state(self, key: str, scope: str, step: str) -> tuple[str, Any]:
        with self.connection() as db:
            existing = db.execute(
                "SELECT scope FROM bitrix_steps WHERE key=? LIMIT 1", (key,),
            ).fetchone()
            if existing and existing["scope"] != scope:
                raise Conflict("Цикл привязан к другому порталу Bitrix24")
            row = db.execute(
                "SELECT state, result FROM bitrix_steps WHERE key=? AND step=?", (key, step),
            ).fetchone()
        return (str(row["state"]), json.loads(row["result"])) if row else ("new", None)

    def bitrix_save(self, key: str, scope: str, step: str, state: str, result: Any = None) -> None:
        with self.connection() as db:
            db.execute("""
                INSERT INTO bitrix_steps(key, scope, step, state, result) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key, step) DO UPDATE SET state=excluded.state, result=excluded.result
                """, (key, scope, step, state, json.dumps(result, ensure_ascii=False)))

    def metrics(self) -> dict[str, Any]:
        with self.connection() as db:
            rows = db.execute("""
                SELECT mode, COUNT(*) AS attempts, AVG(processing_time_sec) AS avg_cycle_seconds,
                    SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out,
                    SUM(CAST(cost_rub AS REAL)) AS cost_rub, SUM(need_human) AS escalations,
                    SUM(saved_minutes) AS saved_minutes, SUM(1-cost_complete) AS unpriced_attempts
                FROM telemetry GROUP BY mode ORDER BY mode
            """).fetchall()
        return {"by_mode": [dict(row) for row in rows]}
