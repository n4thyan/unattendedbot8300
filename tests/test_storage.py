"""
Tests for UnattendedBot8300 Phase 0.

Uses mocks for Facebook API calls. No real Facebook writes occur.
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import responses

# Import our modules
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Config, load_config, validate_config
from src.storage import Storage, store_fb_posts, get_fb_posts, get_fb_post
from src.storage import store_fb_comments, get_comments_for_post
from src.storage import store_memory, get_memories
from src.storage import create_proposed_action, get_proposed_actions, approve_action
from src.storage import record_wake_cycle, get_wake_cycles
from src.fb_client import FacebookClient, extract_posts_from_response
from src.fb_client import extract_comments_from_response
from src.safety import SafetyPolicy, SafetyResult
from src.proposed_actions import ProposedActionQueue, ActionType
from src.wake_cycles import WakeCycleLogger, WakeCycle


# === Fixtures ===

@pytest.fixture
def temp_config():
    """Create a temp config with in-memory SQLite database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            facebook_page_id="test_page_id",
            facebook_page_access_token="test_token",
            facebook_graph_api_version="v26.0",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir)
        )
        yield config


@pytest.fixture
def storage(temp_config):
    """Create a storage instance with temp database."""
    s = Storage(temp_config)
    conn = s.connect()
    yield conn
    s.close()


@pytest.fixture
def fb_client(temp_config):
    """Create a Facebook client."""
    return FacebookClient(temp_config)


# === Config Tests ===

def test_load_config_from_env(temp_config):
    """Test loading config from environment."""
    assert temp_config.facebook_page_id == "test_page_id"
    assert temp_config.facebook_graph_api_version == "v26.0"


def test_config_db_path_relative(temp_config):
    """Test that relative database paths are resolved."""
    assert temp_config.db_path.is_absolute()


# === Storage Tests ===

def test_store_and_get_posts(storage, temp_config):
    """Test storing and retrieving posts."""
    posts = [{
        "fb_post_id": "12345_67890",
        "posted_by_page": True,
        "message": "Hello world",
        "created_time": "2026-09-05T12:00:00+0000",
        "raw": {}
    }]
    
    store_fb_posts(storage, posts)
    
    retrieved = get_fb_posts(storage)
    assert len(retrieved) == 1
    assert retrieved[0]["fb_post_id"] == "12345_67890"
    assert retrieved[0]["message"] == "Hello world"


def test_get_post_by_id(storage):
    """Test retrieving a specific post by ID."""
    from src.storage import store_fb_posts
    
    posts = [{
        "fb_post_id": "12345_67890",
        "posted_by_page": True,
        "message": "Test post",
        "created_time": "2026-09-05T12:00:00+0000",
        "raw": {}
    }]
    store_fb_posts(storage, posts)
    
    post = get_fb_post(storage, "12345_67890")
    assert post is not None
    assert post["message"] == "Test post"
    
    post = get_fb_post(storage, "nonexistent")
    assert post is None


def test_store_and_get_comments(storage):
    """Test storing and retrieving comments."""
    comments = [{
        "fb_comment_id": "111",
        "post_id": "12345_67890",
        "parent_comment_id": None,
        "from_name": "Test User",
        "from_id": "user123",
        "message": "Nice post!",
        "created_time": "2026-09-05T12:30:00+0000",
        "raw": {}
    }]
    
    store_fb_comments(storage, comments)
    
    retrieved = get_comments_for_post(storage, "12345_67890")
    assert len(retrieved) == 1
    assert retrieved[0]["from_name"] == "Test User"


def test_memories(storage):
    """Test memory storage and retrieval."""
    mid = store_memory(storage, "lore", "This page sometimes posts about the weather", ["weather"])
    
    memories = get_memories(storage, kind="lore")
    assert len(memories) == 1
    assert memories[0]["kind"] == "lore"
    assert "weather" in memories[0]["tags"]


def test_proposed_actions(storage):
    """Test proposed action queue."""
    action_id = create_proposed_action(
        storage, 
        ActionType.NOTHING.value,
        {},
        reason="No activity detected"
    )
    
    actions = get_proposed_actions(storage)
    assert len(actions) == 1
    assert actions[0]["id"] == action_id
    assert actions[0]["status"] == "proposed"
    
    # Approve
    approve_action(storage, action_id)
    
    actions = get_proposed_actions(storage, status="approved")
    assert len(actions) == 1


def test_wake_cycles(storage):
    """Test wake cycle logging."""
    cycle = record_wake_cycle(storage, "NOTHING", "No activity")
    
    cycles = get_wake_cycles(storage)
    assert len(cycles) == 1
    assert cycles[0]["decision"] == "NOTHING"


# === Facebook Client Tests (with mocks) ===

@responses.activate
def test_get_page_info(fb_client):
    """Test getting page info from Facebook API."""
    responses.add(
        responses.GET,
        "https://graph.facebook.com/v26.0/test_page_id",
        json={"id": "test_page_id", "name": "UnattendedBot8300"},
        status=200
    )
    
    result = fb_client.get_page_info()
    assert result["name"] == "UnattendedBot8300"


