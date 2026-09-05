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
│   ├── __init__.py
│   ├── config.py            # .env loader + validation
│   ├── storage.py           # SQLite single-source-of-truth
│   ├── fb_client.py         # Graph API v26.0 wrapper
│   ├── memory.py            # Memory storage/retrieval
│   ├── safety.py            # Rate limits, dupe detection, moderation
│   ├── proposed_actions.py  # Approval queue CRUD
│   ├── wake_cycles.py       # Wake cycle logging
│   └── cli.py               # CLI entry: wake, status, approve, reject
├── data/                     # Created at runtime, gitignored
│   └── bot.db                # SQLite database
├── tests/
│   └── test_storage.py      # Tests with mocked Graph API
└── .hermes/                  # Project-local Hermes config (gitignored)
    └── config.yaml           # Hermes profile config for this project
```

## Current Status: Phase 0 Complete ✓

Phase 0 is complete. The system is ready for testing with real Facebook credentials.

```
✓ Project structure created
✓ SQLite storage layer with all required tables
✓ Facebook Graph API client (v26.0 configurable)
✓ Memory storage and retrieval
✓ Safety policy (rate limits, spam detection, dupe check)
✓ Proposed action queue with approval workflow
✓ Wake cycle logging
✓ CLI commands: wake, status, actions, execute, memory
✓ SKILL.md with UnattendedBot8300 identity
✓ Tests with mocks (20 passed)
✓ Dry-run mode verified (no live Facebook writes)
✓ .env.example with placeholders
✓ .gitignore for secrets and db
```

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