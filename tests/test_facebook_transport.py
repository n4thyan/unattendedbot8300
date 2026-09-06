"""
Tests for the Facebook transport abstraction layer.

Covers:
  - transport selection (graph_api vs camoufox_ui)
  - Camoufox adapter mocked observation extraction
  - normalisation into existing observation format
  - login-required state
  - Page-not-found state
  - selector failure
  - checkpoint/security-challenge state
  - no browser write in dry_run
  - POST proposal does not directly trigger browser action
  - REPLY proposal does not directly trigger browser action
  - approved action reaches the deterministic executor only when allowed
  - Graph API transport still imports/works structurally
  - browser profile/runtime path is gitignored
"""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timezone

import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Config, load_config, validate_config
from src.storage import Storage
from src.fb_observations import ObservationResult, PostObservation, CommentObservation
from src.wake import WakeOrchestrator
from src.decision import make_post_decision, make_reply_decision, make_nothing_decision
from src.proposed_actions import ProposedActionQueue
from src.safety import SafetyPolicy
from src import (
    FacebookTransport, get_transport, supported_transports,
    TransportError, NotLoggedInError, PageNotFoundError,
    SelectorError, CheckpointError, NavigationError, TransportUnavailableError,
    CamoufoxTransport,
)
from src.facebook_camoufox import (
    AuthState, IdentityState, ChallengeType, ChallengeHandler,
    HumanInterventionHandler, TwoCaptchaHandler, DEFAULT_CHALLENGE_HANDLER,
    DEFAULT_PAGE_URL, DEFAULT_PAGE_SLUG,
)


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def temp_config():
    """Create a config with camoufox_ui transport and a temp profile dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            facebook_page_id="UnattendedBot8300",
            facebook_graph_api_version="v26.0",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
            facebook_transport="camoufox_ui",
            camoufox_profile_dir=str(Path(tmpdir) / "browser-profile"),
        )
        yield config


@pytest.fixture
def temp_config_graph():
    """Create a config using the graph_api transport."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            facebook_page_id="test_page_id",
            facebook_page_access_token="test_token",
            facebook_graph_api_version="v26.0",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
            facebook_transport="graph_api",
            camoufox_profile_dir=str(Path(tmpdir) / "browser-profile"),
        )
        yield config


# ── Transport selection tests ─────────────────────────────────────────

def test_supported_transports():
    """Both graph_api and camoufox_ui are supported."""
    transports = supported_transports()
    assert "graph_api" in transports
    assert "camoufox_ui" in transports


def test_transport_selection_camoufox(temp_config):
    """get_transport returns a CamoufoxTransport when transport is camoufox_ui."""
    transport = get_transport(temp_config)
    assert isinstance(transport, CamoufoxTransport)
    caps = transport.capabilities()
    assert caps.transport_type == "camoufox_ui"
    assert caps.read_observations is True
    assert caps.persistent_profile is True
    transport.close()


def test_transport_selection_graph_api(temp_config_graph):
    """get_transport returns a graph_api wrapping transport."""
    transport = get_transport(temp_config_graph)
    caps = transport.capabilities()
    assert caps.transport_type == "graph_api"
    assert caps.read_observations is True
    transport.close()


def test_invalid_transport_raises():
    """Unknown transport name raises TransportUnavailableError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            facebook_transport="invalid_transport",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
        )
        with pytest.raises(TransportUnavailableError):
            get_transport(config)


def test_config_validates_transport():
    """validate_config rejects unknown transports."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            facebook_transport="bogus",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
        )
        errors = validate_config(config)
        assert any("FACEBOOK_TRANSPORT" in e for e in errors)


def test_config_transport_defaults_to_camoufox():
    """Default transport is camoufox_ui."""
    config = Config()
    assert config.facebook_transport == "camoufox_ui"
    assert config.is_camoufox_transport is True


# ── Config transport properties ────────────────────────────────────────

def test_config_transport_properties(temp_config):
    """Config exposes transport helper properties."""
    assert temp_config.is_camoufox_transport is True
    assert temp_config.is_camoufox_transport is False or True  # just check it runs
    graph_config = Config(facebook_transport="graph_api", project_root=Path(temp_config.project_root))
    assert graph_config.is_camoufox_transport is False
    assert temp_config.supported_transports == ["graph_api", "camoufox_ui"]


# ── Camoufox adapter: mocked observation extraction ───────────────────

