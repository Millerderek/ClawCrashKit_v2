# Dual Memory Architecture

ClawCrashKit works alongside OpenClaw's built-in memory system, giving your agent both fast in-session recall and long-term memory lifecycle management.

## Built-in Memory (Fast Recall)

OpenClaw includes a built-in memory system using **sqlite-vec** for local vector search. This handles real-time in-session lookups with near-instant response times since everything runs locally (no network hop).

### Configuration

OpenClaw auto-detects your embedding provider. Any provider with a valid API key works:

| Provider | Model | Dimensions | Setup |
|----------|-------|-----------|-------|
| Gemini | gemini-embedding-001 | 3072 | `export GEMINI_API_KEY=...` |
| OpenAI | text-embedding-3-small | 1536 | `export OPENAI_API_KEY=...` |
| Voyage | voyage-3 | 1024 | `export VOYAGE_API_KEY=...` |
| Mistral | mistral-embed | 1024 | `export MISTRAL_API_KEY=...` |

OpenClaw will use whichever key it finds first (configurable via `openclaw configure`).

### How it works

- **Store**: SQLite database at `~/.openclaw/memory/main.sqlite`
- **Vector engine**: sqlite-vec (bundled native extension, no external dependencies)
- **Indexing**: Automatically indexes files in your agent's memory directory
- **Search**: Hybrid mode — vector similarity + full-text search (FTS)
- **Cache**: Embedding cache avoids re-computing vectors for repeated queries

### Check status

```bash
openclaw memory status
```

## ClawCrashKit (Long-term Memory Lifecycle)

ClawCrashKit adds the layers that the built-in system doesn't have:

| Feature | Built-in Memory | ClawCrashKit |
|---------|----------------|--------------|
| Vector search | ✅ sqlite-vec (local) | ✅ Qdrant (via Mem0) |
| Speed | ⚡ Near-instant | ~2-3s (API + network) |
| Keyscoring | ❌ | ✅ Recency, frequency, authority, entity boost |
| Entity extraction | ❌ | ✅ Auto-tags clients, technologies, projects |
| Contradiction detection | ❌ | ✅ Heuristic scanning with Telegram alerts |
| Feedback loop | ❌ | ✅ correct/incorrect adjusts authority scores |
| Garbage collection | ❌ | ✅ Stale/expired memory deprecation |
| Backup & restore | ❌ | ✅ Docker-aware snapshots with checksums |
| Memory refresh | ❌ | ✅ Auto-rebuilds MEMORY.md from vector search |
| Session ingestion | ❌ | ✅ Auto-ingests conversation transcripts |

## How They Work Together

```
User message → Agent
                ├── Built-in Memory (sqlite-vec)
                │   └── Fast in-session recall (~ms)
                │       Returns relevant chunks from indexed files
                │
                └── MEMORY.md (refreshed by ClawCrashKit)
                    └── Pre-loaded context from Mem0 + Qdrant
                        Scored by keyscores, entity-boosted, curated

Background (cron):
  Every 5 min  → memory-refresh.sh queries Mem0 → rebuilds MEMORY.md
  Every 10 min → session_ingest.py → new conversations → Mem0
  Every 30 min → custodian → keyscores, entities, GC, contradictions, Redis cache
  Daily 3 AM   → clawcrashcart → full Docker-aware backup
```

The built-in memory handles quick lookups during conversation. ClawCrashKit handles everything else: scoring memory quality, detecting contradictions, decaying stale facts, processing feedback, and ensuring nothing is lost via automated backups.

## Setup

1. **Built-in memory** works out of the box — just ensure you have an embedding API key set in your environment.

2. **ClawCrashKit** requires the Docker stack (Qdrant + PostgreSQL + Redis). See the main README for setup instructions.

Both systems are independent and can run separately, but they're most powerful together.
