#!/usr/bin/env python3
"""
openclaw-memo — Mem0 Integration for OpenClaw

Persistent memory layer connecting:
  - Qdrant (local Docker) for vector storage
  - OpenAI text-embedding-3-small for embeddings (API)
  - Anthropic Claude Sonnet for LLM reasoning (API)
  - ClawVault for secure credential access

Usage:
  # As a library
  from openclaw_memo import get_memory
  m = get_memory()
  m.add("YOUR_NAME prefers PowerShell for automation", user_id="your_user")
  results = m.search("what scripting language does YOUR_NAME use", user_id="your_user")

  # CLI for testing
  python3 openclaw_memo.py add "YOUR_NAME works at YOUR_COMPANY" --user your_user
  python3 openclaw_memo.py search "where does YOUR_NAME work" --user your_user
  python3 openclaw_memo.py list --user your_user
  python3 openclaw_memo.py status
"""

import argparse
import json
import os
import sys
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# Vault integration — pull API keys securely at runtime
# ═══════════════════════════════════════════════════════════════════════════════

VAULT_ENV = Path("/etc/openclaw/vault.env")
VAULT_FILE = Path("/etc/openclaw/vault.enc")

def load_vault_env():
    """Load master key and other vars from vault.env."""
    if not VAULT_ENV.exists():
        sys.exit(f"Error: {VAULT_ENV} not found")
    env = {}
    for line in VAULT_ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip()
    return env