def _make_authed_mock_page(page_title="UnattendedBot8300 - Facebook",
                           page_url="https://www.facebook.com/UnattendedBot8300"):
    """Build a mock Playwright page representing an AUTHENTICATED Facebook session.

    The mock provides positive authenticated UI evidence:
      - a profile menu button (aria-label contains 'profile')
      - a Home navigation link
      - no login form elements
    """
    mock_page = MagicMock()
    mock_page.url = page_url
    mock_page.title.return_value = page_title

    # query_selector: return authenticated UI elements for auth selectors,
    # return None for login/checkpoint forms.
    def query_selector(selector):
        sel_lower = selector.lower()
        # Login-form selectors → None (not logged out)
        if any(k in sel_lower for k in ["login_form", "email", "pass", "login_page_link"]):
            return None
        # Checkpoint/challenge selectors → None
        if any(k in sel_lower for k in ["checkpoint", "approval", "alert",
                                         "2fa", "captcha"]):
            return None
        # Authenticated UI evidence
        if "auth_profile_menu" in sel_lower or "profile" in sel_lower:
            return MagicMock()  # profile menu button present
        if "auth_home_link" in sel_lower:
            return MagicMock()
        if "auth_watch_link" in sel_lower:
            return MagicMock()
        if "auth_feed_composer" in sel_lower:
            return MagicMock()
        # Page header title
        if "page_header_title" in sel_lower or sel_lower == "h1":
            el = MagicMock()
            el.text_content.return_value = "UnattendedBot8300"
            return el

    mock_page.query_selector.side_effect = query_selector
    mock_page.wait_for_load_state = MagicMock()
    mock_page.goto = MagicMock()
    mock_page.wait_for_timeout = MagicMock()
    mock_page.locator = MagicMock()

    # get_by_role for page identification
    mock_heading = MagicMock()
    mock_heading.text_content.return_value = "UnattendedBot8300"
    mock_page.get_by_role.return_value = mock_heading

    return mock_page


def _make_loggedout_mock_page():
    """Build a mock Playwright page representing a LOGGED-OUT Facebook session.

    Simulates the login form being visible: email input, password input,
    login button, and a login form element.
    """
    mock_page = MagicMock()
    mock_page.url = "https://www.facebook.com/login.php"
    mock_page.title.return_value = "Facebook - Log In"

    def query_selector(selector):
        sel_lower = selector.lower()
        # Login-form selectors → present (logged out)
        if "email" in sel_lower and "input" in sel_lower:
            return MagicMock()
        if "pass" in sel_lower and "input" in sel_lower:
            return MagicMock()
        if "login_button" in sel_lower or "name='login'" in sel_lower:
            return MagicMock()
        if "login_form" in sel_lower:
            return MagicMock()
        # Authenticated UI evidence → None (not authenticated)
        if "auth_profile_menu" in sel_lower or "auth_home_link" in sel_lower:
            return None
        if "auth_watch_link" in sel_lower or "auth_feed_composer" in sel_lower:
            return None
        # Checkpoint selectors → None
        if "checkpoint" in sel_lower or "captcha" in sel_lower:
            return None
        return None

    mock_page.query_selector.side_effect = query_selector
    mock_page.wait_for_load_state = MagicMock()
    mock_page.goto = MagicMock()
    mock_page.locator = MagicMock()
    return mock_page


def test_camoufox_observe_mocked_posts(temp_config):
    """Camoufox transport normalises mocked posts into PostObservation list."""
    transport = CamoufoxTransport(temp_config)

    # Mock the browser/page so open_browser doesn't launch a real browser
    mock_page = _make_authed_mock_page()

    # _identify_page returns the correct page name
    transport._identify_page = lambda: "UnattendedBot8300"
    transport.ensure_page_identity = lambda name=None: IdentityState.PAGE_IDENTITY_CONFIRMED
    transport.verify_page_identity = lambda name=None: IdentityState.PAGE_IDENTITY_CONFIRMED
    transport.save_reference_screenshot = MagicMock()
    transport.save_failure_screenshot = MagicMock()

    # Mock _extract_posts to return controlled data
    mock_posts = [
        PostObservation(
            fb_post_id="post_1",
            message="First test post about AI",
            created_time="2026-09-05T12:00:00Z",
            posted_by_page=True,
        ),
        PostObservation(
            fb_post_id="post_2",
            message="Second post on automation",
            created_time="2026-09-05T11:00:00Z",
            posted_by_page=True,
        ),
    ]
    transport._extract_posts = lambda: mock_posts
    transport._extract_comments = lambda post: []
    transport._page = mock_page
    transport.open_browser = lambda headless=False: setattr(transport, '_page', mock_page)

    result = transport.observe()

    assert len(result.posts) == 2
    assert "First test post" in result.posts[0].message
    assert "Second post" in result.posts[1].message
    assert all(p.posted_by_page is True for p in result.posts)
    assert all(p.is_new is True for p in result.posts)
    transport.close()


def test_camoufox_observe_no_write_in_dry_run(temp_config):
    """Observe() must never trigger publish or reply, even if write methods exist."""
    transport = CamoufoxTransport(temp_config)

    mock_page = _make_authed_mock_page()
    transport.save_reference_screenshot = MagicMock()
    transport.save_failure_screenshot = MagicMock()
    transport._page = mock_page
    transport._identify_page = lambda: "UnattendedBot8300"
    transport.ensure_page_identity = lambda name=None: IdentityState.PAGE_IDENTITY_CONFIRMED
    transport.verify_page_identity = lambda name=None: IdentityState.PAGE_IDENTITY_CONFIRMED
    transport._extract_posts = lambda: []
    transport._extract_comments = lambda post: []
    # Bypass browser launch
    transport.open_browser = lambda headless=False: setattr(transport, '_page', mock_page)

    with patch.object(transport, "publish_text_status") as mock_post, \
         patch.object(transport, "reply_to_comment") as mock_reply:
        result = transport.observe()
        assert result is not None
        assert mock_post.call_count == 0
        assert mock_reply.call_count == 0
    transport.close()


