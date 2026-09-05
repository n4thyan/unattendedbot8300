"""
SQLite storage layer for UnattendedBot8300.

Single source of truth for all persistent data.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import Config


# SQL schema for all tables
SCHEMA = """
-- Posts fetched from Facebook (read-only record)
CREATE TABLE IF NOT EXISTS fb_posts (
    fb_post_id TEXT PRIMARY KEY,
    posted_by_page BOOLEAN,
    message TEXT,
    created_time TEXT,
    raw TEXT
);

-- Comments on our posts
CREATE TABLE IF NOT EXISTS fb_comments (
    fb_comment_id TEXT PRIMARY KEY,
    post_id TEXT REFERENCES fb_posts(fb_post_id),
    parent_comment_id TEXT,
    from_name TEXT,
    from_id TEXT,
    message TEXT,
    created_time TEXT,
    raw TEXT
);

-- Memories
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    tags TEXT
);

-- Short-term state with TTL
CREATE TABLE IF NOT EXISTS short_term (
    id TEXT PRIMARY KEY,
    value TEXT,
    expires_at TEXT
);

-- Proposed action queue
CREATE TABLE IF NOT EXISTS proposed_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    payload TEXT,
    target_fb_object TEXT,
    reason TEXT,
    safety_result TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    error TEXT,
    fb_result_id TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    executed_at TEXT,
    approved_by TEXT
);

-- Rate limit state
CREATE TABLE IF NOT EXISTS rate_limit_state (
    key TEXT PRIMARY KEY,
    window_end TEXT,
    count INTEGER DEFAULT 0
);

-- Configuration overrides
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Wake cycle logging
CREATE TABLE IF NOT EXISTS wake_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    observation_summary TEXT,
    decision TEXT,
    proposed_action_id INTEGER REFERENCES proposed_actions(id),
    decision_summary TEXT,
    error TEXT
);
"""


class Storage:
    """SQLite database manager for all agent state."""
    
    def __init__(self, config: Config):
        self.config = config
        self._db_path = config.db_path
        self._conn: Optional[sqlite3.Connection] = None
    
    @property
    def db_path(self) -> Path:
        return self._db_path
    
    def connect(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
        return self._conn
    
    def _init_schema(self) -> None:
        """Initialize database schema."""
        conn = self.connect()
        conn.executescript(SCHEMA)
        conn.commit()
    
    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
    
    def __enter__(self) -> "Storage":
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# --- FB Posts ---

def store_fb_posts(conn: sqlite3.Connection, posts: list[dict]) -> None:
    """Store Facebook posts in the database."""
    conn.execute("BEGIN")
    try:
        for post in posts:
            conn.execute("""
                INSERT OR REPLACE INTO fb_posts 
                (fb_post_id, posted_by_page, message, created_time, raw)
                VALUES (?, ?, ?, ?, ?)
            """, (
                post.get("fb_post_id"),
                post.get("posted_by_page", False),
                post.get("message"),
                post.get("created_time"),
                json.dumps(post.get("raw", {})) if post.get("raw") else None
            ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise


def get_fb_posts(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Retrieve recent Facebook posts."""
    cur = conn.execute("""
        SELECT * FROM fb_posts 
        ORDER BY created_time DESC 
        LIMIT ?
    """, (limit,))
    return [dict(row) for row in cur.fetchall()]


def get_fb_post(conn: sqlite3.Connection, fb_post_id: str) -> Optional[dict]:
    """Retrieve a specific Facebook post."""
    cur = conn.execute("SELECT * FROM fb_posts WHERE fb_post_id = ?", (fb_post_id,))
    row = cur.fetchone()
    return dict(row) if row else None


# --- FB Comments ---

