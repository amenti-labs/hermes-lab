"""SQLite-backed shared blackboard for swarm coordination.

Minimal coordination layer: trials, claims, and a text summary generator.
Designed to work on exFAT (single file, no complex locking).
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return time.time()


@dataclass
class TrialRecord:
    """A single trial in the blackboard."""
    id: int
    experiment: str
    strategy: str
    params: dict[str, Any]
    score: float | None
    accepted: bool
    status: str  # pending, running, completed, failed
    reasoning: str
    metadata: dict[str, Any]
    created_at: str
    finished_at: str | None
    parent_id: int | None  # for tree/lineage tracking


@dataclass
class Claim:
    """A short-lived lock to prevent duplicate work."""
    id: int
    experiment: str
    worker: str
    description: str
    created_at: str
    expires_at: str


class Blackboard:
    """SQLite-backed shared state for swarm coordination.

    Usage:
        bb = Blackboard("/path/to/blackboard.db")
        bb.submit("exp-1", "bayesian", {"lr": 0.01}, reasoning="TPE suggested")
        bb.update(trial_id, score=0.85, accepted=True, status="completed")
        best = bb.best("exp-1")
        summary = bb.summary("exp-1")
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS trials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment TEXT NOT NULL,
                strategy TEXT NOT NULL DEFAULT '',
                params TEXT NOT NULL DEFAULT '{}',
                score REAL,
                accepted INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                reasoning TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                parent_id INTEGER,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                FOREIGN KEY (parent_id) REFERENCES trials(id)
            );

            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment TEXT NOT NULL,
                worker TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment TEXT NOT NULL,
                worker TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                trial_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (trial_id) REFERENCES trials(id)
            );

            CREATE INDEX IF NOT EXISTS idx_trials_experiment ON trials(experiment);
            CREATE INDEX IF NOT EXISTS idx_trials_score ON trials(score);
            CREATE INDEX IF NOT EXISTS idx_claims_experiment ON claims(experiment);
            CREATE INDEX IF NOT EXISTS idx_feed_experiment ON feed(experiment);
        """)
        self._conn.commit()

    def close(self):
        self._conn.close()

    # ----- Trials -----

    def submit(
        self,
        experiment: str,
        strategy: str,
        params: dict[str, Any],
        *,
        reasoning: str = "",
        metadata: dict[str, Any] | None = None,
        parent_id: int | None = None,
        status: str = "pending",
    ) -> int:
        """Submit a new trial. Returns trial ID."""
        cur = self._conn.execute(
            """INSERT INTO trials
               (experiment, strategy, params, status, reasoning, metadata, parent_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                experiment, strategy, json.dumps(params), status,
                reasoning, json.dumps(metadata or {}), parent_id, now_iso(),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def update(
        self,
        trial_id: int,
        *,
        score: float | None = None,
        accepted: bool | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Update a trial after execution."""
        sets = []
        values = []
        if score is not None:
            sets.append("score = ?")
            values.append(score)
        if accepted is not None:
            sets.append("accepted = ?")
            values.append(int(accepted))
        if status is not None:
            sets.append("status = ?")
            values.append(status)
            if status in ("completed", "failed"):
                sets.append("finished_at = ?")
                values.append(now_iso())
        if metadata is not None:
            sets.append("metadata = ?")
            values.append(json.dumps(metadata))
        if not sets:
            return
        values.append(trial_id)
        self._conn.execute(
            f"UPDATE trials SET {', '.join(sets)} WHERE id = ?",
            values,
        )
        self._conn.commit()

    def query(
        self,
        experiment: str,
        *,
        strategy: str | None = None,
        status: str | None = None,
        limit: int = 100,
        order_by: str = "score DESC",
    ) -> list[TrialRecord]:
        """Query trials for an experiment."""
        where = ["experiment = ?"]
        values: list[Any] = [experiment]
        if strategy:
            where.append("strategy = ?")
            values.append(strategy)
        if status:
            where.append("status = ?")
            values.append(status)
        where_clause = " AND ".join(where)
        rows = self._conn.execute(
            f"SELECT * FROM trials WHERE {where_clause} ORDER BY {order_by} LIMIT ?",
            [*values, limit],
        ).fetchall()
        return [self._row_to_trial(r) for r in rows]

    def best(self, experiment: str, *, direction: str = "maximize") -> TrialRecord | None:
        """Get the best completed trial."""
        order = "score DESC" if direction == "maximize" else "score ASC"
        rows = self._conn.execute(
            f"SELECT * FROM trials WHERE experiment = ? AND status = 'completed' AND score IS NOT NULL ORDER BY {order} LIMIT 1",
            (experiment,),
        ).fetchall()
        return self._row_to_trial(rows[0]) if rows else None

    def count(self, experiment: str, *, status: str | None = None) -> int:
        """Count trials."""
        if status:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM trials WHERE experiment = ? AND status = ?",
                (experiment, status),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM trials WHERE experiment = ?",
                (experiment,),
            ).fetchone()
        return row[0]

    def history(self, experiment: str, *, limit: int = 50) -> list[TrialRecord]:
        """Get trial history ordered by creation time."""
        return self.query(experiment, limit=limit, order_by="created_at ASC")

    # ----- Claims -----

    def claim(
        self,
        experiment: str,
        worker: str,
        description: str,
        ttl_seconds: int = 900,
    ) -> int:
        """Post a claim. Returns claim ID."""
        created = now_iso()
        expires = datetime.fromtimestamp(
            now_ts() + ttl_seconds, tz=timezone.utc
        ).isoformat()
        cur = self._conn.execute(
            "INSERT INTO claims (experiment, worker, description, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (experiment, worker, description, created, expires),
        )
        self._conn.commit()
        return cur.lastrowid

    def active_claims(self, experiment: str) -> list[Claim]:
        """Get non-expired claims."""
        now = now_iso()
        rows = self._conn.execute(
            "SELECT * FROM claims WHERE experiment = ? AND expires_at > ? ORDER BY created_at DESC",
            (experiment, now),
        ).fetchall()
        return [
            Claim(
                id=r["id"], experiment=r["experiment"], worker=r["worker"],
                description=r["description"], created_at=r["created_at"],
                expires_at=r["expires_at"],
            )
            for r in rows
        ]

    def clear_expired_claims(self, experiment: str | None = None) -> int:
        """Remove expired claims. Returns count removed."""
        now = now_iso()
        if experiment:
            cur = self._conn.execute(
                "DELETE FROM claims WHERE experiment = ? AND expires_at <= ?",
                (experiment, now),
            )
        else:
            cur = self._conn.execute(
                "DELETE FROM claims WHERE expires_at <= ?", (now,),
            )
        self._conn.commit()
        return cur.rowcount

    # ----- Feed -----

    def post(
        self,
        experiment: str,
        content: str,
        *,
        worker: str = "",
        trial_id: int | None = None,
    ) -> int:
        """Post to the shared feed."""
        cur = self._conn.execute(
            "INSERT INTO feed (experiment, worker, content, trial_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (experiment, worker, content, trial_id, now_iso()),
        )
        self._conn.commit()
        return cur.lastrowid

    def recent_posts(self, experiment: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent feed posts."""
        rows = self._conn.execute(
            "SELECT * FROM feed WHERE experiment = ? ORDER BY created_at DESC LIMIT ?",
            (experiment, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ----- Summary -----

    def summary(self, experiment: str, *, max_trials: int = 30) -> str:
        """Generate a text summary of completed trials for LLM context.
        AIDE-inspired: just a text dump of what's been tried.
        """
        trials = self.query(
            experiment, status="completed", limit=max_trials,
            order_by="score DESC",
        )
        if not trials:
            return "No completed trials yet."

        best = trials[0] if trials else None
        lines = [
            f"## Blackboard Summary ({len(trials)} trials)",
            f"Best score: {best.score:.6f} (strategy: {best.strategy})" if best and best.score else "",
            "",
        ]

        for i, t in enumerate(trials[:20]):
            score_str = f"{t.score:.6f}" if t.score is not None else "N/A"
            accepted_str = "accepted" if t.accepted else "rejected"
            params_str = json.dumps(t.params, indent=None)
            reasoning_preview = t.reasoning[:100] if t.reasoning else ""
            lines.append(
                f"  [{i+1}] {score_str} ({accepted_str}, {t.strategy}) "
                f"params={params_str}"
            )
            if reasoning_preview:
                lines.append(f"      {reasoning_preview}")

        return "\n".join(lines)

    # ----- Helpers -----

    def _row_to_trial(self, row: sqlite3.Row) -> TrialRecord:
        return TrialRecord(
            id=row["id"],
            experiment=row["experiment"],
            strategy=row["strategy"],
            params=json.loads(row["params"]),
            score=row["score"],
            accepted=bool(row["accepted"]),
            status=row["status"],
            reasoning=row["reasoning"],
            metadata=json.loads(row["metadata"]),
            created_at=row["created_at"],
            finished_at=row["finished_at"],
            parent_id=row["parent_id"],
        )