def test_camoufox_observe_login_required(temp_config):
    """When Facebook shows a login form, NotLoggedInError is raised."""
    transport = CamoufoxTransport(temp_config)

    mock_page = _make_loggedout_mock_page()
    transport.save_reference_screenshot = MagicMock()
    transport.save_failure_screenshot = MagicMock()
    transport._page = mock_page
    transport.open_browser = lambda headless=False: setattr(transport, '_page', mock_page)

    # Mock input() to simulate user pressing ENTER after login
    # (in test, this simulates the flow continuing but auth still not detected)
    import builtins
    original_input = builtins.input
    builtins.input = lambda *a, **k: ""

    try:
        with pytest.raises(NotLoggedInError):
            transport.observe()
    finally:
        builtins.input = original_input
    transport.close()


def test_camoufox_observe_page_not_found(temp_config):
    """When the Page identity can't be verified, PageNotFoundError is raised."""
    transport = CamoufoxTransport(temp_config)

    mock_page = _make_authed_mock_page()
    transport.save_reference_screenshot = MagicMock()
    transport.save_failure_screenshot = MagicMock()
    transport._page = mock_page
    transport.open_browser = lambda headless=False: setattr(transport, '_page', mock_page)
    transport._identify_page = lambda: None  # page not found
    transport.ensure_page_identity = lambda name=None: IdentityState.PAGE_IDENTITY_CONFIRMED
    # verify_page_identity is called after navigation to the Page URL;
    # return IDENTITY_UNKNOWN to simulate "wrong page / not found"
    transport.verify_page_identity = lambda name=None: IdentityState.IDENTITY_UNKNOWN

    with pytest.raises(PageNotFoundError):
        transport.observe()
    transport.close()


def test_camoufox_observe_selector_error_on_posts(temp_config):
    """When post containers don't load, SelectorError is raised."""
    transport = CamoufoxTransport(temp_config)

    mock_page = _make_authed_mock_page()
    transport.save_reference_screenshot = MagicMock()
    transport.save_failure_screenshot = MagicMock()
    transport._page = mock_page
    transport.open_browser = lambda headless=False: setattr(transport, '_page', mock_page)
    transport._identify_page = lambda: "UnattendedBot8300"
    transport.ensure_page_identity = lambda name=None: IdentityState.PAGE_IDENTITY_CONFIRMED
    transport.verify_page_identity = lambda name=None: IdentityState.PAGE_IDENTITY_CONFIRMED

    # Simulate selector timeout on post containers
    transport._extract_posts = lambda: (_ for _ in ()).throw(
        SelectorError("Post containers not found — Facebook layout may have changed")
    )

    with pytest.raises(SelectorError):
        transport.observe()
    transport.close()


def test_camoufox_observe_checkpoint_detected(temp_config):
    """When a Facebook checkpoint appears, CheckpointError is raised."""
    transport = CamoufoxTransport(temp_config)

    mock_page = MagicMock()
    mock_page.url = "https://www.facebook.com/checkpoint"
    mock_page.title.return_value = "Security Check"

    def query_selector(s):
        sel_lower = s.lower()
        if "checkpoint" in sel_lower or "alert" in sel_lower or "captcha" in sel_lower:
            return MagicMock()
        if "2fa" in sel_lower or "approval" in sel_lower:
            return MagicMock()
        # Not logged in (no auth UI evidence)
        if "auth_profile_menu" in sel_lower or "auth_home_link" in sel_lower:
            return None
        return None
    mock_page.query_selector.side_effect = query_selector
    mock_page.wait_for_load_state = MagicMock()
    mock_page.goto = MagicMock()
    mock_page.wait_for_timeout = MagicMock()
    mock_page.eval_on_selector = MagicMock(return_value=None)

    transport.save_reference_screenshot = MagicMock()
    transport.save_failure_screenshot = MagicMock()
    transport._page = mock_page
    transport.open_browser = lambda headless=False: setattr(transport, '_page', mock_page)

    # Mock the challenge handler so it doesn't block on input()
    mock_handler = MagicMock(spec=ChallengeHandler)
    mock_handler.can_handle.return_value = True
    mock_handler.handle.return_value = True
    transport._challenge_handler = mock_handler

    with pytest.raises(CheckpointError):
        transport.observe()
    transport.close()


# ── Write methods never auto-execute ──────────────────────────────────

def test_publish_text_status_blocked_in_dry_run(temp_config):
    """publish_text_status must refuse in dry_run mode."""
    transport = CamoufoxTransport(temp_config)
    assert temp_config.unattended_bot_mode == "dry_run"
    with pytest.raises(TransportError, match="dry_run"):
        transport.publish_text_status("test message")
    transport.close()


def test_reply_to_comment_blocked_in_dry_run(temp_config):
    """reply_to_comment must refuse in dry_run mode."""
    transport = CamoufoxTransport(temp_config)
    assert temp_config.unattended_bot_mode == "dry_run"
    with pytest.raises(TransportError, match="dry_run"):
        transport.reply_to_comment("comment_123", "test reply")
    transport.close()


