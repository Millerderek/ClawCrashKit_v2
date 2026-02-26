#!/usr/bin/env python3
"""
session_ingest — Auto-ingest OpenClaw session summaries into Mem0

Watches /root/.openclaw/memory/ for session markdown files.
Tracks which files have been ingested in a state file.
Extracts key facts from conversations and stores them as memories.

Usage:
  session_ingest.py              # Process new session files
  session_ingest.py --force      # Re-process all files
  session_ingest.py --status     # Show ingestion status
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MEMORY_DIR = Path("/root/.openclaw/memory")
STATE_FILE = Path("/root/openclaw-memory/.session_ingest_state.json")
MEMO_CMD = "/usr/local/bin/openclaw-memo"
USER = "your_user"

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"ingested": {}}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def extract_facts(content):
    """Extract meaningful facts from a session transcript."""
    facts = []
    lines = content.split("\n")

    # Get assistant responses (they contain the useful info)
    current_block = []
    in_assistant = False

    for line in lines:
        if line.startswith("assistant:"):
            in_assistant = True
            current_block = [line[len("assistant:"):].strip()]
        elif line.startswith("user:"):
            if in_assistant and current_block:
                text = " ".join(current_block)
                # Clean up formatting
                text = re.sub(r'\[\[.*?\]\]', '', text)
                text = re.sub(r'\*\*', '', text)
                text = re.sub(r'`[^`]+`', '', text)
                text = text.strip()
                if len(text) > 50:
                    facts.append(text)
            in_assistant = False
            current_block = []
        elif in_assistant:
            current_block.append(line.strip())

    # Capture last block
    if in_assistant and current_block:
        text = " ".join(current_block)
        text = re.sub(r'\[\[.*?\]\]', '', text)
        text = re.sub(r'\*\*', '', text)
        text = text.strip()
        if len(text) > 50:
            facts.append(text)

    return facts

def summarize_fact(fact):
    """Condense a long assistant response into a storable memory."""
    # If it's short enough, use as-is
    if len(fact) < 200:
        return fact

    # Extract bullet points as individual facts
    bullets = re.findall(r'[-•]\s+(.+?)(?=\n[-•]|\n\n|$)', fact)
    if bullets:
        return bullets

    # Truncate long text to first meaningful sentence
    sentences = re.split(r'[.!?]\s+', fact)
    if sentences:
        return sentences[0][:200]

    return fact[:200]

def ingest_file(filepath, state):
    """Ingest a session file into Mem0."""
    content = filepath.read_text()
    file_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    # Skip if already ingested with same content
    fname = filepath.name
    if fname in state["ingested"] and state["ingested"][fname] == file_hash:
        return 0

    facts = extract_facts(content)
    stored = 0

    for fact in facts:
        summaries = summarize_fact(fact)
        if isinstance(summaries, list):
            for s in summaries:
                if len(s.strip()) > 20:
                    store_memory(s.strip())
                    stored += 1
        elif isinstance(summaries, str) and len(summaries.strip()) > 20:
            store_memory(summaries.strip())
            stored += 1

    state["ingested"][fname] = file_hash
    return stored

def store_memory(text):
    """Store a single fact via openclaw-memo."""
    try:
        subprocess.run(
            [MEMO_CMD, "add", text, "--user", USER],
            capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        print(f"  ⚠ Failed to store: {e}")

def cmd_ingest(force=False):
    """Process new session files."""
    state = load_state() if not force else {"ingested": {}}

    if not MEMORY_DIR.exists():
        print(f"  ⚠ Memory directory not found: {MEMORY_DIR}")
        return

    files = sorted(MEMORY_DIR.glob("*.md"))
    if not files:
        print("  No session files found")
        return

    total = 0
    for f in files:
        count = ingest_file(f, state)
        if count > 0:
            print(f"  ✓ {f.name}: {count} facts ingested")
            total += count
        else:
            print(f"  · {f.name}: already ingested")

    save_state(state)
    print(f"\n  Total: {total} new facts ingested from {len(files)} files")

def cmd_status():
    """Show ingestion status."""
    state = load_state()
    files = sorted(MEMORY_DIR.glob("*.md")) if MEMORY_DIR.exists() else []

    print(f"\n  Session Ingestion Status")
    print(f"  {'=' * 40}")
    print(f"  Memory dir: {MEMORY_DIR}")
    print(f"  Files found: {len(files)}")
    print(f"  Files ingested: {len(state.get('ingested', {}))}")

    for f in files:
        status = "✓" if f.name in state.get("ingested", {}) else "·"
        print(f"  {status} {f.name}")

if __name__ == "__main__":
    if "--status" in sys.argv:
        cmd_status()
    elif "--force" in sys.argv:
        cmd_ingest(force=True)
    else:
        cmd_ingest()