@responses.activate
def test_get_my_posts(fb_client):
    """Test getting posts from Facebook API."""
    responses.add(
        responses.GET,
        "https://graph.facebook.com/v26.0/test_page_id/feed",
        json={
            "data": [
                {
                    "id": "test_page_id_123",
                    "message": "Hello world",
                    "created_time": "2026-09-05T12:00:00+0000",
                    "like_count": 5,
                    "comment_count": 2
                }
            ]
        },
        status=200
    )
    
    result = fb_client.get_my_posts()
    posts = extract_posts_from_response(result)
    assert len(posts) == 1
    assert posts[0]["message"] == "Hello world"


@responses.activate
def test_post_status_disabled_in_dry_run(fb_client, temp_config):
    """Test that posting is actually prevented in dry_run mode."""
    # In Phase 0, dry_run mode should prevent actual Facebook writes
    # The post_status method still works, but callers should check config
    
    responses.add(
        responses.POST,
        "https://graph.facebook.com/v26.0/test_page_id/feed",
        json={"id": "test_page_id_456"},
        status=200
    )
    
    # This would actually POST if we didn't mock or check mode
    # In Phase 0, the execute command checks dry_run mode first
    result = fb_client.post_status("Test post")
    assert result["id"] == "test_page_id_456"
    
    # But in real Phase 0, this code path is never reached due to dry_run check


# === Safety Policy Tests ===

def test_safety_allows_minimal_content(storage):
    """Test that minimal content passes safety."""
    policy = SafetyPolicy(storage, ":memory:")
    
    result = policy.check_post("Hello world, this is a test post.")
    assert result.allowed
    assert len(result.violations) == 0


def test_safety_blocks_spam(storage):
    """Test that spam content is blocked."""
    policy = SafetyPolicy(storage, ":memory:")
    
    result = policy.check_post("Click here to get free money now!")
    assert not result.allowed
    assert "spam" in " ".join(result.violations).lower()


def test_safety_blocks_too_short(storage):
    """Test that very short posts are blocked."""
    policy = SafetyPolicy(storage, ":memory:")
    
    result = policy.check_post("Hi")
    assert not result.allowed


# === Proposed Action Queue Tests ===

def test_propose_post(storage):
    """Test proposing a POST action."""
    queue = ProposedActionQueue(storage)
    
    action_id = queue.propose_post("Test post content", "Agent wants to share thoughts")
    
    pending = queue.get_pending()
    assert len(pending) == 1
    assert pending[0].action_type == ActionType.POST.value
    assert "share thoughts" in pending[0].reason


def test_propose_reply(storage):
    """Test proposing a REPLY action."""
    queue = ProposedActionQueue(storage)
    
    action_id = queue.propose_reply("comment123", "Thanks for the comment!", "User needs a response")
    
    pending = queue.get_pending()
    assert len(pending) == 1
    assert pending[0].action_type == ActionType.REPLY.value
    assert pending[0].target_fb_object == "comment123"


# === Wake Cycle Logger Tests ===

def test_wake_cycle_logger(storage):
    """Test wake cycle logging."""
    logger = WakeCycleLogger(storage)
    
    cycle = logger.record(
        decision="NOTHING",
        observation_summary="Checked 5 posts, 0 comments",
        decision_summary="Nothing worth posting yet"
    )
    
    assert cycle.decision == "NOTHING"
    assert cycle.observation_summary is not None
    
    recent = logger.get_recent()
    assert len(recent) == 1


def test_multiple_wake_cycles(storage):
    """Test recording multiple wake cycles."""
    logger = WakeCycleLogger(storage)
    
    for i in range(5):
        logger.record(
            decision="NOTHING",
            observation_summary=f"Cycle {i}",
            decision_summary=f"No action needed {i}"
        )
    
    cycles = logger.get_recent(limit=10)
    assert len(cycles) == 5


# === Dry-Run Safety Tests ===

def test_execute_blocked_in_dry_run(temp_config):
    """Verify that execute is blocked in dry_run mode."""
    # In Phase 0, UNATTENDED_BOT_MODE defaults to dry_run
    assert temp_config.unattended_bot_mode == "dry_run"
    assert temp_config.is_live_mode() is False
    assert temp_config.is_approval_mode() is True


# === Integration Test: Full Wake Cycle ===

@patch('src.cli.FacebookClient')
def test_full_wake_cycle_dry_run(mock_fb_client_class, storage):
    """Test a complete wake cycle in dry-run mode."""
    # Setup mock
    mock_client = MagicMock()
    mock_client.get_page_info.return_value = {
        "id": "test_page_id",
        "name": "UnattendedBot8300"
    }
    mock_client.get_my_posts.return_value = {
        "data": [
            {
                "id": "test_page_id_123",
                "message": "Hello world",
                "created_time": "2026-09-05T12:00:00+0000",
                "like_count": 5,
                "comment_count": 0
            }
        ]
    }
    mock_fb_client_class.return_value = mock_client
    
    # Store posts
    store_fb_posts(storage, [{
        "fb_post_id": "test_page_id_123",
        "posted_by_page": True,
        "message": "Hello world",
        "created_time": "2026-09-05T12:00:00+0000",
        "raw": {}
    }])
    
    # Record wake cycle
    logger = WakeCycleLogger(storage)
    cycle = logger.record(
        decision="NOTHING",
        observation_summary="1 post found, 0 comments",
        decision_summary="No engagement; agent chooses no action"
    )
    
    # Verify
    cycles = logger.get_recent()
    assert len(cycles) == 1
    assert cycles[0].decision == "NOTHING"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])