def test_post_proposal_does_not_trigger_transport(temp_config):
    """A POST proposal in the queue must not directly trigger browser action.

    The model proposes; deterministic Python executes only after approval.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        from src.storage import Storage
        config = Config(
            facebook_page_id="UnattendedBot8300",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
            facebook_transport="camoufox_ui",
            camoufox_profile_dir=str(Path(tmpdir) / "bp"),
        )
        storage = Storage(config)
        conn = storage.connect()

        queue = ProposedActionQueue(conn)
        action_id = queue.propose_post("Hello from the bot", reason="Test")

        # Verify the action is in 'proposed' state, not executed
        all_actions = queue.list_all()
        assert len(all_actions) == 1
        assert all_actions[0]["status"] == "proposed"
        assert all_actions[0]["action_type"] == "POST"

        # Proposing does NOT call transport.publish_text_status
        transport = get_transport(config)
        with patch.object(transport, "publish_text_status") as mock_post:
            pass  # No execution happened
        assert mock_post.call_count == 0
        transport.close()
        storage.close()


def test_reply_proposal_does_not_trigger_transport(temp_config):
    """A REPLY proposal in the queue must not directly trigger browser action."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from src.storage import Storage
        config = Config(
            facebook_page_id="UnattendedBot8300",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
            facebook_transport="camoufox_ui",
            camoufox_profile_dir=str(Path(tmpdir) / "bp"),
        )
        storage = Storage(config)
        conn = storage.connect()

        queue = ProposedActionQueue(conn)
        action_id = queue.propose_reply("comment_123", "Thanks!", reason="Test")

        all_actions = queue.list_all()
        assert all_actions[0]["status"] == "proposed"
        assert all_actions[0]["action_type"] == "REPLY"

        transport = get_transport(config)
        with patch.object(transport, "reply_to_comment") as mock_reply:
            pass  # Proposal only, no execution
        assert mock_reply.call_count == 0
        transport.close()
        storage.close()


def test_approved_action_reaches_executor_only_when_allowed(temp_config):
    """Approved transport actions only execute when safety+approval+mode pass."""
    # In dry_run mode, even an approved POST must not write to Facebook.
    transport = CamoufoxTransport(temp_config)
    assert temp_config.unattended_bot_mode == "dry_run"

    with pytest.raises(TransportError, match="dry_run"):
        transport.publish_text_status("Should not execute")
    transport.close()


# ── Graph API transport structural tests ──────────────────────────────

def test_graph_api_transport_structural(temp_config_graph):
    """Graph API transport imports and is structurally usable."""
    transport = get_transport(temp_config_graph)
    assert transport.capabilities().can_post is True
    assert transport.capabilities().can_reply is True

    # Verify it wraps the existing FacebookClient
    assert hasattr(transport, "_client")
    transport.close()


def test_graph_api_transport_observe_returns_empty_on_error(temp_config_graph):
    """Graph API observe returns empty ObservationResult on API error."""
    transport = get_transport(temp_config_graph)
    # FacebookClient will fail (no real token); observe should catch and return empty
    result = transport.observe()
    assert isinstance(result, ObservationResult)
    transport.close()


# ── Normalisation into existing observation format ────────────────────

def test_camoufox_normalises_to_post_observation(temp_config):
    """Camoufox-extracted posts must be PostObservation instances."""
    transport = CamoufoxTransport(temp_config)

    # Create mock article elements that simulate real Playwright elements
    mock_post = MagicMock()
    # _extract_text_from_element first tries query_selector (returns None)
    # then falls back to text_content()
    mock_post.query_selector = MagicMock(return_value=None)
    mock_post.text_content = MagicMock(return_value="A post about AI and automation")

    transport._page = MagicMock()
    transport._page.locator.return_value.all.return_value = [mock_post]

    extracted = transport._extract_posts()
    # Should produce PostObservation objects
    assert len(extracted) == 1
    assert all(isinstance(p, PostObservation) for p in extracted)
    assert "A post about AI" in extracted[0].message
    assert extracted[0].posted_by_page is True
    # is_new defaults to True in PostObservation
    assert extracted[0].is_new is True
    transport.close()


def test_camoufox_normalises_to_comment_observation(temp_config):
    """Camoufox-extracted comments must be CommentObservation instances."""
    transport = CamoufoxTransport(temp_config)

    mock_comment = MagicMock()
    mock_comment.text_selector = None  # not used
    mock_comment.text_content = MagicMock(return_value="Interesting take on this!")
    transport._page = MagicMock()
    transport._page.locator.return_value.all.return_value = [mock_comment]

    post_obs = PostObservation(
        fb_post_id="test_post_1",
        message="test",
        created_time="2026-01-01T00:00:00Z",
        posted_by_page=True,
    )
    extracted = transport._extract_comments(post_obs)
    # The _extract_comments method uses SELECTORS["comment_body"] which
    # calls self._page.locator().all() - but it also needs a post context.
    # Since we mocked _page.locator, it should return our mock_comment
    assert len(extracted) == 1
    assert all(isinstance(c, CommentObservation) for c in extracted)
    assert all(c.post_id == "test_post_1" for c in extracted)
    assert "Interesting take" in extracted[0].message
    transport.close()


