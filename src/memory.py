"""
Memory storage and retrieval layer for UnattendedBot8300.

Manages persistent memories that help the agent develop personality
and remember important interactions.
"""

import json
from datetime import datetime
from typing import Optional

import sqlite3

from .storage import (
    get_memories, store_memory, search_memories as _search_memories_db,
    get_short_term, set_short_term as _set_short_term_db
)


class MemoryStore:
    """High-level memory interface for the agent."""
    
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
    
    # === Memory Categories ===
    
    def remember(self, kind: str, content: str, tags: Optional[list[str]] = None) -> int:
        """Store a new memory."""
        return store_memory(self._conn, kind, content, tags)
    
    def recall(self, kind: Optional[str] = None, limit: int = 100) -> list[dict]:
        """Recall memories, optionally filtered by kind."""
        return get_memories(self._conn, kind=kind, limit=limit)
    
    def search(self, query: str, limit: int = 50) -> list[dict]:
        """Search memories by content."""
        return _search_memories_db(self._conn, query, limit)
    
    # === Convenience Methods for Common Kinds ===
    
    def remember_lore(self, content: str, tags: Optional[list[str]] = None) -> int:
        """Remember Page lore / running jokes / themes."""
        return self.remember("lore", content, tags or ["lore"])
    
    def remember_liking(self, content: str, tags: Optional[list[str]] = None) -> int:
        """Remember something the agent likes (topics, styles, etc.)."""
        return self.remember("liking", content, tags or ["liking"])
    
    def remember_dislike(self, content: str, tags: Optional[list[str]] = None) -> int:
        """Remember something the agent dislikes to avoid repetition."""
        return self.remember("dislike", content, tags or ["dislike"])
    
    def remember_event(self, content: str, tags: Optional[list[str]] = None) -> int:
        """Remember a notable event."""
        return self.remember("event", content, tags or ["event"])
    
    def remember_reflection(self, content: str, tags: Optional[list[str]] = None) -> int:
        """Remember a reflection on past interactions."""
        return self.remember("reflection", content, tags or ["reflection"])
    
    def remember_pattern(self, content: str, tags: Optional[list[str]] = None) -> int:
        """Remember a pattern of behavior or interaction."""
        return self.remember("pattern", content, tags or ["pattern"])
    
    # === Short-term State ===
    
    def set_temp(self, key: str, value: str, ttl_seconds: Optional[int] = None) -> None:
        """Set temporary state with optional TTL."""
        _set_short_term_db(self._conn, key, value, ttl_seconds)
    
    def get_temp(self, key: str) -> Optional[str]:
        """Get temporary state."""
        return get_short_term(self._conn, key)
    
    # === Integration Helpers ===
    
    def get_recent_posts_summary(self, limit: int = 10) -> str:
        """Get a text summary of recent Facebook posts."""
        from .storage import get_fb_posts
        posts = get_fb_posts(self._conn, limit=limit)
        lines = ["Recent posts:"]
        for post in posts[:limit]:
            msg = post.get("message", "")[:100]
            if len(post.get("message", "")) > 100:
                msg += "..."
            lines.append(f"  - {post.get('created_time', '')}: {msg}")
        return "\n".join(lines)
    
    def get_recent_comments_summary(self, limit: int = 20) -> str:
        """Get a text summary of recent comments on our posts."""
        from .storage import get_comments_for_post, get_fb_posts
        posts = get_fb_posts(self._conn, limit=limit)
        lines = ["Recent comments:"]
        for post in posts[:limit]:
            post_id = post.get("fb_post_id")
            comments = get_comments_for_post(self._conn, post_id)
            for comment in comments[:3]:  # Up to 3 per post
                from_name = comment.get("from_name", "Someone")
                msg = comment.get("message", "")[:80]
                if len(msg) > 80:
                    msg += "..."
                lines.append(f"  - From {from_name}: {msg}")
        return "\n".join(lines)
    
    def get_relevant_memories(self, query: str, max_items: int = 5) -> str:
        """Get memories relevant to a query, formatted for context."""
        memories = self.search(query, limit=max_items)
        if not memories:
            return ""
        
        lines = ["Relevant memories:"]
        for mem in memories[:max_items]:
            lines.append(f"  - {mem.get('content', '')[:100]}")
        return "\n".join(lines)