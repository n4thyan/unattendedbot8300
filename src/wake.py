"""
Model-driven wake cycle module for UnattendedBot8300.

Implements the core pipeline:
observe -> context -> memory -> model decision -> validate -> safety -> proposed-action queue -> wake-cycle logging

This module orchestrates the decision-making process.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import sqlite3

from .config import Config, load_config
from .storage import Storage, get_unreplied_comments
from .fb_client import FacebookClient, extract_posts_from_response, extract_comments_from_response
from .memory import MemoryStore
from .proposed_actions import ProposedActionQueue, ActionType
from .safety import SafetyPolicy, SafetyResult
from .wake_cycles import WakeCycleLogger
from .fb_observations import ObservationCollector, ObservationResult, PostObservation, CommentObservation
from .context_assembler import ContextAssembler, AssembledContext, build_context_prompt
from .decision import (
    DecisionModel, ActionType as DecisionAction,
    validate_decision, make_nothing_decision, make_post_decision,
    make_reply_decision, make_memory_decision
)


@dataclass
class WakeResult:
    """Result of a wake cycle."""
    success: bool
    cycle_id: int
    decision: DecisionModel
    proposed_action_id: Optional[int]
    observation_summary: str
    error: Optional[str] = None


class WakeOrchestrator:
    """
    Orchestrates the full wake cycle:
    observe -> context -> memory -> model decision -> validation -> safety -> queue -> log
    """
    
    def __init__(self, config: Optional[Config] = None):
        self.config = config or load_config()
        self._storage = Storage(self.config)
        self._conn: Optional[sqlite3.Connection] = None
    
    def _connect(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._conn is None:
            self._conn = self._storage.connect()
        return self._conn
    
    def _close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
        self._storage.close()
    
    def wake(self) -> WakeResult:
        """
        Execute a full wake cycle.
        
        Returns:
            WakeResult with the decision and any proposed action
        """
        conn = self._connect()
        
        # 1. OBSERVE - fetch Facebook state (using mocks in dry_run mode)
        observations = self._observe(conn)
        
        # 2. CONTEXT - assemble bounded context
        assembler = ContextAssembler(conn, self.config.unattended_bot_mode)
        context = assembler.assemble(observations, memory_query="agent page activity")
        
        # 3. MEMORY - retrieve relevant memories
        memory = MemoryStore(conn)
        relevant_memories = memory.get_relevant_memories("page activity", 5)
        
        # 4. DECISION - the model makes the decision
        # In Phase 1, this is simplified for testing
        # Full model integration would go here
        decision = self._make_decision(context, memory, observations, conn)
        
        # 5. VALIDATE - ensure decision is well-formed
        decision, error = validate_decision(decision.to_dict())
        if error:
            # Record failed validation
            logger = WakeCycleLogger(conn)
            logger.record(
                decision="ERROR",
                observation_summary=observations.get_summary(),
                decision_summary=f"Decision validation failed: {error}",
            )
            return WakeResult(
                success=False,
                cycle_id=0,
                decision=make_nothing_decision(f"Validation error: {error}"),
                proposed_action_id=None,
                observation_summary=observations.get_summary(),
                error=error,
            )
        
        # 6. SAFETY - check rate limits, spam, duplicates
        safety = SafetyPolicy(conn, str(self.config.db_path))
        safety_result = self._check_safety(safety, decision)
        
        # Store safety result in decision
        if decision.safety_result is None:
            decision.safety_result = {
                "allowed": safety_result.allowed,
                "violations": safety_result.violations,
                "message": safety_result.message,
            }.copy()
        else:
            decision.safety_result["allowed"] = safety_result.allowed
            decision.safety_result["violations"] = safety_result.violations
            decision.safety_result["message"] = safety_result.message
        
        if not safety_result.allowed:
            decision = make_nothing_decision(f"Safety blocked: {safety_result.message}")
        
        # 7. QUEUE - propose action or record NOTHING
        action_queue = ProposedActionQueue(conn)
        proposed_action_id = None
        
        if decision.action_type == DecisionAction.POST:
            proposed_action_id = self._handle_post_decision(action_queue, decision)
        elif decision.action_type == DecisionAction.REPLY:
            proposed_action_id = self._handle_reply_decision(action_queue, decision, conn)
        elif decision.action_type == DecisionAction.MEMORY:
            proposed_action_id = self._handle_memory_decision(action_queue, decision)
        elif decision.action_type == DecisionAction.NOTHING:
            # NOTHING is a valid decision - just record it
            pass
        elif decision.action_type == DecisionAction.EXPLORE:
            # Future placeholder - not implemented yet
            decision = make_nothing_decision("EXPLORE not yet implemented in Phase 1")
        
        # 8. LOG - record wake cycle
        logger = WakeCycleLogger(conn)
        wc = logger.record(
            decision=decision.action_type.value,
            observation_summary=observations.get_summary(),
            proposed_action_id=proposed_action_id,
            decision_summary=decision.decision_summary,
        )
        
        self._close()
        
        return WakeResult(
            success=True,
            cycle_id=wc.id,
            decision=decision,
            proposed_action_id=proposed_action_id,
            observation_summary=observations.get_summary(),
        )
    
    def _observe(self, conn: sqlite3.Connection) -> ObservationResult:
        """Observe Facebook state using the configured transport.

        In dry_run mode the camoufox_ui transport still reads live data
        (it is read-only by design), but no writes are ever attempted.
        The graph_api transport is dormant during development.
        """
        from .facebook_transport import get_transport, TransportError

        transport_name = self.config.facebook_transport

        if transport_name == "camoufox_ui":
            # Experimental browser UI transport (read-only)
            try:
                transport = get_transport(self.config)
                return transport.observe()
            except TransportError:
                # Transport-level failure (login needed, checkpoint, etc.)
                # Return empty observations so the wake cycle can still log
                # the issue and record a NOTHING decision.
                return ObservationResult()
        else:
            # graph_api transport (dormant during development)
            collector = ObservationCollector(conn)

            if self.config.is_live_mode():
                # Live mode - fetch from actual Facebook API
                fb = FacebookClient(self.config)
                try:
                    page_info = fb.get_page_info()
                    posts_resp = fb.get_my_posts(limit=10)

                    posts = extract_posts_from_response(posts_resp)
                    comments = []
                    for post in posts[:3]:
                        comments_resp = fb.get_post_comments(post["fb_post_id"], limit=20)
                        comments.extend(extract_comments_from_response(comments_resp, post["fb_post_id"]))

                    return ObservationResult(
                        posts=[PostObservation(
                            fb_post_id=p.get("fb_post_id", p.get("id")),
                            message=p.get("message", ""),
                            created_time=p.get("created_time", ""),
                            posted_by_page=p.get("posted_by_page", False),
                            like_count=p.get("like_count", 0),
                            comment_count=p.get("comment_count", 0),
                            raw=p,
                        ) for p in posts],
                        comments=[CommentObservation(
                            fb_comment_id=c.get("fb_comment_id", c.get("id")),
                            post_id=c.get("post_id"),
                            message=c.get("message", ""),
                            from_name=c.get("from_name", "Someone"),
                            from_id=c.get("from_id", ""),
                            created_time=c.get("created_time", ""),
                            parent_comment_id=c.get("parent_comment_id"),
                            like_count=c.get("like_count", 0),
                            raw=c,
                        ) for c in comments],
                    )
                except Exception as e:
                    # Return empty observations on error
                    return ObservationResult()
            else:
                # Dry-run mode - use fixtures/mocks
                return collector.collect_from_fixture()
    
    def _make_decision(
        self,
        context: AssembledContext,
        memory: MemoryStore,
        observations: ObservationResult,
        conn: sqlite3.Connection,
    ) -> DecisionModel:
        """
        Make a model decision based on context.
        
        In Phase 1, this is a simplified rule-based placeholder
        that will be replaced by actual Hermes/model interaction.
        """
        # Check for unreplied comments
        if observations.unreplied_comments:
            latest = observations.unreplied_comments[0]
            reason = f"Would reply to {latest.from_name}: '{latest.message[:50]}...'"
            
            # Simple heuristic: if we have unread comments and no pending replies, propose one
            action_queue = ProposedActionQueue(conn)
            pending_replies = [a for a in action_queue.get_pending() 
                              if a.action_type == "REPLY"]
            
            if not pending_replies and self.config.is_approval_mode():
                # Propose a reply in approval mode
                reply_msg = f"Thanks for your comment, {latest.from_name}! {latest.message[:30]}..."
                return make_reply_decision(latest.fb_comment_id, reply_msg, reason)
        
        # Check for potential POST opportunities
        new_posts = [p for p in observations.posts if p.is_new]
        if new_posts and self.config.is_approval_mode():
            # Look for meaningful content
            for post in new_posts:
                if len(post.message) > 50 and "?" not in post.message[:20]:
                    # Could post something related
                    pass
        
        # Default: NOTHING
        reason = "No action warranted based on observations and history"
        return make_nothing_decision(reason)
    
    def _check_safety(self, safety: SafetyPolicy, decision: DecisionModel) -> SafetyResult:
        """Run safety checks on a decision."""
        if decision.action_type == DecisionAction.POST and decision.message:
            return safety.check_post(decision.message)
        elif decision.action_type == DecisionAction.REPLY and decision.message:
            # REPLY safety would need commenter info
            return safety.check_reply(decision.message, "unknown", "Unknown")
        else:
            return safety.check_rate_limits(decision.action_type.value)
    
    def _handle_post_decision(
        self, queue: ProposedActionQueue, decision: DecisionModel
    ) -> int:
        """Handle a POST decision."""
        message = decision.message or ""
        reason = decision.decision_summary
        return queue.propose_post(message, reason)
    
    def _handle_reply_decision(
        self, queue: ProposedActionQueue, decision: DecisionModel,
        conn: sqlite3.Connection
    ) -> int:
        """Handle a REPLY decision."""
        response = decision.message or ""
        reason = decision.decision_summary
        
        # Check if target comment exists and is unread
        target_id = decision.target_object_id or ""
        
        return queue.propose_reply(target_id, response, reason)
    
    def _handle_memory_decision(
        self, queue: ProposedActionQueue, decision: DecisionModel
    ) -> int:
        """Handle a MEMORY decision."""
        kind = decision.memory_kind or "lore"
        content = decision.memory_content or ""
        tags = decision.memory_tags or []
        reason = decision.decision_summary
        return queue.propose_memory(kind, content, tags, reason)


def run_wake_cycle(config: Optional[Config] = None) -> WakeResult:
    """
    Run a single wake cycle.
    
    This is the main entry point for the wake functionality.
    """
    orchestrator = WakeOrchestrator(config)
    return orchestrator.wake()


def run_wake_with_model(
    config: Config,
    model_output: str,
) -> WakeResult:
    """
    Run a wake cycle with model-provided decision.
    
    Args:
        config: Configuration
        model_output: JSON string from the model containing the decision
    
    Returns:
        WakeResult with the processed decision
    """
    orchestrator = WakeOrchestrator(config)
    conn = orchestrator._connect()
    
    # 1. OBSERVE
    observations = orchestrator._observe(conn)
    
    # 2. CONTEXT
    assembler = ContextAssembler(conn, config.unattended_bot_mode)
    context = assembler.assemble(observations)
    
    # 3. MEMORY
    memory_store = MemoryStore(conn)
    memories = memory_store.get_relevant_memories("agent page activity", 5)
    
    # 4. Parse model decision
    try:
        decision_data = json.loads(model_output)
        decision, error = validate_decision(decision_data)
        if error:
            decision = make_nothing_decision(f"Model output invalid: {error}")
    except json.JSONDecodeError as e:
        decision = make_nothing_decision(f"Model output not valid JSON: {e}")
    
    # 5-8. Continue with existing logic
    return orchestrator.wake()