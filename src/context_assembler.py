"""
Context assembler for UnattendedBot8300.

Selects only useful context from observations, memories, state, and pending actions.
Token usage remains bounded - older irrelevant events fall out naturally.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import sqlite3

from .fb_observations import ObservationResult, PostObservation, CommentObservation
from .storage import get_memories, get_wake_cycles, get_proposed_actions, get_short_term


@dataclass
class AssembledContext:
    """Bounded context assembled for model decision making."""
    
    # Agent identity
    agent_name: str = "UnattendedBot8300"
    agent_description: str = (
        "An autonomous AI agent operating a Facebook Page. "
        "I know I am software, not a human. I make my own observations, "
        "decisions, and potentially posts based on my interactions with the Page."
    )
    
    # Recent observations (new only, limited)
    new_posts: list[PostObservation] = field(default_factory=list)
    new_comments: list[CommentObservation] = field(default_factory=list)
    
    # Recent activity (last N items)
    recent_wake_cycles: list[dict] = field(default_factory=list)
    pending_actions: list[dict] = field(default_factory=list)
    
    # Relevant memories
    relevant_memories: list[dict] = field(default_factory=list)
    
    # Short-term state
    short_term_state: dict = field(default_factory=dict)
    
    # Configuration
    mode: str = "dry_run"
    approval_mode: bool = True
    
    def to_prompt_context(
        self,
        max_posts: int = 5,
        max_comments: int = 10,
        max_memories: int = 10,
        max_wake_cycles: int = 3,
        max_pending_actions: int = 5,
    ) -> str:
        """
        Format context as a string suitable for model prompts.
        
        Token usage is bounded by limits above.
        """
        lines = []
        
        # Header
        lines.append("=== UNATTENDEDBOT8300 CONTEXT ===")
        lines.append(f"Mode: {self.mode} (dry_run = no actual Facebook writes)")
        lines.append(f"Approval mode: {self.approval_mode}")
        lines.append("")
        
        # Identity
        lines.append("<<AGENT IDENTITY>>")
        lines.append(f"Name: {self.agent_name}")
        lines.append(f"Description: {self.agent_description}")
        lines.append("")
        
        # New observations
        lines.append("<<NEW OBSERVATIONS>>")
        if self.new_posts:
            lines.append(f"New posts ({len(self.new_posts)}):")
            for post in self.new_posts[:max_posts]:
                msg = post.message[:80] + "..." if len(post.message) > 80 else post.message
                lines.append(f"  - [{post.created_time}] {msg}")
        else:
            lines.append("No new posts")
        
        if self.new_comments:
            lines.append(f"New comments ({len(self.new_comments)}):")
            for comment in self.new_comments[:max_comments]:
                parent = " (reply)" if comment.is_reply() else ""
                msg = comment.message[:60] + "..." if len(comment.message) > 60 else comment.message
                lines.append(f"  - {comment.from_name}: {msg}{parent}")
        else:
            lines.append("No new comments")
        lines.append("")
        
        # Recent wake cycles
        lines.append("<<RECENT WAKE CYCLES>>")
        if self.recent_wake_cycles:
            for wc in self.recent_wake_cycles[:max_wake_cycles]:
                dt = wc.get("started_at", "")[:19]
                decision = wc.get("decision", "")
                summary = wc.get("decision_summary", "")[:50]
                lines.append(f"  {dt}: {decision} - {summary}")
        else:
            lines.append("No wake cycles recorded")
        lines.append("")
        
        # Pending actions
        lines.append("<<PENDING ACTIONS>>")
        if self.pending_actions:
            for action in self.pending_actions[:max_pending_actions]:
                aid = action.get("id", "?")
                atype = action.get("action_type", "?")
                reason = action.get("reason", "")[:50]
                lines.append(f"  {aid}: {atype} - {reason}")
        else:
            lines.append("No pending actions")
        lines.append("")
        
        # Memories
        lines.append("<<RELEVANT MEMORIES>>")
        if self.relevant_memories:
            for mem in self.relevant_memories[:max_memories]:
                kind = mem.get("kind", "unknown")
                content = mem.get("content", "")[:70]
                lines.append(f"  [{kind}] {content}")
        else:
            lines.append("No relevant memories")
        lines.append("")
        
        # Short-term state
        if self.short_term_state:
            lines.append("<<SHORT-TERM STATE>>")
            for key, value in self.short_term_state.items():
                val = str(value)[:50]
                lines.append(f"  {key}: {val}")
            lines.append("")
        
        return "\n".join(lines)
    
    def to_json(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "agent_name": self.agent_name,
            "mode": self.mode,
            "approval_mode": self.approval_mode,
            "new_posts": [p.to_dict() for p in self.new_posts],
            "new_comments": [c.to_dict() for c in self.new_comments],
            "recent_wake_cycles": self.recent_wake_cycles,
            "pending_actions": self.pending_actions,
            "relevant_memories": self.relevant_memories,
            "short_term_state": self.short_term_state,
        }


class ContextAssembler:
    """
    Assembles bounded context from various sources.
    Token usage is controlled by limiting array sizes.
    """
    
    # Token budget constants (rough estimates)
    MAX_POSTS = 5
    MAX_COMMENTS = 10
    MAX_MEMORIES = 10
    MAX_WAKE_CYCLES = 3
    MAX_PENDING_ACTIONS = 5
    
    def __init__(self, conn: sqlite3.Connection, config_mode: str = "dry_run"):
        self._conn = conn
        self._mode = config_mode
    
    def assemble(
        self,
        observations: ObservationResult,
        memory_query: Optional[str] = None,
    ) -> AssembledContext:
        """
        Assemble context from observations and database state.
        
        Args:
            observations: Latest observations from Facebook
            memory_query: Optional query string to find relevant memories
        
        Returns:
            AssembledContext with bounded content
        """
        ctx = AssembledContext(
            mode=self._mode,
            approval_mode=self._mode != "live",  # In dry_run, approval is on by default
        )
        
        # New posts (already marked as new by detector)
        ctx.new_posts = [p for p in observations.posts if p.is_new][:self.MAX_POSTS]
        
        # New comments (already marked as new)
        ctx.new_comments = [c for c in observations.comments if c.is_new][:self.MAX_COMMENTS]
        
        # If we have unreplied comments, they're relevant for REPLY decisions
        # Use database state for comments we need to respond to
        from .storage import get_comments_for_post, get_unreplied_comments
        try:
            unreplied_ids = set()
            for c in get_unreplied_comments(self._conn):
                unreplied_ids.add(c["fb_comment_id"])
                # Also include in new_comments if not already there
                existing = [oc for oc in ctx.new_comments if oc.fb_comment_id == c["fb_comment_id"]]
                if not existing and len(ctx.new_comments) < self.MAX_COMMENTS:
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
                    ctx.new_comments.insert(0, obs)
        except Exception:
            pass
        
        # Recent wake cycles - last N
        try:
            ctx.recent_wake_cycles = get_wake_cycles(self._conn, limit=self.MAX_WAKE_CYCLES)
        except Exception:
            ctx.recent_wake_cycles = []
        
        # Pending actions
        try:
            ctx.pending_actions = get_proposed_actions(self._conn, status="proposed")
            ctx.pending_actions = ctx.pending_actions[:self.MAX_PENDING_ACTIONS]
        except Exception:
            ctx.pending_actions = []
        
        # Relevant memories
        try:
            if memory_query:
                memories = self._get_relevant_memories(memory_query, self.MAX_MEMORIES)
            else:
                memories = get_memories(self._conn, limit=self.MAX_MEMORIES)
            ctx.relevant_memories = memories
        except Exception:
            ctx.relevant_memories = []
        
        # Short-term state
        try:
            from .storage import get_all_short_term
            short_term = get_all_short_term(self._conn)
            ctx.short_term_state = dict(short_term)
        except Exception:
            ctx.short_term_state = {}
        
        return ctx
    
    def _get_relevant_memories(self, query: str, limit: int) -> list[dict]:
        """Get memories relevant to the query using tags and keyword overlap."""
        memories = get_memories(self._conn, limit=limit * 3)  # Get extra to filter
        
        # Score memories by relevance
        scored = []
        query_lower = query.lower()
        
        for mem in memories:
            score = 0
            content = mem.get("content", "").lower()
            tags = mem.get("tags", "") or ""
            
            # Keyword overlap
            query_words = set(w for w in query_lower.split() if len(w) > 2)
            content_words = set(w for w in content.split() if len(w) > 2)
            score += len(query_words & content_words) * 2
            
            # Tag match
            tag_words = set(w.strip() for w in tags.split(",") if w.strip())
            if query_words & tag_words:
                score += 3
            
            # Recency (newer = more relevant)
            try:
                created = datetime.fromisoformat(mem.get("created_at", "").replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
                score -= age_hours / 24  # Decay over days
            except Exception:
                pass
            
            # Kind weighting
            kind = mem.get("kind", "")
            if kind == "lore":
                score += 1
            elif kind == "event":
                score += 0.5
            
            if score > 0:
                scored.append((score, mem))
        
        # Sort by score and return top N
        scored.sort(key=lambda x: x[0], reverse=True)
        return [mem for _, mem in scored[:limit]]


def build_context_prompt(ctx: AssembledContext) -> str:
    """Build the model prompt from assembled context."""
    return ctx.to_prompt_context()