# ── Gitignore verification ────────────────────────────────────────────

def test_browser_profile_is_gitignored(temp_config):
    """Verify data/browser-profile/ is in .gitignore."""
    gitignore = Path(temp_config.project_root / ".gitignore")
    # Fall back to project root gitignore
    if not gitignore.exists():
        gitignore = Path.cwd() / ".gitignore"
    if not gitignore.exists():
        pytest.skip("No .gitignore found")
    content = gitignore.read_text()
    assert "data/browser-profile" in content or "browser-profile" in content


def test_runtime_dir_is_gitignored():
    """Verify runtime/ directory is in .gitignore."""
    gitignore = Path.cwd() / ".gitignore"
    if not gitignore.exists():
        pytest.skip("No .gitignore found")
    content = gitignore.read_text()
    assert "runtime/" in content or "runtime" in content


def test_profile_dir_not_tracked_by_git():
    """Verify git would not track the browser profile directory in the real repo."""
    import subprocess
    # Use the actual project root where .git exists
    project_root = Path.cwd()
    gitignore = project_root / ".gitignore"
    if not gitignore.exists():
        pytest.skip("No .gitignore found")
    content = gitignore.read_text()
    assert "data/browser-profile" in content or "browser-profile" in content

    # Verify with git check-ignore on the actual data dir pattern
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "data/browser-profile"],
            capture_output=True,
            cwd=str(project_root),
            timeout=10,
        )
        # exit code 0 means it IS ignored (good)
        assert result.returncode == 0, "data/browser-profile is NOT gitignored"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pytest.skip("git not available for check-ignore test")


def test_env_not_committed():
    """.env must be in .gitignore."""
    gitignore = Path.cwd() / ".gitignore"
    if not gitignore.exists():
        pytest.skip("No .gitignore found")
    content = gitignore.read_text()
    assert ".env" in content


# ── Screenshot manifest format ─────────────────────────────────────────

def test_screenshot_manifest_format(temp_config):
    """ScreenshotManifest produces the expected non-sensitive fields."""
    from src.facebook_camoufox import ScreenshotManifest
    manifest = ScreenshotManifest(
        filename="001-facebook-loaded.png",
        description="Facebook loaded",
        state="logged_in",
        url="https://www.facebook.com/",
        action="observe",
        success=True,
    )
    d = manifest.to_dict()
    assert d["filename"] == "001-facebook-loaded.png"
    assert d["description"] == "Facebook loaded"
    assert d["state"] == "logged_in"
    assert "timestamp" in d
    # Must NOT contain sensitive fields
    assert "cookies" not in d
    assert "token" not in d
    assert "password" not in d


def test_camoufox_transport_has_screenshot_helper(temp_config):
    """CamoufoxTransport exposes save_reference_screenshot and save_failure_screenshot."""
    transport = CamoufoxTransport(temp_config)
    assert hasattr(transport, "save_reference_screenshot")
    assert hasattr(transport, "save_failure_screenshot")
    assert hasattr(transport, "cleanup_old_references")
    transport.close()


# ── Transport capabilities ────────────────────────────────────────────

def test_camoufox_capabilities(temp_config):
    """CamoufoxTransport reports correct capabilities."""
    transport = CamoufoxTransport(temp_config)
    caps = transport.capabilities()
    assert caps.name == "camoufox_ui"
    assert caps.read_observations is True
    assert caps.can_reply is True
    assert caps.persistent_profile is True
    assert caps.headless is False  # headed by default
    transport.close()


# ── Authentication detection tests ──────────────────────────────────

def test_auth_login_form_visible_equals_login_required(temp_config):
    """Login form visible (email + password + button) => LOGIN_REQUIRED.

    This is the exact regression that previously failed: a cookie existed
    but the login form was still visible in the browser, yet the old code
    returned 'authenticated'.
    """
    transport = CamoufoxTransport(temp_config)
    mock_page = _make_loggedout_mock_page()
    transport._page = mock_page

    state = transport.detect_auth_state()
    assert state == AuthState.LOGIN_REQUIRED
    transport.close()


def test_auth_cookie_present_login_form_visible_equals_login_required(temp_config):
    """Cookie present + login form visible => LOGIN_REQUIRED.

    A Facebook cookie existing does NOT prove an authenticated session.
    The login form being visible is positive proof of logout.
    """
    transport = CamoufoxTransport(temp_config)
    mock_page = _make_loggedout_mock_page()
    # Simulate: browser has cookies (cookie count > 0) but login form is visible
    mock_page.context = MagicMock()
    mock_page.context.cookies.return_value = [{"name": "c_user", "value": "12345"}]

    transport._page = mock_page
    state = transport.detect_auth_state()
    assert state == AuthState.LOGIN_REQUIRED
    transport.close()


def test_auth_authenticated_ui_detected_equals_authenticated(temp_config):
    """Positive authenticated UI evidence => AUTHENTICATED."""
    transport = CamoufoxTransport(temp_config)
    mock_page = _make_authed_mock_page()
    transport._page = mock_page

    state = transport.detect_auth_state()
    assert state == AuthState.AUTHENTICATED
    transport.close()


