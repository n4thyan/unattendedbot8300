"""
Camoufox UI transport for UnattendedBot8300.

EXPERIMENTAL — uses the already-installed Camoufox browser (a Firefox fork
wrapped by Playwright) as a live Facebook UI transport when the official
Meta Graph API is unavailable.

Responsibilities:
  - READ-ONLY observation of the UnattendedBot8300 Facebook Page
  - Deterministic normalisation into the existing fb_observations format
  - Stubbed write methods (publish_text_status, reply_to_comment) that
    are NEVER auto-executed — only callable after model decision ->
    validation -> safety -> rate-limit -> approval.

Security notes:
  - Uses a dedicated persistent browser profile (data/browser-profile/)
    that is gitignored.
  - No cookies, tokens, or auth material are extracted or logged.
  - On Facebook checkpoints / 2FA / login-required, the browser is left
    visible for manual human resolution.
  - Screenshots and debug artifacts go to runtime/browser-references/
    (gitignored, never committed).
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import Config
from .facebook_transport import (
    FacebookTransport, TransportCapabilities,
    NotLoggedInError, PageNotFoundError, SelectorError,
    CheckpointError, NavigationError, TransportError,
)
from .fb_observations import (
    ObservationResult, PostObservation, CommentObservation,
    ObservationDetector,
)


# ── Centralized selector library ─────────────────────────────────────

# Facebook changes generated CSS class names frequently.  We prefer
# ARIA roles, accessible names, labels, and semantic relationships wherever
# possible.  Each selector is a Playwright locator string.
SELECTORS = {
    # Login state
    "login_form": "form[action*='login']",
    "login_input_name": "input#email, input[name='email']",
    "login_input_pass": "input#pass, input[name='pass']",
    "login_button": "button[name='login']",

    # Post composer (write only — never auto-invoked)
    "post_composer_textarea": "div[role='dialog'] div[role='textbox'], div[data-block='true']",

    # Page identity
    "page_header_title": "h1 [data-id] span, span[data-text='true']",
    "page_profile_link": "a[aria-label*='UnattendedBot8300'], a[href*='/UnattendedBot8300']",

    # Posts on a Page timeline
    "post_container": "div[role='article']",
    "post_message": "div[role='article'] div[data-xf-comment='true'], div[role='article'] div[data-testid='post-message']",
    "post_text_fallback": "div[role='article'] div > div > div > div > div",

    # Comments
    "comment_container": "ul[data-testid='fb-ufeedback-comments'], div[data-xf-comment='true']",
    "comment_body": "div[data-testid='comment-body'], div[data-xf-comment-body='true']",

    # Checkpoint / security challenge
    "checkpoint_form": "form#checkpoint_form, form[action*='checkpoint']",
    "checkpoint_input": "input[name='checkpoint_data'], input[name='approvals_code']",
    "2fa_input": "input[name='approval_code'], input[name='nucleus_otp']",
    "security_alert": "div[data-testid='security-checkpoint'], div[role='alert']",
}

# Facebook Page URL (the canonical Page for UnattendedBot8300)
# This should ideally come from config; falls back to slug-based URL.
DEFAULT_PAGE_SLUG = "UnattendedBot8300"

# Explicit wait timeouts (seconds) — no fixed sleep() as primary sync
DEFAULT_TIMEOUT = 15  # seconds
PAGE_LOAD_TIMEOUT = 30


@dataclass
class ScreenshotManifest:
    """Non-sensitive metadata for a single reference screenshot."""
    filename: str
    description: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    state: str = ""
    url: str = ""
    action: str = ""
    success: bool = True
    observation_id: Optional[str] = None
    proposed_action_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "description": self.description,
            "timestamp": self.timestamp,
            "state": self.state,
            "url": self.url,
            "action": self.action,
            "success": self.success,
            "observation_id": self.observation_id,
            "proposed_action_id": self.proposed_action_id,
        }


class CamoufoxTransport(FacebookTransport):
    """Camoufox browser-based Facebook UI transport (experimental).

    Uses the installed ``camoufox`` Python package (v0.5.6) which wraps
    Playwright Firefox.  A dedicated persistent profile directory is used
    so the user can log in manually once and reuse the session.
    """

    def __init__(self, config: Config):
        self._config = config
        self._profile_dir = config.camoufox_profile_path
        self._browser = None
        self._page = None
        self._conn = None  # not needed; we use ObservationDetector if available

        # Runtime directories for screenshots / manifests
        self._runtime_dir = config.project_root / "runtime"
        self._references_dir = self._runtime_dir / "browser-references" / "sessions"
        self._failures_dir = self._runtime_dir / "browser-references" / "failures"

        # Screenshot retention (days)
        self._screenshot_retention_days = 14
        self._screenshots_enabled = True

    # ── Capability metadata ──

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            name="camoufox_ui",
            transport_type="camoufox_ui",
            read_observations=True,
            can_post=True,
            can_reply=True,
            headless=False,
            persistent_profile=True,
        )

    # ── Browser lifecycle ──

    def _ensure_dirs(self) -> None:
        """Create runtime directories for screenshots and manifests."""
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        if self._screenshots_enabled:
            self._references_dir.mkdir(parents=True, exist_ok=True)
            self._failures_dir.mkdir(parents=True, exist_ok=True)

    def open_browser(self, headless: bool = False) -> None:
        """Launch a Camoufox browser with the persistent profile.

        Args:
            headless: If True, run in headless mode.  For initial manual
                login, pass ``headless=False`` (the default).
        """
        from camoufox.sync_api import Camoufox
        self._ensure_dirs()

        # Camoufox's NewBrowser supports persistent_context=True + user_data_dir
        # which maps to playwright.firefox.launch_persistent_context
        self._browser = Camoufox(
            headless=headless,
            persistent_context=True,
            user_data_dir=str(self._profile_dir),
        ).__enter__()
        self._page = self._browser.new_page()

    def close_browser(self) -> None:
        """Close the browser and release resources."""
        if self._page:
            try:
                self._page.close()
            except Exception:
                pass
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        self._browser = None
        self._page = None

    def close(self) -> None:
        self.close_browser()

    def __enter__(self) -> "CamoufoxTransport":
        self.open_browser(headless=False)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close_browser()

    # ── Screenshot / reference helpers ──

    def _session_dir(self) -> Path:
        """Return a timestamped directory for the current browser session."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        d = self._references_dir / ts
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _counter(self) -> int:
        """Sequential counter for screenshot filenames within a session."""
        # We track the session dir on the instance
        return getattr(self, "_screenshot_counter", 0)

    def save_reference_screenshot(self, label: str, metadata: Optional[dict] = None) -> Optional[Path]:
        """Save a non-sensitive reference screenshot during a browser run.

        Screenshots go to runtime/browser-references/sessions/<timestamp>/
        which is gitignored and never committed.

        Args:
            label: Short human-readable label (e.g. "facebook-loaded").
            metadata: Optional dict with state, url, action, success, etc.

        Returns:
            Path to the saved screenshot, or None if disabled.
        """
        if not self._screenshots_enabled or not self._page:
            return None

        # Increment counter
        if not hasattr(self, "_screenshot_counter"):
            self._screenshot_counter = 0
        self._screenshot_counter += 1
        if not hasattr(self, "_session_dir_ref") or self._session_dir_ref is None:
            self._session_dir_ref = self._session_dir()
        session_dir = self._session_dir_ref

        seq = f"{self._screenshot_counter:03d}"
        safe_label = re.sub(r'[^a-zA-Z0-9_-]', '_', label)
        filename = f"{seq}-{safe_label}.png"
        filepath = session_dir / filename

        try:
            self._page.screenshot(path=str(filepath), full_page=False)
        except Exception:
            return None

        # Write manifest entry
        manifest_entry = ScreenshotManifest(
            filename=filename,
            description=label,
            state=metadata.get("state", "") if metadata else "",
            url=metadata.get("url", "") if metadata else "",
            action=metadata.get("action", "") if metadata else "",
            success=metadata.get("success", True) if metadata else True,
            observation_id=metadata.get("observation_id") if metadata else None,
            proposed_action_id=metadata.get("proposed_action_id") if metadata else None,
        )
        self._append_manifest(session_dir, manifest_entry)
        return filepath

    def _append_manifest(self, session_dir: Path, entry: ScreenshotManifest) -> None:
        """Append a manifest entry to the session's manifest.json (list format)."""
        manifest_path = session_dir / "manifest.json"
        entries: list[dict] = []
        if manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    content = f.read().strip()
                    if content:
                        existing = json.loads(content)
                        if isinstance(existing, list):
                            entries = existing
                        elif isinstance(existing, dict) and "entries" in existing:
                            entries = existing["entries"]
            except (json.JSONDecodeError, OSError):
                entries = []

        entries.append(entry.to_dict())

        manifest_data = {
            "session_id": session_dir.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entries": entries,
        }
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)

    def save_failure_screenshot(self, label: str, error: str) -> Optional[Path]:
        """Save a screenshot to the failures directory on error."""
        if not self._screenshots_enabled or not self._page:
            return None
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        safe_label = re.sub(r'[^a-zA-Z0-9_-]', '_', label)
        filepath = self._failures_dir / f"{ts}-{safe_label}.png"
        try:
            self._page.screenshot(path=str(filepath), full_page=True)
        except Exception:
            return None

        # Also log a small text manifest (no sensitive data)
        log_path = self._failures_dir / f"{ts}-{safe_label}.log"
        with open(log_path, "w") as f:
            f.write(f"timestamp: {ts}\nlabel: {label}\nerror: {error}\n")
            f.write(f"url: {self._page.url}\n")
        return filepath

    def cleanup_old_references(self) -> int:
        """Delete reference screenshots older than the retention period."""
        if not self._references_dir.exists():
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - (self._screenshot_retention_days * 86400)
        deleted = 0
        for session_dir in self._references_dir.iterdir():
            if session_dir.is_dir():
                for f in session_dir.rglob("*"):
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                        deleted += 1
                # Remove empty session dirs
                try:
                    if not any(session_dir.iterdir()):
                        session_dir.rmdir()
                except OSError:
                    pass
        return deleted

    # ── URL helpers ──

    def _facebook_url(self) -> str:
        return "https://www.facebook.com/"

    def _page_url(self) -> str:
        """Return the URL for the UnattendedBot8300 Facebook Page.

        Prefers FACEBOOK_PAGE_ID if set (numeric ID -> /pages/...), otherwise
        uses the page slug.  Falls back to the known page slug when the
        configured ID looks like a placeholder (e.g. "test_page_123").
        """
        page_id = self._config.facebook_page_id.strip()
        if page_id and page_id.isdigit():
            return f"https://www.facebook.com/pages/{page_id}"
        if page_id and not page_id.startswith("test_"):
            return f"https://www.facebook.com/{page_id}"
        return f"https://www.facebook.com/{DEFAULT_PAGE_SLUG}"

    # ── Core observation methods ──

    def _check_login_state(self) -> bool:
        """Check if the browser is logged into Facebook.

        Detects the presence of a login form / email input as a signal
        that the user is NOT authenticated.
        """
        try:
            # If a login form is immediately visible, we are not logged in
            self._page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT * 1000)
            login_input = self._page.query_selector(SELECTORS["login_input_name"])
            if login_input is not None:
                return False
            # Also check for the "Log In" button text
            login_btn = self._page.query_selector(SELECTORS["login_button"])
            if login_btn is not None:
                return False
            return True
        except Exception:
            # On any error, assume not logged in (safe default)
            return False

    def _check_checkpoint(self) -> bool:
        """Detect if Facebook has presented a security checkpoint."""
        try:
            checkpoint = self._page.query_selector(SELECTORS["checkpoint_form"])
            if checkpoint is not None:
                return True
            otp_input = self._page.query_selector(SELECTORS["2fa_input"])
            if otp_input is not None:
                return True
            alert = self._page.query_selector(SELECTORS["security_alert"])
            if alert is not None:
                return True
            return False
        except Exception:
            return False

    def observe(self) -> ObservationResult:
        """Read-only observation of the UnattendedBot8300 Facebook Page.

        Flow:
          1. Open Camoufox (persistent profile) headed.
          2. Navigate to Facebook.
          3. Verify logged-in state.
          4. Navigate to the UnattendedBot8300 Page.
          5. Identify that the correct Page is open.
          6. Read recent posts.
          7. Read visible comments on those posts.
          8. Normalise into ObservationResult.

        Raises:
            NotLoggedInError: if Facebook requires manual login (browser left open).
            CheckpointError: if Facebook presents a security challenge.
            PageNotFoundError: if the Page cannot be found.
            SelectorError: if a UI selector does not match (layout change).
        """
        self._session_dir_ref = None
        self._screenshot_counter = 0

        try:
            # 1. Open browser headed (headless=False for manual login support)
            self.open_browser(headless=False)
            self.save_reference_screenshot("browser-launched", {"state": "browser_open"})

            # 2. Navigate to Facebook
            self._page.goto(self._facebook_url(), timeout=PAGE_LOAD_TIMEOUT * 1000)
            self.save_reference_screenshot("facebook-loaded", {"state": "facebook_loaded"})

            # 3. Verify logged-in state
            if not self._check_login_state():
                self.save_failure_screenshot("login-required", "Login form detected")
                raise NotLoggedInError(
                    "Facebook requires manual login. The browser has been left "
                    "open. Please log in manually, then re-run this transport."
                )

            self.save_reference_screenshot("login-confirmed", {"state": "logged_in"})

            # 4. Check for checkpoint/security challenge
            if self._check_checkpoint():
                self.save_failure_screenshot("checkpoint-detected", "Security checkpoint visible")
                raise CheckpointError(
                    "Facebook presented a security checkpoint. The browser has "
                    "been left open. Please complete the challenge manually."
                )

            # 5. Navigate to the UnattendedBot8300 Page
            page_url = self._page_url()
            self._page.goto(page_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT * 1000)
            self.save_reference_screenshot("page-open", {"state": "page_open", "url": self._safe_url()})

            # 6. Identify that the correct Page is open
            page_name = self._identify_page()
            if page_name is None:
                self.save_failure_screenshot("page-not-found", "Page identity not detected")
                raise PageNotFoundError(
                    "Could not identify the UnattendedBot8300 Page. "
                    "The URL may be incorrect or the page may have been removed."
                )
            self.save_reference_screenshot("page-verified", {"state": "page_verified", "url": self._safe_url()})

            # 7. Read recent posts
            posts = self._extract_posts()
            self.save_reference_screenshot("recent-posts", {"state": "posts_extracted"})

            # 8. Read comments on recent posts
            all_comments = []
            for post_obs in posts[:3]:  # comments on up to 3 recent posts
                try:
                    comments = self._extract_comments(post_obs)
                    all_comments.extend(comments)
                except SelectorError:
                    # Skip posts whose comment thread can't be opened
                    continue
            self.save_reference_screenshot("comments-extracted", {"state": "comments_done"})

            return ObservationResult(
                posts=posts,
                comments=all_comments,
                unreplied_comments=self._find_unreplied(all_comments),
            )

        except (NotLoggedInError, CheckpointError, PageNotFoundError):
            raise
        except NavigationError:
            raise
        except SelectorError:
            raise
        except Exception as e:
            if self._page:
                self.save_failure_screenshot("unexpected-error", str(e))
            raise TransportError(f"Camoufox observation failed: {e}") from e
        finally:
            self.cleanup_old_references()

    def _safe_url(self) -> str:
        """Return a URL safe for logging (strips query strings with tokens)."""
        if not self._page:
            return ""
        url = self._page.url or ""
        # Only keep the path, strip query params (may contain session data)
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    def _identify_page(self) -> Optional[str]:
        """Attempt to identify that the correct Facebook Page is open.

        Uses ARIA headings, accessible names, and visible text rather than
        generated CSS class names.
        """
        try:
            # Facebook Pages render the page name in an <h1> or in an
            # element with an aria-label matching the page name
            page_name = self._config.facebook_page_id.strip() or DEFAULT_PAGE_SLUG

            # Try aria-label / accessible name match
            page_header = self._page.get_by_role("heading", level=1)
            header_text = ""
            try:
                header_text = (page_header.text_content() or "").strip()
            except Exception:
                pass

            if page_name.lower() in (header_text or "").lower():
                return header_text or page_name

            # Fallback: check page title
            title = self._page.title() or ""
            if page_name.lower() in title.lower():
                return title

            # Fallback: check for a profile link with the page name
            link = self._page.get_by_role("link", name=re.compile(re.escape(page_name), re.IGNORECASE))
            if link.count() > 0:
                return page_name

            # If we see a generic "Page" heading but can't confirm identity,
            # still return a weak match
            if header_text and ("page" in header_text.lower()):
                return page_name

            return None
        except Exception:
            raise SelectorError("Could not identify Facebook Page — layout may have changed")

    def _extract_posts(self) -> list[PostObservation]:
        """Extract recent posts from the Page timeline.

        Uses ARIA roles (role='article' for posts) and semantic text
        extraction rather than CSS class names.
        """
        posts: list[PostObservation] = []

        try:
            # Wait for post containers to load
            self._page.wait_for_selector(SELECTORS["post_container"], timeout=DEFAULT_TIMEOUT * 1000)
        except Exception:
            raise SelectorError("Post containers not found — Facebook layout may have changed")

        article_elements = self._page.locator(SELECTORS["post_container"]).all()

        for idx, article in enumerate(article_elements[:10]):
            try:
                post_id = f"ui_post_{idx}_{self._page.evaluate('Math.random().toString(36).slice(2,10)')}"

                # Extract message text
                message = self._extract_text_from_element(article)
                if not message or len(message) < 1:
                    continue

                posts.append(PostObservation(
                    fb_post_id=post_id,
                    message=message,
                    created_time=datetime.now(timezone.utc).isoformat(),
                    posted_by_page=True,  # We're on the Page timeline
                    like_count=0,         # Only if reliably exposed
                    comment_count=0,
                    raw={"source": "camoufox_ui", "index": idx},
                ))
            except Exception:
                continue

        if not posts:
            raise SelectorError("No posts could be extracted from the Page timeline")

        return posts

    def _extract_comments(self, post_obs: PostObservation) -> list[CommentObservation]:
        """Extract visible comments on a post."""
        comments: list[CommentObservation] = []

        # Comments may be under a specific element within the post article.
        # We try to find comment bodies using semantic locators.
        try:
            comment_elements = self._page.locator(SELECTORS["comment_body"]).all()
        except Exception:
            raise SelectorError("Comment containers not found — layout may have changed")

        if not comment_elements:
            return []  # No comments visible

        for c in comment_elements[:20]:
            try:
                text = (c.text_content() or "").strip()
                if not text:
                    continue
                # Split "Name" from "comment text" — Facebook comments render
                # the author name as a separate element
                comments.append(CommentObservation(
                    fb_comment_id=f"ui_comment_{post_obs.fb_post_id}_{len(comments)}",
                    post_id=post_obs.fb_post_id,
                    message=text,
                    from_name="Unknown",  # Only if author name is reliably extractable
                    from_id="",
                    created_time=datetime.now(timezone.utc).isoformat(),
                    parent_comment_id=None,
                    like_count=0,
                    raw={"source": "camoufox_ui"},
                ))
            except Exception:
                continue

        return comments

    def _extract_text_from_element(self, element) -> str:
        """Extract visible text from a Playwright ElementHandle/Locator.

        Tries the primary message selector first, then falls back to
        extracting all visible text from the element.
        """
        # Try specific post message selectors
        try:
            msg_el = element.query_selector(SELECTORS["post_message"])
            if msg_el:
                return (msg_el.text_content() or "").strip()
        except Exception:
            pass

        # Fallback: get all visible text from the post article
        try:
            return (element.text_content() or "").strip()
        except Exception:
            return ""

    def _find_unreplied(self, comments: list[CommentObservation]) -> list[CommentObservation]:
        """Top-level comments (not replies) are candidates for reply."""
        return [c for c in comments if not c.is_reply()]

    # ── Write methods (STUBBED — never auto-executed) ──

    def publish_text_status(self, message: str) -> str:
        """Publish a text status to the Page.

        **STUBBED. NEVER auto-executed.** Only callable through:
        model decision -> validation -> safety -> rate-limit -> approval -> executor.

        The deterministic implementation clicks the post composer, types the
        message, and clicks publish — but this is only invoked when an
        approved ProposedAction reaches the executor step.
        """
        if self._config.unattended_bot_mode == "dry_run":
            raise TransportError(
                "publish_text_status blocked in dry_run mode. "
                "Set UNATTENDED_BOT_MODE=live and ensure explicit approval."
            )
        # Actual UI automation would go here (fill composer, click post).
        # NOT implemented during development phase.
        raise NotImplementedError(
            "publish_text_status UI automation is prepared but not yet wired. "
            "Only Graph API transport can execute writes in live mode currently."
        )

    def reply_to_comment(self, target_comment_id: str, message: str) -> str:
        """Reply to a Facebook Page comment.

        **STUBBED. NEVER auto-executed.** Only callable through:
        model decision -> validation -> safety -> rate-limit -> approval -> executor.

        The deterministic implementation locates the comment's "Reply" button,
        fills the reply box, and submits — but only when an approved
        ProposedAction reaches the executor step.
        """
        if self._config.unattended_bot_mode == "dry_run":
            raise TransportError(
                "reply_to_comment blocked in dry_run mode. "
                "Set UNATTENDED_BOT_MODE=live and ensure explicit approval."
            )
        raise NotImplementedError(
            "reply_to_comment UI automation is prepared but not yet wired. "
            "Only Graph API transport can execute writes in live mode currently."
        )
