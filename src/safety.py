"""
Safety policy layer for UnattendedBot8300.

Provides rate limiting, duplicate detection, and content moderation.
Deterministic checks only - content decisions come from the AI model.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import sqlite3


@dataclass
class SafetyResult:
    """Result of safety/policy checks."""
    allowed: bool
    violations: list[str]
    message: str


class SafetyPolicy:
    """Enforces safety policies on proposed actions."""
    
    def __init__(self, conn: sqlite3.Connection, db_path: str):
        self._conn = conn
        self._db_path = db_path
        # Common spam patterns
        self._spam_patterns = [
            r"\b(free |click here|sponsored|ad|buy now)\b",
            r"\b(\$\$\$|dollar|coin|crypto)\b",
            r"https?://\S+",  # URLs in content (might be ok for links, but flagged)
            r"\b(like|share|follow)\b.*\b(our|page|us)\b",  # Engagement farming
        ]
        # Minimum content length before we consider it substantive
        self._min_content_length = 5
    
    def check_post(self, message: str) -> SafetyResult:
        """Check if a proposed post passes safety checks."""
        violations = []
        
        # Length check
        if len(message.strip()) < self._min_content_length:
            violations.append(f"Message too short (min {self._min_content_length} chars)")
        
        # Spam pattern check
        for pattern in self._spam_patterns:
            if re.search(pattern, message, re.IGNORECASE):
                violations.append(f"Potential spam pattern detected: {pattern}")
        
        # Duplicate check - recent identical content
        if self._is_recent_duplicate(message, window_hours=24):
            violations.append("Content is a recent duplicate")
        
        # Excessive punctuation/exclamation marks
        if message.count("!") > 5:
            violations.append("Excessive exclamation marks")
        
        allowed = len(violations) == 0
        message_text = "Post allowed" if allowed else "Post blocked due to violations"
        
        return SafetyResult(
            allowed=allowed,
            violations=violations,
            message=message_text
        )
    
    def check_reply(self, message: str, commenter_id: str, 
                    commenter_name: str) -> SafetyResult:
        """Check if a proposed reply passes safety checks."""
        violations = []
        
        # Length check
        if len(message.strip()) < 3:
            violations.append("Reply too short")
        
        # Spam check
        for pattern in self._spam_patterns:
            if re.search(pattern, message, re.IGNORECASE):
                violations.append(f"Potential spam pattern detected")
        
        # Self-reply check (don't reply to your own comment)
        # Note: In production, we'd track our own comment IDs
        
        allowed = len(violations) == 0
        message_text = "Reply allowed" if allowed else "Reply blocked due to violations"
        
        return SafetyResult(
            allowed=allowed,
            violations=violations,
            message=message_text
        )
    
    def check_rate_limits(self, action_type: str) -> SafetyResult:
        """Check if action is within configured rate limits."""
        from .config import Config
        # This would be called with config, but we need to check rate limits
        # via storage.py functions
        # For now, return allowed=True - the actual rate limit check is in proposed_actions.py
        return SafetyResult(allowed=True, violations=[], message="Rate limit check passed")
    
    def _is_recent_duplicate(self, content: str, window_hours: int = 24) -> bool:
        """Check if this content was recently posted."""
        # Simple hash-based duplicate detection
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        
        # Check in proposed_actions - payload is JSON, so we search the text
        # Note: SQLite JSON functions work on the json_extract or we just LIKE the JSON string
        cur = self._conn.execute("""
            SELECT 1 FROM proposed_actions 
            WHERE status IN ('proposed', 'approved', 'executed')
            AND payload LIKE ?
            LIMIT 1
        """, (f"%{content_hash}%",))
        
        if cur.fetchone():
            return True
        
        return False


# === Standalone helper functions ===

def is_content_spam(text: str) -> bool:
    """Quick check for obvious spam patterns."""
    spam_patterns = [
        r"\b(click here|visit this link|sponsored)\b",
        r"\b(free money|make $\$|earn crypto)\b",
    ]
    for pattern in spam_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def normalize_content(text: str) -> str:
    """Normalize text for comparison (lowercase, strip whitespace, collapse spaces)."""
    return re.sub(r'\s+', ' ', text.lower().strip())


def content_hash(text: str) -> str:
    """Generate a hash for content deduplication."""
    return hashlib.sha256(normalize_content(text).encode()).hexdigest()[:12]