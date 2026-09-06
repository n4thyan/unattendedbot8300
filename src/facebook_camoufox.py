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
    validation -> safety -> rate-limit -> approval -> identity preflight.

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
from enum import Enum
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

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


# ── Auth state enumeration ────────────────────────────────────────────

class AuthState(str, Enum):
    """Deterministic Facebook authentication state.

    UNKNOWN_AUTH_STATE fails closed — it never implies authenticated.
    """
    AUTHENTICATED = "authenticated"
    LOGIN_REQUIRED = "login_required"
    CHECKPOINT_REQUIRED = "checkpoint_required"
    UNKNOWN_AUTH_STATE = "unknown_auth_state"


# ── Facebook identity states ──────────────────────────────────────────

class IdentityState(str, Enum):
    """Result of a Page-identity verification check."""
    PAGE_IDENTITY_CONFIRMED = "page_identity_confirmed"
    PERSONAL_IDENTITY_ACTIVE = "personal_identity_active"
    IDENTITY_UNKNOWN = "identity_unknown"
    PAGE_SWITCH_FAILED = "page_switch_failed"


class ChallengeType(str, Enum):
    """Types of Facebook security challenges that may require intervention."""
    CAPTCHA = "captcha"
    TWO_FACTOR = "two_factor"
    CHECKPOINT = "checkpoint"
    UNKNOWN = "unknown"


# ── Challenge / CAPTCHA intervention abstraction ──────────────────────

@runtime_checkable
class ChallengeHandler(Protocol):
    """Protocol for services that can resolve Facebook security challenges.

    Concrete implementations are decoupled from the transport so the transport
    is never tightly coupled to one solving method.
    """

    def can_handle(self, challenge: ChallengeType) -> bool:
        """Return True if this handler can attempt to resolve the challenge."""
        ...

    def handle(self, challenge: ChallengeType, context: dict) -> bool:
        """Attempt to resolve ``challenge``.  Return True on success.

        ``context`` contains non-sensitive metadata: challenge_type, url,
        screenshot_path, and any detected form field hints.
        """
        ...


class HumanInterventionHandler:
    """Default challenge handler: pause for manual human resolution.

    Always leaves the headed browser visible and emits HUMAN_INTERVENTION_REQUIRED.
    """

    def can_handle(self, challenge: ChallengeType) -> bool:
        return True

    def handle(self, challenge: ChallengeType, context: dict) -> bool:
        print(
            "HUMAN_INTERVENTION_REQUIRED\n"
            f"A Facebook security challenge was detected (type: {challenge.value}).\n"
            "The browser window remains open for manual resolution.\n"
            "Please complete the challenge in the browser, then press ENTER here."
        )
        try:
            input("Press ENTER after resolving the challenge... ")
        except EOFError:
            pass
        return True  # we assume the human resolved it; re-validation follows


class TwoCaptchaHandler(HumanInterventionHandler):
    """Automated challenge handler using the 2captcha service.

    This is the *live* solver path.  It only activates when
    ``config.twocaptcha_api_key`` is set.  The key is read from the local
    environment only — never stored in source, config, or .env committed to
    the repo.

    If the user has not configured a key, it falls back to human intervention.
    """

    CAPTCHA_FALLBACK_SELECTORS = {
        "captcha_image": "img[alt*='captcha'], img[alt*='CAPTCHA'], div[id*='captcha'] img",
        "captcha_input": "input[name*='captcha'], input[name*='captcha_challenge'], input[id*='captcha']",
    }

    def __init__(self, config: Config):
        self._api_key = config.twocaptcha_api_key
        self._config = config
        self._fallback = HumanInterventionHandler()

    def can_handle(self, challenge: ChallengeType) -> bool:
        # We can attempt automated handling for CAPTCHA; for everything else,
        # delegate to the human fallback.
        return challenge == ChallengeType.CAPTCHA and bool(self._api_key)

    def handle(self, challenge: ChallengeType, context: dict) -> bool:
        if not self._api_key:
            return self._fallback.handle(challenge, context)
        if challenge != ChallengeType.CAPTCHA:
            return self._fallback.handle(challenge, context)

        # Import locally so the transport works without 2captcha deps at all
        try:
            from twocaptcha import TwoCaptcha  # type: ignore
        except Exception:
            # Dormant / no 2captcha SDK installed — fall back to human
            return self._fallback.handle(challenge, context)

        page = context.get("page")
        if page is None:
            return self._fallback.handle(challenge, context)

        # Detect the CAPTCHA image and input
        captcha_image_url = self._extract_captcha_image(page, context)
        if not captcha_image_url:
            return self._fallback.handle(challenge, context)

        solver = TwoCaptcha(self._api_key)
        try:
            result = solver.normal(captcha_image_url)
            code = result.get("code", "")
            if not code:
                return self._fallback.handle(challenge, context)
        except Exception as e:
            return self._fallback.handle(challenge, context)

        # Fill the captured code and submit
        try:
            page.fill(self.CAPTCHA_FALLBACK_SELECTORS["captcha_input"], code)
            # Try to submit any visible form/button
            submit_btn = page.query_selector("button[type='submit'], input[type='submit']")
            if submit_btn:
                submit_btn.click()
        except Exception:
            return self._fallback.handle(challenge, context)

        return True

    def _extract_captcha_image(self, page, context: dict) -> Optional[str]:
        """Best-effort extraction of a CAPTCHA image URL from the current page."""
        selectors = self.CAPTCHA_FALLBACK_SELECTORS["captcha_image"].split(", ")
        for sel in selectors:
            try:
                el = page.query_selector(sel.strip())
                if el:
                    src = el.get_attribute("src")
                    if src:
                        return src if src.startswith("http") else f"https://www.facebook.com{src}"
            except Exception:
                continue
        # Check context for a pre-extracted URL
        return context.get("captcha_image_url")


DEFAULT_CHALLENGE_HANDLER: ChallengeHandler = HumanInterventionHandler()


# ── Centralized selector library ──────────────────────────────────────

