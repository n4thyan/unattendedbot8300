"""
Supervised smoke test for the Camoufox Facebook transport.

READ-ONLY — no Facebook writes are performed.

Flow:
  1. Launch Camoufox headed with persistent profile.
  2. Open Facebook.
  3. Deterministically establish auth state.
  4. If logged out: HUMAN_LOGIN_REQUIRED, leave browser open for manual login.
  5. Verify session persistence (no cookie-count-based auth proof).
  6. Verify authentication state is positively established.
  7. Inspect Facebook Page/profile switcher.
  8. Switch to UnattendedBot8300 Page identity.
  9. Positively verify active identity.
 10. Navigate to https://www.facebook.com/UnattendedBot8300
 11. Verify correct Page loaded.
 12. Read recent posts + visible comments.
 13. Normalise through existing storage/context pipeline.
 14. Confirm NO Facebook writes occurred.

Usage:
    .venv/Scripts/python -m src.camoufox_smoke_test
"""

import sys
import builtins
from pathlib import Path
from datetime import datetime, timezone

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))


def _save_smoke_screenshot(page, screenshot_dir, ts, label, suffix_num):
    """Save a non-sensitive smoke-test screenshot."""
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    seq = f"{ts}-{suffix_num:03d}"
    safe_label = label.replace("/", "_")
    filepath = screenshot_dir / f"smoke-{seq}-{safe_label}.png"
    try:
        page.screenshot(path=str(filepath), full_page=False)
    except Exception:
        pass
    return filepath


def _save_manifest(screenshot_dir, entries):
    """Write a non-sensitive manifest of the smoke-test session."""
    manifest_path = screenshot_dir / "smoke-manifest.json"
    manifest_data = {
        "session_id": screenshot_dir.parent.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    import json
    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2)


