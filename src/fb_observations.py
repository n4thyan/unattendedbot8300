"""
Facebook observation pipeline for UnattendedBot8300.

Provides deterministic representation of Facebook observations from Graph API.
Supports NEW vs. previously-seen detection, parent relationships, and engagement metadata.
Uses Graph API-compatible fixtures/mocks for safe offline testing.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pathlib import Path

import sqlite3


class ObservationKind(str, Enum):
    """Types of observations."""
    POST = "post"
    COMMENT = "comment"
    REPLY = "reply"


@dataclass
class PostObservation:
    """Represents an observed Facebook post."""
    fb_post_id: str
    message: str
    created_time: str
    posted_by_page: bool
    like_count: int = 0
    comment_count: int = 0
    is_new: bool = True
    raw: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "fb_post_id": self.fb_post_id,
            "message": self.message,
            "created_time": self.created_time,
            "posted_by_page": self.posted_by_page,
            "like_count": self.like_count,
            "comment_count": self.comment_count,
            "is_new": self.is_new,
            "raw": self.raw,
        }


@dataclass
class CommentObservation:
    """Represents an observed Facebook comment."""
    fb_comment_id: str
    post_id: str
    message: str
    from_name: str
    from_id: str
    created_time: str
    parent_comment_id: Optional[str] = None
    like_count: int = 0
    is_new: bool = True
    raw: dict = field(default_factory=dict)
    
    def is_reply(self) -> bool:
        """Check if this is a reply (not a top-level comment)."""
        return self.parent_comment_id is not None
    
    def to_dict(self) -> dict:
        return {
            "fb_comment_id": self.fb_comment_id,
            "post_id": self.post_id,
            "message": self.message,
            "from_name": self.from_name,
            "from_id": self.from_id,
            "created_time": self.created_time,
            "parent_comment_id": self.parent_comment_id,
            "like_count": self.like_count,
            "is_new": self.is_new,
            "raw": self.raw,
        }


@dataclass
class ObservationResult:
    """Result from an observation cycle."""
    posts: list[PostObservation] = field(default_factory=list)
    comments: list[CommentObservation] = field(default_factory=list)
    unreplied_comments: list[CommentObservation] = field(default_factory=list)
    observation_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def get_summary(self) -> str:
        return (f"Found {len(self.posts)} posts, {len(self.comments)} comments, "
                f"{len(self.unreplied_comments)} unreplied")
    
    def to_dict(self) -> dict:
        return {
            "posts": [p.to_dict() for p in self.posts],
            "comments": [c.to_dict() for c in self.comments],
            "unreplied_comments": [c.to_dict() for c in self.unreplied_comments],
            "observation_time": self.observation_time,
        }


class ObservationDetector:
    """Detects NEW vs previously-seen observations."""
    
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
    
    def is_post_new(self, fb_post_id: str) -> bool:
        """Check if a post ID is new (not in database)."""
        cur = self._conn.execute(
            "SELECT 1 FROM fb_posts WHERE fb_post_id = ? LIMIT 1",
            (fb_post_id,)
        )
        return cur.fetchone() is None
    
    def is_comment_new(self, fb_comment_id: str) -> bool:
        """Check if a comment ID is new (not in database)."""
        cur = self._conn.execute(
            "SELECT 1 FROM fb_comments WHERE fb_comment_id = ? LIMIT 1",
            (fb_comment_id,)
        )
        return cur.fetchone() is None
    
    def mark_post_seen(self, post: PostObservation) -> None:
        """Record that we've seen this post."""
        self._conn.execute("""
            INSERT OR IGNORE INTO fb_posts 
            (fb_post_id, posted_by_page, message, created_time, raw)
            VALUES (?, ?, ?, ?, ?)
        """, (
            post.fb_post_id,
            post.posted_by_page,
            post.message,
            post.created_time,
            json.dumps(post.raw) if post.raw else None
        ))
        self._conn.commit()
    
    def mark_comment_seen(self, comment: CommentObservation) -> None:
        """Record that we've seen this comment."""
        self._conn.execute("""
            INSERT OR IGNORE INTO fb_comments 
            (fb_comment_id, post_id, parent_comment_id, from_name, from_id, message, created_time, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            comment.fb_comment_id,
            comment.post_id,
            comment.parent_comment_id,
            comment.from_name,
            comment.from_id,
            comment.message,
            comment.created_time,
            json.dumps(comment.raw) if comment.raw else None
        ))
        self._conn.commit()


class ObservationCollector:
    """
    Collects observations from various sources.
    In Phase 1, uses Graph API-compatible fixtures/mocks.
    """
    
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._detector = ObservationDetector(conn)
    
    def collect_from_fixture(self, fixture_path: Optional[Path] = None) -> ObservationResult:
        """
        Collect observations from a fixture file.
        
        Args:
            fixture_path: Path to JSON fixture file. If None, uses built-in sample.
        """
        if fixture_path and fixture_path.exists():
            with open(fixture_path) as f:
                data = json.load(f)
        else:
            # Built-in sample fixture for testing
            data = self._get_sample_fixture()
        
        posts = []
        for post_data in data.get("posts", []):
            post = PostObservation(
                fb_post_id=post_data["fb_post_id"],
                message=post_data.get("message", ""),
                created_time=post_data.get("created_time", ""),
                posted_by_page=post_data.get("posted_by_page", False),
                like_count=post_data.get("like_count", 0),
                comment_count=post_data.get("comment_count", 0),
                raw=post_data.get("raw", {}),
            )
            post.is_new = self._detector.is_post_new(post.fb_post_id)
            posts.append(post)
            # Mark as seen
            self._detector.mark_post_seen(post)
        
        comments = []
        unreplied_comments = []
        for comment_data in data.get("comments", []):
            comment = CommentObservation(
                fb_comment_id=comment_data["fb_comment_id"],
                post_id=comment_data["post_id"],
                message=comment_data.get("message", ""),
                from_name=comment_data.get("from_name", "Someone"),
                from_id=comment_data.get("from_id", ""),
                created_time=comment_data.get("created_time", ""),
                parent_comment_id=comment_data.get("parent_comment_id"),
                like_count=comment_data.get("like_count", 0),
                raw=comment_data.get("raw", {}),
            )
            comment.is_new = self._detector.is_comment_new(comment.fb_comment_id)
            comments.append(comment)
            
            # Mark as seen
            self._detector.mark_comment_seen(comment)
            
            # Track unreplied (top-level comments that don't have bot replies)
            if not comment.is_reply():
                unreplied_comments.append(comment)
        
        # Get any existing unreplied comments from database
        try:
            from .storage import get_comments_for_post
            for post in posts:
                db_comments = get_comments_for_post(self._conn, post.fb_post_id)
                for c in db_comments:
                    if not c.get("parent_comment_id"):
                        # Check if already in our list
                        found = any(cm.fb_comment_id == c["fb_comment_id"] for cm in unreplied_comments)
                        if not found:
                            obs = CommentObservation(
                                fb_comment_id=c["fb_comment_id"],
                                post_id=c["post_id"],
                                message=c["message"],
                                from_name=c["from_name"],
                                from_id=c["from_id"],
                                created_time=c["created_time"],
                                parent_comment_id=c.get("parent_comment_id"),
                                like_count=c.get("like_count", 0),
                            )
                            unreplied_comments.append(obs)
        except Exception:
            pass
        
        return ObservationResult(
            posts=posts,
            comments=comments,
            unreplied_comments=unreplied_comments,
        )
    
    def _get_sample_fixture(self) -> dict:
        """Return a sample fixture for testing when none provided."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        
        return {
            "posts": [
                {
                    "fb_post_id": f"test_page_123_456",
                    "message": "Hello world, this is my first test post!",
                    "created_time": (now - timedelta(hours=1)).isoformat(),
                    "posted_by_page": True,
                    "like_count": 10,
                    "comment_count": 3,
                },
            ],
            "comments": [
                {
                    "fb_comment_id": f"comment_789",
                    "post_id": "test_page_123_456",
                    "message": "Nice post! Very interesting thoughts.",
                    "from_name": "Test User",
                    "from_id": "user_123",
                    "created_time": (now - timedelta(minutes=30)).isoformat(),
                    "parent_comment_id": None,
                    "like_count": 2,
                },
            ],
        }


def load_observation_from_response(response: dict, page_id: str) -> ObservationResult:
    """
    Load observations from Graph API response format.
    
    Args:
        response: Raw API response dict
        page_id: The page ID for context
    
    Returns:
        ObservationResult with parsed posts and comments
    """
    result = ObservationResult()
    
    # Parse posts from response
    posts_data = response.get("posts", [])
    for p in posts_data:
        post = PostObservation(
            fb_post_id=p.get("fb_post_id") or p.get("id"),
            message=p.get("message", ""),
            created_time=p.get("created_time", ""),
            posted_by_page=p.get("posted_by_page", False),
            like_count=p.get("like_count", 0),
            comment_count=p.get("comment_count", 0),
            raw=p,
        )
        result.posts.append(post)
    
    # Parse comments from response
    comments_data = response.get("comments", [])
    for c in comments_data:
        comment = CommentObservation(
            fb_comment_id=c.get("fb_comment_id") or c.get("id"),
            post_id=c.get("post_id"),
            message=c.get("message", ""),
            from_name=c.get("from_name", "Someone"),
            from_id=c.get("from_id", ""),
            created_time=c.get("created_time", ""),
            parent_comment_id=c.get("parent_comment_id"),
            like_count=c.get("like_count", 0),
            raw=c,
        )
        result.comments.append(comment)
        
        if not comment.is_reply():
            result.unreplied_comments.append(comment)
    
    return result