# Facebook changes generated CSS class names frequently.  We prefer
# ARIA roles, accessible names, labels, and semantic relationships wherever
# possible.  Each selector is a Playwright locator string.
SELECTORS = {
    # Login state — LOGGED OUT indicators (positive evidence of logout form)
    "login_form": "form[action*='login']",
    "login_input_name": "input#email, input[name='email']",
    "login_input_pass": "input#pass, input[name='pass']",
    "login_button": "button[name='login']",
    "login_page_link": "a[href*='/login'], a[href*='login.php']",

    # Authenticated UI — positive evidence of an authenticated session
    "auth_feed_composer": "div[role='dialog'][aria-label*='What'], div[aria-label*='What'], div[role='button'][aria-label*='Photo'], div[data-testid='fbfeed-composer-inputifier-input']",
    "auth_home_link": "a[aria-label*='Home'], a[href='/']",
    "auth_profile_menu": "div[role='button'][aria-label*='profile'], div[data-testid='fb-profile-menu'], div[aria-label*='profile picture'], div[aria-haspopup='menu']",
    "auth_watch_link": "a[aria-label*='Watch'], a[aria-label*='Reels']",

    # Post composer (write only — never auto-invoked)
    "post_composer_textarea": "div[role='dialog'] div[role='textbox'], div[data-block='true']",

    # Account / Page switcher
    "page_switcher_button": "div[role='button'][aria-label*='Switch'], div[data-testid='profile-card-button-switch'], a[aria-label*='Switch to']",
    "page_switcher_menu": "div[data-testid='profile-card'], div[aria-label*='Switch']",
    "page_option_by_name": None,  # set dynamically per Page name

    # ── Page identity verification (calibrated against live DOM) ──
    # On the home feed with Page identity active:
    #   - composer aria-label="Create a post" and text "What's on your mind, <Name>?"
    #   - left sidebar has a link with text=PageName and href=profile.php?id=...
    #   - left sidebar shows Page-specific controls: Professional dashboard,
    #     Ads Manager, Ad Centre
    # On the actual Page profile URL:
    #   - h1 heading contains the Page display name
    #   - URL is profile.php?id=<numeric_id> (NOT /slug)
    "page_header_title": "h1",
    "page_composer_region": "div[aria-label='Create a post']",
    "page_sidebar_identity_link": None,  # set dynamically: a[href*='profile.php?'] with text
    "page_sidebar_link_by_text": None,   # set dynamically: a >> text=/<PageName>/
    "page_control_professional_dashboard": "a[href*='professional_dashboard'], span >> text=/Professional dashboard/",
    "page_control_ads_manager": "a[href*='ads_manager'], a[href*='ad_campaign/landing'], span >> text=/Ads Manager/",
    "page_control_ad_centre": "a[href*='ad_center'], span >> text=/Ad Centre/",
    "page_identity_badge": None,  # set dynamically

    # Posts on a Page timeline / home feed
    # div[role='article'] in current Facebook DOM are loading-state
    # placeholders that may never populate in the Playwright DOM (virtualised
    # feed).  The real post message text lives in div[dir='auto'] within the
    # main feed area.
    "post_container": "div[role='article']",
    "post_message": "div[dir='auto'][data-ad-rendering-role='story_message'], div[dir='auto'] > div > div > div > div, div[role='article'] div[data-xf-comment='true'], div[role='article'] div[data-testid='post-message']",
    "post_text_fallback": "div[dir='auto']",
    "post_feed_main": "div[role='main']",

    # Comments
    "comment_container": "ul[data-testid='fb-ufeedback-comments'], div[data-xf-comment='true'], div[role='button'][aria-label*='Comment']",
    "comment_body": "div[data-testid='comment-body'], div[data-xf-comment-body='true'], div[dir='auto']",

    # Checkpoint / security challenge
    "checkpoint_form": "form#checkpoint_form, form[action*='checkpoint']",
    "checkpoint_input": "input[name='checkpoint_data'], input[name='approvals_code']",
    "twofa_input": "input[name='approval_code'], input[name='nucleus_otp']",
    "security_alert": "div[data-testid='security-checkpoint'], div[role='alert']",
    "captcha_image": "img[alt*='captcha'], img[alt*='CAPTCHA'], div[id*='captcha'] img",
    "captcha_input": "input[name*='captcha'], input[name*='captcha_challenge'], input[id*='captcha']",
}

# Facebook Page URL (the canonical Page for UnattendedBot8300)
# Default slug — overridden by config.FACEBOOK_PAGE_SLUG
DEFAULT_PAGE_SLUG = "UnattendedBot8300"
DEFAULT_PAGE_URL = "https://www.facebook.com/UnattendedBot8300"

