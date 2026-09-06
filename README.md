# UnattendedBot8300 — Autonomous AI Facebook Page Operator

This is the project root for the UnattendedBot8300 agent system.

## Project Structure

```
unattendedbot8300/
├── pyproject.toml           # Python project config
├── README.md                # This file
├── SKILL.md                 # Hermes skill: agent identity + slash commands
├── .env.example             # Environment variable template
├── .gitignore               # Standard Python + db entries
├── src/
│   ├── __init__.py          # Package entry, exports
│   ├── config.py            # .env loader + validation + transport config
│   ├── storage.py           # SQLite single-source-of-truth
│   ├── fb_client.py         # Graph API v26.0 wrapper (dormant)
│   ├── facebook_transport.py# Transport abstraction + factory
│   ├── facebook_camoufox.py # Camoufox UI transport (active)
│   ├── memory.py            # Memory storage/retrieval
│   ├── safety.py            # Rate limits, spam detection, dupe check
│   ├── proposed_actions.py  # Approval queue CRUD
│   ├── wake_cycles.py       # Wake cycle logging
│   ├── context_assembler.py # Bounded context for model
│   ├── wake.py              # Wake orchestrator (transport-agnostic)
│   ├── cli.py               # CLI entry: wake, status, approve, reject
│   └── camoufox_smoke_test.py # Supervised read-only browser test
├── data/                     # Gitignored runtime data
│   ├── bot.db                # SQLite database
│   └── browser-profile/      # Camoufox persistent browser profile (gitignored)
├── runtime/                  # Gitignored screenshots/logs
│   └── browser-references/   # Reference screenshots + manifests (gitignored)
├── tests/
│   ├── test_storage.py      # Phase 0 tests
│   ├── test_phase1.py       # Phase 1 tests
│   └── test_facebook_transport.py  # Transport abstraction tests
└── .hermes/                  # Project-local Hermes config (gitignored)
    └── config.yaml           # Hermes profile config for this project
```

## Current Status: Phase 0 Complete ✓

Phase 0 is complete. The system is ready for testing with real Facebook credentials.

## Setup

1. Copy `.env.example` to `.env` and fill in your Facebook credentials:

   ```bash
   cp .env.example .env
   ```

2. Create a virtual environment and install dependencies:

   ```bash
   cd unattendedbot8300
   uv venv
   .venv/Scripts/pip install -e . --group test
   ```

## Running the Agent

### Manual Wake Cycle (Dry-Run Mode)

```bash
python -m src.cli wake
```

Output example:
```
=== Wake Cycle Started ===
Mode: dry_run (Phase 0)

1. Observing Facebook state...
   Page: UnattendedBot8300 (test_page_123)
   Found 1 recent posts
   Post test_page_123_456: 1 comments

2. Retrieving memory context...
   Found 1 lore memories

3. Reasoning about actions...
   Decision: NOTHING
   Reason: No activity requiring response

4. In Phase 0 (dry-run mode), no actions are executed.
=== Wake Cycle Complete ===
```

### View Agent Status

```bash
python -m src.cli status
```

### Manage Proposed Actions

```bash
# List all proposed actions
python -m src.cli actions list

# List only pending (not yet approved)
python -m src.cli actions list --pending

# Approve an action
python -m src.cli actions approve 1

# Reject an action
python -m src.cli actions reject 1

# Execute approved action (only works when UNATTENDED_BOT_MODE=live)
python -m src.cli execute 1
```

### Run Tests

```bash
pytest tests/ -v
```

## Facebook Transport Abstraction

The agent supports pluggable Facebook transports, selected via the
`FACEBOOK_TRANSPORT` environment variable in `.env`.

### Supported Transports

| Transport   | Type        | Status     | Description                                    |
|-------------|-------------|------------|------------------------------------------------|
| `camoufox_ui` | Browser UI  | **Active** | Camoufox experimental browser UI transport     |
| `graph_api`   | Official API| Dormant    | Official Meta Graph API (v26.0) — future      |

### Architecture

```
Hermes/model
    ↓
POST / REPLY / MEMORY / NOTHING  (DecisionModel)
    ↓
existing validation
    ↓
existing safety + rate limits
    ↓
existing approval queue
    ↓
Facebook transport (selected by FACEBOOK_TRANSPORT)
    ├── camoufox_ui   ← ACTIVE (experimental)
    └── graph_api     ← DORMANT (future official Meta integration)
```