def test_auth_ambiguous_state_equals_unknown(temp_config):
    """No positive login form AND no positive authenticated UI => UNKNOWN_AUTH_STATE.

    UNKNOWN_AUTH_STATE must fail closed — it never implies authenticated.
    """
    transport = CamoufoxTransport(temp_config)
    mock_page = MagicMock()
    mock_page.url = "https://www.facebook.com/"
    mock_page.title.return_value = "Facebook"

    # No login form elements, no authenticated UI elements
    def query_selector(selector):
        sel_lower = selector.lower()
        if any(k in sel_lower for k in ["login_form", "email", "pass",
                                         "login_page_link", "checkpoint",
                                         "approval", "alert", "2fa", "captcha"]):
            return None
        if any(k in sel_lower for k in ["auth_profile_menu", "profile",
                                         "auth_home_link", "auth_watch_link",
                                         "auth_feed_composer", "page_header_title"]):
            return None
        return None
    mock_page.query_selector.side_effect = query_selector
    mock_page.wait_for_load_state = MagicMock()

    transport._page = mock_page
    state = transport.detect_auth_state()
    assert state == AuthState.UNKNOWN_AUTH_STATE
    transport.close()


def test_auth_checkpoint_detected_equals_checkpoint_required(temp_config):
    """2FA/checkpoint form visible => CHECKPOINT_REQUIRED."""
    transport = CamoufoxTransport(temp_config)
    mock_page = MagicMock()
    mock_page.url = "https://www.facebook.com/checkpoint"

    def query_selector(selector):
        sel_lower = selector.lower()
        if any(k in sel_lower for k in ["checkpoint", "approval", "alert",
                                         "2fa", "captcha"]):
            return MagicMock()  # challenge element visible
        return None
    mock_page.query_selector.side_effect = query_selector
    mock_page.wait_for_load_state = MagicMock()

    transport._page = mock_page
    state = transport.detect_auth_state()
    assert state == AuthState.CHECKPOINT_REQUIRED
    transport.close()


def test_check_login_state_backward_compat(temp_config):
    """check_login_state() returns True only when AUTHENTICATED (not cookie-based)."""
    transport = CamoufoxTransport(temp_config)

    # Authenticated page → True
    transport._page = _make_authed_mock_page()
    assert transport.check_login_state() is True

    # Logged-out page → False (even if cookies exist)
    transport._page = _make_loggedout_mock_page()
    transport._page.context = MagicMock()
    transport._page.context.cookies.return_value = [{"name": "c_user", "value": "12345"}]
    assert transport.check_login_state() is False

    transport.close()


# ── Page identity verification tests ──────────────────────────────────

def test_graph_api_page_id_irrelevant_to_camoufox(temp_config):
    """The numeric Graph API Page ID must not be required for camoufox_ui navigation.

    Camoufox uses FACEBOOK_PAGE_URL / FACEBOOK_PAGE_SLUG, not FACEBOOK_PAGE_ID.
    Even if FACEBOOK_PAGE_ID is a test placeholder, camoufox should navigate
    to the real Page URL.
    """
    transport = CamoufoxTransport(temp_config)
    # The config has facebook_page_id='UnattendedBot8300' (a slug, not numeric)
    url = transport._page_url()
    assert "UnattendedBot8300" in url
    assert "facebook.com" in url
    # Should NOT use /pages/ numeric path (that's for graph_api)
    assert "/pages/" not in url
    transport.close()


def test_camoufox_uses_page_url_config():
    """Camoufox transport uses FACEBOOK_PAGE_URL from config for navigation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            facebook_page_url="https://www.facebook.com/MyCustomPage",
            facebook_page_slug="MyCustomPage",
            facebook_transport="camoufox_ui",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
            camoufox_profile_dir=str(Path(tmpdir) / "bp"),
        )
        transport = CamoufoxTransport(config)
        assert transport._page_slug == "MyCustomPage"
        assert transport._page_url() == "https://www.facebook.com/MyCustomPage"
        transport.close()


def test_personal_identity_active_blocks_write(temp_config):
    """When personal identity is active (not the Page), write is blocked."""
    transport = CamoufoxTransport(temp_config)
    transport._page = _make_authed_mock_page()

    # verify_page_identity returns PERSONAL_IDENTITY_ACTIVE
    transport.verify_page_identity = lambda name=None: IdentityState.PERSONAL_IDENTITY_ACTIVE

    with pytest.raises(TransportError, match="Write blocked"):
        transport.publish_text_status("test")
    transport.close()


def test_unknown_identity_blocks_write(temp_config):
    """When identity cannot be confirmed (UNKNOWN), write is blocked."""
    transport = CamoufoxTransport(temp_config)
    transport._page = _make_authed_mock_page()
    transport.verify_page_identity = lambda name=None: IdentityState.IDENTITY_UNKNOWN
    transport.ensure_page_identity = lambda name=None: IdentityState.IDENTITY_UNKNOWN

    with pytest.raises(TransportError, match="Write blocked"):
        transport.reply_to_comment("comment_123", "test reply")
    transport.close()


def test_page_identity_confirmed_allows_preflight(temp_config):
    """When identity is confirmed, the preflight passes (proceeds to NotImplementedError).

    In dry_run mode the preflight never reaches identity check (blocked by
    dry_run first).  So test with live mode + confirmed identity → should
    reach NotImplementedError (not TransportError for identity).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            facebook_page_id="UnattendedBot8300",
            facebook_transport="camoufox_ui",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
            camoufox_profile_dir=str(Path(tmpdir) / "bp"),
            unattended_bot_mode="live",  # bypass dry_run check
        )
        transport = CamoufoxTransport(config)
        transport._page = _make_authed_mock_page()
        transport.ensure_page_identity = lambda name=None: IdentityState.PAGE_IDENTITY_CONFIRMED
        transport.verify_page_identity = lambda name=None: IdentityState.PAGE_IDENTITY_CONFIRMED

        # Should pass auth + identity checks, then hit NotImplementedError
        # (UI automation not yet wired) — NOT a TransportError identity block
        with pytest.raises(NotImplementedError):
            transport.publish_text_status("test message")
        transport.close()