def store_fb_comments(conn: sqlite3.Connection, comments: list[dict]) -> None:
    """Store Facebook comments in the database."""
    conn.execute("BEGIN")
    try:
        for comment in comments:
            conn.execute("""
                INSERT OR REPLACE INTO fb_comments 
                (fb_comment_id, post_id, parent_comment_id, from_name, from_id, message, created_time, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                comment.get("fb_comment_id"),
                comment.get("post_id"),
                comment.get("parent_comment_id"),
                comment.get("from_name"),
                comment.get("from_id"),
                comment.get("message"),
                comment.get("created_time"),
                json.dumps(comment.get("raw", {})) if comment.get("raw") else None
            ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise


def get_comments_for_post(conn: sqlite3.Connection, fb_post_id: str) -> list[dict]:
    """Get all comments for a specific post."""
    cur = conn.execute("""
        SELECT * FROM fb_comments 
        WHERE post_id = ? 
        ORDER BY created_time ASC
    """, (fb_post_id,))
    return [dict(row) for row in cur.fetchall()]


def get_unreplied_comments(conn: sqlite3.Connection) -> list[dict]:
    """Get comments that don't have bot replies yet."""
    cur = conn.execute("""
        SELECT c.* FROM fb_comments c
        LEFT JOIN fb_comments r ON r.parent_comment_id = c.fb_comment_id
        WHERE c.parent_comment_id IS NULL AND r.fb_comment_id IS NULL
        ORDER BY c.created_time DESC
    """)
    return [dict(row) for row in cur.fetchall()]


# --- Memories ---

def store_memory(conn: sqlite3.Connection, kind: str, content: str, 
                 tags: Optional[list[str]] = None) -> int:
    """Store a new memory and return its ID."""
    now = datetime.utcnow().isoformat()
    cur = conn.execute("""
        INSERT INTO memories (kind, content, created_at, last_seen, tags)
        VALUES (?, ?, ?, ?, ?)
    """, (kind, content, now, now, ",".join(tags) if tags else None))
    conn.commit()
    return cur.lastrowid


def get_memories(conn: sqlite3.Connection, kind: Optional[str] = None,
                 limit: int = 100) -> list[dict]:
    """Retrieve memories, optionally filtered by kind."""
    if kind:
        cur = conn.execute("""
            SELECT * FROM memories 
            WHERE kind = ? 
            ORDER BY last_seen DESC 
            LIMIT ?
        """, (kind, limit))
    else:
        cur = conn.execute("""
            SELECT * FROM memories 
            ORDER BY last_seen DESC 
            LIMIT ?
        """, (limit,))
    return [dict(row) for row in cur.fetchall()]


def search_memories(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict]:
    """Search memories by content."""
    cur = conn.execute("""
        SELECT * FROM memories 
        WHERE content LIKE ? 
        ORDER BY last_seen DESC 
        LIMIT ?
    """, (f"%{query}%", limit))
    return [dict(row) for row in cur.fetchall()]


# --- Short-term State ---

def get_short_term(conn: sqlite3.Connection, key: str) -> Optional[str]:
    """Get a short-term value if not expired."""
    now = datetime.utcnow().isoformat()
    cur = conn.execute(
        "SELECT value FROM short_term WHERE id = ? AND (expires_at IS NULL OR expires_at > ?)",
        (key, now)
    )
    row = cur.fetchone()
    return row["value"] if row else None


def set_short_term(conn: sqlite3.Connection, key: str, value: str,
                   ttl_seconds: Optional[int] = None) -> None:
    """Set a short-term value with optional TTL."""
    now = datetime.utcnow()
    expires_at = None
    if ttl_seconds:
        from datetime import timedelta
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    
    conn.execute("""
        INSERT OR REPLACE INTO short_term (id, value, expires_at)
        VALUES (?, ?, ?)
    """, (key, value, expires_at))
    conn.commit()


# --- Proposed Actions ---

def create_proposed_action(conn: sqlite3.Connection, action_type: str,
                           payload: dict, target_fb_object: Optional[str] = None,
                           reason: str = "") -> int:
    """Create a proposed action and return its ID."""
    now = datetime.utcnow().isoformat()
    cur = conn.execute("""
        INSERT INTO proposed_actions 
        (action_type, payload, target_fb_object, reason, created_at, status)
        VALUES (?, ?, ?, ?, ?, 'proposed')
    """, (action_type, json.dumps(payload), target_fb_object, reason, now))
    conn.commit()
    return cur.lastrowid


def get_proposed_actions(conn: sqlite3.Connection, status: Optional[str] = None) -> list[dict]:
    """Get proposed actions by status."""
    if status:
        cur = conn.execute("""
            SELECT * FROM proposed_actions 
            WHERE status = ? 
            ORDER BY created_at DESC
        """, (status,))
    else:
        cur = conn.execute("""
            SELECT * FROM proposed_actions 
            ORDER BY created_at DESC
        """)
    return [dict(row) for row in cur.fetchall()]


def approve_action(conn: sqlite3.Connection, action_id: int, 
                   approved_by: str = "admin") -> None:
    """Approve a proposed action."""
    now = datetime.utcnow().isoformat()
    conn.execute("""
        UPDATE proposed_actions 
        SET status = 'approved', approved_at = ?, approved_by = ?
        WHERE id = ? AND status = 'proposed'
    """, (now, approved_by, action_id))
    conn.commit()


def reject_action(conn: sqlite3.Connection, action_id: int) -> None:
    """Reject a proposed action."""
    conn.execute("""
        UPDATE proposed_actions 
        SET status = 'rejected'
        WHERE id = ? AND status = 'proposed'
    """, (action_id,))
    conn.commit()


def mark_executed(conn: sqlite3.Connection, action_id: int, 
                  fb_result_id: Optional[str] = None, error: Optional[str] = None) -> None:
    """Mark an action as executed (from approved)."""
    now = datetime.utcnow().isoformat()
    status = "failed" if error else "executed"
    conn.execute("""
        UPDATE proposed_actions 
        SET status = ?, executed_at = ?, fb_result_id = ?, error = ?
        WHERE id = ?
    """, (status, now, fb_result_id, error, action_id))
    conn.commit()


# --- Rate Limits ---

def check_rate_limit(conn: sqlite3.Connection, key: str, 
                     window_seconds: int, limit: int) -> bool:
    """Check if we're within rate limit. Returns True if allowed."""
    now = datetime.utcnow()
    window_end = None
    count = 0
    
    cur = conn.execute("SELECT window_end, count FROM rate_limit_state WHERE key = ?", (key,))
    row = cur.fetchone()
    if row:
        # Check if window has expired
        if row["window_end"] and row["window_end"] > now.isoformat():
            count = row["count"]
        else:
            # Window expired, reset
            from datetime import timedelta
            window_end = (now + timedelta(seconds=window_seconds)).isoformat()
    
    if count >= limit:
        return False
    
    # Increment counter
    if window_end is None:
        from datetime import timedelta
        window_end = (now + timedelta(seconds=window_seconds)).isoformat()
    
    conn.execute("""
        INSERT OR REPLACE INTO rate_limit_state (key, window_end, count)
        VALUES (?, ?, ?)
    """, (key, window_end, count + 1))
    conn.commit()
    return True


# --- Wake Cycles ---

def record_wake_cycle(conn: sqlite3.Connection, decision: str,
                      observation_summary: str = "",
                      proposed_action_id: Optional[int] = None,
                      decision_summary: str = "",
                      error: Optional[str] = None) -> int:
    """Record a wake cycle completion."""
    now = datetime.utcnow()
    cur = conn.execute("""
        INSERT INTO wake_cycles 
        (started_at, completed_at, observation_summary, decision, proposed_action_id, decision_summary, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (now.isoformat(), now.isoformat(), observation_summary, decision,
          proposed_action_id, decision_summary, error))
    conn.commit()
    return cur.lastrowid


def get_wake_cycles(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Get recent wake cycles."""
    cur = conn.execute("""
        SELECT * FROM wake_cycles 
        ORDER BY started_at DESC 
        LIMIT ?
    """, (limit,))
    return [dict(row) for row in cur.fetchall()]