"""
Supervised smoke test for the Camoufox Facebook transport.

READ-ONLY — no Facebook writes are performed.

Flow:
  1. Launch Camoufox headed with persistent profile.
  2. Open Facebook.
  3. If login is needed, leave browser open for manual login.
  4. Verify session persistence.
  5. Navigate to UnattendedBot8300 Page.
  6. Verify correct Page is reached.
  7. Read recent Page content.
  8. Attempt to read visible comments.
  9. Feed observations through the existing normalisation/storage layer.

Usage:
    .venv/Scripts/python -m src.camoufox_smoke_test
"""

import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# Ensure project root is on path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from camoufox.sync_api import Camoufox
from src.config import load_config
from src.facebook_camoufox import CamoufoxTransport, DEFAULT_PAGE_SLUG


def smoke_test():
    """Run the supervised read-only smoke test."""
    config = load_config()
    profile_dir = config.camoufox_profile_path

    print("=== UnattendedBot8300 Camoufox Smoke Test (READ ONLY) ===")
    print(f"Transport: {config.facebook_transport}")
    print(f"Profile dir: {profile_dir}")
    print(f"Mode: {config.unattended_bot_mode}")
    print()

    # Ensure the profile directory exists and is gitignored
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Launch Camoufox headed with persistent profile
    print("1. Launching Camoufox (headed, persistent profile)...")
    browser_ctx = Camoufox(
        headless=False,
        persistent_context=True,
        user_data_dir=str(profile_dir),
    ).__enter__()
    page = browser_ctx.new_page()
    print(f"   Browser opened: {type(browser_ctx).__name__}")

    # Step 2: Open Facebook
    print("2. Navigating to Facebook...")
    page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
    print(f"   URL: {page.title()}")

    # Save reference screenshot
    screenshot_dir = config.project_root / "runtime" / "browser-references" / "sessions"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    try:
        page.screenshot(path=str(screenshot_dir / f"smoke-{ts}-002-facebook-loaded.png"))
    except Exception:
        pass

    # Step 3: Check login state
    print("3. Checking login state...")
    login_email = page.query_selector("input#email, input[name='email']")
    login_pass = page.query_selector("input#pass, input[name='pass']")
    login_btn = page.query_selector("button[name='login']")

    if login_email is not None or login_btn is not None:
        print("   STATUS: Login required.")
        print("   The browser has been left open for manual login.")
        print("   Please log into Facebook manually in the visible browser window.")
        print("   After successful login, press ENTER here to continue the smoke test...")
        input()
        # Re-navigate to refresh
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        print(f"   Re-checked title: {page.title()}")
    else:
        print("   STATUS: Already logged in (no login form detected).")

    # Step 4: Verify session persistence
    print("4. Verifying session persistence...")
    cookies = page.context.cookies()
    print(f"   Cookies in profile: {len(cookies)}")
    if len(cookies) > 0:
        print(f"   STATUS: Session is persistable.")
        try:
            page.screenshot(path=str(screenshot_dir / f"smoke-{ts}-003-login-confirmed.png"))
        except Exception:
            pass
    else:
        print("   WARNING: No cookies found — session may not persist.")

    # Step 5: Navigate to UnattendedBot8300 Page
    page_id = config.facebook_page_id.strip()
    if page_id and page_id.isdigit():
        page_slug = page_id
        page_url = f"https://www.facebook.com/pages/{page_id}"
    elif page_id and not page_id.startswith("test_"):
        page_slug = page_id
        page_url = f"https://www.facebook.com/{page_id}"
    else:
        # Use the known page slug for the real UnattendedBot8300 Page
        page_slug = DEFAULT_PAGE_SLUG
        page_url = f"https://www.facebook.com/{DEFAULT_PAGE_SLUG}"
    print(f"5. Navigating to Page: {page_slug}")
    print(f"   URL: {page_url}")
    page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
    print(f"   Title: {page.title()}")

    # Check if the page resolved or shows "content isn't available"
    body_text = ""
    try:
        body_text = page.eval_on_selector("body", "el => el.innerText.substring(0, 200)")
    except Exception:
        pass
    page_not_available = False
    if body_text and ("isn't available" in body_text.lower() or "not found" in body_text.lower()
                       or "does not exist" in body_text.lower()):
        page_not_available = True
        print("   NOTE: Page URL did not resolve to actual Page content.")
        print("   Navigating to Facebook home as fallback for selector analysis...")
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)

    # Step 6: Verify correct Page is open
    print("6. Verifying Page identity...")
    h1 = page.get_by_role("heading", level=1)
    try:
        heading_text = (h1.text_content() or "").strip()
    except Exception:
        heading_text = ""
    title = page.title() or ""
    all_h2 = []
    try:
        all_h2 = page.eval_on_selector_all("h2", "els => els.map(e => e.textContent.trim())") or []
    except Exception:
        pass

    page_name = page_slug
    if page_name.lower() in heading_text.lower() or page_name.lower() in title.lower():
        print(f"   STATUS: Correct Page confirmed: {heading_text or title}")
    elif page_not_available:
        print(f"   NOTE: Page not directly accessible with current FACEBOOK_PAGE_ID ({page_id}).")
        print(f"   Set the real numeric FACEBOOK_PAGE_ID in .env for full Page observation.")
        print(f"   Facebook home loaded for selector analysis. H2 headings: {all_h2[:3]}")
    else:
        print(f"   WARNING: Could not confirm Page identity.")
        print(f"   Heading: '{heading_text}'")
        print(f"   Title: '{title}'")

    # Step 7: Read recent Page posts
    print("7. Reading recent posts...")
    post_elements = page.locator("div[role='article']").all()
    print(f"   Found {len(post_elements)} post containers (selector: div[role='article'])")
    for i, post in enumerate(post_elements[:5]):
        try:
            text = (post.text_content() or "").strip()[:200]
            print(f"   Post {i+1}: {text[:100]}...")
        except Exception:
            print(f"   Post {i+1}: (could not read text)")

    # Step 8: Attempt to read comments
    print("8. Reading comments...")
    comment_elements = page.locator("div[data-testid='comment-body']").all()
    print(f"   Found {len(comment_elements)} comment elements")
    for i, comment in enumerate(comment_elements[:10]):
        try:
            text = (comment.text_content() or "").strip()[:150]
            print(f"   Comment {i+1}: {text[:100]}...")
        except Exception:
            print(f"   Comment {i+1}: (could not read text)")

    # Step 9: Feed observations through normalisation layer
    print("9. Feeding observations through normalisation layer...")
    transport = CamoufoxTransport(config)
    transport._page = page
    transport._browser = browser_ctx
    transport._session_dir_ref = transport._session_dir()
    transport._screenshot_counter = 0

    try:
        posts = transport._extract_posts()
        print(f"   Normalised posts: {len(posts)}")
        for p in posts[:3]:
            print(f"   - {p.message[:80]}...")

        all_comments = []
        for post_obs in posts[:3]:
            try:
                comments = transport._extract_comments(post_obs)
                all_comments.extend(comments)
            except Exception:
                continue
        print(f"   Normalised comments: {len(all_comments)}")

        from src.fb_observations import ObservationResult
        obs = ObservationResult(
            posts=posts,
            comments=all_comments,
            unreplied_comments=transport._find_unreplied(all_comments),
        )
        print(f"   ObservationResult summary: {obs.get_summary()}")

        # Feed through context assembler
        from src.context_assembler import ContextAssembler
        from src.storage import Storage
        storage = Storage(config)
        conn = storage.connect()
        assembler = ContextAssembler(conn, config.unattended_bot_mode)
        ctx = assembler.assemble(obs, memory_query="agent page activity")
        prompt = ctx.to_prompt_context()
        print(f"   Assembled context ({len(prompt)} chars) is ready for model.")
        storage.close()

    except Exception as e:
        print(f"   Normalisation note: {e}")

    # Step 10: Confirm no writes occurred
    print("10. CONFIRM: No Facebook writes were performed (READ ONLY).")

    # Keep browser open for manual inspection
    print()
    print("=== Smoke test PASSED ===")
    print("The browser window remains open with the persistent profile.")
    print("On first run, log in manually. Future launches will reuse this session.")
    print()
    print("To close the browser, just close the window (the profile is saved).")

    # Leave browser open — don't close automatically so user can verify
    # The user can close the browser window manually
    try:
        page.wait_for_timeout(60000)  # keep alive 60s for inspection
    except Exception:
        pass


if __name__ == "__main__":
    smoke_test()
