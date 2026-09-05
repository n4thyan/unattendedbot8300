"""
Proposed actions queue for UnattendedBot8300.

Manages the approval queue where the agent proposes actions
for human review before execution.
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from typing import Optional

import sqlite3


class ActionType(Enum):
    """Types of actions the agent can propose."""
    POST = "POST"
    REPLY = "REPLY"
    MEMORY = "MEMORY"
    NOTHING = "NOTHING"


class ActionStatus(Enum):
    """Status of a proposed action."""
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


@dataclass
class ProposedAction:
    """Represents a proposed action in the approval queue."""
    action_type: str
    payload: dict
    target_fb_object: Optional[str]
    reason: str
    safety_result: Optional[dict] = None
    error: Optional[str] = None
    fb_result_id: Optional[str] = None
    approved_by: Optional[str] = None
    created_at: Optional[str] = None
    approved_at: Optional[str] = None
    executed_at: Optional[str] = None
    
    @classmethod
    def from_db_row(cls, row: dict) -> "ProposedAction":
        """Create from database row."""
        return cls(
            action_type=row["action_type"],
            payload=row["payload"],
            target_fb_object=row.get("target_fb_object"),
            reason=row.get("reason", ""),
            safety_result=json.loads(row["safety_result"]) if row.get("safety_result") else None,
            error=row.get("error"),
            fb_result_id=row.get("fb_result_id"),
            approved_by=row.get("approved_by"),
            created_at=row.get("created_at"),
            approved_at=row.get("approved_at"),
            executed_at=row.get("executed_at")
        )
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)


class ProposedActionQueue:
    """Manages the proposed actions queue."""
    
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
    
    def propose(self, action_type: ActionType, payload: dict,
                target_fb_object: Optional[str] = None,
                reason: str = "") -> int:
        """Propose a new action and return its ID."""
        now = datetime.utcnow().isoformat()
        
        cur = self._conn.execute("""
            INSERT INTO proposed_actions 
            (action_type, payload, target_fb_object, reason, created_at, status)
            VALUES (?, ?, ?, ?, ?, 'proposed')
        """, (
            action_type.value,
            json.dumps(payload),
            target_fb_object,
            reason,
            now
        ))
        self._conn.commit()
        return cur.lastrowid
    
    def propose_post(self, message: str, reason: str = "") -> int:
        """Propose a new page post."""
        return self.propose(
            ActionType.POST,
            {"message": message},
            reason=reason
        )
    
    def propose_reply(self, comment_id: str, message: str, 
                      reason: str = "") -> int:
        """Propose a reply to a comment."""
        return self.propose(
            ActionType.REPLY,
            {"message": message},
            target_fb_object=comment_id,
            reason=reason
        )
    
    def propose_memory(self, kind: str, content: str, 
                       tags: Optional[list[str]] = None,
                       reason: str = "") -> int:
        """Propose storing a memory."""
        return self.propose(
            ActionType.MEMORY,
            {"kind": kind, "content": content, "tags": tags or []},
            reason=reason
        )
    
    def get_pending(self) -> list[ProposedAction]:
        """Get all proposed and approved actions."""
        cur = self._conn.execute("""
            SELECT * FROM proposed_actions 
            WHERE status IN ('proposed', 'approved')
            ORDER BY created_at DESC
        """)
        return [ProposedAction.from_db_row(dict(row)) for row in cur.fetchall()]
    
    def get_by_id(self, action_id: int) -> Optional[ProposedAction]:
        """Get a specific action by ID."""
        cur = self._conn.execute("""
            SELECT * FROM proposed_actions WHERE id = ?
        """, (action_id,))
        row = cur.fetchone()
        return ProposedAction.from_db_row(dict(row)) if row else None
    
    def list_all(self) -> list[dict]:
        """List all actions (for CLI display)."""
        cur = self._conn.execute("""
            SELECT * FROM proposed_actions 
            ORDER BY created_at DESC
        """)
        return [dict(row) for row in cur.fetchall()]
    
    def list_pending(self) -> list[dict]:
        """List pending actions (for CLI display)."""
        cur = self._conn.execute("""
            SELECT * FROM proposed_actions 
            WHERE status = 'proposed'
            ORDER BY created_at DESC
        """)
        return [dict(row) for row in cur.fetchall()]
    
    def approve(self, action_id: int, approved_by: str = "admin") -> bool:
        """Approve a proposed action."""
        now = datetime.utcnow().isoformat()
        result = self._conn.execute("""
            UPDATE proposed_actions 
            SET status = 'approved', approved_at = ?, approved_by = ?
            WHERE id = ? AND status = 'proposed'
        """, (now, approved_by, action_id))
        self._conn.commit()
        return result.rowcount > 0
    
    def reject(self, action_id: int) -> bool:
        """Reject a proposed action."""
        result = self._conn.execute("""
            UPDATE proposed_actions 
            SET status = 'rejected'
            WHERE id = ? AND status = 'proposed'
        """, (action_id,))
        self._conn.commit()
        return result.rowcount > 0
    
    def execute(self, action_id: int, result_on_success: Optional[str] = None,
                error: Optional[str] = None) -> bool:
        """Mark an action as executed (called after actual Facebook API call)."""
        now = datetime.utcnow().isoformat()
        status = "failed" if error else "executed"
        result = self._conn.execute("""
            UPDATE proposed_actions 
            SET status = ?, executed_at = ?, fb_result_id = ?, error = ?
            WHERE id = ? AND status = 'approved'
        """, (status, now, result_on_success, error, action_id))
        self._conn.commit()
        return result.rowcount > 0
    
    def clear_executed(self, older_than_days: int = 30) -> int:
        """Clear executed/failed actions older than specified days."""
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=older_than_days)).isoformat()
        result = self._conn.execute("""
            DELETE FROM proposed_actions 
            WHERE status IN ('executed', 'failed') AND created_at < ?
        """, (cutoff,))
        self._conn.commit()
        return result.rowcount