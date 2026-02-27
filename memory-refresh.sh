#!/bin/bash
MEMO="/root/openclaw-memory/openclaw_memo.py"
MEMORY="/root/.openclaw/MEMORY.md"
USER="your_user"

cat > "$MEMORY" << 'PINNED'
# OpenClaw — Pinned Memory

## Identity
- Agent name: OpenClaw (conversational alias: YOUR_AGENT)
- Operator: YOUR_NAME — IT engineer at YOUR_COMPANY
- Primary model: Kimi K2.5
- Escalation chain: Claude Opus → Gemini 2.0 Pro → GPT-4o

## Infrastructure
- VPS: YOUR_VPS_IP (Contabo)
- OpenClaw install: /root/.openclaw
- Credentials: ClawVault (/root/APIKeys)

## Memory System
- Qdrant: 127.0.0.1:6333 (Docker — vector store for semantic memory)
- PostgreSQL: 127.0.0.1:5432 (Docker — keyscores, entity tags, contradictions)
- Redis: 127.0.0.1:6379 (Docker — cache, entity indices, pre-filter)
- Mem0 CLI: openclaw-memo (search/add/list)
- Backup: clawcrashcart (Docker-aware backup/restore)

## To search memory run: openclaw-memo search "query" --user your_user --limit 5
## To store new facts run: openclaw-memo add "fact" --user your_user

PINNED

echo "" >> "$MEMORY"
echo "## Recent Memory Snapshot" >> "$MEMORY"
echo "Last refreshed: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$MEMORY"
echo "" >> "$MEMORY"

for topic in "YOUR_NAME work YOUR_COMPANY" "infrastructure VPS Docker" "EXAMPLE_CLIENT Teams telephony" "ClawCrashCart backup" "OpenClaw memory Qdrant"; do
    results=$(python3 "$MEMO" search "$topic" --user "$USER" --limit 3 2>&1 | grep -oP '\[[\d.]+\] .+')
    if [ -n "$results" ]; then
        echo "### $topic" >> "$MEMORY"
        while IFS= read -r line; do
            echo "- $line" >> "$MEMORY"
        done <<< "$results"
        echo "" >> "$MEMORY"
    fi
done

echo "Memory refresh complete: $(wc -c < "$MEMORY") bytes"