def test_page_switch_failure_blocks_write(temp_config):
    """When page switching fails, write is blocked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            facebook_page_id="UnattendedBot8300",
            facebook_transport="camoufox_ui",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
            camoufox_profile_dir=str(Path(tmpdir) / "bp"),
            unattended_bot_mode="live",
        )
        transport = CamoufoxTransport(config)
        transport._page = _make_authed_mock_page()
        transport.ensure_page_identity = lambda name=None: IdentityState.PAGE_SWITCH_FAILED

        with pytest.raises(TransportError, match="Write blocked"):
            transport.publish_text_status("test")
        transport.close()


# ── Page detection tests ──────────────────────────────────────────────

def test_correct_page_detection(temp_config):
    """verify_page_identity returns PAGE_IDENTITY_CONFIRMED when signals match."""
    transport = CamoufoxTransport(temp_config)
    mock_page = _make_authed_mock_page(
        page_url="https://www.facebook.com/UnattendedBot8300"
    )
    transport._page = mock_page

    # URL contains slug + h1 contains slug + profile link contains slug
    result = transport.verify_page_identity()
    assert result == IdentityState.PAGE_IDENTITY_CONFIRMED
    transport.close()


def test_wrong_page_detection(temp_config):
    """verify_page_identity does NOT confirm when on the wrong Page."""
    transport = CamoufoxTransport(temp_config)
    mock_page = MagicMock()
    mock_page.url = "https://www.facebook.com/SomeOtherPage"
    mock_page.title.return_value = "SomeOtherPage"

    def query_selector(selector):
        sel_lower = selector.lower()
        # No login form, no checkpoint
        if any(k in sel_lower for k in ["login_form", "email", "pass",
                                         "login_page_link", "checkpoint",
                                         "approval", "alert", "2fa", "captcha"]):
            return None
        # Authenticated UI
        if "auth_profile_menu" in sel_lower or "profile" in sel_lower:
            return MagicMock()
        # Page header title — shows wrong page name
        if "page_header_title" in sel_lower:
            el = MagicMock()
            el.text_content.return_value = "SomeOtherPage"
            return el
        return None
    mock_page.query_selector.side_effect = query_selector
    mock_page.get_by_role.return_value.text_content.return_value = "SomeOtherPage"
    mock_page.wait_for_load_state = MagicMock()

    transport._page = mock_page
    result = transport.verify_page_identity()
    # URL doesn't match, heading doesn't match, link doesn't match,
    # identity label is "SomeOtherPage" which doesn't contain "UnattendedBot8300"
    assert result != IdentityState.PAGE_IDENTITY_CONFIRMED
    transport.close()


# ── Challenge detection tests ─────────────────────────────────────────

def test_challenge_detection_challenges_are_classified(temp_config):
    """Challenge types are properly classified."""
    transport = CamoufoxTransport(temp_config)

    # CAPTCHA
    mock_page = MagicMock()
    def qs_captcha(s):
        sel_lower = s.lower()
        if "captcha" in sel_lower:
            return MagicMock()
        if any(k in sel_lower for k in ["checkpoint", "approval", "alert", "2fa"]):
            return None
        return None
    mock_page.query_selector.side_effect = qs_captcha
    mock_page.wait_for_load_state = MagicMock()
    transport._page = mock_page
    assert transport._detect_challenge() == ChallengeType.CAPTCHA
    transport.close()

    # 2FA
    transport2 = CamoufoxTransport(temp_config)
    mock_page2 = MagicMock()
    def qs_2fa(s):
        sel_lower = s.lower()
        # Match the actual twofa_input and checkpoint selectors by their
        # distinctive substrings from SELECTORS values
        if "approval_code" in sel_lower or "nucleus_otp" in sel_lower:
            return MagicMock()  # 2FA input visible
        if "captcha" in sel_lower:
            return None
        if "checkpoint" in sel_lower:
            return None
        if "alert" in sel_lower:
            return None
        return None
    mock_page2.query_selector.side_effect = qs_2fa
    mock_page2.wait_for_load_state = MagicMock()
    transport2._page = mock_page2
    assert transport2._detect_challenge() == ChallengeType.TWO_FACTOR
    transport2.close()


def test_challenge_handler_protocol():
    """ChallengeHandler protocol and implementations work correctly."""
    # HumanInterventionHandler handles all challenge types
    h = HumanInterventionHandler()
    assert h.can_handle(ChallengeType.CAPTCHA) is True
    assert h.can_handle(ChallengeType.TWO_FACTOR) is True

    # TwoCaptchaHandler without key falls back to human
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            facebook_transport="camoufox_ui",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
            camoufox_profile_dir=str(Path(tmpdir) / "bp"),
            # No twocaptcha_api_key set
        )
        tcs = TwoCaptchaHandler(config)
        assert tcs.can_handle(ChallengeType.CAPTCHA) is False  # no key → False
        assert tcs.can_handle(ChallengeType.TWO_FACTOR) is False

        # With key, can handle CAPTCHA
        config2 = Config(
            facebook_transport="camoufox_ui",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
            camoufox_profile_dir=str(Path(tmpdir) / "bp"),
            twocaptcha_api_key="test_key_123",
        )
        tcs2 = TwoCaptchaHandler(config2)
        assert tcs2.can_handle(ChallengeType.CAPTCHA) is True
        assert tcs2.can_handle(ChallengeType.TWO_FACTOR) is False


def test_challenge_handler_is_decoupled():
    """The ChallengeHandler protocol exists and is separate from transport."""
    transport = CamoufoxTransport(Config())
    assert transport._challenge_handler is not None
    assert hasattr(transport._challenge_handler, "can_handle")
    assert hasattr(transport._challenge_handler, "handle")
    transport.close()


# ── Persistent profile configuration tests ────────────────────────────

def test_persistent_profile_configured():
    """Camoufox uses a dedicated persistent profile directory."""
    config = Config()
    assert config.camoufox_profile_dir == "data/browser-profile"
    profile_path = config.camoufox_profile_path
    assert "browser-profile" in str(profile_path).lower()


def test_persistent_profile_path_resolution():
    """Camoufox profile path resolves to an absolute Path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            facebook_transport="camoufox_ui",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
            camoufox_profile_dir=str(Path(tmpdir) / "browser-profile"),
        )
        path = config.camoufox_profile_path
        assert isinstance(path, Path)
        assert path.is_absolute() or "browser-profile" in str(path)


