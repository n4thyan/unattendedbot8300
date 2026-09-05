"""
Wake cycle logging for UnattendedBot8300.

Records each wake cycle for observability and debugging.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import sqlite3


@dataclass
class WakeCycle:
    """Represents a wake cycle record."""
    id: Optional[int] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    observation_summary: str = ""
    decision: str = ""
    proposed_action_id: Optional[int] = None
    decision_summary: str = ""
    error: Optional[str] = None
    
    @classmethod
    def from_db_row(cls, row: dict) -> "WakeCycle":
        """Create from database row."""
        return cls(
            id=row["id"],
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            observation_summary=row.get("observation_summary", ""),
            decision=row.get("decision", ""),
            proposed_action_id=row.get("proposed_action_id"),
            decision_summary=row.get("decision_summary", ""),
            error=row.get("error")
        )


class WakeCycleLogger:
    """Manages wake cycle logging."""
    
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
    
    def start(self, observation_summary: str = "") -> int:
        """Start a new wake cycle and return its ID."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute("""
            INSERT INTO wake_cycles (started_at, observation_summary)
            VALUES (?, ?)
        """, (now, observation_summary))
        self._conn.commit()
        return cur.lastrowid
    
    def complete(self, cycle_id: int, decision: str, 
                 proposed_action_id: Optional[int] = None,
                 decision_summary: str = "",
                 error: Optional[str] = None) -> None:
        """Complete a wake cycle."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            UPDATE wake_cycles 
            SET completed_at = ?, decision = ?, proposed_action_id = ?, 
                decision_summary = ?, error = ?
            WHERE id = ?
        """, (now, decision, proposed_action_id, decision_summary, error, cycle_id))
        self._conn.commit()
    
    def record(self, decision: str, observation_summary: str = "",
               proposed_action_id: Optional[int] = None,
               decision_summary: str = "",
               error: Optional[str] = None) -> WakeCycle:
        """Record a complete wake cycle."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute("""
            INSERT INTO wake_cycles 
            (started_at, completed_at, observation_summary, decision, 
             proposed_action_id, decision_summary, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (now, now, observation_summary, decision, proposed_action_id, 
              decision_summary, error))
        self._conn.commit()
        
        return WakeCycle(
            id=cur.lastrowid,
            started_at=now,
            completed_at=now,
            observation_summary=observation_summary,
            decision=decision,
            proposed_action_id=proposed_action_id,
            decision_summary=decision_summary,
            error=error
        )
    
    def get_recent(self, limit: int = 50) -> list[WakeCycle]:
        """Get recent wake cycles."""
        cur = self._conn.execute("""
            SELECT * FROM wake_cycles 
            ORDER BY started_at DESC 
            LIMIT ?
        """, (limit,))
        return [WakeCycle.from_db_row(dict(row)) for row in cur.fetchall()]
    
    def get_by_decision(self, decision: str) -> list[WakeCycle]:
        """Get wake cycles by decision type."""
        cur = self._conn.execute("""
            SELECT * FROM wake_cycles 
            WHERE decision = ? 
            ORDER BY started_at DESC
        """, (decision,))
        return [WakeCycle.from_db_row(dict(row)) for row in cur.fetchall()]


# === Statistics ===

def get_cycle_stats(conn: sqlite3.Connection) -> dict:
    """Get statistics about wake cycles."""
    cur = conn.execute("""
        SELECT 
            COUNT(*) as total_cycles,
            SUM(CASE WHEN decision = 'POST' THEN 1 ELSE 0 END) as post_cycles,
            SUM(CASE WHEN decision = 'REPLY' THEN 1 ELSE 0 END) as reply_cycles,
            SUM(CASE WHEN decision = 'MEMORY' THEN 1 ELSE 0 END) as memory_cycles,
            SUM(CASE WHEN decision = 'NOTHING' THEN 1 ELSE 0 END) as nothing_cycles
        FROM wake_cycles
    """)
    row = cur.fetchone()
    return dict(row)