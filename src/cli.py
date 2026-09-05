"""
CLI entry point for UnattendedBot8300.

Provides commands for wake cycles, status, actions, and execution.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .config import Config, load_config, validate_config
from .storage import Storage
from .fb_client import FacebookClient
from .memory import MemoryStore
from .proposed_actions import ProposedActionQueue, ActionType
from .safety import SafetyPolicy, SafetyResult
from .wake_cycles import WakeCycleLogger


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="unattendedbot8300",
        description="Autonomous AI agent for Facebook Page management"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # wake command
    wake_parser = subparsers.add_parser("wake", help="Start a wake cycle")
    wake_parser.add_argument("--dry-run", action="store_true", 
                             help="Run in dry-run mode (no Facebook writes)")
    
    # status command
    status_parser = subparsers.add_parser("status", help="Show agent status")
    
    # actions command
    actions_parser = subparsers.add_parser("actions", help="Manage proposed actions")
    actions_sub = actions_parser.add_subparsers(dest="action_cmd")
    
    actions_list = actions_sub.add_parser("list", help="List all actions")
    actions_list.add_argument("--pending", action="store_true", help="List pending only")
    
    actions_approve = actions_sub.add_parser("approve", help="Approve an action")
    actions_approve.add_argument("id", type=int, help="Action ID")
    
    actions_reject = actions_sub.add_parser("reject", help="Reject an action")
    actions_reject.add_argument("id", type=int, help="Action ID")
    
    # execute command
    execute_parser = subparsers.add_parser("execute", help="Execute an approved action")
    execute_parser.add_argument("id", type=int, help="Action ID")
    
    # memory command
    memory_parser = subparsers.add_parser("memory", help="View/add memories")
    memory_sub = memory_parser.add_subparsers(dest="mem_cmd")
    
    memory_list = memory_sub.add_parser("list", help="List memories")
    memory_list.add_argument("--kind", help="Filter by kind (lore, liking, dislike, etc.)")
    
    memory_add = memory_sub.add_parser("add", help="Add a memory")
    memory_add.add_argument("content", help="Memory content")
    memory_add.add_argument("--kind", default="lore", help="Memory kind")
    
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        sys.exit(1)
    
    if args.command == "wake":
        return handle_wake(args)
    elif args.command == "status":
        return handle_status()
    elif args.command == "actions":
        return handle_actions(args)
    elif args.command == "execute":
        return handle_execute(args)
    elif args.command == "memory":
        return handle_memory(args)
    
    return 0


def handle_wake(args) -> int:
    """Handle the wake command."""
    config = load_config()
    errors = validate_config(config)
    if errors:
        print("Configuration errors:")
        for e in errors:
            print(f"  - {e}")
        return 1
    
    # In Phase 0, wake is always dry-run
    print("=== Wake Cycle Started ===")
    print(f"Mode: dry_run (Phase 0)")
    print()
    
    storage = Storage(config)
    conn = storage.connect()
    
    # 1. Observe Facebook state
    print("1. Observing Facebook state...")
    fb = FacebookClient(config)
    
    try:
        page_info = fb.get_page_info()
        print(f"   Page: {page_info.get('name', 'Unknown')} ({page_info.get('id', '')})")
    except Exception as e:
        print(f"   Warning: Could not fetch page info: {e}")
        page_info = {}
    
    # Get posts
    try:
        posts_resp = fb.get_my_posts(limit=10)
        from .fb_client import extract_posts_from_response
        posts = extract_posts_from_response(posts_resp)
        print(f"   Found {len(posts)} recent posts")
        
        # Store posts
        from src.storage import store_fb_posts
        store_fb_posts(conn, posts)
        
        # Get comments for recent posts
        for post in posts[:3]:  # Get comments for up to 3 recent posts
            comments_resp = fb.get_post_comments(post["fb_post_id"], limit=20)
            from .fb_client import extract_comments_from_response
            comments = extract_comments_from_response(comments_resp, post["fb_post_id"])
            from .storage import store_fb_comments
            store_fb_comments(conn, comments)
            print(f"   Post {post['fb_post_id']}: {len(comments)} comments")
    except Exception as e:
        print(f"   Warning: Could not fetch posts/comments: {e}")
    
    # 2. Retrieve relevant memory
    print("\n2. Retrieving memory context...")
    memory = MemoryStore(conn)
    recent_lore = memory.recall(kind="lore", limit=5)
    print(f"   Found {len(recent_lore)} lore memories")
    
    recent_commenters = memory.recall(kind="event", limit=5)
    print(f"   Found {len(recent_commenters)} event memories")
    
    # 3. Reason about potential actions
    print("\n3. Reasoning about actions...")
    
    # Get recent comments that could be replied to
    from .storage import get_unreplied_comments, get_comments_for_post
    repliable = []
    for post in posts[:5]:
        comments = get_comments_for_post(conn, post["fb_post_id"])
        for c in comments:
            if not c.get("parent_comment_id"):  # Not already a reply
                repliable.append(c)
    
    # Simple reasoning: propose reply to most recent comment
    action_proposed = None
    proposed_action_id = None
    reason = ""
    
    if repliable:
        latest = repliable[0]
        from_name = latest.get("from_name", "Someone")
        msg = latest.get("message", "")[:50]
        if len(msg) > 50:
            msg += "..."
        
        reason = f"Would reply to {from_name}: '{msg}'"
        
        # In Phase 0, we just propose a "NOTHING" decision since we're dry-run
        # The actual proposed action would come from the model in full implementation
        action_proposed = ActionType.NOTHING.value
        reason = "Dry-run mode: observed activity but not proposing actionable change"
        
    else:
        action_proposed = ActionType.NOTHING.value
        reason = "No comments to reply to; agent decides no action is warranted"
    
    # Record in wake cycle
    logger = WakeCycleLogger(conn)
    cycle = logger.record(
        decision=action_proposed,
        observation_summary=f"Found {len(posts)} posts, {len(repliable)} potentially repliable comments",
        decision_summary=reason
    )
    
    print(f"   Decision: {action_proposed}")
    print(f"   Reason: {reason}")
    
    # 4. In Phase 0, we don't actually execute - all actions stay proposed
    print("\n4. In Phase 0 (dry-run mode), no actions are executed.")
    print(f"   Wake cycle ID: {cycle.id}")
    
    storage.close()
    print("\n=== Wake Cycle Complete ===")
    return 0


def handle_status() -> int:
    """Handle the status command."""
    config = load_config()
    storage = Storage(config)
    conn = storage.connect()
    
    print("=== UnattendedBot8300 Status ===\n")
    
    # Configuration
    print("Configuration:")
    print(f"  Mode: {config.unattended_bot_mode}")
    print(f"  Approval mode: {config.approval_mode}")
    print(f"  API Version: {config.facebook_graph_api_version}")
    print()
    
    # Recent wake cycles
    from .wake_cycles import WakeCycleLogger, get_cycle_stats
    logger = WakeCycleLogger(conn)
    recent = logger.get_recent(5)
    
    print("Recent Wake Cycles:")
    if recent:
        for c in recent:
            print(f"  {c.started_at}: {c.decision} - {c.decision_summary[:60]}")
    else:
        print("  No wake cycles recorded yet")
    print()
    
    # Pending actions
    queue = ProposedActionQueue(conn)
    pending = queue.list_pending()
    
    print("Pending Actions:")
    if pending:
        for a in pending:
            print(f"  ID {a['id']}: {a['action_type']} - {a['reason'][:60]}")
    else:
        print("  No pending actions")
    print()
    
    # Stats
    stats = get_cycle_stats(conn)
    print("Statistics:")
    print(f"  Total wake cycles: {stats['total_cycles']}")
    print(f"  POST decisions: {stats['post_cycles']}")
    print(f"  REPLY decisions: {stats['reply_cycles']}")
    print(f"  MEMORY decisions: {stats['memory_cycles']}")
    print(f"  NOTHING decisions: {stats['nothing_cycles']}")
    
    storage.close()
    return 0


def handle_actions(args) -> int:
    """Handle the actions command."""
    config = load_config()
    storage = Storage(config)
    conn = storage.connect()
    queue = ProposedActionQueue(conn)
    
    if args.action_cmd == "list":
        actions = queue.list_pending() if args.pending else queue.list_all()
        
        print("Proposed Actions Queue:")
        print("-" * 80)
        if actions:
            for a in actions:
                print(f"ID: {a['id']}")
                print(f"  Type: {a['action_type']}")
                print(f"  Status: {a['status']}")
                print(f"  Reason: {a['reason']}")
                if a.get('payload'):
                    payload_preview = a['payload'][:100]
                    print(f"  Payload: {payload_preview}...")
                print(f"  Created: {a['created_at']}")
                if a.get('approved_at'):
                    print(f"  Approved: {a['approved_at']} by {a.get('approved_by', 'unknown')}")
                print()
        else:
            print("  No actions found\n")
    
    elif args.action_cmd == "approve":
        success = queue.approve(args.id)
        if success:
            print(f"Approved action {args.id}")
        else:
            print(f"Failed to approve action {args.id} (not found or not proposed)")
        return 1 if not success else 0
    
    elif args.action_cmd == "reject":
        success = queue.reject(args.id)
        if success:
            print(f"Rejected action {args.id}")
        else:
            print(f"Failed to reject action {args.id} (not found or not proposed)")
        return 1 if not success else 0
    
    storage.close()
    return 0


def handle_execute(args) -> int:
    """Handle the execute command."""
    config = load_config()
    
    # Phase 0 safety: do not actually execute Facebook writes
    if config.unattended_bot_mode == "dry_run":
        print("WARNING: Dry-run mode enabled. Actions will NOT be executed on Facebook.")
        print("To enable live execution, set UNATTENDED_BOT_MODE=live in .env")
        return 1
    
    storage = Storage(config)
    conn = storage.connect()
    queue = ProposedActionQueue(conn)
    
    action = queue.get_by_id(args.id)
    if not action:
        print(f"Action {args.id} not found")
        return 1
    
    if action.action_type != ActionType.REPLY.value and action.action_type != ActionType.POST.value:
        print(f"Cannot execute action type: {action.action_type}")
        return 1
    
    print(f"Executing action {args.id}: {action.action_type}")
    print(f"Reason: {action.reason}")
    
    fb = FacebookClient(config)
    fb_result_id = None
    error = None
    
    try:
        if action.action_type == ActionType.POST.value:
            message = action.payload.get("message", "")
            result = fb.post_status(message)
            fb_result_id = result.get("id")
            print(f"Posted to Facebook: {fb_result_id}")
        
        elif action.action_type == ActionType.REPLY.value:
            message = action.payload.get("message", "")
            comment_id = action.target_fb_object
            result = fb.reply_to_comment(comment_id, message)
            fb_result_id = result.get("id")
            print(f"Replied on Facebook: {fb_result_id}")
    
    except Exception as e:
        error = str(e)
        print(f"Error: {e}")
    
    queue.execute(args.id, fb_result_id, error)
    
    storage.close()
    return 0 if not error else 1


def handle_memory(args) -> int:
    """Handle the memory command."""
    config = load_config()
    storage = Storage(config)
    conn = storage.connect()
    memory = MemoryStore(conn)
    
    if args.mem_cmd == "list":
        memories = memory.recall(kind=args.kind) if args.kind else memory.recall()
        print("Memories:")
        if memories:
            for m in memories:
                kind = m.get("kind", "unknown")
                content = m.get("content", "")[:80]
                print(f"  [{kind}] {content}")
        else:
            print("  No memories recorded")
    
    elif args.mem_cmd == "add":
        mid = memory.remember(args.kind, args.content)
        print(f"Added memory #{mid}")
    
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())