# Explicit wait timeouts (seconds)
DEFAULT_TIMEOUT = 15
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
        self._page_slug = config.facebook_page_slug or DEFAULT_PAGE_SLUG
        self._page_url_value = config.facebook_page_url or DEFAULT_PAGE_URL
        self._browser = None
        self._page = None

        # Runtime directories for screenshots / manifests
        self._runtime_dir = config.project_root / "runtime"
        self._references_dir = self._runtime_dir / "browser-references" / "sessions"
        self._failures_dir = self._runtime_dir / "browser-references" / "failures"

        # Screenshot retention (days)
        self._screenshot_retention_days = 14
        self._screenshots_enabled = True

        # Challenge handler (defaults to human intervention; 2captcha wires in
        # when a key is configured)
        self._challenge_handler: ChallengeHandler = self._build_challenge_handler()

    def _build_challenge_handler(self) -> ChallengeHandler:
        """Wire the appropriate challenge handler based on config."""
        if self._config.twocaptcha_api_key:
            return TwoCaptchaHandler(self._config)
        return DEFAULT_CHALLENGE_HANDLER

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
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        d = self._references_dir / ts
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_reference_screenshot(self, label: str, metadata: Optional[dict] = None) -> Optional[Path]:
        """Save a non-sensitive reference screenshot during a browser run.

        Screenshots go to runtime/browser-references/sessions/<timestamp>/
        which is gitignored and never committed.
        """
        if not self._screenshots_enabled or not self._page:
            return None

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

        log_path = self._failures_dir / f"{ts}-{safe_label}.log"
        with open(log_path, "w") as f:
            f.write(f"timestamp: {ts}\nlabel: {label}\nerror: {error}\n")
            f.write(f"url: {self._safe_url()}\n")
        return filepath

    def cleanup_old_references(self) -> int:
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

        Uses FACEBOOK_PAGE_URL (browser UI config) — NOT the numeric Graph
        API Page ID.  The numeric FACEBOOK_PAGE_ID is only for graph_api.
        """
        return self._page_url_value

    def _safe_url(self) -> str:
        if not self._page:
            return ""
        url = self._page.url or ""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    # ── Authentication detection ──

    def detect_auth_state(self) -> AuthState:
        """Deterministically establish Facebook authentication state.

        This is derived from the CURRENT rendered Facebook UI, not from
        cookies, URL, or page title.  Positive authenticated evidence is
        required before returning AUTHENTICATED.

        Returns one of:
          AUTHENTICATED   — positively detected authenticated UI
          LOGIN_REQUIRED  — login form / email+password inputs visible
          CHECKPOINT_REQUIRED — 2FA / checkpoint challenge visible
          UNKNOWN_AUTH_STATE   — ambiguous; fails closed
        """
        page = self._page
        if page is None:
            return AuthState.UNKNOWN_AUTH_STATE

        try:
            page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT * 1000)
        except Exception:
            return AuthState.UNKNOWN_AUTH_STATE

        # ── 1. Detect challenges / checkpoints FIRST (they override auth) ──
        challenge = self._detect_challenge()
        if challenge is not None:
            if challenge == ChallengeType.CAPTCHA:
                return AuthState.UNKNOWN_AUTH_STATE
            return AuthState.CHECKPOINT_REQUIRED

        # ── 2. Detect LOGGED-OUT UI (positive login-form evidence) ──
        if self._detect_login_form_present():
            return AuthState.LOGIN_REQUIRED

        # ── 3. Detect AUTHENTICATED UI (positive evidence required) ──
        if self._detect_authenticated_ui():
            return AuthState.AUTHENTICATED

        # ── 4. Nothing positive detected → fail closed ──
        return AuthState.UNKNOWN_AUTH_STATE

    def _detect_login_form_present(self) -> bool:
        """Return True if the rendered UI positively shows a Facebook login form.

        Requires BOTH an email/phone field AND a password field AND a login
        button to be present — this is the exact regression that previously
        failed: a cookie existed but the login form was still visible.
        """
        page = self._page
        if page is None:
            return False

        login_form = None
        try:
            login_form = page.query_selector(SELECTORS["login_form"])
        except Exception:
            pass

        email_input = None
        pass_input = None
        login_btn = None
        try:
            email_input = page.query_selector(SELECTORS["login_input_name"])
            pass_input = page.query_selector(SELECTORS["login_input_pass"])
            login_btn = page.query_selector(SELECTORS["login_button"])
        except Exception:
            pass

        # Positive evidence: all three key login elements are present
        if email_input is not None and pass_input is not None and login_btn is not None:
            return True
        # Also if an explicit login form element wraps them
        if login_form is not None and (email_input is not None or pass_input is not None):
            return True

        # Check for explicit "Log In" link on a logged-out landing
        try:
            login_link = page.query_selector(SELECTORS["login_page_link"])
            if login_link is not None:
                # Verify there's an email input too (login page vs generic link)
                if email_input is not None:
                    return True
        except Exception:
            pass

        return False

    def _detect_authenticated_ui(self) -> bool:
        """Return True only if the rendered UI positively shows an
        authenticated Facebook session.

        Requires positive evidence of authenticated navigation controls —
        NOT merely the absence of a login form and NOT cookie presence.
        """
        page = self._page
        if page is None:
            return False

        # Evidence 1: profile/account menu button (avatar or name dropdown)
        try:
            profile_menu = page.query_selector(SELECTORS["auth_profile_menu"])
            if profile_menu is not None:
                return True
        except Exception:
            pass

        # Evidence 2: home/feed navigation link (authenticated users see Home)
        try:
            home_link = page.query_selector(SELECTORS["auth_home_link"])
            if home_link is not None:
                return True
        except Exception:
            pass

        # Evidence 3: feed/post composer on the Facebook home page
        try:
            composer = page.query_selector(SELECTORS["auth_feed_composer"])
            if composer is not None:
                return True
        except Exception:
            pass

        # Evidence 4: Watch/Reels links visible to authenticated users
        try:
            watch_link = page.query_selector(SELECTORS["auth_watch_link"])
            if watch_link is not None:
                return True
        except Exception:
            pass

        return False

    def _detect_challenge(self) -> Optional[ChallengeType]:
        """Detect if Facebook has presented a security challenge.

        Returns ChallengeType if a challenge is detected, None otherwise.
        """
        page = self._page
        if page is None:
            return None

        # 2FA / OTP input
        try:
            otp = page.query_selector(SELECTORS["twofa_input"])
            if otp is not None:
                return ChallengeType.TWO_FACTOR
        except Exception:
            pass

        # Checkpoint form
        try:
            cp = page.query_selector(SELECTORS["checkpoint_form"])
            if cp is not None:
                return ChallengeType.CHECKPOINT
        except Exception:
            pass

        # Security alert
        try:
            alert = page.query_selector(SELECTORS["security_alert"])
            if alert is not None:
                return ChallengeType.CHECKPOINT
        except Exception:
            pass

        # CAPTCHA image + input
        try:
            cap_img = page.query_selector(SELECTORS["captcha_image"])
            cap_in = page.query_selector(SELECTORS["captcha_input"])
            if cap_img is not None or cap_in is not None:
                return ChallengeType.CAPTCHA
        except Exception:
            pass

        return None

    def _handle_challenge(self, challenge: ChallengeType, context: Optional[dict] = None) -> None:
        """Handle a detected challenge via the configured handler.

        Saves a safe reference screenshot and delegates to the handler.
        Raises CheckpointError after handler returns so the caller can
        re-validate auth state.
        """
        ctx = context or {}
        ctx["challenge_type"] = challenge.value
        ctx["url"] = self._safe_url()
        ctx["page"] = self._page
        ctx["screenshot_dir"] = str(self._failures_dir)

        screenshot_path = self.save_failure_screenshot(
            f"challenge-{challenge.value}",
            f"Facebook security challenge: {challenge.value}",
        )
        ctx["screenshot_path"] = str(screenshot_path) if screenshot_path else None

        # Delegate to the configured handler (human or 2captcha)
        self._challenge_handler.handle(challenge, ctx)

        raise CheckpointError(
            f"Facebook presented a {challenge.value} challenge. "
            f"The browser remains open. After manual resolution, re-run to "
            f"re-validate authentication state."
        )

    def check_login_state(self) -> bool:
        """Backward-compatible wrapper: returns True only if AUTHENTICATED.

        This replaces the old broken implementation that returned True merely
        because a login form was *not* found.  A cookie existing does NOT
        prove an authenticated Facebook session — positive UI evidence is
        required.
        """
        return self.detect_auth_state() == AuthState.AUTHENTICATED

    # ── Page identity / profile switching ──

    def get_active_facebook_identity(self) -> str:
        """Return the currently-active Facebook identity (profile or Page name).

        Inspects Facebook's actual rendered profile-switcher / account menu.
        On the home feed with a Page identity active, the sidebar identity link
        (e.g. text "UnattendedBot8300" with href "profile.php?id=...") is the
        most reliable positive signal.  We also check the composer placeholder
        and the top-right profile menu button.

        Returns the visible identity label, or an empty string if it cannot
        be positively determined.
        """
        page = self._page
        if page is None:
            return ""

        # ── Signal 1: left-sidebar identity link with profile.php href ──
        # Calibrated against live DOM: when the Page identity is active,
        # the left sidebar contains <a href="profile.php?id=<id>">UnattendedBot8300</a>
        try:
            sidebar_links = page.eval_on_selector_all(
                "div[role='navigation'] a[href*='profile.php?id']",
                "(els) => els.map(e => (e.textContent || '').trim())",
            )
            for text in sidebar_links:
                if text:
                    return text
        except Exception:
            pass

        # ── Signal 2: top-right profile menu button (aria-label) ──
        # On the real Facebook DOM, the top-right button has aria-label="Your profile"
        # (a generic navigation label) when the PERSONAL profile is active — NOT the
        # personal name.  When a Page is active, the aria-label is the Page name.
        # We must NOT return "Your profile" as an identity name; it is a UI
        # navigation label, not an identity.
        candidates = [
            SELECTORS["auth_profile_menu"],
            "div[aria-label*='profile']",
            "span[title]",
            "a[aria-current='page']",
            "div[data-testid='profile-card-button-name']",
        ]
        for sel in candidates:
            try:
                el = page.query_selector(sel)
                if el is not None:
                    name = (el.get_attribute("aria-label") or "").strip()
                    if not name:
                        name = (el.text_content() or "").strip()
                    if not name:
                        name = (el.get_attribute("title") or "").strip()
                    # Skip generic UI labels — they are not identity names
                    if name and name.lower() not in ("your profile", "profile"):
                        return name
            except Exception:
                continue

        # ── Signal 3: h1 / Page header heading ──
        try:
            h1 = page.query_selector(SELECTORS["page_header_title"])
            if h1 is not None:
                text = (h1.text_content() or "").strip()
                if text:
                    return text
        except Exception:
            pass

        return ""

    def _open_page_switcher(self) -> bool:
        """Open the Facebook account/Page switcher menu.

        Returns True if the switcher was opened, False if it could not be
        found or clicked.
        """
        page = self._page
        if page is None:
            return False

        # Look for the profile menu button — the entry point to the switcher
        trigger_selectors = [
            SELECTORS["auth_profile_menu"],
            "div[aria-label*='profile'][role='button']",
            "div[data-testid='profile-card-button']",
            "a[aria-label*='profile']",
            "div[role='button'][aria-haspopup='menu']",
        ]
        for sel in trigger_selectors:
            try:
                el = page.query_selector(sel)
                if el is not None:
                    el.click()
                    return True
            except Exception:
                continue
        return False

    def switch_to_page_identity(self, page_name: str) -> bool:
        """Switch the active Facebook identity to the named managed Page.

        Inspects the real Facebook profile/Page switcher UI and clicks
        the option matching ``page_name``.

        Returns True on success, False if the switch could not be performed.

        Note: Facebook's Page switcher is a dropdown menu.  The Page option
        may be rendered as a menuitem, a link, or nested text.  We try
        multiple selector strategies to find the matching entry.
        """
        page = self._page
        if page is None:
            return False

        if not self._open_page_switcher():
            return False

        # Wait for the switcher menu to render (bounded, not a fixed sleep)
        try:
            page.wait_for_selector(
                "div[role='menu'], div[role='menuitem'], div[data-visualcompletion='']",
                state="attached",
                timeout=DEFAULT_TIMEOUT * 1000,
            )
        except Exception:
            pass

        # Look for a link/menu-item matching the Page name.
        # We cast a wide net: Facebook renders managed Pages with their display
        # name as visible text, sometimes inside a span, sometimes as an
        # aria-label on a menuitem, sometimes as a link text.
        option_selectors = [
            f"div[role='menuitem'][aria-label*='{re.escape(page_name)}']",
            f"span >> text=/{re.escape(page_name)}/i",
            f"a >> text=/{re.escape(page_name)}/i",
            f"div[role='menuitem'] >> text=/{re.escape(page_name)}/i",
            f"span[id*='text'] >> text=/{re.escape(page_name)}/",
            f"a[href*='/pages/{re.escape(page_name)}']",
            f"a[href*='/{re.escape(page_name)}']",
        ]
        for sel in option_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.first.click()
                    return True
            except Exception:
                continue

        return False

    def _detect_page_identity_from_composer(self, page_name: str) -> bool:
        """Check whether the post composer shows the Page identity.

        When a Page identity is active, the composer placeholder reads
        'What's on your mind, <PageName>?'.  This is a strong positive
        signal because it is dynamically personalized to the active identity.
        """
        page = self._page
        if page is None:
            return False
        target = page_name.lower()
        try:
            composer = page.query_selector(SELECTORS["page_composer_region"])
            if composer is None:
                return False
            text = (composer.text_content() or "").strip().lower()
            if "what's on your mind" in text and target in text:
                return True
        except Exception:
            pass
        # Fallback: search body text for the composer signature
        try:
            body = page.eval_on_selector("body", "el => el.innerText") or ""
            body_lower = body.lower()
            if "what's on your mind" in body_lower and target in body_lower:
                idx = body_lower.find("what's on your mind")
                snippet = body[idx:idx + 80].lower()
                if target in snippet:
                    return True
        except Exception:
            pass
        return False

    def _detect_page_identity_from_sidebar(self, page_name: str) -> bool:
        """Check whether the left sidebar shows the target Page identity.

        When a Page identity is active, the left sidebar contains a link
        with the Page display name as its text and a profile.php?id= href.
        """
        page = self._page
        if page is None:
            return False
        target = page_name.lower()
        try:
            links = page.eval_on_selector_all(
                "div[role='navigation'] a",
                "(els) => els.map(e => ({text: (e.textContent||'').trim(), href: e.href || ''}))",
            )
            for link in links:
                if target in link["text"].lower() and "profile.php?id=" in link["href"]:
                    return True
            # Also check by text alone (more lenient than href)
            for link in links:
                if target in link["text"].lower():
                    return True
        except Exception:
            pass
        return False

    def _detect_page_identity_from_controls(self, page_name: str) -> bool:
        """Check whether Page-specific controls are visible in the sidebar.

        When a Page identity is active (vs personal), the left sidebar shows
        Page-management controls such as 'Professional dashboard', 'Ads Manager',
        and 'Ad Centre'.  These are strong positive signals that the active
        identity is a Page, not a personal profile.
        """
        page = self._page
        if page is None:
            return False
        signals = 0
        for key in ("page_control_professional_dashboard", "page_control_ads_manager",
                     "page_control_ad_centre"):
            try:
                el = page.query_selector(SELECTORS[key])
                if el is not None:
                    text = (el.text_content() or "").strip().lower()
                    if text and len(text) > 2:
                        signals += 1
            except Exception:
                pass
        # 2+ Page-control signals is strong evidence of Page identity
        return signals >= 2

    def _is_personal_identity_active(self) -> bool:
        """Check whether the personal profile (not a Page) is the active identity.

        On the home feed, the personal profile does NOT show Page-management
        controls (Professional dashboard, Ads Manager, Ad Centre) in the
        sidebar, and the composer reads 'What's on your mind, <Name>?' for
        the personal profile name.
        """
        page = self._page
        if page is None:
            return False
        # If the profile menu button says "Your profile" (not a Page name)
        # and there are no Page controls, we're on the personal identity.
        try:
            profile_btn = page.query_selector(SELECTORS["auth_profile_menu"])
            if profile_btn is not None:
                aria = (profile_btn.get_attribute("aria-label") or "").strip()
                if aria == "Your profile":
                    # Check that Page controls are NOT present
                    has_pg_controls = self._detect_page_identity_from_controls(
                        self._page_slug
                    )
                    if not has_pg_controls:
                        return True
        except Exception:
            pass
        return False

    def verify_page_identity(self, page_name: Optional[str] = None) -> IdentityState:
        """Positively verify that the active Facebook identity is the target Page.

        Uses multiple independent signals calibrated against the live DOM:

        On the home feed (Page identity active):
          - Composer text: "What's on your mind, <PageName>?" (composer signal)
          - Left sidebar identity link with text=PageName (sidebar signal)
          - Page-specific controls: Professional dashboard, Ads Manager, Ad Centre
            (controls signal)

        On the Page profile URL (profile.php?id=<id>):
          - h1 "Manage Page" or h2 with the Page display name (heading signal)
          - URL contains profile.php?id= (URL signal)
          - profile link href points to profile.php?id= (link signal)
          - Body text contains the Page name + "Manage Page" / "Page profile content"
            (body text signal)

        Requires 2+ independent positive signals before returning
        PAGE_IDENTITY_CONFIRMED.  Anything else must block writes.
        """
        page = self._page
        if page is None:
            return IdentityState.IDENTITY_UNKNOWN

        target = page_name or self._page_slug
        target_lower = target.lower()

        # ── Signal 1: Composer shows Page-personalized placeholder ──
        # Home feed with Page identity: "What's on your mind, UnattendedBot8300?"
        composer_match = self._detect_page_identity_from_composer(target)

        # ── Signal 2: Left sidebar shows Page identity link ──
        sidebar_match = self._detect_page_identity_from_sidebar(target)

        # ── Signal 3: Page-specific controls visible ──
        controls_match = self._detect_page_identity_from_controls(target)

        # ── Signal 4: heading (h1 or h2) contains the Page display name ──
        heading_match = False
        try:
            headings = page.eval_on_selector_all(
                "h1, h2, div[role='heading']",
                "(els) => els.map(e => (e.textContent || '').trim().toLowerCase())",
            )
            heading_match = any(target_lower in h for h in headings if h)
        except Exception:
            try:
                h1 = page.query_selector(SELECTORS["page_header_title"])
                if h1 is not None:
                    h1_text = (h1.text_content() or "").strip().lower()
                    if target_lower in h1_text:
                        heading_match = True
            except Exception:
                pass

        # ── Signal 5: URL path contains profile.php?id= OR the page slug ──
        url = self._safe_url().lower()
        url_match = ("profile.php?id=" in url or target_lower in url)

        # ── Signal 6: profile link href contains profile.php?id= with Page name ──
        link_match = False
        try:
            links = page.eval_on_selector_all(
                "a[href*='profile.php?id=']",
                "(els) => els.map(e => ({href: e.href, text: (e.textContent||'').trim()}))",
            )
            link_match = any(target_lower in l["text"].lower() for l in links)
        except Exception:
            pass

        # ── Signal 7: active identity label matches ──
        identity_label = self.get_active_facebook_identity().lower()
        identity_match = target_lower in identity_label if identity_label else False

        # ── Signal 8: body text contains Page name + Page-management context ──
        # On the Page profile URL, "Manage Page" heading + Page name in body
        # confirms we're on the correct Page.
        body_page_match = False
        try:
            body_text = page.eval_on_selector("body", "el => el.innerText") or ""
            body_lower = body_text.lower()
            if target_lower in body_lower and ("manage page" in body_lower or
                                                "page profile" in body_lower or
                                                "comment as" in body_lower):
                body_page_match = True
        except Exception:
            pass

        positive_signals = sum([
            composer_match,      # home feed, Page active
            sidebar_match,       # home feed, Page active
            controls_match,      # home feed, Page active
            heading_match,       # Page profile URL / home feed
            url_match,           # Page profile URL
            link_match,          # Page profile URL / sidebar
            identity_match,      # any context where identity label shows Page name
            body_page_match,     # Page profile URL body text
        ])

        if positive_signals >= 2:
            return IdentityState.PAGE_IDENTITY_CONFIRMED

        if positive_signals == 1:
            # Single weak signal — not enough to confirm a Page identity.
            # Could be the personal profile viewing the Page, or a stale
            # identity indicator.  Fail closed.
            return IdentityState.IDENTITY_UNKNOWN

        # No positive signals — determine whether personal is active
        if self._is_personal_identity_active():
            return IdentityState.PERSONAL_IDENTITY_ACTIVE
        return IdentityState.IDENTITY_UNKNOWN

    def ensure_page_identity(self, page_name: Optional[str] = None) -> IdentityState:
        """Ensure the active Facebook identity is the target Page.

        1. Check current identity.
        2. If not the Page, attempt to switch via the profile switcher.
        3. Wait for the Page UI transition to settle using Playwright
           expectations (NOT a fixed sleep).
        4. Re-verify after the transition.

        The key fix: after clicking the Page option, Facebook needs time to
        re-render the UI (sidebar, composer, controls).  We wait for a
        POSITIVE Page-identity signal to appear, with a bounded timeout.
        Only if the timeout expires without positive evidence do we return
        PAGE_SWITCH_FAILED.
        """
        target = page_name or self._page_slug

        # Step 1: Check current identity
        current = self.verify_page_identity(target)
        if current == IdentityState.PAGE_IDENTITY_CONFIRMED:
            return current

        # Step 2: Attempt to switch
        switched = self.switch_to_page_identity(target)
        if not switched:
            return IdentityState.PAGE_SWITCH_FAILED

        # Step 3: Wait for the Page UI transition to settle.
        # We poll for positive identity evidence with a bounded timeout,
        # using wait_for_function instead of a fixed sleep.
        page = self._page
        target_lower = target.lower()
        deadline_ms = PAGE_LOAD_TIMEOUT * 1000

        try:
            page.wait_for_function(
                f"""
                (target) => {{
                    const bodyText = document.body.innerText || '';
                    const bodyLower = bodyText.toLowerCase();
                    const composerMatch = bodyLower.includes("what's on your mind") &&
                                          bodyLower.includes(target);
                    const sidebarLinks = Array.from(document.querySelectorAll('div[role="navigation"] a'))
                        .map(a => (a.textContent || '').trim().toLowerCase())
                        .some(t => t.includes(target));
                    return composerMatch || sidebarLinks;
                }}
                """,
                arg=target_lower,
                timeout=deadline_ms,
            )
        except Exception:
            # Timeout expired without positive signal
            pass

        # Also wait for networkidle to settle any pending navigation
        try:
            page.wait_for_load_state("domcontentloaded", timeout=deadline_ms)
        except Exception:
            pass

        # Step 4: Re-verify with the calibrated signals
        after = self.verify_page_identity(target)
        return after

    def discover_page_url(self) -> Optional[str]:
        """Discover the actual canonical Page/profile URL from the live Facebook UI.

        This is called AFTER the Page identity is confirmed active.  It does NOT
        rely on a guessed slug — it reads the real href from the rendered DOM.

        Facebook Pages often have a numeric profile.php?id=<id> URL that does
        NOT match the display name slug.  We inspect:
          - Left sidebar identity link (href of the link with Page-name text)
          - Profile picture / avatar link
          - Page header profile link
          - Any <a> whose text or aria-label contains the target Page name

        Returns the discovered canonical Page URL, or None if not found.
        """
        page = self._page
        if page is None:
            return None

        target = self._page_slug.lower()

        # Signal 1: Left sidebar link with the Page name
        try:
            links = page.eval_on_selector_all(
                "div[role='navigation'] a",
                "(els) => els.map(e => ({href: e.href || '', text: (e.textContent||'').trim().toLowerCase()}))",
            )
            for link in links:
                if target in link["text"] and link["href"]:
                    return link["href"]
        except Exception:
            pass

        # Signal 2: Any link whose aria-label or text contains the target
        try:
            links = page.eval_on_selector_all(
                f"a[aria-label*='{target}'], a[aria-label*='{self._page_slug}']",
                "(els) => els.map(e => e.href || '')",
            )
            for href in links:
                if href and "facebook.com" in href:
                    return href
        except Exception:
            pass

        # Signal 3: h1 heading context — find nearby profile link
        try:
            h1 = page.query_selector(SELECTORS["page_header_title"])
            if h1 is not None:
                h1_text = (h1.text_content() or "").strip().lower()
                if target in h1_text:
                    # Walk up to find a parent <a> link
                    parent_link = page.eval_on_selector(
                        SELECTORS["page_header_title"],
                        "(el) => { let p = el.parentElement; while (p && p.tagName !== 'A') p = p.parentElement; return p ? p.href : ''; }",
                    )
                    if parent_link and "facebook.com" in parent_link:
                        return parent_link
        except Exception:
            pass

        # Signal 4: Current page URL if it already looks like a Page profile
        current_url = self._safe_url()
        if "profile.php?id=" in current_url:
            return current_url

        return None

    # ── Fail-closed write preflight ──

    def _verify_write_preflight(self) -> IdentityState:
        """Mandatory preflight before ANY Facebook write.

        Checks:
          1. Authenticated session (positive UI evidence).
          2. Active identity == target Page.

        Raises TransportError if the preflight fails.  This is the hard
        safety boundary that prevents writing as the personal account.
        """
        # 1. Auth check
        auth = self.detect_auth_state()
        if auth != AuthState.AUTHENTICATED:
            raise TransportError(
                f"Write blocked: authentication state is {auth.value}, "
                f"not AUTHENTICATED. No Facebook writes will occur."
            )

        # 2. Identity check
        identity = self.ensure_page_identity(self._page_slug)
        if identity != IdentityState.PAGE_IDENTITY_CONFIRMED:
            raise TransportError(
                f"Write blocked: active identity is {identity.value}, "
                f"not PAGE_IDENTITY_CONFIRMED. "
                f"The bot will NEVER accidentally publish to the personal account."
            )

        return identity

    # ── Core observation methods ──

    def observe(self) -> ObservationResult:
        """Read-only observation of the UnattendedBot8300 Facebook Page.

        Flow:
          1. Open Camoufox (persistent profile) headed.
          2. Navigate to Facebook.
          3. Deterministically establish auth state.
          4. If logged out: HUMAN_LOGIN_REQUIRED, leave browser visible.
          5. Verify authenticated state.
          6. Ensure Page identity (switch if needed).
          7. Navigate to the UnattendedBot8300 Page.
          8. Verify correct Page.
          9. Read recent posts + visible comments.
         10. Normalise into ObservationResult.
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

            # 3. Deterministically establish auth state
            auth_state = self.detect_auth_state()

            # 4. If logged out → HUMAN_LOGIN_REQUIRED
            if auth_state == AuthState.LOGIN_REQUIRED:
                self.save_failure_screenshot("login-required", "Login form detected")
                print(
                    "HUMAN_LOGIN_REQUIRED\n"
                    "Complete Facebook login in the visible Camoufox window.\n"
                    "Automation is waiting for authenticated Facebook UI.\n"
                    "After successful login, press ENTER here to continue."
                )
                try:
                    input("Press ENTER after completing Facebook login... ")
                except EOFError:
                    pass

                # Re-navigate and re-verify auth
                self._page.goto(self._facebook_url(), timeout=PAGE_LOAD_TIMEOUT * 1000)
                self.save_reference_screenshot("post-login-facebook", {"state": "post_login"})
                auth_state = self.detect_auth_state()
                if auth_state != AuthState.AUTHENTICATED:
                    self.save_failure_screenshot("login-still-required", str(auth_state))
                    raise NotLoggedInError(
                        f"Facebook login was not completed. Auth state: {auth_state.value}. "
                        f"The browser remains open for manual login."
                    )

            # Handle checkpoint / challenges
            if auth_state == AuthState.CHECKPOINT_REQUIRED:
                challenge = self._detect_challenge() or ChallengeType.CHECKPOINT
                self._handle_challenge(challenge)
                raise CheckpointError(
                    "Facebook presented a security challenge. The browser has been "
                    "left open. Please complete the challenge manually, then re-run."
                )

            if auth_state == AuthState.UNKNOWN_AUTH_STATE:
                self.save_failure_screenshot("unknown-auth-state", "Ambiguous auth UI")
                raise NotLoggedInError(
                    "Could not positively verify Facebook authentication state. "
                    "The browser remains open — please log in manually if needed, "
                    "then re-run."
                )

            # 5. Authentication positively verified
            self.save_reference_screenshot("auth-confirmed", {"state": "authenticated"})

            # 6. Ensure Page identity (switch from personal to Page)
            identity_state = self.ensure_page_identity()
            self.save_reference_screenshot(
                "identity-check",
                {"state": identity_state.value},
            )
            if identity_state != IdentityState.PAGE_IDENTITY_CONFIRMED:
                # Not yet on the Page — navigate to it
                self.save_reference_screenshot(
                    "identity-not-confirmed",
                    {"state": identity_state.value, "action": "navigate_to_page"},
                )

            # 7. Navigate to the UnattendedBot8300 Page
            page_url = self._page_url()
            self._page.goto(page_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT * 1000)
            self.save_reference_screenshot("page-open", {
                "state": "page_open",
                "url": self._safe_url(),
            })

            # 8. Re-verify Page identity after navigation
            identity = self.verify_page_identity()
            if identity != IdentityState.PAGE_IDENTITY_CONFIRMED:
                self.save_failure_screenshot(
                    "wrong-page",
                    f"Page identity after navigation: {identity.value}",
                )
                raise PageNotFoundError(
                    f"Could not positively verify the UnattendedBot8300 Page. "
                    f"Identity state: {identity.value}. "
                    f"You may be viewing as the personal profile, or the Page "
                    f"identity was not confirmed."
                )
            self.save_reference_screenshot("page-verified", {
                "state": "page_identity_confirmed",
                "url": self._safe_url(),
            })

            # 9. Read recent posts
            posts = self._extract_posts()
            self.save_reference_screenshot("recent-posts", {"state": "posts_extracted"})

            # 10. Read comments on recent posts
            all_comments = []
            for post_obs in posts[:3]:
                try:
                    comments = self._extract_comments(post_obs)
                    all_comments.extend(comments)
                except SelectorError:
                    continue
            self.save_reference_screenshot("comments-extracted", {"state": "comments_done"})

            return ObservationResult(
                posts=posts,
                comments=all_comments,
                unreplied_comments=self._find_unreplied(all_comments),
            )

        except (NotLoggedInError, CheckpointError, PageNotFoundError,
                NavigationError, SelectorError):
            raise
        except Exception as e:
            if self._page:
                self.save_failure_screenshot("unexpected-error", str(e))
            raise TransportError(f"Camoufox observation failed: {e}") from e
        finally:
            self.cleanup_old_references()

    def _extract_page_error_text(self) -> Optional[str]:
        """Check the rendered body for Facebook error messages.

        Distinguishes genuine Page content from Facebook error pages
        (e.g. "This content isn't available right now").
        """
        try:
            body = self._page.eval_on_selector("body", "el => el.innerText") or ""
        except Exception:
            return None
        body_lower = body.lower()
        for marker in [
            "isn't available",
            "not found",
            "does not exist",
            "you can't use facebook",
            "this page isn't available",
        ]:
            if marker in body_lower:
                return marker
        return None

    def _identify_page(self) -> Optional[str]:
        """Attempt to positively identify the UnattendedBot8300 Page.

        Uses ARIA headings, accessible names, and visible text rather than
        generated CSS class names.
        """
        try:
            page_name = self._page_slug

            # Try aria-label / accessible name match
            page_header = self._page.get_by_role("heading", level=1)
            header_text = ""
            try:
                header_text = (page_header.text_content() or "").strip()
            except Exception:
                pass
            if page_name.lower() in (header_text or "").lower():
                return header_text or page_name

            # Check for Facebook error content
            error = self._extract_page_error_text()
            if error:
                return None

            # Fallback: check page title
            title = self._page.title() or ""
            if page_name.lower() in title.lower():
                return title

            # Fallback: check for a profile link with the page name
            link = self._page.get_by_role("link", name=re.compile(re.escape(page_name), re.IGNORECASE))
            if link.count() > 0:
                return page_name

            return None
        except Exception:
            raise SelectorError("Could not identify Facebook Page — layout may have changed")

    def _extract_post_permalink_from_element(self, element) -> Optional[str]:
        """Extract a Facebook permalink from a post element's rendered links."""
        try:
            links = self._page.eval_on_selector_all(
                "div[role='article'] a[href*='/posts/'], div[role='article'] a[href*='/permalink/'], div[role='article'] a[href*='/p/'], div[role='article'] a[href*='/photo/']",
                "(els) => els.map(e => e.href)",
            )
            if links:
                return links[0]
        except Exception:
            pass
        return None

    def _extract_posts_from_body_text(self) -> list[PostObservation]:
        """Fallback post extraction using body text analysis.

        When div[role='article'] elements are loading-state placeholders
        (virtualised feed), we parse the body innerText to find post-like
        content.  Posts are separated from UI elements by their content
        depth — actual post text is substantial, multi-sentence content
        that is NOT a known UI fragment.

        Calibrated against the live Facebook DOM (September 2026).
        """
        posts: list[PostObservation] = []
        try:
            # Scroll to encourage lazy-loading
            for _ in range(8):
                self._page.evaluate("window.scrollBy(0, 1000)")
                self._page.wait_for_timeout(1500)

            body_text = self._page.eval_on_selector("body", "el => el.innerText") or ""
        except Exception:
            return posts

        # Known UI fragment tokens to exclude from post messages
        ui_fragments = {
            "facebook", "menu", "home", "watch", "marketplace", "groups",
            "gaming", "messenger", "notifications", "friends", "memories",
            "saved", "reels", "see more", "meta ai", "feeds",
            "create a post", "what's on your mind", "share a photo or video",
            "create story", "search facebook", "feed posts",
            "professional dashboard", "ads manager", "ad centre",
            "your shortcuts", "suggested", "edit cover photo",
            "share a thought", "privacy", "terms", "advertising", "ad choices",
            "cookies", "more", "follow", "your profile", "number of unread",
            "unread", "manage posts", "edit profile", "edit details",
            "contact info", "featured", "details", "posts",
            "manage page", "page profile", "comment as", "create ads",
            "edit audience", "ad centre", "boost post", "settings",
            "pages", "page navigation", "actions for this post",
            "number of followers", "followers", "following",
            "finish setting up", "add the essentials", "view all",
            "not yet rated", "no context",
            # Page management UI (not posts)
            "boost instagram post", "manage your business",
            "setup business", "setup/business/profile-management",
            "if you're a business", "give people another",
            "websites are one of", "let people know where to go",
            "add your address", "link to your website",
            "add phone number", "not started",
            "add the essentials that people look for",
            "so that your page feels complete",
            "add your phone number",
        }

        # Lines that look like actual post messages: substantial text
        # (not single chars from CSS obfuscation, not UI fragments)
        lines = body_text.split("\n")
        candidate_posts = []
        for line in lines:
            text = line.strip()
            if not text:
                continue
            # Skip CSS character-obfuscation fragments (mostly single chars)
            if len(text) < 10:
                continue
            text_lower = text.lower()
            # Skip known UI fragments
            if text_lower in ui_fragments or any(k in text_lower for k in ui_fragments):
                continue
            # Skip lines that are obviously not posts (single words, etc.)
            if len(text) < 20:
                continue
            # Skip lines that look like notifications or metadata
            if text_lower.startswith("number of") or text_lower.startswith("unread"):
                continue
            candidate_posts.append(text)

        # Use unique candidate posts (dedup)
        seen = set()
        for text in candidate_posts:
            if text in seen:
                continue
            seen.add(text)
            if len(text) < 20:
                continue
            post_id = f"ui_post_body_{abs(hash(text)) % 100000}"
            posts.append(PostObservation(
                fb_post_id=post_id,
                message=text,
                created_time=datetime.now(timezone.utc).isoformat(),
                posted_by_page=False,  # These are from the home feed, not the Page
                like_count=0,
                comment_count=0,
                raw={"source": "camoufox_ui_body_text", "index": len(posts)},
            ))
            if len(posts) >= 10:
                break

        return posts

    def _extract_posts(self) -> list[PostObservation]:
        """Extract recent posts from the Page timeline or home feed.

        Uses ARIA roles (role='article' for posts) and semantic text
        extraction rather than CSS class names.

        On the current Facebook UI, div[role='article'] elements may be
        loading-state placeholders that never populate in the DOM (the feed
        is virtualised).  As a calibrated fallback, we extract post messages
        from div[dir='auto'] elements within the main feed area.
        """
        posts: list[PostObservation] = []

        # Attempt 1: standard article-based extraction
        try:
            self._page.wait_for_selector(SELECTORS["post_container"], timeout=DEFAULT_TIMEOUT * 1000)
            article_elements = self._page.locator(SELECTORS["post_container"]).all()

            # Check if articles are loading-state placeholders (no text content)
            real_articles = []
            for article in article_elements[:10]:
                try:
                    msg = self._extract_text_from_element(article)
                    if msg and len(msg) >= 1:
                        real_articles.append((article, msg))
                except Exception:
                    continue

            for idx, (article, message) in enumerate(real_articles):
                try:
                    post_id = f"ui_post_{idx}_{self._page.evaluate('Math.random().toString(36).slice(2,10)')}"
                    fb_permalink = self._extract_post_permalink_from_element(article)
                    created_time = self._extract_post_timestamp(article)
                    posts.append(PostObservation(
                        fb_post_id=fb_permalink or post_id,
                        message=message,
                        created_time=created_time or datetime.now(timezone.utc).isoformat(),
                        posted_by_page=True,
                        like_count=0,
                        comment_count=0,
                        raw={"source": "camoufox_ui", "index": idx},
                    ))
                except Exception:
                    continue
        except Exception:
            pass

        # Attempt 2: body-text fallback for virtualised feeds
        if not posts:
            posts = self._extract_posts_from_body_text()

        if not posts:
            raise SelectorError("No posts could be extracted from the Page timeline")
        return posts

    def _extract_post_permalink(self, article) -> Optional[str]:
        """Extract a Facebook permalink from a post's rendered links.

        Delegates to _extract_post_permalink_from_element which searches
        the full page DOM (since article elements may be loading-state
        placeholders in the virtualised feed).
        """
        return self._extract_post_permalink_from_element(article)

    def _extract_post_timestamp(self, article) -> Optional[str]:
        """Extract a visible timestamp from a post if reliably exposed."""
        try:
            ts = article.query_selector("div time, div[role='article'] a[aria-label*=' at '], span[data-testid='fbfeed-story-time']")
            if ts:
                title = ts.get_attribute("title") or ts.text_content()
                if title:
                    return title.strip()
        except Exception:
            pass
        return None

    def _extract_comments(self, post_obs: PostObservation) -> list[CommentObservation]:
        """Extract visible comments on a post.

        Returns an empty list if no comments are visible (not an error —
        not all posts have visible comments, and the Page profile URL
        may not render comment containers).
        """
        comments: list[CommentObservation] = []

        try:
            comment_elements = self._page.locator(SELECTORS["comment_body"]).all()
        except Exception:
            comment_elements = []

        if not comment_elements:
            return comments

        for c in comment_elements[:20]:
            try:
                text = (c.text_content() or "").strip()
                if not text:
                    continue

                # Try to extract commenter display name
                from_name = "Unknown"
                try:
                    # Facebook renders the commenter name as a sibling/parent
                    name_el = c.query_selector("div[data-testid='comment-name'], div[aria-label], a[aria-label], strong, b")
                    if name_el:
                        from_name = (name_el.text_content() or "Unknown").strip() or "Unknown"
                except Exception:
                    pass

                comments.append(CommentObservation(
                    fb_comment_id=f"ui_comment_{post_obs.fb_post_id}_{len(comments)}",
                    post_id=post_obs.fb_post_id,
                    message=text,
                    from_name=from_name,
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
        """Extract visible text from a Playwright ElementHandle/Locator."""
        try:
            msg_el = element.query_selector(SELECTORS["post_message"])
            if msg_el:
                return (msg_el.text_content() or "").strip()
        except Exception:
            pass
        try:
            return (element.text_content() or "").strip()
        except Exception:
            return ""

    def _find_unreplied(self, comments: list[CommentObservation]) -> list[CommentObservation]:
        """Top-level comments (not replies) are candidates for reply."""
        return [c for c in comments if not c.is_reply()]

    # ── Write methods (STUBBED — never auto-executed) ──

    def _write_preflight_blocked_in_dry_run(self) -> None:
        """Common guard: refuse writes in dry_run mode."""
        if self._config.unattended_bot_mode == "dry_run":
            raise TransportError(
                "Write blocked in dry_run mode. "
                "Set UNATTENDED_BOT_MODE=live and ensure explicit approval."
            )

    def publish_text_status(self, message: str) -> str:
        """Publish a text status to the Page.

        **STUBBED. NEVER auto-executed.** Only callable through:
        model decision -> validation -> safety -> rate-limit -> approval ->
        identity preflight -> executor.

        The deterministic implementation clicks the post composer, types the
        message, and clicks publish — but this is only invoked when an
        approved ProposedAction reaches the executor step, in live mode,
        after positive identity verification.
        """
        self._write_preflight_blocked_in_dry_run()

        # Mandatory fail-closed identity verification
        self._verify_write_preflight()

        # Actual UI automation would go here (fill composer, click post).
        # NOT implemented during development phase.
        raise NotImplementedError(
            "publish_text_status UI automation is prepared but not yet wired. "
            "Only Graph API transport can execute writes in live mode currently."
        )

    def reply_to_comment(self, target_comment_id: str, message: str) -> str:
        """Reply to a Facebook Page comment.

        **STUBBED. NEVER auto-executed.** Only callable through:
        model decision -> validation -> safety -> rate-limit -> approval ->
        identity preflight -> executor.
        """
        self._write_preflight_blocked_in_dry_run()

        # Mandatory fail-closed identity verification
        self._verify_write_preflight()

        raise NotImplementedError(
            "reply_to_comment UI automation is prepared but not yet wired. "
            "Only Graph API transport can execute writes in live mode currently."
        )