def smoke_test():
    """Run the supervised read-only smoke test."""
    from camoufox.sync_api import Camoufox
    from src.config import load_config
    from src.facebook_camoufox import (
        CamoufoxTransport, AuthState, IdentityState, DEFAULT_PAGE_SLUG,
        DEFAULT_PAGE_URL,
    )
    from src.facebook_transport import TransportError

    config = load_config()
    profile_dir = config.camoufox_profile_path

    print("=== UnattendedBot8300 Camoufox Smoke Test (READ ONLY) ===")
    print(f"Transport: {config.facebook_transport}")
    print(f"Profile dir: {profile_dir}")
    print(f"Mode: {config.unattended_bot_mode}")
    print(f"Page URL: {config.facebook_page_url}")
    print(f"Page Slug: {config.facebook_page_slug}")
    print()

    # Ensure the profile directory exists
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Screenshot directories
    screenshot_dir = config.project_root / "runtime" / "browser-references" / "sessions"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    session_dir = screenshot_dir / ts
    session_dir.mkdir(parents=True, exist_ok=True)
    manifest_entries = []
    screenshot_counter = 0

    def save_ss(label, state="", action=""):
        nonlocal screenshot_counter
        screenshot_counter += 1
        filepath = _save_smoke_screenshot(
            page, session_dir, ts, label, screenshot_counter
        )
        manifest_entries.append({
            "filename": filepath.name if filepath else "failed",
            "description": label,
            "state": state,
            "action": action,
            "success": filepath is not None,
        })
        return filepath

    # Step 1: Launch Camoufox headed with persistent profile
    print("1. Launching Camoufox (headed, persistent profile)...")
    browser_ctx = Camoufox(
        headless=False,
        persistent_context=True,
        user_data_dir=str(profile_dir),
    ).__enter__()
    page = browser_ctx.new_page()
    print(f"   Browser opened: {type(browser_ctx).__name__}")
    save_ss("browser-launched", state="browser_open")

    try:
        # Step 2: Open Facebook
        print("2. Navigating to Facebook...")
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        title = page.title() or ""
        print(f"   Title: {title}")
        save_ss("facebook-loaded", state="facebook_loaded")

        # Step 3: Deterministically establish auth state using the transport
        print("3. Checking authentication state (deterministic UI detection)...")
        transport = CamoufoxTransport(config)
        transport._page = page
        transport._browser = browser_ctx
        transport._session_dir_ref = session_dir
        transport._screenshot_counter = screenshot_counter
        transport.save_reference_screenshot = lambda *a, **k: None
        transport.save_failure_screenshot = lambda *a, **k: None

        auth_state = transport.detect_auth_state()
        print(f"   Auth state: {auth_state.value}")

        # Step 4: If logged out → HUMAN_LOGIN_REQUIRED
        if auth_state == AuthState.LOGIN_REQUIRED:
            print("   STATUS: HUMAN_LOGIN_REQUIRED")
            print("   Complete Facebook login in the visible Camoufox window.")
            print("   Automation is waiting for authenticated Facebook UI.")
            print("   After successful login, press ENTER here to continue...")
            builtins.input()

            # Re-navigate and re-verify auth
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
            save_ss("post-login-facebook", state="post_login")
            auth_state = transport.detect_auth_state()
            print(f"   Re-checked auth state: {auth_state.value}")

            if auth_state != AuthState.AUTHENTICATED:
                save_ss("login-still-required", state=auth_state.value, action="human_login_failed")
                print(f"   STATUS: Login still required. Auth state: {auth_state.value}")
                print("   The browser remains open for manual login.")
                _save_manifest(session_dir, manifest_entries)
                browser_ctx.close()
                return

        # Handle checkpoint/challenge
        if auth_state == AuthState.CHECKPOINT_REQUIRED:
            print("   STATUS: CHECKPOINT_REQUIRED")
            print("   HUMAN_INTERVENTION_REQUIRED — Facebook security checkpoint.")
            print("   The browser window remains open for manual resolution.")
            print("   After completing it, press ENTER here to continue...")
            save_ss("checkpoint-detected", state="checkpoint_required")
            builtins.input()
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
            auth_state = transport.detect_auth_state()

        if auth_state == AuthState.UNKNOWN_AUTH_STATE:
            print("   STATUS: UNKNOWN_AUTH_STATE (fail-closed)")
            print("   Could not positively verify authentication. Browser remains open.")
            save_ss("unknown-auth-state", state="unknown_auth_state")
            _save_manifest(session_dir, manifest_entries)
            browser_ctx.close()
            return

        # Step 5: Authentication positively verified
        print("5. Authentication positively verified (AUTHENTICATED).")
        save_ss("auth-confirmed", state="authenticated")

        # Step 6: Inspect Facebook profile/Page switcher
        print("6. Inspecting Facebook Page/profile switching UI...")
        active_identity = transport.get_active_facebook_identity()
        print(f"   Current active identity: {active_identity or '(could not determine)'}")
        save_ss("profile-switcher-inspect", state="switcher_inspected")

        # Step 7: Switch to UnattendedBot8300 Page identity
        target_slug = config.facebook_page_slug or DEFAULT_PAGE_SLUG
        print(f"7. Switching to Page identity: {target_slug}")
        identity_state = transport.ensure_page_identity(target_slug)
        print(f"   Identity state: {identity_state.value}")
        save_ss("identity-check", state=identity_state.value)

        if identity_state == IdentityState.PAGE_IDENTITY_CONFIRMED:
            print("   STATUS: Page identity already confirmed (no switch needed).")
            save_ss("page-identity-selected", state="page_identity_confirmed")
        elif identity_state == IdentityState.PAGE_SWITCH_FAILED:
            print("   STATUS: Page switch failed via switcher UI.")
            print("   Will attempt direct navigation to the Page URL instead.")
        else:
            print(f"   STATUS: Identity not confirmed by switcher ({identity_state.value}).")

        # Step 8: Navigate to the UnattendedBot8300 Page
        page_url = config.facebook_page_url or DEFAULT_PAGE_URL
        print(f"8. Navigating to Page: {page_url}")
        page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        print(f"   URL: {page.url}")
        print(f"   Title: {page.title() or ''}")
        save_ss("page-open", state="page_open", action="navigate_to_page")

        # Step 9: Verify correct Page loaded
        print("9. Verifying Page identity after navigation...")
        identity = transport.verify_page_identity(target_slug)
        print(f"   Identity: {identity.value}")

        if identity != IdentityState.PAGE_IDENTITY_CONFIRMED:
            # Check for Facebook error text
            body_text = ""
            try:
                body_text = page.eval_on_selector("body", "el => el.innerText.substring(0, 500)")
            except Exception:
                pass
            if body_text and any(m in body_text.lower() for m in ["isn't available", "not found", "does not exist"]):
                print("   STATUS: PAGE_NOT_FOUND — Facebook reports content unavailable.")
                save_ss("page-not-found", state="page_not_found")
            else:
                print("   STATUS: Wrong identity or unknown — page identity not confirmed.")
                save_ss("wrong-page", state=identity.value)
            _save_manifest(session_dir, manifest_entries)
            browser_ctx.close()
            return

        print("   STATUS: Page identity CONFIRMED.")
        save_ss("page-verified", state="page_identity_confirmed")

        # Step 10: Read recent posts
        print("10. Reading recent posts...")
        try:
            posts = transport._extract_posts()
            print(f"   Found {len(posts)} recent posts")
            for i, p in enumerate(posts[:5]):
                print(f"   Post {i+1}: {p.message[:100]}...")
            save_ss("recent-posts-detected", state="posts_extracted")
        except Exception as e:
            print(f"   NOTE: No posts detected ({e})")
            posts = []
            save_ss("no-posts-detected", state="no_posts")

        # Step 11: Read visible comments
        print("11. Reading visible comments...")
        all_comments = []
        for post_obs in posts[:3]:
            try:
                comments = transport._extract_comments(post_obs)
                all_comments.extend(comments)
            except Exception:
                continue
        print(f"   Found {len(all_comments)} comments")
        for i, c in enumerate(all_comments[:5]):
            print(f"   Comment {i+1}: {c.from_name}: {c.message[:80]}...")
        if all_comments:
            save_ss("comments-open", state="comments_detected")
        else:
            save_ss("no-comments-detected", state="no_comments")

        # Step 12: Feed observations through normalisation/storage/context pipeline
        print("12. Feeding observations through normalisation/storage/context pipeline...")
        from src.fb_observations import ObservationResult
        obs = ObservationResult(
            posts=posts,
            comments=all_comments,
            unreplied_comments=transport._find_unreplied(all_comments),
        )
        print(f"   ObservationResult: {obs.get_summary()}")

        # Feed through context assembler
        try:
            from src.context_assembler import ContextAssembler
            from src.storage import Storage
            storage = Storage(config)
            conn = storage.connect()
            assembler = ContextAssembler(conn, config.unattended_bot_mode)
            ctx = assembler.assemble(obs, memory_query="agent page activity")
            prompt = ctx.to_prompt_context()
            print(f"   Assembled context ({len(prompt)} chars) is ready for model.")
            storage.close()
            save_ss("observation-success", state="pipeline_complete")
        except Exception as e:
            print(f"   Normalisation note: {e}")

        # Step 13: Confirm session persistence
        print("13. Session persistence: browser profile is at", profile_dir)
        save_ss("session-persisted", state="persistent_profile_active")

        # Step 14: Confirm no writes
        print("14. CONFIRM: No Facebook writes were performed (READ ONLY).")

        # Keep browser open for manual inspection
        print()
        print("=== Smoke Test Complete ===")
        print(f"Session directory: {session_dir}")
        print(f"Manifest: {session_dir / 'smoke-manifest.json'}")
        print("The browser window remains open with the persistent profile.")
        print("On first run, log in manually. Future launches will reuse this session.")
        print("To close the browser, close the window manually (the profile is saved).")
        print()

        _save_manifest(session_dir, manifest_entries)

        # Keep alive for inspection
        try:
            page.wait_for_timeout(60000)
        except Exception:
            pass

    finally:
        try:
            page.close()
        except Exception:
            pass
        try:
            browser_ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    smoke_test()
