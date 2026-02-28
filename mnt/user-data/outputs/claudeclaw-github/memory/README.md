# ClawCrashKit v2

A persistent, layered memory system for [OpenClaw](https://openclaw.ai) AI agents with Docker-aware backup/restore, keyscoring, entity extraction, contradiction detection, and feedback loops.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Agent Session                      │
│            (reads MEMORY.md on start)                │
├─────────────────────────────────────────────────────┤
│  Pinned Tier    │ MEMORY.md — refreshed every 5 min │
│  (deterministic)│ from Mem0 semantic search          │
├─────────────────┼───────────────────────────────────┤
│  Vector Tier    │ Qdrant + Mem0 — semantic search    │
│  (semantic)     │ with entity-boosted re-ranking     │
├─────────────────┼───────────────────────────────────┤
│  Metadata Tier  │ PostgreSQL — keyscores, entities,  │
│  (structured)   │ contradictions, feedback, jobs     │
├─────────────────┼───────────────────────────────────┤
│  Cache Tier     │ Redis — entity→memory mappings,    │
│  (fast lookup)  │ pre-computed keyscores             │
└─────────────────┴───────────────────────────────────┘
```

## Components

| Tool | Purpose |
|------|---------|
| `openclaw-memo` | Mem0 CLI — add, search, list, delete, ingest memories |
| `openclaw-custodian` | Background maintenance — keyscores, entities, GC, contradictions, Redis cache |
| `clawcrashcart` | Docker-aware backup & restore for the full memory stack |
| `entity_boost.py` | Entity-aware re-ranking for search results via Redis |
| `session_ingest.py` | Auto-ingest session transcripts into Mem0 |
| `memory-refresh.sh` | Cron script to rebuild MEMORY.md from Mem0 |
| `telegram_notify.py` | Telegram alerts for contradictions (not included — create from template) |

## Quick Start

### 1. Deploy the stack

```bash
# Start Qdrant, PostgreSQL, Redis
docker compose up -d

# Initialize PostgreSQL schema
docker exec -i openclaw-memory-postgres-1 psql -U openclaw -d openclaw_memory < init-db.sql
```

### 2. Configure credentials

```bash
cp config.example.env config.env
# Edit config.env with your API keys
```

### 3. Install CLI tools

```bash
chmod +x openclaw_memo.py openclaw_custodian.py clawcrashcart.py session_ingest.py
ln -sf $(pwd)/openclaw_memo.py /usr/local/bin/openclaw-memo
ln -sf $(pwd)/openclaw_custodian.py /usr/local/bin/openclaw-custodian
ln -sf $(pwd)/clawcrashcart.py /usr/local/bin/clawcrashcart
ln -sf $(pwd)/session_ingest.py /usr/local/bin/openclaw-session-ingest
```

### 4. Seed initial memories

```bash
openclaw-memo add "Your first memory fact here" --user your_user
openclaw-memo search "test query" --user your_user
```

### 5. Run the custodian

```bash
openclaw-custodian run    # Full maintenance cycle
openclaw-custodian stats  # View stack health
```

### 6. Set up automation (cron)

```bash
# Memory refresh every 5 minutes
*/5 * * * * /path/to/memory-refresh.sh >> /tmp/memory-refresh.log 2>&1

# Custodian every 30 minutes
*/30 * * * * /usr/local/bin/openclaw-custodian run >> /tmp/custodian.log 2>&1

# Session ingestion every 10 minutes
*/10 * * * * /usr/local/bin/openclaw-session-ingest >> /tmp/session-ingest.log 2>&1

# Daily backup at 3 AM
0 3 * * * /usr/local/bin/clawcrashcart backup --notes "daily" >> /tmp/backup.log 2>&1
```

## Keyscoring

Each memory gets a composite relevance score:

| Component | Weight | Source |
|-----------|--------|--------|
| Recency | 0.35 | Exponential decay over days since last access |
| Frequency | 0.20 | Logarithmic scaling of access count |
| Authority | 0.30 | Adjusted by user feedback (+0.15 correct, -0.25 incorrect) |
| Entity Boost | 0.15 | Contextual boost when query matches tagged entities |

## Feedback Loop

```bash
# Mark a memory as correct (boosts authority)
openclaw-custodian feedback <memory-id-prefix> correct

# Mark as incorrect (drops authority, flags for review)
openclaw-custodian feedback <memory-id-prefix> incorrect

# See what needs attention
openclaw-custodian review
```

## Backup & Restore

```bash
# Full backup (Qdrant snapshots + PostgreSQL dump + Redis RDB + configs)
clawcrashcart backup --notes "pre-migration"

# Restore specific component
clawcrashcart restore snapshot_20260226_235959 --component qdrant

# Restore everything
clawcrashcart restore snapshot_20260226_235959 --component all

# List / verify / prune
clawcrashcart list --verbose
clawcrashcart verify snapshot_20260226_235959
clawcrashcart prune --keep-last 10
```

## Telegram Notifications

Create `telegram_notify.py` with your bot token and chat ID to receive alerts when contradictions are detected:

```python
import json, urllib.request

BOT_TOKEN = "your-bot-token"
CHAT_ID = "your-chat-id"

def send(text, parse_mode="Markdown"):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False
```

## Dual Memory Architecture

ClawCrashKit works alongside OpenClaw's built-in memory (sqlite-vec). See [DUAL_MEMORY.md](DUAL_MEMORY.md) for the full architecture and how both systems complement each other.

## Requirements

- Python 3.10+
- Docker & Docker Compose
- Node.js 20+ (for OpenClaw)
- `pip install mem0ai cryptography`
