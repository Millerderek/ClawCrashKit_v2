# ClaudeClaw

**Telegram → Claude Agent SDK Bridge**

A single-file bot (~890 LOC) that turns Telegram messages into Claude Code CLI work. Instead of building a new agent platform, ClaudeClaw gives you **mobile remote-control for your existing Claude Code workspace** — same CLAUDE.md, same skills, same MCP servers, same local files.

Uses **Claude Code OAuth** (Pro/Max subscription) by default. No API key required.

Each user gets a **persistent `ClaudeSDKClient` session** — Claude remembers the full conversation across messages, just like chatting in a terminal.

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────────────┐
│  Telegram    │     │   ClaudeClaw     │     │  Your Workstation       │
│  (mobile)    │────▶│   Bridge Bot     │────▶│                         │
│              │◀────│                  │◀────│  ├── CLAUDE.md          │
└─────────────┘     │  SessionManager  │     │  ├── Skills              │
                    │  per-user clients │     │  ├── MCP Servers         │
                    │  with full        │     │  ├── Local Files         │
                    │  conversation     │     │  └── Permissions/Hooks   │
                    │  history          │     └─────────────────────────┘
                    └──────────────────┘
```

## Why?

If you already live inside Claude Code, you're duplicating effort by building:
- A skills engine → **Claude Code already has one**
- A memory system → **CLAUDE.md already does this**
- Integrations → **MCP servers already handle this**
- Orchestration → **The Agent SDK loop already does this**

ClaudeClaw just bridges mobile access to that environment. Improvements apply everywhere.

## Quick Start

### 1. Prerequisites

- Python 3.10+
- Node.js 18+ (for Claude Code CLI)
- A Telegram account

### 2. Create Your Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the bot token

### 3. Get Your Telegram User ID

Message [@userinfobot](https://t.me/userinfobot) — it replies with your numeric ID.

### 4. Authenticate Claude Code

```bash
# Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Login with your Pro/Max subscription
claude
# Follow the prompts to authenticate via browser
```

This creates an OAuth session that the SDK uses automatically. **No API key needed.**

### 5. Install & Run

```bash
git clone https://github.com/youruser/claudeclaw.git
cd claudeclaw
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env: set TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS

# Launch
python3 claudeclaw.py
```

### 6. Message Your Bot

Open Telegram, find your bot, send any message.

## Authentication

ClaudeClaw supports two auth methods:

| Method | Setup | Cost Model |
|--------|-------|------------|
| **Claude Code OAuth** (recommended) | Run `claude` and login once | Uses your Pro/Max subscription |
| API Key | Set `ANTHROPIC_API_KEY` in `.env` | Pay-per-token via API |

The SDK auto-detects which is available. OAuth is checked first.

### OAuth Session Persistence

The `claude` login creates a session in `~/.claude/`. As long as that directory exists and your subscription is active, the bot will authenticate automatically — even after restarts.

## Configuration

All config is via environment variables (or `.env` file):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Main bot token from @BotFather |
| `ALLOWED_USER_IDS` | ✅ | — | Comma-separated Telegram user IDs |
| `ANTHROPIC_API_KEY` | — | — | Fallback if no OAuth session |
| `PERMISSION_BOT_TOKEN` | — | — | Permission bot token (setup wizard on first run) |
| `CLAUDECLAW_WORKING_DIR` | — | `~` | Workspace path |
| `CLAUDECLAW_MODEL` | — | SDK default | Model override |
| `CLAUDECLAW_PERMISSION_MODE` | — | `default` | `default` or `acceptEdits` |
| `CLAUDECLAW_ALLOWED_TOOLS` | — | SDK defaults | `Read,Write,Edit,Bash,Glob,Grep,WebSearch,WebFetch` |
| `CLAUDECLAW_SYSTEM_PROMPT` | — | — | System prompt prepend |
| `CLAUDECLAW_MAX_TURNS` | — | `0` (unlimited) | Max agent loop turns |
| `CLAUDECLAW_USE_PROJECT_SETTINGS` | — | `true` | Load CLAUDE.md / .claude/settings.json |
| `CLAUDECLAW_REQUIRE_PERMISSION` | — | `true` | Per-task approval gate |
| `CLAUDECLAW_AUTO_ALLOW_BASH` | — | memory tools | Comma-separated Bash prefixes that skip approval |
| `OPENCLAW_PATH` | — | — | OpenClaw memory files directory |
| `CRASHCART_PATH` | — | — | ClawCrashCart backup directory |
| `CLAUDECLAW_INCLUDE_DAILY_LOG` | — | `true` | Include today's daily log |

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and config |
| `/status` | Bot status, auth method, memory detection |
| `/new` | Reset conversation and all approvals |
| `/memory` | Inspect loaded OpenClaw memory files |
| `/workspace` | List files in workspace directory |
| `/whoami` | Your Telegram user ID and auth status |

## OpenClaw Memory Integration

ClaudeClaw can load [OpenClaw](https://github.com/openclawai/openclaw) memory files as system prompt context:

| File | Role |
|------|------|
| `soul.md` | Ethics & constraints |
| `identity.md` | Persona & vibe |
| `USER.md` | User context & preferences |
| `MEMORY.md` | Long-term curated facts |
| `TOOLS.md` | Capability map |
| `HEARTBEAT.md` | Autonomous task schedule |
| `AGENTS.md` | Multi-agent hierarchy |
| `memory/YYYY-MM-DD.md` | Today's daily log |

Set `OPENCLAW_PATH` to your OpenClaw directory, or `CRASHCART_PATH` to your [ClawCrashCart](https://github.com/youruser/clawcrashcart) backup. Files are searched in order: OpenClaw → CrashCart → workspace.

## Docker

```bash
cp .env.example .env
# Edit .env