def get_vault_key(key_name: str) -> str:
    """Decrypt and retrieve a specific key from ClawVault."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        sys.exit("Error: cryptography package not installed")

    env = load_vault_env()
    master = env.get("VAULT_MASTER_KEY", "")
    if not master:
        sys.exit("Error: VAULT_MASTER_KEY not set in vault.env")

    fernet = Fernet(master.encode())
    vault_data = json.loads(fernet.decrypt(VAULT_FILE.read_bytes()))
    
    if key_name not in vault_data:
        sys.exit(f"Error: {key_name} not found in vault")
    
    return vault_data[key_name]

# ═══════════════════════════════════════════════════════════════════════════════
# Mem0 configuration and initialization
# ═══════════════════════════════════════════════════════════════════════════════

def get_memo_config() -> dict:
    """Build Mem0 config using ClawVault credentials."""
    openai_key = get_vault_key("OPENAI_API_KEY")
    anthropic_key = get_vault_key("ANTHROPIC_API_KEY")

    return {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "host": "localhost",
                "port": 6333,
                "collection_name": "openclaw_memories",
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "api_key": openai_key,
                "model": "text-embedding-3-small",
            },
        },
        "llm": {
            "provider": "anthropic",
            "config": {
                "api_key": anthropic_key,
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 2000,
            },
        },
        "version": "v1.1",
    }

def get_memory():
    """Initialize and return a Mem0 Memory instance."""
    from mem0 import Memory
    config = get_memo_config()
    return Memory.from_config(config)

# ═══════════════════════════════════════════════════════════════════════════════
# CLI commands
# ═══════════════════════════════════════════════════════════════════════════════

GREEN  = "\033[92m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def cmd_add(args):
    """Add a memory."""
    m = get_memory()
    result = m.add(args.text, user_id=args.user)
    
    if result and result.get("results"):
        for r in result["results"]:
            event = r.get("event", "unknown")
            memory = r.get("memory", "")
            print(f"  {GREEN}✓{RESET} [{event}] {memory}")
    else:
        print(f"  {GREEN}✓{RESET} Memory added")
    return 0

def cmd_search(args):
    """Search memories with entity-boosted scoring."""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from entity_boost import get_boost
    m = get_memory()
    results = m.search(args.query, user_id=args.user, limit=args.limit)
    if not results.get("results"):
        print(f"  {YELLOW}No memories found{RESET}")
        return 0
    boosted = []
    for r in results["results"]:
        memory = r.get("memory", "")
        score = r.get("score", 0)
        mem_id = r.get("id", "")[:8]
        boost = get_boost(args.query, mem_id)
        boosted.append((score + boost, score, boost, memory, mem_id))
    boosted.sort(key=lambda x: x[0], reverse=True)
    for i, (final, orig, boost, memory, mem_id) in enumerate(boosted, 1):
        btag = f" {GREEN}+{boost:.2f}\u2191{RESET}" if boost > 0 else ""
        print(f"  {CYAN}{i}.{RESET} [{final:.3f}] {memory} {DIM}({mem_id}){RESET}{btag}")
    return 0
def cmd_list(args):
    """List all memories for a user."""
    m = get_memory()
    results = m.get_all(user_id=args.user)
    
    memories = results.get("results", [])
    if not memories:
        print(f"  {YELLOW}No memories stored for user: {args.user}{RESET}")
        return 0
    
    print(f"  {BOLD}Memories for {args.user}:{RESET}\n")
    for i, r in enumerate(memories, 1):
        memory = r.get("memory", "")
        mem_id = r.get("id", "")[:8]
        created = r.get("created_at", "")[:19]
        print(f"  {CYAN}{i:3d}.{RESET} {memory}")
        print(f"       {DIM}id: {mem_id}  created: {created}{RESET}")
    
    print(f"\n  {BOLD}Total: {len(memories)} memories{RESET}")
    return 0

def cmd_delete(args):
    """Delete a specific memory by ID."""
    m = get_memory()
    m.delete(args.memory_id)
    print(f"  {GREEN}✓{RESET} Memory deleted: {args.memory_id}")
    return 0

def cmd_reset(args):
    """Delete all memories for a user."""
    if not args.yes:
        try:
            confirm = input(f"  {RED}Delete ALL memories for user '{args.user}'? [y/N]{RESET} ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n  Aborted.")
            return 1
        if confirm != "y":
            print("  Aborted.")
            return 1
    
    m = get_memory()
    m.delete_all(user_id=args.user)
    print(f"  {GREEN}✓{RESET} All memories deleted for user: {args.user}")
    return 0

def cmd_status(args):
    """Check Mem0 system status."""
    print(f"\n{CYAN}{BOLD}{'═' * 50}")
    print(f"  OpenClaw Memo — Status")
    print(f"{'═' * 50}{RESET}\n")

    # Check vault access
    try:
        load_vault_env()
        print(f"  {GREEN}✓{RESET} ClawVault accessible")
    except Exception as e:
        print(f"  {RED}✗{RESET} ClawVault: {e}")
        return 1

    # Check API keys
    for key_name in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"]:
        try:
            val = get_vault_key(key_name)
            masked = val[:8] + "..." + val[-4:]
            print(f"  {GREEN}✓{RESET} {key_name}: {masked}")
        except Exception as e:
            print(f"  {RED}✗{RESET} {key_name}: {e}")

    # Check Qdrant
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient("localhost", port=6333)
        collections = client.get_collections().collections
        coll_names = [c.name for c in collections]
        print(f"  {GREEN}✓{RESET} Qdrant: {len(collections)} collection(s) {coll_names}")
    except Exception as e:
        print(f"  {RED}✗{RESET} Qdrant: {e}")

    # Check Mem0 initialization
    try:
        m = get_memory()
        print(f"  {GREEN}✓{RESET} Mem0 initialized")
    except Exception as e:
        print(f"  {RED}✗{RESET} Mem0: {e}")

    print()
    return 0

def cmd_ingest(args):
    """Ingest a markdown file as memories (one fact per line/section)."""
    m = get_memory()
    filepath = Path(args.file)
    
    if not filepath.exists():
        print(f"  {RED}✗{RESET} File not found: {filepath}")
        return 1

    content = filepath.read_text().strip()
    if not content:
        print(f"  {YELLOW}File is empty{RESET}")
        return 0

    # Split into meaningful chunks — by headers or double newlines
    chunks = []
    current_chunk = []
    
    for line in content.splitlines():
        line = line.strip()
        if not line:
            if current_chunk:
                chunks.append(" ".join(current_chunk))
                current_chunk = []
            continue
        if line.startswith("#"):
            if current_chunk:
                chunks.append(" ".join(current_chunk))
                current_chunk = []
            # Skip pure headers, but keep header + content together
            continue
        # Skip list markers for cleaner text
        if line.startswith("- "):
            line = line[2:]
        if line.startswith("* "):
            line = line[2:]
        current_chunk.append(line)
    
    if current_chunk:
        chunks.append(" ".join(current_chunk))
    
    # Filter out very short chunks
    chunks = [c for c in chunks if len(c) > 10]
    
    print(f"  {CYAN}→{RESET} Ingesting {len(chunks)} chunks from {filepath.name}\n")
    
    added = 0
    for i, chunk in enumerate(chunks, 1):
        try:
            result = m.add(chunk, user_id=args.user)
            events = result.get("results", []) if result else []
            event_types = [r.get("event", "?") for r in events]
            print(f"  {GREEN}✓{RESET} [{i}/{len(chunks)}] {chunk[:80]}{'...' if len(chunk) > 80 else ''}")
            if event_types:
                print(f"    {DIM}events: {', '.join(event_types)}{RESET}")
            added += 1
        except Exception as e:
            print(f"  {RED}✗{RESET} [{i}/{len(chunks)}] Failed: {e}")
    
    print(f"\n  {BOLD}Ingested {added}/{len(chunks)} chunks{RESET}")
    return 0

# ═══════════════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        prog="openclaw-memo",
        description="OpenClaw Mem0 Integration — Persistent Memory Layer",
    )
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="Add a memory")
    p_add.add_argument("text", help="Memory text to add")
    p_add.add_argument("--user", default="your_user", help="User ID (default: your_user)")

    # search
    p_search = sub.add_parser("search", help="Search memories")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--user", default="your_user", help="User ID (default: your_user)")
    p_search.add_argument("--limit", type=int, default=5, help="Max results")

    # list
    p_list = sub.add_parser("list", help="List all memories")
    p_list.add_argument("--user", default="your_user", help="User ID (default: your_user)")

    # delete
    p_del = sub.add_parser("delete", help="Delete a memory by ID")
    p_del.add_argument("memory_id", help="Memory ID to delete")

    # reset
    p_reset = sub.add_parser("reset", help="Delete all memories for a user")
    p_reset.add_argument("--user", default="your_user", help="User ID (default: your_user)")
    p_reset.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    # status
    sub.add_parser("status", help="Check system status")

    # ingest
    p_ingest = sub.add_parser("ingest", help="Ingest a markdown file as memories")
    p_ingest.add_argument("file", help="Path to markdown file")
    p_ingest.add_argument("--user", default="your_user", help="User ID (default: your_user)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    cmds = {
        "add": cmd_add,
        "search": cmd_search,
        "list": cmd_list,
        "delete": cmd_delete,
        "reset": cmd_reset,
        "status": cmd_status,
        "ingest": cmd_ingest,
    }
    return cmds[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
