"""
Memory storage and retrieval layer for UnattendedBot8300.

Manages persistent memories that help the agent develop personality
and remember important interactions. Enhanced with relevance scoring.
"""

import json
import re
from datetime import datetime, timezone
from typing import Optional

import sqlite3

from .storage import (
    get_memories, store_memory as _store_memory_db, search_memories as _search_memories_db,
    get_short_term, set_short_term as _set_short_term_db
)


class MemoryStore:
    """
    High-level memory interface for the agent.
    
    Supports continuity for:
    - Page lore
    - Previous public interactions
    - Recurring jokes
    - Interests that emerge
    - Likes/dislikes
    - Notable failures/successes
    - Reflections
    - Recurring commenters (based only on their public interactions)
    """
    
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
    
    # === Memory Categories ===
    
    def remember(self, kind: str, content: str, tags: Optional[list[str]] = None) -> int:
        """Store a new memory."""
        return _store_memory_db(self._conn, kind, content, tags)
    
    def recall(self, kind: Optional[str] = None, limit: int = 100) -> list[dict]:
        """Recall memories, optionally filtered by kind."""
        return get_memories(self._conn, kind=kind, limit=limit)
    
    def search(self, query: str, limit: int = 50) -> list[dict]:
        """Search memories by content."""
        return _search_memories_db(self._conn, query, limit)
    
    # === Enhanced Memory Retrieval ===
    
    def get_relevant_memories(self, query: str, max_items: int = 5) -> list[dict]:
        """
        Get memories relevant to a query using multiple signals:
        - Tags
        - Keyword overlap
        - Recency
        - Importance/kind
        """
        # Get candidate memories
        candidate_memories = get_memories(self._conn, limit=max_items * 3)
        
        if not candidate_memories:
            return []
        
        # Score memories
        scored = []
        query_lower = query.lower()
        query_words = set(w for w in query_lower.split() if len(w) > 2)
        
        for mem in candidate_memories:
            score = 0
            content = mem.get("content", "").lower()
            tags_str = mem.get("tags", "") or ""
            tags = [t.strip() for t in tags_str.split(",") if t.strip()]
            kind = mem.get("kind", "")
            
            # Keyword overlap in content (weighted)
            content_words = set(w for w in content.split() if len(w) > 2)
            overlap = query_words & content_words
            score += len(overlap) * 3
            
            # Tag matching (weighted higher)
            tag_words = set(t.lower() for t in tags)
            if query_words & tag_words:
                score += 5
            if tag_words & set(t.lower() for t in tags):
                score += 2
            
            # Recency decay (newer = more relevant)
            try:
                created = datetime.fromisoformat(
                    mem.get("created_at", "").replace("Z", "+00:00")
                )
                age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
                score -= age_hours / 24  # Decay 1 point per day
            except Exception:
                score += 1  # Penalize entries we can't parse dates for
            
            # Kind weighting
            if kind == "lore":
                score += 3
            elif kind == "event":
                score += 2
            elif kind == "reflection":
                score += 1.5
            elif kind == "liking":
                score += 1
            elif kind == "dislike":
                score += 0.5
            
            scored.append((score, mem))
        
        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)
        
        return [mem for _, mem in scored[:max_items]]
    
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
    
    def get_relevant_memories_for_context(self, query: str, max_items: int = 5) -> str:
        """Get memories relevant to a query, formatted for context."""
        memories = self.get_relevant_memories(query, max_items)
        if not memories:
            return ""
        
        lines = ["Relevant memories:"]
        for mem in memories[:max_items]:
            content = mem.get('content', '')[:100]
            lines.append(f"  - {content}")
        return "\n".join(lines)
    
    def get_memories_by_kind(self, kind: str, limit: int = 50) -> list[dict]:
        """Get memories by kind category."""
        return get_memories(self._conn, kind=kind, limit=limit)
    
    def get_memories_by_tags(self, tags: list[str], limit: int = 50) -> list[dict]:
        """Get memories that have any of the specified tags."""
        all_memories = get_memories(self._conn, limit=limit * 3)
        result = []
        
        for mem in all_memories:
            mem_tags_str = mem.get("tags", "") or ""
            mem_tags = set(t.strip().lower() for t in mem_tags_str.split(","))
            query_tags = set(t.lower() for t in tags)
            
            if mem_tags & query_tags:
                result.append(mem)
                if len(result) >= limit:
                    break
        
        return result
    
    def update_memory(self, memory_id: int, content: Optional[str] = None,
                      tags: Optional[list[str]] = None) -> bool:
        """Update a memory's content or tags."""
        now = datetime.now(timezone.utc).isoformat()
        
        updates = []
        params = []
        
        if content is not None:
            updates.append("content = ?")
            params.append(content)
        
        if tags is not None:
            updates.append("tags = ?")
            params.append(",".join(tags))
        
        updates.append("last_seen = ?")
        params.append(now)
        
        params.append(memory_id)
        
        if not updates:
            return False
        
        query = f"UPDATE memories SET {', '.join(updates)} WHERE id = ?"
        result = self._conn.execute(query, params)
        self._conn.commit()
        
        return result.rowcount > 0