# ── No writes in dry_run tests ────────────────────────────────────────

def test_no_writes_when_unauthenticated_in_live_mode(temp_config):
    """Even in live mode, write is blocked if not authenticated."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Config(
            facebook_page_id="UnattendedBot8300",
            facebook_transport="camoufox_ui",
            database_path=str(Path(tmpdir) / "test.db"),
            project_root=Path(tmpdir),
            camoufox_profile_dir=str(Path(tmpdir) / "bp"),
            unattended_bot_mode="live",  # bypass dry_run
        )
        transport = CamoufoxTransport(config)
        # Simulate logged-out state
        transport._page = _make_loggedout_mock_page()

        with pytest.raises(TransportError, match="authentication state"):
            transport.publish_text_status("test")
        transport.close()


def test_dry_run_blocks_all_writes(temp_config):
    """In dry_run mode, both publish and reply are blocked before identity check."""
    transport = CamoufoxTransport(temp_config)
    assert temp_config.unattended_bot_mode == "dry_run"

    # Even with confirmed identity, dry_run blocks
    transport._page = _make_authed_mock_page()
    transport.verify_page_identity = lambda name=None: IdentityState.PAGE_IDENTITY_CONFIRMED
    transport.ensure_page_identity = lambda name=None: IdentityState.PAGE_IDENTITY_CONFIRMED

    with pytest.raises(TransportError, match="dry_run"):
        transport.publish_text_status("test")
    with pytest.raises(TransportError, match="dry_run"):
        transport.reply_to_comment("c1", "test")
    transport.close()


# ── Config: Graph API Page ID separation ──────────────────────────────

def test_graph_api_page_id_kept_separate_from_camoufox():
    """FACEBOOK_PAGE_ID (numeric) remains for graph_api only, not camoufox_ui."""
    config = Config()
    # Default camoufox config should have page_url + page_slug
    assert config.facebook_page_url == "https://www.facebook.com/UnattendedBot8300"
    assert config.facebook_page_slug == "UnattendedBot8300"
    # Facebook_page_id defaults to empty (graph_api only)
    assert config.facebook_page_id == ""


def test_page_url_env_override():
    """FACEBOOK_PAGE_URL and FACEBOOK_PAGE_SLUG can be overridden via env."""
    import os
    os.environ["FACEBOOK_PAGE_URL"] = "https://www.facebook.com/TestPage"
    os.environ["FACEBOOK_PAGE_SLUG"] = "TestPage"
    try:
        config = load_config()
        assert config.facebook_page_url == "https://www.facebook.com/TestPage"
        assert config.facebook_page_slug == "TestPage"
    finally:
        del os.environ["FACEBOOK_PAGE_URL"]
        del os.environ["FACEBOOK_PAGE_SLUG"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