docker compose up -d
```

The compose file mounts `~/.claude` read-only so the container can use your OAuth session.

## Security

**You are giving chat-driven access to a machine that can read files and optionally run commands.** Be careful.

### Dual-Bot Permission System

ClaudeClaw uses two Telegram bots:

| Bot | Role |
|-----|------|
| **Main bot** | Your conversation with Claude — messages, responses, file uploads |
| **Permission bot** | Receives task approval requests with Approve/Deny buttons |

On first run, ClaudeClaw walks you through creating the permission bot. This keeps approval prompts out of your main conversation.

### Per-Task Approval

When `CLAUDECLAW_REQUIRE_PERMISSION=true` (default), each message you send triggers **one approval request** before Claude can use write/execute tools. You tap once, and all tools needed to complete that task run without further prompts.

```
🔐 Task Permission

Message:
Fix the nginx config and restart the service

First action: cat /etc/nginx/nginx.conf

[✅ Approve Task]  [❌ Deny]
```

One tap. Claude then runs Read → Edit → Bash → Bash — all auto-approved for that task. Your next message triggers a fresh approval.

**Read-only tools** (Read, Glob, Grep, WebSearch, WebFetch) are always auto-approved — no prompt needed.

**Memory tools** (`openclaw-memo`, `openclaw-custodian`, `clawcrashcart`, `docker`) are also auto-approved. Configurable via `CLAUDECLAW_AUTO_ALLOW_BASH`.

### Other Protections

- **User allowlist**: Only IDs in `ALLOWED_USER_IDS` can interact
- **Workspace scoping**: Agent operates within `CLAUDECLAW_WORKING_DIR`
- **SDK permission mode**: `acceptEdits` auto-accepts file writes at the SDK level (since we gate via Telegram)
- **Telegram is NOT e2e encrypted**: Don't send secrets through the bot

Recommendations:
1. Keep the permission gate enabled (default)
2. Use `CLAUDECLAW_MAX_TURNS` to prevent runaway loops
3. Run in Docker for filesystem isolation

## How It Works

1. **Telegram polling** — No inbound ports needed
2. **Auth check** — User ID verified against allowlist
3. **Session create** — `ClaudeSDKClient` connects and maintains conversation history per user
4. **Memory load** — OpenClaw files assembled into system prompt
5. **SDK query** — `client.query()` sends message within existing session (Claude remembers everything)
6. **Workspace** — CLI loads CLAUDE.md, MCP servers, skills
7. **Agent loop** — Claude reads files, runs tools as needed
8. **Response** — Text blocks collected and sent to Telegram (chunked at 4096 chars)
9. **Session persistence** — Same client reused for follow-up messages; `/new` resets

## Memory Stack (ClawCrashKit v2)

ClaudeClaw ships with a bundled persistent memory system in the `memory/` directory. This is optional but gives the agent long-term memory across sessions via Qdrant vectors, PostgreSQL metadata, and Redis caching.

```
┌─────────────────────────────────────────────────────┐
│  Pinned Tier    │ MEMORY.md — refreshed every 5 min │
├─────────────────┼───────────────────────────────────┤
│  Vector Tier    │ Qdrant + Mem0 — semantic search    │
├─────────────────┼───────────────────────────────────┤
│  Metadata Tier  │ PostgreSQL — keyscores, entities   │
├─────────────────┼───────────────────────────────────┤
│  Cache Tier     │ Redis — entity→memory mappings     │
└─────────────────┴───────────────────────────────────┘
```

### Quick Start

```bash
# Start the memory stack (Qdrant, PostgreSQL, Redis)
cd memory && bash setup.sh

# Or manually with Docker Compose
docker compose --profile memory up -d
```

### Bundled Tools

| Tool | Purpose |
|------|---------|
| `openclaw-memo` | Mem0 CLI — add, search, list, delete, ingest memories |
| `openclaw-custodian` | Background maintenance — keyscores, entities, GC, contradictions |
| `clawcrashcart` | Docker-aware backup & restore for the full memory stack |
| `openclaw-session-ingest` | Auto-ingest session transcripts into Mem0 |

All of these are auto-approved in the permission gate — the agent can use them without OTP prompts. See `memory/README.md` for full documentation.

## Based On

Inspired by the "ClaudeClaw" pattern from Mark Kashef's "I Replaced OpenClaw With Claude Code in One Day" — the idea that you can skip building a custom agent platform by bridging mobile access to your existing Claude Code environment.

## License

MIT