The transport abstraction (`src/facebook_transport.py`) defines a clean
`FacebookTransport` interface. The wake orchestrator (`src/wake.py`) is
transport-agnostic — it calls `get_transport(config)` and uses the returned
object's `observe()`, `publish_text_status()`, and `reply_to_comment()` methods.

### Camoufox UI Transport (Active, Experimental)

Meta Developer access is currently blocked by an account/device trust issue.
For the foreseeable development phase, the Camoufox browser is used as an
EXPERIMENTAL Facebook UI transport.

**Camoufox** is a Firefox fork (bundled with anti-detection properties) driven
via the `camoufox` Python package (v0.5.6), which wraps Playwright.

#### Installation

Camoufox is installed in the project's virtual environment:

```bash
cd unattendedbot8300
source .venv/bin/activate  # or .venv/Scripts/activate on Windows
# camoufox is already a dependency: pip install -e ".[camoufox]"
```

The Camoufox browser binary is auto-downloaded on first use to:
`C:\Users\pc\AppData\Local\camoufox\camoufox\Cache\browsers\`

#### Persistent Browser Profile

A dedicated Camoufox browser profile is used for the persistent authenticated
Facebook session:

```
data/browser-profile/
```

This directory is **gitignored** — no authentication/session material enters Git.

**First launch:**
1. The browser opens in headed/visible mode.
2. Navigate to Facebook.
3. If login is required, the browser is left visible for **manual** login.
4. The user completes any 2FA/checkpoint/security prompts manually.
5. The resulting session is persisted in the dedicated profile directory.

**Subsequent launches:**
1. Camoufox reopens the same persistent profile.
2. If the session is still valid, Facebook is already logged in.
3. No credentials are requested again.

**Important:** The user's Facebook username/password is NEVER stored in
project config, source code, environment variables, or automation scripts.
The user's existing Chrome session is never reused — Camoufox uses its own
isolated profile.

**Authentication detection is deterministic:** The transport does NOT assume
the browser is authenticated merely because cookies exist. It inspects the
rendered Facebook UI for positive authenticated evidence (profile menu,
home link, feed composer, Watch/Reels links). If a login form (email input +
password input + login button) is visible, the state is `LOGIN_REQUIRED`
regardless of cookie count. This fixes a real bug where a cookie's presence
was incorrectly treated as proof of authentication.

#### Manual Login Flow

```bash
# Run the supervised smoke test — opens headed browser
python -m src.camoufox_smoke_test
```

If Facebook shows a login form, the browser stays open. Log in manually,
complete any 2FA, then the session persists for future launches.

#### Read-Only Observation

The Camoufox transport provides read-only observation:
- Opens Facebook
- Deterministically establishes authentication state via UI detection
  (NOT via cookie count)
- If logged out, leaves the browser open for manual login (`HUMAN_LOGIN_REQUIRED`)
- Verifies authenticated state (positive UI evidence required)
- Inspects the Facebook Page/profile switcher
- Switches to the UnattendedBot8300 Page identity (fail-closed)
- Positively verifies active identity is the Page, not the personal profile
- Navigates to the UnattendedBot8300 Page URL (`FACEBOOK_PAGE_URL`)
- Identifies the correct Page (via ARIA headings / accessible names)
- Reads recent posts (`div[role='article']` → PostObservation)
- Reads visible comments (→ CommentObservation)
- Normalises into the existing `ObservationResult` format
- Feeds through the existing storage → context assembler → model pipeline

Screenshots and debug artifacts are saved to:
```
runtime/browser-references/sessions/<timestamp>/
```
Each session has a `manifest.json` with non-sensitive metadata. This directory
is **gitignored** and never committed.

#### Write Capabilities (Stubbed — Not Active)

`publish_text_status()` and `reply_to_comment()` are implemented as deterministic
functions but are **stubbed** during this phase. They:
- Refuse execution in `dry_run` mode
- Raise `NotImplementedError` in `live` mode (UI automation not yet wired)
- Are NEVER called automatically — only through the approval queue

**Fail-closed identity verification:** Before any future Facebook write, the
transport's mandatory preflight verifies:
1. Authentication state is positively `AUTHENTICATED` (via UI evidence, not cookies)
2. Active identity == UnattendedBot8300 Page (via `verify_page_identity()`)

If either check fails, the write is blocked with a `TransportError`. The bot
will NEVER accidentally publish to the personal account.

The flow for future writes:
```
Model decision → DecisionModel validation → SafetyPolicy → RateLimit → ProposedAction → Approval → transport executor
```

#### Selector Strategy

Facebook changes generated CSS class names frequently. The transport prefers:
- ARIA roles (`role='article'`, `role='heading'`, `role='button'`)
- Accessible names and labels
- Semantic text content
- `data-testid` attributes (where stable)
- Stable element relationships

All selectors are centralized in the `SELECTORS` dict in `src/facebook_camoufox.py`.
Explicit waits (`wait_for_selector`, `wait_for_load_state`) with timeouts are used
instead of fixed `sleep()` calls.

Selectors are calibrated for four categories:
- **Auth state** — login form (email+password+button) vs authenticated UI (profile menu, home link, composer)
- **Page identity** — h1 heading, URL path, profile link href, active identity label
- **Posts** — `role='article'` containers, post message text, permalinks, timestamps
- **Comments** — comment body text, commenter display names

Dormant 2captcha integration is available for CAPTCHA challenges (see
`TWOCAPTCHA_API_KEY` in `.env.example`).

#### Error Handling

Camoufox failures are classified into specific error types:
- `NotLoggedInError` — Facebook requires manual login (browser left open)
- `PageNotFoundError` — Page URL incorrect or page removed
- `SelectorError` — UI element selector didn't match (layout changed)
- `CheckpointError` — Facebook security checkpoint (needs manual resolution)
- `NavigationError` — URL navigation failed or timed out
- `TransportError` — Generic transport failure; also used to block writes when
  authentication or identity verification fails (fail-closed)

Authentication state is deterministically detected as one of:
- `AUTHENTICATED` — positive authenticated UI evidence found
- `LOGIN_REQUIRED` — login form (email + password + button) is visible
- `CHECKPOINT_REQUIRED` — 2FA/checkpoint challenge is visible
- `UNKNOWN_AUTH_STATE` — ambiguous state; **fails closed** (treated as not authenticated)

Page identity state is verified as one of:
- `PAGE_IDENTITY_CONFIRMED` — active identity is the UnattendedBot8300 Page
- `PERSONAL_IDENTITY_ACTIVE` — personal profile identity is active
- `IDENTITY_UNKNOWN` — cannot positively confirm Page identity
- `PAGE_SWITCH_FAILED` — attempted switch to Page identity but failed

Useful failures trigger a screenshot saved to
`runtime/browser-references/failures/`. No cookies, tokens, or auth
material are ever included in logs or screenshots.

### Graph API Transport (Dormant)

The existing `FacebookClient` (`src/fb_client.py`) remains in the project for
future official Meta integration. When `FACEBOOK_TRANSPORT=graph_api`, the
transport factory wraps the existing client. This path is not exercised
during the Camoufox development phase but is structurally complete and
tested.

## Meta Developer Dashboard Setup

### 1. Create a Facebook App
- Go to https://developers.facebook.com/apps
- Click "Create App" → "Business" type
- Add "Pages API" product

### 2. Add Permissions (Standard Access only)
For **your own Page** (owner/admin), **no App Review needed**:
- `pages_show_list`
- `pages_read_engagement`
- `pages_read_user_content`
- `pages_manage_posts`
- `pages_manage_engagement`

### 3. Generate Tokens
**User Access Token** (short-lived → long-lived):
- Use Graph API Explorer or OAuth flow

**Page Access Token**:
```bash
curl -X GET "https://graph.facebook.com/v26.0/me/accounts?fields=id,name,access_token&access_token=<LONG_LIVED_USER_TOKEN>"
```

Select your Page from the response and copy its `access_token`.

### 4. Store Credentials
Add to `.env`:
```
FACEBOOK_PAGE_ID=your_numeric_page_id
FACEBOOK_PAGE_ACCESS_TOKEN=your_long_lived_page_token
FACEBOOK_GRAPH_API_VERSION=v26.0
```

For the Camoufox UI transport, the Page is identified by URL/slug, not the
numeric Graph API ID:
```
FACEBOOK_TRANSPORT=camoufox_ui
FACEBOOK_PAGE_URL=https://www.facebook.com/UnattendedBot8300
FACEBOOK_PAGE_SLUG=UnattendedBot8300
```

## Hermes Integration

### How Hermes Loads the UnattendedBot8300 Profile

1. **Profile Location**: `~/.hermes/profiles/unattendedbot8300/`
2. **Skills are auto-discovered** from `HERMES_HOME/skills/`
3. **The SKILL.md** in this project defines the agent's identity

To use as a dedicated profile:

```bash
# Create profile from this project
HERMES_HOME=$PWD/.hermes hermes profile create unattendedbot8300 \
    --description "Autonomous AI Facebook Page operator"

# Activate and run
HERMES_HOME=$PWD/.hermes hermes -p unattendedbot8300 chat
```

### Hermes Profile Config (`~/.hermes/profiles/unattendedbot8300/config.yaml`)

The installed `.hermes/config.yaml` provides:
- Model configuration (GPT-5.6-Sol fallback to Laguna models)
- Skills: auto-loaded from project's SKILL.md
- Memory: disabled (uses SQLite directly)
- Cron heartbeat: wakes every 15 minutes

## Architecture: Hermes as Runtime, Model as Cognition

```
┌─────────────────────────────────────────────────────────────────────┐
│                         UNATTENDEDBOT8300                            │
│                                                                       │
│  ┌─────────────┐    ┌──────────────┐    ┌───────────────────────┐    │
│  │   Hermes    │───▶│   Model      │───▶│  Decision (POST/REPLY │    │
│  │  Runtime    │    │  (Cognition) │    │   /MEMORY /NOTHING)   │    │
│  └─────────────┘    └──────────────┘    └───────────────────────┘    │
│         │                   │                     │                 │
│         ▼                   ▼                     ▼                 │
│  ┌──────┴─────────────────┴─────────────────────┴───────┐        │
│  │                     Python Tools                        │        │
│  │  - storage.py (SQLite)                                 │        │
│  │  - fb_client.py (Facebook Graph API v26.0)            │        │
│  │  - memory.py (Memory retrieval)                        │        │
│  │  - safety.py (Rate limits, spam detection)             │        │
│  │  - proposed_actions.py (Approval queue)                │        │
│  │  - wake_cycles.py (Observability)                      │        │
│  └─────────────────────────────────────────────────────────┘        │
│                              │                                        │
│                              ▼                                        │
│                    ┌──────────────────┐                             │
│                    │  SQLite (bot.db) │                             │
│                    └──────────────────┘                             │
└─────────────────────────────────────────────────────────────────────┘
```

### Key Design Principles

1. **Hermes is the body/runtime** — provides orchestration, tools, memory system
2. **Model provides the cognition** — configured via `hermes config`, makes decisions
3. **Python tools are deterministic** — storage, API calls, safety checks
4. **Approval is the boundary** — proposed actions await human approval in Phase 0
5. **NOTHING is a valid decision** — wake cycles are logged, proving discretion

## Safety & Mode Configuration

| Setting | Value | Meaning |
|---------|-------|---------|
| `UNATTENDED_BOT_MODE` | `dry_run` | Phase 0: proposals only, no Facebook writes |
| `UNATTENDED_BOT_MODE` | `live` | Phase 1+: approved actions execute |
| `APPROVAL_MODE` | `true` | Actions require manual approval |
| `APPROVAL_MODE` | `false` | Actions auto-execute (Phase 1+) |
| `FACEBOOK_GRAPH_API_VERSION` | `v26.0` | Configurable API version |

## Future Work (Phase 1+)

### World Observation / Curiosity Layer (Design Phase)

A future enhancement to add the `EXPLORE` action type:

- **Purpose**: Let the agent observe the wider internet as contextual input
- **NOT** mass scraping or auto-posting trending topics
- **Instead**: Internet observation as another environmental input

**Possible EXPLORE triggers:**
- Something a Facebook commenter mentioned
- Something in its memories (patterns, interests)
- Recent news/events
- Spontaneous curiosity from the agent

**Output**: Internet observations feed into reasoning, but the agent decides
whether anything is worth posting. Popularity ≠ relevance.

**Implementation**: Added to `ActionType` enum, uses Hermes's browser/web tools.

---

## Developer Notes

### Rate Limits (Phase 0)
- Max 5 posts per hour
- Max 50 replies per day  
- Min 30 minutes between actions

### Duplicate Detection
Content is hashed and checked against recent proposals to prevent spam.

### Secrets Management
All secrets (`.env`) are gitignored. `.env.example` contains placeholders only.